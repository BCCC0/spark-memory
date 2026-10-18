"""Query semantics: prefix matching and the one-shot relaxed retry (`S32`).

The rules are taken from Basic Memory's SQLite path, read from its source rather than
its docs. Most of these are pure, because the rules live in two renderers; the
end-to-end cases use the daemon to prove the retry is reachable and — the part that
matters — that a loosened result is **marked**.
"""

from __future__ import annotations

from conftest import note_text
from mem.server.search import SearchIndex, _fts_query, _relaxed_query


def test_text_index_uses_wal_and_a_busy_timeout(tmp_path):
    index = SearchIndex(tmp_path / "search.db")
    with index._connect() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 10000


def test_a_writer_does_not_exclude_a_reader(tmp_path):
    index = SearchIndex(tmp_path / "search.db")
    with index._connect() as writer:
        writer.execute("BEGIN EXCLUSIVE")
        writer.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('probe', '1')")
        with index._connect() as reader:
            assert reader.execute("SELECT COUNT(*) FROM meta").fetchone()[0] >= 0
        writer.rollback()

# -- the strict renderer -------------------------------------------------------


def test_terms_are_required_and_prefix_matched():
    assert _fts_query("fan control") == '"fan"* AND "control"*'


def test_quoting_the_input_changes_nothing():
    """Quotes are not phrase syntax here: the tokeniser strips them."""
    assert _fts_query('"fan control"') == _fts_query("fan control")


def test_a_hyphenated_token_stays_one_quoted_term():
    assert _fts_query("fan-control") == '"fan-control"*'


def test_an_empty_query_renders_nothing():
    assert _fts_query("   ") is None


# -- the relaxed renderer ------------------------------------------------------


def test_relaxation_needs_three_terms():
    assert _relaxed_query("fan control") is None, "two terms is already narrow"
    assert _relaxed_query("spark fan control") is not None


def test_relaxation_refuses_a_quoted_query():
    assert _relaxed_query('"spark fan control"') is None


def test_relaxation_refuses_a_numeric_term():
    assert _relaxed_query("spark 42 control") is None
    assert _relaxed_query("spark 1 2 control") is None


def test_relaxation_prunes_stopwords_and_ors_the_rest():
    relaxed = _relaxed_query("what is the fan control verdict")
    assert relaxed is not None
    assert " OR " in relaxed
    assert '"what"*' not in relaxed and '"the"*' not in relaxed
    assert '"fan"*' in relaxed and '"verdict"*' in relaxed


def test_relaxation_gives_up_when_every_term_is_a_stopword():
    assert _relaxed_query("what is the it") is None


# -- end to end through the daemon ---------------------------------------------


def test_a_prefix_query_finds_a_longer_word(ready):
    """`token` must find `tokenization` — the recall fix that S32 adopts."""
    ready.write("n.md", note_text(summary="prefix", body="tokenization is the trick"))
    ready.cli_json("add", "n.md", "--as", "n")

    hits = ready.cli_json("search", "token")["hits"]
    assert [hit["id"] for hit in hits] == ["n"]


def test_a_three_term_query_that_misses_is_retried_and_marked(ready):
    """Strict AND returns nothing, so it retries — and says that it did.

    This is the exact shape that made the workload audit's headline query fail:
    `fan control` matched, and adding a third absent term made it match nothing.
    """
    ready.write("n.md", note_text(summary="fan control", body="the fan control verdict"))
    ready.cli_json("add", "n.md", "--as", "n")

    payload = ready.cli_json("search", "fan control zzzznotaterm")
    assert payload["hits"], "the looser retry should rescue this query"
    assert payload.get("relaxed") is True, "and the caller must be told"
    assert all(hit.get("relaxed") for hit in payload["hits"])
    assert [hit["id"] for hit in payload["hits"]] == ["n"]


def test_a_query_that_matches_strictly_is_not_marked(ready):
    ready.write("n.md", note_text(summary="fan control", body="the fan control verdict"))
    ready.cli_json("add", "n.md", "--as", "n")

    payload = ready.cli_json("search", "fan control")
    assert payload["hits"]
    assert "relaxed" not in payload
    assert not any(hit.get("relaxed") for hit in payload["hits"])


def test_a_relaxed_hit_cannot_escape_the_callers_filters(ready):
    """Only the MATCH text is loosened; the WHERE clause is untouched."""
    ready.write("n.md", note_text(summary="alpha", status="draft", body="fan control alpha"))
    ready.cli_json("add", "n.md", "--as", "n")

    payload = ready.cli_json("search", "fan control zzzznotaterm", "--status", "done")
    assert payload["hits"] == [], "the retry must not widen the filter"
    assert "relaxed" not in payload
