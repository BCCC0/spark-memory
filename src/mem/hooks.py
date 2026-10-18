"""Installing and removing the auto-track hook in a repository.

The installed file is deliberately **dumb**: it resolves the current revision and
delegates to ``mem hook update``. All behaviour lives in the CLI, so adding or
changing an action never requires re-installing hooks in every clone -- and a
clone that already carries the shim starts honouring a new action immediately.

The block is marked so it can be replaced idempotently and removed cleanly, and
so a hook the user already had is preserved rather than overwritten.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from .errors import MemError

__all__ = [
    "MARK_BEGIN",
    "MARK_END",
    "HOOK_EVENTS",
    "hook_dir",
    "hook_path",
    "install",
    "uninstall",
    "status",
]

MARK_BEGIN = "# >>> mem auto-track >>>"
MARK_END = "# <<< mem auto-track <<<"

#: Pre-commit validates staged files locally; the later hooks sync to the service.
HOOK_EVENTS: tuple[str, ...] = ("pre-commit", "post-commit", "post-merge")


def hook_dir(repo: Path) -> Path | None:
    """The directory git actually reads hooks from, honouring ``core.hooksPath``."""
    git_dir = _git_dir(repo)
    if git_dir is None:
        return None
    configured = _git(repo, "config", "--get", "core.hooksPath")
    if configured:
        candidate = Path(configured)
        if not candidate.is_absolute():
            candidate = (repo / candidate).resolve()
        return candidate
    return git_dir / "hooks"


def hook_path(repo: Path, event: str) -> Path | None:
    directory = hook_dir(repo)
    return None if directory is None else directory / event


def status(repo: Path) -> dict[str, object]:
    """What `mem hook ls` reports: where the hook would live and what is installed."""
    directory = hook_dir(repo)
    installed = []
    if directory is not None:
        for event in HOOK_EVENTS:
            target = directory / event
            if target.is_file() and MARK_BEGIN in _read(target):
                installed.append(event)
    return {"hook_dir": str(directory) if directory else None, "installed": installed}


def install(repo: Path) -> dict[str, object]:
    """Insert (or refresh) the marked block in every hook event we use.

    Idempotent: the existing block is replaced, never duplicated, and anything
    the user already had in the file is preserved.
    """
    directory = hook_dir(repo)
    if directory is None:
        raise MemError(
            "invalid_request",
            f"{repo} is not a git repository",
            {"repo": str(repo)},
        )
    if shutil.which("git") is None:  # pragma: no cover - git is how we got here
        raise MemError("internal_error", "git is not on PATH")
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise MemError(
            "internal_error",
            f"cannot create {directory}: {exc.strerror or exc}",
            {"path": str(directory)},
        ) from exc

    written = []
    for event in HOOK_EVENTS:
        target = directory / event
        existing = _read(target) if target.is_file() else None
        body = _merge(existing, _block(repo, event))
        _write(target, body)
        _make_executable(target)
        written.append(str(target))
    return {"hook_dir": str(directory), "installed": [Path(p).name for p in written], "files": written}


def uninstall(repo: Path) -> dict[str, object]:
    """Remove only our block, leaving any user hook exactly as it was."""
    directory = hook_dir(repo)
    if directory is None:
        raise MemError("invalid_request", f"{repo} is not a git repository", {"repo": str(repo)})
    removed = []
    for event in HOOK_EVENTS:
        target = directory / event
        if not target.is_file():
            continue
        text = _read(target)
        if MARK_BEGIN not in text:
            continue
        remainder = _strip(text)
        if _is_effectively_empty(remainder):
            # The file was ours alone; remove it rather than leave an empty hook
            # (or one holding nothing but the shebang we added).
            try:
                target.unlink()
            except OSError as exc:  # pragma: no cover - permissions
                raise MemError(
                    "internal_error",
                    f"cannot remove {target}: {exc.strerror or exc}",
                    {"path": str(target)},
                ) from exc
        else:
            _write(target, remainder)
        removed.append(event)
    return {"hook_dir": str(directory), "removed": removed}


# -- internals -------------------------------------------------------------


# BUG(medium): the generated shim pins sys.executable at install time and
# always invokes "<interpreter>" -m mem.cli for all three hook events.
# Why valid: if that virtualenv is moved or deleted the hook can invoke neither
# the CLI nor _log_hook (the helper in cli.py), so the documented "a hook's
# silence is still diagnosable" property (the _log_hook docstring) does not hold
# -- silence becomes indistinguishable from a capture that never ran.
# Fix hint: emit the invocation from one helper that resolves `mem` at run time
# -- prefer a `mem` found on PATH, fall back to the absolute interpreter, and
# use [sys.executable] alone when getattr(sys, "frozen", False) -- and use it
# for all three hook events.
def _block(repo: Path, event: str) -> str:
    """The shim to install, pinned to *this* interpreter.

    The interpreter is resolved at install time rather than relying on ``mem``
    being on ``PATH``: the WSL client and the Windows client would otherwise
    install the same text and one of them would fail. Everything is quoted,
    because a checkout path with a space is normal on Windows.

    Paths are written with forward slashes even on Windows. The shim is POSIX
    ``sh``, where a backslash inside double quotes is an escape character --
    ``"D:\\src\\python.exe"`` would be mangled into ``D:srcpython.exe`` before the
    shell ever tried to run it. Git for Windows accepts forward slashes, so one
    form is correct on both platforms.
    """
    interpreter = (sys.executable or "python").replace("\\", "/")
    if event == "pre-commit":
        return "\n".join([
            MARK_BEGIN,
            f'"{interpreter}" -m mem.cli hook check --repo "{repo.as_posix()}" || exit $?',
            MARK_END,
        ])
    return "\n".join(
        [
            MARK_BEGIN,
            "# Runs synchronously, and deliberately so. Backgrounding this with `&`",
            "# looked free but is not: git reaps the process group when the hook",
            "# returns, so a capture that has not finished is killed. Against a fast",
            "# local daemon it happened to win the race; against a remote host it",
            "# lost it, and the loss was silent -- no record, no log line.",
            "# The cost of waiting is one round trip per commit, which is the price",
            "# of a capture that actually happens.",
            'if command -v git >/dev/null 2>&1; then',
            f'  _mem_repo="{repo.as_posix()}"',
            '  if [ -n "$_mem_repo" ]; then',
            f'    "{interpreter}" -m mem.cli hook update --repo "$_mem_repo" >/dev/null 2>&1 || true',
            "  fi",
            "  unset _mem_repo",
            "fi",
            MARK_END,
        ]
    )


def _merge(existing: str | None, block: str) -> str:
    """The file content after installing *block*, preserving anything already there."""
    if existing is None or not existing.strip():
        # A brand-new hook still needs a shebang to be executed by git.
        return "#!/bin/sh\n" + block + "\n"
    stripped = _strip(existing)
    if stripped.strip():
        return stripped.rstrip("\n") + "\n" + block + "\n"
    # The file existed but held only a shebang (or only our old block).
    head = stripped if stripped.startswith("#!") or stripped.strip() else "#!/bin/sh\n"
    if not head.strip():
        head = "#!/bin/sh\n"
    return head.rstrip("\n") + "\n" + block + "\n"


def _strip(text: str) -> str:
    """*text* with our marked block removed, including its surrounding blank lines."""
    lines = text.split("\n")
    kept: list[str] = []
    inside = False
    for line in lines:
        if line.strip() == MARK_BEGIN:
            inside = True
            continue
        if line.strip() == MARK_END:
            inside = False
            continue
        if not inside:
            kept.append(line)
    # Collapse a trailing run of blank lines the block may have left behind.
    while len(kept) > 1 and not kept[-1].strip() and not kept[-2].strip():
        kept.pop()
    return "\n".join(kept).rstrip("\n") + "\n" if any(line.strip() for line in kept) else ""


def _is_effectively_empty(text: str) -> bool:
    """True when *text* holds nothing worth keeping — blank, or only a shebang."""
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#!"):
            continue
        return False
    return True


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MemError(
            "internal_error",
            f"cannot read {path}: {exc.strerror or exc}",
            {"path": str(path)},
        ) from exc


def _write(path: Path, text: str) -> None:
    try:
        path.write_text(text, encoding="utf-8", newline="\n")
    except OSError as exc:
        raise MemError(
            "internal_error",
            f"cannot write {path}: {exc.strerror or exc}",
            {"path": str(path)},
        ) from exc


def _make_executable(path: Path) -> None:
    try:
        mode = path.stat().st_mode
        path.chmod(mode | 0o111)
    except OSError:  # pragma: no cover - Windows, where this is a no-op
        pass


def _git(repo: Path, *args: str) -> str:
    import subprocess

    try:
        result = subprocess.run(
            ["git", *args], cwd=str(repo), capture_output=True, text=True, check=False
        )
    except OSError:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _git_dir(repo: Path) -> Path | None:
    value = _git(repo, "rev-parse", "--absolute-git-dir")
    return Path(value) if value else None
