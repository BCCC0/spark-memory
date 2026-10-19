# BUG(low): the module docstring documents the server-URL precedence as
# "credentials > env > project marker > config file > default", which is wrong.
# Why valid: config.load applies the project marker LAST (the find_marker block),
# after read_credentials and after the MEM_SERVER environment override, so the
# marker overrides credentials -- config.load's own docstring and INSTALL.md
# state the real order, so an agent reading this docstring predicts the wrong
# server.
# Fix hint: correct the docstring to "default < config file < env < credentials
# < project marker", matching config.load.
"""The ``mem`` command-line interface.

Everyday client commands use the standard library. The local Git summary
preflight imports PyYAML on demand; no CLI command imports Starlette.

Precedence for the server URL: credentials > env > project marker > config file >
default. Exit codes are ``MEM-CLI.md`` §4, and every failure travels as one so an
agent never parses prose (invariant C6).
"""

from __future__ import annotations

import argparse
import ast
import datetime as _dt
import getpass
import hashlib
import json
import os
import platform
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from . import config as cfg
from .client import DEFAULT_TIMEOUT, EMBED_TIMEOUT, Client
from .errors import MemError
from .device_home import default_alias, initialize_device_home
from .ids import RESERVED_PROJECT_NOTE, id_from_path, validate_id, validate_project
from .keys import add_key, rm_key, mv_key, repath_key
from .paths import resolve_origin
from . import hooks
from . import tracking
from .version import API_VERSION, __version__, build_commit

#: `mem flush` is the explicit barrier, but it must not hang forever when the
#: index cannot catch up. The Go client uses the same three-minute bound.
FLUSH_TIMEOUT = 180.0


class _Parser(argparse.ArgumentParser):
    """argparse exits 2 on a usage error, which collides with
    ``server_unreachable`` (exit 2). Usage errors get their own code (64).

    The failure travels as a ``MemError`` rather than ``SystemExit`` so it leaves
    through the same envelope as everything else: ``MEM-CLI.md`` §4 requires that
    usage errors still emit the JSON object on stderr, and prose is exactly what
    invariant C6 forbids an agent to parse.
    """

    def error(self, message: str):  # type: ignore[override]
        raise MemError(
            "usage_error",
            message,
            {"usage": self.format_usage().strip()},
        )


def _common_flags() -> argparse.ArgumentParser:
    """Flags accepted both before and after the subcommand.

    ``MEM-CLI.md`` writes them as ``mem login [--server <url>]`` — after the
    verb — so the subparsers must accept them too. ``SUPPRESS`` as the default
    stops an absent subcommand-level flag from clobbering the global value.
    """
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--human", action="store_true", default=argparse.SUPPRESS,
        help="human-readable output",
    )
    common.add_argument(
        "--server", default=argparse.SUPPRESS, help="override the server URL"
    )
    return common


def build_parser() -> _Parser:
    parser = _Parser(prog="mem", description="stateless project memory")
    parser.add_argument("--human", action="store_true", help="human-readable output")
    parser.add_argument("--server", help="override the server URL")
    parser.add_argument(
        "--version",
        action="version",
        version=f"mem {__version__} (python, commit {build_commit()})",
    )
    sub = parser.add_subparsers(dest="command")
    common = _common_flags()

    login = sub.add_parser("login", parents=[common], help="enroll this device")
    login.add_argument("--name", help="device display name (default: hostname)")
    login.add_argument("--alias", help="immutable device alias (default: hostname slug)")
    login.add_argument("--secret", help="enrollment secret (or $MEM_ENROLL_SECRET)")

    sub.add_parser("logout", parents=[common], help="delete the local token")

    init = sub.add_parser(
        "init", parents=[common], help="create a project and write .mem.toml"
    )
    init.add_argument("project", nargs="?", help="project name (default: directory name)")
    init.add_argument("--summary", help="one-line project summary")

    status = sub.add_parser("status", parents=[common], help="server-side status")
    status.add_argument("--origins", action="store_true", help="report this device's local origins")
    sub.add_parser("flush", parents=[common], help="wait for accepted writes and text indexing")
    sub.add_parser("projects", parents=[common], help="list projects")

    manifest = sub.add_parser("manifest", parents=[common], help="list files under a topic")
    manifest.add_argument("topic", nargs="?", default="")

    pull = sub.add_parser("pull", parents=[common], help="restore missing local files")
    pull.add_argument("ids", nargs="*", help="specific note ids (default: every missing file this device owns)")

    export = sub.add_parser("export", parents=[common], help="write a project into a clean directory")
    export.add_argument("project", help="project to export")
    export.add_argument("--dst", required=True, help="empty or missing destination directory")

    device = sub.add_parser("device", parents=[common], help="manage this device's private home")
    device_sub = device.add_subparsers(dest="device_command")
    device_sub.add_parser("init", parents=[common], help="create ~/.device/DEVICE.md safely")

    ls = sub.add_parser("ls", parents=[common], help="list notes")
    ls.add_argument("prefix", nargs="?")
    ls.add_argument("--status")
    ls.add_argument("--type")
    ls.add_argument("--tag")
    ls.add_argument("--since")
    ls.add_argument("--sort", default="updated", choices=["updated", "id"])
    ls.add_argument("--limit", type=int, default=50)
    ls.add_argument("--cursor")
    ls.add_argument(
        "--full", action="store_true",
        help="include extended metadata (never includes fetch URLs)",
    )

    read = sub.add_parser("read", parents=[common], help="read one or more notes")
    read.add_argument("ids", nargs="+", help="<id> or <id>@<revision>")
    read.add_argument("--revision")
    read.add_argument("--outline", action="store_true", help="headings only")
    read.add_argument(
        "--link-only",
        action="store_true",
        help="omit the whole note body and return a signed link plus freshness",
    )

    devices = sub.add_parser("devices", parents=[common], help="list or retire devices")
    devices.add_argument("--retire", metavar="DEVICE_ID", help="retire a device")

    add = sub.add_parser(
        "add", parents=[common], help="write and version files in one atomic commit"
    )
    add.add_argument("files", nargs="+", help="files to track")
    add.add_argument("--pointer", action="store_true", help="track all files as pointers without reading their bytes")
    add.add_argument("--as", dest="as_id", help="explicit note id")
    add.add_argument(
        "--no-origin",
        action="store_true",
        help="store content without recording a local authoring path",
    )
    add.add_argument("--expect", help="refuse unless the note's sha256 matches")
    add.add_argument(
        "--from-git",
        metavar="REV",
        help="take the content from REV (as committed) instead of the working tree",
    )
    add.add_argument("-m", "--message", help="describe this change (default: derived)")

    pointer = sub.add_parser("pointer", parents=[common], help="manage explicit pointer references")
    pointer_sub = pointer.add_subparsers(dest="pointer_command")
    pointer_add = pointer_sub.add_parser("add", parents=[common], help="add an explicit @file, @dir, or @locked reference")
    pointer_add.add_argument("expression")
    pointer_add.add_argument("id")
    pointer_add.add_argument("-m", "--message")

    rm = sub.add_parser(
        "rm", parents=[common], help="remove and version one note (one atomic act)"
    )
    rm.add_argument("id", help="the note id to remove")
    rm.add_argument("--expect", help="refuse unless the note's sha256 matches")
    rm.add_argument("-m", "--message", help="describe this change (default: derived)")

    mv = sub.add_parser("mv", parents=[common], help="move a working file or a record/topic")
    mv.add_argument("source", help="working file path, or note ID with --id")
    mv.add_argument("destination", help="new working file path, or new ID with --id")
    mv.add_argument("--id", action="store_true", help="move note IDs instead of working files")
    mv.add_argument("--dry-run", action="store_true", help="preview affected IDs and references")
    mv.add_argument("--expect", help="path mode: refuse unless the note's sha256 matches")
    mv.add_argument("--expect-head", help="ID mode: refuse unless the repository HEAD matches")
    mv.add_argument("-m", "--message", help="describe this move")

    search = sub.add_parser("search", parents=[common], help="server-side text search")
    search.add_argument("query")
    search.add_argument("--mode", default="text", choices=["text", "hybrid"])
    search.add_argument("--project")
    search.add_argument("--status")
    search.add_argument("--type")
    search.add_argument("--tag")
    search.add_argument("--limit", type=int, default=20)

    index = sub.add_parser(
        "index", parents=[common], help="inspect or rebuild the search index"
    )
    index.add_argument("--rebuild", action="store_true", help="drop and re-embed everything")

    py = sub.add_parser(
        "python",
        parents=[common],
        help="run a program against mem (client-side; read-only by default)",
    )
    py.add_argument("script", nargs="?", help="a script file; omit to read stdin")
    py.add_argument("-c", "--code", help="the program, as a string")
    py.add_argument(
        "--allow-write",
        action="store_true",
        help="bind add/rm/mv as well (not a security boundary -- see the docs)",
    )

    hook = sub.add_parser(
        "hook",
        parents=[common],
        help="follow a git repository's commits into memory",
    )
    hook_sub = hook.add_subparsers(dest="hook_command")

    hook_add = hook_sub.add_parser(
        "add", parents=[common], help="declare path globs to follow"
    )
    hook_add.add_argument("patterns", nargs="+", help="glob(s); prefix with ! to exclude")

    hook_rm = hook_sub.add_parser(
        "rm", parents=[common], help="stop following path globs"
    )
    hook_rm.add_argument("patterns", nargs="+")

    hook_sub.add_parser("ls", parents=[common], help="show patterns, hook state, watermark")

    hook_init = hook_sub.add_parser(
        "init", parents=[common], help="install pre-commit validation and post-commit sync hooks"
    )
    hook_init.add_argument(
        "--uninstall", action="store_true", help="remove only mem's block"
    )

    hook_update = hook_sub.add_parser(
        "update",
        parents=[common],
        help="capture committed changes since the last capture (also run by the hook)",
    )
    hook_update.add_argument("--repo", help="the repository to read (default: here)")
    hook_update.add_argument("--rev", help="the revision to capture up to (default: HEAD)")
    hook_update.add_argument(
        "--dry-run", action="store_true", help="report what would be added, write nothing"
    )

    hook_check = hook_sub.add_parser("check", parents=[common], help="validate staged, hook-selected Markdown locally")
    hook_check.add_argument("--repo", help="the repository to check (default: here)")

    return parser


# --------------------------------------------------------------------------
# output


def emit(payload: Any, *, human: str | None = None, as_human: bool = False) -> None:
    """Print the machine payload, compactly.

    The default output is consumed by agents and parsers, not read on a terminal:
    indentation is pure cost. On a 31-note listing it was 2,544 characters
    (~700 tokens) of whitespace -- 18.5% of the payload -- for no information. A
    caller that wants to read it asks for `--human` instead.
    """
    if as_human and human is not None:
        print(human)
    else:
        print(json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":")))


def fail(exc: MemError, as_human: bool) -> int:
    """Report a failure. Always one JSON object unless a human asked for prose.

    ``usage_error`` is the exception, and the rule lives here rather than at the
    call site: the only way to raise it is from inside argument parsing, which
    aborts before ``--human`` is reliably known -- so a guard in ``main`` could
    never fire and would be dead code. Keeping it here also means a future
    handler that raises it still gets the envelope ``MEM-CLI.md`` §4 requires.
    """
    if as_human and exc.code != "usage_error":
        print(f"mem: {exc.code}: {exc.message}", file=sys.stderr)
    else:
        print(json.dumps(exc.to_dict(), ensure_ascii=False), file=sys.stderr)
    return exc.exit_code


def _client(
    config: cfg.ClientConfig,
    args: argparse.Namespace,
    *,
    timeout: float | None = None,
) -> Client:
    server = args.server or config.server
    token = config.credentials.token if config.credentials else None
    return Client(server, token, timeout=timeout or DEFAULT_TIMEOUT)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _body_of(text: str) -> str:
    """The note's prose, without its YAML frontmatter.

    The server owns the frontmatter: it records `origin:`, reflows `tags` and
    wraps `summary` on the way in, so a working file can never be byte-identical
    to the committed revision even when the author changed nothing. Comparing raw
    bytes therefore reports "diverged" for every note, which is why the
    substance -- the body the author actually edits -- is what gets compared.

    Mirrors the server's `_body_only`: the closing delimiter must sit on a line of
    its own, because multi-line YAML values (a Windows `path:`, a summary
    containing `---`) defeat a naive split.
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return text
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "\n".join(lines[index + 1 :]).lstrip("\n")
    return text


def _parse_iso_epoch(value: str) -> float | None:
    """ISO-8601 to epoch seconds, or ``None`` when it is absent or unparseable.

    ``None`` is a real answer here, not an error: a caller that cannot establish
    which copy is newer must be told the committed revision (the one thing it can
    always fetch) rather than be handed a working path on a guess.
    """
    text = (value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = _dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_dt.timezone.utc)
    return moment.timestamp()


def _committed_content(
    rev: str,
    path: Path,
    config: cfg.ClientConfig,
    *,
    note_id: str,
    limit: int,
) -> bytes:
    """The bytes of *path* **as committed at** *rev*.

    The working tree is not consulted. A hook fires after a commit, and the file
    on disk may already hold the next edit; memory must record what git recorded,
    or the note it stores is a draft that never existed as a revision.
    """
    root = tracking.repo_root(path.parent)
    if root is None:
        raise MemError(
            "invalid_request",
            f"--from-git needs a git repository containing {path}",
            {"path": str(path)},
        )
    try:
        rel = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise MemError(
            "invalid_request",
            f"{path} is not inside the repository at {root}",
            {"path": str(path), "repo": str(root)},
        ) from exc
    content = tracking.committed_bytes(rev, rel, root)
    if len(content) > limit:
        raise MemError(
            "invalid_request",
            f"{note_id} is {len(content)} bytes at {rev}, over the inline upload limit; "
            "use --pointer",
            {"id": note_id, "rev": rev, "bytes": len(content), "limit": limit},
        )
    return content


def _newer_side(note_updated: str | None, local_mtime: float) -> str | None:
    """``"local"`` or ``"server"`` — which copy was written later.

    Wall-clock based, so a skewed clock can invert it. That is why the record
    also carries both raw timestamps and ``timestamp_basis``: the comparison is
    offered as evidence, not as an authority.
    """
    committed = _parse_iso_epoch(note_updated or "")
    if committed is None:
        return None
    return "local" if local_mtime > committed else "server"


def _freshness_line(record: dict[str, Any]) -> str:
    """One line for ``--human``: where the bytes are, and which side is newer."""
    source = record.get("source")
    if source == "local":
        line = f"working file is newer: {record.get('origin_path')}"
    elif source == "same":
        line = f"working file matches the committed revision: {record.get('origin_path')}"
    elif source == "server" and record.get("origin_path"):
        line = (
            "committed revision is newer than your working file "
            f"({record['origin_path']}); fetch: {record.get('fetch_url', '')}"
        )
    else:
        line = f"fetch: {record.get('fetch_url', '')}"
    if record.get("link_only") and "fetch: " not in line and record.get("fetch_url"):
        line += f"; link: {record['fetch_url']}"
    return line


def _strip_freshness_hashes(record: dict[str, Any]) -> None:
    """Keep the comparison result, not its inputs.

    ``sha256`` stays: it identifies the committed file and is the value
    ``--expect`` takes. ``body_sha256`` and the local hashes are implementation
    detail behind ``body_matches``.
    """
    for key in ("body_sha256", "content_sha256", "local_sha256", "local_body_sha256"):
        record.pop(key, None)


def _derive_id(path: Path, as_id: str | None, *, base: Path | None = None) -> str:
    """The id a file gets, relative to the **project marker** — not to the pwd (S35 D7).

    Deriving from the pwd gave one file two different ids depending on where the
    command ran, and reduced anything outside the tree to its bare filename — which is
    how two unrelated notes collide on one id. Deriving from the marker makes the base
    a reference resolves against and the base an id is derived from the same thing.
    """
    if as_id:
        return validate_id(as_id)
    root = (base or Path.cwd()).resolve()
    try:
        candidate = str(path.resolve().relative_to(root))
    except ValueError:
        # Refuse rather than guess a basename: a silent id is how collisions happen.
        raise MemError(
            "invalid_request",
            f"{path} is outside the project root {root}",
            {"path": str(path), "root": str(root), "hint": "pass --as <id>"},
        ) from None
    return validate_id(id_from_path(candidate))


def _require_origin_inside(path: Path, root: Path) -> str:
    """Validate the authoring path before it can be recorded as ``origin``.

    The project root is the directory holding ``.mem.toml``. Resolve symlinks on
    both sides so an inside-looking path that escapes the tree is caught, reject
    UNC paths, and compare with the platform's case rules. Returns the resolved
    path the caller should upload.
    """
    detail = {"path": str(path), "root": str(root),
              "hint": "copy the file under the project root, use `mem pointer add` for a reference, or pass --no-origin --as <id>"}
    try:
        candidate = path.resolve(strict=True)
        base = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise MemError("origin_outside_root", f"cannot resolve {path} inside the project root {root}: {exc}", detail) from exc

    if os.name == "nt":
        def _unc(value: str) -> bool:
            lowered = value.lower()
            if lowered.startswith("\\\\?\\unc\\"):
                return True
            return value.startswith("\\\\") and not lowered.startswith("\\\\?\\")

        if _unc(str(candidate)) or _unc(str(base)):
            raise MemError(
                "origin_outside_root",
                f"{path} is a UNC path; project origins must be local paths",
                detail,
            )
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise MemError(
            "origin_outside_root",
            f"{path} is outside the project root {base}",
            detail,
        ) from exc
    return str(candidate)


# --------------------------------------------------------------------------
# verbs


def cmd_login(args: argparse.Namespace) -> int:
    existing = cfg.read_config_file()
    server = args.server or existing.get("server") or cfg.DEFAULT_SERVER
    name = args.name or socket.gethostname()
    alias = args.alias or default_alias(socket.gethostname())
    secret = args.secret or os.environ.get("MEM_ENROLL_SECRET")
    if not secret:
        secret = getpass.getpass("enrollment secret: ")

    client = Client(server)
    result = client.enroll(
        name, secret, alias=alias, hostname=socket.gethostname(),
        os_name=platform.system().lower(), arch=platform.machine(),
    )
    # Credentials FIRST. The server returns the token exactly once, so writing
    # the config file before them meant a failed config write destroyed an
    # already-created device's only token (seen for real: two orphaned devices).
    credentials_path = cfg.write_credentials(
        cfg.Credentials(
            server=server,
            device_id=result["device_id"],
            token=result["token"],
            display_name=result.get("display_name", name),
            alias=result["alias"],
        )
    )
    cfg.write_config_file(server)
    emit(
        {
            "server": server,
            "device_id": result["device_id"],
            "display_name": result.get("display_name", name),
            "alias": result["alias"],
            "credentials": str(credentials_path),
        },
        human=f"enrolled {result['device_id']} [{result['alias']}] ({result.get('display_name', name)}) at {server}",
        as_human=args.human,
    )
    return 0


def cmd_logout(args: argparse.Namespace) -> int:
    removed = cfg.delete_credentials()
    emit(
        {"removed": removed},
        human="local token deleted" if removed else "no local token to delete",
        as_human=args.human,
    )
    return 0


def _project_note(project: str, summary: str) -> str:
    """Compose PROJECT.md without importing the server's YAML stack.

    JSON strings are valid YAML scalars, so this stays dependency-free and the
    server still validates the result through the normal write path.
    """
    title = f"{project} — project"
    return (
        "---\n"
        f"summary: {json.dumps(summary)}\n"
        "status: active\n"
        "type: project\n"
        "---\n"
        f"\n# {title}\n"
        "\n## Scope\n"
        f"\n{summary}\n"
    )


def cmd_init(args: argparse.Namespace) -> int:
    config = cfg.load(require_project=False, require_auth=True)
    directory = Path.cwd()
    project = validate_project(args.project or directory.name)
    summary = args.summary or f"{project} project"

    existing_marker = directory / cfg.MARKER_NAME
    if existing_marker.exists():
        # Idempotent by decision: warn, do not overwrite (MEM-RESOLUTIONS.md S10).
        emit(
            {"project": project, "marker": str(existing_marker), "created": False},
            human=f"{existing_marker} already exists — left unchanged",
            as_human=args.human,
        )
        return 0

    client = _client(config, args)
    known = {entry.get("project") for entry in client.projects()}
    created = False
    project_receipt: str | None = None
    if project not in known:
        note = _project_note(project, summary).encode("utf-8")
        queued = client.add(
            project,
            RESERVED_PROJECT_NOTE,
            note,
            filename="PROJECT.md",
            idempotency_key=add_key(
                _sha256(note),
                RESERVED_PROJECT_NOTE,
                None,
                origin_path=str(directory / "PROJECT.md"),
                message=f"mem: create project {project}",
            ),
            origin_path=str(directory / "PROJECT.md"),
            message=f"mem: create project {project}",
        )
        project_receipt = queued["receipt"]
        created = True

    cfg.write_marker(directory, project)
    emit(
        {"project": project, "marker": str(directory / cfg.MARKER_NAME),
         "created": created, **({"status": "queued", "receipt": project_receipt}
                              if project_receipt else {})},
        human=(
            f"project {project} {'created' if created else 'already on the server'}; "
            f"wrote {directory / cfg.MARKER_NAME}"
        ),
        as_human=args.human,
    )
    return 0


def _audit_origins(config: cfg.ClientConfig, client: Client) -> dict[str, Any]:
    from .pointers import parse_pointer

    root = (config.marker_path.parent if config.marker_path else Path.cwd()).resolve()
    own_device = config.credentials.device_id if config.credentials else ""
    cursor = ""
    rows: list[dict[str, Any]] = []
    while True:
        page = client.notes(
            config.project, full="true", limit=500, cursor=cursor or None
        )
        rows.extend(page.get("notes") or [])
        cursor = page.get("next_cursor") or ""
        if not cursor:
            break

    entries: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for row in rows:
        if row.get("kind") in {"topic", "file", "dir", "locked"}:
            # Topics and pointer records have no note origin. Treating them as
            # remote origins inflated the audit and produced pathless entries.
            continue
        origin = row.get("origin") or {}
        device = origin.get("device")
        if device != own_device:
            entries.append({"id": row["id"], "state": "remote"})
            continue
        pointer = origin.get("pointer")
        if not pointer:
            entries.append({"id": row["id"], "state": "unresolved"})
            continue
        try:
            path = Path(parse_pointer(pointer).path)
        except Exception:
            entries.append({"id": row["id"], "state": "unresolved"})
            continue
        key = str(path).lower() if os.name == "nt" else str(path)
        seen[key] = seen.get(key, 0) + 1
        if not path.is_file():
            state = "missing"
        else:
            resolved = path.resolve()
            if not resolved.is_relative_to(root):
                state = "outside_root"
            else:
                marker = _nearest_project_marker(resolved)
                current = root / ".mem.toml"
                state = "cross_project" if marker and marker.resolve() != current.resolve() else "ok"
        entries.append({"id": row["id"], "path": str(path), "state": state})

    counts = {"ok": 0, "missing": 0, "outside_root": 0, "cross_project": 0,
              "ambiguous": 0, "remote": 0, "unresolved": 0}
    for entry in entries:
        path = entry.get("path")
        if path:
            key = str(Path(path)).lower() if os.name == "nt" else str(Path(path))
            if seen.get(key, 0) > 1:
                entry["state"] = "ambiguous"
        counts[entry["state"]] += 1

    return {"counts": counts, "entries": entries}


def cmd_status(args: argparse.Namespace) -> int:
    config = cfg.load(require_project=bool(args.origins), require_auth=True)
    client = _client(config, args)
    result = client.status(config.project or "")
    origins = _audit_origins(config, client) if args.origins else None
    if origins is not None:
        result["origins"] = origins
    if args.human:
        lock = result.get("lock", {})
        text = [
            f"server   {client.server}",
            f"project  {result.get('project') or '(none)'}",
            f"head     {result.get('head')}",
            f"message  {result.get('message') or '(none)'}",
            f"lock     {'held by ' + str(lock.get('by')) if lock.get('held') else 'free'}",
            f"tree     {'DIRTY -- uncommitted content present' if result.get('index_dirty') else 'clean'}",
            f"max_bytes {result.get('read', {}).get('max_bytes')}",
        ]
        if origins is not None:
            text.append(
                "origins  "
                + ", ".join(f"{name} {count}" for name, count in origins["counts"].items())
            )
            for entry in origins["entries"]:
                if entry["state"] != "ok":
                    text.append(f"  {entry['state']:<13} {entry['id']}  {entry.get('path', '')}")
        emit(result, human="\n".join(text), as_human=True)
        return 0
    emit(result)
    return 0


def cmd_flush(args: argparse.Namespace) -> int:
    config = cfg.load(require_project=False, require_auth=True)
    client = _client(config, args)
    progress = client.queue_progress()
    through = progress["through"]
    deadline = time.monotonic() + FLUSH_TIMEOUT
    while progress["pending"] or progress.get("indexed_head") != progress.get("head"):
        if time.monotonic() >= deadline:
            raise MemError(
                "lock_timeout",
                "flush timed out waiting for queued writes and indexing",
                {"through": through},
            )
        time.sleep(0.1)
        progress = client.queue_progress(through)
    if progress["failed"]:
        raise MemError("queued_write_failed", f"{len(progress['failed'])} queued write(s) failed",
                       {"through": through, "failed": progress["failed"]})
    emit({"status": "flushed", "through": through, "head": progress["head"]},
         human=f"flushed through job {through}", as_human=args.human)
    return 0


def cmd_projects(args: argparse.Namespace) -> int:
    config = cfg.load(require_project=False, require_auth=True)
    client = _client(config, args)
    rows = client.projects()
    human = "\n".join(f"{r['project']:<24} {r['notes']:>4} notes  {r.get('updated') or ''}" for r in rows)
    emit(rows, human=human or "(no projects)", as_human=args.human)
    return 0


def cmd_manifest(args: argparse.Namespace) -> int:
    config = cfg.load(require_project=True, require_auth=True)
    topic = validate_id(args.topic) if args.topic else ""
    result = _client(config, args).manifest(config.project, topic)
    emit(result, human=f"{len(result['entries'])} entries at revision {result['revision']}", as_human=args.human)
    return 0


def cmd_device(args: argparse.Namespace) -> int:
    if args.device_command != "init":
        raise MemError("usage_error", "use `mem device init`")
    config = cfg.load(require_project=False, require_auth=True)
    creds = config.credentials
    if not creds or not creds.alias:
        raise MemError("unauthorized", "this enrollment has no device alias; run `mem login --alias <stable-name>`")
    path = initialize_device_home(alias=creds.alias, device_id=creds.device_id)
    emit({"path": str(path), "alias": creds.alias, "scope": "device", "share": "never"},
         human=f"initialized private device home at {path}", as_human=args.human)
    return 0


def cmd_ls(args: argparse.Namespace) -> int:
    config = cfg.load(require_project=True, require_auth=True)
    client = _client(config, args)
    result = client.notes(
        config.project,
        prefix=args.prefix,
        status=args.status,
        type=args.type,
        tag=args.tag,
        since=args.since,
        sort=args.sort,
        limit=args.limit,
        cursor=args.cursor,
        full=args.full,
    )
    if args.human:
        lines = [
            f"{n['id']:<44} {str(n.get('status')):<10} {str(n.get('type')):<9} {n.get('summary')}"
            for n in result["notes"]
        ]
        if result.get("next_cursor"):
            lines.append(f"-- next: {result['next_cursor']}")
        emit(result, human="\n".join(lines) or "(no notes)", as_human=True)
        return 0
    emit(result)
    return 0


def _split_revision(
    raw: str, explicit: str | None
) -> tuple[str, str | None, str | None, tuple[int, int] | None]:
    """``<id>[@<rev>][#<section>|<start>-<end>]`` (``MEM-CLI.md`` §3).

    ``#`` and ``:`` are unambiguous separators — neither can appear in an id, whose
    segments are ``[a-z0-9-]`` joined by ``/``. The selector is stripped **before** the
    revision so a heading containing ``@`` cannot be mistaken for one.
    """
    section: str | None = None
    span: tuple[int, int] | None = None
    if "#" in raw:
        raw, _, found = raw.partition("#")
        section = found.strip()
    elif ":" in raw:
        raw, _, found = raw.partition(":")
        head, _, tail = found.partition("-")
        try:
            span = (int(head), int(tail))
        except ValueError:
            raise MemError(
                "usage_error", f"range {found!r} is not <start>-<end>", {"range": found}
            ) from None
    if "@" in raw:
        head, _, tail = raw.rpartition("@")
        if head and tail:
            return validate_id(head), (explicit or tail), section, span
    return validate_id(raw), explicit, section, span


def _read_one(args: argparse.Namespace, config: cfg.ClientConfig, client: Client,
              raw_id: str) -> tuple[dict[str, Any], str]:
    note_id, revision, section, span = _split_revision(raw_id, args.revision)
    link_only = bool(getattr(args, "link_only", False))
    if link_only and (section is not None or span is not None):
        raise MemError(
            "usage_error",
            "--link-only applies to whole notes; a section or range always inlines",
            {"id": note_id},
        )
    if link_only and args.outline:
        raise MemError(
            "usage_error",
            "--link-only does not apply to --outline; an outline has no body",
            {"id": note_id},
        )
    if args.outline and (section is not None or span is not None):
        raise MemError(
            "usage_error",
            "--outline returns headings only; a section or range does not apply",
            {"id": note_id},
        )

    if args.outline:
        # Headings come from the server's outline rung, so no body is ever
        # transferred -- the whole point of a cheaper rung.
        result = client.outline(config.project, note_id, revision)
        if result.get("status") == "pending":
            return result, f"{note_id}: pending as {result['receipt']}"
        if result.get("status") == "failed":
            failure = result["error"]
            raise MemError(failure["code"], failure["message"],
                           {**failure.get("detail", {}), "receipt": result["receipt"],
                            "committed_revision": result.get("committed_revision")})
        lines = [f"{'  ' * (h['level'] - 1)}{h['text']}" for h in result["headings"]]
        return result, "\n".join(lines)

    record = client.note(
        config.project,
        note_id,
        revision,
        section=section,
        span=span,
        max_bytes=config.max_bytes,
        link_only=link_only,
    )
    if record.get("status") == "pending":
        return record, f"{note_id}: pending as {record['receipt']} (committed: {record.get('committed_revision') or 'none'})"
    if record.get("status") == "failed":
        failure = record["error"]
        raise MemError(failure["code"], failure["message"],
                       {**failure.get("detail", {}), "receipt": record["receipt"],
                        "committed_revision": record.get("committed_revision")})
    if record.get("kind") in {"file", "dir", "locked"}:
        alias = config.credentials.alias if config.credentials else None
        if alias and alias == record.get("points_at"):
            if record["kind"] == "locked":
                local_path = record.get("local_path")
            else:
                from .pointers import parse_pointer
                local_path = parse_pointer(record["pointer"]).path
                record["local_path"] = local_path
            if not local_path:
                raise MemError("internal_error", "server omitted the local pointer path")
            target = Path(local_path).expanduser()
            if record["kind"] == "locked":
                record["local_resolvable"] = True
            else:
                expected = target.is_file() if record["kind"] == "file" else target.is_dir()
                record["local_resolvable"] = expected
                if expected:
                    info = target.stat()
                    record["local_size"] = info.st_size
                    record["local_mtime"] = info.st_mtime
    body = record.get("body")
    # The client's own threshold is advisory and now actually consulted: it says
    # what *this* client is willing to take inline, independently of the
    # server's default (MEM-RESOLUTIONS.B2). The key must be *removed*, not
    # merely unbound -- rebuilding the dict would carry the body straight back in.
    #
    # `deferred_by_client` is set whenever THIS client declined a body, including
    # when the server had already truncated the same note. The two are different
    # facts -- the server's cap and this client's willingness -- and without the
    # flag the second is invisible, since `truncated` is true either way.
    if body is not None and len(body.encode("utf-8")) > config.max_bytes:
        record.pop("body", None)
        record["truncated"] = True
        record["deferred_by_client"] = True
        body = None
    elif (
        body is None
        and record.get("truncated")
        and record.get("bytes", 0) > config.max_bytes
        and not record.get("link_only")
    ):
        # The client sends its advisory limit to the server as `max_bytes`, so the
        # SERVER is what withholds the body here -- at this client's request. The
        # decline is still this client's, and without the flag it is invisible:
        # `truncated` is true either way, so a caller cannot tell a handoff it
        # asked for from one the server's own default imposed. A link-only read is
        # different: the caller asked for no body, so there is no client decline
        # to report.
        record["deferred_by_client"] = True

    # Which copy is fresh: the committed revision, or this device's working file?
    #
    # Only the client can answer. The server sees the committed bytes and knows
    # the commit time; it cannot stat a file on somebody else's disk. So it hands
    # over `body_sha256` + `note_updated` alongside the recorded working path,
    # and the decision is made here, where the file actually is. The whole body
    # is hashed even for a slice, so a section read can say whether the working
    # file as a whole still matches the committed revision.
    #
    # The working file is NOT expected to equal the committed revision -- it is
    # the author's copy, and it is legitimately ahead of memory between an edit
    # and the `mem add` that submits it. Divergence is normal; the caller is told
    # which side is newer rather than being handed an unqualified path.
    if record.get("kind") not in {"file", "dir", "locked"}:
        if record.get("origin_path"):
            working = resolve_origin(record["origin_path"])
            if working is None:
                # Recorded but gone (a temp path since cleaned, or a directory), or
                # belonging to a path space this platform cannot express. Say so
                # rather than offering a location that cannot be opened.
                record.pop("origin_path", None)
                record["source"] = "none"
                record["local_file"] = False
            else:
                try:
                    raw_bytes = working.read_bytes()
                    body_digest = hashlib.sha256(
                        _body_of(raw_bytes.decode("utf-8", "replace")).rstrip("\n").encode("utf-8")
                    ).hexdigest()
                    stat = working.stat()
                except OSError:
                    record.pop("origin_path", None)
                    record["source"] = "none"
                    record["local_file"] = False
                else:
                    record["origin_path"] = str(working)
                    record["local_file"] = True
                    record["local_bytes"] = stat.st_size
                    record["local_mtime"] = stat.st_mtime
                    committed_body = record.get("body_sha256")
                    if not committed_body:
                        # An older server did not identify the body; do not
                        # invent a comparison that was never performed.
                        record["source"] = "none"
                    elif body_digest == committed_body:
                        record["source"] = "same"
                        record["body_matches"] = True
                    else:
                        record["body_matches"] = False
                        newer = _newer_side(record.get("note_updated"), stat.st_mtime)
                        # Unknown ordering (no commit time, or an unparseable one)
                        # defaults to the committed revision: it is the one thing the
                        # caller can always fetch, so it is the safe answer to give
                        # when freshness cannot be established.
                        record["source"] = "local" if newer == "local" else "server"
                        # Only here was a comparison actually performed, so only here
                        # may the basis be claimed.
                        record["timestamp_basis"] = "mtime_vs_commit_time"
        else:
            # No working file was offered at all -- either another device committed
            # this note, or the recorded origin path was rejected as not a real
            # location. This is NOT a statement that the committed revision is newer,
            # which is why it is not `source: "server"`.
            record["source"] = "none"
            record["local_file"] = False
        _strip_freshness_hashes(record)

    if record.get("kind") in {"file", "dir", "locked"}:
        state = "resolvable on this device" if record.get("local_resolvable") else f"points at {record.get('points_at', 'another device')}; availability unverified"
        human = f"{record['id']} is a {record['kind']} pointer ({state}); target bytes are not served"
    elif body is not None:
        human = body
    else:
        human = f"{record['id']} is {record['bytes']} bytes; {_freshness_line(record)}"
    return record, human


def cmd_read(args: argparse.Namespace) -> int:
    config = cfg.load(require_project=True, require_auth=True)
    client = _client(config, args)
    if len(args.ids) == 1:
        record, human = _read_one(args, config, client, args.ids[0])
        emit(record, human=human, as_human=args.human)
        return 0
    results = []
    human_parts = []
    first_error: MemError | None = None
    for raw_id in args.ids:
        try:
            record, human = _read_one(args, config, client, raw_id)
        except MemError as exc:
            first_error = first_error or exc
            results.append({"id": raw_id, "ok": False, "error": exc.to_dict()["error"]})
            human_parts.append(f"{raw_id}: {exc.code}: {exc.message}")
        else:
            results.append({"id": raw_id, "ok": True, "record": record})
            human_parts.append(f"{raw_id}:\n{human}")
    emit({"results": results}, human="\n\n".join(human_parts), as_human=args.human)
    if first_error:
        fail(MemError(first_error.code, "one or more reads failed",
                      {"failed": [r["id"] for r in results if not r["ok"]]}), args.human)
        return first_error.exit_code
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    files = args.files
    if args.pointer and files[0].startswith(("@file(", "@dir(", "@locked(")):
        if len(files) != 2:
            raise MemError("usage_error", "explicit pointer requires an expression and id")
        print("mem: deprecated: use `mem pointer add EXPRESSION ID`", file=sys.stderr)
        args.file, args.pointer_id = files
        return _cmd_add_one(args)
    if len(files) > 1 and (args.as_id or args.expect or args.from_git):
        raise MemError("usage_error", "--as, --expect, and --from-git require one file")
    if len(files) > 1 and not args.message:
        raise MemError("usage_error", "multiple files require -m/--message")
    if args.pointer:
        if args.from_git:
            raise MemError("usage_error", "--pointer cannot be combined with --from-git")
        return _cmd_add_files(args)
    if len(files) == 1:
        args.file, args.pointer_id = files[0], None
        return _cmd_add_one(args)
    return _cmd_add_files(args)


def cmd_pointer(args: argparse.Namespace) -> int:
    if args.pointer_command != "add":
        raise MemError("usage_error", "use: mem pointer add EXPRESSION ID")
    args.file, args.pointer_id = args.expression, args.id
    args.pointer = True
    args.as_id = None
    args.expect = None
    args.from_git = None
    return _cmd_add_one(args)


def _precheck_markdown(content: bytes, label: str) -> None:
    from .summary import parse_frontmatter, validate_summary

    try:
        data, _ = parse_frontmatter(content.decode("utf-8"))
        validate_summary(data)
    except UnicodeDecodeError as exc:
        raise MemError("schema_invalid", f"{label} is not UTF-8") from exc


def _cmd_add_files(args: argparse.Namespace) -> int:
    config = cfg.load(require_project=True, require_auth=True)
    no_origin = bool(getattr(args, "no_origin", False))
    if no_origin and args.pointer:
        raise MemError("usage_error", "--no-origin applies to content uploads; a pointer never records an origin")
    client = _client(config, args)
    policy = client.policy()
    allowed = set(policy["inline_extensions"])
    root = config.marker_path.parent if config.marker_path else Path.cwd()
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in args.files:
        path = Path(raw)
        if not path.is_file():
            raise MemError("invalid_request", f"not a file: {raw}")
        if path.resolve().is_relative_to((Path.home() / ".device").resolve()):
            raise MemError("invalid_request", "device-home files are private and cannot be added to project memory")
        extension = path.suffix.lower()
        inline = not args.pointer and extension in allowed
        origin_path = None
        if inline and not no_origin:
            origin_path = _require_origin_inside(path, root)
        note_id = _derive_id(path, args.as_id, base=config.marker_path.parent if config.marker_path else None)
        if note_id in seen:
            raise MemError("invalid_request", f"duplicate id in batch: {note_id}")
        seen.add(note_id)
        if inline:
            if path.stat().st_size > policy["max_upload_bytes"]:
                raise MemError("invalid_request", f"{path} exceeds the inline upload limit; use --pointer")
            if extension != ".md" and not note_id.endswith(extension):
                raise MemError("invalid_request", f"text id must retain the {extension} extension")
            content = path.read_bytes()
            kind = "note" if extension == ".md" else "text"
            if kind == "note":
                _precheck_markdown(content, str(path))
        else:
            from .pointers import parse_pointer
            if not config.credentials or not config.credentials.alias:
                raise MemError("unauthorized", "this enrollment needs a device alias to create a pointer")
            pointer = parse_pointer(f"@file({config.credentials.alias}:{path.resolve()})")
            content = (pointer.canonical + "\n").encode("utf-8")
            kind = "pointer"
            origin_path = None
        item = {"id": note_id, "filename": path.name, "kind": kind,
                "origin_path": origin_path, "sha256": _sha256(content)}
        if inline and len(args.files) > 1:
            item["content_path"] = str(path.resolve())
        else:
            item["content"] = content
        items.append(item)

    if len(items) == 1:
        item = items[0]
        result = client.add(
            config.project, item["id"], item["content"], filename=item["filename"],
            idempotency_key=add_key(_sha256(item["content"]), item["id"], args.expect,
                                    origin_path=item["origin_path"], message=args.message),
            if_match=args.expect, origin_path=item["origin_path"], message=args.message,
            kind=item["kind"],
        )
        if item["kind"] == "pointer":
            result["warning"] = f"{item['filename']} content was not stored; only its pointer is tracked"
        emit(result, human=f"queued {item['id']} as {result['receipt']}", as_human=args.human)
        return 0

    digest = hashlib.sha256()
    digest.update(config.project.encode("utf-8"))
    digest.update(args.message.encode("utf-8"))
    for item in items:
        for value in (item["id"], item["kind"], item["filename"], item["origin_path"] or "", item["sha256"]):
            digest.update(value.encode("utf-8"))
            digest.update(b"\0")
    result = client.add_many(config.project, items, message=args.message,
                             idempotency_key=f"{digest.hexdigest()}:add-many")
    human = f"queued {len(items)} files as {result['receipt']}"
    emit(result, human=human, as_human=args.human)
    return 0


def _cmd_add_one(args: argparse.Namespace) -> int:
    config = cfg.load(require_project=True, require_auth=True)
    no_origin = bool(getattr(args, "no_origin", False))
    if no_origin and args.pointer:
        raise MemError("usage_error", "--no-origin applies to content uploads; a pointer never records an origin")
    client = _client(config, args)

    from_git = getattr(args, "from_git", None)
    if from_git and args.pointer:
        raise MemError("usage_error", "--from-git reads committed file content and so cannot be combined with --pointer")

    path: Path | None = None
    storage_warning: str | None = None
    if args.pointer:
        from .pointers import parse_pointer
        if not args.pointer_id:
            raise MemError("usage_error", "pointer mode requires: mem add --pointer @file(alias:path) <id>")
        pointer = parse_pointer(args.file)
        content = (pointer.canonical + "\n").encode("utf-8")
        note_id = validate_id(args.as_id or args.pointer_id)
        origin_path = None
        kind = "pointer"
    else:
        if args.pointer_id:
            raise MemError("usage_error", "the second positional id is only valid with --pointer")
        path = Path(args.file)
        if not path.is_file():
            raise MemError("invalid_request", f"not a file: {args.file}")
        if path.resolve().is_relative_to((Path.home() / ".device").resolve()):
            raise MemError("invalid_request", "device-home files are private and cannot be added to project memory")
        policy = client.policy()
        allowed = set(policy["inline_extensions"])
        extension = path.suffix.lower()
        inline = extension in allowed
        origin_path = None
        if inline and not no_origin:
            origin_path = _require_origin_inside(
                path, config.marker_path.parent if config.marker_path else Path.cwd()
            )
        note_id = _derive_id(
            path, args.as_id, base=config.marker_path.parent if config.marker_path else None
        )
        if inline:
            if path.stat().st_size > policy["max_upload_bytes"]:
                raise MemError("invalid_request", f"{path} exceeds the inline upload limit; use --pointer")
            if extension != ".md" and not note_id.endswith(extension):
                raise MemError("invalid_request", f"text id must retain the {extension} extension")
            content = path.read_bytes()
            kind = "note" if extension == ".md" else "text"
        else:
            from .pointers import parse_pointer
            if not config.credentials or not config.credentials.alias:
                raise MemError("unauthorized", "this enrollment needs a device alias to create a pointer")
            pointer = parse_pointer(f"@file({config.credentials.alias}:{path.resolve()})")
            content = (pointer.canonical + "\n").encode("utf-8")
            origin_path = None
            kind = "pointer"
            storage_warning = f"{path.name} content was not stored; only its pointer is tracked"
        if from_git and kind != "pointer":
            content = _committed_content(
                from_git, path, config, note_id=note_id, limit=policy["max_upload_bytes"]
            )
    digest = _sha256(content)
    if kind == "note":
        _precheck_markdown(content, str(path or note_id))

    result = client.add(
        config.project,
        note_id,
        content,
        filename=path.name if path else "pointer.md",
        # The key identifies the whole request: content, path, provenance, message,
        # precondition and operation. The precondition is load-bearing -- a replay is checked
        # BEFORE the CAS, so a key that ignored `--expect` would let a stale
        # precondition be answered by a replay from a different request (S21/S25).
        idempotency_key=add_key(
            digest,
            note_id,
            args.expect,
            origin_path=origin_path,
            message=args.message,
        ),
        if_match=args.expect,
        # Record where this actually came from on THIS device (S12).
        origin_path=origin_path,
        message=args.message,
        kind=kind,
    )
    if storage_warning:
        result["warning"] = storage_warning
    if args.human:
        verb = "replayed" if result.get("idempotent_replay") else "queued"
        emit(
            result,
            human=(f"{verb} {note_id} as {result['receipt']}"
                   + (f"; {storage_warning}" if storage_warning else "")),
            as_human=True,
        )
        return 0
    emit(result)
    return 0


def cmd_rm(args: argparse.Namespace) -> int:
    config = cfg.load(require_project=True, require_auth=True)
    client = _client(config, args)
    note_id = validate_id(args.id)

    result = client.rm(
        config.project,
        note_id,
        idempotency_key=rm_key(note_id, args.expect, message=args.message),
        if_match=args.expect,
        message=args.message,
    )
    if args.human:
        text = f"queued rm {note_id} as {result['receipt']}"
        emit(result, human=text, as_human=True)
        return 0
    emit(result)
    return 0


def _nearest_project_marker(path: Path) -> Path | None:
    current = path if path.is_dir() else path.parent
    for directory in (current, *current.parents):
        candidate = directory / ".mem.toml"
        if candidate.is_file():
            return candidate
    return None


def _require_move_path(
    path: Path,
    root: Path,
    *,
    must_exist: bool,
    allow_existing: Path | None = None,
) -> Path:
    """Validate one side of an in-project path move."""
    detail = {"path": str(path), "root": str(root)}
    if must_exist:
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise MemError("local_file_missing", f"working file is missing: {path}", detail) from exc
        if not resolved.is_file():
            raise MemError("local_file_missing", f"not a regular file: {path}", detail)
    else:
        try:
            parent = path.parent.resolve(strict=True)
        except OSError as exc:
            raise MemError(
                "invalid_request", f"destination directory does not exist: {path.parent}", detail
            ) from exc
        resolved = parent / path.name
        if resolved.exists() and (allow_existing is None or resolved != allow_existing):
            raise MemError("local_path_exists", f"destination already exists: {path}", detail)

    root = root.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise MemError(
            "origin_outside_root", f"{path} is outside the project root {root}", detail
        ) from exc

    marker = _nearest_project_marker(resolved)
    current = root / ".mem.toml"
    if marker is not None and marker.resolve() != current.resolve():
        raise MemError(
            "cross_project_move",
            f"{path} belongs to another project ({marker.parent})",
            {**detail, "other_project_root": str(marker.parent)},
        )
    return resolved


def _wait_for_queue(client: Client) -> dict[str, Any]:
    """Wait for queued work to drain; abort if any prior job failed."""
    deadline = time.monotonic() + FLUSH_TIMEOUT
    progress = client.queue_progress()
    through = progress["through"]
    while progress["pending"]:
        if progress.get("failed"):
            raise MemError(
                "queued_write_failed",
                f"{len(progress['failed'])} queued write(s) failed",
                {"through": through, "failed": progress["failed"]},
            )
        if time.monotonic() >= deadline:
            raise MemError("lock_timeout", "timed out waiting for queued writes", {"through": through})
        time.sleep(0.1)
        progress = client.queue_progress(through)
    if progress.get("failed"):
        raise MemError(
            "queued_write_failed",
            f"{len(progress['failed'])} queued write(s) failed",
            {"through": through, "failed": progress["failed"]},
        )
    return progress


def _wait_for_job(client: Client, receipt: str, seq: int) -> dict[str, Any]:
    deadline = time.monotonic() + FLUSH_TIMEOUT
    while True:
        progress = client.queue_progress(seq)
        failed = [row for row in progress.get("failed", []) if row.get("receipt") == receipt]
        if failed:
            raise MemError(
                "queued_write_failed",
                f"move {receipt} failed",
                {"through": seq, "failed": failed},
            )
        if progress["pending"] == 0:
            return progress
        if time.monotonic() >= deadline:
            raise MemError("lock_timeout", "timed out waiting for the move to commit", {"through": seq})
        time.sleep(0.1)


def _move_head(client: Client, project: str) -> str | None:
    """Best-effort final head; the move has already committed."""
    try:
        return client.status(project).get("head")
    except Exception:
        return None


def _rename_local(source: Path, destination: Path) -> None:
    if str(source) == str(destination):
        return
    if os.name == "nt" and str(source).lower() == str(destination).lower():
        temporary = source.with_name(source.name + ".mem-mv-tmp")
        os.rename(source, temporary)
        os.rename(temporary, destination)
        return
    os.rename(source, destination)


def _note_rows(client: Client, project: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cursor = ""
    while True:
        page = client.notes(
            project, full="true", limit=500, cursor=cursor or None
        )
        rows.extend(page.get("notes") or [])
        cursor = page.get("next_cursor") or ""
        if not cursor:
            return rows


def _atomic_write(path: Path, data: bytes, *, create_parents: bool) -> None:
    if create_parents:
        path.parent.mkdir(parents=True, exist_ok=True)
    elif not path.parent.is_dir():
        raise MemError(
            "invalid_request",
            f"parent directory does not exist: {path.parent}",
            {"path": str(path)},
        )
    fd, temporary = tempfile.mkstemp(prefix=".mem-pull-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        try:
            # A hard link is atomic and fails if the target already exists, so a
            # racing writer can never be overwritten by os.replace().
            os.link(temporary, path)
        except FileExistsError as exc:
            raise MemError(
                "local_path_exists", f"destination already exists: {path}", {"path": str(path)}
            ) from exc
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def _clean_export_destination(value: str) -> Path:
    destination = Path(value).expanduser()
    if not destination.is_absolute():
        destination = Path.cwd() / destination
    # Resolve existing symlinks and normalise `..`; strict=False also handles a
    # destination that does not exist yet.
    try:
        destination = destination.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise MemError("invalid_request", f"cannot resolve export destination: {exc}", {"destination": str(destination)}) from exc

    if any((directory / ".mem.toml").is_file() for directory in (destination, *destination.parents)):
        raise MemError(
            "invalid_request",
            f"export destination is inside a memory project: {destination}",
            {"destination": str(destination)},
        )
    if destination.exists():
        if not destination.is_dir():
            raise MemError("invalid_request", f"export destination is not a directory: {destination}", {"destination": str(destination)})
        try:
            non_empty = any(destination.iterdir())
        except OSError as exc:
            raise MemError("internal_error", f"cannot inspect export destination: {exc}", {"destination": str(destination)}) from exc
        if non_empty:
            raise MemError("invalid_request", f"export destination is not empty: {destination}", {"destination": str(destination)})
    return destination


def _export_target(destination: Path, row: dict[str, Any]) -> Path:
    note_id = validate_id(str(row["id"]))
    if str(row.get("kind")) == "note":
        return destination / f"{note_id}.md"
    if str(row.get("kind")) == "text":
        return destination / note_id
    raise MemError("invalid_request", f"cannot export a {row.get('kind')!r} record", {"id": note_id})


def cmd_pull(args: argparse.Namespace) -> int:
    config = cfg.load(require_project=True, require_auth=True)
    client = _client(config, args)
    from .pointers import parse_pointer

    root = (config.marker_path.parent if config.marker_path else Path.cwd()).resolve()
    current_marker = (root / ".mem.toml").resolve()
    own_device = config.credentials.device_id if config.credentials else ""
    rows = _note_rows(client, config.project)
    requested = set(args.ids)
    seen: set[str] = set()
    written: list[str] = []
    skipped: list[str] = []
    damaged: list[dict[str, Any]] = []

    for row in rows:
        note_id = str(row.get("id") or "")
        if requested and note_id not in requested:
            continue
        if row.get("kind") not in {"note", "text"}:
            continue
        seen.add(note_id)
        origin = row.get("origin") or {}
        if not requested and origin.get("device") != own_device:
            continue
        if origin.get("device") != own_device or not origin.get("pointer"):
            damaged.append({"id": note_id, "reason": "missing_origin"})
            continue
        try:
            target = Path(parse_pointer(str(origin["pointer"])).path).expanduser()
            resolved = target.resolve(strict=False)
        except Exception:
            damaged.append({"id": note_id, "reason": "missing_origin"})
            continue
        if not resolved.is_relative_to(root):
            damaged.append({"id": note_id, "path": str(target), "reason": "outside_root"})
            continue
        marker = _nearest_project_marker(resolved)
        if marker is not None and marker.resolve() != current_marker:
            damaged.append({"id": note_id, "path": str(target), "reason": "nested_project"})
            continue
        if target.exists():
            if target.is_file():
                skipped.append(str(target))
            else:
                damaged.append({"id": note_id, "path": str(target), "reason": "not_a_file"})
            continue
        if not target.parent.is_dir():
            damaged.append({"id": note_id, "path": str(target), "reason": "missing_parent"})
            continue
        try:
            _atomic_write(target, client.raw_bytes(config.project, note_id), create_parents=False)
        except MemError:
            raise
        except OSError as exc:
            damaged.append({"id": note_id, "path": str(target), "reason": f"write_failed: {exc}"})
            continue
        written.append(str(target))

    for note_id in requested - seen:
        damaged.append({"id": note_id, "reason": "not_found"})

    result = {"written": written, "skipped": skipped, "damaged": damaged}
    human = f"pulled {len(written)} file(s); skipped {len(skipped)}"
    if damaged:
        human += f"; damaged {len(damaged)}"
    emit(result, human=human, as_human=args.human)
    if damaged:
        raise MemError("invalid_request", "pull completed with damaged files", {"damaged": damaged})
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    config = cfg.load(require_project=False, require_auth=True)
    project = validate_project(args.project)
    destination = _clean_export_destination(args.dst)
    client = _client(config, args)

    rows = _note_rows(client, project)
    written: list[str] = []
    for row in rows:
        if row.get("kind") not in {"note", "text"}:
            continue
        target = _export_target(destination, row)
        if target.exists():
            raise MemError("local_path_exists", f"export target already exists: {target}", {"path": str(target)})
        try:
            _atomic_write(target, client.raw_bytes(project, str(row["id"])), create_parents=True)
        except MemError:
            raise
        except OSError as exc:
            raise MemError(
                "internal_error", f"cannot write {target}: {exc}", {"path": str(target)}
            ) from exc
        written.append(str(target))

    result = {"project": project, "destination": str(destination), "count": len(written), "written": written}
    emit(result, human=f"exported {len(written)} file(s) to {destination}", as_human=args.human)
    return 0


def cmd_mv(args: argparse.Namespace) -> int:
    config = cfg.load(require_project=True, require_auth=True)
    client = _client(config, args)

    if args.id:
        if args.expect:
            raise MemError("usage_error", "--expect is only valid for path moves")
        folder = args.source.endswith("/")
        if folder != args.destination.endswith("/"):
            raise MemError("invalid_request", "folder moves require trailing '/' on both IDs")
        source = validate_id(args.source.rstrip("/"))
        destination = validate_id(args.destination.rstrip("/"))
        source_arg = source + ("/" if folder else "")
        destination_arg = destination + ("/" if folder else "")
        if args.dry_run:
            result = client.mv(
                config.project, source_arg, destination_arg,
                idempotency_key=None, if_match=args.expect_head,
                message=args.message, dry_run=True,
            )
            emit(result, human=f"would move {source_arg} to {destination_arg}", as_human=args.human)
            return 0
        _wait_for_queue(client)
        result = client.mv(
            config.project, source_arg, destination_arg,
            idempotency_key=mv_key(source_arg, destination_arg, args.expect_head,
                                   message=args.message),
            if_match=args.expect_head, message=args.message, dry_run=False,
        )
        _wait_for_job(client, result["receipt"], result["seq"])
        final = {**result, "status": "committed", "head": _move_head(client, config.project)}
        emit(final, human=f"moved {source_arg} to {destination_arg}", as_human=args.human)
        return 0

    if args.expect_head:
        raise MemError("usage_error", "--expect-head is only valid with --id")
    if args.source.endswith("/") or args.destination.endswith("/"):
        raise MemError("invalid_request", "path moves take files, not topic IDs")
    if not Path(args.source).expanduser().exists():
        try:
            validate_id(args.source)
            validate_id(args.destination)
        except MemError:
            pass
        else:
            if not Path(args.source).suffix and not Path(args.destination).suffix:
                raise MemError(
                    "usage_error",
                    "mem mv now moves working files; use `mem mv --id` to move note IDs",
                    {"source": args.source, "destination": args.destination},
                )
    root = config.marker_path.parent if config.marker_path else Path.cwd()
    source = _require_move_path(Path(args.source).expanduser(), root, must_exist=True)
    destination = _require_move_path(
        Path(args.destination).expanduser(), root, must_exist=False, allow_existing=source
    )
    old_path, new_path = str(source), str(destination)

    if args.dry_run:
        result = client.repath(
            config.project, old_path, new_path,
            if_match=args.expect, message=args.message, dry_run=True,
        )
        emit(result, human=f"would move {old_path} to {new_path}", as_human=args.human)
        return 0

    _wait_for_queue(client)
    # Resolve before touching the filesystem, so a typo cannot move a file that
    # no note owns.
    lookup = client.repath(
        config.project, old_path, new_path,
        if_match=args.expect, message=args.message, dry_run=True,
    )
    try:
        _rename_local(source, destination)
    except OSError as exc:
        raise MemError(
            "internal_error",
            "cannot move local file: " + str(exc),
            {"source": str(source), "destination": str(destination)},
        ) from exc
    try:
        result = client.repath(
            config.project, old_path, new_path,
            idempotency_key=repath_key(old_path, new_path, args.expect, message=args.message),
            if_match=args.expect, message=args.message, dry_run=False,
        )
        _wait_for_job(client, result["receipt"], result["seq"])
    except Exception:
        try:
            _rename_local(destination, source)
        except OSError as rollback:
            raise MemError(
                "internal_error",
                "move failed and the local rename could not be rolled back",
                {"move_error": "see previous error", "rollback_error": str(rollback)},
            ) from rollback
        raise

    final = {
        "status": "committed",
        "id": lookup["id"],
        "old_path": old_path,
        "new_path": new_path,
        "head": _move_head(client, config.project),
    }
    emit(final, human=f"moved {old_path} to {new_path}", as_human=args.human)
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    config = cfg.load(require_project=not args.project, require_auth=True)
    # Hybrid search may have to load the embedding model and sync the index on the
    # first call, which outlasts the interactive timeout (S29).
    client = _client(
        config, args, timeout=EMBED_TIMEOUT if args.mode == "hybrid" else None
    )
    project = args.project or config.project
    result = client.search(
        project,
        args.query,
        mode=args.mode,
        status=args.status,
        type=args.type,
        tag=args.tag,
        limit=args.limit,
    )
    if args.human:
        lines = []
        for hit in result["hits"]:
            where = hit.get("line_range")
            suffix = f":{where[0]}" if where else ""
            via = ",".join(hit.get("matched_by") or []) or result.get("mode", "")
            lines.append(f"{hit['id']}{suffix:<6} [{via:<11}] {hit.get('summary') or ''}")
        if result.get("queue_pending"):
            lines.append(f"warning: {result['queue_pending']} write(s) pending; results may be stale")
        if result.get("index_lag"):
            lines.append("warning: search index is behind the committed revision")
        emit(result, human="\n".join(lines) or "(no hits)", as_human=True)
        return 0
    emit(result)
    return 0


def cmd_index(args: argparse.Namespace) -> int:
    config = cfg.load(require_project=False, require_auth=True)
    # A rebuild re-embeds the corpus, so it needs the embedding allowance; plain
    # status is cheap and stays on the interactive timeout.
    client = _client(config, args, timeout=EMBED_TIMEOUT if args.rebuild else None)
    if args.rebuild:
        result = client.index_rebuild()
        emit(
            result,
            human=(
                f"rebuilt: {result.get('chunks')} chunks over {result.get('notes')} notes, "
                f"{result.get('embedded')} embedded"
            ),
            as_human=args.human,
        )
        return 0
    result = client.index_status()
    if args.human:
        text = result.get("text", {})
        hybrid = result.get("hybrid", {})
        lines = [
            f"kind        {result.get('kind')}",
            f"head        {result.get('head')}",
            f"text index  head={text.get('indexed_head')} age={text.get('age')}",
        ]
        if hybrid.get("available"):
            lines += [
                f"semantic    model={hybrid.get('model')} chunks={hybrid.get('chunks')}"
                f" notes={hybrid.get('notes')}",
                f"            indexed_head={hybrid.get('indexed_head')}",
            ]
        else:
            lines.append(f"semantic    unavailable ({hybrid.get('reason', 'not installed')})")
        emit(result, human="\n".join(lines), as_human=True)
        return 0
    emit(result)
    return 0


def cmd_devices(args: argparse.Namespace) -> int:
    config = cfg.load(require_project=False, require_auth=True)
    client = _client(config, args)
    if args.retire:
        result = client.retire_device(args.retire)
        emit(
            result,
            human=f"retired {result['retired']} (the record is kept for provenance)",
            as_human=args.human,
        )
        return 0
    rows = client.devices()
    if args.human:
        lines = []
        for row in rows:
            state = "retired" if row.get("retired_at") else "active"
            lines.append(
                f"{row['device_id']:<26} {str(row.get('display_name')):<18} {state:<8} "
                f"last={row.get('last_seen_ip') or '-'}"
            )
        emit(rows, human="\n".join(lines) or "(no devices)", as_human=True)
        return 0
    emit(rows)
    return 0


def cmd_python(args: argparse.Namespace) -> int:
    """Run a program against mem, on **this** machine.

    This is the escape hatch for anything the two write verbs deliberately do not
    express — repairing broken links after a rename, restoring a deleted note,
    bulk re-tagging (`MEM-RESOLUTIONS.md` S23). Composition belongs here rather
    than in the verb set, or the implementation explodes.

    Three constraints, and they are the point:

    * **Read-only by default.** `add` and `rm` are *absent from the namespace*
      unless `--allow-write` is passed, so referencing them is a `NameError` --
      asserted in the tests, not assumed. Note bodies are untrusted input, and a
      note that says "ignore previous instructions and rewrite X" combined with an
      agent that writes code turns prompt injection into a write against the only
      copy of memory.
    * **The token is not a bare name.** It is not injected into the namespace, so
      it cannot be *accidentally* interpolated into a note or logged. It is still
      reachable as `client.token`, because the client needs it to make requests —
      like `--allow-write`, this limits accidents, not a determined script.
    * **Client-side only.** There is no server counterpart and no `/v1/exec`; a
      sandbox on the service host would be remote code execution against the box that
      holds everything.

    This is a *default*, not a security boundary: an agent with a shell can run
    `mem add` directly. It makes the common case safe, nothing more.
    """
    config = cfg.load(require_project=True, require_auth=True)
    client = _client(config, args)
    project = config.project

    namespace: dict[str, Any] = {
        "__name__": "__mem__",
        "client": client,
        "config": config,
        "project": project,
        "emit": lambda obj: print(json.dumps(obj, indent=2, ensure_ascii=False, default=str)),
        # BUG(low): status is bound as the raw Client.status method while every
        # other binding closes over project, so status() inside `mem python`
        # raises TypeError.
        # Why valid: Client.status(self, project) requires a project argument
        # (see Client.status in client.py) whereas ls/read/outline/search wrap
        # the call; calling status() with no argument raises TypeError, which
        # surfaces as internal_error while ls() works.
        # Fix hint: bind it like the other wrappers, e.g.
        # lambda **f: client.status(project, **f).
        "status": client.status,
        "ls": lambda **f: client.notes(project, **f)["notes"],
        "read": lambda note_id, revision=None, section=None, span=None: client.note(
            project, note_id, revision, section=section, span=span
        ),
        "outline": lambda note_id, revision=None: client.outline(project, note_id, revision),
        "search": lambda q, **f: client.search(project, q, **f)["hits"],
    }

    if args.allow_write:

        def _add(path: str, as_id: str | None = None, expect: str | None = None,
                 message: str | None = None):
            source = Path(path)
            content = source.read_bytes()
            note_id = validate_id(as_id) if as_id else _derive_id(
                source, None, base=config.marker_path.parent if config.marker_path else None
            )
            digest = _sha256(content)
            return client.add(
                project, note_id, content, filename=source.name,
                idempotency_key=add_key(
                    digest,
                    note_id,
                    expect,
                    origin_path=str(source.resolve()),
                    message=message,
                ),
                if_match=expect, origin_path=str(source.resolve()), message=message,
            )

        def _rm(note_id: str, expect: str | None = None, message: str | None = None):
            target = validate_id(note_id)
            return client.rm(
                project, target, idempotency_key=rm_key(target, expect, message=message),
                if_match=expect, message=message,
            )

        namespace["add"] = _add
        namespace["rm"] = _rm
        namespace["mv"] = lambda source, destination, expect_head=None, message=None: client.mv(
            project, source, destination,
            idempotency_key=mv_key(source, destination, expect_head, message=message),
            if_match=expect_head, message=message,
        )

    # `is not None`, not truthiness: `-c ""` is an empty program, not "read stdin".
    # The falsy test silently fell through to `sys.stdin.read()`, which blocks on a
    # TTY and looks like a hang.
    if args.code is not None:
        source, filename = args.code, "<-c>"
    elif args.script:
        script = Path(args.script)
        if not script.is_file():
            raise MemError("invalid_request", f"not a file: {args.script}")
        source, filename = script.read_text(encoding="utf-8"), str(script)
    else:
        source, filename = sys.stdin.read(), "<stdin>"

    try:
        _run_program(source, filename, namespace)
    except MemError:
        raise
    except Exception as exc:  # noqa: BLE001 - the script's own failure
        # A typed error, not a bare traceback (MEM-RESOLUTIONS.md S22) -- but the
        # traceback rides along in `detail`, because the author of the script is
        # the person who needs it.
        import traceback

        raise MemError(
            "internal_error",
            f"{exc.__class__.__name__}: {exc}",
            {"traceback": traceback.format_exc()},
        ) from exc
    return 0


def _run_program(source: str, filename: str, namespace: dict[str, Any]) -> None:
    """Run a program in ``namespace`` and print its last expression's value.

    ``exec`` discards a trailing expression's value, so ``mem python -c "ls()"``
    would print nothing at all — which defeats the point of the composition
    runner (`MEM-ACCESS.md` §3.1: "the script's own return value (if any) is
    printed"). The final expression is therefore split off and evaluated on its
    own, the way a REPL does.

    A final statement that is not an expression, or one that evaluates to
    ``None``, prints nothing rather than a noisy ``null``. ``print()`` inside the
    program keeps working, because stdout is inherited.
    """
    tree = ast.parse(source, filename)
    trailing = (
        tree.body.pop() if tree.body and isinstance(tree.body[-1], ast.Expr) else None
    )
    # Compile BOTH halves before executing either. `ast.parse` accepts constructs
    # that only fail at compile time (`await`/`yield` outside a function), and
    # compiling the tail lazily would run the whole prefix -- side effects, and
    # under --allow-write real writes -- before reporting a program that could
    # never have started at all.
    program = (
        compile(ast.fix_missing_locations(tree), filename, "exec")
        if tree.body
        else None
    )
    expression = (
        compile(ast.Expression(trailing.value), filename, "eval")
        if trailing is not None
        else None
    )
    if program is not None:
        exec(program, namespace)
    if expression is not None:
        value = eval(expression, namespace)  # noqa: S307 - running the caller's program is the point
        if value is not None:
            print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


def cmd_hook(args: argparse.Namespace) -> int:
    """Follow a git repository's commits into memory.

    Three verbs with distinct jobs, so each one stays simple:

    * ``add``/``rm``/``ls`` — the *policy*: which paths are followed. Stored in
      the project marker and therefore device-local, like ``.gitignore``.
    * ``init`` — the *mechanism*: a shim in ``.git/hooks`` that delegates here.
    * ``update`` — the *behaviour*: capture everything committed since the last
      capture. Safe to run by hand, which is what makes catch-up free.

    ``update`` reads committed blobs, never the working tree, and never fails the
    commit that triggered it: a hook that can break your history is worse than a
    memory that lags.
    """
    command = getattr(args, "hook_command", None) or "ls"

    if command in {"add", "rm", "ls"}:
        marker = cfg.find_marker()
        if not marker:
            raise MemError(
                "no_project",
                "no .mem.toml found in this directory or any parent",
                {"hint": "run `mem init <project>` here"},
            )
        marker_path = marker[0]
        patterns = cfg.read_marker_patterns(marker_path)
        if command == "add":
            added = [p for p in args.patterns if p not in patterns]
            patterns = [*patterns, *added]
            cfg.write_marker_patterns(marker_path, patterns)
        elif command == "rm":
            patterns = [p for p in patterns if p not in set(args.patterns)]
            cfg.write_marker_patterns(marker_path, patterns)
        payload = {
            "marker": str(marker_path),
            "patterns": patterns,
            "hook": hooks.status(Path.cwd()),
        }
        if args.human:
            lines = [f"{len(patterns)} pattern(s) in {marker_path}"]
            lines += [f"  {p}" for p in patterns]
            lines.append(f"hook installed: {payload['hook']['installed'] or 'no'}")
            emit(payload, human="\n".join(lines), as_human=True)
            return 0
        emit(payload)
        return 0

    if command == "init":
        repo = _hook_repo(args)
        result = hooks.uninstall(repo) if args.uninstall else hooks.install(repo)
        result["repo"] = str(repo)
        verb = "uninstalled from" if args.uninstall else "installed into"
        emit(result, human=f"{verb} {result['hook_dir']}", as_human=args.human)
        return 0

    if command == "update":
        return _hook_update(args)

    if command == "check":
        return _hook_check(args)

    raise MemError("usage_error", f"unknown hook command: {command}")


def _hook_repo(args: argparse.Namespace) -> Path:
    """The repository a hook verb acts on: ``--repo`` if given, else here."""
    candidate = Path(getattr(args, "repo", None) or Path.cwd())
    root = tracking.repo_root(candidate)
    if root is None:
        raise MemError(
            "invalid_request",
            f"{candidate} is not inside a git repository",
            {"path": str(candidate)},
        )
    return root


def _marker_for_repo(repo: Path) -> tuple[Path, dict[str, Any]] | None:
    """The marker that owns *repo*, with the same containment rule as Go."""
    marker = cfg.find_marker(repo)
    if marker is None:
        return None
    marker_path, data = marker
    root = marker_path.parent.resolve()
    try:
        repo.resolve().relative_to(root)
    except ValueError as exc:
        raise MemError(
            "invalid_request",
            f"{repo} is outside the project root {root}; "
            "run `mem init` in this repository to track it separately",
            {"repo": str(repo), "project_root": str(root)},
        ) from exc
    return marker_path, data


def _hook_check(args: argparse.Namespace) -> int:
    """Validate the staged blobs that an opted-in repository is about to commit."""
    from .summary import parse_frontmatter, validate_summary

    repo = _hook_repo(args)
    marker = _marker_for_repo(repo)
    patterns = cfg.read_marker_patterns(marker[0]) if marker else []
    if not patterns:
        emit({"checked": [], "errors": []}, human="no files selected by mem hook", as_human=args.human)
        return 0
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMRT", "-z"],
        cwd=str(repo), capture_output=True, check=False,
    )
    if result.returncode != 0:
        raise MemError("internal_error", "cannot inspect staged paths")
    paths = [p for p in result.stdout.decode("utf-8", "replace").split("\0") if p]
    selected = [p for p in tracking.tracked_paths(patterns, paths) if p.lower().endswith(".md")]
    errors: list[dict[str, str]] = []
    for path in selected:
        blob = subprocess.run(["git", "show", f":{path}"], cwd=str(repo), capture_output=True, check=False)
        if blob.returncode != 0:
            errors.append({"path": path, "message": "cannot read staged file"})
            continue
        try:
            text = blob.stdout.decode("utf-8")
            data, _ = parse_frontmatter(text)
            validate_summary(data)
        except (UnicodeDecodeError, MemError) as exc:
            errors.append({"path": path, "message": str(exc)})
    if errors:
        raise MemError("schema_invalid", "hook-selected Markdown needs a valid summary",
                       {"files": errors})
    emit({"checked": selected, "errors": []},
         human=f"validated {len(selected)} staged Markdown file(s)", as_human=args.human)
    return 0


def _hook_update(args: argparse.Namespace) -> int:
    """Capture every tracked path committed since the last capture.

    Exits 0 for every per-file problem: this runs from a hook, where a non-zero
    status is noise at best and a damaged workflow at worst. Failures go to
    ``mem-hook.log`` in the git dir, which is where a human would look.
    """
    repo = _hook_repo(args)
    result: dict[str, Any] = {
        "repo": str(repo),
        "patterns": 0,
        "added": [],
        "receipts": [],
        "skipped": [],
        "orphaned": [],
        "errors": [],
        "dry_run": bool(args.dry_run),
    }
    try:
        marker = _marker_for_repo(repo)
    except MemError as exc:
        result["errors"].append(exc.message)
        _log_hook(repo, result["errors"])
        emit(result, as_human=args.human)
        return 0

    marker_path = marker[0] if marker else None
    patterns = cfg.read_marker_patterns(marker_path) if marker_path else []
    result["patterns"] = len(patterns)
    if not patterns:
        # Nothing declared is not an error: the hook may be installed repo-wide.
        emit(result, human="no patterns declared; nothing to do", as_human=args.human)
        return 0

    config = cfg.load(require_project=False, require_auth=False)
    if marker is not None:
        marker_path, marker_data = marker
        cwd_marker = cfg.find_marker()
        cwd_project = cwd_marker[1].get("project") if cwd_marker else None
        repo_project = marker_data.get("project")
        if cwd_project and repo_project and cwd_project != repo_project:
            raise MemError(
                "invalid_request",
                "current directory belongs to a different project than --repo",
                {"repo_project": repo_project, "cwd_project": cwd_project},
            )
        config.project = repo_project
        config.marker_path = marker_path
        if marker_data.get("server"):
            config.server = str(marker_data["server"])

    if not config.project or not config.credentials:
        result["errors"].append("not enrolled in a project here; run `mem login` and `mem init`")
        _log_hook(repo, result["errors"])
        emit(result, as_human=args.human)
        return 0

    rev = args.rev or _head_rev(repo)
    if not rev:
        result["errors"].append("could not resolve a revision to capture")
        _log_hook(repo, result["errors"])
        emit(result, as_human=args.human)
        return 0

    # `since = watermark` even when it equals `rev`: an empty range is the
    # correct answer for "nothing new", whereas dropping it to None would re-read
    # the whole commit and re-add every file. That mistake made a second run
    # duplicate the first, which is exactly what the watermark exists to prevent.
    watermark = tracking.read_watermark(repo)
    since = watermark or None

    try:
        if since is None:
            tree = subprocess.run(
                ["git", "ls-tree", "-r", "-z", "--name-only", rev],
                cwd=str(repo), capture_output=True, check=False,
            )
            if tree.returncode != 0:
                raise MemError("invalid_request", f"cannot list files at {rev}")
            paths = [p for p in tree.stdout.decode("utf-8", "replace").split("\0") if p]
        else:
            paths = tracking.changed_paths(rev, repo, since=since)
    except MemError as exc:
        result["errors"].append(exc.message)
        _log_hook(repo, result["errors"])
        emit(result, as_human=args.human)
        return 0

    matched = tracking.tracked_paths(patterns, paths)
    result["changed"] = len(paths)
    result["skipped"] = [p for p in paths if p not in set(matched)]

    # Report records whose file git no longer has, rather than silently keeping
    # them: memory accumulating entries for deleted files is invisible otherwise.
    deleted = _deleted_tracked(rev, since, repo, patterns)
    result["orphaned"] = deleted
    matched = [p for p in matched if p not in set(deleted)]

    if args.dry_run:
        result["would_add"] = matched
        emit(
            result,
            human="\n".join(
                [f"would capture {len(matched)} path(s) at {rev[:12]}"]
                + [f"  {p}" for p in matched]
                + ([f"  orphaned: {', '.join(deleted)}"] if deleted else [])
            ),
            as_human=args.human,
        )
        return 0

    if matched:
        try:
            client = _client(config, args)
            policy = client.policy()
        except MemError as exc:
            result["errors"].append(f"{', '.join(matched)}: {exc.message}")
        else:
            items: list[dict[str, Any]] = []
            for path in matched:
                try:
                    items.append(_prepare_capture(repo, rev, path, config, policy))
                except MemError as exc:
                    result["errors"].append(f"{path}: {exc.message}")
                except Exception as exc:  # noqa: BLE001 - hooks must report, not crash
                    result["errors"].append(f"{path}: {exc}")
            if not result["errors"]:
                branch = _branch(repo) or "detached"
                message = f"sync {len(items)} file(s) from {branch} at {rev[:12]}"
                try:
                    batches: list[list[dict[str, Any]]] = []
                    batch: list[dict[str, Any]] = []
                    batch_bytes = 0
                    for item in items:
                        size = len(item["content"])
                        if batch and (len(batch) == 1000 or batch_bytes + size > 256 * 1024 * 1024):
                            batches.append(batch)
                            batch, batch_bytes = [], 0
                        batch.append(item)
                        batch_bytes += size
                    if batch:
                        batches.append(batch)
                    for group in batches:
                        if len(group) == 1:
                            item = group[0]
                            queued = client.add(
                                config.project, item["id"], item["content"],
                                filename=item["filename"],
                                idempotency_key=add_key(
                                    _sha256(item["content"]), item["id"], None,
                                    origin_path=item["origin_path"], message=message,
                                ),
                                origin_path=item["origin_path"], message=message,
                                kind=item["kind"],
                            )
                        else:
                            digest = hashlib.sha256()
                            digest.update(config.project.encode("utf-8"))
                            digest.update(message.encode("utf-8"))
                            for item in group:
                                for value in (item["id"], item["kind"], item["filename"],
                                              item["origin_path"] or "", _sha256(item["content"])):
                                    digest.update(value.encode("utf-8"))
                                    digest.update(b"\0")
                            queued = client.add_many(config.project, group, message=message,
                                                     idempotency_key=f"{digest.hexdigest()}:add-many")
                        result["receipts"].append(queued["receipt"])
                        result["added"].extend(item["path"] for item in group)
                except MemError as exc:
                    result["errors"].append(f"batch: {exc.message}")
                except Exception as exc:  # noqa: BLE001 - a hook must not raise
                    result["errors"].append(f"batch: {exc}")

    if result["errors"]:
        _log_hook(repo, result["errors"])
    if not result["errors"]:
        tracking.write_watermark(repo, rev)
    result["status"] = "queued" if result["receipts"] else "idle"

    if args.human:
        lines = [f"queued {len(result['added'])} path(s) from {rev[:12]}"]
        lines += [f"  + {p}" for p in result["added"]]
        lines += [f"  ! {e}" for e in result["errors"]]
        if deleted:
            lines.append("  orphaned (still in memory, gone from git): " + ", ".join(deleted))
        emit(result, human="\n".join(lines), as_human=True)
        return 0
    emit(result)
    return 0


def _prepare_capture(repo: Path, rev: str, path: str, config: cfg.ClientConfig,
                     policy: dict[str, Any]) -> dict[str, Any]:
    """Prepare the latest committed bytes without changing memory."""
    from .pointers import parse_pointer

    allowed = set(policy["inline_extensions"])
    working = repo / path
    extension = working.suffix.lower()
    note_id = _derive_id(
        working, None, base=config.marker_path.parent if config.marker_path else None
    )
    origin_path = str(working.resolve())

    if extension in allowed:
        _require_origin_inside(
            working, config.marker_path.parent if config.marker_path else Path.cwd()
        )
        content = tracking.committed_bytes(rev, path, repo)
        if len(content) > policy["max_upload_bytes"]:
            raise MemError(
                "invalid_request",
                f"{len(content)} bytes exceeds the inline limit; declare it as a pointer instead",
            )
        if extension != ".md" and not note_id.endswith(extension):
            raise MemError("invalid_request", f"text id must retain the {extension} extension")
        kind = "note" if extension == ".md" else "text"
        if kind == "note":
            _precheck_markdown(content, path)
    else:
        if not config.credentials or not config.credentials.alias:
            raise MemError("unauthorized", "this enrollment needs a device alias to create a pointer")
        pointer = parse_pointer(f"@file({config.credentials.alias}:{working.resolve()})")
        content = (pointer.canonical + "\n").encode("utf-8")
        origin_path = ""
        kind = "pointer"

    return {"id": note_id, "path": path, "filename": working.name, "kind": kind,
            "origin_path": origin_path or None, "content": content}


def _deleted_tracked(
    rev: str, since: str | None, repo: Path, patterns: list[str]
) -> list[str]:
    """Tracked paths the revision deleted (present before, absent after)."""
    run = lambda *a: subprocess.run(  # noqa: E731 - a one-off local runner
        ["git", *a], cwd=str(repo), capture_output=True, check=False
    )
    args = ["diff", "--name-only", "--diff-filter=D", "-z"]
    args.append(f"{since}..{rev}" if since else f"{rev}^..{rev}")
    result = run(*args)
    if result.returncode != 0:
        return []
    raw = result.stdout.decode("utf-8", "replace")
    return tracking.tracked_paths(patterns, [p for p in raw.split("\0") if p])


def _head_rev(repo: Path) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, check=False
    )
    if result.returncode != 0:
        return None
    value = result.stdout.decode("utf-8", "replace").strip()
    return value or None


def _branch(repo: Path) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(repo), capture_output=True, check=False
    )
    if result.returncode != 0:
        return None
    value = result.stdout.decode("utf-8", "replace").strip()
    return None if not value or value == "HEAD" else value


def _log_hook(repo: Path, errors: list[str]) -> None:
    """Append failures to the git dir so a hook's silence is still diagnosable."""
    git_dir = hooks.hook_dir(repo)
    if git_dir is None:
        return
    target = git_dir.parent / "mem-hook.log"
    stamp = _dt.datetime.now().isoformat(timespec="seconds")
    try:
        with target.open("a", encoding="utf-8") as stream:
            for error in errors:
                stream.write(f"{stamp} {error}\n")
    except OSError:
        # Losing a log line must never be the reason a commit looks failed.
        pass


DISPATCH = {
    "login": cmd_login,
    "logout": cmd_logout,
    "init": cmd_init,
    "status": cmd_status,
    "flush": cmd_flush,
    "projects": cmd_projects,
    "manifest": cmd_manifest,
    "pull": cmd_pull,
    "export": cmd_export,
    "device": cmd_device,
    "ls": cmd_ls,
    "read": cmd_read,
    "add": cmd_add,
    "rm": cmd_rm,
    "mv": cmd_mv,
    "search": cmd_search,
    "index": cmd_index,
    "devices": cmd_devices,
    "python": cmd_python,
    "hook": cmd_hook,
    "pointer": cmd_pointer,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    human = False
    try:
        # Parsing is inside the `try` on purpose: a usage error raises a
        # `MemError` from `_Parser.error`, and it must leave through the same
        # envelope as every other failure rather than escaping as a traceback.
        args = parser.parse_args(argv)
        if not args.command:
            args.command = "status"
            args.human = getattr(args, "human", False)
        human = getattr(args, "human", False)
        return DISPATCH[args.command](args)
    except MemError as exc:
        # stderr carries exactly one JSON object: an agent must never have to
        # parse prose to learn what happened (invariant C6). The "run mem login"
        # advice already travels inside `detail.hint`. `fail` keeps usage errors
        # on the JSON path even under --human.
        return fail(exc, human)
    except KeyboardInterrupt:  # pragma: no cover
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
