"""Batch git reads and the derived catalog.

These pin the two properties the performance work depends on: a batch read
agrees with `git show`, and the catalog is derivable from git alone (so it can
be deleted and rebuilt at any time).
"""

from __future__ import annotations

from mem.server.catalog import Catalog
from mem.server.repo import MemoryRepo

from conftest import note_text


def _seed(repo: MemoryRepo, name: str, summary: str) -> str:
    path = f"projects/demo/{name}.md"
    target = repo.root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"---\nsummary: {summary}\n---\n\n# {summary}\n\nbody\n", encoding="utf-8")
    return path


def test_cat_file_batch_matches_git_show(tmp_path):
    repo = MemoryRepo(tmp_path / "repo")
    repo.ensure()
    first = _seed(repo, "alpha", "Alpha")
    second = _seed(repo, "beta", "Beta")
    repo.commit_paths("add two", [first, second])

    bodies = repo.read_notes([first, second, "projects/demo/missing.md"])
    assert bodies[first] == repo.note_bytes(first)
    assert bodies[second] == repo.note_bytes(second)
    assert bodies["projects/demo/missing.md"] is None


def test_note_bytes_raises_for_a_missing_path(tmp_path):
    repo = MemoryRepo(tmp_path / "repo")
    repo.ensure()
    missing = "projects/demo/nope.md"
    try:
        repo.note_bytes(missing)
    except Exception as exc:  # noqa: BLE001 - the typed error is what matters
        assert getattr(exc, "code", "") == "not_found"
    else:  # pragma: no cover
        raise AssertionError("note_bytes returned for a missing path")


def test_catalog_ensure_tracks_add_and_delete(tmp_path):
    repo = MemoryRepo(tmp_path / "repo")
    repo.ensure()
    first = _seed(repo, "alpha", "Alpha")
    repo.commit_paths("add alpha", [first])

    catalog = Catalog(tmp_path / "catalog.db")
    catalog.ensure(repo, repo.head())
    assert {row["note_id"] for row in catalog.rows()} == {"alpha"}

    second = _seed(repo, "beta", "Beta")
    repo.commit_paths("add beta", [second])
    catalog.ensure(repo, repo.head())
    assert {row["note_id"] for row in catalog.rows()} == {"alpha", "beta"}

    (repo.root / second).unlink()
    repo.commit_paths("remove beta", [second])
    catalog.ensure(repo, repo.head())
    assert {row["note_id"] for row in catalog.rows()} == {"alpha"}


def test_catalog_rebuild_equals_incremental(tmp_path):
    repo = MemoryRepo(tmp_path / "repo")
    repo.ensure()
    first = _seed(repo, "alpha", "Alpha")
    repo.commit_paths("add alpha", [first])
    second = _seed(repo, "beta", "Beta")
    repo.commit_paths("add beta", [second])

    incremental = Catalog(tmp_path / "incremental.db")
    incremental.ensure(repo, repo.head())
    rebuilt = Catalog(tmp_path / "rebuilt.db")
    rebuilt.rebuild(repo, repo.head())

    def rows(catalog: Catalog) -> list[tuple]:
        return sorted(
            (row["project"], row["note_id"], row["summary"], row["size"])
            for row in catalog.rows()
        )

    assert rows(incremental) == rows(rebuilt)


def test_catalog_drops_a_row_that_stops_parsing(tmp_path):
    repo = MemoryRepo(tmp_path / "repo")
    repo.ensure()
    path = _seed(repo, "alpha", "Alpha")
    repo.commit_paths("add alpha", [path])
    catalog = Catalog(tmp_path / "catalog.db")
    catalog.ensure(repo, repo.head())
    assert {row["note_id"] for row in catalog.rows()} == {"alpha"}

    (repo.root / path).write_text("---\nsummary: [unclosed\n---\nbody\n", encoding="utf-8")
    repo.commit_paths("break alpha", [path])
    catalog.ensure(repo, repo.head())
    assert {row["note_id"] for row in catalog.rows()} == set(), catalog.rows()


def test_listing_a_prefix_with_full_does_not_crash(ready):
    for name in ("one", "two"):
        ready.write(f"{name}.md", note_text(summary=name.title()))
        assert ready.cli("add", f"{name}.md", "--as", f"sub/a/{name}").returncode == 0
    ready.flush()
    listing = ready.cli_json("ls", "sub", "--full")
    # A prefix listing returns the direct child topic, not the leaves; the
    # regression was the `--full` path crashing on that topic record.
    ids = {row["id"] for row in listing["notes"]}
    assert "sub/a" in ids


def test_listing_uses_catalog_fields(ready):
    ready.write("catalog.md", note_text(summary="Catalog row"))
    assert ready.cli("add", "catalog.md").returncode == 0
    ready.flush()
    listing = ready.cli_json("ls")
    row = next(r for r in listing["notes"] if r["id"] == "catalog")
    assert row["summary"] == "Catalog row"
    assert row["status"] == "active"
    assert row["kind"] == "note"
