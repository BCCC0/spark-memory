"""CLI move contract: exact IDs, topic remapping, references and atomic history."""

from __future__ import annotations

import base64
import json
from concurrent.futures import ThreadPoolExecutor

from conftest import note_text


def test_file_move_updates_links_and_frontmatter(ready):
    ready.write("source.md", note_text(body="## Why\nA decision."))
    ready.cli_json("add", "source.md", "--as", "ops/source")
    backlink = note_text(body="See [decision](ops/source.md#why) and [again](ops/source).")
    backlink = backlink.replace("status: active", "status: active\nsupersedes: ops/source")
    ready.write("backlink.md", backlink)
    ready.cli_json("add", "backlink.md", "--as", "ops/backlink")
    before = ready.cli_json("status")["head"]

    preview = ready.cli_json("mv", "--id", "ops/source", "ops/renamed", "--dry-run")
    assert preview["moved"] == {"ops/source": "ops/renamed"}
    assert preview["references_updated"] == ["ops/backlink"]
    assert ready.cli_json("status")["head"] == before

    result = ready.cli_json("mv", "--id", "ops/source", "ops/renamed", "--expect-head", before)
    assert result["changed"] is True
    assert result["head"] != before
    assert ready.cli("read", "ops/source").returncode == 9
    assert ready.cli_json("read", "ops/renamed")["id"] == "ops/renamed"
    updated = (ready.repo / "projects/demo/ops/backlink.md").read_text(encoding="utf-8")
    assert "ops/renamed.md#why" in updated
    assert "[again](ops/renamed)" in updated
    assert "supersedes: ops/renamed" in updated
    assert ready.cli_json("search", "decision")["hits"]


def test_topic_move_preserves_text_and_pointer_targets(ready):
    text = ready.write("guide.txt", "Topic guide\n")
    ready.cli_json("add", str(text), "--as", "tools/setup/guide.txt")
    binary = ready.workdir / "tool.bin"
    binary.write_bytes(bytes([0, 1, 2]))
    ready.cli_json("add", str(binary), "--as", "tools/setup/tool.bin")
    target = ready.cli_json("read", "tools/setup/tool.bin")["local_path"]
    result = ready.cli_json("mv", "--id", "tools/", "archive/tools/")
    assert result["moved"] == {
        "tools/setup/guide.txt": "archive/tools/setup/guide.txt",
        "tools/setup/tool.bin": "archive/tools/setup/tool.bin",
    }
    assert (ready.repo / "projects/demo/archive/tools/setup/guide.txt").read_bytes() == text.read_bytes()
    assert ready.cli_json("read", "archive/tools/setup/tool.bin")["local_path"] == target
    assert ready.cli_json("manifest", "tools")["entries"] == []
    assert len(ready.cli_json("manifest", "archive/tools")["entries"]) == 2


def test_move_rejects_collisions_wildcards_and_stale_head(ready):
    ready.write("first.md", note_text())
    ready.cli_json("add", "first.md", "--as", "first")
    ready.write("second.md", note_text())
    ready.cli_json("add", "second.md", "--as", "second")
    head = ready.cli_json("status")["head"]
    assert ready.cli("mv", "--id", "first", "second").returncode == 4
    assert ready.cli("mv", "--id", "first*", "other").returncode == 4
    assert ready.cli("mv", "--id", "first", "other", "--expect-head", "0" * 40).returncode == 7
    assert ready.cli_json("status")["head"] == head
    assert ready.cli_json("read", "first")["id"] == "first"


def test_folder_cannot_move_into_itself(ready):
    ready.write("a.md", note_text())
    ready.cli_json("add", "a.md", "--as", "tools/a")
    assert ready.cli("mv", "--id", "tools/", "tools/nested/").returncode == 4


def test_move_replay_and_size_limit(ready, monkeypatch):
    from mem.server import moves

    ready.write("a.md", note_text(body="[self](a)"))
    ready.cli_json("add", "a.md", "--as", "a")
    ready.write("b.md", note_text(body="[a](a)"))
    ready.cli_json("add", "b.md", "--as", "b")
    head = ready.cli_json("status")["head"]
    monkeypatch.setattr(moves, "MAX_MOVE_RECORDS", 1)
    assert ready.cli("mv", "--id", "a", "c").returncode == 4
    assert ready.cli_json("status")["head"] == head
    monkeypatch.setattr(moves, "MAX_MOVE_RECORDS", 100)
    first = ready.cli_json("mv", "--id", "a", "c")
    replay = ready.cli_json("mv", "--id", "a", "c")
    assert replay["idempotent_replay"] is True
    assert replay["head"] == first["head"]


def test_failed_move_rolls_back_all_paths(ready, monkeypatch):
    from mem.errors import MemError
    from mem.server.repo import MemoryRepo

    ready.write("a.md", note_text(body="[self](a)"))
    ready.cli_json("add", "a.md", "--as", "a")
    original = (ready.repo / "projects/demo/a.md").read_bytes()
    head = ready.cli_json("status")["head"]
    previous = MemoryRepo.commit_paths

    def fail_move(self, message, relpaths, *, device_id=None):
        if message.startswith("mem: mv"):
            raise MemError("internal_error", "simulated Git failure")
        return previous(self, message, relpaths, device_id=device_id)

    monkeypatch.setattr(MemoryRepo, "commit_paths", fail_move)
    accepted = ready.cli("mv", "--id", "a", "c")
    assert accepted.returncode == 1
    assert "queued_write_failed" in accepted.stderr
    assert ready.cli_json("status")["head"] == head
    assert (ready.repo / "projects/demo/a.md").read_bytes() == original
    assert not (ready.repo / "projects/demo/c.md").exists()
    assert not (ready.state / "move-journal.json").exists()


def test_recover_interrupted_move_before_commit(ready):
    from mem.server.moves import recover_move
    from mem.server.repo import MemoryRepo

    ready.write("a.md", note_text())
    ready.cli_json("add", "a.md", "--as", "a")
    source = ready.repo / "projects/demo/a.md"
    original = source.read_bytes()
    destination = ready.repo / "projects/demo/c.md"
    head = ready.cli_json("status")["head"]
    journal = ready.state / "move-journal.json"
    journal.write_text(json.dumps({
        "head": head, "paths": ["projects/demo/a.md", "projects/demo/c.md"],
        "before": {"projects/demo/a.md": base64.b64encode(original).decode("ascii"),
                   "projects/demo/c.md": None},
    }), encoding="utf-8")
    source.unlink()
    destination.write_bytes(original)
    assert recover_move(MemoryRepo(ready.repo), ready.state) is True
    assert source.read_bytes() == original
    assert not destination.exists()
    assert not journal.exists()


def test_move_reindexes_text_and_embeddings(ready):
    ready.write("a.md", note_text(body="A unique migration lantern for search."))
    ready.cli_json("add", "a.md", "--as", "old/a")
    ready.wait_for_vectors()
    ready.cli_json("mv", "--id", "old/", "new/")
    text_hits = ready.cli_json("search", "migration lantern")["hits"]
    assert "new/a" in [hit["id"] for hit in text_hits]
    assert "old/a" not in [hit["id"] for hit in text_hits]
    ready.wait_for_vectors()
    hybrid = ready.cli_json("search", "migration lantern", "--mode", "hybrid")
    assert "new/a" in [hit["id"] for hit in hybrid["hits"]]
    assert "old/a" not in [hit["id"] for hit in hybrid["hits"]]


def test_concurrent_moves_commit_only_one_destination(ready):
    from mem.client import Client
    from mem.errors import MemError

    ready.write("a.md", note_text())
    ready.cli_json("add", "a.md", "--as", "a")
    client = Client(ready.server, ready.token())

    def attempt(destination):
        try:
            return client.mv("demo", "a", destination,
                             idempotency_key=f"move-a-to-{destination}")
        except MemError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ("b", "c")))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, MemError) for result in results) == 1
    ready.flush()
    assert sum((ready.repo / f"projects/demo/{name}.md").exists() for name in ("b", "c")) == 1
    assert not (ready.repo / "projects/demo/a.md").exists()
    assert ready.cli_json("status")["index_dirty"] is False


def test_rewrite_preserves_crlf_and_quoted_frontmatter():
    from mem.server.moves import _rewrite_note

    original = ("---\r\nstatus: active\r\nsupersedes: 'old/a'\r\n"
                "title: Keep spacing\r\n---\r\nSee [note](old/a.md#part).\r\n")
    updated = _rewrite_note(original, {"old/a": "new/a"}, "backlink")
    assert updated == original.replace("old/a", "new/a").encode("utf-8")


def test_recover_committed_move_records_replay(ready):
    from mem.server.moves import recover_move
    from mem.server.repo import MemoryRepo
    from mem.server.idempotency import IdempotencyStore

    ready.write("a.md", note_text())
    ready.cli_json("add", "a.md", "--as", "a")
    old_head = ready.cli_json("status")["head"]
    result = ready.cli_json("mv", "--id", "a", "b")
    destination = ready.repo / "projects/demo/b.md"
    body = destination.read_bytes()
    destination.unlink()
    journal = ready.state / "move-journal.json"
    journal.write_text(json.dumps({
        "head": old_head, "paths": ["projects/demo/a.md", "projects/demo/b.md"],
        "before": {"projects/demo/a.md": None, "projects/demo/b.md": None},
        "after": {"projects/demo/a.md": None,
                  "projects/demo/b.md": base64.b64encode(body).decode("ascii")},
        "key": "crash-replay", "response": {"source": "a", "destination": "b"},
        "message": "mem: mv a b",
    }), encoding="utf-8")
    store = IdempotencyStore(ready.state / "idempotency.json")
    assert recover_move(MemoryRepo(ready.repo), ready.state, store) is True
    assert destination.read_bytes() == body
    assert store.get("crash-replay")["head"] == result["head"]
