"""Note ids and the path rules — ``MEM-SCHEMA.md`` §2.

An **id is the path inside the project, without the ``.md`` extension**, so
``projects/example-project/ops/spark-fan-ro.md`` has id ``ops/spark-fan-ro``. Because
``id ≡ path``, a rename changes the id, which is why ``doctor`` must flag broken
links.

The rules are strict on purpose: a violation is an **error** (exit ``4``), not
something the tool silently cleans up. Per ``MEM-RESOLUTIONS.md`` B5 the CLI does
**not** transliterate ``My Notes/Foo Bar.md`` — it refuses it and asks for
``--as``.

This module is dependency-free so the CLI can validate locally without importing
anything the server needs.
"""

from __future__ import annotations

import posixpath

from .errors import schema_invalid

MAX_DIR = 40
MAX_FILE = 80
EM_DASH = "\u2014"

# The project's own note is literally ``PROJECT.md`` (MEM-SCHEMA.md §1), which
# cannot satisfy the lowercase rule. It is the single reserved exception.
RESERVED_PROJECT_NOTE = "PROJECT"

_ALLOWED = "abcdefghijklmnopqrstuvwxyz0123456789-."


def _why_not(segment: str) -> str:
    """Explain, specifically, why a segment is illegal — a vague error here
    costs the user a round-trip."""
    if any(ch.isupper() for ch in segment):
        return "contains uppercase (ids are lowercase)"
    for ch in segment:
        if ch == EM_DASH:
            return "contains an em dash \u2014 (use a plain '-')"
        if ch in " _":
            return f"contains {'a space' if ch == ' ' else 'an underscore'}"
        if ch == "/":
            return "contains '/', which separates segments"
        if ch not in _ALLOWED:
            return f"contains {ch!r}, which is not allowed"
    if segment.startswith("-"):
        return "starts with '-'"
    if segment.endswith("-"):
        return "ends with '-'"
    return "is not a valid segment"


def validate_segment(segment: str, *, limit: int, kind: str = "segment") -> str:
    """Validate one path segment, returning it unchanged. Raises on violation."""
    if not segment:
        raise schema_invalid(f"empty {kind}")
    if segment in (".", ".."):
        raise schema_invalid(f"{kind} may not be {segment!r}")
    if segment.startswith(".") or segment.endswith(".") or ".." in segment:
        raise schema_invalid(f"{kind} may not start/end with a dot or contain '..'")
    if len(segment) > limit:
        raise schema_invalid(
            f"{kind} {segment!r} is {len(segment)} chars; the limit is {limit}"
        )
    bad = _why_not(segment)
    if bad != "is not a valid segment":
        raise schema_invalid(f"{kind} {segment!r} is invalid: {bad}")
    return segment


def validate_id(note_id: str) -> str:
    """Validate a whole note id (``dir/dir/file``). Returns the normalised id."""
    if note_id is None:
        raise schema_invalid("missing id")
    candidate = str(note_id).replace("\\", "/").strip("/")
    if not candidate:
        raise schema_invalid("empty id")
    if candidate.startswith("/"):
        raise schema_invalid(f"id {note_id!r} is absolute")
    if candidate == RESERVED_PROJECT_NOTE:
        return candidate  # the project's own note, e.g. projects/<p>/PROJECT.md
    segments = candidate.split("/")
    for index, segment in enumerate(segments):
        is_file = index == len(segments) - 1
        validate_segment(
            segment, limit=MAX_FILE if is_file else MAX_DIR, kind="filename" if is_file else "directory"
        )
    return candidate


def validate_project(project: str) -> str:
    """A project is a single directory name under ``projects/``."""
    if project is None or not str(project).strip():
        raise schema_invalid("missing project")
    candidate = str(project).strip()
    if "/" in candidate or "\\" in candidate:
        raise schema_invalid(f"project {project!r} must be a single name")
    return validate_segment(candidate, limit=MAX_DIR, kind="project name")


def id_from_path(path: str) -> str:
    """Derive the default id from a client-side file path.

    Directories are preserved and ``.md`` is stripped. **Nothing else is
    changed** — no lowercasing, no space-to-hyphen. A path that does not already
    satisfy :func:`validate_id` is refused upstream.
    """
    candidate = str(path).replace("\\", "/")
    while candidate.startswith("./"):
        candidate = candidate[2:]
    if candidate.lower().endswith(".md"):
        candidate = candidate[:-3]
    return candidate


def note_relpath(project: str, note_id: str) -> str:
    """Repo-relative path of a note, with traversal safety applied.

    Every write endpoint goes through here: the server accepts a
    client-supplied path and writes it into its own working tree, so an
    unvalidated path is a directory traversal (``MEM-SERVER.md`` §8).
    """
    project = validate_project(project)
    note_id = validate_id(note_id)
    rel = posixpath.join("projects", project, f"{note_id}.md")
    normalised = posixpath.normpath(rel)
    if normalised.startswith("..") or normalised.startswith("/"):
        raise schema_invalid(f"path escapes the repository: {rel!r}")
    return normalised


def text_relpath(project: str, file_id: str) -> str:
    """Repo-relative path of an inline text file, retaining its extension."""
    project = validate_project(project)
    file_id = validate_id(file_id)
    rel = posixpath.join("projects", project, file_id)
    normalised = posixpath.normpath(rel)
    if normalised.startswith("..") or normalised.startswith("/"):
        raise schema_invalid(f"path escapes the repository: {rel!r}")
    return normalised


def split_public_path(relpath: str) -> tuple[str, str] | None:
    """``projects/<project>/<id>.md`` -> ``(project, id)``, else ``None``.

    Three parts is the common case (a note directly under a project), not four —
    ``parts[2:]`` is the id, and it may be a single segment.
    """
    parts = relpath.replace("\\", "/").split("/")
    if len(parts) < 3 or parts[0] != "projects":
        return None
    project = parts[1]
    note_id = "/".join(parts[2:])
    if note_id.endswith(".md"):
        return project, note_id[:-3]
    return project, note_id

