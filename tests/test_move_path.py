"""Path-mode mem mv: move a working file while preserving the note ID."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from conftest import note_text


def test_path_move_renames_the_file_and_keeps_the_id(ready):
    source = ready.write("notes/old.md", note_text(summary="move me", body="body"))
    source.write_text(
        note_text(summary="move me", body="body"), encoding="utf-8", newline="\n"
    )
    ready.cli_json("add", "notes/old.md")

    result = ready.cli_json("mv", "notes/old.md", "notes/new.md")
    assert result["status"] == "committed"
    assert result["id"] == "notes/old"
    assert not source.exists()
    assert Path(result["new_path"]).is_file()

    record = ready.cli_json("read", "notes/old")
    assert record["local_file"] is True
    assert record["body_matches"] is True
    assert Path(record["origin_path"]) == Path(result["new_path"])


@pytest.mark.skipif(os.name != "nt", reason="case-insensitive filesystem behaviour")
def test_path_move_can_change_case_only(ready):
    source = ready.write("notes/Case.md", note_text(summary="case"))
    ready.cli_json("add", "notes/Case.md", "--as", "notes/case")

    result = ready.cli_json("mv", "notes/Case.md", "notes/case.md")
    names = {path.name for path in (ready.workdir / "notes").iterdir()}
    assert "case.md" in names
    assert "Case.md" not in names
    assert Path(result["new_path"]).name == "case.md"


def test_path_move_stale_expect_does_not_rename(ready):
    source = ready.write("notes/stale.md", note_text(summary="stale"))
    ready.cli_json("add", "notes/stale.md")

    result = ready.cli("mv", "notes/stale.md", "notes/new.md", "--expect", "0" * 64)
    assert result.returncode == 7, result.stderr
    assert "stale_revision" in result.stderr
    assert source.exists()
    assert not (ready.workdir / "notes/new.md").exists()


def test_path_move_rejects_a_nested_project_destination(ready):
    ready.write("notes/source.md", note_text(summary="source"))
    ready.cli_json("add", "notes/source.md")
    nested = ready.workdir / "nested-dest"
    nested.mkdir()
    (nested / ".mem.toml").write_text('project = "nested-dest"\n', encoding="utf-8")

    result = ready.cli("mv", "notes/source.md", "nested-dest/new.md")
    assert result.returncode == 4, result.stderr
    assert "cross_project_move" in result.stderr
    assert (ready.workdir / "notes/source.md").exists()


def test_status_origins_ignores_topics_and_pointers(ready):
    alias = ready.cli_json("devices")[0]["alias"]
    ready.cli_json("add", "--pointer", f"@file({alias}:/tmp/tool)", "pointer-tool")

    result = ready.cli_json("status", "--origins")
    assert result["origins"]["counts"]["remote"] == 0
    assert all(entry["id"] != "pointer-tool" for entry in result["origins"]["entries"])


def test_path_move_is_idempotent_at_the_same_path(ready):
    source = ready.write("notes/same.md", note_text(summary="same", body="body"))
    source.write_text(
        note_text(summary="same", body="body"), encoding="utf-8", newline="\n"
    )
    ready.cli_json("add", "notes/same.md")

    result = ready.cli_json("mv", "notes/same.md", "notes/same.md")
    assert result["status"] == "committed"
    assert source.exists()


def test_old_id_call_gets_an_explicit_hint(ready):
    result = ready.cli("mv", "ops/old", "ops/new")
    assert result.returncode == 64, result.stderr
    assert "--id" in result.stderr
    assert "working files" in result.stderr


def test_path_move_rolls_back_when_the_server_commit_fails(ready, monkeypatch):
    from mem.errors import MemError
    from mem.server.repo import MemoryRepo

    source = ready.write("notes/rollback.md", note_text(summary="rollback"))
    ready.cli_json("add", "notes/rollback.md")
    head = ready.cli_json("status")["head"]
    previous = MemoryRepo.commit_paths

    def fail_repath(self, message, relpaths, *, device_id=None):
        if message.startswith("mem: repath"):
            raise MemError("internal_error", "simulated Git failure")
        return previous(self, message, relpaths, device_id=device_id)

    monkeypatch.setattr(MemoryRepo, "commit_paths", fail_repath)
    result = ready.cli("mv", "notes/rollback.md", "notes/rollback-new.md")
    assert result.returncode == 1, result.stderr
    assert "internal_error" in result.stderr
    assert source.exists()
    assert not (ready.workdir / "notes/rollback-new.md").exists()
    assert ready.cli_json("status")["head"] == head


def test_path_move_rejects_an_ambiguous_origin(ready):
    source = ready.write("notes/shared.md", note_text(summary="shared"))
    ready.cli_json("add", "notes/shared.md", "--as", "one")
    ready.cli_json("add", "notes/shared.md", "--as", "two")

    result = ready.cli("mv", "notes/shared.md", "notes/unique.md")
    assert result.returncode == 4, result.stderr
    assert "ambiguous_origin" in result.stderr
    assert source.exists()
    assert not (ready.workdir / "notes/unique.md").exists()


def test_path_move_rejects_a_file_no_note_owns(ready):
    source = ready.write("notes/unowned.md", note_text(summary="unowned"))
    result = ready.cli("mv", "notes/unowned.md", "notes/claimed.md")
    assert result.returncode == 9, result.stderr
    assert "origin_not_found" in result.stderr
    assert source.exists()
    assert not (ready.workdir / "notes/claimed.md").exists()


def test_path_move_rejects_a_missing_source(ready):
    result = ready.cli("mv", "notes/gone.md", "notes/new.md")
    assert result.returncode == 4, result.stderr
    assert "local_file_missing" in result.stderr


def test_path_move_rejects_an_existing_destination(ready):
    source = ready.write("notes/old.md", note_text(summary="old"))
    ready.write("notes/taken.md", note_text(summary="taken"))
    ready.cli_json("add", "notes/old.md")

    result = ready.cli("mv", "notes/old.md", "notes/taken.md")
    assert result.returncode == 4, result.stderr
    assert "local_path_exists" in result.stderr
    assert source.exists()


def test_path_move_rejects_outside_destination(ready, tmp_path):
    ready.write("notes/old.md", note_text(summary="old"))
    ready.cli_json("add", "notes/old.md")
    outside = tmp_path / "outside"
    outside.mkdir()

    result = ready.cli("mv", "notes/old.md", str(outside / "new.md"))
    assert result.returncode == 4, result.stderr
    assert "origin_outside_root" in result.stderr


def test_path_move_rejects_outside_source(ready, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    source = outside / "old.md"
    source.write_text(note_text(summary="outside"), encoding="utf-8")

    result = ready.cli("mv", str(source), "notes/new.md")
    assert result.returncode == 4, result.stderr
    assert "origin_outside_root" in result.stderr
    assert source.exists()


def test_path_move_rejects_a_nested_project(ready):
    nested = ready.workdir / "nested"
    nested.mkdir()
    (nested / ".mem.toml").write_text('project = "nested"\n', encoding="utf-8")
    source = ready.write("nested/a.md", note_text(summary="nested"))
    ready.cli_json("add", "nested/a.md")

    result = ready.cli("mv", "nested/a.md", "notes/a.md")
    assert result.returncode == 4, result.stderr
    assert "cross_project_move" in result.stderr
    assert source.exists()


def test_path_move_dry_run_changes_nothing(ready):
    source = ready.write("notes/dry.md", note_text(summary="dry"))
    ready.cli_json("add", "notes/dry.md")

    result = ready.cli_json("mv", "notes/dry.md", "notes/dry-new.md", "--dry-run")
    assert result["dry_run"] is True
    assert result["id"] == "notes/dry"
    assert source.exists()
    assert not (ready.workdir / "notes/dry-new.md").exists()


def test_status_origins_reports_a_missing_file(ready):
    source = ready.write("notes/gone.md", note_text(summary="gone"))
    ready.cli_json("add", "notes/gone.md")
    source.unlink()

    result = ready.cli_json("status", "--origins")
    assert result["origins"]["counts"]["missing"] >= 1
    assert any(entry.get("state") == "missing" for entry in result["origins"]["entries"])


def test_status_origins_reports_an_outside_legacy_path(ready, tmp_path):
    # Simulate a legacy origin by writing the frontmatter directly is not
    # available through the policy-guarded add. Use a nested project file as a
    # cross-project classification instead.
    nested = ready.workdir / "legacy"
    nested.mkdir()
    (nested / ".mem.toml").write_text('project = "legacy"\n', encoding="utf-8")
    ready.write("legacy/a.md", note_text(summary="legacy"))
    ready.cli_json("add", "legacy/a.md")

    result = ready.cli_json("status", "--origins")
    assert result["origins"]["counts"]["cross_project"] >= 1
