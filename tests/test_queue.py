"""The synchronous write contract over real HTTP and CLI processes."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from conftest import note_text


def test_add_returns_a_committed_result_and_read_converges(ready):
    ready.write("queued.md", note_text(summary="Queued example"))
    result = json.loads(ready.cli("add", "queued.md", "-m", "commit it").stdout)
    assert result["status"] == "committed"
    assert result["affected_ids"] == ["queued"]
    assert result["head"]

    flushed = ready.flush()
    assert flushed["status"] == "flushed"
    record = ready.cli_json("read", "queued")
    assert record["summary"] == "Queued example"


def test_add_prechecks_missing_summary_locally(ready):
    ready.write("bad.md", "# No summary\n")
    result = ready.cli("add", "bad.md")
    assert result.returncode == 4
    assert "summary" in result.stderr


def test_a_concurrent_writer_waits_for_the_write_lock(ready):
    """Writes serialise on the lock instead of being accepted into a queue."""
    write_lock = ready._uvicorn.config.app.state.lock
    write_lock.acquire("test-holder")
    ready.write("busy.md", note_text(summary="Busy"))
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        pending = pool.submit(ready.cli, "add", "busy.md")
        try:
            pending.result(timeout=1.0)
        except Exception:  # noqa: BLE001 - still blocked on the lock
            pass
        else:  # pragma: no cover - the lock should have blocked it
            raise AssertionError("the write did not wait for the lock")
    finally:
        write_lock.release()
    try:
        result = pending.result(timeout=30)
    finally:
        pool.shutdown()
    assert result.returncode == 0, result.stderr
    assert ready.cli_json("read", "busy")["summary"] == "Busy"
