"""Durable queue boundaries independent of the HTTP worker."""

from concurrent.futures import ThreadPoolExecutor
import subprocess
import sys

import pytest

from mem.errors import MemError
from mem.server.queue import WriteQueue


def _submit(queue: WriteQueue, project: str, note_id: str, key: str):
    return queue.submit(project=project, kind="add", targets=[note_id],
                        manifest={"file_count": 0}, files=[], idem=key)


def test_idempotency_is_scoped_to_project(tmp_path):
    queue = WriteQueue(tmp_path)
    first = _submit(queue, "alpha", "same", "key")
    second = _submit(queue, "beta", "same", "key")
    assert first["receipt"] != second["receipt"]
    assert queue.lookup("alpha", "", "key")["receipt"] == first["receipt"]
    assert queue.lookup("beta", "", "key")["receipt"] == second["receipt"]


def test_idempotency_is_scoped_to_device(tmp_path):
    queue = WriteQueue(tmp_path)
    first = queue.submit(project="demo", kind="add", targets=["same"],
                         manifest={"file_count": 0, "device_id": "a"}, files=[], idem="key")
    assert queue.lookup("demo", "a", "key")["receipt"] == first["receipt"]
    assert queue.lookup("demo", "b", "key") is None


def test_two_queue_instances_cannot_reserve_the_same_id(tmp_path):
    left, right = WriteQueue(tmp_path), WriteQueue(tmp_path)

    def submit(queue):
        try:
            return _submit(queue, "demo", "same", "left" if queue is left else "right")
        except MemError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, (left, right)))
    assert sum(isinstance(row, dict) for row in results) == 1
    assert [row.code for row in results if isinstance(row, MemError)] == ["target_busy"]


def test_only_one_process_can_publish_queue(tmp_path):
    left, right = WriteQueue(tmp_path), WriteQueue(tmp_path)
    left.claim_worker()
    try:
        try:
            right.claim_worker()
        except MemError as exc:
            assert exc.code == "target_busy"
        else:
            raise AssertionError("second worker acquired the queue")
    finally:
        left.release_worker()
    right.claim_worker()
    right.release_worker()


def test_publisher_lock_is_keyed_to_the_repository(tmp_path):
    """Different state dirs must not create two publishers for one repo."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    left = WriteQueue(tmp_path / "state-a", repo)
    right = WriteQueue(tmp_path / "state-b", repo)

    left.claim_worker()
    try:
        with pytest.raises(MemError) as blocked:
            right.claim_worker()
        assert blocked.value.code == "target_busy"
    finally:
        left.release_worker()

    right.claim_worker()
    right.release_worker()


def test_publisher_lock_resolves_a_worktree_git_file(tmp_path):
    """A worktree's `.git` is a file; its lock must use the common git dir."""
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True
        )

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    git("commit", "--allow-empty", "-qm", "base")
    worktree = tmp_path / "worktree"
    git("worktree", "add", "-q", str(worktree), "HEAD")
    assert (worktree / ".git").is_file()

    left = WriteQueue(tmp_path / "state-a", worktree)
    right = WriteQueue(tmp_path / "state-b", repo)
    left.claim_worker()
    try:
        with pytest.raises(MemError) as blocked:
            right.claim_worker()
        assert blocked.value.code == "target_busy"
    finally:
        left.release_worker()

    right.claim_worker()
    right.release_worker()
    assert (repo / ".git" / "mem-worker.lock").exists()


def test_worker_lock_is_visible_across_processes(tmp_path):
    queue = WriteQueue(tmp_path)
    code = (
        "import sys; from pathlib import Path; from mem.server.queue import WriteQueue; "
        "from mem.errors import MemError; q=WriteQueue(Path(sys.argv[1])); "
        "\ntry: q.claim_worker()\nexcept MemError: sys.exit(3)\n"
        "else: q.release_worker(); sys.exit(0)"
    )
    queue.claim_worker()
    try:
        blocked = subprocess.run([sys.executable, "-c", code, str(tmp_path)], check=False)
        assert blocked.returncode == 3
    finally:
        queue.release_worker()
    available = subprocess.run([sys.executable, "-c", code, str(tmp_path)], check=False)
    assert available.returncode == 0


def test_successful_retry_resolves_failed_flush_receipt(tmp_path):
    queue = WriteQueue(tmp_path)
    first = _submit(queue, "demo", "note", "key")
    queue.finish(first["receipt"], error={"code": "internal_error", "message": "failed"})
    assert [row["receipt"] for row in queue.progress()["failed"]] == [first["receipt"]]
    retry = _submit(queue, "demo", "note", "key")
    queue.finish(retry["receipt"], result={"head": "new"})
    assert queue.progress()["failed"] == []
