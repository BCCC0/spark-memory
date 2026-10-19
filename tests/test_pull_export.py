"""Pull restores missing files; export writes a project snapshot."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import note_text


def test_pull_restores_every_missing_owned_file(ready):
    source = ready.write("notes/missing.md", note_text(summary="missing"))
    ready.cli_json("add", "notes/missing.md")
    source.unlink()
    head = ready.cli_json("status")["head"]

    result = ready.cli_json("pull")
    assert str(source) in result["written"]
    assert source.is_file()
    assert ready.cli_json("status")["head"] == head
    record = ready.cli_json("read", "notes/missing")
    assert record["local_file"] is True
    assert record["body_matches"] is True


def test_unknown_project_fails_loudly(ready, tmp_path):
    bad = tmp_path / "bad-project"
    bad.mkdir()
    (bad / ".mem.toml").write_text('project = "no-such-project"\n', encoding="utf-8")
    destination = tmp_path / "empty-destination"

    status = ready.cli("status", cwd=bad)
    assert status.returncode == 6, status.stderr
    assert "no_project" in status.stderr

    pull = ready.cli("pull", cwd=bad)
    assert pull.returncode == 6, pull.stderr
    assert "no_project" in pull.stderr

    export = ready.cli(
        "export", "no-such-project", "--dst", str(destination), cwd=bad
    )
    assert export.returncode == 6, export.stderr
    assert "no_project" in export.stderr
    assert not destination.exists()


def test_pull_default_skips_other_device_notes(ready):
    from mem.client import Client

    enrolled = Client(ready.server).enroll(
        "other", "test-secret", alias="other-device",
        hostname="other-host", os_name="linux", arch="amd64",
    )
    other = Client(ready.server, enrolled["token"])
    other.add(
        "demo", "other-note", note_text(summary="other").encode(),
        filename="other.md", idempotency_key="other-note-key",
        origin_path="/home/alice/other.md", message="other device",
    )
    ready.flush()

    result = ready.cli_json("pull")
    assert all("other-note" not in path for path in result["written"])
    assert result["damaged"] == []

    explicit = ready.cli("pull", "other-note")
    assert explicit.returncode == 4
    assert "missing_origin" in explicit.stderr


def test_pull_skips_an_existing_file(ready):
    source = ready.write("notes/existing.md", note_text(summary="existing"))
    ready.cli_json("add", "notes/existing.md")
    before = source.read_bytes()

    result = ready.cli_json("pull")
    assert str(source) in result["skipped"]
    assert source.read_bytes() == before


def test_pull_one_id_only(ready):
    first = ready.write("notes/one.md", note_text(summary="one"))
    second = ready.write("notes/two.md", note_text(summary="two"))
    ready.cli_json("add", "notes/one.md")
    ready.cli_json("add", "notes/two.md")
    first.unlink()
    second.unlink()

    ready.cli_json("pull", "notes/one")
    assert first.is_file()
    assert not second.exists()


def test_pull_reports_a_note_without_an_origin(ready):
    source = ready.write("notes/no-origin.md", note_text(summary="no-origin"))
    ready.cli_json("add", "notes/no-origin.md", "--no-origin")
    source.unlink()

    result = ready.cli("pull", "notes/no-origin")
    assert result.returncode == 4, result.stderr
    assert "missing_origin" in result.stderr


def test_export_writes_the_project_without_a_marker(ready, tmp_path):
    ready.write("notes/a.md", note_text(summary="a"))
    ready.write("notes/b.txt", "plain text\n")
    ready.cli_json("add", "notes/a.md")
    ready.cli_json("add", "notes/b.txt")
    markerless = tmp_path / "markerless"
    markerless.mkdir()
    destination = tmp_path / "exported"

    result = ready.cli_json(
        "export", "demo", "--dst", str(destination), cwd=markerless
    )
    assert result["project"] == "demo"
    assert (destination / "notes" / "a.md").is_file()
    assert (destination / "notes" / "b.txt").is_file()


def test_export_refuses_a_non_empty_destination(ready, tmp_path):
    ready.write("notes/a.md", note_text(summary="a"))
    ready.cli_json("add", "notes/a.md")
    destination = tmp_path / "exported"
    destination.mkdir()
    (destination / "already.txt").write_text("keep", encoding="utf-8")

    result = ready.cli("export", "demo", "--dst", str(destination))
    assert result.returncode == 4, result.stderr
    assert "not empty" in result.stderr
    assert (destination / "already.txt").read_text(encoding="utf-8") == "keep"


def test_export_refuses_a_symlink_into_the_project(ready, tmp_path):
    ready.write("notes/a.md", note_text(summary="a"))
    ready.cli_json("add", "notes/a.md")
    target = ready.workdir / "notes"
    target.mkdir(exist_ok=True)
    link = tmp_path / "export-link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    result = ready.cli("export", "demo", "--dst", str(link))
    assert result.returncode == 4, result.stderr
    assert "inside a memory project" in result.stderr


def test_export_refuses_a_project_subdirectory(ready):
    ready.write("notes/a.md", note_text(summary="a"))
    ready.cli_json("add", "notes/a.md")

    result = ready.cli("export", "demo", "--dst", "exported")
    assert result.returncode == 4, result.stderr
    assert "inside a memory project" in result.stderr
    assert not (ready.workdir / "exported").exists()


def test_export_leaves_origin_unchanged(ready, tmp_path):
    source = ready.write("notes/origin.md", note_text(summary="origin"))
    ready.cli_json("add", "notes/origin.md")
    before = ready.cli_json("read", "notes/origin")["origin_path"]
    destination = tmp_path / "snapshot"

    ready.cli_json("export", "demo", "--dst", str(destination))
    after = ready.cli_json("read", "notes/origin")["origin_path"]
    assert after == before
    assert Path(after) == source
