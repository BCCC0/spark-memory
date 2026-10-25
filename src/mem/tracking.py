"""Auto-track: which committed paths follow into memory, and how far we have read.

This module is the whole of the *decision* half of ``mem hook``. It is stdlib
only, does no network I/O, and takes a ``git runner`` callable so it can be
tested without a repository.

The shape is deliberately a **watermark**, not "the last commit". A hook that
only ever looked at ``HEAD^..HEAD`` would lose any commit captured while the
server was unreachable, could not catch up after a clone, and would re-do work
every time it ran. Remembering "the last commit we captured" makes ``update``
idempotent, catch-up-capable, and safe to run by hand.
"""

from __future__ import annotations

import fnmatch
import json
import os
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable

from . import config as cfg
from .errors import MemError

__all__ = [
    "TRACK_ACTION",
    "DEFAULT_ACTIONS",
    "matches_patterns",
    "tracked_paths",
    "changed_paths",
    "committed_bytes",
    "is_root_commit",
    "repo_root",
    "state_path",
    "read_watermark",
    "write_watermark",
]

#: The one action that ships today. `update` runs the enabled actions in order,
#: so a second behaviour is a configuration change rather than a new hook body
#: in every cloned repository.
TRACK_ACTION = "track"
DEFAULT_ACTIONS: tuple[str, ...] = (TRACK_ACTION,)

STATE_FILE = "hook-state.json"


# -- pattern matching ------------------------------------------------------


def matches_patterns(patterns: Iterable[str], path: str) -> bool:
    """True when *path* (repo-relative, POSIX separators) is tracked.

    Semantics mirror the useful half of ``.gitignore``:

    * a plain glob matches anywhere in the tree unless it names a directory
      (``*.md`` matches ``deep/a.md``, ``notes/*.md`` does not match
      ``notes/deep/a.md``);
    * an entry prefixed with ``!`` **excludes**, and an exclusion always wins
      over an inclusion;
    * a trailing ``/`` means "everything under this directory";
    * ``**`` matches zero or more path segments, so ``docs/**/*.md`` covers both
      ``docs/a.md`` and ``docs/sub/deep/a.md``.

    Returns ``False`` for an empty pattern list: declaring nothing must mean
    "track nothing", never "track everything".
    """
    includes = [p for p in patterns if p and not p.startswith("!")]
    excludes = [p[1:] for p in patterns if p and p.startswith("!")]
    if not includes:
        return False
    parts = _parts(path)
    if any(_match_one(_normalise(pattern), parts) for pattern in excludes):
        return False
    return any(_match_one(_normalise(pattern), parts) for pattern in includes)


def _parts(path: str) -> list[str]:
    return [part for part in PurePosixPath(path.replace("\\", "/")).parts if part not in ("", "/")]


def _normalise(pattern: str) -> list[str]:
    """A declared glob as path segments, with the friendly shorthands expanded.

    A pattern with no slash is matched anywhere in the tree — the same courtesy
    ``.gitignore`` extends — so ``*.md`` becomes ``**/*.md``. A trailing slash
    becomes a recursive match, so ``docs/`` becomes ``docs/**``.
    """
    cleaned = pattern.replace("\\", "/").strip()
    if cleaned.startswith("./"):
        cleaned = cleaned[2:]
    if cleaned.endswith("/"):
        cleaned = cleaned + "**"
    if not cleaned:
        return []
    parts = [part for part in cleaned.split("/") if part not in ("", ".")]
    if "/" not in cleaned:
        parts = ["**", *parts]
    return parts


def _match_one(pattern: list[str], parts: list[str]) -> bool:
    """Recursive segment match where ``**`` consumes zero or more segments."""
    if not pattern:
        return not parts
    head, rest = pattern[0], pattern[1:]
    if head == "**":
        # Zero segments consumed, or one more consumed and `**` tried again.
        return _match_one(rest, parts) or (bool(parts) and _match_one(pattern, parts[1:]))
    if not parts or not _fnmatch_segment(head, parts[0]):
        return False
    return _match_one(rest, parts[1:])


def _fnmatch_segment(pattern: str, value: str) -> bool:
    return fnmatch.fnmatchcase(value, pattern)


def tracked_paths(patterns: Iterable[str], paths: Iterable[str]) -> list[str]:
    """The subset of *paths* that the patterns select, in the order given."""
    return [path for path in paths if matches_patterns(patterns, path)]


# -- git introspection -----------------------------------------------------


def repo_root(cwd: Path | None = None) -> Path | None:
    """The working-tree toplevel for *cwd*, or ``None`` outside a repository."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(cwd or Path.cwd()),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return Path(value) if value else None


def _default_runner(repo: Path) -> Callable[..., subprocess.CompletedProcess]:
    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], cwd=str(repo), capture_output=True, check=False
        )

    return run


def is_root_commit(rev: str, repo: Path, *, runner: Callable | None = None) -> bool:
    """True when *rev* has no parent (the first commit in a history)."""
    run = runner or _default_runner(repo)
    result = run("rev-list", "--max-parents=0", rev)
    if result.returncode != 0:
        return False
    roots = result.stdout.decode("utf-8", "replace").split()
    return rev in roots or (len(roots) == 1 and bool(roots[0]))


def changed_paths(
    rev: str,
    repo: Path,
    *,
    since: str | None = None,
    runner: Callable | None = None,
) -> list[str]:
    """Repo-relative paths touched by *rev*, or by ``since..rev`` when given.

    ``-z`` is not a stylistic choice: without it git quotes and escapes paths
    containing spaces, quotes or non-ASCII, and a quoted path silently fails to
    match any pattern.
    """
    run = runner or _default_runner(repo)
    if since is not None and since == rev:
        # An empty range means "nothing new since the last capture". Asking git
        # for `<rev>..<rev>` would also work, but returning early keeps the
        # common no-op case from spawning a subprocess at all.
        return []
    args = ["diff-tree", "--no-commit-id", "--name-only", "-r", "-z", "--root"]
    if since:
        args.append(f"{since}..{rev}")
    else:
        args.append(rev)
    result = run(*args)
    if result.returncode != 0:
        raise MemError(
            "invalid_request",
            f"cannot read changed paths for {rev}",
            {"rev": rev, "repo": str(repo)},
        )
    raw = result.stdout.decode("utf-8", "replace")
    return [item for item in raw.split("\0") if item]


def committed_bytes(rev: str, path: str, repo: Path, *, runner: Callable | None = None) -> bytes:
    """The blob for *path* **as committed at** *rev*.

    This is the point of the whole exercise: the working tree may hold an edit
    that was never committed, and memory must record what git recorded.
    """
    run = runner or _default_runner(repo)
    result = run("show", f"{rev}:{path}")
    if result.returncode != 0:
        raise MemError(
            "not_found",
            f"{path} does not exist at {rev}",
            {"rev": rev, "path": path},
        )
    return result.stdout


def committed_bytes_many(
    rev: str, paths: Iterable[str], repo: Path
) -> dict[str, bytes | None]:
    """Read many committed blobs with one git cat-file --batch process.

    A hook batch can carry 1,000 files; one git show per file dominated the
    post-commit hook. Missing paths map to None so the caller can report them
    together.
    """
    wanted = list(dict.fromkeys(paths))
    if not wanted:
        return {}
    payload = ("\n".join(f"{rev}:{path}" for path in wanted) + "\n").encode("utf-8")
    result = subprocess.run(
        ["git", "cat-file", "--batch"],
        cwd=str(repo),
        input=payload,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise MemError(
            "not_found",
            f"cannot read {len(wanted)} path(s) at {rev}",
            {"rev": rev},
        )
    raw = result.stdout
    position = 0
    bodies: dict[str, bytes | None] = {}
    for path in wanted:
        newline = raw.find(b"\n", position)
        if newline < 0:
            raise MemError("internal_error", "truncated output from git cat-file")
        header = raw[position:newline].decode("utf-8", "replace")
        parts = header.rsplit(" ", 2)
        if len(parts) == 3 and parts[1] == "blob" and parts[2].isdigit():
            size = int(parts[2])
            start = newline + 1
            bodies[path] = raw[start:start + size]
            position = start + size + 1
        else:
            bodies[path] = None
            position = newline + 1
    return bodies


# -- watermark -------------------------------------------------------------


def state_path() -> Path:
    """Where capture watermarks live, alongside the rest of this client's state."""
    return cfg.config_dir() / STATE_FILE


def read_watermark(repo: Path) -> str | None:
    """The last captured commit for *repo*, or ``None`` if it has never run."""
    data = _read_state()
    entry = (data.get("repos") or {}).get(_repo_key(repo)) or {}
    value = entry.get("last")
    return str(value) if value else None


def write_watermark(repo: Path, rev: str) -> None:
    """Record that *rev* has been captured.

    Written atomically: a hook killed mid-run must not leave a half-written
    state file, because a corrupt watermark would silently re-capture or skip.
    """
    data = _read_state()
    repos = data.setdefault("repos", {})
    repos[_repo_key(repo)] = {
        "last": str(rev),
        "path": str(repo),
        "updated": _now_iso(),
    }
    data["version"] = 1
    target = state_path()
    tmp = target.with_suffix(".json.tmp")
    _write_text(target.parent, tmp, json.dumps(data, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, target)


def _repo_key(repo: Path) -> str:
    try:
        resolved = repo.resolve()
    except OSError:  # pragma: no cover - unreadable mount
        resolved = repo
    return resolved.as_posix()


def _read_state() -> dict[str, Any]:
    path = state_path()
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # A corrupt watermark is recoverable by re-running; refusing to run at
        # all would strand the hook with no way forward.
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _write_text(parent: Path, path: Path, text: str) -> None:
    try:
        parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise MemError(
            "internal_error",
            f"cannot write {path}: {exc.strerror or exc}",
            {"path": str(path), "hint": "set MEM_CONFIG_DIR to a writable directory"},
        ) from exc


def _now_iso() -> str:
    import datetime as _dt

    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
