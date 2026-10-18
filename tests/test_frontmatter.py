"""Unit tests for frontmatter validation — ``MEM-SCHEMA.md`` §3 and §6."""

from __future__ import annotations

import pytest

from mem.errors import MemError
from mem.server import frontmatter as fm


def fm_text(**overrides) -> str:
    data = {
        "summary": "a fine summary",
        "status": "active",
        "type": "reference",
    }
    data.update(overrides)
    lines = [f"{k}: {v}" for k, v in data.items() if v is not None]
    return "---\n" + "\n".join(lines) + "\n---\n\n# Title\n\nprose\n"


def test_valid_note_passes():
    data, body, warnings = fm.require_valid(fm_text(), "note")
    assert data["type"] == "reference"
    assert fm.title_of(body) == "Title"
    assert warnings == []


def test_missing_summary_is_rejected_on_upload():
    text = "---\nstatus: active\ntype: reference\n---\n\n# T\n"
    with pytest.raises(MemError, match="summary"):
        fm.require_valid(text, "note")


def test_yaml_wrapped_summary_is_one_line_value():
    text = "---\nsummary: 'A description that YAML\n  wraps onto another source line.'\n---\n# T\n"
    data, _, _ = fm.require_valid(text, "note")
    assert data["summary"] == "A description that YAML wraps onto another source line."


def test_summary_length_and_newline_rules():
    with pytest.raises(MemError):
        fm.require_valid(fm_text(summary="x" * 201), "note")
    fm.require_valid(fm_text(summary="x" * 200), "note")  # exactly at the limit
    with pytest.raises(MemError):
        fm.validate({"summary": "a\nb", "status": "active", "type": "reference"}, "note")


def test_derived_keys_are_rejected():
    text = fm_text() + ""
    text = text.replace("---\n\n# Title", "permalink: somewhere\n---\n\n# Title")
    with pytest.raises(MemError) as info:
        fm.require_valid(text, "note")
    assert "derived" in info.value.message


def test_unknown_keys_are_rejected():
    text = "---\nsummary: s\nstatus: active\ntype: reference\nbogus: 1\n---\n\n# T\n"
    with pytest.raises(MemError) as info:
        fm.require_valid(text, "note")
    assert "unknown frontmatter key" in info.value.message


def test_type_and_status_enums():
    with pytest.raises(MemError):
        fm.require_valid(fm_text(type="note"), "note")  # 'note' is not in the enum
    with pytest.raises(MemError):
        fm.require_valid(fm_text(status="wip"), "note")


def test_superseded_requires_superseded_by():
    with pytest.raises(MemError) as info:
        fm.require_valid(fm_text(status="superseded"), "note")
    assert "superseded_by" in info.value.message
    fm.require_valid(
        fm_text(status="superseded", superseded_by="ops/other"), "note"
    )


def test_invalid_id_is_an_error():
    with pytest.raises(MemError):
        fm.require_valid(fm_text(), "Bad Id")


def test_missing_section_is_only_a_warning():
    data, body, warnings = fm.require_valid(fm_text(type="adr"), "note")
    assert any("Context" in w for w in warnings)
    assert data["type"] == "adr"


def test_no_heading_is_a_warning():
    text = "---\nsummary: s\nstatus: active\ntype: reference\n---\nprose only\n"
    _, _, warnings = fm.require_valid(text, "note")
    assert any("heading" in w for w in warnings)


def test_parse_without_frontmatter_returns_empty_mapping():
    data, body = fm.parse("# Just a heading\n")
    assert data == {}
    assert body.startswith("# Just a heading")


def test_unterminated_frontmatter_is_an_error():
    with pytest.raises(MemError):
        fm.parse("---\nsummary: s\n")


def test_inject_round_trips_and_preserves_the_body():
    data, body = fm.parse(fm_text())
    rendered = fm.inject(data, body, {"origin": {"device": "d1", "path": "C:/x.md"}})
    again, body_again = fm.parse(rendered)
    assert again["origin"] == {"device": "d1", "path": "C:/x.md"}
    assert body_again.strip() == body.strip()


def test_build_produces_a_valid_note():
    text = fm.build(
        title="Demo", summary="demo project", status="active", note_type="project",
        body="## Scope\n\nstuff\n",
    )
    data, body, warnings = fm.require_valid(text, "PROJECT")
    assert data["type"] == "project"
    assert fm.title_of(body) == "Demo"
    assert warnings == []
