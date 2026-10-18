"""Frontmatter: parse, validate, and inject — ``MEM-SCHEMA.md`` §3 and §6.

Validation is strict where the schema says *error* (exit ``4``) and lenient where
it says *warning*. The distinction matters: an unknown or **derived** key is an
error because a stored copy of a derived value can silently disagree with the
truth (``MEM.md`` invariant C3).
"""

from __future__ import annotations

import re
from typing import Any

import yaml

from ..errors import MemError, schema_invalid
from ..ids import validate_id
from ..summary import parse_frontmatter, validate_summary

SUMMARY_MAX = 200

STATUSES = ("draft", "active", "done", "superseded")
TYPES = ("adr", "spec", "decision", "session", "runbook", "reference", "project")

REQUIRED_AUTHORED = ("summary", "status", "type")
OPTIONAL_AUTHORED = ("tags", "supersedes", "superseded_by")
TOOL_INJECTED = ("origin", "attachments")

# Rejected outright: these are computed or come from git/the path.
DERIVED_KEYS = frozenset(
    {
        "id",
        "title",
        "project",
        "created",
        "updated",
        "author",
        "device",
        "sha256",
        "url",
        "bytes",
        "line_count",
        "permalink",
        "date",
    }
)

ALLOWED_KEYS = frozenset(REQUIRED_AUTHORED + OPTIONAL_AUTHORED + TOOL_INJECTED)

REQUIRED_SECTIONS: dict[str, tuple[str, ...]] = {
    "adr": ("Context", "Decision", "Consequences"),
    "spec": ("Goal", "Non-goals", "Open decisions"),
    "decision": ("Decision", "Rationale"),
    "session": ("What happened", "Commits", "Lessons"),
    "runbook": ("Prerequisites", "Steps", "Verification"),
    "reference": (),
    "project": ("Scope",),
}

_DELIM = re.compile(r"^---\s*$")
_LINK = re.compile(r"\]\(\s*([^)\s]+)\s*\)")

#: ``[[...]]`` — the Obsidian / Basic Memory wikilink convention, which mem does
#: **not** parse. Detected only so that a reference written this way is *reported*
#: instead of silently ignored (S34).
_WIKILINK = re.compile(r"\[\[[^\]]+\]\]")


#: Schemes and prefixes that are never note references.
_NOT_A_REFERENCE = ("http://", "https://", "mailto:", "#")


def _is_path_like(target: str) -> bool:
    """A filesystem path, not a note id (`S35` D3).

    A reference is an id **relative to the project root** — the same from any
    directory, on any device. `./x` and `../x` are relative to something the system
    cannot know (the reader's pwd, or the referring file's own directory), so they are
    not references. They are *reported* rather than silently treated as a missing note.
    """
    return target.startswith(("/", "./", "../"))


def note_refs(body: str) -> list[tuple[str, str | None]]:
    """``(id, anchor)`` for every markdown link that is a note reference.

    ``MEM-SCHEMA.md`` §4: links between notes are markdown links to other ids
    (``[the fan verdict](ops/spark-fan-ro.md)``). External URLs, bare fragments and
    paths are not references. The anchor is now **kept** instead of discarded: `#` means
    *section* in a read, so it must mean the same in a link (`S35` D1).
    """
    found: list[tuple[str, str | None]] = []
    for raw in _LINK.findall(body):
        if raw.startswith(_NOT_A_REFERENCE) or _is_path_like(raw):
            continue
        target, _, anchor = raw.partition("#")
        target = target.strip()
        if target.endswith(".md"):
            target = target[:-3]
        target = target.strip("/")
        if not target:
            continue
        reference = (target, anchor.strip() or None)
        if reference not in found:
            found.append(reference)
    return found


def bad_refs(body: str) -> list[str]:
    """Path-shaped link targets, which cannot be resolved as references (`S35` D3)."""
    bad: list[str] = []
    for raw in _LINK.findall(body):
        if raw.startswith(_NOT_A_REFERENCE):
            continue
        if _is_path_like(raw) and raw not in bad:
            bad.append(raw)
    return bad


def note_links(body: str) -> list[str]:
    """The ids this note links to — `note_refs` without the anchors."""
    return [target for target, _ in note_refs(body)]


def parse(text: str) -> tuple[dict[str, Any], str]:
    """Split a note into ``(frontmatter, body)``. Missing frontmatter is ``{}``."""
    return parse_frontmatter(text)


def title_of(body: str) -> str | None:
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
        if line.strip():
            return None
    return None


def _headings(body: str) -> list[str]:
    return [m.group(1).strip() for m in re.finditer(r"^##\s+(.+?)\s*$", body, re.M)]


def validate(data: dict[str, Any], note_id: str) -> list[str]:
    """Raise ``schema_invalid`` on errors; return the list of warnings."""
    # 5. the id itself
    validate_id(note_id)

    # 3. derived keys must never be stored -- checked before the generic
    #    unknown-key rule, so the report names the real problem.
    derived = sorted(set(data) & DERIVED_KEYS)
    if derived:
        raise schema_invalid(
            f"derived key(s) must not appear in frontmatter: {', '.join(derived)}",
            reason="computed at read time (MEM-SCHEMA.md §3.3)",
        )

    # 4. unknown keys
    unknown = sorted(set(data) - ALLOWED_KEYS)
    if unknown:
        raise schema_invalid(
            f"unknown frontmatter key(s): {', '.join(unknown)}",
            allowed=sorted(ALLOWED_KEYS),
        )

    # 1. Type and status are optional in the generalized file catalog. When
    #    authored, they retain the same strict validation as before.
    note_type = data.get("type")
    if note_type is not None and note_type not in TYPES:
        raise schema_invalid(f"type {note_type!r} is not one of {list(TYPES)}")

    status = data.get("status")
    if status is not None and status not in STATUSES:
        raise schema_invalid(f"status {status!r} is not one of {list(STATUSES)}")

    validate_summary(data)

    # 6. supersession must be recorded from both sides
    if status == "superseded" and not data.get("superseded_by"):
        raise schema_invalid("status 'superseded' requires 'superseded_by'")

    tags = data.get("tags")
    if tags is not None and (
        not isinstance(tags, list) or not all(isinstance(t, str) for t in tags)
    ):
        raise schema_invalid("'tags' must be a list of strings")

    return []


def content_warnings(data: dict[str, Any], body: str) -> list[str]:
    """Warnings that need only this note (section, heading and link checks)."""
    warnings: list[str] = []
    if title_of(body) is None:
        warnings.append("first line is not an '# ' heading")
    present = _headings(body)
    for section in REQUIRED_SECTIONS.get(str(data.get("type")), ()):
        if section not in present:
            warnings.append(f"missing required section '## {section}'")
    # `[[wikilinks]]` are NOT parsed (S34). Every migrated Basic Memory note used
    # them, and all three had to be rewritten to `[text](id)` — so they were
    # references that NOTHING could see, which is the silent-failure class this
    # project refuses to ship. Saying so costs one line and one warning; resolving
    # them by title would mean a second grammar whose references break on rename.
    wikilink = _WIKILINK.search(body)
    if wikilink is not None:
        warnings.append(
            f"wikilink {wikilink.group(0)} is not parsed; "
            "reference another note as [text](id)"
        )
    # `./x` and `../x` are relative to something nothing here knows, so they are
    # reported rather than left to surface as a bogus "link does not exist" (S35 D3).
    for target in bad_refs(body):
        warnings.append(
            f"link {target} is a relative path; references are note ids relative to "
            "the project root, the same from any directory"
        )
    return warnings


def serialise(data: dict[str, Any], body: str) -> str:
    """Re-render a note, preserving key order and the body verbatim."""
    front = yaml.safe_dump(
        dict(data), sort_keys=False, allow_unicode=True, default_flow_style=False
    ).rstrip("\n")
    if not body.startswith("\n"):
        body = "\n" + body
    return f"---\n{front}\n---{body}"


def inject(data: dict[str, Any], body: str, updates: dict[str, Any]) -> str:
    """Merge tool-injected keys (``origin``, ``attachments``) and re-render."""
    merged = dict(data)
    merged.update(updates)
    return serialise(merged, body)


def build(
    *,
    title: str,
    summary: str,
    status: str,
    note_type: str,
    body: str = "",
    tags: list[str] | None = None,
) -> str:
    """Compose a note from scratch (used by ``mem init``)."""
    data: dict[str, Any] = {"summary": summary, "status": status, "type": note_type}
    if tags:
        data["tags"] = tags
    text = body if body.startswith("\n") else "\n" + body
    return serialise(data, f"\n# {title}\n{text}")


def require_valid(text: str, note_id: str) -> tuple[dict[str, Any], str, list[str]]:
    """Parse + validate in one step, returning ``(frontmatter, body, warnings)``."""
    data, body = parse(text)
    validate(data, note_id)
    return data, body, content_warnings(data, body)


__all__ = [
    "MemError",
    "SUMMARY_MAX",
    "STATUSES",
    "TYPES",
    "ALLOWED_KEYS",
    "DERIVED_KEYS",
    "REQUIRED_SECTIONS",
    "parse",
    "validate",
    "content_warnings",
    "serialise",
    "inject",
    "build",
    "require_valid",
    "title_of",
    "note_refs",
    "bad_refs",
    "note_links",
]
