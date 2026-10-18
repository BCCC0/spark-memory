"""Regressions for the pre-public server hardening pass."""

from __future__ import annotations

import json
import sqlite3
import threading
import urllib.request

import pytest

from conftest import Harness, note_text
from mem.client import Client
from mem.errors import MemError
from mem.server.repo import MemoryRepo
from mem.server.queue import WriteQueue


def test_health_does_not_run_git_on_the_event_loop(harness, monkeypatch):
    def head_on_caller_thread(self):
        return threading.current_thread().name

    monkeypatch.setattr(MemoryRepo, "head", head_on_caller_thread)
    with urllib.request.urlopen(f"{harness.server}/health", timeout=5) as response:
        payload = json.load(response)
    assert "asyncio" in payload["head"], payload


def test_a_device_may_only_retire_itself(ready):
    rows = ready.cli_json("devices")
    current = next(
        row for row in rows
        if row["display_name"] == "test-device" and not row["retired_at"]
    )
    other = Client(ready.server).enroll(
        "other", "test-secret", alias="other-device",
        hostname="other-host", os_name="linux", arch="amd64",
    )
    with pytest.raises(MemError) as refused:
        Client(ready.server, other["token"]).retire_device(current["device_id"])
    assert refused.value.code == "unauthorized"

    retired = Client(ready.server, ready.token()).retire_device(current["device_id"])
    assert retired["retired"] == current["device_id"]


def test_queue_loop_survives_a_next_job_failure(repo_template, tmp_path, monkeypatch):
    original = WriteQueue.next_job
    calls = {"n": 0}

    def flaky(self):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient queue failure")
        return original(self)

    monkeypatch.setattr(WriteQueue, "next_job", flaky)
    harness = Harness(tmp_path, template=repo_template)
    harness.start()
    try:
        harness.login()
        harness.init("demo")
        harness.write("x.md", note_text(summary="x"))
        harness.cli_json("add", "x.md", "--as", "x")
        harness.flush()
    finally:
        harness.stop()
    assert calls["n"] >= 2


def test_a_mirror_reset_failure_cannot_unpublish_a_settled_write(ready, monkeypatch):
    """A timeout after update-ref must not turn a shipped write into a failed job.

    The reset refreshes only the served checkout; the commit is already the ref's
    new value. Recording the job as failed would de-key its idempotency entry and a
    retry would create a second job for the same bytes.
    """
    served = ready.repo.resolve()
    original = MemoryRepo.git

    def flaky_git(self, *args, **kwargs):
        if self.root.resolve() == served and args[:2] == ("reset", "--hard"):
            raise MemError("internal_error", "git operation timed out")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(MemoryRepo, "git", flaky_git)
    client = Client(ready.server, ready.token())
    body = note_text(summary="published", body="reset-timeout-token").encode("utf-8")
    with sqlite3.connect(ready.state / "write-queue" / "jobs.db") as db:
        before = db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    first = client.add(
        "demo", "published", body,
        filename="published.md", idempotency_key="reset-timeout",
    )
    finished = ready.wait_for_receipt(first["receipt"])
    assert finished["head"], finished
    assert ready.cli_json("read", "published")["summary"] == "published"

    retry = client.add(
        "demo", "published", body,
        filename="published.md", idempotency_key="reset-timeout",
    )
    assert retry["idempotent_replay"] is True
    assert retry["receipt"] == first["receipt"]

    with sqlite3.connect(ready.state / "write-queue" / "jobs.db") as db:
        after = db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        status = db.execute(
            "SELECT status FROM jobs WHERE id=?", (first["receipt"],)
        ).fetchone()[0]
    assert after == before + 1, "the retry must not enqueue a second job"
    assert status == "committed"


def test_writes_cannot_escape_to_a_sibling_with_the_same_prefix(tmp_path):
    root = tmp_path / "mem"
    sibling = tmp_path / "mem-evil"
    repo = MemoryRepo(root)
    repo.ensure()
    sibling.mkdir()

    with pytest.raises(MemError):
        repo.write_note("../mem-evil/escaped.md", b"escaped")
    assert not (sibling / "escaped.md").exists()

    with pytest.raises(MemError):
        repo.remove_note("../mem-evil/escaped.md")

    # rollback_path must never raise and must not touch an outside file either.
    outside = sibling / "keep.md"
    outside.write_bytes(b"keep")
    repo.rollback_path("../mem-evil/keep.md", restore=None)
    assert outside.read_bytes() == b"keep"
