"""The eight acceptance checks from ``MEM.md``, as real runnable tests.

Each one is an end-to-end claim about the protocol: the real daemon over real
HTTP, the real CLI as a subprocess. Test 8 is expected-skip by decision
(``MEM-RESOLUTIONS.md`` S8) and must not be reported as passing.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from conftest import note_text


# 1 ---------------------------------------------------------------------


def test_1_no_body_through_the_cli(ready):
    ready.write("short.md", note_text(summary="short one"))
    # The large note carries a token that appears nowhere else, so the search
    # genuinely exercises the >threshold path instead of only the small one.
    ready.write("long.md", note_text(summary="long one", body="uniqueword " + "x" * 9000))
    ready.cli_json("add", "short.md", "--as", "short")
    ready.cli_json("add", "long.md", "--as", "long")

    listed = ready.cli_json("ls")
    assert listed["notes"], "expected notes to be listed"
    for record in listed["notes"]:
        assert "body" not in record, "ls must never return a body"

    found = ready.cli_json("search", "uniqueword")
    assert found["hits"], "expected the large note to be found"
    for hit in found["hits"]:
        assert "body" not in hit, "search must never return a body, at any size"

    small = ready.cli_json("search", "prose")
    assert small["hits"]
    for hit in small["hits"]:
        assert "body" not in hit


# 2 ---------------------------------------------------------------------


def test_2_threshold_is_signalled(ready):
    ready.write("long.md", note_text(summary="big", body="y" * 9000))
    ready.cli_json("add", "long.md", "--as", "big")

    record = ready.cli_json("read", "big")
    assert record["bytes"] > 4096
    assert record["truncated"] is True, "a bounded read must say so"
    assert "body" not in record, "a >threshold note must not be inlined"
    assert Path(record["origin_path"]).is_file(), "the authoring device gets its own file"
    assert "s=" in record["fetch_url"], "and still a way to fetch the committed bytes"
    assert record["source"] in {"local", "server", "same", "none"}

    # ... and the same note resolves by pinned revision.
    pinned = ready.cli_json("read", "big", "--revision", record["revision"])
    assert pinned["sha256"] == record["sha256"]


def test_2b_small_note_is_inlined(ready):
    ready.write("small.md", note_text(summary="small"))
    ready.cli_json("add", "small.md", "--as", "small")
    record = ready.cli_json("read", "small")
    assert record["truncated"] is False
    assert "body" in record
    assert record["summary"] == "small"


# 3 ---------------------------------------------------------------------


def test_3_refused_write_when_server_unreachable(ready):
    ready.write("late.md", note_text(summary="cannot land"))
    before = sorted(p.relative_to(ready.repo).as_posix() for p in ready.repo.rglob("*.md"))

    ready.stop()
    result = ready.cli("add", "late.md", "--as", "late")
    assert result.returncode == 2, f"expected exit 2, got {result.returncode}: {result.stderr}"

    after = sorted(p.relative_to(ready.repo).as_posix() for p in ready.repo.rglob("*.md"))
    assert before == after, "a refused write must not have written anything"


# 4 ---------------------------------------------------------------------


def test_4_stale_write_is_refused(ready):
    # Note this is also the idempotency-collision case: the final add sends the
    # same bytes as the previous successful one, so a key derived from
    # content+path alone would be a *replay* and would skip the CAS entirely
    # (MEM-RESOLUTIONS.md S21). The key must cover the precondition.
    ready.write("drift.md", note_text(summary="original"))
    ready.cli_json("add", "drift.md", "--as", "drift")

    original = ready.cli_json("read", "drift")
    stale_sha = original["sha256"]

    # Move the note out from under the client.
    ready.write("drift.md", note_text(summary="changed elsewhere"))
    ready.cli_json("add", "drift.md", "--as", "drift")
    moved = ready.cli_json("read", "drift")
    assert moved["sha256"] != stale_sha

    result = ready.cli("add", "drift.md", "--as", "drift", "--expect", stale_sha)
    assert result.returncode == 7, f"expected exit 7, got {result.returncode}: {result.stderr}"

    unchanged = ready.cli_json("read", "drift")
    assert unchanged["sha256"] == moved["sha256"], "the refused write must not have applied"


# 5 ---------------------------------------------------------------------


def test_5_idempotent_retry_creates_one_note(ready):
    ready.write("once.md", note_text(summary="only once"))
    first = ready.cli_json("add", "once.md", "--as", "once")
    second = ready.cli_json("add", "once.md", "--as", "once")

    assert second.get("idempotent_replay") is True
    assert first["sha256"] == second["sha256"]

    # The falsifiable claim. Counting notes cannot fail here -- with id == path,
    # two notes at one id are unrepresentable -- so the real assertion is that a
    # replay does not create a SECOND commit.
    assert ready.cli_json("status")["head"] == first["head"], (
        "a replay must not move HEAD"
    )
    listed = [n["id"] for n in ready.cli_json("ls")["notes"]]
    assert listed.count("once") == 1


# 6 ---------------------------------------------------------------------


def test_6_lock_is_released_when_a_client_dies(ready):
    """A dying client cannot block the next write.

    Note what this can and cannot prove: the lock is a **server-side, per-request
    mutex** (``MEM-RESOLUTIONS.md`` B1), so a client never holds it and killing
    one is structurally harmless. The observable consequences are asserted here
    -- the lock is free afterwards and the next write succeeds. A leaked lock
    would surface as ``lock_timeout`` in the *following* test, which is why
    ``test_6b`` hammers the lock directly.
    """
    ready.write("victim.md", note_text(summary="interrupted", body="z" * 4000))
    proc = subprocess.Popen(
        [sys.executable, "-m", "mem.cli", "add", "victim.md", "--as", "victim"],
        env=ready.env,
        cwd=str(ready.workdir),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(0.25)
    proc.kill()
    proc.wait(timeout=10)

    ready.write("after.md", note_text(summary="after the death"))
    result = ready.cli("add", "after.md", "--as", "after")
    assert result.returncode == 0, f"a dead client must not block the next write: {result.stderr}"

    ready.flush()
    state = ready.cli_json("status")["lock"]
    assert state["held"] is False, "the lock must be free once the request is over"


def test_6b_concurrent_writes_all_succeed(ready):
    """A lock leak would make some of these fail with lock_timeout."""
    names = [f"c{i}" for i in range(1, 7)]
    for name in names:
        ready.write(f"{name}.md", note_text(summary=name))

    results: dict[str, int] = {}
    guard = threading.Lock()

    def one(name: str) -> None:
        out = ready.cli("add", f"{name}.md", "--as", name)
        with guard:
            results[name] = out.returncode

    threads = [threading.Thread(target=one, args=(name,)) for name in names]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert set(results) == set(names), "every write must have finished"
    assert all(code == 0 for code in results.values()), f"expected all zero: {results}"
    ready.flush()
    assert ready.cli_json("status")["lock"]["held"] is False


# 7 ---------------------------------------------------------------------


def test_7_rebuild_loses_nothing(ready):
    ready.write("a.md", note_text(summary="alpha"))
    ready.write("b.md", note_text(summary="beta", body="searchable token"))
    ready.cli_json("add", "a.md", "--as", "a")
    ready.cli_json("add", "b.md", "--as", "b")

    before_list = ready.cli_json("ls")
    before_search = ready.cli_json("search", "searchable")
    before_read = ready.cli_json("read", "b")

    # Rebuild derived search state from Git; the durable queue stays intact.
    ready.cli_json("index", "--rebuild")
    ready.flush()

    after_list = ready.cli_json("ls")
    after_search = ready.cli_json("search", "searchable")
    after_read = ready.cli_json("read", "b")

    assert [n["id"] for n in before_list["notes"]] == [n["id"] for n in after_list["notes"]]
    assert [h["id"] for h in before_search["hits"]] == [h["id"] for h in after_search["hits"]]
    assert before_read["sha256"] == after_read["sha256"]
    assert before_read["body"] == after_read["body"]


# 8 ---------------------------------------------------------------------


def test_8_cross_site_base_url_is_configurable(ready):
    from mem.server.app import Settings, build_record
    from mem.server.repo import MemoryRepo

    ready.write("x.md", note_text(summary="cross site"))
    ready.cli_json("add", "x.md", "--as", "x")
    settings = Settings(
        repo_root=ready.repo, devices_db=ready.tmp / "mem" / "devices.db",
        enroll_secret=ready.secret_file, state_dir=ready.state,
        base_url="http://192.0.2.10:8321", link_secret=ready.link_secret_file,
        max_bytes=1,
    )
    record = build_record(MemoryRepo(ready.repo), "demo", "x", settings, max_bytes=1)
    assert record["fetch_url"].startswith("http://192.0.2.10:8321/")
