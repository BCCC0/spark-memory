"""Idempotency keys — shared by the client and the server.

They live at the top level, **not** under ``mem.server``, so the CLI can compute a
key without importing Starlette (``MEM-RESOLUTIONS.md`` keeps the client
dependency-free).

The key identifies the whole request, not merely its note bytes. It carries the
**operation** (``MEM-RESOLUTIONS.md`` S25), optimistic-concurrency precondition,
and the metadata that changes a committed result. Otherwise a new commit message or
origin path can be silently replaced by an old cached response. `rm` has no content
to hash, so its key omits that component entirely:

    add:  <sha256(content)>:<path>:<if_match or "">:<sha256(origin,message)>:add
    rm:   <path>:<if_match or "">:<sha256(message)>:rm

The precondition is part of the key because a *replay* is checked **before** the
CAS: without it, re-adding unchanged content with a different `--expect` would
collide with a replay and silently skip the compare-and-swap (``S21``).
"""

from __future__ import annotations

import hashlib


def _metadata_hash(*values: str | None) -> str:
    """Return an unambiguous, safe key component for request metadata."""
    data = "\0".join(value or "" for value in values).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def add_key(
    content_sha256: str,
    path: str,
    if_match: str | None,
    *,
    origin_path: str | None = None,
    message: str | None = None,
) -> str:
    return f"{content_sha256}:{path}:{if_match or ''}:{_metadata_hash(origin_path, message)}:add"


def rm_key(path: str, if_match: str | None, *, message: str | None = None) -> str:
    return f"{path}:{if_match or ''}:{_metadata_hash(message)}:rm"


def mv_key(source: str, destination: str, if_match: str | None, *, message: str | None = None) -> str:
    return f"{source}:{destination}:{if_match or ''}:{_metadata_hash(message)}:mv"
