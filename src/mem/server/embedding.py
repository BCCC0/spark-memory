"""Semantic index: markdown-aware chunking, `sqlite-vec`, incremental sync.

**The design is taken from what Basic Memory actually does**, read from its source
in a reference deployment rather than from its documentation, because it is proven
on exactly this corpus:

* **Chunk at markdown boundaries** — headings *and* bullets — capped at 900 chars. Its
  comment says why: *"keeping bullets separate gives individual facts their own
  retrieval vector"*. The 120-char overlap applies **only inside a single oversized
  paragraph**, which is what Basic Memory actually does (`semantic_chunking.py:246-261`);
  ordinary adjacent chunks have none. An earlier version of this docstring claimed the
  overlap was global, which overstated it.
* **Key each chunk by ORDINAL within its note** (`{note}:{index}`) and identify its
  *content* by a `source_hash`. That pair is what makes the sync incremental.
* **A per-note fingerprint** of all its chunk inputs gives a fast whole-note skip.
* **The embedded text is title + locator + the chunk**, not the bare body — again what
  Basic Memory does (`compose_row_source_text`). `source_hash` is computed over *that*
  composed text, so a title change re-embeds the note instead of silently keeping a
  vector that no longer describes it.

**Incremental is a two-level thing, and the second level is the one that matters.**
An unchanged note costs *nothing* (fingerprint match ⇒ zero embedding jobs). A
changed note re-embeds **only the chunks whose `source_hash` changed** — an
unchanged chunk keeps its vector and merely has its metadata row rewritten. Chunks
that no longer exist are deleted; rows missing a vector are re-queued.

**Deliberate departures from Basic Memory:**

* **The sync is triggered by the WRITE path, never by a read** (``S32``; an earlier
  version of this docstring described the opposite, "lazy sync on search", which is the
  design that was reversed). `add`/`rm` schedule a background pass; a read serves
  whatever is indexed and reports `index_lag` when it is behind. The model load and the
  embedding run in a worker thread, so neither can block the event loop. **The text
  index is the exception** — it needs no model, is cheap, and still rebuilds on the read
  path (off the loop), so write-then-search keeps working.
* **A model change forces a full reindex**, because the model name is stored per
  chunk and a mismatch makes every chunk stale.

Nothing here runs unless the ``embed`` extra is installed; ``mem`` is a useful
text-search service without it.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ..errors import MemError

#: Chunk geometry, matching the proven implementation.
CHUNK_CHARS = 900
OVERLAP_CHARS = 120

#: Bumped when the `chunk` table's shape changes. A mismatch wipes the derived index
#: rather than mixing rows written under two different schemas (S32).
CHUNK_SCHEMA = "2"

#: The model, and its alias as stored in the index.
MODEL_NAME = "BAAI/bge-small-en-v1.5"
MODEL_ALIAS = "bge-small-en-v1.5"
DIM = 384

_HEADING = re.compile(r"^\s*#{1,6}\s+")
_BULLET = re.compile(r"^\s*[\-\*]\s+")


def available() -> bool:
    """Is the ``embed`` extra installed?

    Answered by ``find_spec`` rather than by importing. Importing ``fastembed`` costs
    ~375 ms (it pulls in onnxruntime), and ``create_app`` asks this on every start just
    to decide whether to build the vector index. The real import happens in
    :class:`Embedder` on first use, so nothing is skipped — only deferred.

    This asks whether the extra is *installable*, not whether it *works*. A broken
    install (a missing onnxruntime DLL, say) therefore reports ``True`` here and fails
    on first embed with the typed error from ``VectorIndex._embedder_or_fail``'s circuit
    breaker, rather than refusing ``--mode hybrid`` up front. Deliberate: startup stays
    sub-millisecond, and the failure still arrives as one ``MemError`` carrying a hint.
    """
    for name in ("fastembed", "sqlite_vec"):
        try:
            if importlib.util.find_spec(name) is None:
                return False
        except (ImportError, ValueError):
            # A parent package that is itself missing, or a module with no __spec__.
            return False
    return True


@dataclass(frozen=True)
class Chunk:
    """One retrieval unit: a slice of a note with its content identity."""

    ordinal: int
    text: str
    source_hash: str
    line_start: int
    line_end: int

    def key(self, note_id: str) -> str:
        return f"{note_id}:{self.ordinal}"


@dataclass(frozen=True)
class NoteSource:
    """What the index needs from one note (``S32``).

    ``body`` is chunked and embedded; ``title`` heads every chunk's embedded text;
    ``status``/``note_type``/``tags`` are **denormalised onto the chunk rows** so the
    vector half can honour the same filters the text half already applies, instead of
    Basic Memory's second filtered scan with its 50 000-row cap.
    """

    title: str
    body: str
    status: str | None = None
    note_type: str | None = None
    tags: list[str] | None = None


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalize(vector: list[float]) -> list[float]:
    """L2-normalise, so an L2 distance becomes a cosine distance (``S32``)."""
    norm = sum(value * value for value in vector) ** 0.5
    if norm == 0.0:
        return vector  # no direction to preserve; better than emitting NaNs
    return [value / norm for value in vector]


def compose_source(context: str, text: str) -> str:
    """The text that is actually embedded (``S32``).

    Basic Memory embeds title + permalink + category + content
    (``compose_row_source_text``); mem has no category, so the note title and its
    ``project/id`` locator stand in for the rest.

    Kept in one place on purpose: ``source_hash`` and the embedding input **must** be
    the same string. If they ever drift, a change to the composed text keeps a vector
    that no longer describes it, and nothing detects that.
    """
    return f"{context}\n{text}" if context else text


def _window(text: str) -> list[str]:
    """Char-window one still-oversized block, with the overlap."""
    pieces: list[str] = []
    step = max(1, CHUNK_CHARS - OVERLAP_CHARS)
    for start in range(0, len(text), step):
        piece = text[start : start + CHUNK_CHARS].strip()
        if piece:
            pieces.append(piece)
        if start + CHUNK_CHARS >= len(text):
            break
    return pieces


def _split_oversized(section: str) -> list[str]:
    """Split an oversized section on structure first, characters only as a fallback.

    Basic Memory splits at paragraph boundaries *and* at individual bullet items, then
    char-windows only a block that is still too long (``semantic_chunking.py:194-261``).
    Char-windowing the whole section instead — which mem did — discards those
    boundaries, and the bullet boundary is the one its own comment singles out as
    mattering.
    """
    blocks: list[str] = []
    for paragraph in re.split(r"\n\s*\n", section):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        lines = paragraph.splitlines()
        if len(lines) == 1:
            blocks.append(paragraph)
            continue
        group: list[str] = []
        for line in lines:
            if _BULLET.match(line):
                # Each bullet keeps its own vector.
                if group:
                    blocks.append("\n".join(group).strip())
                    group = []
                blocks.append(line.strip())
            else:
                group.append(line)
        if group:
            blocks.append("\n".join(group).strip())

    pieces: list[str] = []
    buffer = ""
    for block in blocks:
        if not block:
            continue
        if len(block) > CHUNK_CHARS:
            if buffer:
                pieces.append(buffer)
                buffer = ""
            pieces.extend(_window(block))
            continue
        candidate = block if not buffer else f"{buffer}\n\n{block}"
        if len(candidate) <= CHUNK_CHARS:
            buffer = candidate
        else:
            pieces.append(buffer)
            buffer = block
    if buffer:
        pieces.append(buffer)
    return [piece for piece in pieces if piece]


def chunk_markdown(text: str, *, context: str = "") -> list[Chunk]:
    """Split note body text at markdown boundaries.

    ``context`` — the title and the ``project/id`` locator — is **not** stored as part
    of the chunk text, but it *is* folded into ``source_hash``, because it is part of
    what gets embedded. See `compose_source`.

    Line ranges are **approximate for oversized sections** (a character split
    cannot know which line it landed on without re-scanning); they are exact for
    boundary-delimited chunks, which is the common case.
    """
    normalized = (text or "").strip()
    if not normalized:
        return []
    lines = normalized.splitlines()

    # Section boundaries: before every heading and every bullet.
    sections: list[tuple[int, int]] = []
    start = 0
    for index in range(1, len(lines)):
        if _HEADING.match(lines[index]) or _BULLET.match(lines[index]):
            if index > start:
                sections.append((start, index))
            start = index
    sections.append((start, len(lines)))

    chunks: list[Chunk] = []
    buffer_text = ""
    buffer_start: int | None = None
    buffer_end: int | None = None

    def flush() -> None:
        nonlocal buffer_text, buffer_start, buffer_end
        if buffer_text.strip() and buffer_start is not None:
            body = buffer_text.strip()
            chunks.append(
                Chunk(
                    ordinal=len(chunks),
                    text=body,
                    source_hash=_hash(compose_source(context, body)),
                    line_start=buffer_start + 1,
                    line_end=(buffer_end or buffer_start) + 1,
                )
            )
        buffer_text, buffer_start, buffer_end = "", None, None

    for (section_start, section_end) in sections:
        section = "\n".join(lines[section_start:section_end]).strip()
        if not section:
            continue
        is_bullet = bool(_BULLET.match(section))

        if len(section) > CHUNK_CHARS:
            flush()
            for piece in _split_oversized(section):
                chunks.append(
                    Chunk(
                        ordinal=len(chunks),
                        text=piece,
                        source_hash=_hash(compose_source(context, piece)),
                        line_start=section_start + 1,
                        line_end=section_end,
                    )
                )
            continue

        if is_bullet:
            flush()
            chunks.append(
                Chunk(
                    ordinal=len(chunks),
                    text=section,
                    source_hash=_hash(compose_source(context, section)),
                    line_start=section_start + 1,
                    line_end=section_end,
                )
            )
            continue

        candidate = section if not buffer_text else f"{buffer_text}\n\n{section}"
        if len(candidate) <= CHUNK_CHARS:
            buffer_text = candidate
            buffer_start = section_start if buffer_start is None else buffer_start
            buffer_end = section_end
        else:
            flush()
            buffer_text, buffer_start, buffer_end = section, section_start, section_end
    flush()
    return chunks


def note_fingerprint(chunks: Iterable[Chunk]) -> str:
    """Hash every chunk input for one note — the fast whole-note skip key."""
    digest = hashlib.sha256()
    for chunk in chunks:
        digest.update(chunk.source_hash.encode())
        digest.update(b"\0")
    return digest.hexdigest()


class Embedder:
    """`fastembed` wrapper. Imported lazily so the extra stays optional.

    `MEM_EMBED_CACHE` points at an existing model cache. Worth setting on the service host:
    the weights are already on disk from Basic Memory, and this workstation cannot
    reach HuggingFace at all (ConnectTimeout, verified) — so a first run without a
    cache simply fails rather than downloading.
    """

    def __init__(self, model_name: str = MODEL_NAME, cache_dir: str | None = None) -> None:
        import os  # noqa: PLC0415

        from fastembed import TextEmbedding  # noqa: PLC0415

        resolved_cache = cache_dir or os.environ.get("MEM_EMBED_CACHE")
        kwargs: dict[str, Any] = {
            "model_name": model_name,
            # Basic Memory passes this, citing a tens-of-GB arena leak on repeated
            # model loads (S32). An older fastembed that rejects the kwarg must not
            # take the daemon down with it, so fall back rather than fail.
            "enable_cpu_mem_arena": False,
        }
        if resolved_cache:
            kwargs["cache_dir"] = resolved_cache
        try:
            self.model = TextEmbedding(**kwargs)
        except TypeError:
            kwargs.pop("enable_cpu_mem_arena", None)
            self.model = TextEmbedding(**kwargs)
        self.model_name = MODEL_ALIAS
        # One ONNX session may now be reached from a worker thread that a
        # `to_thread` call handed us, and from the query path. Serialise it.
        self._embed_lock = threading.Lock()

    def embed(self, texts: list[str]) -> list[list[float]]:
        """L2-normalised vectors.

        Normalised on **both** sides — stored and query — so sqlite-vec's L2 distance
        converts to cosine as `1 - d*d/2` (S32). Before this, mem stored raw vectors
        and scored `1 - distance` against an L2 metric: the same ordering, but not a
        cosine similarity, so no threshold built on it would have meant anything.
        """
        with self._embed_lock:
            raw = [list(map(float, vector)) for vector in self.model.embed(texts)]
        return [_normalize(vector) for vector in raw]


class VectorIndex:
    """A `sqlite-vec` index beside the text index, synced incrementally."""

    def __init__(
        self,
        path: Path,
        *,
        embedder: Embedder | None = None,
        model: str = MODEL_ALIAS,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._embedder = embedder
        self._embedder_error: MemError | None = None
        # Guards model CONSTRUCTION (S33); `Embedder` guards its own `embed()`.
        self._load_lock = threading.Lock()
        self.model = model
        self._loaded = False
        self._prepare()

    # -- plumbing ----------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        import sqlite_vec  # noqa: PLC0415

        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return conn

    def _prepare(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS chunk(
                  key         TEXT PRIMARY KEY,
                  project     TEXT NOT NULL,
                  note_id     TEXT NOT NULL,
                  revision    TEXT,
                  source_hash TEXT NOT NULL,
                  model       TEXT NOT NULL,
                  line_start  INTEGER,
                  line_end    INTEGER,
                  text        TEXT NOT NULL,
                  status      TEXT,
                  note_type   TEXT,
                  tags        TEXT
                ) STRICT;
                CREATE INDEX IF NOT EXISTS chunk_note ON chunk(project, note_id);
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT) STRICT;
                -- The schema marker lives in its OWN table on purpose. `rebuild()`
                -- clears `meta` (fingerprints and head), and while the marker lived
                -- there too, a rebuild looked like a schema mismatch to the NEXT
                -- process — which then wiped the freshly built index and re-embedded
                -- the whole corpus a second time (S33).
                CREATE TABLE IF NOT EXISTS index_schema(
                  key TEXT PRIMARY KEY, value TEXT
                ) STRICT;
                """
            )
            # Add columns an older index lacks. **ADD COLUMN, never DROP/recreate**:
            # this table is shared by every project, and Basic Memory's own history
            # records a reindex `DROP TABLE` destroying a sibling project's rows
            # (`services/search_service.py:152-167`). mem's index is single-owner and
            # rebuildable, but the lesson costs nothing to keep (S32).
            present = {row["name"] for row in conn.execute("PRAGMA table_info(chunk)")}
            for column in ("status", "note_type", "tags"):
                if column not in present:
                    conn.execute(f"ALTER TABLE chunk ADD COLUMN {column} TEXT")

            conn.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunk USING vec0("
                f"key TEXT PRIMARY KEY, embedding FLOAT[{DIM}])"
            )

            row = conn.execute(
                "SELECT value FROM index_schema WHERE key = 'chunk_schema'"
            ).fetchone()
            if (row["value"] if row else None) != CHUNK_SCHEMA:
                # The embedded text changed (S32), so every stored vector is stale.
                # Deleted by predicate and re-derived on the next sync. Read from, and
                # written to, `index_schema` — never `meta`, which `rebuild()` clears.
                conn.execute("DELETE FROM chunk")
                conn.execute("DELETE FROM vec_chunk")
                conn.execute(
                    "INSERT OR REPLACE INTO index_schema(key, value)"
                    " VALUES ('chunk_schema', ?)",
                    (CHUNK_SCHEMA,),
                )

    def _embedder_or_fail(self) -> Embedder:
        if self._embedder is not None:
            return self._embedder
        if self._embedder_error is not None:
            # Circuit breaker. A model that cannot be loaded (no cache, no route to
            # HuggingFace) would otherwise cost the full download timeout on EVERY
            # attempt, and the reindexer attempts on every write. One slow failure
            # per process, then a cheap typed error each time (S32).
            raise self._embedder_error
        # Serialise CONSTRUCTION, not just use. The reindex worker thread and a hybrid
        # request can both arrive here cold, and two `TextEmbedding` instances mean two
        # ONNX sessions — exactly the arena leak `enable_cpu_mem_arena=False` is there
        # to prevent. `embed()` has its own lock; this one covers the load (S33).
        with self._load_lock:
            if self._embedder is not None:  # another thread won the race
                return self._embedder
            if self._embedder_error is not None:
                raise self._embedder_error
            if not available():
                raise MemError(
                    "internal_error",
                    "the semantic index is not installed (the `embed` extra)",
                )
            try:
                self._embedder = Embedder()
            except Exception as exc:  # noqa: BLE001 - reported, never fatal
                self._embedder_error = MemError(
                    "internal_error",
                    f"the embedding model could not be loaded: {exc}",
                    {"model": MODEL_ALIAS, "hint": "set MEM_EMBED_CACHE to a model cache"},
                )
                raise self._embedder_error from exc
        return self._embedder

    # -- state -------------------------------------------------------------

    def indexed_head(self) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = 'head'").fetchone()
        return row["value"] if row else None

    def status(self) -> dict[str, Any]:
        with self._connect() as conn:
            chunks = conn.execute("SELECT COUNT(*) AS n FROM chunk").fetchone()["n"]
            notes = conn.execute("SELECT COUNT(DISTINCT project || '/' || note_id) AS n FROM chunk").fetchone()["n"]
        return {
            "kind": "hybrid",
            "present": chunks > 0,
            "chunks": chunks,
            "notes": notes,
            "model": self.model,
            "indexed_head": self.indexed_head(),
            "available": True,
        }

    # -- sync --------------------------------------------------------------

    def sync(self, notes: Iterable[Any], read_source, head: str | None) -> dict[str, int]:
        """Bring the index up to date with `notes`, embedding only what changed.

        `notes` are objects with `.project`, `.id`, `.path` (the same shape
        ``MemoryRepo.notes()`` yields); `read_source(path)` returns a `NoteSource`.
        """
        embedder = self._embedder_or_fail()
        stats = {"notes": 0, "chunks": 0, "embedded": 0, "deleted": 0, "skipped": 0}

        with self._connect() as conn:
            existing = {
                row["key"]: row
                for row in conn.execute(
                    "SELECT key, project, note_id, source_hash, model,"
                    " line_start, line_end, text, status, note_type, tags FROM chunk"
                )
            }

        seen: set[str] = set()
        # chunk, project, note_id, the composed text to embed, status, type, tags
        pending: list[
            tuple[Chunk, str, str, str, str | None, str | None, str | None]
        ] = []

        for meta in notes:
            stats["notes"] += 1
            source = read_source(meta.path)
            body = source.body
            # The title and the locator head every chunk's embedded text (S32), and
            # the SAME string feeds source_hash — so a retitled note re-embeds rather
            # than keeping a vector that no longer describes it.
            context = f"{source.title}\n{meta.project}/{meta.id}"
            tags = json.dumps(source.tags or [])
            chunks = chunk_markdown(body, context=context)
            stats["chunks"] += len(chunks)
            fingerprint = note_fingerprint(chunks)
            stored_fp = None
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT value FROM meta WHERE key = ?",
                    (f"fp:{meta.project}/{meta.id}",),
                ).fetchone()
            stored_fp = row["value"] if row else None

            wanted = {chunk.key(meta.id): chunk for chunk in chunks}
            seen.update(wanted)

            # Fast path: neither the content nor the filterable metadata changed.
            # The metadata check is not incidental — without it a status change alone
            # would be skipped, and the vector half would keep filtering on a stale
            # status until the body happened to change (S32).
            metadata_same = all(
                (prior := existing.get(key)) is not None
                and prior["status"] == source.status
                and prior["note_type"] == source.note_type
                and prior["tags"] == tags
                for key in wanted
            )
            if stored_fp == fingerprint and metadata_same and all(
                key in existing and existing[key]["source_hash"] == chunk.source_hash
                and existing[key]["model"] == self.model
                for key, chunk in wanted.items()
            ):
                stats["skipped"] += 1
                continue

            with self._connect() as conn:
                for key, chunk in wanted.items():
                    prior = existing.get(key)
                    if (
                        prior is not None
                        and prior["source_hash"] == chunk.source_hash
                        and prior["model"] == self.model
                        and prior["note_id"] == meta.id
                    ):
                        # Unchanged content: keep the vector, refresh only metadata.
                        conn.execute(
                            "UPDATE chunk SET revision = ?, line_start = ?, line_end = ?,"
                            " status = ?, note_type = ?, tags = ? WHERE key = ?",
                            (
                                head,
                                chunk.line_start,
                                chunk.line_end,
                                source.status,
                                source.note_type,
                                tags,
                                key,
                            ),
                        )
                        continue
                    pending.append(
                        (
                            chunk,
                            meta.project,
                            meta.id,
                            compose_source(context, chunk.text),
                            source.status,
                            source.note_type,
                            tags,
                        )
                    )

                conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                    (f"fp:{meta.project}/{meta.id}", fingerprint),
                )

        # Anything indexed but no longer present is dropped.
        stale = [key for key in existing if key not in seen]
        if stale:
            with self._connect() as conn:
                for key in stale:
                    conn.execute("DELETE FROM chunk WHERE key = ?", (key,))
                    conn.execute("DELETE FROM vec_chunk WHERE key = ?", (key,))
            stats["deleted"] = len(stale)
            for key in stale:
                existing.pop(key, None)

        # Embed in batches. The embedded text is the COMPOSED one, which is also what
        # source_hash covers — see `compose_source`.
        for start in range(0, len(pending), 32):
            batch = pending[start : start + 32]
            vectors = embedder.embed([item[3] for item in batch])
            with self._connect() as conn:
                for item, vector in zip(batch, vectors):
                    chunk, project, note_id, _text, status, note_type, tags = item
                    key = chunk.key(note_id)
                    conn.execute(
                        "INSERT OR REPLACE INTO chunk(key, project, note_id, revision,"
                        " source_hash, model, line_start, line_end, text,"
                        " status, note_type, tags)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            key, project, note_id, head, chunk.source_hash, self.model,
                            chunk.line_start, chunk.line_end, chunk.text,
                            status, note_type, tags,
                        ),
                    )
                    conn.execute("DELETE FROM vec_chunk WHERE key = ?", (key,))
                    conn.execute(
                        "INSERT INTO vec_chunk(key, embedding) VALUES (?, ?)",
                        (key, _serialize(vector)),
                    )
            stats["embedded"] += len(batch)

        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('head', ?)", (head or "",)
            )
        return stats

    def rebuild(self, notes: Iterable[Any], read_source, head: str | None) -> dict[str, int]:
        with self._connect() as conn:
            conn.execute("DELETE FROM chunk")
            conn.execute("DELETE FROM vec_chunk")
            conn.execute("DELETE FROM meta")
        return self.sync(notes, read_source, head)

    # -- query -------------------------------------------------------------

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
        """Nearest chunks, honouring **the same filters as the text half** (S32).

        The filters live on the chunk rows — denormalised at sync time — so they can be
        predicates here instead of Basic Memory's approach: a second filtered scan of
        up to 50 000 rows intersected in Python, which silently drops hits beyond the
        cap. Same guarantee, no cap.
        """
        embedder = self._embedder_or_fail()
        vector = embedder.embed([query])[0]
        # `k` is sqlite-vec's k-nearest knob; ask for more than we return so the
        # filters still have something to choose from.
        params: list[Any] = [_serialize(vector), max(20, limit * 4)]
        sql = (
            "SELECT c.key, c.project, c.note_id, c.line_start, c.line_end,"
            "       c.source_hash, h.distance"
            " FROM (SELECT key, distance FROM vec_chunk"
            "       WHERE embedding MATCH ? AND k = ?) h"
            " JOIN chunk c ON c.key = h.key"
        )
        where: list[str] = []
        if project:
            where.append("c.project = ?")
            params.append(project)
        if status:
            where.append("c.status = ?")
            params.append(status)
        if note_type:
            where.append("c.note_type = ?")
            params.append(note_type)
        if tag:
            # Same JSON-array containment shape the text index uses.
            where.append("c.tags LIKE ?")
            params.append(f'%"{tag}"%')
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY h.distance LIMIT ?"
        params.append(max(1, min(limit, 200)))

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            {
                "id": row["note_id"],
                "project": row["project"],
                # Vectors are stored L2-normalised, so an L2 distance d converts to a
                # cosine similarity as 1 - d*d/2 (S32). Previously this was `1 - d`,
                # which is a different metric; the ordering was the same, the number
                # was not a similarity.
                "score": round(max(0.0, 1.0 - float(row["distance"]) ** 2 / 2.0), 6),
                "line_range": [row["line_start"], row["line_end"]],
                "chunk_key": row["key"],
                "revision": None,
            }
            for row in rows
        ]


def _serialize(vector: list[float]) -> bytes:
    import struct  # noqa: PLC0415

    return struct.pack(f"{len(vector)}f", *vector)
