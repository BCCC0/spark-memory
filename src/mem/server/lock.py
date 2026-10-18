"""The write lock — an in-process mutex with a mandatory timeout.

``MEM-RESOLUTIONS.md`` B1 settles the contradiction in the specs: this is a
mutex, **not** an OS advisory lock, and the daemon must therefore run as a single
process (``--workers 1``). A second worker would silently get its own lock.

The timeout is not optional: a bare mutex that a stuck git operation holds
forever deadlocks every client (``MEM.md``, traps).
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Iterator

from ..errors import MemError

DEFAULT_TIMEOUT = 15.0


class WriteLock:
    def __init__(self, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._lock = threading.Lock()
        self._timeout = timeout
        self._holder: str | None = None
        self._since: float | None = None
        self._guard = threading.Lock()

    @property
    def timeout(self) -> float:
        return self._timeout

    def acquire(self, holder: str) -> None:
        acquired = self._lock.acquire(timeout=self._timeout)
        if not acquired:
            state = self.state()
            raise MemError(
                "lock_timeout",
                f"timed out after {self._timeout:.0f}s waiting for the write lock",
                {"held_by": state.get("by"), "since": state.get("since")},
            )
        with self._guard:
            self._holder = holder
            self._since = time.time()

    def release(self) -> None:
        with self._guard:
            self._holder = None
            self._since = None
        with contextlib.suppress(RuntimeError):
            self._lock.release()

    @contextlib.contextmanager
    def hold(self, holder: str) -> Iterator[None]:
        self.acquire(holder)
        try:
            yield
        finally:
            self.release()

    def state(self) -> dict[str, object]:
        with self._guard:
            return {
                "held": self._holder is not None,
                "by": self._holder,
                "since": self._since,
            }
