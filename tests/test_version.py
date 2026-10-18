"""The client and daemon identify their implementation and build revision."""

from __future__ import annotations

import subprocess
import sys


def test_python_client_version_is_explicit():
    out = subprocess.run(
        [sys.executable, "-m", "mem.cli", "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.startswith("mem "), out.stdout
    assert "(python, commit " in out.stdout, out.stdout


def test_memd_version_is_explicit():
    out = subprocess.run(
        [sys.executable, "-m", "mem.server", "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.startswith("memd "), out.stdout
    assert "(python, commit " in out.stdout, out.stdout
