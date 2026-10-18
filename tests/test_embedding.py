"""MVP-1: the semantic index, incremental sync, and hybrid search.

The chunking tests are pure and always run. The index tests need the `embed`
extra (and download bge-small on first use). The corpus test uses the **real 20
notes** from Basic Memory when they are present — see `MEM_TEST_CORPUS`.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from conftest import note_text
from mem.server import embedding

_corpus_env = os.environ.get("MEM_TEST_CORPUS")
CORPUS = Path(_corpus_env) if _corpus_env else None

# The pure chunking tests always run. Everything that *embeds* needs bge-small,
# which has to be fetched once. Those tests are opt-in; the real-corpus test also
# needs MEM_TEST_CORPUS to name a local corpus.
needs_embed = pytest.mark.skipif(
    not embedding.available() or not os.environ.get("MEM_TEST_EMBED"),
    reason="needs the `embed` extra AND a cached model (set MEM_TEST_EMBED=1)",
)


# -- chunking: pure, no model ------------------------------------------------


def test_bullets_get_their_own_vector():
    """Basic Memory's comment: separate bullets are individual facts."""
    text = "intro para\n\n- first fact\n- second fact\n\nclosing para\n"
    texts = [c.text for c in embedding.chunk_markdown(text)]
    first = [t for t in texts if t.startswith("- first fact")]
    assert first, f"no standalone bullet chunk in {texts}"
    assert len(first) == 1 and "second" not in first[0], (
        "a bullet must not be merged with the next one"
    )
    assert any(t.startswith("- second fact") for t in texts)


def test_headings_are_boundaries_once_the_limit_forces_a_split():
    """Small sections legitimately merge; a heading starts a chunk when it must."""
    big = "x" * 600  # two of these cannot share one 900-char chunk
    text = f"# Title\n\n{big}\n\n## Section\n\n{big}\n"
    texts = [c.text for c in embedding.chunk_markdown(text)]
    assert len(texts) >= 2, f"expected a split, got {len(texts)} chunk(s)"
    assert any(t.startswith("# Title") for t in texts)
    assert any(t.startswith("## Section") for t in texts)


def test_small_adjacent_sections_merge_up_to_the_limit():
    text = "\n\n".join(f"para {i}" for i in range(5))
    chunks = embedding.chunk_markdown(text)
    assert len(chunks) == 1
    assert len(chunks[0].text) <= embedding.CHUNK_CHARS


def test_oversized_section_is_split_with_overlap():
    text = "## Big\n\n" + ("word " * 400)
    chunks = embedding.chunk_markdown(text)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk.text) <= embedding.CHUNK_CHARS + 10


def test_an_oversized_section_splits_on_paragraph_boundaries():
    """Structure first, characters only as a fallback — Basic Memory's real rule.

    Each paragraph is 40 words, so a chunk that cut mid-paragraph would hold a word
    count that is not a multiple of 40. mem used to char-window the whole section,
    which is exactly what this rejects.
    """
    paragraph = ("sentence " * 40).strip()
    text = "## Big\n\n" + "\n\n".join([paragraph] * 3) + "\n"
    chunks = embedding.chunk_markdown(text)

    assert len(chunks) > 1, "three 359-char paragraphs cannot share one 900-char chunk"
    for chunk in chunks:
        assert chunk.text.count("sentence") % 40 == 0, (
            f"a chunk was cut mid-paragraph: {chunk.text[:60]!r}"
        )


def test_bullets_stay_whole_inside_an_oversized_section():
    """Basic Memory's comment: separate bullets are individual facts."""
    facts = [f"- fact {index} " + "y" * 200 for index in range(6)]
    text = "## Big\n\n" + "\n".join(facts) + "\n"
    chunks = embedding.chunk_markdown(text)

    bullet_lines = [
        line
        for chunk in chunks
        for line in chunk.text.splitlines()
        if line.startswith("- ")
    ]
    assert len(bullet_lines) == 6, f"a bullet was cut or duplicated: {bullet_lines}"
    assert bullet_lines == facts, "bullets must survive whole, in order"


def test_only_a_still_oversized_paragraph_gets_the_overlap():
    """The 120-char overlap is not global; it exists for one unbreakable block."""
    text = "## Big\n\n" + ("z" * 1500)
    chunks = embedding.chunk_markdown(text)
    windows = [chunk.text for chunk in chunks if chunk.text.startswith("z")]

    assert len(windows) > 1, "a 1500-char paragraph must be windowed"
    assert (
        windows[0][-embedding.OVERLAP_CHARS :] == windows[1][: embedding.OVERLAP_CHARS]
    ), "consecutive windows of one paragraph share the documented overlap"


def test_the_title_is_part_of_a_chunks_identity():
    """The embedded text is title + locator + chunk, so a retitle re-embeds (S32).

    The *stored* text is unchanged — only the identity moves — which is what makes a
    retitled note re-embed instead of keeping a vector that no longer describes it.
    """
    body = "# T\n\nalpha\n"
    plain = embedding.chunk_markdown(body)
    titled = embedding.chunk_markdown(body, context="T\ndemo/n")

    assert plain[0].text == titled[0].text, "the stored chunk text is unchanged"
    assert plain[0].source_hash != titled[0].source_hash
    assert embedding.note_fingerprint(plain) != embedding.note_fingerprint(titled)


def test_ordinals_are_contiguous_and_hashes_are_per_chunk():
    chunks = embedding.chunk_markdown("# T\n\n## A\n\none\n\n## B\n\ntwo\n")
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))
    assert len({c.source_hash for c in chunks}) == len(chunks)


def test_fingerprint_is_stable_and_content_sensitive():
    a = embedding.chunk_markdown("# T\n\nalpha\n")
    b = embedding.chunk_markdown("# T\n\nalpha\n")
    c = embedding.chunk_markdown("# T\n\nbeta\n")
    assert embedding.note_fingerprint(a) == embedding.note_fingerprint(b)
    assert embedding.note_fingerprint(a) != embedding.note_fingerprint(c)


def test_empty_body_yields_no_chunks():
    assert embedding.chunk_markdown("") == []
    assert embedding.chunk_markdown("   \n  ") == []


# -- incremental sync: the point of the whole design -------------------------


def _repo_with(tmp_path: Path, notes: dict[str, str]):
    from mem.server.repo import MemoryRepo

    repo = MemoryRepo(tmp_path / "memory")
    repo.ensure()
    for note_id, text in notes.items():
        rel = f"projects/demo/{note_id}.md"
        repo.write_note(rel, text.encode())
        repo.commit_paths(f"add {note_id}", [rel], device_id="d1")
    return repo


def _sync(vectors, repo, head):
    return vectors.sync(repo.notes(), lambda path: _source(repo, path), head or repo.head())


def _source(repo, path):
    """What the index reads from a note (S32): title, body, and the filter metadata."""
    from mem.server import frontmatter as fm

    data, body = fm.parse(repo.read_note(path))
    tags = data.get("tags")
    return embedding.NoteSource(
        title=fm.title_of(body) or "",
        body=body,
        status=data.get("status"),
        note_type=data.get("type"),
        tags=[str(t) for t in tags] if isinstance(tags, list) else None,
    )


@needs_embed
@pytest.mark.real_embedder
def test_unchanged_corpus_costs_nothing(tmp_path):
    repo = _repo_with(
        tmp_path,
        {
            "a": note_text(summary="a", body="alpha content here"),
            "b": note_text(summary="b", body="beta content here"),
        },
    )
    vectors = embedding.VectorIndex(tmp_path / "vectors.db")

    first = _sync(vectors, repo, repo.head())
    assert first["embedded"] > 0, "the first sync must embed"
    assert first["skipped"] == 0

    second = _sync(vectors, repo, repo.head())
    assert second["embedded"] == 0, "an unchanged corpus must embed NOTHING"
    assert second["skipped"] == 2


@needs_embed
@pytest.mark.real_embedder
def test_only_changed_chunks_are_re_embedded(tmp_path):
    """The assertion the design exists to satisfy."""
    from mem.server.repo import MemoryRepo

    notes = {
        "a": note_text(summary="a", body="## One\n\nalpha original\n\n## Two\n\nalpha second\n"),
        "b": note_text(summary="b", body="## One\n\nbeta original\n\n## Two\n\nbeta second\n"),
    }
    repo = _repo_with(tmp_path, notes)
    vectors = embedding.VectorIndex(tmp_path / "vectors.db")
    _sync(vectors, repo, repo.head())

    # Change ONE section of ONE note.
    repo.write_note(
        "projects/demo/a.md",
        note_text(summary="a", body="## One\n\nalpha EDITED\n\n## Two\n\nalpha second\n").encode(),
    )
    repo.commit_paths("edit a", ["projects/demo/a.md"], device_id="d1")

    stats = _sync(vectors, repo, repo.head())
    assert stats["embedded"] == 1, f"only the edited chunk should re-embed: {stats}"
    assert stats["skipped"] == 1, "the untouched note must be skipped entirely"
    assert stats["deleted"] == 0


@needs_embed
@pytest.mark.real_embedder
def test_a_removed_note_is_dropped_from_the_index(tmp_path):
    repo = _repo_with(tmp_path, {"a": note_text(summary="a", body="alpha")})
    vectors = embedding.VectorIndex(tmp_path / "vectors.db")
    _sync(vectors, repo, repo.head())
    assert vectors.status()["chunks"] > 0

    repo.remove_note("projects/demo/a.md")
    repo.commit_paths("rm a", ["projects/demo/a.md"], device_id="d1")

    stats = _sync(vectors, repo, repo.head())
    assert stats["deleted"] > 0
    assert vectors.status()["chunks"] == 0


@needs_embed
@pytest.mark.real_embedder
def test_rebuild_ignores_the_existing_index(tmp_path):
    repo = _repo_with(tmp_path, {"a": note_text(summary="a", body="alpha")})
    vectors = embedding.VectorIndex(tmp_path / "vectors.db")
    _sync(vectors, repo, repo.head())

    stats = vectors.rebuild(repo.notes(), lambda p: _source(repo, p), repo.head())
    assert stats["embedded"] > 0
    assert stats["skipped"] == 0


# -- hybrid search, end to end through the daemon ----------------------------


@needs_embed
@pytest.mark.real_embedder
def test_hybrid_finds_a_paraphrase_that_text_search_misses(ready):
    """The reason to have embeddings at all: no shared vocabulary."""
    ready.write(
        "ops/cooling.md",
        note_text(
            summary="cooling the accelerator",
            body="The chassis gets warm under sustained inference, so airflow matters.",
        ),
    )
    ready.cli_json("add", "ops/cooling.md", "--as", "cooling")
    # A read no longer triggers the sync, so wait for the write path's background pass
    # instead of assuming the search did the work (S32).
    ready.wait_for_vectors()

    # No word overlap with the note.
    text = ready.cli_json("search", "thermal management")
    hybrid = ready.cli_json("search", "thermal management", "--mode", "hybrid")

    assert hybrid["mode"] == "hybrid"
    assert "cooling" in [h["id"] for h in hybrid["hits"]], (
        f"the semantic half should find it; text returned {[h['id'] for h in text['hits']]}"
    )


@needs_embed
@pytest.mark.real_embedder
def test_hybrid_reports_which_half_matched(ready):
    ready.write("a.md", note_text(summary="alpha", body="zebra token"))
    ready.cli_json("add", "a.md", "--as", "a")
    ready.wait_for_vectors()
    hits = ready.cli_json("search", "zebra", "--mode", "hybrid")["hits"]
    assert hits
    assert set(hits[0]["matched_by"]) <= {"text", "vector"}
    assert "text" in hits[0]["matched_by"], "an exact term must be found by bm25 too"


@needs_embed
@pytest.mark.real_embedder
def test_index_status_and_rebuild(ready):
    ready.write("a.md", note_text(summary="alpha", body="alpha"))
    ready.cli_json("add", "a.md", "--as", "a")
    # A search used to force the sync; now the write path schedules it (S32).
    ready.wait_for_vectors()

    status = ready.cli_json("index")
    assert status["kind"] == "hybrid"
    assert status["hybrid"]["available"] is True
    assert status["hybrid"]["chunks"] >= 1
    assert status["hybrid"]["indexed_head"]

    rebuilt = ready.cli_json("index", "--rebuild")
    assert rebuilt["rebuilt"] is True
    assert rebuilt["skipped"] == 0


def test_unknown_mode_is_still_refused(ready):
    result = ready.cli("search", "x", "--mode", "fuzzy")
    assert result.returncode == 64, "an unknown rung must raise, never fall back"


# -- the real corpus ---------------------------------------------------------


@pytest.mark.skipif(CORPUS is None or not CORPUS.is_dir(), reason="real corpus not configured")
@needs_embed
@pytest.mark.real_embedder
def test_the_real_corpus_indexes_and_retrieves(tmp_path):
    """Index the actual 20 Basic Memory notes — the only honest scale check.

    They are copied in read-only; nothing is migrated, and their frontmatter is
    *parsed* but never validated, so the fact that every id violates
    `MEM-SCHEMA.md` §2 is deliberately irrelevant here.
    """
    from mem.server.repo import MemoryRepo

    assert CORPUS is not None
    sources = sorted(CORPUS.glob("*/*.md"))
    assert len(sources) >= 20, f"expected the real corpus, found {len(sources)}"

    repo = MemoryRepo(tmp_path / "memory")
    repo.ensure()
    copied = []
    for index, source in enumerate(sources):
        rel = f"projects/legacy/note-{index:02d}.md"
        repo.write_note(rel, source.read_bytes())
        copied.append(rel)
    repo.commit_paths("import legacy corpus", copied, device_id="test")

    vectors = embedding.VectorIndex(tmp_path / "vectors.db")
    stats = _sync(vectors, repo, repo.head())
    assert stats["notes"] == len(copied)
    assert stats["chunks"] > stats["notes"], "long notes must chunk into several pieces"

    # A query whose vocabulary is certainly in the corpus.
    hits = vectors.search("secure boot kernel module signing", limit=5)
    assert hits, "expected semantic hits in the real corpus"
    assert hits[0]["line_range"][0] >= 1

    # And the incremental property at real scale.
    again = _sync(vectors, repo, repo.head())
    assert again["embedded"] == 0
    assert again["skipped"] == len(copied)
