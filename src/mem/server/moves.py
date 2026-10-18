"""Bounded, exact-ID move planning for the shared repository."""

from __future__ import annotations

import re
import json
import os
import base64
from dataclasses import dataclass
from pathlib import Path

from ..errors import MemError
from ..ids import note_relpath, text_relpath, validate_id
from ..pointers import pointer_record
from . import frontmatter as fm
from .repo import MemoryRepo

MAX_MOVE_RECORDS = 100
MAX_MOVE_BYTES = 8 * 1024 * 1024
_LINK = re.compile(r"\]\(\s*([^\s)]+)\s*\)")


@dataclass
class MovePlan:
    head: str
    source: str
    destination: str
    folder: bool
    moved: dict[str, str]
    writes: dict[str, bytes]
    removals: set[str]
    referenced: list[str]
    bytes_affected: int

    @property
    def paths(self) -> list[str]:
        return sorted(set(self.writes) | self.removals)

    def response(self) -> dict:
        return {
            "source": self.source + ("/" if self.folder else ""),
            "destination": self.destination + ("/" if self.folder else ""),
            "folder": self.folder,
            "moved": self.moved,
            "references_updated": self.referenced,
            "changed_records": len(self.writes),
            "bytes_affected": self.bytes_affected,
            "head_before": self.head,
        }


def _rewrite_links(body: str, mapping: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        raw = match.group(1)
        if raw.startswith(("http://", "https://", "mailto:", "#", "/", "./", "../")):
            return match.group(0)
        target, marker, anchor = raw.partition("#")
        suffix = ".md" if target.endswith(".md") else ""
        key = target[:-3] if suffix else target
        if key not in mapping:
            return match.group(0)
        return match.group(0).replace(raw, mapping[key] + suffix + (marker + anchor if marker else ""), 1)

    return _LINK.sub(replace, body)


def _rewrite_note(text: str, mapping: dict[str, str], note_id: str) -> bytes:
    data, _ = fm.parse(text)
    lines = text.splitlines(keepends=True)
    closing = next((index for index, line in enumerate(lines[1:], 1)
                    if line.strip() == "---"), None) if lines and lines[0].strip() == "---" else None
    header = lines[:closing + 1] if closing is not None else []
    body = "".join(lines[closing + 1:]) if closing is not None else text
    new_body = _rewrite_links(body, mapping)
    new_data = dict(data)
    for key in ("supersedes", "superseded_by"):
        old = data.get(key)
        if not isinstance(old, str) or old not in mapping:
            continue
        changed = False
        for index in range(1, len(header) - 1):
            match = re.match(rf"^({key}:[ \t]*)(.*?)(\r?\n)?$", header[index])
            if not match:
                continue
            scalar = match.group(2).strip()
            quote = scalar[:1] if scalar[:1] in ("'", '"') and scalar[-1:] == scalar[:1] else ""
            if scalar.strip(quote) != old:
                raise MemError("invalid_request", f"cannot safely rewrite {key} in {note_id}")
            header[index] = match.group(1) + scalar.replace(old, mapping[old], 1) + (match.group(3) or "")
            new_data[key] = mapping[old]
            changed = True
            break
        if not changed:
            raise MemError("invalid_request", f"cannot locate {key} in {note_id}")
    return ("".join(header) + new_body).encode("utf-8")


def plan_move(repo: MemoryRepo, project: str, source: str, destination: str,
              *, folder: bool) -> MovePlan:
    source = validate_id(source)
    destination = validate_id(destination)
    if source == "PROJECT" or destination == "PROJECT":
        raise MemError("invalid_request", "PROJECT is a reserved record")
    if source == destination:
        raise MemError("invalid_request", "source and destination are the same")
    if folder and destination.startswith(source + "/"):
        raise MemError("invalid_request", "a topic cannot move into itself")

    head = repo.head()
    metas = [meta for meta in repo.notes() if meta.project == project]
    selected = [meta for meta in metas if (meta.id.startswith(source + "/") if folder else meta.id == source)]
    if not selected:
        raise MemError("not_found", f"no {'topic' if folder else 'record'} at {source}")
    if not folder and len(selected) != 1:
        raise MemError("invalid_request", "source ID is ambiguous")

    mapping = {meta.id: destination + meta.id[len(source):] for meta in selected}
    if len(set(mapping.values())) != len(mapping):
        raise MemError("invalid_request", "move would create duplicate IDs")
    source_paths = {meta.path for meta in selected}
    all_ids = {meta.id for meta in metas}
    writes: dict[str, bytes] = {}
    removals: set[str] = set(source_paths)
    referenced: list[str] = []
    bytes_affected = 0

    for meta in metas:
        raw = repo.note_bytes(meta.path)
        new_id = mapping.get(meta.id)
        new_path = (note_relpath(project, new_id) if meta.path.endswith(".md")
                    else text_relpath(project, new_id)) if new_id else meta.path
        if new_id and not meta.path.endswith(".md"):
            if Path(new_path).suffix.lower() != Path(meta.path).suffix.lower():
                raise MemError("invalid_request", "inline text must keep its extension")
        if new_id and new_id in all_ids and new_id not in mapping:
            raise MemError("invalid_request", f"destination {new_id} already exists")
        if new_id and new_path not in source_paths and (repo.root / new_path).exists():
            raise MemError("invalid_request", f"destination path {new_path} already exists")

        updated = raw
        if meta.path.endswith(".md") and pointer_record(raw.decode("utf-8")) is None:
            text = raw.decode("utf-8")
            updated = _rewrite_note(text, mapping, new_id or meta.id)
            if updated != raw or new_id:
                new_data, _ = fm.parse(updated.decode("utf-8"))
                fm.validate(new_data, new_id or meta.id)
            if updated != raw and not new_id:
                referenced.append(meta.id)
        if new_id or updated != raw:
            if new_path in writes and new_path != meta.path:
                raise MemError("invalid_request", f"destination path {new_path} collides")
            writes[new_path] = updated
            bytes_affected += len(raw) + len(updated)

    if len(writes) > MAX_MOVE_RECORDS or bytes_affected > MAX_MOVE_BYTES:
        raise MemError("invalid_request", "move exceeds the atomic size limit",
                       {"records": len(writes), "max_records": MAX_MOVE_RECORDS,
                        "bytes": bytes_affected, "max_bytes": MAX_MOVE_BYTES})
    return MovePlan(head or "", source, destination, folder, mapping, writes,
                    removals - set(writes), sorted(referenced), bytes_affected)


def recover_move(repo: MemoryRepo, state_dir: Path, store=None) -> bool:
    """Converge a killed move's index and worktree on the committed HEAD."""
    # BUG(medium): a corrupt move journal makes the daemon unstartable, with no typed
    # error and no recovery path.
    # Why valid: `create_app` calls `recover_move` before it serves anything, and
    # neither this `json.loads` nor the `MemError` raised for a bad
    # path below is caught -- `__main__` does not wrap `create_app` either. An operator
    # therefore cannot start the service until they delete the file by hand, and the
    # condition surfaces as a boot-time traceback rather than a diagnosable state.
    # Fix hint: treat an unreadable or schema-invalid journal as recoverable -- log it,
    # quarantine the file, converge the worktree on HEAD, and let the daemon start.
    journal = state_dir / "move-journal.json"
    if not journal.exists():
        return False
    data = json.loads(journal.read_text(encoding="utf-8"))
    paths = data["paths"]
    for rel in paths:
        target = repo.root / rel
        if not rel.startswith("projects/") or ".." in Path(rel).parts or not target.resolve().is_relative_to(repo.root.resolve()):
            raise MemError("internal_error", f"invalid move journal path: {rel}")
    repo.git("reset", "-q", "HEAD", "--", *paths, check=False)
    head = repo.head()
    for rel in paths:
        target = repo.root / rel
        if head == data["head"]:
            encoded = data["before"][rel]
            body = base64.b64decode(encoded) if encoded is not None else None
        else:
            encoded = data.get("after", {}).get(rel)
            body = base64.b64decode(encoded) if encoded is not None else None
        if body is not None:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
        elif target.is_file():
            target.unlink()
    if head != data["head"] and data.get("key") and store is not None:
        store.put(data["key"], {**data["response"], "head": head,
                                "message": data["message"], "changed": True,
                                "idempotent_replay": False})
    journal.unlink()
    return True


def commit_move(repo: MemoryRepo, state_dir: Path, plan: MovePlan,
                *, message: str, device_id: str, key: str | None = None,
                store=None) -> dict:
    if repo.head() != plan.head:
        raise MemError("stale_revision", "repository changed during move preflight",
                       {"expected": plan.head, "actual": repo.head()})
    if repo.git("status", "--porcelain", "--", *plan.paths, check=False).strip():
        raise MemError("invalid_request", "move touches uncommitted repository paths")
    state_dir.mkdir(parents=True, exist_ok=True)
    journal = state_dir / "move-journal.json"
    if journal.exists():
        recover_move(repo, state_dir, store)
    pending = journal.with_suffix(".tmp")
    before = {rel: (base64.b64encode((repo.root / rel).read_bytes()).decode("ascii")
                    if (repo.root / rel).is_file() else None) for rel in plan.paths}
    after = {rel: (base64.b64encode(plan.writes[rel]).decode("ascii")
                   if rel in plan.writes else None) for rel in plan.paths}
    with pending.open("w", encoding="utf-8") as stream:
        json.dump({"head": plan.head, "paths": plan.paths, "before": before,
                   "after": after, "key": key, "response": plan.response(),
                   "message": message}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(pending, journal)
    with repo.snapshot_lock:
        try:
            for rel in sorted(plan.removals):
                repo.remove_note(rel)
            for rel, body in sorted(plan.writes.items()):
                repo.write_note(rel, body)
            committed = repo.commit_paths(message, plan.paths, device_id=device_id)
            if not committed["changed"]:
                raise MemError("internal_error", "move produced no Git commit")
            result = {**plan.response(), "head": committed["head"], "message": message,
                      "changed": True, "idempotent_replay": False}
            if key and store is not None:
                store.put(key, result)
            journal.unlink()
        except Exception:
            recover_move(repo, state_dir, store)
            raise
    return result
