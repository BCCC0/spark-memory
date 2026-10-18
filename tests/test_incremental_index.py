"""The text index advances by changed path, not by rebuilding the corpus."""

from __future__ import annotations

from mem.ids import note_relpath
from mem.server.repo import MemoryRepo
from mem.server.search import SearchIndex
from conftest import note_text


def _add(repo: MemoryRepo, note_id: str, summary: str, body: str = "body") -> str:
    path = note_relpath("demo", note_id)
    repo.write_note(path, note_text(summary=summary, body=body).encode("utf-8"))
    repo.commit_paths(f"write {note_id}", [path])
    return path


def test_ensure_reads_only_the_changed_path(tmp_path, monkeypatch):
    repo = MemoryRepo(tmp_path / "repo")
    repo.ensure()
    for note_id in ("alpha", "beta", "gamma"):
        _add(repo, note_id, note_id, f"{note_id}-token")

    index = SearchIndex(tmp_path / "search.db")
    assert index.ensure(repo, repo.head()) is True
    assert [hit["id"] for hit in index.search("alpha-token")] == ["alpha"]

    read: list[str] = []
    original = repo.read_note

    def recording_read(path, revision=None):
        read.append(path)
        return original(path, revision)

    monkeypatch.setattr(repo, "read_note", recording_read)
    new_path = _add(repo, "delta", "delta", "delta-token")

    assert index.ensure(repo, repo.head()) is True
    assert read == [new_path], read
    assert [hit["id"] for hit in index.search("delta-token")] == ["delta"]
    assert [hit["id"] for hit in index.search("alpha-token")] == ["alpha"]


def test_a_move_deletes_the_old_path_and_indexes_the_new_one(tmp_path):
    repo = MemoryRepo(tmp_path / "repo")
    repo.ensure()
    old = _add(repo, "old", "old", "renamed-token")
    index = SearchIndex(tmp_path / "search.db")
    index.ensure(repo, repo.head())
    assert [hit["id"] for hit in index.search("renamed-token")] == ["old"]

    new = note_relpath("demo", "new")
    text = repo.read_note(old)
    repo.remove_note(old)
    repo.write_note(new, text.encode("utf-8"))
    repo.commit_paths("move old to new", [old, new])

    assert index.ensure(repo, repo.head()) is True
    hits = index.search("renamed-token")
    assert [hit["id"] for hit in hits] == ["new"], hits


def test_rewritten_history_falls_back_to_a_full_rebuild(tmp_path, monkeypatch):
    repo = MemoryRepo(tmp_path / "repo")
    repo.ensure()
    paths = [_add(repo, f"n{i}", f"note {i}", f"token-{i}") for i in range(3)]
    index = SearchIndex(tmp_path / "search.db")
    index.ensure(repo, repo.head())

    # Amend the last commit: the prior indexed head is no longer an ancestor.
    target = paths[0]
    repo.write_note(target, note_text(summary="amended", body="amended-token").encode("utf-8"))
    repo.git("add", "-A", "--", target)
    repo.git("commit", "--amend", "-m", "rewritten history")

    read: list[str] = []
    original = repo.read_note

    def recording_read(path, revision=None):
        read.append(path)
        return original(path, revision)

    monkeypatch.setattr(repo, "read_note", recording_read)
    assert index.ensure(repo, repo.head()) is True
    assert sorted(read) == sorted(paths), read
    assert [hit["id"] for hit in index.search("amended-token")] == ["n0"]
