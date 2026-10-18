"""Cross-platform path translation for a *working file* recorded by another OS.

A note's ``origin.path`` is stored in the **uploading client's** path space: a
note added from Windows records ``D:\\src\\x.md``, and one added from WSL records
``/mnt/d/src/x.md``. Those name the same bytes, but neither is openable by the
other client. This module is the whole of the translation, and it is deliberately
small, pure, and I/O-free so it can be tested without a live pairing.

Two rules matter more than the conversions themselves:

* **Never fabricate a path.** An unmapped drive (``/mnt/e/...`` on a box with no
  E:) or any unrecognised shape resolves to ``None``. The caller then reports the
  committed revision instead of handing out a location that does not exist.
* **The path is a hint, not an identity.** Divergence is decided by hashing the
  file, which is path-independent. Translation only decides whether a *usable*
  path can be offered at all.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath

__all__ = [
    "looks_like_windows_path",
    "looks_like_wsl_path",
    "to_windows",
    "to_wsl",
    "translate",
    "resolve_origin",
]

# ``D:`` / ``D:\`` / ``D:/`` — an explicit Windows drive.
_DRIVE_RE = re.compile(r"^([A-Za-z]):(?:[\\/]|$)")

# WSL's automount convention for Windows drives: ``/mnt/<letter>/...``.
_WSL_MOUNT_RE = re.compile(r"^/mnt/([A-Za-z])(?:/|$)")

# ``/mnt/c`` itself, with or without a trailing slash and nothing under it.
_WSL_BARE_MOUNT_RE = re.compile(r"^/mnt/([A-Za-z])/?$")


def looks_like_windows_path(value: str) -> bool:
    """True when *value* names a Windows drive — i.e. drive letter, not case."""
    return bool(_DRIVE_RE.match(value.strip()))


def looks_like_wsl_path(value: str) -> bool:
    """True when *value* is under WSL's ``/mnt/<letter>`` automount."""
    return bool(_WSL_MOUNT_RE.match(value.strip().replace("\\", "/")))


def to_windows(value: str | Path) -> str | None:
    """``/mnt/d/src/x`` -> ``D:\\src\\x``. ``None`` when it is not a WSL mount."""
    raw = str(value).strip()
    if looks_like_windows_path(raw):
        return str(PureWindowsPath(raw).expanduser() if "~" in raw else PureWindowsPath(raw))
    posix = raw.replace("\\", "/")
    match = _WSL_MOUNT_RE.match(posix)
    if not match:
        return None
    drive = match.group(1).upper()
    parts = PurePosixPath(posix).parts
    # parts == ('/', 'mnt', 'd', 'src', 'x') -> drop '/', 'mnt', and the letter.
    tail = [part for part in parts[3:]]
    return str(PureWindowsPath(f"{drive}:/").joinpath(*tail)) if tail else f"{drive}:\\"


def to_wsl(value: str | Path) -> str | None:
    """``D:\\src\\x`` -> ``/mnt/d/src/x``. ``None`` when it is not a drive path."""
    raw = str(value).strip()
    if looks_like_wsl_path(raw):
        return raw.replace("\\", "/")
    match = _DRIVE_RE.match(raw)
    if not match:
        return None
    drive = match.group(1).lower()
    windows = PureWindowsPath(raw)
    tail = list(windows.parts[1:])
    if not tail:
        return f"/mnt/{drive}"
    return str(PurePosixPath(f"/mnt/{drive}").joinpath(*tail))


def translate(value: str | Path) -> str | None:
    """The same path expressed in the *current* platform's path space.

    Returns ``None`` when the value does not name a path this platform can
    express — which is not an error, it is a caller that should fall back to the
    committed revision.
    """
    raw = str(value)
    if sys.platform == "win32":
        return str(PureWindowsPath(raw)) if looks_like_windows_path(raw) else to_windows(raw)
    return raw.replace("\\", "/") if looks_like_wsl_path(raw) else to_wsl(raw)


def resolve_origin(value: str | Path | None) -> Path | None:
    """The path to a caller's working file, or ``None`` if there isn't one.

    Tries the value as given, then its translation, and accepts only a real file.
    A directory, a missing path, an unmapped drive, or an unreadable location all
    yield ``None`` so the caller reports the committed revision rather than a
    dangling location. Never raises: a read must not fail because a hint is odd.
    """
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    candidates: list[Path] = []
    original = Path(raw).expanduser()
    candidates.append(original)
    translated = translate(raw)
    if translated:
        translated_path = Path(translated).expanduser()
        if translated_path != original:
            candidates.append(translated_path)
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None
