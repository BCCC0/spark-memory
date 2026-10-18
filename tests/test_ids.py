"""Unit tests for the id rules — ``MEM-SCHEMA.md`` §2, `MEM-RESOLUTIONS.md` B5."""

from __future__ import annotations

import pytest

from mem.errors import MemError
from mem.ids import (
    RESERVED_PROJECT_NOTE,
    id_from_path,
    note_relpath,
    validate_id,
    validate_project,
)


@pytest.mark.parametrize(
    "good",
    ["note", "ops/spark-fan-ro", "a/b/c/d", "2026-10-05-session", RESERVED_PROJECT_NOTE],
)
def test_accepts_valid_ids(good):
    assert validate_id(good) == good


@pytest.mark.parametrize(
    "bad",
    [
        "Note",            # uppercase
        "my note",         # space
        "my_note",         # underscore
        "my\u2014note",    # em dash
        "-leading",
        "trailing-",
        "a//b",
        "",
    ],
)
def test_rejects_invalid_ids(bad):
    with pytest.raises(MemError) as info:
        validate_id(bad)
    assert info.value.code == "schema_invalid"
    assert info.value.exit_code == 4


def test_rejects_overlong_segments():
    with pytest.raises(MemError):
        validate_id("x" * 81)  # filename limit is 80
    with pytest.raises(MemError):
        validate_id("y" * 41 + "/ok")  # directory limit is 40
    # Exactly at the limits is fine.
    assert validate_id("x" * 80)
    assert validate_id("y" * 40 + "/ok")


def test_strips_surrounding_slashes_and_backslashes():
    assert validate_id("/ops/note/") == "ops/note"
    assert validate_id("ops\\note") == "ops/note"


def test_id_from_path_does_not_transliterate():
    assert id_from_path("ops/note.md") == "ops/note"
    assert id_from_path(".\\ops\\note.md") == "ops/note"
    # It preserves the offending characters, so validate_id can refuse them.
    assert id_from_path("My Notes/Foo Bar.md") == "My Notes/Foo Bar"
    with pytest.raises(MemError):
        validate_id(id_from_path("My Notes/Foo Bar.md"))


def test_project_name_rules():
    assert validate_project("example-project") == "example-project"
    for bad in ["Upper", "two words", "a/b", ""]:
        with pytest.raises(MemError):
            validate_project(bad)


def test_note_relpath_is_traversal_safe():
    assert note_relpath("demo", "ops/note") == "projects/demo/ops/note.md"
    with pytest.raises(MemError):
        note_relpath("demo", "../../etc/passwd")
    with pytest.raises(MemError):
        note_relpath("../demo", "note")


