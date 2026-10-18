"""Compare parser failures from the Go and Python CLIs."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest


CASES = [
    ("--nonsense",), ("ls", "--bogus"), ("read",), ("add",), ("rm",),
    ("mv",), ("search",), ("hook", "add"), ("pointer", "add"),
    ("device",), ("ls", "a", "b"), ("status", "extra"),
    ("search", "q", "--mode", "bad"), ("ls", "--sort", "bad"),
    ("add", "--as"), ("read", "--outline", "--bogus"),
    ("bogus", "--help"), ("hook", "bogus", "--help"),
    ("--human=false", "read", "x"), ("--status", "x", "ls"),
    ("-mfoo", "status"),
]


def _without_python(value):
    """The Go binary does not implement the Python-only `mem python` verb."""
    if isinstance(value, dict):
        return {key: _without_python(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_without_python(item) for item in value]
    if isinstance(value, str):
        return (
            value.replace("python, ", "")
            .replace(", python", "")
            .replace(",python,", ",")
            .replace(",python}", "}")
        )
    return value


@pytest.mark.skipif(not os.environ.get("MEM_TEST_CLIENT_BINARY"), reason="Go client not selected")
def test_usage_error_envelopes_match_python():
    root = os.path.dirname(os.path.dirname(__file__))
    env = os.environ.copy()
    env["PYTHONPATH"] = os.path.join(root, "src") + os.pathsep + env.get("PYTHONPATH", "")
    mismatches = []
    for args in CASES:
        go = subprocess.run([os.environ["MEM_TEST_CLIENT_BINARY"], *args], cwd=root,
                            env=env, capture_output=True, text=True, encoding="utf-8", timeout=10)
        py = subprocess.run([sys.executable, "-m", "mem.cli", *args], cwd=root,
                            env=env, capture_output=True, text=True, encoding="utf-8", timeout=10)
        go_payload = json.loads(go.stderr) if go.stderr.strip() else None
        py_payload = _without_python(json.loads(py.stderr)) if py.stderr.strip() else None
        if (go.returncode, go_payload) != (py.returncode, py_payload):
            mismatches.append((args, go.stderr.strip(), py.stderr.strip()))
    assert not mismatches, "\n".join(map(str, mismatches))
