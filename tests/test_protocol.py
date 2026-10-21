"""Protocol-level checks: auth, project resolution, and the typed exit codes.

These guard the parts of ``MEM-CLI.md`` §4 that acceptance tests 1-7 do not
touch — especially the deliberate split between exit ``2`` (server down) and exit
``8`` (not enrolled), which an agent must be able to tell apart.
"""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request

from conftest import note_text


def test_health_is_open_and_reports_head(harness):
    with urllib.request.urlopen(f"{harness.server}/health", timeout=5) as response:
        payload = json.loads(response.read())
    assert payload["ok"] is True
    assert "version" in payload
    assert "head" in payload


def test_enroll_rejects_a_wrong_secret(harness):
    patient = harness.cli("login", "--server", harness.server, "--name", "x", "--secret", "nope")
    assert patient.returncode == 8, patient.stderr


def test_reads_require_a_token(harness):
    with urllib.request.urlopen(f"{harness.server}/health", timeout=5) as response:
        assert response.status == 200
    try:
        urllib.request.urlopen(f"{harness.server}/v1/status", timeout=5)
    except urllib.error.HTTPError as exc:
        assert exc.code == 401
        body = json.loads(exc.read())
        assert body["error"]["code"] == "unauthorized"
    else:  # pragma: no cover
        raise AssertionError("an unauthenticated read must be refused")


def test_missing_credentials_exit_8(harness):
    result = harness.cli("status")
    assert result.returncode == 8, f"expected exit 8, got {result.returncode}: {result.stderr}"
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "unauthorized"


def test_no_project_for_pwd_exit_6(ready):
    elsewhere = ready.tmp / "elsewhere"
    elsewhere.mkdir(exist_ok=True)
    # A literal "no marker anywhere above" is not constructible in this environment:
    # the repo root legitimately carries a `.mem.toml` (`mem init example-project`), the test
    # tree lives inside it, and the sandbox forbids writing outside the workspace so
    # `tempfile` cannot escape it. Shadow that ancestor with the NEAREST marker, one
    # that declares no project — which resolves to exactly the same `no_project`.
    (elsewhere / ".mem.toml").write_text("# deliberately declares no project\n", encoding="utf-8")

    result = ready.cli("ls", cwd=elsewhere)
    assert result.returncode == 6, f"expected exit 6, got {result.returncode}: {result.stderr}"
    assert json.loads(result.stderr)["error"]["code"] == "no_project"


def test_schema_invalid_exit_4(ready):
    # A derived key must never be stored.
    bad = "---\nsummary: s\nstatus: active\ntype: reference\npermalink: x\n---\n\n# T\n"
    ready.write("bad.md", bad)
    result = ready.cli("add", "bad.md", "--as", "bad")
    assert result.returncode == 4, f"expected exit 4, got {result.returncode}: {result.stderr}"
    assert json.loads(result.stderr)["error"]["code"] == "schema_invalid"


def test_illegal_local_filename_is_refused_not_transliterated(ready):
    ready.write("My Notes.md", note_text(summary="spaces"))
    result = ready.cli("add", "My Notes.md")
    assert result.returncode == 4, result.stderr

    # ... but --as makes it expressible.
    ok = ready.cli("add", "My Notes.md", "--as", "my-notes")
    assert ok.returncode == 0, ok.stderr


def test_target_moved_exit_9(ready):
    result = ready.cli("read", "does-not-exist")
    assert result.returncode == 9, f"expected exit 9, got {result.returncode}: {result.stderr}"
    assert json.loads(result.stderr)["error"]["code"] == "target_moved"


def test_init_is_idempotent(ready):
    again = ready.cli_json("init", "demo")
    assert again["created"] is False


def test_marker_is_found_by_walking_up(ready):
    nested = ready.workdir / "deep" / "deeper"
    nested.mkdir(parents=True, exist_ok=True)
    ready.write("nested.md", note_text(summary="nested"), directory=nested)
    result = ready.cli("add", "nested.md", cwd=nested)
    assert result.returncode == 0, result.stderr
    ready.flush()
    # The marker is still found by walking up — but the id is now relative to the
    # MARKER, not to the cwd (`S35` D7, amending B5): one file must not get two ids
    # depending on where the command happened to run.
    listed = ready.cli_json("ls")
    assert any(n["id"] == "deep/deeper/nested" for n in listed["notes"])


def test_status_reports_the_tree_and_the_last_change(ready):
    """Staging is gone (S23); what remains is visibility plus the head message."""
    ready.write("s.md", note_text(summary="s"))
    added = ready.cli_json("add", "s.md", "--as", "s", "-m", "add the s note")

    status = ready.cli_json("status")
    assert "staging" not in status, "staging was deleted, not renamed"
    # A write IS a commit, so the tree is clean the moment the call returns.
    assert status["index_dirty"] is False
    assert status["head"] == added["head"]
    assert status["message"] == "add the s note"
    assert status["read"]["max_bytes"] == 4096


def test_search_hit_carries_a_revision(ready):
    ready.write("z.md", note_text(summary="zebra", body="the zebra token"))
    ready.cli_json("add", "z.md", "--as", "z")
    found = ready.cli_json("search", "zebra")
    assert found["hits"], "expected a hit"
    for hit in found["hits"]:
        assert hit["revision"], "every hit must carry the revision its range refers to"
        assert "id" in hit and "summary" in hit


def test_usage_error_exits_64_with_the_json_envelope(harness):
    """A usage error is a typed outcome, not argparse prose (`MEM-CLI.md` §4).

    Exit 64 is deliberately *not* 2, so "you typed it wrong" can never be confused
    with "the server is down" -- and invariant C6 forbids an agent to parse prose,
    so the parser raises a `MemError` and stderr carries exactly one JSON object.
    """
    for args in (("--nonsense",), ("ls", "--bogus"), ("read",)):
        result = harness.cli(*args)
        assert result.returncode == 64, (
            f"{args}: expected exit 64, got {result.returncode}: {result.stderr}"
        )
        payload = json.loads(result.stderr)
        assert payload["error"]["code"] == "usage_error", args
        assert len(result.stderr.strip().splitlines()) == 1, args


def test_usage_error_is_json_even_under_human(harness):
    """`--human` cannot apply: parsing failed, so the flag is not trustworthy."""
    result = harness.cli("--human", "--nonsense")
    assert result.returncode == 64, result.stderr
    assert json.loads(result.stderr)["error"]["code"] == "usage_error"


def test_fail_keeps_usage_errors_on_the_json_path():
    """The "usage errors are always JSON" rule lives in `fail`, so it is testable.

    A `usage_error` can only be raised from inside argument parsing, which aborts
    before `--human` is reliably known -- a guard at the call site could never
    fire. Putting the rule in `fail` keeps it reachable for a future handler and
    gives this test something real to assert.
    """
    import io
    from contextlib import redirect_stderr

    from mem.cli import fail
    from mem.errors import MemError

    buffer = io.StringIO()
    with redirect_stderr(buffer):
        code = fail(MemError("usage_error", "bad flag", {"usage": "usage: mem"}), True)

    assert code == 64
    payload = json.loads(buffer.getvalue())
    assert payload["error"]["code"] == "usage_error"
    # The usage text the old `print_usage` emitted survives inside `detail`.
    assert payload["error"]["detail"]["usage"] == "usage: mem"


def test_a_write_git_refuses_to_stage_fails_loudly(ready):
    """An ignore rule swallowing a note is an error, not a silent no-op (S30).

    With the path ignored, `git add -A` stages nothing and the commit layer reports
    "nothing changed" -- while the file sits on disk in **no commit**. That is not
    the same as a byte-identical re-write, so it must not be reported as a
    successful no-op, and the write must be rolled back.
    """
    gitignore = ready.repo / ".gitignore"
    gitignore.write_text(
        gitignore.read_text(encoding="utf-8") + "projects/demo/private/\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", ".gitignore"], cwd=ready.repo, check=True)
    subprocess.run(["git", "commit", "-m", "ignore private notes"], cwd=ready.repo,
                   check=True, capture_output=True)
    ready.write("p.md", note_text(summary="swallowed"))

    result = ready.cli("add", "p.md", "--as", "private/p")
    assert result.returncode != 0
    error = json.loads(result.stderr)["error"]
    assert error["code"] in {"internal_error", "schema_invalid"}, error
    # Rolled back rather than left on disk uncommitted.
    assert not (ready.repo / "projects" / "demo" / "private" / "p.md").exists()

