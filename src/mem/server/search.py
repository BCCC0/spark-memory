"""Server-side text search for MVP-0.

``MEM-PLAN.md`` §2 is explicit that the **index is not needed for MVP** — search
is a server-side text search that returns the same *coordinate* shape the
semantic version will return, so the interface is stable across both.

``MEM-RESOLUTIONS.md`` S3 chooses SQLite **FTS5** with bm25 ranking. FTS5 is not
guaranteed to be compiled into every Python build, so there is a plain-scan
fallback: correctness never depends on the extension being present.

The index lives under ``.mem/`` and is rebuildable by contract (``MEM.md`` I1):
delete it and nothing is lost.

**Matching rules** (``MEM-RESOLUTIONS.md`` S32, taken from Basic Memory's source): every
term is required and each is a **prefix** match (``"tok"*``), so `token` finds
`tokenization`. Quotes carry no meaning — the tokeniser strips them — and there is no
stemming and no stopword removal. If a question-shaped query matches **nothing**, it is
retried **once** more loosely: an OR of the same terms with stopwords pruned, under the
guards in `_relaxed_query`. Hits from that retry are marked ``relaxed``, so a loosened
result is never something the caller cannot see.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
import unicodedata
from pathlib import Path

from . import frontmatter as fm
from ..ids import split_public_path
from ..pointers import pointer_record
from .repo import MemoryRepo

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS notes USING fts5(
  note_id UNINDEXED, project UNINDEXED, path UNINDEXED,
  status UNINDEXED, note_type UNINDEXED, tags UNINDEXED,
  title, summary, body
);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT) STRICT;
CREATE TABLE IF NOT EXISTS docs(
  note_id TEXT, project TEXT, path TEXT, status TEXT,
  note_type TEXT, tags TEXT, title TEXT, summary TEXT, body TEXT
);
"""

_TOKEN = re.compile(r"[\w\-]+", re.UNICODE)

#: The one-shot relaxation guard, taken from Basic Memory's `search_query.py:252-302`
#: (S32). A query is only eligible to be retried more loosely when it is
#: question-shaped: short, quoted, or numeric queries already carry intent, and
#: loosening them would return noise rather than rescue a miss.
RELAX_MIN_TOKENS = 3
RELAXATION_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "in",
        "is", "it", "its", "of", "on", "that", "the", "to", "was", "were", "what",
        "when", "where", "which", "who", "why", "will", "with",
    }
)


def _fts_query(query: str) -> str | None:
    """Every term required, each a **prefix** match.

    ``"tok"*`` rather than an exactly-quoted token, so a query for `token` finds
    `tokenization` — the behaviour Basic Memory has (`tok*`, `sqlite_search_repository.py:349-354`)
    and mem lacked (S32). `_TOKEN` cannot contain a quote, so no escaping is needed.
    """
    tokens = _TOKEN.findall(query or "")
    if not tokens:
        return None
    return " AND ".join(f'"{token}"*' for token in tokens)


def _relaxed_query(query: str) -> str | None:
    """A looser OR of the same terms, or ``None`` when the query is not eligible.

    The retry Basic Memory performs once on an empty result. The guards are what
    keep it from being a silent downgrade — and the caller is told when it fires
    (the hits are marked `relaxed`), because a loosened result a caller cannot
    detect is exactly the kind of behaviour this project refuses to ship.
    """
    raw = (query or "").strip()
    if not raw or '"' in raw:
        return None  # a quoted query is a deliberate phrase; do not loosen it
    tokens = [token.lower() for token in _TOKEN.findall(raw)]
    if len(tokens) < RELAX_MIN_TOKENS:
        return None  # two terms is already narrow enough to trust
    if any(
        all(unicodedata.category(char).startswith("N") for char in token)
        for token in tokens
    ):
        return None  # an identifier or a number, not a question
    kept = [token for token in tokens if token not in RELAXATION_STOPWORDS]
    if not kept:
        return None
    return " OR ".join(f'"{token}"*' for token in kept)


def _tags_json(raw: object) -> str:
    if isinstance(raw, list):
        return json.dumps(raw)
    return json.dumps([])


class SearchIndex:
    def __init__(self, db_path: Path) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fts = True
        self._connect_and_prepare()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        # WAL lets readers keep the last committed snapshot while a rebuild is
        # writing; without it a full replacement excludes every concurrent search.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    @staticmethod
    def _tables(conn: sqlite3.Connection) -> set[str]:
        return {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE name IN ('notes', 'docs')"
            )
        }

    @classmethod
    def _clear_tables(cls, conn: sqlite3.Connection) -> None:
        for table in ("notes", "docs"):
            if table in cls._tables(conn):
                conn.execute(f"DELETE FROM {table}")

    @classmethod
    def _delete_paths(cls, conn: sqlite3.Connection, paths: list[str]) -> None:
        for table in ("notes", "docs"):
            if table in cls._tables(conn):
                conn.executemany(
                    f"DELETE FROM {table} WHERE path=?",
                    [(path,) for path in paths],
                )

    def _connect_and_prepare(self) -> None:
        with self._connect() as conn:
            try:
                conn.executescript(FTS_SCHEMA)
                self._fts = True
            except sqlite3.OperationalError:
                # No FTS5 in this build -- fall back to the plain docs table.
                self._fts = False
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT) STRICT;
                    CREATE TABLE IF NOT EXISTS docs(
                      note_id TEXT, project TEXT, path TEXT, status TEXT,
                      note_type TEXT, tags TEXT, title TEXT, summary TEXT, body TEXT
                    );
                    """
                )
            mode = "fts" if self._fts else "docs"
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'mode'"
            ).fetchone()
            previous = row["value"] if row else None
            if previous is not None and previous != mode:
                # A DB opened on a build with a different FTS5 availability must
                # not keep rows in the other table while head already matches.
                self._clear_tables(conn)
                conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES ('head', '')"
                )
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('mode', ?)", (mode,)
            )

    def _prepare_if_needed(self) -> None:
        """Recreate the schema if the index file went away underneath us.

        The whole index is derived and deletable by contract (``MEM.md`` I1), and
        "delete it and rebuild" means the daemon must survive the file being
        removed while it is running -- not just at startup. On Linux the unlink
        really happens, so this is the normal path, not a defensive one.
        """
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT name FROM sqlite_master WHERE name = 'meta'"
                ).fetchone()
            if row is not None:
                return
        except sqlite3.Error:
            pass
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connect_and_prepare()

    # -- building ----------------------------------------------------------

    def indexed_head(self) -> str | None:
        self._prepare_if_needed()
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = 'head'").fetchone()
        return row["value"] if row else None

    def ensure(self, repo: MemoryRepo, head: str | None) -> bool:
        """Advance the index to ``head``; return True when it changed.

        A linear commit can be indexed by touching only the paths that commit
        changed. A rewritten history, a schema repair, or a first build still
        needs the full rebuild.
        """
        if not head:
            return False
        indexed = self.indexed_head()
        if indexed == head:
            return False
        if indexed and repo.is_ancestor(head, indexed):
            # A worker pinned to an older commit must not rebuild the shared
            # text index backwards while a write has already advanced it.
            return False
        if indexed and repo.is_ancestor(indexed, head):
            self.update(repo, head, repo.changed_paths(indexed, head))
        else:
            self.rebuild(repo, head)
        return True

    def indexed_at(self) -> float | None:
        """Wall-clock time of the last rebuild, for the `index.age` field."""
        self._prepare_if_needed()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'indexed_at'"
            ).fetchone()
        try:
            return float(row["value"]) if row else None
        except (TypeError, ValueError):
            return None

    def _row_for(self, repo: MemoryRepo, path: str) -> tuple[object, ...] | None:
        """The index row for one path, or None when it is not indexable."""
        try:
            split = split_public_path(path)
            if split is None:
                return None
            project, note_id = split
            text = repo.read_note(path)
            data, body = fm.parse(text) if path.endswith(".md") else ({}, text)
            pointer = pointer_record(text) if path.endswith(".md") else None
        except Exception:  # noqa: BLE001 - a broken note must not break search
            return None
        if pointer and pointer.kind == "locked":
            summary = pointer.summary
            body = ""
        else:
            summary = (pointer.canonical if pointer else str(data.get("summary") or
                       (body.splitlines()[0][:200] if body.splitlines() else "")))
        return (
            note_id,
            project,
            path,
            str(data.get("status", "")),
            str(data.get("type", "")),
            _tags_json(data.get("tags")),
            fm.title_of(body) or "",
            summary,
            body,
        )

    @staticmethod
    def _stamp(conn: sqlite3.Connection, head: str | None) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('head', ?)", (head or "",)
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('indexed_at', ?)",
            (str(time.time()),),
        )

    def rebuild(self, repo: MemoryRepo, head: str | None) -> int:
        """Replace the whole index. Used for first build, repair, and history rewrites."""
        self._prepare_if_needed()
        rows = [
            row for path in repo.list_note_paths()
            if (row := self._row_for(repo, path)) is not None
        ]
        with self._connect() as conn:
            target = "notes" if self._fts else "docs"
            self._clear_tables(conn)
            conn.executemany(
                f"INSERT INTO {target}(note_id, project, path, status, note_type,"
                " tags, title, summary, body) VALUES (?,?,?,?,?,?,?,?,?)",
                rows,
            )
            self._stamp(conn, head)
        return len(rows)

    def update(self, repo: MemoryRepo, head: str | None, changed_paths: list[str]) -> int:
        """Replace only the rows for paths touched by one commit."""
        self._prepare_if_needed()
        paths = sorted({path for path in changed_paths if path.startswith("projects/")})
        rows = []
        for path in paths:
            if repo.is_inline_note(path):
                row = self._row_for(repo, path)
                if row is not None:
                    rows.append(row)
        with self._connect() as conn:
            target = "notes" if self._fts else "docs"
            self._delete_paths(conn, paths)
            conn.executemany(
                f"INSERT INTO {target}(note_id, project, path, status, note_type,"
                " tags, title, summary, body) VALUES (?,?,?,?,?,?,?,?,?)",
                rows,
            )
            self._stamp(conn, head)
        return len(rows)

    # -- querying ----------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        project: str | None = None,
        status: str | None = None,
        note_type: str | None = None,
        tag: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        self._prepare_if_needed()
        table = "notes" if self._fts else "docs"
        tokens = [t.lower() for t in _TOKEN.findall(query or "")]
        if not tokens:
            return []

        where: list[str] = []
        params: list[object] = []

        if self._fts:
            match = _fts_query(query)
            if match is None:
                return []
            where.append("notes MATCH ?")
            params.append(match)
            columns = "note_id, project, path, title, summary, body, bm25(notes) AS score_raw"
            # bm25() is negative and *smaller is better*, so ascending is best-first.
            order = "ORDER BY bm25(notes)"
        else:
            clause = " OR ".join(
                "(title LIKE ? OR summary LIKE ? OR body LIKE ?)" for _ in tokens
            )
            where.append(f"({clause})")
            for token in tokens:
                like = f"%{token}%"
                params.extend([like, like, like])
            columns = "note_id, project, path, title, summary, body, NULL AS score_raw"
            order = "ORDER BY note_id"

        if project:
            where.append("project = ?")
            params.append(project)
        if status:
            where.append("status = ?")
            params.append(status)
        if note_type:
            where.append("note_type = ?")
            params.append(note_type)
        if tag:
            where.append("tags LIKE ?")
            params.append(f'%"{tag}"%')

        sql = (
            f"SELECT {columns} FROM {table} "
            f"WHERE {' AND '.join(where)} {order} LIMIT ?"
        )
        params.append(max(1, min(int(limit), 200)))

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            relaxed = False
            if self._fts and not rows:
                # One looser retry, the way Basic Memory rescues a question-shaped
                # query that strict AND turned into nothing (S32). Only the MATCH
                # text changes, so a relaxed hit cannot escape the caller's filters.
                fallback = _relaxed_query(query)
                if fallback:
                    params[0] = fallback  # params[0] is the MATCH text, by construction
                    rows = conn.execute(sql, params).fetchall()
                    relaxed = bool(rows)

        hits: list[dict] = []
        for row in rows:
            body = row["body"] or ""
            if self._fts:
                # Flip bm25 so that, like every other score here, larger is better.
                score = -float(row["score_raw"] or 0.0)
            else:
                haystack = f"{row['title']}\n{row['summary']}\n{body}".lower()
                score = float(sum(haystack.count(token) for token in tokens))
            hit = {
                "id": row["note_id"],
                "project": row["project"],
                "summary": row["summary"],
                "score": score,
                "line_range": self._line_range(body, tokens),
                "revision": None,  # filled in by the caller, which knows HEAD
            }
            if relaxed:
                # Never silent: the caller can see that these are the looser match.
                hit["relaxed"] = True
            hits.append(hit)
        return hits

    @staticmethod
    def _line_range(body: str, tokens: list[str]) -> list[int] | None:
        """Approximate the matched snippet's location.

        This is a **scan for the most specific query term**, not a read of the
        FTS5 snippet: the longest token is the least likely to appear by
        coincidence, so it anchors the range. Documented as an approximation
        rather than sold as a snippet — an earlier comment claimed a fidelity
        that the code never had.
        """
        if not tokens:
            return None
        ordered = sorted(tokens, key=len, reverse=True)
        lines = body.splitlines()
        for token in ordered:
            for number, line in enumerate(lines, start=1):
                if token in line.lower():
                    return [number, number]
        return None
