"""Safe initialization of the private per-device home."""

from __future__ import annotations

import os
import platform
import re
import subprocess
from pathlib import Path

from .errors import MemError

_SYNC_NAMES = {"onedrive", "dropbox", "google drive", "googledrive", "icloud drive", "icloud"}


def _run_git(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=False)
    except OSError as exc:
        raise MemError("internal_error", f"cannot inspect git configuration: {exc}") from exc


def _check_unsynced_home(home: Path) -> None:
    normalized = str(home.resolve()).casefold()
    parts = {part.casefold() for part in home.parts}
    synced_env = any(
        normalized == str(Path(value).resolve()).casefold()
        or normalized.startswith(str(Path(value).resolve()).casefold() + os.sep)
        for name in ("OneDrive", "Dropbox", "GOOGLE_DRIVE")
        if (value := os.environ.get(name))
    )
    # BUG(medium): the sync-service check also does a raw substring test over
    # the whole path, not just over path components.
    # Why valid: `any(name in normalized for name in _SYNC_NAMES)` matches
    # "dropbox" or "icloud" anywhere in the resolved path, so a home such as
    # /home/me/dropbox-notes or /srv/icloud-backup is refused even though no
    # path component is a sync folder; the component set `parts` already does
    # the correct check on the same line.
    # Fix hint: drop the substring test and match sync-service names as path
    # components only.
    if synced_env or any(name in parts for name in _SYNC_NAMES) or any(name in normalized for name in _SYNC_NAMES):
        raise MemError("invalid_request", f"home directory appears cloud-synced: {home}; choose a private unsynced home before initializing device data")


def _ensure_global_ignore(home: Path) -> Path:
    configured = _run_git(["config", "--global", "--get", "core.excludesFile"], cwd=home)
    ignore = Path(configured.stdout.strip()).expanduser() if configured.returncode == 0 and configured.stdout.strip() else home / ".config" / "git" / "ignore"
    ignore.parent.mkdir(parents=True, exist_ok=True)
    current = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
    if not any(line.strip() in {"/.device/", ".device/"} for line in current.splitlines()):
        with ignore.open("a", encoding="utf-8") as stream:
            if current and not current.endswith("\n"):
                stream.write("\n")
            stream.write(".device/\n")
    if configured.returncode != 0:
        set_result = _run_git(["config", "--global", "core.excludesFile", str(ignore)], cwd=home)
        if set_result.returncode:
            raise MemError("internal_error", f"cannot set global Git ignore file: {set_result.stderr.strip()}")
    return ignore


def _reject_tracked_path(home: Path) -> None:
    root = _run_git(["rev-parse", "--show-toplevel"], cwd=home)
    if root.returncode:
        return
    repo = Path(root.stdout.strip()).resolve()
    target = home / ".device"
    try:
        relative = target.relative_to(repo)
    except ValueError:
        return
    tracked = _run_git(["ls-files", "--", relative.as_posix()], cwd=repo)
    if tracked.stdout.strip():
        raise MemError("invalid_request", "~/.device/ is already tracked by Git; refusing to write device data")


# BUG(medium): _ensure_global_ignore mutates global git config before the
# identity check that can refuse the run, so a refusal leaves the global config
# already changed.
# Why valid: _ensure_global_ignore(home) writes .device/ into the global excludes
# file and may run `git config --global core.excludesFile ...`, while the
# identity check that compares the existing DEVICE.md fields can still raise
# "this is not this device's home"; the side effect is not rolled back.
# Fix hint: perform the identity check before any global mutation.
def initialize_device_home(*, alias: str, device_id: str) -> Path:
    home = Path.home().resolve()
    _check_unsynced_home(home)
    target_dir = home / ".device"
    if target_dir.is_symlink():
        raise MemError("invalid_request", "~/.device is a symlink; refusing to write outside the device home")
    _reject_tracked_path(home)
    _ensure_global_ignore(home)
    target = target_dir / "DEVICE.md"
    # BUG(medium): the device identity is bound to platform.node(), which is
    # unstable across DHCP leases and domain renames.
    # Why valid: the value is written into the DEVICE.md header below and
    # compared against the identity check's hostname field on every later run,
    # so a machine whose hostname or network name changes fails the equality
    # test and the genuine device home refuses to initialize.
    # Fix hint: key the identity to a stable machine identifier, or compare
    # hostname only as a non-fatal diagnostic.
    hostname = platform.node()
    if target.exists():
        try:
            content = target.read_text(encoding="utf-8")
            identity = dict(re.findall(r"(?m)^([a-z_]+):\s*(.*?)\s*$", content.split("---", 2)[1]))
        except Exception as exc:
            raise MemError("invalid_request", f"cannot verify existing {target}: {exc}") from exc
        if (identity.get("device") != alias or identity.get("device_id") != device_id
                or identity.get("hostname") != hostname):
            raise MemError("invalid_request", "this is not this device's home")
        return target
    target_dir.mkdir(parents=True, exist_ok=True)
    header = (
        f"---\ndevice: {alias}\ndevice_id: {device_id}\nhostname: {hostname}\n"
        f"os: {platform.system().lower()}\narch: {platform.machine()}\nscope: device\nshare: never\n---\n"
        "\n# Device home\n\nDevice-specific facts and pointers for this machine.\n"
    )
    try:
        with target.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(header)
    except FileExistsError:
        raise MemError("invalid_request", "DEVICE.md appeared during initialization; refusing to overwrite it") from None
    except OSError as exc:
        raise MemError("internal_error", f"cannot write {target}: {exc}") from exc
    return target


def default_alias(hostname: str) -> str:
    value = re.sub(r"[^a-z0-9-]+", "-", hostname.lower()).strip("-")
    value = re.sub(r"-+", "-", value)[:40].strip("-")
    return value or "device"
