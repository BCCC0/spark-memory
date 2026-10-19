"""The authoring path must stay inside the project root.

A note's ``origin`` is a link back to the file the author edited. A path from
``%TEMP%``, a sibling directory, a symlink escape, or a UNC share is not a
usable link for this project; it must be refused before upload rather than
baked into the committed note.
"""

from __future__ import annotations

import json
import os

import pytest

from conftest import note_text


def _outside(tmp_path, name="outside.md"):
    outside = tmp_path / "outside"
    outside.mkdir()
    return outside / name


def _error(result):
    assert result.returncode == 4, result.stderr
    return json.loads(result.stderr)["error"]


def test_an_outside_origin_is_refused_even_with_an_explicit_id(ready, tmp_path):
    source = _outside(tmp_path)
    source.write_text(note_text(summary="outside"), encoding="utf-8")

    result = ready.cli("add", str(source), "--as", "outside")
    error = _error(result)
    assert error["code"] == "origin_outside_root"
    assert str(source) in error["message"]
    assert "outside" not in [row["id"] for row in ready.cli_json("manifest")["entries"]]


def test_no_origin_allows_outside_content_with_an_explicit_id(ready, tmp_path):
    source = _outside(tmp_path)
    source.write_text(note_text(summary="outside-no-link"), encoding="utf-8")

    added = ready.cli_json("add", str(source), "--as", "outside-no-link", "--no-origin")
    assert added["content_stored"] is True
    record = ready.cli_json("read", "outside-no-link")
    assert record["body"]
    assert record["local_file"] is False
    assert record["source"] == "none"
    assert "origin_path" not in record


def test_no_origin_inside_the_root_also_omits_the_link(ready):
    source = ready.write("inside-no-link.md", note_text(summary="inside-no-link"))
    ready.cli_json("add", "inside-no-link.md", "--as", "inside-no-link", "--no-origin")

    record = ready.cli_json("read", "inside-no-link")
    assert record["body"]
    assert record["local_file"] is False
    assert "origin_path" not in record


def test_a_pointer_may_still_reference_an_outside_file(ready, tmp_path):
    source = _outside(tmp_path, "tool.py")
    source.write_text("print('outside')\n", encoding="utf-8")
    alias = ready.cli_json("devices")[0]["alias"]

    added = ready.cli_json("add", "--pointer", f"@file({alias}:{source})", "outside-tool")
    assert added["content_stored"] is False
    record = ready.cli_json("read", "outside-tool")
    assert record["kind"] == "file"
    assert record.get("local_file") is not True


def test_no_origin_with_pointer_is_a_usage_error(ready):
    result = ready.cli(
        "add", "--pointer", "@file(dev:/tmp/x)", "x", "--no-origin"
    )
    assert result.returncode == 64, result.stderr
    assert "--no-origin" in result.stderr


def test_a_symlink_inside_the_root_cannot_escape_it(ready, tmp_path):
    source = _outside(tmp_path, "escaped.md")
    source.write_text(note_text(summary="escaped"), encoding="utf-8")
    link = ready.workdir / "escape.md"
    try:
        os.symlink(source, link)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    result = ready.cli("add", "escape.md")
    error = _error(result)
    assert error["code"] == "origin_outside_root"
