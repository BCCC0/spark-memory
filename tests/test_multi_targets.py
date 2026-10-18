"""Multiple reads and one-commit adds over the real HTTP service."""

from __future__ import annotations

import json
import subprocess

from conftest import note_text


def test_multi_read_keeps_full_records_and_reports_each_failure(ready):
    ready.write("first.md", note_text(summary="first summary"))
    ready.cli_json("add", "first.md")
    result = ready.cli("read", "first", "missing", "PROJECT")
    assert result.returncode == 9
    rows = json.loads(result.stdout)["results"]
    assert [row["id"] for row in rows] == ["first", "missing", "PROJECT"]
    assert rows[0]["record"]["summary"] == "first summary"
    assert rows[0]["record"]["id"] == "first"
    assert rows[1]["error"]["code"] == "target_moved"
    assert rows[2]["ok"] is True
    assert json.loads(result.stderr)["error"]["code"] == "target_moved"


def test_multi_add_mixed_content_and_pointer_one_commit(ready):
    ready.write("one.md", note_text(summary="one"))
    ready.write("two.txt", "Two text lines\n")
    ready.write("three.bin", "SECRET BINARY TARGET")
    before = ready.cli_json("status")["head"]
    result = ready.cli_json("add", "one.md", "two.txt", "three.bin", "-m", "record three files")
    assert result["head"] != before
    assert result["message"] == "record three files"
    assert [row["content_stored"] for row in result["results"]] == [True, True, False]
    assert ready.cli_json("read", "one")["message"] == "record three files"
    assert ready.cli_json("read", "two.txt")["message"] == "record three files"
    assert ready.cli_json("read", "three.bin")["kind"] == "file"
    assert "SECRET BINARY TARGET" not in (ready.repo / result["results"][2]["path"]).read_text()
    assert ready.cli_json("status")["index_dirty"] is False
    replay = ready.cli_json("add", "one.md", "two.txt", "three.bin", "-m", "record three files")
    assert replay["idempotent_replay"] is True
    assert replay["head"] == result["head"]


def test_multi_add_validates_all_before_writing(ready):
    ready.write("good.md", note_text(summary="good"))
    ready.write("bad.md", "---\nsummary: [invalid]\n---\n# Bad\n")
    before = ready.cli_json("status")["head"]
    result = ready.cli("add", "good.md", "bad.md", "-m", "should fail")
    assert result.returncode == 4
    assert ready.cli_json("status")["head"] == before
    assert ready.cli_json("ls")["total"] == 1  # PROJECT only


def test_summary_is_required_on_new_and_updated_markdown(ready):
    path = ready.write("summary.md", "# A title alone is insufficient\n")
    missing = ready.cli("add", "summary.md")
    assert missing.returncode == 4
    path.write_text(note_text(summary="valid"), encoding="utf-8")
    ready.cli_json("add", "summary.md")
    path.write_text("# An update without summary\n", encoding="utf-8")
    update = ready.cli("add", "summary.md")
    assert update.returncode == 4
    assert ready.cli_json("read", "summary")["summary"] == "valid"


def test_multi_add_rolls_back_if_git_ignores_one_path(ready):
    ignore = ready.repo / ".gitignore"
    ignore.write_text(ignore.read_text(encoding="utf-8") + "projects/demo/ignored.md\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=ready.repo, check=True)
    subprocess.run(["git", "commit", "-m", "ignore one note"], cwd=ready.repo, check=True,
                   capture_output=True)
    ready.write("good.md", note_text(summary="good"))
    ready.write("ignored.md", note_text(summary="ignored"))
    before = ready.cli_json("status")["head"]
    failed = ready.cli("add", "good.md", "ignored.md", "-m", "both or neither")
    assert failed.returncode == 0
    assert ready.cli("flush").returncode == 1
    assert ready.cli_json("status")["head"] == before
    assert not (ready.repo / "projects/demo/good.md").exists()
    assert not (ready.repo / "projects/demo/ignored.md").exists()


def test_pointer_upgrade_allowed_and_content_downgrade_refused(ready):
    ready.write("a.txt", "Stored content\n")
    ready.write("b.txt", "Other content\n")
    ready.cli_json("add", "--pointer", "a.txt", "b.txt", "-m", "catalog sources")
    assert ready.cli_json("read", "a.txt")["kind"] == "file"
    assert ready.cli_json("read", "b.txt")["kind"] == "file"
    result = ready.cli_json("add", "a.txt", "b.txt", "-m", "store sources")
    assert [row["content_stored"] for row in result["results"]] == [True, True]
    assert ready.cli_json("read", "a.txt")["body"] == (ready.workdir / "a.txt").read_bytes().decode()
    assert not (ready.repo / "projects/demo/a.txt.md").exists()
    before = result["head"]
    downgrade = ready.cli("add", "--pointer", "a.txt", "b.txt", "-m", "downgrade")
    assert downgrade.returncode == 4
    assert ready.cli_json("status")["head"] == before


def test_explicit_pointer_command(ready):
    alias = ready.cli_json("devices")[0]["alias"]
    ready.cli_json("pointer", "add", f"@dir({alias}:/tmp/examples)", "examples")
    assert ready.cli_json("read", "examples")["kind"] == "dir"


def test_single_file_pointer_upgrade_and_downgrade_rule(ready):
    ready.write("solo.txt", "One stored file\n")
    ready.cli_json("add", "--pointer", "solo.txt")
    assert ready.cli_json("read", "solo.txt")["kind"] == "file"
    upgraded = ready.cli_json("add", "solo.txt")
    assert upgraded["content_stored"] is True
    assert not (ready.repo / "projects/demo/solo.txt.md").exists()
    assert ready.cli_json("read", "solo.txt")["kind"] == "text"
    refused = ready.cli("add", "--pointer", "solo.txt")
    assert refused.returncode == 4
    assert ready.cli_json("status")["head"] == upgraded["head"]
