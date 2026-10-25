"""Regression coverage for the 0.2.0 pre-merge correctness gate."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from mem import cli
from mem.client import Client
from mem.errors import MemError
from mem.server.app import Reindexer
from mem.server.catalog import Catalog
from mem.server.idempotency import IdempotencyStore
from mem.server.repo import MemoryRepo
from mem.server.search import SearchIndex

from conftest import Harness, note_text


def test_hook_timeout_scales_with_batch():
    assert cli._hook_timeout(1) == 180.0
    assert cli._hook_timeout(101) == 180.0
    assert cli._hook_timeout(1000) == 560.0
    assert cli._hook_timeout(10000) == 900.0


def _commits_touching(repo, relpath: str) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(repo), "log", "--format=%H", "--", relpath],
        capture_output=True, text=True, check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def test_concurrent_same_key_commits_once_and_replays(ready):
    client = Client(ready.server, ready.token())
    bodies = [
        note_text(summary="first", body="one").encode("utf-8"),
        note_text(summary="second", body="two").encode("utf-8"),
    ]

    def add(body):
        return client.add(
            "demo", "race", body, filename="race.md",
            idempotency_key="same-key", message="same-key",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(add, bodies))

    assert len({row["head"] for row in results}) == 1
    assert len({row["receipt"] for row in results}) == 1
    assert sum(row["idempotent_replay"] for row in results) == 1
    assert len(_commits_touching(ready.repo, "projects/demo/race.md")) == 1


def test_one_key_cannot_replay_across_operations(ready):
    client = Client(ready.server, ready.token())
    client.add(
        "demo", "shared", note_text(summary="shared").encode("utf-8"),
        filename="shared.md", idempotency_key="shared",
    )
    removed = client.rm("demo", "shared", idempotency_key="shared")
    assert removed["removed"] is True
    assert removed["idempotent_replay"] is False
    with pytest.raises(MemError):
        client.note("demo", "shared")


def test_a_failed_write_stores_no_replay_entry(ready, monkeypatch):
    client = Client(ready.server, ready.token())
    original = MemoryRepo.commit_paths

    def fail(self, *args, **kwargs):
        raise MemError("internal_error", "injected commit failure")

    monkeypatch.setattr(MemoryRepo, "commit_paths", fail)
    with pytest.raises(MemError):
        client.add(
            "demo", "retry", note_text(summary="retry").encode("utf-8"),
            filename="retry.md", idempotency_key="retry-key",
        )
    store_path = ready.state / "idempotency.json"
    stored = json.loads(store_path.read_text(encoding="utf-8")) if store_path.exists() else {}
    assert not any(key.endswith(":retry-key") for key in stored)

    monkeypatch.setattr(MemoryRepo, "commit_paths", original)
    retried = client.add(
        "demo", "retry", note_text(summary="retry").encode("utf-8"),
        filename="retry.md", idempotency_key="retry-key",
    )
    assert retried["idempotent_replay"] is False
    assert len(_commits_touching(ready.repo, "projects/demo/retry.md")) == 1


def test_idempotency_store_is_thread_safe(tmp_path):
    store = IdempotencyStore(tmp_path / "idem.json")
    errors: list[BaseException] = []

    def worker(number: int) -> None:
        try:
            for index in range(250):
                store.put(f"{number}:{index}", {"head": "h" * 40, "changed": True})
        except BaseException as exc:  # noqa: BLE001 - the point is to catch it
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(worker, range(8)))
    assert errors == []
    assert len(store) == 1000


def test_rm_leaves_an_untracked_worktree_file_alone(ready):
    path = ready.repo / "projects/demo/untracked.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(note_text(summary="not committed"), encoding="utf-8")
    result = ready.cli_json("rm", "untracked")
    assert result["removed"] is False
    assert result["already_absent"] is True
    assert result["changed"] is False
    assert path.exists()


def test_rm_commits_a_deletion_that_is_staged(ready):
    ready.write("staged.md", note_text(summary="staged"))
    ready.cli_json("add", "staged.md", "--as", "staged")
    path = ready.repo / "projects/demo/staged.md"
    path.unlink()
    subprocess.run(["git", "-C", str(ready.repo), "add", "-A", "--", "projects/demo/staged.md"], check=True)
    result = ready.cli_json("rm", "staged")
    assert result["removed"] is True
    assert result["changed"] is True
    probe = subprocess.run(
        ["git", "-C", str(ready.repo), "cat-file", "-e", "HEAD:projects/demo/staged.md"],
        capture_output=True,
    )
    assert probe.returncode != 0


def test_rm_still_removes_a_note_whose_worktree_file_was_deleted(ready):
    ready.write("gone.md", note_text(summary="gone"))
    ready.cli_json("add", "gone.md", "--as", "gone")
    (ready.repo / "projects/demo/gone.md").unlink()
    result = ready.cli_json("rm", "gone")
    assert result["removed"] is True
    assert result["changed"] is True


def _seed(repo: MemoryRepo, name: str, summary: str) -> str:
    path = f"projects/demo/{name}.md"
    target = repo.root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"---\nsummary: {summary}\n---\n\n# {summary}\n\nbody\n", encoding="utf-8")
    return path


def test_legacy_running_job_is_reset_and_drained(tmp_path, repo_template):
    from mem.server.queue import WriteQueue

    harness = Harness(tmp_path, template=repo_template)
    harness.start()
    try:
        harness.login()
        harness.init("demo")
        device_id = harness.cli_json("devices")[0]["device_id"]
    finally:
        harness.stop()

    body = note_text(summary="legacy running").encode("utf-8")
    queue = WriteQueue(harness.state, harness.repo)
    submitted = queue.submit(
        project="demo", kind="add", targets=["legacy"],
        manifest={
            "file_count": 1, "device_id": device_id, "project": "demo",
            "id": "legacy", "rel": "projects/demo/legacy.md",
            "message": "legacy add", "if_match": None, "kind": "note",
        },
        files=[body], idem=None,
    )
    with sqlite3.connect(queue.db) as db:
        db.execute("UPDATE jobs SET status='running' WHERE id=?", (submitted["receipt"],))

    harness.start()
    try:
        record = harness.cli_json("read", "legacy")
        assert record["summary"] == "legacy running"
        with sqlite3.connect(queue.db) as db:
            status = db.execute(
                "SELECT status FROM jobs WHERE id=?", (submitted["receipt"],)
            ).fetchone()[0]
        assert status == "committed"
    finally:
        harness.stop()


def test_catalog_does_not_rewind_to_an_older_head(tmp_path):
    repo = MemoryRepo(tmp_path / "repo")
    repo.ensure()
    first = _seed(repo, "alpha", "Alpha")
    repo.commit_paths("add alpha", [first])
    old = repo.head()
    catalog = Catalog(tmp_path / "catalog.db")
    catalog.ensure(repo, old)

    second = _seed(repo, "beta", "Beta")
    repo.commit_paths("add beta", [second])
    new = repo.head()
    catalog.ensure(repo, new)
    assert catalog.ensure(repo, old) is False
    assert catalog.head() == new
    assert {row["note_id"] for row in catalog.rows()} == {"alpha", "beta"}


class _FakeRepo:
    def __init__(self, head: str, bodies: dict[str, bytes]):
        self.head_value = head
        self.bodies = bodies

    @contextmanager
    def pinned(self):
        yield self.head_value

    def head(self) -> str:
        return self.head_value

    def notes(self):
        return [SimpleNamespace(path=path) for path in self.bodies]

    def read_notes(self, paths):
        return {path: self.bodies.get(path) for path in paths}


class _FakeCatalog:
    def __init__(self, hashes: dict[str, str]):
        self.hashes = hashes

    def ensure(self, repo, head):
        return False

    def rows(self):
        return [{"path": path, "sha256": sha} for path, sha in self.hashes.items()]


class _FakeIndex:
    def __init__(self, head: str):
        self.head = head

    def indexed_head(self):
        return self.head

    def ensure(self, repo, head):
        self.head = head


class _FakeVectors:
    def __init__(self):
        self.seen: list[dict[str, str]] = []

    def sync(self, metas, source_for, head):
        self.seen.append({meta.path: source_for(meta.path).body for meta in metas})


def test_reindexer_caches_the_body_it_actually_read():
    path = "projects/demo/alpha.md"
    body_one = b"---\nsummary: one\n---\n\n# One\n\nversion one\n"
    body_two = b"---\nsummary: two\n---\n\n# Two\n\nversion two\n"
    repo = _FakeRepo("h1", {path: body_one})
    # The catalog has already advanced to h2 while this pass is pinned to h1.
    catalog = _FakeCatalog({path: hashlib.sha256(body_two).hexdigest()})
    index = _FakeIndex("h1")
    vectors = _FakeVectors()
    worker = Reindexer(repo, index, vectors, catalog)

    worker._run_once()
    assert worker._source_cache[path][0] == hashlib.sha256(body_one).hexdigest()
    assert "version one" in vectors.seen[-1][path]

    # The next pass is pinned to h2 and must actually re-read the new body,
    # not reuse the stale entry because the catalog sha matched.
    repo.bodies[path] = body_two
    repo.head_value = "h2"
    index.head = "h2"
    catalog.hashes[path] = hashlib.sha256(body_two).hexdigest()
    worker._run_once()
    assert "version two" in vectors.seen[-1][path]


def test_text_index_does_not_rewind_to_an_older_head(tmp_path):
    repo = MemoryRepo(tmp_path / "repo")
    repo.ensure()
    first = _seed(repo, "alpha", "Alpha")
    repo.commit_paths("add alpha", [first])
    old = repo.head()
    index = SearchIndex(tmp_path / "search.db")
    index.ensure(repo, old)

    second = _seed(repo, "beta", "Beta")
    repo.commit_paths("add beta", [second])
    new = repo.head()
    index.ensure(repo, new)
    assert index.ensure(repo, old) is False
    assert index.indexed_head() == new
