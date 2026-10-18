"""Human-output parity for the side-by-side Go client rollout."""

from __future__ import annotations

import os
import json
import re
import subprocess
import sys

import pytest

from conftest import note_text


@pytest.mark.skipif(not os.environ.get("MEM_TEST_CLIENT_BINARY"), reason="Go client not selected")
def test_go_human_output_for_read_commands(ready):
    ready.write("human.md", note_text(summary="human output", body="hello human\n"))
    ready.cli_json("add", "human.md", "--as", "human")
    ready.flush()

    for args in (
        ("status",), ("projects",), ("ls",), ("read", "human"),
        ("manifest",), ("search", "human"), ("index",), ("devices",),
        ("flush",), ("hook", "ls"),
    ):
        go = ready.cli(*args, "--human")
        py = subprocess.run(
            [sys.executable, "-m", "mem.cli", *args, "--human"],
            cwd=ready.workdir, env=ready.env, capture_output=True,
            text=True, encoding="utf-8", timeout=60,
        )
        assert go.returncode == py.returncode == 0, (args, go.stderr, py.stderr)
        normalize = lambda text: re.sub(r"age=[^\n]+", "age=<elapsed>", text)
        assert normalize(go.stdout) == normalize(py.stdout), (args, go.stdout, py.stdout)


@pytest.mark.skipif(not os.environ.get("MEM_TEST_CLIENT_BINARY"), reason="Go client not selected")
def test_go_login_prompts_for_secret(ready):
    env = ready.env.copy()
    env.pop("MEM_ENROLL_SECRET", None)
    config_dir = ready.tmp / "prompt-config"
    env["MEM_CONFIG_DIR"] = str(config_dir)
    result = subprocess.run(
        [os.environ["MEM_TEST_CLIENT_BINARY"], "login", "--server", ready.server,
         "--name", "prompted-device", "--alias", "prompted-device"],
        cwd=ready.workdir, env=env, input="test-secret\n", capture_output=True,
        text=True, encoding="utf-8", timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "enrollment secret:" in result.stderr
    assert "test-secret" not in result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["alias"] == "prompted-device"
    assert (config_dir / "credentials").is_file()


@pytest.mark.skipif(not os.environ.get("MEM_TEST_CLIENT_BINARY"), reason="Go client not selected")
def test_go_help_matches_python():
    root = os.path.dirname(os.path.dirname(__file__))
    env = os.environ.copy()
    env["PYTHONPATH"] = os.path.join(root, "src") + os.pathsep + env.get("PYTHONPATH", "")
    help_map = json.loads(open(os.path.join(root, "client", "internal", "cli", "help.json"), encoding="utf-8").read())
    assert "python" not in help_map, "the Go client must not advertise mem python"
    for key in help_map:
        args = [] if key == "root" else key.split(" ")
        go = subprocess.run([os.environ["MEM_TEST_CLIENT_BINARY"], *args, "--help"], cwd=root,
                            env=env, capture_output=True, text=True, encoding="utf-8", timeout=10)
        assert go.returncode == 0, (key, go.stderr)
        assert go.stdout == help_map[key], key
        assert "python" not in go.stdout, key

    failure = subprocess.run(
        [os.environ["MEM_TEST_CLIENT_BINARY"], "python", "--help"], cwd=root, env=env,
        capture_output=True, text=True, encoding="utf-8", timeout=10,
    )
    assert failure.returncode == 64
    assert json.loads(failure.stderr)["error"]["code"] == "usage_error"
