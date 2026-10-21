"""Derived metadata catalog — the read model for `ls`, `projects`, and ids.

Git remains the record. Everything here is rebuildable from a commit: the table
stores what a note's frontmatter and history say, never bytes that only exist
here. `ensure()` brings it to a head with one diff; a missing or rewritten
catalog is dropped and rebuilt.

This exists because the read path used to parse every note's body for every
listing: 41 notes meant 41 `git show` spawns and a full frontmatter parse. The
catalog turns that into one row scan.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from ..ids import split_public_path
from ..pointers import pointer_record
from . import frontmatter as fm
from .repo import MemoryRepo

SCHEMA = """
CREATE TABLE IF NOT EXISTS catalog(
  project TEXT NOT NULL,
  note_id TEXT NOT NULL,
  path TEXT NOT NULL,
  kind TEXT NOT NULL,
  media_type TEXT NOT NULL,
  summary TEXT,
  status TEXT,
  note_type TEXT,
  tags TEXT,
  created TEXT,
  updated TEXT,
  author TEXT,
  message TEXT,
  size INTEGER,
  sha256 TEXT,
  links TEXT,
  warnings TEXT,
  PRIMARY KEY(project, note_id)
) STRICT;
CREATE INDEX IF NOT EXISTS catalog_path ON catalog(path);
CREATE INDEX IF NOT EXISTS catalog_updated ON catalog(updated DESC, note_id);
CREATE TABLE IF NOT EXISTS catalog_meta(key TEXT PRIMARY KEY, value TEXT) STRICT;
"""


class Catalog:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    # -- plumbing ----------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _head(self, conn: sqlite3.Connection) -> str | None:
        row = conn.execute("SELECT value FROM catalog_meta WHERE key='head'").fetchone()
        return row["value"] if row else None

    def head(self) -> str | None:
        with self._connect() as conn:
            return self._head(conn)

    def _stamp(self, conn: sqlite3.Connection, head: str | None) -> None:
        conn.execute(
            "INSERT INTO catalog_meta(key, value) VALUES('head', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (head or "",),
        )

    # -- building ----------------------------------------------------------

    def _row(self, path: str, raw: bytes, times: dict[str, tuple[str, str, str, str]]
             ) -> tuple[Any, ...] | None:
        split = split_public_path(path)
        if not split:
            return None
        project, note_id = split
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
        is_text_file = not path.endswith(".md")
        pointer = pointer_record(text) if not is_text_file else None
        warnings: list[str] = []
        if pointer:
            kind = pointer.kind
            media_type = "text/plain"
            summary = pointer.summary
            status = note_type = None
            tags: list[str] = []
            links: list[str] = []
        else:
            try:
                data, body = ({}, text) if is_text_file else fm.parse(text)
            except Exception:  # noqa: BLE001 - a broken note must not break a rebuild
                return None
            kind = "text" if is_text_file else "note"
            media_type = "text/plain" if is_text_file else "text/markdown"
            raw_summary = data.get("summary")
            summary = str(raw_summary) if raw_summary else (
                fm.title_of(body) or (body.splitlines()[0][:200] if is_text_file and body.splitlines() else "")
            )
            status = data.get("status")
            note_type = data.get("type")
            raw_tags = data.get("tags") or []
            if isinstance(raw_tags, str):
                raw_tags = [raw_tags]
            if isinstance(raw_tags, (list, tuple)):
                tags = [str(item) for item in raw_tags if isinstance(item, (str, int, float, bool))]
            else:
                tags = []
            links = set(fm.note_links(body)) if not is_text_file else set()
            for key in ("supersedes", "superseded_by"):
                value = data.get(key)
                if isinstance(value, str) and value:
                    links.add(value)
            links = sorted(links)
            warnings = [] if is_text_file else fm.content_warnings(data, body)
        made, changed, who, subject = times.get(path, ("", "", "", ""))
        return (
            project, note_id, path, kind, media_type, summary,
            str(status) if status is not None else None,
            str(note_type) if note_type is not None else None,
            json.dumps(list(tags)),
            made or None, changed or None, who or None, subject or None,
            len(raw), hashlib.sha256(raw).hexdigest(), json.dumps(links), json.dumps(warnings),
        )

    def _write_rows(self, conn: sqlite3.Connection, rows: Iterable[tuple[Any, ...]]) -> int:
        count = 0
        for row in rows:
            if row is None:
                continue
            conn.execute(
                "INSERT INTO catalog(project, note_id, path, kind, media_type, summary,"
                " status, note_type, tags, created, updated, author, message, size,"
                " sha256, links, warnings) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(project, note_id) DO UPDATE SET path=excluded.path,"
                " kind=excluded.kind, media_type=excluded.media_type, summary=excluded.summary,"
                " status=excluded.status, note_type=excluded.note_type, tags=excluded.tags,"
                " updated=excluded.updated, author=excluded.author, message=excluded.message,"
                " size=excluded.size, sha256=excluded.sha256, links=excluded.links,"
                " warnings=excluded.warnings",
                row,
            )
            count += 1
        return count

    def rebuild(self, repo: MemoryRepo, head: str | None) -> int:
        paths = repo.list_note_paths()
        bodies = repo.read_notes(paths, head)
        times = repo.commit_index()
        rows = [self._row(path, bodies[path], times) for path in paths if bodies.get(path) is not None]
        with self._connect() as conn:
            conn.execute("DELETE FROM catalog")
            count = self._write_rows(conn, rows)
            self._stamp(conn, head)
        return count

    def ensure(self, repo: MemoryRepo, head: str | None) -> bool:
        """Bring the catalog to *head*; returns True when anything changed."""
        if not head:
            return False
        current = self.head()
        if current == head:
            return False
        if current and repo.is_ancestor(current, head):
            changed = repo.changed_paths(current, head)
            if changed:
                bodies = repo.read_notes(changed, head)
                times = repo.commit_index()
                rows: list[tuple[Any, ...]] = []
                removed: list[str] = []
                for path in changed:
                    body = bodies.get(path)
                    # A deleted path, a note that stopped being UTF-8, or one
                    # whose frontmatter broke all mean "no row": the old row must
                    # not survive a head stamp.
                    row = None if body is None else self._row(path, body, times)
                    if row is None:
                        removed.append(path)
                    else:
                        rows.append(row)
                with self._connect() as conn:
                    conn.executemany("DELETE FROM catalog WHERE path=?", [(p,) for p in removed])
                    self._write_rows(conn, rows)
                    self._stamp(conn, head)
            else:
                with self._connect() as conn:
                    self._stamp(conn, head)
            return True
        self.rebuild(repo, head)
        return True

    # -- reading -----------------------------------------------------------

    def rows(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM catalog")]

    def ids(self) -> set[tuple[str, str]]:
        with self._connect() as conn:
            return {
                (row["project"], row["note_id"])
                for row in conn.execute("SELECT project, note_id FROM catalog")
            }

    def note(self, project: str, note_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM catalog WHERE project=? AND note_id=?", (project, note_id)
            ).fetchone()
            return dict(row) if row else None
