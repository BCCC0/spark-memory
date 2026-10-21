"""Regressions for the pre-public server hardening pass."""

from __future__ import annotations

import json
import threading
import urllib.request

import pytest

from conftest import Harness, note_text
from mem.client import Client
from mem.errors import MemError
from mem.server.app import Reindexer
from mem.server.repo import MemoryRepo
from mem.server.search import SearchIndex


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


def test_retrying_a_write_replays_the_committed_result(ready):
    """Synchronous writes keep idempotency: a retry returns the first result."""
    client = Client(ready.server, ready.token())
    body = note_text(summary="published", body="replay-token").encode("utf-8")

    first = client.add(
        "demo", "published", body,
        filename="published.md", idempotency_key="replay",
    )
    assert first["status"] == "committed", first
    assert first["head"]

    retry = client.add(
        "demo", "published", body,
        filename="published.md", idempotency_key="replay",
    )
    assert retry["idempotent_replay"] is True
    assert retry["receipt"] == first["receipt"]
    assert retry["head"] == first["head"]
    assert ready.cli_json("read", "published")["summary"] == "published"


def test_queue_endpoint_is_a_compatibility_stub(ready):
    progress = Client(ready.server, ready.token()).queue_progress()
    assert progress["pending"] == 0
    assert progress["through"] == 0
    assert "reindex" in progress
    assert progress["head"] == progress["head"]


def test_nudge_respects_a_failed_passes_backoff():
    class Fake:
        _failure_delay = 1.0

        def __init__(self):
            self.requests = 0

        def request(self):
            self.requests += 1

    fake = Fake()
    Reindexer.nudge(fake)
    assert fake.requests == 0

    fake._failure_delay = 0.0
    Reindexer.nudge(fake)
    assert fake.requests == 1


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
