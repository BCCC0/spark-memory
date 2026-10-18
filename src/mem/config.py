"""Client-side configuration until discovery and credentials.

Three files, per ``MEM-CLI.md`` §2 and ``MEM-SERVER.md`` §3.7:

* ``~/.config/mem/config.toml``   — machine defaults (``server``, ``[read]``)
* ``~/.config/mem/credentials``   — 0600, ``{server, device_id, display_name, token}``
* ``<project>/.mem.toml``         — the project marker, found by walking up

``MEM-RESOLUTIONS.md`` S6 settles the field naming: the credentials file stores
``device_id`` and ``display_name``, and ``device`` is accepted as a read-time
alias for ``device_id``.

Stdlib only — this is imported by the CLI.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import MemError

DEFAULT_SERVER = "http://127.0.0.1:8321"
DEFAULT_MAX_BYTES = 4096
MARKER_NAME = ".mem.toml"

DIRECTIVE_HINT = "run `mem login`"  # surfaced on exit 8


def config_dir() -> Path:
    """Machine config directory. ``MEM_CONFIG_DIR`` overrides it (tests, and
    running two identities on one box)."""
    override = os.environ.get("MEM_CONFIG_DIR")
    if override:
        return Path(override)
    home = os.environ.get("HOME") or os.environ.get("USERPROFILE") or str(Path.home())
    return Path(home) / ".config" / "mem"


@dataclass
class Credentials:
    server: str
    device_id: str
    token: str
    display_name: str = ""
    alias: str = ""

    def to_toml(self) -> str:
        lines = [
            f'server = {_toml_str(self.server)}',
            f'device_id = {_toml_str(self.device_id)}',
            f'display_name = {_toml_str(self.display_name)}',
            f'alias = {_toml_str(self.alias)}',
            f'token = {_toml_str(self.token)}',
        ]
        return "\n".join(lines) + "\n"


@dataclass
class ClientConfig:
    server: str = DEFAULT_SERVER
    max_bytes: int = DEFAULT_MAX_BYTES
    credentials: Credentials | None = None
    project: str | None = None
    marker_path: Path | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def _toml_str(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def read_config_file() -> dict[str, Any]:
    path = config_dir() / "config.toml"
    if not path.exists():
        return {}
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise MemError("invalid_request", f"{path} is not valid TOML: {exc}") from exc


def _write_local(path: Path, text: str, mode: int | None = None) -> None:
    """Write a local config file, turning filesystem errors into typed ones.

    A local ``PermissionError`` escaping as a traceback breaks invariant C6 far
    worse than a wrong exit code would: an agent gets no code and no JSON object
    to read. This is how a real enrollment was lost once — the device was created
    server-side and the one-time token was thrown away by the crash that followed.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if mode is not None:
            handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(text)
        else:
            path.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise MemError(
            "internal_error",
            f"cannot write {path}: {exc.strerror or exc}",
            {
                "path": str(path),
                "hint": "set MEM_CONFIG_DIR to a writable directory",
            },
        ) from exc


def _read_marker_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MemError(
            "internal_error",
            f"cannot read {path}: {exc.strerror or exc}",
            {"path": str(path)},
        ) from exc


def read_marker_sections(path: Path) -> dict[str, Any]:
    """Parse a project marker, tolerating an absent or malformed file.

    Used for read-modify-write so sections this module does not own survive. A
    malformed marker returns ``{}`` rather than raising: the caller is about to
    rewrite it, and losing a corrupt file is better than refusing to work.
    """
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(_read_marker_text(path))
    except (tomllib.TOMLDecodeError, OSError):
        return {}


def _replace_marker_table(text: str, table: str, body: list[str] | None) -> str:
    """Replace (or append, or drop) one ``[table]`` in a marker's text.

    Done as line surgery rather than a TOML re-emit so that comments and fields
    this module does not know about are preserved byte-for-byte. ``body`` is the
    table's lines *without* the header; ``None`` removes the table entirely.
    """
    lines = text.split("\n")
    header = f"[{table}]"
    start = next((i for i, line in enumerate(lines) if line.strip() == header), None)
    if start is not None:
        end = len(lines)
        for i in range(start + 1, len(lines)):
            stripped = lines[i].strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                end = i
                break
        replacement = [] if body is None else [header, *body]
        new_lines = lines[:start] + replacement + lines[end:]
    elif body is None:
        new_lines = lines
    else:
        new_lines = [*lines]
        while new_lines and not new_lines[-1].strip():
            new_lines.pop()
        new_lines += ["", header, *body]
    return "\n".join(new_lines).rstrip("\n") + "\n"


def _toml_str_array(values: list[str]) -> str:
    return "[" + ", ".join(_toml_str(value) for value in values) + "]"


def read_marker_patterns(path: Path) -> list[str]:
    """The declared auto-track globs, in order. Empty when none are set."""
    section = read_marker_sections(path).get("track") or {}
    patterns = section.get("patterns") or []
    if isinstance(patterns, str):
        return [patterns]
    return [str(item) for item in patterns]


def write_marker_patterns(path: Path, patterns: list[str]) -> Path:
    """Set ``[track].patterns``, preserving every other section of the marker."""
    text = _read_marker_text(path) if path.is_file() else ""
    body = [f"patterns = {_toml_str_array([str(p) for p in patterns])}"] if patterns else None
    _write_local(path, _replace_marker_table(text, "track", body))
    return path


def write_config_file(server: str, *, max_bytes: int | None = None) -> Path:
    path = config_dir() / "config.toml"
    lines = [f'server = {_toml_str(server)}', "[read]"]
    lines.append(f"max_bytes = {max_bytes or DEFAULT_MAX_BYTES}")
    _write_local(path, "\n".join(lines) + "\n")
    return path


def read_credentials() -> Credentials | None:
    path = config_dir() / "credentials"
    if not path.exists():
        return None
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise MemError("invalid_request", f"{path} is not valid TOML: {exc}") from exc
    device_id = data.get("device_id") or data.get("device") or ""
    if not (data.get("server") and data.get("token")):
        raise MemError(
            "unauthorized",
            f"{path} is missing server or token — run `mem login` again",
        )
    return Credentials(
        server=str(data["server"]),
        device_id=str(device_id),
        token=str(data["token"]),
        display_name=str(data.get("display_name", "")),
        alias=str(data.get("alias", "")),
    )


def write_credentials(creds: Credentials) -> Path:
    path = config_dir() / "credentials"
    # Created with 0600 from the start rather than chmod-ed after the write.
    _write_local(path, creds.to_toml(), mode=0o600)
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - Windows
        pass
    return path


def delete_credentials() -> bool:
    path = config_dir() / "credentials"
    try:
        if path.exists():
            path.unlink()
            return True
    except OSError as exc:
        raise MemError(
            "internal_error", f"cannot delete {path}: {exc.strerror or exc}"
        ) from exc
    return False


def find_marker(start: Path | None = None) -> tuple[Path, dict[str, Any]] | None:
    """Walk up from ``start`` to the nearest ``.mem.toml`` — true git behaviour."""
    current = (start or Path.cwd()).resolve()
    for directory in [current, *current.parents]:
        candidate = directory / MARKER_NAME
        if candidate.is_file():
            try:
                with candidate.open("rb") as handle:
                    return candidate, tomllib.load(handle)
            except tomllib.TOMLDecodeError as exc:
                raise MemError(
                    "invalid_request", f"{candidate} is not valid TOML: {exc}"
                ) from exc
    return None


def write_marker(directory: Path, project: str, server: str | None = None) -> Path:
    path = directory / MARKER_NAME
    lines = [f'project = {_toml_str(project)}']
    if server:
        lines.append(f'server = {_toml_str(server)}')
    _write_local(path, "\n".join(lines) + "\n")
    return path


def load(*, require_project: bool = False, require_auth: bool = True) -> ClientConfig:
    """Assemble the effective client configuration, with the CLI's precedence:
    credentials < config.toml < environment < the project marker's override."""
    config = ClientConfig()
    file_config = read_config_file()
    if file_config.get("server"):
        config.server = str(file_config["server"])
    read_section = file_config.get("read") or {}
    if read_section.get("max_bytes"):
        config.max_bytes = int(read_section["max_bytes"])
    if os.environ.get("MEM_SERVER"):
        config.server = os.environ["MEM_SERVER"]
    config.raw = file_config

    creds = read_credentials()
    config.credentials = creds
    if creds:
        config.server = creds.server

    marker = find_marker()
    if marker:
        marker_path, marker_data = marker
        config.marker_path = marker_path
        config.project = marker_data.get("project")
        if marker_data.get("server"):
            config.server = str(marker_data["server"])

    if require_project:
        if not config.project:
            raise MemError(
                "no_project",
                "no .mem.toml found in this directory or any parent",
                {"hint": "run `mem init <project>` here"},
            )
        if not config.marker_path:
            config.marker_path = Path.cwd() / MARKER_NAME

    if require_auth and not creds:
        raise MemError(
            "unauthorized",
            "this device is not enrolled",
            {"hint": DIRECTIVE_HINT},
        )
    return config
