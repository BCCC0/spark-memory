"""Typed outcomes — one table mapping a code to HTTP status and CLI exit code.

``MEM-SERVER.md`` §6 defines the mapping; ``MEM-CLI.md`` §4 defines the exit
codes. The point of the table is invariant **C6**: an agent must never have to
parse prose to learn what happened, so every failure travels as one of these
codes and nothing else.
"""

from __future__ import annotations

from typing import Any

# code -> (http_status, cli_exit)
TABLE: dict[str, tuple[int, int]] = {
    "unauthorized": (401, 8),
    # Reserved — never emitted in MVP-0: a re-enrolling device always gets a new
    # device_id (MEM-RESOLUTIONS.md B4). Kept so code and spec agree.
    "device_exists": (409, 8),
    "server_unreachable": (503, 2),
    "lock_held": (423, 3),
    "lock_timeout": (423, 3),
    "target_busy": (409, 3),
    "queue_full": (503, 3),
    "queued_write_failed": (409, 1),
    "schema_invalid": (422, 4),
    # Retired (S30): "nothing changed" is now a successful no-op, so this code is
    # no longer emitted. Kept so the code and `MEM-CLI.md` §4's table still agree.
    "staging_diverged": (409, 5),
    "no_project": (404, 6),
    "stale_revision": (412, 7),
    "target_moved": (410, 9),
    "not_found": (404, 9),
    "invalid_request": (400, 4),
    # The client refused to record an authoring path outside the project root.
    # Client-side only, like `usage_error`: the server does not know the root.
    "origin_outside_root": (400, 4),
    "internal_error": (500, 1),
    # Client-side only: the CLI could not parse its own arguments. It never
    # reaches the server. It exists so a usage error travels as the one JSON
    # object `MEM-CLI.md` §4 requires, instead of argparse's prose.
    "usage_error": (400, 64),
}

# There is deliberately no `EXIT_USAGE` constant: nothing needs the number outside
# this table, and a second name for it is a second thing to keep in step. The exit
# code for a usage error is `MemError("usage_error", ...).exit_code`.


class MemError(Exception):
    """A failure that has a wire code, an HTTP status and a CLI exit code."""

    def __init__(
        self,
        code: str,
        message: str,
        detail: dict[str, Any] | None = None,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        if code not in TABLE:
            raise ValueError(f"unknown error code: {code!r}")
        self.code = code
        self.message = message
        self.detail = detail or {}
        self.http_status = http_status if http_status is not None else TABLE[code][0]

    @property
    def exit_code(self) -> int:
        return TABLE[self.code][1]

    def to_dict(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, "detail": self.detail}}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"MemError({self.code!r}, {self.message!r})"


def schema_invalid(message: str, **detail: Any) -> MemError:
    return MemError("schema_invalid", message, detail)


def not_found(message: str, **detail: Any) -> MemError:
    return MemError("not_found", message, detail)
