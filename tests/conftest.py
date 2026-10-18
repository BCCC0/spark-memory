"""Shared pytest fixtures.

Acceptance tests drive the **real** server over **real** HTTP in a subprocess,
and invoke the **real** CLI as a subprocess. That is deliberate: the eight
checks in ``MEM.md`` are end-to-end claims about the protocol, and an in-process
test client would not exercise the transport, the exit codes, or the "unreachable
server refuses the write" rule.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# Sibling module in this directory (tests/ carries no __init__.py, so pytest's
# default prepend import mode puts it on sys.path -- the same reason the test
# modules can do `from conftest import note_text`).
from fake_embeddings import use_fake_embeddings  # noqa: E402

# The daemon is started in a thread by default: it still binds a real port and
# serves real HTTP, which is what the acceptance tests are about, but it does not
# pay interpreter + import + uvicorn startup once per test. Set this to run the
# original subprocess daemon instead -- the full process-boundary path, for when
# that is the thing under test.
SUBPROCESS_MODE = os.environ.get("MEM_TEST_SUBPROCESS") == "1"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _env(home: Path, config_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    env["MEM_CONFIG_DIR"] = str(config_dir)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run_cli(
    args: list[str],
    *,
    env: dict[str, str],
    cwd: Path,
    timeout: float = 60.0,
) -> subprocess.CompletedProcess:
    client_binary = os.environ.get("MEM_TEST_CLIENT_BINARY")
    command = [client_binary] if client_binary else [sys.executable, "-m", "mem.cli"]
    return subprocess.run(
        [*command, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=str(cwd),
        timeout=timeout,
    )


class Harness:
    """A running daemon plus a CLI bound to it."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        template: Path | None = None,
        mode: str = "thread",
        inline_extensions: tuple[str, ...] = (".md", ".txt"),
    ) -> None:
        self.tmp = tmp_path
        self.repo = tmp_path / "memory"
        self.home = tmp_path / "home"
        self.config_dir = self.home / ".config" / "mem"
        self.state = self.repo / ".mem"
        self.secret_file = tmp_path / "mem" / "enroll.secret"
        # The capability-link secret (S31). Explicit so tests can read and rotate it,
        # instead of depending on how `Path.home()` resolves in the child.
        self.link_secret_file = tmp_path / "mem" / "link.secret"
        self.home.mkdir(parents=True, exist_ok=True)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.secret_file.parent.mkdir(parents=True, exist_ok=True)
        # Written before the daemon starts, or it would mint its own random one.
        self.secret_file.write_text("test-secret\n", encoding="utf-8")
        self.port = _free_port()
        self.server = f"http://127.0.0.1:{self.port}"
        self.env = _env(self.home, self.config_dir)
        self.env["MEM_ENROLL_SECRET"] = "test-secret"
        self.process: subprocess.Popen | None = None
        self._uvicorn = None
        self._thread: threading.Thread | None = None
        self.mode = mode
        self.inline_extensions = inline_extensions
        self.workdir = tmp_path / "work"
        self.workdir.mkdir(parents=True, exist_ok=True)

        if template is not None:
            # The same starting point `ensure()` would build, without its five
            # `git` subprocesses (~300 ms here). `ensure()` then finds `.git` and
            # the identity already in place and spawns nothing at all.
            shutil.copytree(template / ".git", self.repo / ".git")
            shutil.copy2(template / ".gitignore", self.repo / ".gitignore")

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self.mode == "subprocess":
            self._start_subprocess()
        else:
            self._start_threaded()

    def _start_subprocess(self) -> None:
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "mem.server",
                "--repo",
                str(self.repo),
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--devices-db",
                str(self.tmp / "mem" / "devices.db"),
                "--enroll-secret",
                str(self.secret_file),
                "--link-secret",
                str(self.link_secret_file),
                "--state-dir",
                str(self.state),
                "--inline-extensions",
                ",".join(self.inline_extensions),
            ],
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        deadline = time.time() + 30
        while time.time() < deadline:
            if self.process.poll() is not None:
                out, err = self.process.communicate()
                raise RuntimeError(f"daemon died on startup:\n{out}\n{err}")
            try:
                with urllib.request.urlopen(f"{self.server}/health", timeout=1) as response:
                    if response.status == 200:
                        return
            except (urllib.error.URLError, OSError):
                time.sleep(0.15)
        raise RuntimeError("daemon did not become healthy")

    def _start_threaded(self) -> None:
        """Serve the real app on a real port, inside this process.

        Everything the acceptance tests assert about the transport still holds:
        a real socket, real HTTP, the DNS-rebinding host check, and the
        `request.client.host` locality guard. What is saved is the per-test
        interpreter, import, and uvicorn startup -- the app itself is rebuilt
        from scratch for every test, so isolation is unchanged.
        """
        import uvicorn

        from mem.server.app import Settings, create_app

        settings = Settings(
            repo_root=self.repo,
            devices_db=self.tmp / "mem" / "devices.db",
            enroll_secret=self.secret_file,
            state_dir=self.state,
            base_url=self.server,
            link_secret=self.link_secret_file,
            inline_extensions=self.inline_extensions,
        )
        # proxy_headers=False matches `memd` exactly: the locality guard reads
        # `request.client.host`, which proxy-header middleware would rewrite.
        config = uvicorn.Config(
            create_app(settings),
            host="127.0.0.1",
            port=self.port,
            log_level="warning",
            proxy_headers=False,
        )
        self._uvicorn = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._uvicorn.run, name="memd", daemon=True)
        self._thread.start()

        # `Server.started` is set once the socket is accepting, so this is a real
        # readiness signal rather than a sleep-and-hope.
        deadline = time.time() + 30
        while not self._uvicorn.started:
            if not self._thread.is_alive():
                raise RuntimeError("daemon died on startup")
            if time.time() > deadline:
                raise RuntimeError("daemon did not become healthy")
            time.sleep(0.005)

    def stop(self) -> None:
        if self._uvicorn is not None:
            self._uvicorn.should_exit = True
            if self._thread is not None:
                self._thread.join(timeout=10)
            self._uvicorn = None
            self._thread = None
            return
        if self.process is None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            self.process.kill()
            self.process.wait(timeout=10)
        self.process = None

    # -- helpers -----------------------------------------------------------

    def cli(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
        return run_cli(list(args), env=self.env, cwd=cwd or self.workdir)

    def flush(self) -> dict:
        result = self.cli("flush")
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    def cli_json(self, *args: str, cwd: Path | None = None):
        result = self.cli(*args, cwd=cwd)
        assert result.returncode == 0, f"{args} failed: {result.stderr}"
        payload = json.loads(result.stdout)
        if args and args[0] == "init" and isinstance(payload, dict) and payload.get("receipt"):
            self.wait_for_receipt(payload["receipt"])
            return payload
        # Most pre-queue tests inspect the committed result. Keep that fixture
        # contract while dedicated queue tests inspect the raw CLI receipt.
        if isinstance(payload, dict) and payload.get("receipt") and payload.get("status") in {"queued", "running", "committed"}:
            finished = self.wait_for_receipt(payload["receipt"])
            if payload.get("idempotent_replay"):
                finished["idempotent_replay"] = True
            if payload.get("warning"):
                finished["warning"] = payload["warning"]
            return finished
        if args[:2] == ("hook", "update"):
            self.wait_for_queue()
        return payload

    def wait_for_receipt(self, receipt: str) -> dict:
        self.flush()
        db_path = self.state / "write-queue" / "jobs.db"
        with sqlite3.connect(db_path) as db:
            row = db.execute("SELECT status, result, error FROM jobs WHERE id=?", (receipt,)).fetchone()
        assert row and row[0] == "committed", f"queued write {receipt} did not commit: {row}"
        return json.loads(row[1])

    def wait_for_queue(self) -> None:
        self.flush()

    def wait_for_text_index(self, timeout: float = 30.0) -> None:
        index_path = self.state / "search.db"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with sqlite3.connect(index_path) as db:
                row = db.execute("SELECT value FROM meta WHERE key='head'").fetchone()
            indexed = row[0] if row else None
            head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo,
                                  capture_output=True, text=True, check=True).stdout.strip()
            if indexed == head:
                return
            time.sleep(0.03)
        raise AssertionError("text index did not catch up")

    def token(self) -> str:
        """The enrolled device's token, for API-level tests."""
        import tomllib

        with (self.config_dir / "credentials").open("rb") as handle:
            return tomllib.load(handle)["token"]

    def login(self) -> dict:
        return self.cli_json("login", "--server", self.server, "--name", "test-device")

    def init(self, project: str = "demo") -> dict:
        return self.cli_json("init", project, "--summary", f"{project} project")

    def write(self, name: str, text: str, *, directory: Path | None = None) -> Path:
        target = (directory or self.workdir) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    def wait_for_vectors(self, *, timeout: float = 180.0) -> dict:
        """Wait until the semantic index has caught up with HEAD (S32).

        A hybrid search no longer triggers the sync — the write path schedules it and a
        read serves whatever is ready — so a test that writes and then searches must
        wait for the background pass instead of assuming the read did the work. Bounded,
        and it fails loudly rather than letting a stale index masquerade as a miss.
        """
        import time as _time

        deadline = _time.monotonic() + timeout
        status = {}
        while _time.monotonic() < deadline:
            status = self.cli_json("index")
            hybrid = status.get("hybrid") or {}
            if hybrid.get("indexed_head") and hybrid.get("indexed_head") == status.get("head"):
                return status
            _time.sleep(1.0)
        raise AssertionError(
            f"the vector index never caught up with HEAD within {timeout}s: {status}"
        )


def note_text(
    *,
    summary: str = "a note",
    status: str = "active",
    note_type: str = "reference",
    title: str = "A note",
    body: str = "Some prose.",
    tags: list[str] | None = None,
) -> str:
    front = [f"summary: {json.dumps(summary)}", f"status: {status}", f"type: {note_type}"]
    if tags:
        front.append("tags: [" + ", ".join(tags) + "]")
    return "---\n" + "\n".join(front) + "\n---\n\n# " + title + "\n\n" + body + "\n"


@pytest.fixture(autouse=True)
def _fake_embeddings(request, monkeypatch):
    """Use the deterministic test embedder; see tests/fake_embeddings.py.

    Without this, every write test runs the real model: `create_app` builds a
    `VectorIndex` whenever the `embed` extra is installed, and the reindexer embeds
    on the write path -- loading an ONNX session and, on a cold cache, attempting a
    HuggingFace download. Tests marked `real_embedder` opt out and keep the real one.
    """
    use_fake_embeddings(request, monkeypatch)


@pytest.fixture(scope="session")
def repo_template(tmp_path_factory) -> Path:
    """A pre-initialised repository, copied per test.

    `MemoryRepo.ensure()` on a fresh directory spawns five `git` subprocesses
    (~300 ms on Windows). Copying an existing `.git` reaches the same starting
    point in ~10 ms, and `ensure()` then finds the identity already present so it
    spawns nothing at all.
    """
    from mem.server.repo import MemoryRepo

    root = tmp_path_factory.mktemp("repo-template") / "memory"
    repo = MemoryRepo(root)
    repo.ensure()
    # xdist workers copy this template concurrently. A background git
    # maintenance/gc run creates and removes objects/maintenance.lock while
    # copytree is walking it, which makes the copy fail spuriously.
    repo.git("config", "gc.auto", "0")
    repo.git("config", "maintenance.auto", "false")
    return root


@pytest.fixture
def harness(tmp_path: Path, repo_template: Path):
    h = Harness(
        tmp_path,
        template=repo_template,
        mode="subprocess" if SUBPROCESS_MODE else "thread",
    )
    h.start()
    try:
        yield h
    finally:
        h.stop()


@pytest.fixture
def subprocess_harness(tmp_path: Path, repo_template: Path):
    """A daemon in its own process, for tests that kill or restart it.

    Killing the daemon mid-write (the S26 crash race) is a claim about a real
    process, so it cannot run against the threaded daemon.
    """
    h = Harness(tmp_path, template=repo_template, mode="subprocess")
    h.start()
    try:
        yield h
    finally:
        h.stop()


@pytest.fixture
def ready(harness: Harness) -> Harness:
    """A logged-in device with an initialised project."""
    harness.login()
    harness.init("demo")
    return harness
