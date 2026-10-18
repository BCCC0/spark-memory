"""The public queued-write contract over real HTTP and CLI processes."""

from __future__ import annotations

import time
import json

from conftest import note_text


def _until_read(ready, note_id: str, timeout: float = 15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = ready.cli_json("read", note_id)
        if record.get("status") != "pending":
            return record
        time.sleep(0.05)
    raise AssertionError(f"{note_id} remained pending")


def test_add_returns_queue_receipt_and_read_converges(ready):
    ready.write("queued.md", note_text(summary="Queued example"))
    receipt = json.loads(ready.cli("add", "queued.md", "-m", "queue it").stdout)
    assert receipt["status"] == "queued"
    assert receipt["affected_ids"] == ["queued"]
    assert "head" not in receipt

    flushed = ready.flush()
    assert flushed["status"] == "flushed"
    record = ready.cli_json("read", "queued")
    assert record["summary"] == "Queued example"


def test_add_prechecks_missing_summary_locally(ready):
    ready.write("bad.md", "# No summary\n")
    result = ready.cli("add", "bad.md")
    assert result.returncode == 4
    assert "summary" in result.stderr


def test_busy_file_is_rejected_and_read_search_show_hints(ready):
    write_lock = ready._uvicorn.config.app.state.lock
    write_lock.acquire("test-holder")
    try:
        ready.write("busy.md", note_text(summary="Busy"))
        accepted = json.loads(ready.cli("add", "busy.md").stdout)
        pending = json.loads(ready.cli("read", "busy").stdout)
        assert pending["status"] == "pending"
        assert pending["receipt"] == accepted["receipt"]
        assert ready.cli_json("search", "Busy")["queue_pending"] >= 1
        refused = ready.cli("add", "busy.md", "-m", "second write")
        assert refused.returncode == 3
        assert "target_busy" in refused.stderr
    finally:
        write_lock.release()
    ready.flush()
    assert ready.cli_json("read", "busy")["summary"] == "Busy"
