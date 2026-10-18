"""Idempotency for writes.

A network failure *after* the server wrote would otherwise duplicate an item on
retry — the gap the prior art admits to (``MEM-SERVER.md`` §4). The key is derived
from the request and the server replays the original result.

**The key carries the operation** (``MEM-RESOLUTIONS.md`` S25). It previously did
not, which was fine while there was only one write verb and wrong the moment there
were two: `add` and `rm` on the same path would have shared a key. `rm` has no
content to hash, so its key omits that component entirely.

    add:  <sha256(content)>:<path>:<if_match or "">:add
    rm:   <path>:<if_match or "">:rm

``MEM-RESOLUTIONS.md`` S5 fixes the bound at the last 1000 keys, in insertion
order. The store lives under ``.mem/``, which is gitignored and rebuildable, so
losing it costs at most a duplicate-detection window — never content.

> This module was called ``staging.py``. It never held staging — it holds the
> idempotency map — and the name became actively misleading once staging was
> deleted (``MEM-RESOLUTIONS.md`` S23).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..keys import add_key, rm_key

LIMIT = 1000

__all__ = ["IdempotencyStore", "add_key", "rm_key", "LIMIT"]


class IdempotencyStore:
    def __init__(self, path: Path, limit: int = LIMIT) -> None:
        self.path = Path(path)
        self.limit = limit
        self._data: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._data = raw
        except (json.JSONDecodeError, OSError):
            # A corrupt store is not worth failing a write over: start clean.
            self._data = {}

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Trim oldest first; dict preserves insertion order.
        while len(self._data) > self.limit:
            self._data.pop(next(iter(self._data)))
        self.path.write_text(json.dumps(self._data), encoding="utf-8")

    def get(self, key: str) -> Any | None:
        return self._data.get(key)

    def put(self, key: str, value: Any) -> None:
        self._data[key] = value
        self._flush()

    def __len__(self) -> int:
        return len(self._data)
