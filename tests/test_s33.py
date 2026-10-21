"""S33 — defects an independent e2e pass found in S32, and what now pins them.

The D1 defect is the important one: `S32` claimed the query embed ran in a worker
thread; it did not, so a hybrid read put the model back on the event loop. The
behavioural proof of that needs the model and therefore a host with the model cached; what can be pinned
anywhere is the *shape* of the call, which is exactly what regressed.
"""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path

from mem.server import embedding


def test_a_hybrid_read_does_not_embed_on_the_event_loop():
    """A source-level check, deliberately — and it pins the exact regression.

    `VectorIndex.search` embeds the query, so calling it directly from the async
    handler blocks the loop on a cold cache. A behavioural test needs the model
    (model cached); this asserts the call is wrapped in `asyncio.to_thread` at all, which is
    the thing that was missing while a doc claimed otherwise.
    """
    source = (
        Path(__file__).resolve().parents[1] / "src" / "mem" / "server" / "app.py"
    ).read_text(encoding="utf-8")

    assert "vector_hits = await asyncio.to_thread(" in source, (
        "the hybrid read must dispatch the vector search to a worker thread"
    )
    assert not re.search(r"vector_hits = vectors\.search\(", source), (
        "a bare `vectors.search(...)` call is back on the event loop"
    )
    # The read path dispatches its git-subprocess work too.
    assert "await asyncio.to_thread(\n            snapshot,\n            list_records," in source
    assert "record = await asyncio.to_thread(_build)" in source


def test_write_transactions_do_not_block_the_event_loop():
    """Git and the blocking write mutex must run together in a worker thread."""
    source = (
        Path(__file__).resolve().parents[1] / "src" / "mem" / "server" / "app.py"
    ).read_text(encoding="utf-8")

    assert "result = await asyncio.to_thread(\n            _run_inline," in source


def test_model_construction_is_serialised(tmp_path):
    """Two cold callers must not build two ONNX sessions (S33).

    `embed()` had a lock; construction did not, so the reindex worker and a hybrid
    request could each build a model — the arena leak that
    `enable_cpu_mem_arena=False` exists to avoid.
    """
    built: list[int] = []

    class SlowEmbedder:
        def __init__(self, *args, **kwargs):
            built.append(1)
            time.sleep(0.2)  # widen the window the lock has to cover

        def embed(self, texts):
            return [[0.0] * embedding.DIM for _ in texts]

    original = embedding.Embedder
    embedding.Embedder = SlowEmbedder
    try:
        index = embedding.VectorIndex(tmp_path / "vectors.db")
        threads = [
            threading.Thread(target=index._embedder_or_fail) for _ in range(8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        embedding.Embedder = original

    assert len(built) == 1, f"the model was constructed {len(built)} times"


def test_a_rebuild_does_not_invalidate_the_schema_marker(tmp_path):
    """`rebuild()` clears `meta`; the marker must not live there (S33).

    While it did, the next process start read "no schema" as "old schema" and wiped the
    freshly built index — so `mem index --rebuild` cost a second full corpus embed after
    the next restart.
    """
    path = tmp_path / "vectors.db"
    index = embedding.VectorIndex(path)
    with index._connect() as conn:
        stored = conn.execute(
            "SELECT value FROM index_schema WHERE key = 'chunk_schema'"
        ).fetchone()
    assert stored is not None and stored["value"] == embedding.CHUNK_SCHEMA

    # Exactly what `rebuild()` deletes.
    with index._connect() as conn:
        conn.execute("DELETE FROM chunk")
        conn.execute("DELETE FROM vec_chunk")
        conn.execute("DELETE FROM meta")

    # A new process attaches to the same file: the marker survived, so nothing is wiped.
    embedding.VectorIndex(path)
    with embedding.VectorIndex(path)._connect() as conn:
        still = conn.execute(
            "SELECT value FROM index_schema WHERE key = 'chunk_schema'"
        ).fetchone()
    assert still is not None and still["value"] == embedding.CHUNK_SCHEMA, (
        "a rebuild looked like a schema change to the next process"
    )
