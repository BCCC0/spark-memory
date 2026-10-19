"""S35 — ranged and section reads, the link graph, and marker-relative ids.

The test that matters most is `test_a_search_hits_line_range_can_be_read_back`:
`search` has always handed out a `line_range`, and nothing could consume one. That
round trip is the feature.
"""

from __future__ import annotations

import pytest

from conftest import note_text
from mem.errors import MemError
from mem.server.app import _slice_bounds

BODY = "\n".join(
    [
        "# T",  # 1
        "",  # 2
        "## A",  # 3
        "alpha content",  # 4
        "",  # 5
        "### A1",  # 6
        "nested content",  # 7
        "",  # 8
        "## B",  # 9
        "beta content",  # 10
    ]
)


# -- the pure resolver ---------------------------------------------------------


def test_a_section_runs_to_the_next_same_or_higher_heading():
    # A nested subsection belongs to its parent; the next `##` ends it.
    assert _slice_bounds(BODY, section="A", range=None) == (3, 8)
    assert _slice_bounds(BODY, section="A1", range=None) == (6, 8)
    # Nothing follows B, so it runs to the end.
    assert _slice_bounds(BODY, section="B", range=None) == (9, 10)


def test_a_section_name_is_case_insensitive():
    assert _slice_bounds(BODY, section="a", range=None) == (3, 8)


def test_a_range_is_returned_verbatim_and_never_clamped():
    assert _slice_bounds(BODY, section=None, range=(4, 4)) == (4, 4)
    for bad in ((0, 2), (5, 4), (1, 99)):
        with pytest.raises(MemError) as err:
            _slice_bounds(BODY, section=None, range=bad)
        assert err.value.code == "invalid_request"
        assert err.value.detail["line_count"] == 10


def test_a_missing_section_lists_what_exists():
    with pytest.raises(MemError) as err:
        _slice_bounds(BODY, section="Nope", range=None)
    assert err.value.code == "not_found"
    assert err.value.detail["available"] == ["T", "A", "A1", "B"]


def test_asking_for_both_a_section_and_a_range_is_refused():
    with pytest.raises(MemError) as err:
        _slice_bounds(BODY, section="A", range=(1, 2))
    assert err.value.code == "invalid_request"


# -- end to end through the daemon ---------------------------------------------


def test_a_ranged_read_returns_exactly_those_lines(ready):
    ready.write("n.md", note_text(summary="s", body="l1\nl2\nl3\nl4\nl5"))
    ready.cli_json("add", "n.md", "--as", "n")

    record = ready.cli_json("read", "n:5-6")
    assert record["range"] == [5, 6]
    assert record["body"] == "l3\nl4"


def test_a_section_read_returns_only_that_section(ready):
    ready.write("n.md", note_text(summary="s", body="## One\n\nfirst\n\n## Two\n\nsecond"))
    ready.cli_json("add", "n.md", "--as", "n")

    record = ready.cli_json("read", "n#One")
    assert "first" in record["body"]
    assert "second" not in record["body"]
    assert record["range"][0] == 3


def test_a_search_hits_line_range_can_be_read_back(ready):
    """The whole point: a coordinate from `search` is now consumable by `read`."""
    ready.write(
        "n.md", note_text(summary="needle", body="filler\nfiller\nfindme here\nfiller")
    )
    ready.cli_json("add", "n.md", "--as", "n")

    hit = ready.cli_json("search", "findme")["hits"][0]
    start, end = hit["line_range"]
    record = ready.cli_json("read", f"n:{start}-{end}")

    assert record["range"] == [start, end]
    assert "findme" in record["body"], (
        "the range search reported must land on the matched text"
    )


def test_a_slice_bypasses_the_inline_cap_and_a_whole_read_does_not(ready):
    """C1 is amended (S35 D2), not merely excepted — asserted on both halves."""
    ready.write("big.md", note_text(summary="big", body="word " * 2000))
    ready.cli_json("add", "big.md", "--as", "big")

    whole = ready.cli_json("read", "big")
    assert whole["truncated"] is True
    assert "body" not in whole

    sliced = ready.cli_json("read", "big:1-1")
    assert sliced["truncated"] is False
    assert sliced["range"] == [1, 1]
    assert "body" in sliced

    # A named slice always inlines, even when it spans more than the client's
    # advisory cap. Only a whole read may be deferred.
    huge = ready.cli_json("read", "big:3-3")
    assert huge["truncated"] is False
    assert "deferred_by_client" not in huge
    assert huge["body"]


def test_a_range_outside_the_note_is_refused_not_clamped(ready):
    ready.write("n.md", note_text(summary="s", body="one"))
    ready.cli_json("add", "n.md", "--as", "n")

    result = ready.cli("read", "n:1-999")
    assert result.returncode == 4, result.stderr
    assert "outside this note" in result.stderr


def test_a_missing_section_exits_9_and_names_what_exists(ready):
    ready.write("n.md", note_text(summary="s", body="## Real\n\nx"))
    ready.cli_json("add", "n.md", "--as", "n")

    result = ready.cli("read", "n#Nope")
    assert result.returncode == 9, result.stderr
    assert "Real" in result.stderr, "the error must list the sections that do exist"


def test_outline_refuses_a_slice(ready):
    ready.write("n.md", note_text(summary="s", body="## Real\n\nx"))
    ready.cli_json("add", "n.md", "--as", "n")
    assert ready.cli("read", "n#Real", "--outline").returncode == 64


# -- references ----------------------------------------------------------------


def test_a_read_serves_the_link_graph_and_ls_does_not(ready):
    ready.write("b.md", note_text(summary="b", body="target"))
    ready.cli_json("add", "b.md", "--as", "ops/b")
    ready.write("a.md", note_text(summary="a", body="see [b](ops/b)"))
    ready.cli_json("add", "a.md", "--as", "a")

    record = ready.cli_json("read", "a")
    assert record["links"] == ["ops/b"]
    assert not [w for w in record["warnings"] if "link ->" in w], record["warnings"]

    row = next(n for n in ready.cli_json("ls")["notes"] if n["id"] == "a")
    assert "links" not in row, "`links` is a read-only field: `ls` must not grow it"


def test_a_link_to_a_missing_section_warns(ready):
    ready.write("b.md", note_text(summary="b", body="## Real\n\nx"))
    ready.cli_json("add", "b.md", "--as", "ops/b")
    ready.write("a.md", note_text(summary="a", body="see [b](ops/b#Nope)"))
    ready.cli_json("add", "a.md", "--as", "a")

    warnings = ready.cli_json("read", "a")["warnings"]
    assert any("no such section" in w for w in warnings), warnings


def test_a_link_to_an_existing_section_does_not_warn(ready):
    ready.write("b.md", note_text(summary="b", body="## Real\n\nx"))
    ready.cli_json("add", "b.md", "--as", "ops/b")
    ready.write("a.md", note_text(summary="a", body="see [b](ops/b#Real)"))
    ready.cli_json("add", "a.md", "--as", "a")

    warnings = ready.cli_json("read", "a")["warnings"]
    assert not [w for w in warnings if "section" in w], warnings


def test_a_relative_path_target_is_reported_not_resolved(ready):
    """`./x` is relative to something nothing here can know (S35 D3)."""
    ready.write("a.md", note_text(summary="a", body="see [b](../b)"))
    ready.cli_json("add", "a.md", "--as", "a")

    warnings = ready.cli_json("read", "a")["warnings"]
    assert any("relative path" in w for w in warnings), warnings
    assert not any("does not exist" in w for w in warnings), (
        "it must not masquerade as a missing note"
    )


# -- D7: one file, one id ------------------------------------------------------


def test_the_same_file_added_from_two_directories_gets_one_id(ready):
    """B5 said "client-relative", which the code read as the *pwd* (S35 D7)."""
    ready.write("deep/x.md", note_text(summary="x", body="body"))
    first = ready.cli_json("add", "deep/x.md", cwd=ready.workdir)["id"]

    ready.write("deep/x.md", note_text(summary="x", body="body changed"))
    second = ready.cli_json("add", "x.md", cwd=ready.workdir / "deep")["id"]

    assert first == second == "deep/x"


def test_a_file_outside_the_marker_is_refused_not_renamed_to_its_basename(ready, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    ready.write("stray.md", note_text(summary="stray"), directory=outside)

    result = ready.cli("add", str(outside / "stray.md"))
    assert result.returncode == 4, result.stderr
    assert "origin_outside_root" in result.stderr
    assert "outside the project root" in result.stderr


def test_a_large_slice_is_not_stripped_by_the_client_cap(ready):
    ready.write("huge-slice.md", note_text(summary="huge", body="x" * 9000))
    ready.cli_json("add", "huge-slice.md", "--as", "huge-slice")

    record = ready.cli_json("read", "huge-slice:3-3")
    assert record["truncated"] is False
    assert record["body"] == "x" * 9000
    assert "deferred_by_client" not in record


def test_a_slice_reports_freshness_against_the_whole_note(ready):
    text = note_text(summary="slice-fresh", body="one\ntwo\nthree\nfour")
    source = ready.write("slice-fresh.md", text)
    source.write_text(text, encoding="utf-8", newline="\n")
    ready.cli_json("add", "slice-fresh.md", "--as", "slice-fresh")

    record = ready.cli_json("read", "slice-fresh:4-5")
    assert record["body"] == "two\nthree"
    assert record["local_file"] is True
    assert record["body_matches"] is True, "the whole working file still matches"
    assert record["source"] == "same"
    assert record["origin_path"] == str(source)
    assert "body_sha256" not in record
    assert "local_sha256" not in record

    source.write_text(
        note_text(summary="slice-fresh", body="one\ntwo changed\nthree\nfour"),
        encoding="utf-8",
        newline="\n",
    )
    changed = ready.cli_json("read", "slice-fresh:4-5")
    assert changed["body_matches"] is False
    assert changed["source"] in {"local", "server"}
    assert changed["body"] == "two\nthree", "the slice itself is unchanged"
