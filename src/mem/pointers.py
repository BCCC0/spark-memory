"""Parsing and safe presentation for device file pointers."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import schema_invalid

_POINTER = re.compile(r"^@(file|dir|locked)\(([^():]+):(.+)\)$")
_ALIAS = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


@dataclass(frozen=True)
class Pointer:
    kind: str
    alias: str
    path: str

    @property
    def canonical(self) -> str:
        return f"@{self.kind}({self.alias}:{self.path})"

    @property
    def summary(self) -> str:
        if self.kind == "locked":
            return f"locked path on {self.alias}"
        return self.canonical


def parse_pointer(value: str, *, allow_unqualified: bool = False, self_alias: str | None = None) -> Pointer:
    """Parse one canonical pointer; Windows drive colons remain part of its path."""
    raw = str(value).strip()
    if raw.startswith(("@file(", "@dir(", "@locked(")) and raw.endswith(")"):
        inner = raw[raw.index("(") + 1 : -1]
        windows_drive = bool(re.match(r"^[A-Za-z]:[\\/]", inner))
        if ":" not in inner or (allow_unqualified and windows_drive):
            if not allow_unqualified:
                raise schema_invalid("project pointers require a device alias: @file(alias:path)")
            alias, path = self_alias or "self", inner
            kind = raw[1 : raw.index("(")]
        else:
            match = _POINTER.fullmatch(raw)
            if not match:
                raise schema_invalid("pointer must use @file(alias:path), @dir(alias:path), or @locked(alias:path)")
            kind, alias, path = match.groups()
    else:
        raise schema_invalid("pointer must use @file(alias:path), @dir(alias:path), or @locked(alias:path)")
    if not _ALIAS.fullmatch(alias):
        raise schema_invalid(f"invalid device alias {alias!r}")
    if alias == "self" and not allow_unqualified:
        raise schema_invalid("'self' is only valid inside a device home; project pointers need an enrolled alias")
    if not path or "\n" in path or "\r" in path or ")" in path:
        raise schema_invalid("pointer path must be a non-empty single line without ')'")
    return Pointer(kind, alias, path)


def pointer_record(text: str) -> Pointer | None:
    """Recognize a pointer record, allowing its canonical trailing newline."""
    raw = text.rstrip("\r\n")
    if "\n" in raw or "\r" in raw:
        return None
    try:
        return parse_pointer(raw)
    except Exception:
        return None
