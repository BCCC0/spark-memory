"""The daemon — routes, auth wiring, and the atomic write path.

MVP-0 is responsibilities 1–4 of ``MEM-SERVER.md`` §1: the repository, the file
server, the write lock, and ``id ≡ path``. There is no staging (``S23`` deleted
it), no index and no web agent.

Three deliberate departures from the letter of the spec, all recorded:

* ``GET /health`` is **unauthenticated** — a liveness probe returning
  ``ok``/``version``/``head``. The head sha is already public in the repo and is
  not a secret. Everything else requires a token.
* **A read URL may carry its own credential instead of a token.** ``/raw`` accepts a
  signed compact ``?e=&s=`` link (and legacy ``?exp=&sig=`` links), which is how a caller with no device token — an
  browser or other HTTP client — fetches a note larger than the inline threshold. The
  bearer path is unchanged, and a signature is bound to the note, the expiry and the
  revision. See ``MEM-RESOLUTIONS.md`` S31.
* ``POST /v1/add`` takes an extra ``origin_path`` field. The spec listed only
  ``project``/``path``/file bytes, but ``origin`` is ``{device, absolute path}``
  (``MEM-SCHEMA.md`` §3.4) and the absolute path on the *client* cannot be derived
  from the note id. See ``MEM-RESOLUTIONS.md`` S11/S12.

Every write is one atomic act: ``/v1/add`` = write → ``git add`` → ``git commit``,
``/v1/rm`` = remove → ``git commit``. The commit is **path-limited** and a failure
**rolls back**, so the repository cannot be left unclean (``S26``).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import datetime as dt
import hashlib
import hmac
import json
import logging
import shutil
import re
import secrets
import sys
import tempfile
import time
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import quote, unquote

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from ..errors import MemError
from ..ids import note_relpath, text_relpath, split_public_path, validate_id, validate_project
from ..pointers import pointer_record
from ..version import API_VERSION
from . import embedding
from . import frontmatter as fm
from .auth import DeviceRegistry
from .lock import WriteLock
from .repo import MemoryRepo
from .queue import WriteQueue
from .search import SearchIndex
from .idempotency import IdempotencyStore, add_key, rm_key
from .moves import plan_move, commit_move, recover_move

OPEN_PATHS = {"/health", "/v1/enroll"}
logger = logging.getLogger("mem.queue")
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
DEFAULT_LIMIT = 50
DEFAULT_INLINE_EXTENSIONS = (".md", ".txt")

_SINCE = re.compile(r"^(\d+)([smhdw])$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


@dataclass
class Settings:
    repo_root: Path
    devices_db: Path
    enroll_secret: Path
    state_dir: Path
    max_bytes: int = 4096
    inline_extensions: tuple[str, ...] = DEFAULT_INLINE_EXTENSIONS
    lock_timeout: float = 15.0
    #: The address clients use to fetch shared note bodies.
    base_url: str = "http://127.0.0.1:8321"
    secret: str = field(default="", repr=False)
    #: The capability-link secret (``S31``). Its own file, beside the registry: it
    #: grants read access to every note, so it is as sensitive as ``devices.db`` and
    #: belongs in the same backup set. Rotating it invalidates every outstanding link.
    link_secret: Path = field(default_factory=lambda: Path.home() / "mem" / "link.secret")
    #: How long a signed read URL stays valid. The signature *is* the credential, so
    #: this is the only bound on a link that leaks into a transcript or a log.
    link_ttl: int = 86400
    #: Resolved lazily and cached, mirroring `secret`. Never logged or repr'd.
    link_key: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        allowed = tuple(dict.fromkeys(str(ext).strip().lower() for ext in self.inline_extensions))
        if ".md" not in allowed or any(not re.fullmatch(r"\.[a-z0-9]+", ext) for ext in allowed):
            raise ValueError("inline_extensions must include .md and contain only dot-prefixed extensions")
        self.inline_extensions = allowed


# --------------------------------------------------------------------------
# helpers


def _error_response(exc: MemError) -> JSONResponse:
    return JSONResponse(exc.to_dict(), status_code=exc.http_status)


def _since_seconds(value: str | None) -> int | None:
    if not value:
        return None
    match = _SINCE.match(str(value).strip())
    if not match:
        return None
    return int(match.group(1)) * _UNIT_SECONDS[match.group(2)]


def _iso_epoch(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def _encode_cursor(record: dict) -> str:
    raw = f"{record.get('updated') or ''}|{record.get('id') or ''}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[str, str] | None:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    if "|" not in raw:
        return None
    updated, note_id = raw.split("|", 1)
    return updated, note_id


def _int_param(
    params: Any, name: str, default: int, *, low: int = 1, high: int = 200
) -> int:
    """Parse a bounded integer query parameter, or fail with a typed error.

    An unvalidated ``int()`` here used to raise inside the handler and surface as
    a bare HTTP 500 in ``text/plain``, which broke invariant C6.
    """
    raw = params.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise MemError("invalid_request", f"'{name}' must be an integer, got {raw!r}") from None
    return max(low, min(value, high))


def _headings(body: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for number, line in enumerate(body.splitlines(), start=1):
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if match:
            found.append(
                {"level": len(match.group(1)), "text": match.group(2), "line": number}
            )
    return found


def _has_heading(repo: MemoryRepo, project: str, note_id: str, anchor: str) -> bool:
    """Does the target note have a heading matching ``anchor``? (S35)

    Best-effort: a note that cannot be read produces no extra warning rather than a
    spurious one, because this is a diagnostic on someone else's note.
    """
    try:
        rel = repo.public_path(project, note_id)
        text = repo.read_note(rel)
        _, body = fm.parse(text) if rel.endswith(".md") else ({}, text)
    except Exception:  # noqa: BLE001 - a diagnostics check must not break a read
        return True
    return any(heading["text"].lower() == anchor.strip().lower() for heading in _headings(body))


def _slice_bounds(
    body: str, *, section: str | None, range: tuple[int, int] | None
) -> tuple[int, int]:
    """Resolve a section or a line range to an inclusive 1-based body span (S35).

    Body-relative and 1-based is what `search` reports as `line_range` and what
    `_headings` returns, so a hit's coordinate means the same thing here — no
    conversion layer exists or is needed (D5).
    """
    if section is not None and range is not None:
        raise MemError(
            "invalid_request",
            "give either a section or a range, not both",
            {"section": section, "range": list(range)},
        )
    lines = body.splitlines()
    total = len(lines)

    if section is not None:
        headings = _headings(body)
        match = next(
            (h for h in headings if h["text"].lower() == section.strip().lower()), None
        )
        if match is None:
            # `available` mirrors how the unknown-search-mode error lists what IS
            # valid — the caller can correct itself without another round trip.
            raise MemError(
                "not_found",
                f"no section {section!r} in this note",
                {"section": section, "available": [h["text"] for h in headings]},
            )
        start = match["line"]
        end = total
        for heading in headings:
            if heading["line"] > start and heading["level"] <= match["level"]:
                end = heading["line"] - 1
                break
        return start, max(start, end)

    start, end = range  # type: ignore[misc]
    if start < 1 or end < start or end > total:
        # Deliberately an error rather than a clamp: a range that no longer fits means
        # the coordinate moved, and silently returning other lines would be a lie.
        raise MemError(
            "invalid_request",
            f"range {start}-{end} is outside this note",
            {"line_count": total, "range": [start, end]},
        )
    return start, end


def _parse_range(text: str) -> tuple[int, int]:
    """``start-end``, 1-based inclusive — what the CLI's ``id:start-end`` becomes."""
    head, _, tail = text.partition("-")
    try:
        return int(head), int(tail)
    except ValueError:
        raise MemError(
            "invalid_request",
            f"range {text!r} is not <start>-<end>",
            {"range": text},
        ) from None


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return None


def _link_signature(key: str, project: str, note_id: str, exp: int, rev: str = "") -> str:
    """A capability signature over exactly what the URL grants (``S31``).

    The revision is covered as well, so a holder cannot change which version the
    capability exposes.
    """
    material = f"{project}\n{note_id}\n{exp}\n{rev}".encode("utf-8")
    return hmac.new(key.encode("utf-8"), material, hashlib.sha256).hexdigest()[:32]


def _link_key(settings: Settings) -> str:
    """The link secret, created on first use and cached like ``settings.secret``."""
    if not settings.link_key:
        settings.link_key = _load_or_create_secret(settings.link_secret)
    return settings.link_key


def _base36(value: int) -> str:
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    if value == 0:
        return "0"
    out = ""
    while value:
        value, digit = divmod(value, 36)
        out = alphabet[digit] + out
    return out


def _link_url(
    settings: Settings, project: str, note_id: str, revision: str | None = None
) -> str:
    """A URL that needs no ``Authorization`` header, bound to an expiry (``S31``).

    Built from the **configured** ``base_url`` rather than the host this request
    arrived on, so one link resolves from both the LAN and the tailnet.
    """
    exp = int(time.time()) + settings.link_ttl
    signature = bytes.fromhex(
        _link_signature(_link_key(settings), project, note_id, exp, revision or "")
    )
    sig = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    query = f"e={_base36(exp)}&s={sig}"
    if revision:
        query += f"&r={quote(revision)}"
    return (
        f"{settings.base_url.rstrip('/')}/raw/{quote(project)}/{quote(note_id)}"
        f"?{query}"
    )


def _capability_request(request: Request, settings: Settings) -> tuple[str, str] | None:
    """The ``(project, id)`` a valid capability signature authorises, or ``None``.

    ``None`` on every doubt — absent, malformed, expired, tampered, or carrying a
    ``rev`` the signature does not cover — so the caller falls through to the bearer
    check and sees today's 401 rather than a new failure mode.
    """
    params = request.query_params
    compact = params.get("e") is not None or params.get("s") is not None
    exp_raw = params.get("e") if compact else params.get("exp")
    sig = params.get("s") if compact else params.get("sig")
    if not exp_raw or not sig:
        return None
    try:
        exp = int(exp_raw, 36) if compact else int(exp_raw)
    except ValueError:
        return None
    if exp < time.time():
        return None
    parts = [p for p in request.url.path.split("/") if p]
    # ['raw', '<project>', '<id segment>', ...] — ids are path-shaped, so the id is
    # everything after the project.
    if len(parts) < 3 or parts[0] != "raw":
        return None
    project = unquote(parts[1])
    note_id = "/".join(unquote(part) for part in parts[2:])
    revision = params.get("r") if compact else params.get("rev")
    if compact and params.get("rev") is not None:
        return None
    if not compact and params.get("r") is not None:
        return None
    expected = bytes.fromhex(
        _link_signature(_link_key(settings), project, note_id, exp, revision or "")
    )
    if compact:
        try:
            padded = sig + "=" * (-len(sig) % 4)
            supplied = base64.urlsafe_b64decode(padded.encode("ascii"))
        except (binascii.Error, UnicodeEncodeError, ValueError):
            return None
        valid = hmac.compare_digest(supplied, expected)
    else:
        valid = hmac.compare_digest(sig, expected.hex())
    return (project, note_id) if valid else None


# --------------------------------------------------------------------------
# note projection

# A basename that is an opaque identifier rather than a filename: a UUID, or a
# long unbroken hex run. The live corpus contains a recorded origin path whose
# basename is a bare 24-hex id, and handing that to a caller as "your working
# file" is worse than handing nothing, because it looks actionable and is not.
_OPAQUE_BASENAME_RE = re.compile(
    r"^(?:[0-9a-f]{16,}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$",
    re.IGNORECASE,
)


def _looks_like_location(value: str) -> bool:
    """True when *value* is an absolute path that could actually be opened.

    Absolute is necessary but not sufficient: a UUID or an extensionless hash is
    an identifier that happens to sit in a directory, not a file a caller can
    read, so it is never offered as one.
    """
    if not value:
        return False
    # Deliberately NOT `Path(value).is_absolute()`: on Windows a POSIX path such as
    # `/mnt/d/src/x.md` is not absolute, and the whole point of recording the
    # uploader's path is that the uploader may have been on the other OS.
    if not (
        PureWindowsPath(value).is_absolute() or PurePosixPath(value).is_absolute()
    ):
        return False
    basename = PureWindowsPath(value).name or PurePosixPath(value).name
    if not basename:
        return False
    stem, dot, suffix = basename.rpartition(".")
    if not dot or not suffix:
        # No extension: accept only a plainly-named file, not an opaque id.
        return bool(basename) and not _OPAQUE_BASENAME_RE.match(basename)
    return not _OPAQUE_BASENAME_RE.match(stem)


def _body_only(text: str) -> str:
    """Strip YAML frontmatter so only the note's prose remains.

    Used to hash what the author actually edits. The server re-serialises
    frontmatter on upload (it records `origin:` and reflows `tags`), so a working
    file is never byte-identical to the committed revision. A byte-level
    comparison would call every note diverged and make freshness meaningless.

    The closing delimiter must be found on a line of its own. A naive
    ``split("\\n---")`` breaks on multi-line YAML values -- a Windows `path:`
    contains a backslash and the next line begins with two spaces, and a summary
    can contain a literal ``---`` -- which silently hashed frontmatter as if it
    were body.
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return text
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "\n".join(lines[index + 1 :]).lstrip("\n")
    return text


def build_record(
    repo: MemoryRepo,
    project: str,
    note_id: str,
    settings: Settings,
    *,
    revision: str | None = None,
    max_bytes: int = 4096,
    known: set[tuple[str, str]] | None = None,
    caller_device_id: str | None = None,
    caller_alias: str | None = None,
    alias_for_device: Any = None,
    section: str | None = None,
    range: tuple[int, int] | None = None,
    link_only: bool = False,
) -> dict[str, Any]:
    rel = repo.public_path(project, note_id, revision)
    try:
        raw = repo.note_bytes(rel, revision)
    except MemError as exc:
        if exc.code == "not_found":
            raise MemError(
                "target_moved",
                f"no note at id {project}/{note_id}",
                {"id": note_id, "project": project},
            ) from exc
        raise

    text = raw.decode("utf-8", "replace")
    pointer = pointer_record(text) if rel.endswith(".md") else None
    if pointer:
        result: dict[str, Any] = {
            "id": note_id, "project": project, "kind": pointer.kind,
            "media_type": "text/plain", "summary": pointer.summary,
            "sha256": repo.sha256_bytes(raw), "bytes": len(raw),
            "revision": revision or repo.head(), "truncated": False,
        }
        result["pointer"] = pointer.canonical if pointer.kind != "locked" else f"@locked({pointer.alias}:<redacted>)"
        result["points_at"] = pointer.alias
        result["local_resolvable"] = None
        if pointer.kind == "locked" and caller_alias and caller_alias == pointer.alias:
            result["local_path"] = pointer.path
        return result
    is_text_file = not rel.endswith(".md")
    data, body = ({}, text) if is_text_file else fm.parse(text)
    # Hashing happens over the whole note, before a slice narrows the body the
    # caller receives. Freshness is a property of the committed revision, not of
    # the fragment this particular read asked for.
    whole_body = body
    errors: list[str] = []
    if not is_text_file:
        try:
            fm.validate(data, note_id)
        except MemError as exc:
            errors.append(exc.message)

    # Broken links are produced HERE rather than by a verb (MEM-RESOLUTIONS.md
    # S27): `doctor` was their only named producer and it is gone, so removing it
    # without this would have silently deleted the capability.
    warnings = [] if is_text_file else fm.content_warnings(data, body)
    links: list[str] = []
    if known is not None and not is_text_file:
        for target, anchor in fm.note_refs(body):
            if (project, target) not in known:
                warnings.append(f"link -> {target} does not exist")
                continue
            links.append(target)
            # An anchor is kept now (S35 D1), so a link that names a section which no
            # longer exists must say so. Checked here (the `read` path, where the
            # target is read anyway) and NOT in `list_records`: validating every link
            # of every row would turn one `ls` into a corpus walk.
            if anchor and not _has_heading(repo, project, target, anchor):
                warnings.append(f"link -> {target}#{anchor} has no such section")

    # A slice: `search` already hands out a `line_range`, and until now nothing could
    # consume one. Resolved against the SAME body the coordinates are numbered from,
    # so a hit's range means here exactly what it meant there (S35 D5).
    sliced = section is not None or range is not None
    slice_start = slice_end = 0
    if sliced:
        slice_start, slice_end = _slice_bounds(body, section=section, range=range)
        body = "\n".join(body.splitlines()[slice_start - 1 : slice_end])

    size = len(raw)
    origin = data.get("origin") if isinstance(data.get("origin"), dict) else {}
    same_origin_device = bool(
        caller_device_id and origin.get("device") == caller_device_id
    )
    # The committed revision's time, so a caller can compare it against its own
    # working file rather than being told which copy is authoritative.
    meta_updated = repo.note_updated(rel, revision)
    truncated = False if sliced else size > max_bytes
    # A "handoff" is a read whose body is not inlined and therefore needs a URL.
    # A slice is not a handoff: it always inlines the bounded fragment it named.
    handoff = truncated or (link_only and not sliced)
    origin_path = origin.get("path")

    record: dict[str, Any] = {
        "id": note_id,
        "project": project,
        "kind": "text" if is_text_file else "note",
        "media_type": "text/plain" if is_text_file else "text/markdown",
        "title": fm.title_of(body),
        "summary": data.get("summary") or fm.title_of(body) or (body.splitlines()[0][:200] if is_text_file and body.splitlines() else ""),
        "status": data.get("status"),
        "type": data.get("type"),
        "tags": data.get("tags", []),
        "sha256": repo.sha256_bytes(raw),
        "bytes": size,
        "line_count": body.count("\n") + 1,
        "revision": revision or repo.head(),
        # The commit message of THIS revision — the description of the change
        # (MEM-RESOLUTIONS.md S24). Derived from git, never authored.
        "message": (
            repo.revision_message(revision)
            if revision
            else repo.note_message(rel)
        ),
        "warnings": warnings,
        "errors": errors,
        # Omitted rather than null when `known` was unavailable (S35).
        **({"links": links} if known is not None and not is_text_file else {}),
        # A slice is a partial view, so the range it actually returned IS part of the
        # answer — a caller must be able to cite what it read (S35).
        **({"range": [slice_start, slice_end]} if sliced else {}),
        # A slice bypasses the cap (S35 D2): C1 exists to stop a WHOLE note flooding a
        # client, and a caller that named a section has already bounded the cost.
        "truncated": truncated,
        # The body was omitted by request, not by the cap. Keeping this separate
        # from `truncated` lets a caller tell a deliberate link-only read from a
        # size-driven handoff.
        **({"link_only": True} if link_only and not sliced else {}),
    }
    if handoff:
        # The committed revision is ALWAYS addressable, and always identified by
        # its bytes. `local_path`/`fetch_url` used to be mutually exclusive, which
        # left the origin device unable to reach the committed revision at all --
        # the one case where it matters most, because the working file it was
        # handed may be either ahead of or behind what memory holds.
        if revision:
            # Historical reads remain bearer-only; the compact revision path is
            # not a capability and cannot grant anonymous access to old content.
            record["fetch_url"] = (
                f"{settings.base_url.rstrip('/')}/raw/{quote(project)}/{quote(note_id)}"
                f"?r={quote(revision)}"
            )
        else:
            record["fetch_url"] = _link_url(settings, project, note_id)

    # Body-only hash: what the author edits, with the server-owned frontmatter
    # excluded. This is the one a caller can meaningfully compare its working
    # file against (see `_body_only`). It is computed from the WHOLE body even
    # when the response carries a slice, so freshness does not change with the
    # selector the caller used. Trailing blank lines are normalised away:
    # whether a file ends in a newline is not an author edit.
    record["body_sha256"] = hashlib.sha256(
        _body_only(whole_body).rstrip("\n").encode("utf-8")
    ).hexdigest()

    if origin_path and _looks_like_location(str(origin_path)):
        if same_origin_device:
            # The caller's OWN working file, recorded when it was uploaded. It
            # is a hint, not the committed revision: the file may have moved or
            # changed since. The client hashes it and reports which is fresher.
            record["origin_path"] = str(origin_path)
            record["note_updated"] = meta_updated
        elif alias_for_device and handoff:
            author_alias = alias_for_device(str(origin.get("device") or ""))
            if author_alias:
                record["origin_pointer"] = f"@file({author_alias}:{origin_path})"
    if sliced or (size <= max_bytes and not link_only):
        record["body"] = body
    return record


def known_ids(repo: MemoryRepo) -> set[tuple[str, str]]:
    """Every ``(project, id)`` in the repo — the corpus a link can resolve against."""
    return {(meta.project, meta.id) for meta in repo.notes()}


def note_source(repo: MemoryRepo, relpath: str) -> embedding.NoteSource:
    """Everything the semantic index needs from one note (S32).

    The title is embedded (Basic Memory embeds title + permalink, not the bare body),
    and `status`/`type`/`tags` are carried so the vector half can apply the same
    filters as the text half instead of silently ignoring them.
    """
    try:
        text = repo.read_note(relpath)
        pointer = pointer_record(text) if relpath.endswith(".md") else None
        if pointer:
            return embedding.NoteSource(title=pointer.summary, body="")
        data, body = fm.parse(text) if relpath.endswith(".md") else ({}, text)
    except Exception:  # noqa: BLE001 - a broken note must not break indexing
        return embedding.NoteSource(title="", body="")
    tags = data.get("tags")
    status = data.get("status")
    note_type = data.get("type")
    return embedding.NoteSource(
        title=fm.title_of(body) or "",
        body=body,
        status=str(status) if status is not None else None,
        note_type=str(note_type) if note_type is not None else None,
        tags=[str(tag) for tag in tags] if isinstance(tags, list) else None,
    )


def _summary_of(repo: MemoryRepo, project: str | None, note_id: str) -> str:
    """Summary for a hit only the vector half found — it is a per-chunk index."""
    if not project:
        return ""
    try:
        rel = repo.public_path(project, note_id)
        text = repo.read_note(rel)
        pointer = pointer_record(text) if rel.endswith(".md") else None
        if pointer:
            return pointer.summary
        data, _ = fm.parse(text) if rel.endswith(".md") else ({}, text)
    except Exception:  # noqa: BLE001
        return ""
    return str(data.get("summary", ""))


def merge_hits(
    text_hits: list[dict[str, Any]], vector_hits: list[dict[str, Any]], limit: int
) -> list[dict[str, Any]]:
    """Merge two ranked lists by **reciprocal rank fusion**.

    RRF is used rather than score blending because bm25 and cosine distance are
    not on a comparable scale, and inventing a weight between them would be a
    number nobody could justify. RRF only needs the *ranks*, which both lists
    genuinely have (`MEM-RESOLUTIONS.md` S3).
    """
    fused: dict[str, dict[str, Any]] = {}
    for ranking in (text_hits, vector_hits):
        for rank, hit in enumerate(ranking, start=1):
            key = f"{hit.get('project')}/{hit['id']}"
            entry = fused.setdefault(
                key, {"hit": dict(hit), "score": 0.0, "sources": []}
            )
            entry["score"] += 1.0 / (60 + rank)
            entry["sources"].append("text" if ranking is text_hits else "vector")
            # Prefer the finer-grained coordinate when the vector index has one.
            if ranking is vector_hits and hit.get("line_range"):
                entry["hit"]["line_range"] = hit["line_range"]
    ordered = sorted(fused.values(), key=lambda e: e["score"], reverse=True)
    out: list[dict[str, Any]] = []
    for entry in ordered[:limit]:
        hit = entry["hit"]
        hit["score"] = round(entry["score"], 6)
        hit["matched_by"] = sorted(set(entry["sources"]))
        out.append(hit)
    return out


def list_records(
    repo: MemoryRepo,
    *,
    project: str | None = None,
    prefix: str | None = None,
    status: str | None = None,
    note_type: str | None = None,
    tag: str | None = None,
    since: str | None = None,
    sort: str = "updated",
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
    full: bool = False,
    alias_for_device: Any = None,
) -> dict[str, Any]:
    cutoff = None
    seconds = _since_seconds(since)
    if seconds is not None:
        cutoff = dt.datetime.now(dt.timezone.utc).timestamp() - seconds

    known = known_ids(repo)
    records: list[dict[str, Any]] = []
    topic_records: dict[str, dict[str, Any]] = {}
    for meta in repo.notes():
        if project and meta.project != project:
            continue
        if prefix and not (
            meta.id == prefix.rstrip("/") or meta.id.startswith(prefix.rstrip("/") + "/")
        ):
            continue
        try:
            raw = repo.note_bytes(meta.path)
            text = raw.decode("utf-8", "replace")
            is_text_file = not meta.path.endswith(".md")
            data, body = ({}, text) if is_text_file else fm.parse(text)
            pointer = pointer_record(text) if not is_text_file else None
        except Exception:  # noqa: BLE001 - a broken note must not break listing
            continue
        if status and data.get("status") != status:
            continue
        if note_type and data.get("type") != note_type:
            continue
        tags = data.get("tags") or []
        if tag and tag not in tags:
            continue
        if cutoff is not None:
            epoch = _iso_epoch(meta.updated)
            if epoch is not None and epoch < cutoff:
                continue
        warnings = [] if pointer or is_text_file else fm.content_warnings(data, body)
        if not pointer and not is_text_file:
            warnings.extend(
                f"link -> {target} does not exist"
                for target in fm.note_links(body)
                if (meta.project, target) not in known
            )
        if prefix:
            base = prefix.strip("/")
            remainder = meta.id[len(base) + 1 :] if meta.id.startswith(base + "/") else ""
            if remainder and "/" in remainder:
                child_topic = f"{base}/{remainder.split('/', 1)[0]}"
                topic = topic_records.setdefault(child_topic, {
                    "id": child_topic, "kind": "topic", "media_type": "inode/directory",
                    "summary": "topic", "status": None, "type": None,
                    "updated": meta.updated, "warnings": [],
                })
                if meta.updated and (not topic.get("updated") or meta.updated > topic["updated"]):
                    topic["updated"] = meta.updated
                continue
        record = {
            "id": meta.id,
            "project": meta.project,
            "kind": pointer.kind if pointer else ("text" if is_text_file else "note"),
            "media_type": "text/plain" if pointer or is_text_file else "text/markdown",
            "summary": pointer.summary if pointer else (data.get("summary", "") or fm.title_of(body) or (body.splitlines()[0][:200] if is_text_file and body.splitlines() else "")),
            "status": data.get("status"),
            "type": data.get("type"),
            "updated": meta.updated,
            "warnings": warnings,
        }
        if full:
            record.update(
                {
                    "project": meta.project,
                    "tags": tags,
                    "created": meta.created,
                    "author": meta.author,
                    "message": meta.message,
                    "origin": ({
                        "device": data.get("origin", {}).get("device"),
                        **({"pointer": f"@file({alias_for_device(str(data['origin'].get('device') or ''))}:{data['origin'].get('path')})"}
                           if alias_for_device and isinstance(data.get("origin"), dict)
                           and data["origin"].get("path")
                           and alias_for_device(str(data["origin"].get("device") or ""))
                           and (Path(str(data["origin"].get("path"))).is_absolute()
                                or PureWindowsPath(str(data["origin"].get("path"))).is_absolute())
                           else {})
                    } if isinstance(data.get("origin"), dict) else None),
                    "sha256": repo.sha256_bytes(raw),
                    "bytes": len(raw),
                }
            )
        records.append(record)

    records.extend(topic_records.values())

    reverse = True
    if sort == "id":
        records.sort(key=lambda r: (r["project"] or "", r["id"]), reverse=False)
    else:
        records.sort(key=lambda r: (r.get("updated") or "", r["id"]), reverse=reverse)

    start = 0
    if cursor:
        decoded = _decode_cursor(cursor)
        if decoded:
            target = decoded
            for index, record in enumerate(records):
                if ((record.get("updated") or ""), record["id"]) == target:
                    start = index + 1
                    break

    window = records[start : start + max(1, limit)]
    if not full:
        for record in window:
            record.pop("project", None)
    next_cursor = _encode_cursor(records[start + len(window) - 1]) if len(records) > start + len(window) else None
    return {"notes": window, "next_cursor": next_cursor, "total": len(records)}


# --------------------------------------------------------------------------
# app


class Reindexer:
    """Bring the derived indexes up to date **off the request path** (``S32``).

    This is the shape Basic Memory uses: its vector sync is scheduled from the *write*
    path as an asyncio task, its read path never triggers one, and a search serves
    whatever is already `ready` (``index/local_schedulers.py``, ``search_router.py``).
    ``await asyncio.to_thread`` is what keeps the loop free while the embedding runs.

    There is no queue and no second process — one event, one task, and coalescing, so a
    burst of writes costs one pass rather than one pass per write.

    The `S29` note that prompted this claimed Basic Memory "runs a background job
    queue"; reading its source, `queue_wait_seconds_total` measures contention *inside
    one batching loop*, not a queue. There was less to copy than the note implied.
    """

    def __init__(self, repo: MemoryRepo, index: SearchIndex, vectors: Any) -> None:
        self._repo = repo
        self._index = index
        self._vectors = vectors
        self._event = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._failure_delay = 0.0
        # `requested` is the trigger; `runs`/`errors` are completions. They are
        # separate on purpose: a pass can be slow (a cold model) or stuck (no model at
        # all), and "was it scheduled?" must stay answerable while that is true.
        self.stats: dict[str, Any] = {
            "requested": 0,
            "runs": 0,
            "errors": 0,
            "last_error": None,
        }

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="mem-reindex")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    # -- the trigger -------------------------------------------------------

    def request(self) -> None:
        """Ask for a pass. Called from the write path, never blocks.

        Coalescing is implicit: a request arriving during a run leaves the event set,
        so the loop runs once more and the newest write is always eventually indexed.
        """
        self._event.set()
        self.stats["requested"] += 1

    def nudge(self) -> None:
        """Ask for a pass unless the last failure is still backing off.

        A read poll may discover an externally-made commit that no write path
        scheduled, so it still needs to trigger indexing. It must not, however,
        set the event on every poll while a failed pass is sleeping: that would
        retry at the poll rate instead of the configured backoff.
        """
        if self._failure_delay > 0:
            return
        self.request()

    async def _loop(self) -> None:
        while True:
            await self._event.wait()
            self._event.clear()
            try:
                await asyncio.to_thread(self._run_once)
                self.stats["runs"] += 1
                self._failure_delay = 0.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the daemon outlives the index
                # A missing model cache or a broken corpus must not kill the service,
                # and must not become a hot loop on every write either (S32).
                self.stats["errors"] += 1
                self.stats["last_error"] = f"{type(exc).__name__}: {exc}"
                print(
                    f"memd: reindex failed: {self.stats['last_error']}",
                    file=sys.stderr,
                )
                self._failure_delay = min(
                    60.0, max(1.0, (self._failure_delay or 0.5) * 2)
                )
                await asyncio.sleep(self._failure_delay)

    def _run_once(self) -> None:
        """Runs in a worker thread: no event loop, so nothing here can block one.

        Only the **vectors**. The text index needs no model, is cheap, and is still
        rebuilt on the read path (off the loop) — so a write never waits for it, and a
        wedged or missing model cannot delay it either. That coupling was a real bug in
        the first version of this: the text rebuild sat behind an embedding that was
        never going to finish.
        """
        with self._repo.pinned():
            head = self._repo.head()
            self._index.ensure(self._repo, head)
            if self._vectors is None:
                return
            metas = self._repo.notes()
            sources = {meta.path: note_source(self._repo, meta.path) for meta in metas}
        self._vectors.sync(
            metas, sources.__getitem__, head
        )


def create_app(settings: Settings) -> Starlette:
    repo = MemoryRepo(settings.repo_root, settings.inline_extensions)
    repo.ensure()
    # The committed blob is the read source; Git must not normalize CRLF bytes.
    repo.git("config", "core.autocrlf", "false")
    # BUG(low): this store is effectively dead code, and it is what remains of a
    # documented guarantee that no longer holds.
    # Why valid: every live caller of the write path passes `key=None` (see
    # `_execute_queued`), so `store.get`/`store.put` are reachable only from the
    # unreachable non-dry-run branch of `_move`, and from `recover_move`'s replay branch,
    # which is gated on a `key` that is always None. Idempotency is therefore the queue's
    # `idem` column alone, and the file-backed "bounded key window" is unenforced.
    # Fix hint: delete the store and its plumbing, or make it the single idempotency
    # mechanism and delete the queue's; keeping both is what let the bound rot.
    store = IdempotencyStore(settings.state_dir / "idempotency.json")
    recover_move(repo, settings.state_dir, store)

    def snapshot(func, *args, **kwargs):
        with repo.pinned():
            return func(*args, **kwargs)
    registry = DeviceRegistry(settings.devices_db)
    # Resolve the capability-link secret NOW rather than on first use (S31). Eager is
    # both safer and more honest: two concurrent first requests could otherwise each
    # generate a different key, one of which would mint signatures that never verify,
    # and an unwritable secret directory should fail at startup rather than on the
    # first read.
    _link_key(settings)
    lock = WriteLock(settings.lock_timeout)
    index = SearchIndex(settings.state_dir / "search.db")
    # The semantic half is optional: without the `embed` extra, `search` is the
    # FTS5 service it has always been and `--mode hybrid` is refused (S16).
    vectors = (
        embedding.VectorIndex(settings.state_dir / "vectors.db")
        if embedding.available()
        else None
    )

    if not settings.secret:
        settings.secret = _load_or_create_secret(settings.enroll_secret)

    reindexer = Reindexer(repo, index, vectors)
    write_queue = WriteQueue(settings.state_dir, settings.repo_root)
    queue_wake = asyncio.Event()

    # -- handlers ----------------------------------------------------------

    async def health(request: Request) -> JSONResponse:
        # repo.head() spawns git; keep it off the event loop so an unauthenticated
        # liveness probe cannot stall every concurrent request.
        head = await asyncio.to_thread(repo.head)
        return JSONResponse(
            {"ok": True, "version": API_VERSION, "head": head}
        )

    async def policy(request: Request) -> JSONResponse:
        return JSONResponse({"inline_extensions": list(settings.inline_extensions),
                             "max_upload_bytes": MAX_UPLOAD_BYTES})

    async def enroll(request: Request) -> JSONResponse:
        payload = await _json_body(request)
        display_name = str(payload.get("display_name", "")).strip()
        supplied = str(payload.get("enroll_secret", ""))
        if not secrets.compare_digest(supplied, settings.secret):
            raise MemError("unauthorized", "enrollment secret is incorrect")
        alias = str(payload.get("alias") or "").strip()
        hostname = str(payload.get("hostname") or display_name).strip()
        os_name = str(payload.get("os") or "unknown").strip()
        arch = str(payload.get("arch") or "unknown").strip()
        record, token = registry.enroll(
            display_name, _client_ip(request), alias=alias, hostname=hostname,
            os_name=os_name, arch=arch,
        )
        return JSONResponse({**record, "token": token})

    async def status(request: Request) -> JSONResponse:
        project = request.query_params.get("project")
        if project:
            validate_project(project)
        def _status() -> dict[str, Any]:
            with repo.pinned():
                head = repo.head()
                indexed_head = index.indexed_head()
                indexed_at = index.indexed_at()
                dirty = repo.index_dirty()
                message = repo.head_message()
            return {
                "project": project,
                "head": head,
                # The last change made (S24) -- `/v1/status` reports the head
                # commit's message instead of a staging list.
                "message": message,
                "server": API_VERSION,
                "lock": lock.state(),
                "index": {
                    "present": indexed_head is not None,
                    "indexed_head": indexed_head,
                    # Seconds since the text index was rebuilt for this HEAD
                    # (MEM-SERVER.md §4 requires an age, not just a flag).
                    "age": (time.time() - indexed_at) if indexed_at else None,
                    "kind": "hybrid" if vectors is not None else "text",
                    "semantic": vectors is not None,
                },
                # Global, by resolution B3 -- and no longer a *staging* list.
                # MEM-RESOLUTIONS.md S23 deleted staging; what remains is
                # visibility into whether the tree holds uncommitted content
                # (S26). `true` here means something is wrong, not normal.
                "index_dirty": dirty,
                "read": {"max_bytes": settings.max_bytes},
            }
        return JSONResponse(await asyncio.to_thread(_status))

    async def queue_progress(request: Request) -> JSONResponse:
        raw = request.query_params.get("through")
        try:
            through = int(raw) if raw is not None else None
        except ValueError as exc:
            raise MemError("invalid_request", "through must be an integer") from exc
        if through is not None and through < 0:
            raise MemError("invalid_request", "through must be nonnegative")
        progress = await asyncio.to_thread(write_queue.progress, through)
        if progress["pending"] == 0:
            progress["head"] = await asyncio.to_thread(repo.head)
            progress["indexed_head"] = await asyncio.to_thread(index.indexed_head)
            if progress["head"] != progress["indexed_head"]:
                # An external commit has no write path to schedule indexing.
                # `nudge` still respects a failed pass's backoff.
                reindexer.nudge()
        progress["reindex"] = dict(reindexer.stats)
        return JSONResponse(progress)

    async def projects(request: Request) -> JSONResponse:
        def _project_rows() -> list[dict[str, Any]]:
            return snapshot(_collect_projects)

        def _collect_projects() -> list[dict[str, Any]]:
            counts: dict[str, dict[str, Any]] = {}
            for meta in repo.notes():
                entry = counts.setdefault(meta.project, {"project": meta.project, "notes": 0, "updated": None})
                entry["notes"] += 1
                if meta.updated and (entry["updated"] is None or meta.updated > entry["updated"]):
                    entry["updated"] = meta.updated
            for name in repo.project_names():
                counts.setdefault(name, {"project": name, "notes": 0, "updated": None})
            return sorted(counts.values(), key=lambda e: e["project"])

        return JSONResponse(await asyncio.to_thread(_project_rows))

    async def notes(request: Request) -> JSONResponse:
        params = request.query_params
        project = params.get("project")
        if project:
            validate_project(project)
        # OFF THE LOOP. `list_records` calls `known_ids()` and `repo.notes()`, each of
        # which walks the whole git history through a `git log --name-only` subprocess.
        # On the loop, concurrent `ls` calls starve everything else: measured, 30
        # simultaneous `ls` produced hard `/health` timeouts (>5 s) while every request
        # itself succeeded (S33).
        payload = await asyncio.to_thread(
            snapshot,
            list_records,
            repo,
            project=project,
            prefix=params.get("prefix"),
            status=params.get("status"),
            note_type=params.get("type"),
            tag=params.get("tag"),
            since=params.get("since"),
            sort=params.get("sort") or "updated",
            limit=_int_param(params, "limit", DEFAULT_LIMIT, low=1, high=500),
            cursor=params.get("cursor"),
            full=(params.get("full") or "").lower() in {"1", "true", "yes"},
            alias_for_device=registry.alias_for,
        )
        return JSONResponse(payload)

    async def manifest(request: Request) -> JSONResponse:
        params = request.query_params
        project = validate_project(params.get("project") or "")
        topic = (params.get("topic") or "").strip("/")
        if topic:
            validate_id(topic)
        subtree = f"projects/{project}/{topic}" if topic else f"projects/{project}"
        def _manifest() -> dict[str, Any]:
            head = repo.path_revision(subtree)
            entries = []
            for meta in repo.notes():
                if meta.project != project or (topic and not (meta.id == topic or meta.id.startswith(topic + "/"))):
                    continue
                raw = repo.note_bytes(meta.path)
                parsed = pointer_record(raw.decode("utf-8", "replace")) if meta.path.endswith(".md") else None
                entries.append({
                    "id": meta.id,
                    "kind": parsed.kind if parsed else ("note" if meta.path.endswith(".md") else "text"),
                    "media_type": "text/markdown" if meta.path.endswith(".md") and not parsed else "text/plain",
                    "size": len(raw), "sha256": repo.sha256_bytes(raw),
                    "revision": repo.path_revision(meta.path),
                })
            return {"project": project, "topic": topic, "revision": head, "entries": entries}

        return JSONResponse(await asyncio.to_thread(snapshot, _manifest))

    async def note(request: Request) -> JSONResponse:
        params = request.query_params
        project = params.get("project")
        if not project:
            raise MemError("invalid_request", "project is required")
        validate_project(project)
        note_id = request.path_params["id"]
        # OFF THE LOOP for the same reason as `ls`: building a record runs `known_ids`,
        # `repo.head()`, `repo.note_message()` and `note_bytes`, each a subprocess (S33).
        caller_device_id = request.state.device["device_id"]
        caller_alias = request.state.device.get("alias")
        revision = params.get("revision")
        if not revision:
            queued = await asyncio.to_thread(write_queue.state, project, note_id)
            if queued:
                def _committed_revision() -> str | None:
                    rel = repo.public_path(project, note_id, allow_missing=True)
                    return repo.path_revision(rel)
                queued["id"] = note_id
                queued["project"] = project
                queued["committed_revision"] = await asyncio.to_thread(snapshot, _committed_revision)
                return JSONResponse(queued)
        # A slice, so a caller can consume the `line_range` a search hit reported
        # instead of fetching the whole note (S35).
        section = params.get("section")
        raw_range = params.get("range")
        span = _parse_range(raw_range) if raw_range else None
        link_only = (params.get("link_only") or "").lower() in {"1", "true", "yes"}

        def _build() -> dict[str, Any]:
            return snapshot(build_record,
                repo,
                project,
                note_id,
                settings,
                revision=revision,
                max_bytes=_int_param(
                    params, "max_bytes", settings.max_bytes,
                    low=1, high=settings.max_bytes,
                ),
                known=known_ids(repo),
                caller_device_id=caller_device_id,
                caller_alias=caller_alias,
                alias_for_device=registry.alias_for,
                section=section,
                range=span,
                link_only=link_only,
            )

        record = await asyncio.to_thread(_build)
        _log_read(settings, record)
        return JSONResponse(record)

    async def raw(request: Request) -> Response:
        project = validate_project(request.path_params["project"])
        note_id = validate_id(request.path_params["id"])
        revision = request.query_params.get("r") or request.query_params.get("rev")
        try:
            def _raw_bytes():
                rel = repo.public_path(project, note_id, revision)
                return rel, repo.note_bytes(rel, revision)
            rel, body = await asyncio.to_thread(snapshot, _raw_bytes)
        except MemError as exc:
            if exc.code == "not_found":
                raise MemError(
                    "target_moved", f"no note at id {project}/{note_id}"
                ) from exc
            raise
        if rel.endswith(".md") and pointer_record(body.decode("utf-8", "replace")):
            raise MemError("invalid_request", "pointer entries have metadata only; target bytes are never served")
        return Response(body, media_type="text/markdown" if rel.endswith(".md") else "text/plain")

    async def search(request: Request) -> JSONResponse:
        params = request.query_params
        query = params.get("q") or ""
        project = params.get("project")
        if project:
            validate_project(project)
        mode = (params.get("mode") or "text").lower()
        if mode not in ("text", "hybrid"):
            # The design rule is explicit: an unsupported rung must raise, never
            # fall back (MEM-CLI.md §3).
            raise MemError(
                "invalid_request",
                f"unknown search mode {mode!r}",
                {"available": ["text", "hybrid"]},
            )
        if mode == "hybrid" and vectors is None:
            raise MemError(
                "internal_error",
                "the semantic index is not installed (the `embed` extra)",
                {"available": ["text"]},
            )

        head = await asyncio.to_thread(repo.head)
        text_head = await asyncio.to_thread(index.indexed_head)
        limit = _int_param(params, "limit", 20, low=1, high=200)
        text_hits = index.search(
            query,
            project=project,
            status=params.get("status"),
            note_type=params.get("type"),
            tag=params.get("tag"),
            limit=limit,
        )

        if mode == "text":
            for hit in text_hits:
                hit["revision"] = text_head
            payload: dict[str, Any] = {
                "hits": text_hits,
                "mode": "text",
                "revision": text_head,
                "queue_pending": await asyncio.to_thread(write_queue.pending_count, project),
                "index_lag": text_head != head,
            }
            if any(hit.get("relaxed") for hit in text_hits):
                # The strict query found nothing and the looser retry did. Say so at
                # the top level too, so a caller need not inspect every hit (S32).
                payload["relaxed"] = True
            return JSONResponse(payload)

        # Vectors are NOT synced here any more: the write path scheduled that (S32). A
        # hybrid search may therefore run against a slightly older vector index, and
        # says so rather than hiding it — "no results" and "not indexed yet" must be
        # distinguishable by the caller.
        lagging = await asyncio.to_thread(vectors.indexed_head) != head or text_head != head
        # **OFF THE LOOP, and this was the last place it was not.** `VectorIndex.search`
        # embeds the QUERY, so on a cold cache it loads the model — the single most
        # expensive thing this service does. Calling it directly here put that cost back
        # on the event loop, so every other request waited behind it: the exact failure
        # S32 exists to remove, left in the one path that runs once per hybrid read. The
        # original S32 acceptance test missed it because it measured a *cold start* on
        # the write path and never issued a hybrid read (S33).
        vector_hits = await asyncio.to_thread(
            vectors.search,
            query,
            project=project,
            # The same filters the text half got, so a vector-only hit can no longer
            # violate --status/--type/--tag (S32).
            status=params.get("status"),
            note_type=params.get("type"),
            tag=params.get("tag"),
            limit=limit,
        )
        if lagging:
            live_ids = await asyncio.to_thread(snapshot, known_ids, repo)
            vector_hits = [hit for hit in vector_hits if (hit.get("project"), hit.get("id")) in live_ids]
        hits = merge_hits(text_hits, vector_hits, limit)
        # `merge_hits` copies each hit, so the text half's relaxation marker survives
        # the fusion and can be reported here too (S32).
        relaxed = any(hit.get("relaxed") for hit in hits)
        for hit in hits:
            hit.pop("chunk_key", None)
            hit["revision"] = head
            if not hit.get("summary"):
                hit["summary"] = _summary_of(repo, hit.get("project"), hit["id"])
        payload = {"hits": hits, "mode": "hybrid", "revision": head,
                   "queue_pending": await asyncio.to_thread(write_queue.pending_count, project),
                   "index_lag": lagging}
        if relaxed:
            payload["relaxed"] = True
        if lagging:
            payload["index_lag"] = True
        return JSONResponse(payload)

    async def index_status(request: Request) -> JSONResponse:
        payload: dict[str, Any] = {
            "text": {"present": index.indexed_head() is not None,
                     "indexed_head": index.indexed_head(),
                     "age": (time.time() - index.indexed_at()) if index.indexed_at() else None},
            "head": repo.head(),
            # Visibility for the background pass (S32). Without this, "the index has not
            # caught up yet" and "the index is failing to build" look identical from
            # outside — and a caller cannot tell a slow sync from a broken one.
            "reindex": dict(reindexer.stats),
        }
        payload["hybrid"] = (
            vectors.status()
            if vectors is not None
            else {"available": False, "kind": "text", "reason": "the `embed` extra is not installed"}
        )
        payload["kind"] = "hybrid" if vectors is not None else "text"
        return JSONResponse(payload)

    async def index_rebuild(request: Request) -> JSONResponse:
        if vectors is None:
            raise MemError(
                "internal_error", "the semantic index is not installed (the `embed` extra)"
            )
        # Explicit and admin-facing, so it stays synchronous from the caller's point of
        # view — but off the loop, so a cold model load cannot stall `/health` (S32).
        def _rebuild() -> tuple[str | None, dict[str, int]]:
            with repo.pinned():
                head = repo.head()
                index.rebuild(repo, head)
                metas = repo.notes()
                sources = {meta.path: note_source(repo, meta.path) for meta in metas}
            return head, vectors.rebuild(metas, sources.__getitem__, head)

        head, stats = await asyncio.to_thread(_rebuild)
        return JSONResponse({"rebuilt": True, "head": head, **stats})

    async def outline(request: Request) -> JSONResponse:
        """The cheap rung: headings only, never a body.

        This exists so ``mem read --outline`` does not have to download the whole
        note through ``/raw`` -- which is exactly what made ``--outline`` more
        expensive than the full read it was meant to undercut.
        """
        params = request.query_params
        project = params.get("project")
        if not project:
            raise MemError("invalid_request", "project is required")
        validate_project(project)
        note_id = validate_id(params.get("id") or "")
        revision = params.get("revision")
        if not revision:
            queued = await asyncio.to_thread(write_queue.state, project, note_id)
            if queued:
                rel = await asyncio.to_thread(repo.public_path, project, note_id,
                                              None, allow_missing=True)
                queued.update({"id": note_id, "project": project,
                               "committed_revision": await asyncio.to_thread(repo.path_revision, rel)})
                return JSONResponse(queued)
        def _outline() -> tuple[dict[str, Any], int]:
            rel = repo.public_path(project, note_id, revision)
            try:
                raw = repo.note_bytes(rel, revision)
            except MemError as exc:
                if exc.code == "not_found":
                    raise MemError("target_moved", f"no note at id {project}/{note_id}") from exc
                raise
            text = raw.decode("utf-8", "replace")
            pointer = pointer_record(text) if rel.endswith(".md") else None
            if pointer:
                return {"id": note_id, "project": project, "kind": pointer.kind,
                        "media_type": "text/plain", "size": len(raw),
                        "sha256": repo.sha256_bytes(raw), "revision": revision or repo.head()}, len(raw)
            _, body = fm.parse(text) if rel.endswith(".md") else ({}, text)
            headings = _headings(body)
            if not headings:
                raise MemError("schema_invalid", f"{note_id} has no headings to outline")
            return {"id": note_id, "project": project,
                    "revision": revision or repo.head(), "sha256": repo.sha256_bytes(raw),
                    "line_count": body.count("\n") + 1, "headings": headings}, len(raw)

        record, size = await asyncio.to_thread(snapshot, _outline)
        if "kind" in record:
            return JSONResponse(record)
        _log_read(
            settings,
            {"id": note_id, "project": project, "bytes": size, "truncated": True},
            mode="outline",
        )
        return JSONResponse(record)

    async def devices(request: Request) -> JSONResponse:
        """The device log -- the mitigation MEM-SERVER.md §3.5 relies on."""
        return JSONResponse(registry.list_devices())

    async def retire_device(request: Request) -> JSONResponse:
        payload = await _json_body(request)
        device_id = str(payload.get("device_id") or "").strip()
        if not device_id:
            raise MemError("invalid_request", "device_id is required")
        caller = str(request.state.device.get("device_id") or "")
        if device_id != caller:
            raise MemError(
                "unauthorized",
                "a device may only retire itself; fleet administration is operator-only",
                {"caller": caller, "requested": device_id},
            )
        if not registry.retire(device_id):
            raise MemError("not_found", f"no active device {device_id!r}")
        return JSONResponse({"retired": device_id})

    def _add_commit(
        *, device_id: str, project: str, rel: str, note_id: str, injected: bytes,
        message: str, key: str | None, if_match: str | None,
        work: MemoryRepo | None = None,
    ) -> dict[str, Any]:
        """Run the complete locked add transaction in a worker thread.

        The lock and Git subprocesses are blocking primitives. Running this whole
        transaction through ``to_thread`` lets another request reach the mutex and
        receive its typed ``lock_timeout`` instead of starving the ASGI event loop.
        """
        work = work or repo
        with lock.hold(device_id):
            # A stale retry key must not turn a content-to-pointer downgrade into
            # a replay of an older pointer write.
            existing = work.public_path(project, note_id, allow_missing=True)
            prior_target = work.root / existing
            prior_bytes = prior_target.read_bytes() if prior_target.is_file() else None
            was_pointer = prior_bytes is not None and existing.endswith(".md") and pointer_record(prior_bytes.decode("utf-8", "replace")) is not None
            becoming_pointer = rel.endswith(".md") and pointer_record(injected.decode("utf-8", "replace")) is not None
            if prior_bytes is not None and not was_pointer and becoming_pointer:
                raise MemError("invalid_request", f"{note_id} stores content; remove it before adding a pointer")
            if key:
                cached = store.get(key)
                if cached is not None and prior_bytes is not None and existing == rel and was_pointer == becoming_pointer:
                    return _replay_result(cached)

            target = work.root / rel
            if existing != rel and prior_bytes is not None and not was_pointer:
                raise MemError("invalid_request", f"{note_id} already exists as {existing}; remove it before changing storage kind")
            previous = target.read_bytes() if target.is_file() else None
            current = work.sha256_bytes(prior_bytes) if prior_bytes is not None else None
            if if_match is not None and if_match != current:
                raise MemError(
                    "stale_revision",
                    f"{note_id} has moved since {if_match[:12]}",
                    {"expected": if_match, "actual": current},
                )
            try:
                if existing != rel and prior_bytes is not None:
                    work.remove_note(existing)
                work.write_note(rel, injected)
                committed = work.commit_paths(message, [rel, existing] if existing != rel and prior_bytes is not None else [rel], device_id=device_id)
                if not committed["changed"] and not work.is_tracked(rel):
                    raise MemError(
                        "internal_error",
                        f"{rel} was written but is not tracked by git",
                        {"path": rel, "hint": "an ignore rule may cover this path"},
                    )
            except Exception:
                work.rollback_path(rel, restore=previous)
                if existing != rel and prior_bytes is not None:
                    work.rollback_path(existing, restore=prior_bytes)
                raise

            result = {
                "id": note_id,
                "project": project,
                "path": rel,
                "sha256": work.sha256_bytes(injected),
                "bytes": len(injected),
                "head": committed["head"],
                "message": message,
                "changed": committed["changed"],
                "idempotent_replay": False,
            }
            if key:
                store.put(key, result)
            return result

    def _preflight_add(project: str, note_id: str, rel: str,
                       injected: bytes, if_match: str | None = None) -> None:
        with repo.pinned():
            existing = repo.public_path(project, note_id, allow_missing=True)
            old = repo.note_bytes(existing) if existing in repo.list_note_paths() else None
            was_pointer = (old is not None and existing.endswith(".md") and
                           pointer_record(old.decode("utf-8", "replace")) is not None)
            becoming_pointer = (rel.endswith(".md") and
                                pointer_record(injected.decode("utf-8", "replace")) is not None)
            if old is not None and not was_pointer and becoming_pointer:
                raise MemError("invalid_request", f"{note_id} stores content; remove it before adding a pointer")
            if old is not None and existing != rel and not was_pointer:
                raise MemError("invalid_request", f"{note_id} already exists as {existing}")
            actual = repo.sha256_bytes(old) if old is not None else None
            if if_match is not None and if_match != actual:
                raise MemError("stale_revision", f"{note_id} has moved since {if_match[:12]}",
                               {"expected": if_match, "actual": actual})

    def _preflight_remove(project: str, note_id: str, if_match: str) -> None:
        with repo.pinned():
            rel = repo.public_path(project, note_id, allow_missing=True)
            old = repo.note_bytes(rel) if rel in repo.list_note_paths() else None
            actual = repo.sha256_bytes(old) if old is not None else None
            if actual != if_match:
                raise MemError("stale_revision", f"{note_id} has moved since {if_match[:12]}",
                               {"expected": if_match, "actual": actual})

    def _rm_commit(
        *, device_id: str, project: str, rel: str, note_id: str,
        message: str, key: str | None, if_match: str | None,
        work: MemoryRepo | None = None,
    ) -> dict[str, Any]:
        """Run the complete locked remove transaction in a worker thread."""
        work = work or repo
        target = work.root / rel
        with lock.hold(device_id):
            if key:
                cached = store.get(key)
                if cached is not None:
                    return _replay_result(cached)

            existed = target.is_file()
            if not existed and if_match is not None:
                raise MemError(
                    "stale_revision", f"{note_id} does not exist",
                    {"expected": if_match, "actual": None},
                )
            original = target.read_bytes() if existed else None
            if existed and if_match is not None:
                current = work.sha256_bytes(original)
                if if_match != current:
                    raise MemError(
                        "stale_revision",
                        f"{note_id} has moved since {if_match[:12]}",
                        {"expected": if_match, "actual": current},
                    )
            try:
                work.remove_note(rel)
                committed = work.commit_paths(message, [rel], device_id=device_id)
            except Exception:
                work.rollback_path(rel, restore=original)
                raise

            result = {
                "id": note_id,
                "project": project,
                "path": rel,
                "removed": existed,
                "already_absent": not existed,
                "head": committed["head"],
                "message": message if committed["changed"] else None,
                "changed": committed["changed"],
                "idempotent_replay": False,
            }
            if key:
                store.put(key, result)
            return result

    async def add(request: Request) -> JSONResponse:
        """Write **and version** one note, as a single atomic act (S23/S28).

        `git add` and `git commit` both happen inside the lock, the commit is
        path-limited, and any failure rolls the tree back — so a write can never
        leave the repository unclean (S26).
        """
        device = request.state.device
        form = await request.form()
        project = validate_project(str(form.get("project", "")))
        note_id = validate_id(str(form.get("path", "")))

        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            raise MemError("invalid_request", "missing 'file' part")
        content: bytes = await upload.read()
        if len(content) > MAX_UPLOAD_BYTES:
            raise MemError("invalid_request", "file is too large for one request")

        kind = str(form.get("kind") or "note")
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MemError("schema_invalid", "tracked records must be UTF-8 text") from exc
        if kind == "pointer":
            pointer = pointer_record(text)
            if pointer is None:
                raise MemError("invalid_request", "pointer uploads must contain exactly one canonical pointer line")
            if not registry.has_alias(pointer.alias):
                raise MemError("invalid_request", f"pointer alias {pointer.alias!r} is not enrolled")
            rel = note_relpath(project, note_id)
            injected = (pointer.canonical + "\n").encode("utf-8")
        elif kind == "note":
            if not str(getattr(upload, "filename", "")).lower().endswith(".md"):
                raise MemError("invalid_request", "kind=note requires a .md source filename")
            if pointer_record(text):
                raise MemError("invalid_request", "pointer records require kind=pointer")
            rel = note_relpath(project, note_id)
            data, body = fm.parse(text)
            fm.validate(data, note_id)  # exit 4 before anything is written
            origin_path = form.get("origin_path")
            injected = fm.inject(
                data, body,
                {"origin": {"device": device["device_id"], "path": str(origin_path) if origin_path else note_id}},
            ).encode("utf-8")
        elif kind == "text":
            extension = Path(str(getattr(upload, "filename", ""))).suffix.lower()
            if extension == ".md" or extension not in settings.inline_extensions:
                raise MemError("invalid_request", f"{extension or 'extensionless file'} is not enabled for inline text")
            if not note_id.endswith(extension):
                raise MemError("invalid_request", f"text id must retain the {extension} extension")
            if "\x00" in text:
                raise MemError("schema_invalid", "inline text cannot contain NUL bytes")
            rel = text_relpath(project, note_id)
            injected = content
        else:
            raise MemError("invalid_request", "kind must be 'note', 'text', or 'pointer'")

        message = str(form.get("message") or "").strip() or f"mem: add {note_id}"
        if len(message) > 200:
            raise MemError("invalid_request", "message is too long (max 200)")

        key = request.headers.get("idempotency-key")
        if_match = request.headers.get("if-match")
        await asyncio.to_thread(_preflight_add, project, note_id, rel, injected)
        prior = await asyncio.to_thread(write_queue.lookup, project, device["device_id"], key)
        if prior:
            return JSONResponse(prior, status_code=202)
        if if_match:
            await asyncio.to_thread(_preflight_add, project, note_id, rel, injected, if_match)
        manifest = {"file_count": 1, "device_id": device["device_id"],
                    "project": project, "id": note_id, "rel": rel,
                    "message": message, "if_match": if_match, "kind": kind}
        result = await asyncio.to_thread(
            write_queue.submit, project=project, kind="add", targets=[note_id],
            manifest=manifest, files=[injected], idem=key,
        )
        queue_wake.set()
        return JSONResponse(result, status_code=202)

    async def _add_many_inner(request: Request, temp_root: Path) -> JSONResponse:
        """Validate a set of uploads and commit all changed paths together."""
        device = request.state.device
        form = await request.form()
        project = validate_project(str(form.get("project", "")))
        message = str(form.get("message") or "").strip()
        if not message or len(message) > 200:
            raise MemError("invalid_request", "batch message must be 1-200 characters")
        try:
            metadata = json.loads(str(form.get("items", "")))
        except (ValueError, TypeError) as exc:
            raise MemError("invalid_request", "items must be a JSON array") from exc
        if not isinstance(metadata, list) or not 2 <= len(metadata) <= 1000:
            raise MemError("invalid_request", "batch needs 2-1000 items")
        prepared: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        total_bytes = 0
        for index, item in enumerate(metadata):
            if not isinstance(item, dict):
                raise MemError("invalid_request", f"item {index} must be an object")
            note_id = validate_id(str(item.get("id") or ""))
            if note_id in seen_ids:
                raise MemError("invalid_request", f"duplicate id in batch: {note_id}")
            seen_ids.add(note_id)
            upload = form.get(f"file{index}")
            if upload is None or not hasattr(upload, "read"):
                raise MemError("invalid_request", f"missing file{index}")
            content: bytes = await upload.read()
            total_bytes += len(content)
            if len(content) > MAX_UPLOAD_BYTES or total_bytes > 256 * 1024 * 1024:
                raise MemError("invalid_request", "batch upload exceeds size limit")
            expected_sha = item.get("sha256")
            if expected_sha and hashlib.sha256(content).hexdigest() != expected_sha:
                raise MemError("invalid_request", f"{note_id} changed after client preflight")
            kind = str(item.get("kind") or "")
            filename = str(getattr(upload, "filename", ""))
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise MemError("schema_invalid", f"{note_id} is not UTF-8") from exc
            if kind == "pointer":
                pointer = pointer_record(text)
                if pointer is None or not registry.has_alias(pointer.alias):
                    raise MemError("invalid_request", f"invalid pointer for {note_id}")
                rel = note_relpath(project, note_id)
                injected = (pointer.canonical + "\n").encode("utf-8")
            elif kind == "note":
                if not filename.lower().endswith(".md") or pointer_record(text):
                    raise MemError("invalid_request", f"{note_id} requires a Markdown source")
                rel = note_relpath(project, note_id)
                data, body = fm.parse(text)
                fm.validate(data, note_id)
                injected = fm.inject(data, body, {"origin": {
                    "device": device["device_id"],
                    "path": str(item.get("origin_path") or note_id),
                }}).encode("utf-8")
            elif kind == "text":
                extension = Path(filename).suffix.lower()
                if extension == ".md" or extension not in settings.inline_extensions or not note_id.endswith(extension) or "\x00" in text:
                    raise MemError("invalid_request", f"{note_id} is not permitted inline text")
                rel = text_relpath(project, note_id)
                injected = content
            else:
                raise MemError("invalid_request", f"invalid kind for {note_id}")
            content_path = temp_root / str(index)
            await asyncio.to_thread(content_path.write_bytes, injected)
            prepared.append({"id": note_id, "rel": rel, "kind": kind, "content": content_path})

        key = request.headers.get("idempotency-key")
        for item in prepared:
            await asyncio.to_thread(_preflight_add, project, item["id"], item["rel"],
                                    await asyncio.to_thread(item["content"].read_bytes))

        manifest = {"file_count": len(prepared), "device_id": device["device_id"],
                    "project": project, "message": message,
                    "items": [{k: v for k, v in item.items() if k != "content"}
                              for item in prepared]}
        result = await asyncio.to_thread(
            write_queue.submit, project=project, kind="add-many",
            targets=[item["id"] for item in prepared], manifest=manifest,
            files=[item["content"] for item in prepared], idem=key,
        )
        queue_wake.set()
        return JSONResponse(result, status_code=202)

    async def add_many(request: Request) -> JSONResponse:
        with tempfile.TemporaryDirectory(prefix="upload-", dir=settings.state_dir) as root:
            return await _add_many_inner(request, Path(root))

    async def rm(request: Request) -> JSONResponse:
        """Remove **and version** one note, as a single atomic act (S23/S28).

        Idempotent by **rule**, not only by key (S25): an absent id with no
        precondition is already the desired end state — it succeeds and creates no
        commit. With `If-Match`, an absent note is `stale_revision`.
        """
        device = request.state.device
        payload = await _json_body(request)
        project = validate_project(str(payload.get("project") or ""))
        note_id = validate_id(str(payload.get("id") or ""))
        rel = repo.public_path(project, note_id, allow_missing=True)

        message = str(payload.get("message") or "").strip() or f"mem: rm {note_id}"
        if len(message) > 200:
            raise MemError("invalid_request", "message is too long (max 200)")

        key = request.headers.get("idempotency-key")
        if_match = request.headers.get("if-match")
        prior = await asyncio.to_thread(write_queue.lookup, project, device["device_id"], key)
        if prior:
            return JSONResponse(prior, status_code=202)
        if if_match:
            await asyncio.to_thread(_preflight_remove, project, note_id, if_match)
        manifest = {"file_count": 0, "device_id": device["device_id"],
                    "project": project, "id": note_id, "rel": rel,
                    "message": message, "if_match": if_match}
        result = await asyncio.to_thread(
            write_queue.submit, project=project, kind="rm", targets=[note_id],
            manifest=manifest, files=[], idem=key,
        )
        queue_wake.set()
        return JSONResponse(result, status_code=202)

    async def mv(request: Request) -> JSONResponse:
        device = request.state.device
        payload = await _json_body(request)
        project = validate_project(str(payload.get("project") or ""))
        raw_source = str(payload.get("source") or "")
        raw_destination = str(payload.get("destination") or "")
        folder = raw_source.endswith("/")
        if folder != raw_destination.endswith("/"):
            raise MemError("invalid_request", "folder moves require trailing '/' on both IDs")
        source = validate_id(raw_source.rstrip("/"))
        destination = validate_id(raw_destination.rstrip("/"))
        dry_run = payload.get("dry_run") is True
        message = str(payload.get("message") or "").strip() or f"mem: mv {raw_source} {raw_destination}"
        if len(message) > 200:
            raise MemError("invalid_request", "message is too long (max 200)")
        key = request.headers.get("idempotency-key")
        expected_head = request.headers.get("if-match")

        if not dry_run:
            prior = await asyncio.to_thread(write_queue.lookup, project, device["device_id"], key)
            if prior:
                return JSONResponse(prior, status_code=202)

        # BUG(low): the write branch of this helper is unreachable.
        # Why valid: `_move()` is called only under `if dry_run:` further down, so the
        # non-dry-run path below -- the lock, the re-plan, `commit_move`, and both
        # `store.get` lookups -- never executes. Real moves go through the queue instead.
        # Fix hint: delete the dead branch (the two `dry_run` guards then collapse into
        # one), or route dry-run previews through the same planning helper the queue uses
        # so a preview and a commit cannot disagree.
        def _move() -> dict[str, Any]:
            if key and not dry_run:
                cached = store.get(key)
                if cached is not None:
                    return _replay_result(cached)
            if dry_run:
                plan = snapshot(plan_move, repo, project, source, destination, folder=folder)
                if expected_head and plan.head != expected_head:
                    raise MemError("stale_revision", "repository head changed",
                                   {"expected": expected_head, "actual": plan.head})
                return {**plan.response(), "dry_run": True}
            with lock.hold(device["device_id"]):
                if key:
                    cached = store.get(key)
                    if cached is not None:
                        return _replay_result(cached)
                plan = snapshot(plan_move, repo, project, source, destination, folder=folder)
                if expected_head and plan.head != expected_head:
                    raise MemError("stale_revision", "repository head changed",
                                   {"expected": expected_head, "actual": plan.head})
                result = commit_move(repo, settings.state_dir, plan,
                                     message=message, device_id=device["device_id"],
                                     key=key, store=store)
                return result

        if dry_run:
            return JSONResponse(await asyncio.to_thread(_move))
        plan = await asyncio.to_thread(snapshot, plan_move, repo, project, source,
                                       destination, folder=folder)
        if expected_head and plan.head != expected_head:
            raise MemError("stale_revision", "repository head changed",
                           {"expected": expected_head, "actual": plan.head})
        targets = sorted(set(plan.moved) | set(plan.moved.values()) | set(plan.referenced))
        manifest = {"file_count": 0, "device_id": device["device_id"],
                    "project": project, "source": source,
                    "destination": destination, "folder": folder,
                    "message": message, "expected_head": expected_head}
        result = await asyncio.to_thread(
            write_queue.submit, project=project, kind="mv", targets=targets,
            manifest=manifest, files=[], idem=key, require_empty_project=True,
        )
        queue_wake.set()
        return JSONResponse(result, status_code=202)

    routes = [
        Route("/health", health, methods=["GET"]),
        Route("/v1/enroll", enroll, methods=["POST"]),
        Route("/v1/status", status, methods=["GET"]),
        Route("/v1/queue", queue_progress, methods=["GET"]),
        Route("/v1/policy", policy, methods=["GET"]),
        Route("/v1/projects", projects, methods=["GET"]),
        Route("/v1/notes", notes, methods=["GET"]),
        Route("/v1/manifest", manifest, methods=["GET"]),
        Route("/v1/notes/{id:path}", note, methods=["GET"]),
        Route("/v1/search", search, methods=["GET"]),
        Route("/v1/index/status", index_status, methods=["GET"]),
        Route("/v1/index/rebuild", index_rebuild, methods=["POST"]),
        Route("/v1/outline", outline, methods=["GET"]),
        Route("/v1/devices", devices, methods=["GET"]),
        Route("/v1/devices/retire", retire_device, methods=["POST"]),
        Route("/raw/{project}/{id:path}", raw, methods=["GET"]),
        Route("/v1/add", add, methods=["POST"]),
        Route("/v1/add-many", add_many, methods=["POST"]),
        Route("/v1/rm", rm, methods=["POST"]),
        Route("/v1/mv", mv, methods=["POST"]),
    ]

    middleware = [
        Middleware(
            _AuthMiddleware,
            registry=registry,
            base_url=settings.base_url,
            settings=settings,
        )
    ]
    def _commit_queued_batch(work: MemoryRepo, manifest: dict[str, Any], files: list[bytes]) -> dict[str, Any]:
        project = manifest["project"]
        device_id = manifest["device_id"]
        message = manifest["message"]
        items = [{**row, "content": files[index]}
                 for index, row in enumerate(manifest["items"])]
        with lock.hold(device_id):
            previous: dict[str, bytes | None] = {}
            for item in items:
                existing = work.public_path(project, item["id"], allow_missing=True)
                old_path = work.root / existing
                old = old_path.read_bytes() if old_path.is_file() else None
                was_pointer = (old is not None and existing.endswith(".md")
                               and pointer_record(old.decode("utf-8", "replace")) is not None)
                if old is not None and not was_pointer and item["kind"] == "pointer":
                    raise MemError("invalid_request", f"{item['id']} stores content; remove it before adding a pointer")
                if old is not None and existing != item["rel"] and not was_pointer:
                    raise MemError("invalid_request", f"{item['id']} already exists as {existing}")
                item["old_rel"] = existing if old is not None else None
                for rel in {item["rel"], *([existing] if old is not None else [])}:
                    if rel not in previous:
                        target = work.root / rel
                        previous[rel] = target.read_bytes() if target.is_file() else None
            try:
                for item in items:
                    if item["old_rel"] and item["old_rel"] != item["rel"]:
                        work.remove_note(item["old_rel"])
                    work.write_note(item["rel"], item["content"])
                committed = work.commit_paths(message, list(previous), device_id=device_id)
                for item in items:
                    if not work.is_tracked(item["rel"]):
                        raise MemError("internal_error", f"{item['rel']} was not tracked by git")
            except Exception:
                for rel, old in previous.items():
                    work.rollback_path(rel, restore=old)
                raise
            return {"project": project, "head": committed["head"],
                    "changed": committed["changed"], "message": message,
                    "idempotent_replay": False,
                    "results": [{"id": item["id"], "path": item["rel"],
                                 "kind": item["kind"],
                                 "sha256": work.sha256_bytes(item["content"]),
                                 "bytes": len(item["content"]),
                                 "content_stored": item["kind"] != "pointer",
                                 "changed": item["rel"] in committed["committed"] or
                                 bool(item["old_rel"] and item["old_rel"] in committed["committed"])}
                                for item in items]}

    def _execute_queued(job: dict[str, Any], work: MemoryRepo) -> dict[str, Any]:
        manifest, files = write_queue.payload(job["id"])
        kind = job["kind"]
        if kind == "add":
            result = _add_commit(
                device_id=manifest["device_id"], project=manifest["project"],
                rel=manifest["rel"], note_id=manifest["id"], injected=files[0],
                message=manifest["message"], key=None,
                if_match=manifest["if_match"],
                work=work,
            )
            return {**result, "storage": "pointer" if manifest["kind"] == "pointer" else "content",
                    "content_stored": manifest["kind"] != "pointer",
                    "target_tracked": manifest["kind"] != "pointer",
                    "record_tracked": True}
        if kind == "add-many":
            return _commit_queued_batch(work, manifest, files)
        if kind == "rm":
            return _rm_commit(
                device_id=manifest["device_id"], project=manifest["project"],
                rel=manifest["rel"], note_id=manifest["id"],
                message=manifest["message"], key=None,
                if_match=manifest["if_match"],
                work=work,
            )
        if kind == "mv":
            with lock.hold(manifest["device_id"]):
                plan = plan_move(work, manifest["project"],
                                 manifest["source"], manifest["destination"],
                                 folder=manifest["folder"])
                if manifest["expected_head"] and plan.head != manifest["expected_head"]:
                    raise MemError("stale_revision", "repository head changed",
                                   {"expected": manifest["expected_head"], "actual": plan.head})
                return commit_move(work, settings.state_dir, plan,
                                   message=manifest["message"],
                                   device_id=manifest["device_id"])
        raise MemError("internal_error", f"unknown queued operation {kind}")

    queue_stopping = False

    def _head_contains(revision: str) -> bool:
        """True when *revision* is already an ancestor of the served HEAD."""
        return bool(revision) and revision in repo.git(
            "rev-list", "HEAD", check=False
        ).splitlines()

    def _publish_queued(job: dict[str, Any]) -> dict[str, Any]:
        receipt = job["id"]
        if job.get("prepared_head") and _head_contains(job["prepared_head"]):
            return json.loads(job["prepared_result"])
        base = repo.head()
        ref = repo.git("symbolic-ref", "HEAD").strip()
        # Keep the worktree path short: Git for Windows otherwise interprets
        # <revision>:<path> as an overlong filename during blob reads.
        stage_root = Path(tempfile.gettempdir()) / "sparkmem-staging"
        stage_root.mkdir(parents=True, exist_ok=True)
        stage = stage_root / ("s-" + uuid.uuid4().hex[:12])
        # BUG(medium): each job builds a fresh MemoryRepo and pays a full worktree
        # checkout plus a full reset of the served tree -- O(corpus) work per write.
        # Why valid: a new instance starts with an empty `_paths_cache`, so the first
        # `public_path` call re-runs a `git show` per tracked path (see `list_note_paths`
        # in repo.py); `worktree add` materialises the whole tree, and the `reset --hard`
        # below rewrites the whole checkout -- for every job, including no-op ones.
        # `_paths_cache` retains a single revision, so every published commit invalidates
        # it again.
        # Fix hint: reuse one persistent staging worktree (reset it between jobs instead
        # of remove/add), share a revision-keyed path cache, and consider batching
        # consecutive jobs into one commit.
        repo.git("worktree", "add", "--detach", str(stage), base)
        work = MemoryRepo(stage, settings.inline_extensions)
        try:
            result = _execute_queued(job, work)
            head = work.head()
            write_queue.prepared(receipt, head, result)
            if head != base:
                try:
                    repo.git("update-ref", ref, head, base)
                except MemError:
                    # `update-ref` may have completed before its process timed out.
                    # Once the prepared commit is reachable, the write is shipped and
                    # must be settled as success, not recorded as failed.
                    if not _head_contains(head):
                        raise
            # The checkout is a mirror, not the read source. Refresh it after
            # publication; a crash is repaired at startup. A refresh failure must
            # never turn an already-published write into a failed job.
            try:
                repo.git("reset", "--hard", head, check=False)
            except MemError:
                logger.warning(
                    "mirror refresh failed after publishing %s at %s; startup will repair it",
                    receipt,
                    head,
                    exc_info=True,
                )
            return result
        finally:
            try:
                repo.git("worktree", "remove", "--force", str(stage), check=False)
            except MemError:
                logger.warning(
                    "could not remove staging worktree %s; leaving it for cleanup",
                    stage,
                    exc_info=True,
                )
            if stage.resolve().is_relative_to(stage_root.resolve()):
                shutil.rmtree(stage, ignore_errors=True)

    async def _queue_loop() -> None:
        """Process jobs until shutdown, surviving transient queue/storage failures.

        The whole iteration is supervised. A failure in `next_job`, in publishing,
        or in recording the terminal state must not end the only publisher: the
        API would keep accepting writes into a queue nothing drains.
        """
        nonlocal queue_stopping
        while not queue_stopping:
            try:
                job = await asyncio.to_thread(write_queue.next_job)
                if job is None:
                    queue_wake.clear()
                    job = await asyncio.to_thread(write_queue.next_job)
                    if job is None:
                        await queue_wake.wait()
                        continue
                try:
                    result = await asyncio.to_thread(_publish_queued, job)
                except MemError as exc:
                    await asyncio.to_thread(
                        write_queue.finish, job["id"], error=exc.to_dict()["error"]
                    )
                except Exception as exc:  # noqa: BLE001 - keep the queue worker alive
                    failure = MemError("internal_error", f"queued write failed: {exc}")
                    await asyncio.to_thread(
                        write_queue.finish, job["id"], error=failure.to_dict()["error"]
                    )
                else:
                    await asyncio.to_thread(write_queue.finish, job["id"], result=result)
                    reindexer.request()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a dead publisher is worse than a retry
                logger.exception("write-queue worker failed; retrying")
                await asyncio.sleep(1)

    @asynccontextmanager
    async def lifespan(_app):
        nonlocal queue_stopping
        await asyncio.to_thread(write_queue.claim_worker)
        # Git is authoritative; repair the checkout only after owning the queue.
        try:
            await asyncio.to_thread(repo.git, "reset", "--hard", "HEAD")
            reindexer.start()
        except Exception:
            await asyncio.to_thread(write_queue.release_worker)
            raise
        queue_stopping = False
        queue_wake.set()
        queue_task = asyncio.create_task(_queue_loop(), name="mem-write-queue")
        # One pass at startup so a restart converges instead of serving a stale index
        # until the next write happens to trigger one (S32).
        reindexer.request()
        try:
            yield
        finally:
            queue_stopping = True
            queue_wake.set()
            try:
                await queue_task
            finally:
                try:
                    await reindexer.stop()
                finally:
                    await asyncio.to_thread(write_queue.release_worker)

    app = Starlette(routes=routes, middleware=middleware, lifespan=lifespan)

    app.state.settings = settings
    app.state.repo = repo
    app.state.registry = registry
    app.state.lock = lock
    app.state.index = index
    app.state.vectors = vectors
    app.state.reindexer = reindexer
    app.state.write_queue = write_queue
    _install_error_handler(app)
    return app


def _replay_result(cached: dict[str, Any]) -> dict[str, Any]:
    """A stored write result, marked as a replay.

    ``changed`` arrived after the first deployment (``MEM-RESOLUTIONS.md`` S30) and
    the store is a file that survives an upgrade, so an entry written by the older
    daemon has no ``changed`` key at all. Only *successful* writes were ever stored,
    so a missing value is ``True`` -- with one exception: the ``rm`` already-absent
    branch stored ``already_absent: true`` and made no commit, so its ``changed``
    is ``False``. Backfilling here keeps the documented response shape
    (``MEM-SERVER.md`` §6) true across the upgrade, not only for new writes.
    """
    if "changed" in cached:
        return {**cached, "idempotent_replay": True}
    return {
        **cached,
        "changed": not cached.get("already_absent", False),
        "idempotent_replay": True,
    }


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise MemError("invalid_request", "body must be JSON") from exc
    if not isinstance(payload, dict):
        raise MemError("invalid_request", "body must be a JSON object")
    return payload


def _load_or_create_secret(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    secret = secrets.token_urlsafe(24)
    path.write_text(secret + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:  # pragma: no cover - Windows
        pass
    return secret


def _log_read(settings: Settings, record: dict[str, Any], *, mode: str | None = None) -> None:
    """Per-read instrumentation: size plus inline-or-handoff.

    ``read.max_bytes`` is a tunable, and without this log there is no evidence to
    tune it with (``MEM-SERVER.md`` §4).
    """
    entry = {
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        "id": record.get("id"),
        "project": record.get("project"),
        "size": record.get("bytes"),
        "mode": mode or ("handoff" if record.get("truncated") else "inline"),
    }
    try:
        settings.state_dir.mkdir(parents=True, exist_ok=True)
        with (settings.state_dir / "reads.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def _install_error_handler(app: Starlette) -> None:
    async def handle_mem(request: Request, exc: Exception) -> Response:
        assert isinstance(exc, MemError)
        return _error_response(exc)

    async def handle_any(request: Request, exc: Exception) -> Response:
        # Every failure must leave as the §6 JSON envelope. An unhandled
        # exception previously escaped as `500 text/plain`, which an agent
        # cannot parse (invariant C6).
        detail = f"{exc.__class__.__name__}: {exc}" if str(exc) else exc.__class__.__name__
        return JSONResponse(
            {"error": {"code": "internal_error", "message": detail, "detail": {}}},
            status_code=500,
        )

    app.add_exception_handler(MemError, handle_mem)
    app.add_exception_handler(Exception, handle_any)


class _AuthMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app,
        registry: DeviceRegistry,
        base_url: str = "",
        settings: Settings | None = None,
    ) -> None:
        super().__init__(app)
        self.registry = registry
        self.base_url = base_url
        self.settings = settings

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in OPEN_PATHS:
            return await call_next(request)
        # A signed link is a credential in its own right (S31): read-only, anonymous,
        # bounded by `exp`, and covering the revision. Checked BEFORE the bearer path,
        # which is left completely unchanged — so a valid token always works and a bad
        # signature falls through to today's 401 rather than a new failure mode.
        if (
            path.startswith("/raw/")
            and self.settings is not None
            and _capability_request(request, self.settings) is not None
        ):
            request.state.capability = True
            return await call_next(request)
        token = _bearer(request)
        device = self.registry.verify(token or "")
        if device is None:
            return _error_response(
                MemError(
                    "unauthorized",
                    "missing or invalid device token",
                    {"hint": "run `mem login`"},
                )
            )
        request.state.device = device
        self.registry.touch(device["device_id"], _client_ip(request))
        return await call_next(request)
