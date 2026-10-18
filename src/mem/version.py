from __future__ import annotations

import os
import subprocess
from functools import lru_cache
from pathlib import Path

__version__ = "0.1.2"

# API/version string reported by GET /health and used to detect a stale daemon.
API_VERSION = "mem/0.1"


@lru_cache(maxsize=1)
def build_commit() -> str:
    """The source revision, when running from a checkout or a stamped build.

    A wheel installed outside a git checkout has no revision; callers print
    ``unknown`` rather than inventing one.
    """
    stamped = os.environ.get("MEM_BUILD_COMMIT", "").strip()
    if stamped:
        return stamped[:12]
    root = Path(__file__).resolve().parents[2]
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short=12", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return completed.stdout.strip() if completed.returncode == 0 else "unknown"
