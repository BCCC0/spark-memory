"""The auto-track hook: installation, and what a capture actually stores.

Two properties matter more than the feature itself:

* installing must never destroy a hook the user already had, and uninstalling
  must put it back exactly -- a tool that eats someone's hook is uninstalled once;
* a failed capture must never fail the commit that triggered it, and must leave a
  trace, since a hook's silence is otherwise indistinguishable from success.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from mem import hooks, tracking


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout


def _commit(repo: Path, message: str = "c") -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD").strip()


def _hook(repo: Path, event: str = "post-commit") -> Path:
    return repo / ".git" / "hooks" / event


NOTE = "---\nsummary: tracked\nstatus: active\ntype: reference\n---\n\n# Tracked\n\nbody\n"


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A bare repository, for the installation tests."""
    repo = tmp_path / "tracked"
    repo.mkdir()
    _git(repo, "init", "-q", ".")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    return repo


@pytest.fixture
def tracked(ready, git_repo: Path) -> Path:
    """A repository that is also a project root: the realistic auto-track setup.

    Ids are derived relative to the marker, so the marker has to live *in* the
    repository for the paths git reports to become note ids.
    """
    ready.cli_json("init", "tracked-repo", "--summary", "auto-track fixture", cwd=git_repo)
    return git_repo


# -- installation ----------------------------------------------------------


def test_install_writes_all_events_and_is_idempotent(git_repo):
    result = hooks.install(git_repo)
    assert sorted(result["installed"]) == ["post-commit", "post-merge", "pre-commit"]

    hooks.install(git_repo)
    for event in hooks.HOOK_EVENTS:
        text = _hook(git_repo, event).read_text(encoding="utf-8")
        assert text.count(hooks.MARK_BEGIN) == 1
        assert text.count(hooks.MARK_END) == 1


def test_install_delegates_rather_than_embedding_behaviour(git_repo):
    hooks.install(git_repo)
    text = _hook(git_repo).read_text(encoding="utf-8")
    assert "hook update" in text
    assert "--repo" in text


def test_the_shim_runs_synchronously(git_repo):
    """Regression: backgrounding the capture loses it.

    The shim used to end its invocation with ``&``. Git reaps the process group
    when the hook returns, so a capture that had not finished was killed --
    silently, with no record and no log line. It survived only against a fast
    local daemon and lost the race against a remote host, which is the normal
    case for this service.
    """
    hooks.install(git_repo)
    text = _hook(git_repo).read_text(encoding="utf-8")

    update_lines = [line for line in text.split("\n") if "hook update" in line]
    assert update_lines, "the shim must delegate to `hook update`"
    for line in update_lines:
        assert not line.rstrip().endswith("&"), (
            "the capture must finish before the hook returns: git kills the "
            "process group on exit, so a backgrounded capture is silently lost"
        )
    # A failing capture must still not fail the commit.
    assert "|| true" in text


def test_the_shim_carries_no_backslashes(git_repo):
    """The shim is POSIX sh, where a backslash in double quotes is an escape.

    A Windows interpreter path written with backslashes would be mangled before
    the shell ever tried to run it, so the installed text must be slash-only.
    """
    hooks.install(git_repo)
    assert "\\" not in _hook(git_repo).read_text(encoding="utf-8")


def test_install_preserves_an_existing_user_hook(git_repo):
    original = "#!/bin/sh\necho USER_HOOK_RAN\n"
    _hook(git_repo).write_text(original, encoding="utf-8")
    _hook(git_repo, "pre-commit").write_text(original, encoding="utf-8")

    hooks.install(git_repo)
    text = _hook(git_repo).read_text(encoding="utf-8")
    assert "echo USER_HOOK_RAN" in text
    assert text.count(hooks.MARK_BEGIN) == 1
    assert "echo USER_HOOK_RAN" in _hook(git_repo, "pre-commit").read_text(encoding="utf-8")

    hooks.uninstall(git_repo)
    assert _hook(git_repo).read_text(encoding="utf-8") == original
    assert _hook(git_repo, "pre-commit").read_text(encoding="utf-8") == original


def test_uninstall_removes_only_our_files(git_repo):
    hooks.install(git_repo)
    assert _hook(git_repo).exists()
    hooks.uninstall(git_repo)
    assert not _hook(git_repo).exists()
    assert not _hook(git_repo, "post-merge").exists()
    assert not _hook(git_repo, "pre-commit").exists()


def test_status_reports_what_is_installed(git_repo):
    assert hooks.status(git_repo)["installed"] == []
    hooks.install(git_repo)
    assert sorted(hooks.status(git_repo)["installed"]) == ["post-commit", "post-merge", "pre-commit"]


def test_hook_verbs_refuse_a_non_repository(tmp_path):
    from mem.errors import MemError

    with pytest.raises(MemError):
        hooks.install(tmp_path / "not-a-repo")


# -- capture ---------------------------------------------------------------


def test_hook_update_uses_the_repo_marker_from_a_markerless_cwd(ready, tracked):
    """`--repo` must supply the project; the caller's cwd need not be a project."""
    ready.cli_json("hook", "add", "*.md", cwd=tracked)
    (tracked / "outside.md").write_text(NOTE, encoding="utf-8", newline="\n")
    _commit(tracked, "add outside")

    markerless = ready.tmp / "markerless"
    markerless.mkdir()
    result = ready.cli_json(
        "hook", "update", "--repo", str(tracked), cwd=markerless
    )
    assert result["added"] == ["outside.md"], result
    assert ready.cli_json("read", "outside", cwd=tracked)["id"] == "outside"


def test_hook_update_refuses_a_different_cwd_project(ready, tracked, tmp_path):
    ready.cli_json("hook", "add", "*.md", cwd=tracked)
    (tracked / "note.md").write_text(NOTE, encoding="utf-8", newline="\n")
    _commit(tracked)

    other = tmp_path / "other-project"
    other.mkdir()
    (other / ".mem.toml").write_text('project = "other"\n', encoding="utf-8")

    failed = ready.cli("hook", "update", "--repo", str(tracked), cwd=other)
    assert failed.returncode == 4, failed.stderr
    payload = json.loads(failed.stderr)
    assert payload["error"]["code"] == "invalid_request"
    assert "different project" in payload["error"]["message"]


def test_hook_check_uses_the_repo_marker_from_a_markerless_cwd(ready, tracked):
    ready.cli_json("hook", "add", "docs/*.md", cwd=tracked)
    _commit(tracked, "baseline")
    (tracked / "docs").mkdir()
    (tracked / "docs" / "guide.md").write_text(NOTE, encoding="utf-8")
    _git(tracked, "add", "docs/guide.md")

    markerless = ready.tmp / "markerless-check"
    markerless.mkdir()
    result = ready.cli_json(
        "hook", "check", "--repo", str(tracked), cwd=markerless
    )
    assert result["checked"] == ["docs/guide.md"], result


def test_captures_a_committed_markdown_file_as_a_note(ready, tracked):
    ready.cli_json("hook", "add", "*.md", cwd=tracked)
    (tracked / "note.md").write_text(NOTE, encoding="utf-8", newline="\n")
    _commit(tracked, "add the note")

    result = ready.cli_json("hook", "update", "--repo", str(tracked), cwd=tracked)
    assert result["added"] == ["note.md"], result

    record = ready.cli_json("read", "note.md", cwd=tracked)
    assert record["kind"] == "note"
    assert record["title"] == "Tracked"


def test_hook_splits_more_than_100_files_into_server_sized_batches(ready, tracked, monkeypatch):
    ready.cli_json("hook", "add", "*.txt", cwd=tracked)
    for index in range(101):
        (tracked / f"item-{index:03}.txt").write_text("tracked\n", encoding="utf-8")
    head = _commit(tracked, "many files")

    result = ready.cli_json("hook", "update", "--repo", str(tracked), cwd=tracked)
    assert result["errors"] == [], result["errors"]
    assert len(result["added"]) == 101, result
    monkeypatch.setenv("MEM_CONFIG_DIR", str(ready.config_dir))
    assert tracking.read_watermark(tracked) == head


def test_captures_a_non_text_file_as_a_pointer(ready, tracked):
    ready.cli_json("hook", "add", "*.png", cwd=tracked)
    (tracked / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\nbinary-ish")
    _commit(tracked)

    result = ready.cli_json("hook", "update", "--repo", str(tracked), cwd=tracked)
    assert result["added"] == ["logo.png"], result

    record = ready.cli_json("read", "logo.png", cwd=tracked)
    assert record["kind"] == "file", record
    assert record["points_at"], "a pointer must name its device"


def test_an_uncommitted_edit_is_not_captured(ready, tracked):
    """The capture must read the revision, not the working tree."""
    ready.cli_json("hook", "add", "*.md", cwd=tracked)
    target = tracked / "note.md"
    target.write_text(NOTE, encoding="utf-8", newline="\n")
    _commit(tracked)
    ready.cli_json("hook", "update", "--repo", str(tracked), cwd=tracked)

    # Edit without committing, then capture again at the SAME revision.
    draft = NOTE.replace("# Tracked", "# Draft only")
    target.write_text(draft, encoding="utf-8", newline="\n")
    ready.cli_json("hook", "update", "--repo", str(tracked), "--rev", "HEAD", cwd=tracked)

    body = ready.cli_json("read", "note.md", cwd=tracked)
    assert body["title"] == "Tracked", "memory must hold the committed revision, not the draft"


def test_a_second_run_captures_nothing_new(ready, tracked):
    """The watermark is what makes a hook firing twice harmless."""
    ready.cli_json("hook", "add", "*.md", cwd=tracked)
    (tracked / "note.md").write_text(NOTE, encoding="utf-8", newline="\n")
    _commit(tracked)

    first = ready.cli_json("hook", "update", "--repo", str(tracked), cwd=tracked)
    assert first["added"] == ["note.md"]

    second = ready.cli_json("hook", "update", "--repo", str(tracked), cwd=tracked)
    assert second["added"] == []
    assert second["changed"] == 0, "the watermark should leave nothing to read"


def test_dry_run_writes_nothing(ready, tracked):
    ready.cli_json("hook", "add", "*.md", cwd=tracked)
    (tracked / "note.md").write_text(NOTE, encoding="utf-8", newline="\n")
    _commit(tracked)

    plan = ready.cli_json("hook", "update", "--repo", str(tracked), "--dry-run", cwd=tracked)
    assert plan["would_add"] == ["note.md"]
    assert plan["added"] == []

    # Nothing was written, so a real run still has work to do.
    result = ready.cli_json("hook", "update", "--repo", str(tracked), cwd=tracked)
    assert result["added"] == ["note.md"]


def test_summary_mixed_case_legacy_bool_is_a_string(ready, tracked):
    """PyYAML and the server accept `summary: yEs`; the Go preflight must too."""
    ready.cli_json("hook", "add", "*.md", cwd=tracked)
    (tracked / "mixed.md").write_text(
        NOTE.replace("summary: tracked", "summary: yEs"), encoding="utf-8"
    )
    _commit(tracked, "mixed-case summary")
    result = ready.cli_json("hook", "update", "--repo", str(tracked), cwd=tracked)
    assert result["errors"] == []
    assert "mixed.md" in result["added"]
    assert ready.cli_json("read", "mixed", cwd=tracked)["summary"] == "yEs"


def test_first_run_reports_an_already_deleted_tracked_file(ready, tracked):
    """Without a watermark the range is rev^..rev, so an orphan is still seen."""
    ready.cli_json("hook", "add", "*.md", cwd=tracked)
    (tracked / "gone.md").write_text(NOTE, encoding="utf-8")
    _commit(tracked, "add")
    _git(tracked, "rm", "-q", "gone.md")
    _commit(tracked, "remove")
    result = ready.cli_json("hook", "update", "--repo", str(tracked), "--dry-run", cwd=tracked)
    assert "gone.md" in result["orphaned"], result
    assert result["errors"] == []


def test_scalar_track_patterns_are_accepted(ready, tracked):
    """Python writes/accepts `patterns = "*.md"`; the Go parser must too."""
    marker = tracked / ".mem.toml"
    text = marker.read_text(encoding="utf-8")
    if "[track]" not in text:
        text += '\n[track]\npatterns = "*.md"\n'
    else:
        text = text.replace('patterns = ["*.md"]', 'patterns = "*.md"')
    marker.write_text(text, encoding="utf-8")
    listed = ready.cli_json("hook", "ls", cwd=tracked)
    assert listed["patterns"] == ["*.md"]


def test_from_git_reads_the_commit_not_the_working_tree(ready, tracked):
    """An invalid working copy must not block adding a valid committed revision."""
    ready.cli_json("hook", "add", "*.md", cwd=tracked)
    target = tracked / "note.md"
    target.write_text(NOTE, encoding="utf-8")
    sha = _commit(tracked, "add note")
    target.write_text("# Missing summary\n", encoding="utf-8")
    ready.cli_json("add", "--from-git", sha, "note.md", cwd=tracked)
    assert ready.cli_json("read", "note", cwd=tracked)["summary"] == "tracked"


def test_without_patterns_the_hook_does_nothing(ready, tracked):
    """No declaration means nothing to do -- not "capture everything"."""
    (tracked / "note.md").write_text(NOTE, encoding="utf-8", newline="\n")
    _commit(tracked)

    result = ready.cli_json("hook", "update", "--repo", str(tracked), cwd=tracked)
    assert result["added"] == []
    assert result["patterns"] == 0


def test_a_deleted_tracked_file_is_reported_not_silently_dropped(ready, tracked):
    ready.cli_json("hook", "add", "*.md", cwd=tracked)
    (tracked / "note.md").write_text(NOTE, encoding="utf-8", newline="\n")
    _commit(tracked)
    ready.cli_json("hook", "update", "--repo", str(tracked), cwd=tracked)

    _git(tracked, "rm", "-q", "note.md")
    _commit(tracked, "remove the note")

    result = ready.cli_json("hook", "update", "--repo", str(tracked), cwd=tracked)
    assert "note.md" in result["orphaned"], result
    # Reported, not removed: memory keeps the record until asked otherwise.
    assert ready.cli_json("read", "note.md", cwd=tracked)["id"] == "note.md"


def test_a_failure_is_logged_and_does_not_fail_the_hook(ready, tracked):
    """A hook must not be able to break a commit: failures are recorded, not raised.

    The server is pointed at a closed port, which is the real shape of the failure
    this guards against the service being unreachable while the developer keeps working.
    """
    ready.cli_json("hook", "add", "*.md", cwd=tracked)
    (tracked / "note.md").write_text(NOTE, encoding="utf-8", newline="\n")
    _commit(tracked)

    # 127.0.0.1:9 is the discard port; nothing listens there.
    result = ready.cli(
        "hook", "update", "--repo", str(tracked), "--server", "http://127.0.0.1:9",
        cwd=tracked,
    )
    assert result.returncode == 0, "the hook must exit 0 even when the write fails"

    payload = json.loads(result.stdout)
    assert payload["added"] == []
    assert payload["errors"], "the failure must be reported, not swallowed"

    log = tracked / ".git" / "mem-hook.log"
    assert log.is_file(), "a hook's silence must still be diagnosable"
    assert "note.md" in log.read_text(encoding="utf-8")


def test_hook_ls_reports_patterns_and_state(ready, tracked):
    ready.cli_json("hook", "add", "*.md", "!drafts/**", cwd=tracked)
    listed = ready.cli_json("hook", "ls", cwd=tracked)
    assert listed["patterns"] == ["*.md", "!drafts/**"]
    assert "installed" in listed["hook"]


def test_hook_rm_drops_a_pattern(ready, tracked):
    ready.cli_json("hook", "add", "*.md", "*.txt", cwd=tracked)
    ready.cli_json("hook", "rm", "*.txt", cwd=tracked)
    assert ready.cli_json("hook", "ls", cwd=tracked)["patterns"] == ["*.md"]


def test_the_watermark_advances_only_after_a_capture(ready, tracked, monkeypatch):
    ready.cli_json("hook", "add", "*.md", cwd=tracked)
    (tracked / "note.md").write_text(NOTE, encoding="utf-8", newline="\n")
    sha = _commit(tracked)

    # The CLI runs in a subprocess with the harness's config dir, so read the
    # watermark from there rather than from this process's default location.
    monkeypatch.setenv("MEM_CONFIG_DIR", str(ready.config_dir))
    assert tracking.read_watermark(tracked) is None

    ready.cli_json("hook", "update", "--repo", str(tracked), cwd=tracked)
    assert tracking.read_watermark(tracked) == sha


def test_precommit_checks_staged_selected_markdown_offline(ready, tracked):
    ready.cli_json("hook", "add", "docs/*.md", cwd=tracked)
    baseline = _commit(tracked, "baseline")
    hooks.install(tracked)
    (tracked / "docs").mkdir()
    selected = tracked / "docs" / "guide.md"
    selected.write_text(NOTE, encoding="utf-8")
    _git(tracked, "add", "docs/guide.md")
    selected.write_text("# Unstaged invalid edit\n", encoding="utf-8")
    assert ready.cli_json("hook", "check", "--repo", str(tracked),
                          "--server", "http://127.0.0.1:9", cwd=tracked)["checked"] == ["docs/guide.md"]

    _git(tracked, "add", "docs/guide.md")
    failure = ready.cli("hook", "check", "--repo", str(tracked), cwd=tracked)
    assert failure.returncode == 4
    assert json.loads(failure.stderr)["error"]["detail"]["files"][0]["path"] == "docs/guide.md"
    blocked = subprocess.run(["git", "commit", "-m", "invalid"], cwd=tracked,
                             capture_output=True, text=True, check=False)
    assert blocked.returncode != 0
    assert _git(tracked, "rev-parse", "HEAD").strip() == baseline

    # An unrelated Markdown file is outside the selected patterns.
    (tracked / "outside.md").write_text("# No summary here\n", encoding="utf-8")
    _git(tracked, "reset", "-q", "HEAD", "--", "docs/guide.md")
    _git(tracked, "add", "outside.md")
    assert ready.cli_json("hook", "check", "--repo", str(tracked), cwd=tracked)["checked"] == []


def test_failed_capture_keeps_watermark_and_retry_uses_latest_state(ready, tracked, monkeypatch):
    ready.cli_json("hook", "add", "*.md", cwd=tracked)
    (tracked / "good.md").write_text(NOTE, encoding="utf-8")
    bad = tracked / "bad.md"
    bad.write_text("# Missing summary\n", encoding="utf-8")
    _commit(tracked, "mixed state")
    monkeypatch.setenv("MEM_CONFIG_DIR", str(ready.config_dir))
    failed = ready.cli_json("hook", "update", "--repo", str(tracked), cwd=tracked)
    assert failed["added"] == []
    assert failed["errors"]
    assert tracking.read_watermark(tracked) is None
    assert ready.cli_json("ls", cwd=tracked)["total"] == 1  # PROJECT only

    bad.write_text(NOTE.replace("summary: tracked", "summary: repaired"), encoding="utf-8")
    latest = _commit(tracked, "repair summary")
    synced = ready.cli_json("hook", "update", "--repo", str(tracked), cwd=tracked)
    assert sorted(synced["added"]) == ["bad.md", "good.md"]
    assert tracking.read_watermark(tracked) == latest
    assert ready.cli_json("read", "bad", cwd=tracked)["summary"] == "repaired"
