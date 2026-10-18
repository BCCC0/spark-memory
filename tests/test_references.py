"""Reference syntax: what mem parses, and what it refuses to ignore (`S34`).

The Basic Memory corpus used `[[wikilinks]]` for **every** one of its
cross-references — 3 links, 0 markdown ones — and mem parses none of them. Before
this, all three were invisible: real references that no verb, warning or error could
mention. These tests pin the reporting, not a resolution feature: mem deliberately
does not resolve wikilinks (see `S34`).
"""

from __future__ import annotations

from conftest import note_text
from mem.server import frontmatter as fm


def test_a_wikilink_is_reported_rather_than_ignored():
    warning = fm.content_warnings({}, "# T\n\nsee [[Some Other Note]] for detail\n")
    assert any("[[Some Other Note]]" in w for w in warning), warning
    assert any("not parsed" in w for w in warning), warning


def test_a_markdown_link_is_not_warned_about():
    """The supported syntax must stay silent — only the unsupported one speaks."""
    warnings = fm.content_warnings({}, "# T\n\nsee [other](ops/other) for detail\n")
    assert not [w for w in warnings if "wikilink" in w], warnings


def test_a_body_with_no_links_produces_no_link_warning():
    warnings = fm.content_warnings({}, "# T\n\nno references here\n")
    assert not [w for w in warnings if "wikilink" in w], warnings


def test_a_wikilink_is_still_not_a_parsed_link():
    """Reporting it must not quietly turn it into a link — it is not resolved."""
    assert fm.note_links("# T\n\nsee [[Some Other Note]]\n") == []


def test_the_warning_reaches_a_read_record(ready):
    """Both `read` and `ls` call `content_warnings`, so one fix covers both."""
    ready.write(
        "n.md",
        note_text(summary="has a wikilink", body="see [[Another Note]] for detail"),
    )
    ready.cli_json("add", "n.md", "--as", "n")

    record = ready.cli_json("read", "n")
    assert any("not parsed" in w for w in record["warnings"]), record["warnings"]

    listed = ready.cli_json("ls")
    row = next(r for r in listed["notes"] if r["id"] == "n")
    assert any("not parsed" in w for w in row["warnings"]), row["warnings"]
