"""Shared YAML summary validation for server uploads and local Git preflight."""

from __future__ import annotations

import re
from typing import Any

import yaml

from .errors import schema_invalid

SUMMARY_MAX = 200
_DELIM = re.compile(r"^---\s*$")


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Return YAML frontmatter and body; missing frontmatter yields an empty map."""
    lines = text.splitlines()
    if not lines or not _DELIM.match(lines[0]):
        return {}, text
    for index in range(1, len(lines)):
        if _DELIM.match(lines[index]):
            raw = "\n".join(lines[1:index])
            body = "\n".join(lines[index + 1:])
            try:
                data = yaml.safe_load(raw) if raw.strip() else {}
            except yaml.YAMLError as exc:
                raise schema_invalid(f"frontmatter is not valid YAML: {exc}") from exc
            if data is None:
                data = {}
            if not isinstance(data, dict):
                raise schema_invalid("frontmatter must be a mapping")
            return data, body
    raise schema_invalid("frontmatter opened with '---' but was never closed")


def validate_summary(data: dict[str, Any]) -> str:
    summary = data.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise schema_invalid("'summary' is required and must be a non-empty string")
    if "\n" in summary or "\r" in summary:
        raise schema_invalid("'summary' must not contain a newline")
    if len(summary) > SUMMARY_MAX:
        raise schema_invalid(f"'summary' is {len(summary)} chars; the limit is {SUMMARY_MAX}")
    return summary
