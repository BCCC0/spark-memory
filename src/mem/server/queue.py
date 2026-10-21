"""Durable, bounded, single-consumer write queue.

SQLite owns job order and reservations. Upload bytes live in a sibling spool;
the manifest is committed only after its directory has been made durable.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..errors import MemError

if os.name == "nt":
    import ctypes
    import msvcrt
else:
    import fcntl

MAX_JOBS = 1000
MAX_PENDING_BYTES = 2 * 1024 * 1024 * 1024


def _sync_dir(path: Path) -> None:
    """Persist directory-entry changes on POSIX; fail closed on sync errors."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _durable_replace(source: Path, target: Path) -> None:
    if os.name == "nt":
        # Windows does not expose directory fsync through os.open. The documented
        # write-through directory move supplies the metadata durability boundary.
        move = ctypes.windll.kernel32.MoveFileExW
        move.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32)
        move.restype = ctypes.c_int
        if not move(str(source), str(target), 0x8):  # MOVEFILE_WRITE_THROUGH
            raise ctypes.WinError()
    else:
        os.replace(source, target)
        _sync_dir(target.parent)


def _scoped_key(project: str, device_id: str, idem: str | None) -> str | None:
    return f"{len(project)}:{project}{len(device_id)}:{device_id}{idem}" if idem else None


class WriteQueue:
    def __init__(self, state_dir: Path, repo_root: Path | None = None) -> None:
        self.root = state_dir / "write-queue"
        self.spool = self.root / "spool"
        self.spool.mkdir(parents=True, exist_ok=True)
        self.db = self.root / "jobs.db"
        self.repo_root = Path(repo_root).resolve() if repo_root is not None else None
        self._guard = threading.RLock()
        self._worker_handle = None
        self._finished = 0
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT UNIQUE NOT NULL,
                    project TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    bytes INTEGER NOT NULL,
                    idem TEXT UNIQUE,
                    created REAL NOT NULL,
                    error TEXT,
                    result TEXT,
                    prepared_head TEXT,
                    prepared_result TEXT
                );
                CREATE TABLE IF NOT EXISTS targets (
                    project TEXT NOT NULL,
                    id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    PRIMARY KEY(project, id)
                );
                CREATE TABLE IF NOT EXISTS job_targets (
                    project TEXT NOT NULL,
                    id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    PRIMARY KEY(project, id, job_id)
                );
                CREATE INDEX IF NOT EXISTS jobs_status_seq ON jobs(status, seq);
                CREATE INDEX IF NOT EXISTS job_targets_lookup ON job_targets(project, id);
            """)

    def _lock_path(self) -> Path:
        """The publisher lock belongs to the repository, not the state directory.

        `--state-dir` is operator-settable, so a lock under it lets two daemons
        serving the same repository appoint themselves publisher independently.
        The repository's git dir is the stable identity both must agree on.
        """
        if self.repo_root is None:
            # Library/test fallback: no repository was supplied.
            return self.root / "worker.lock"
        dot_git = self.repo_root / ".git"
        if dot_git.is_dir():
            return dot_git / "mem-worker.lock"
        # A worktree or `--separate-git-dir` repo has a `.git` FILE. Resolve the
        # common git dir so every worktree of one repository contends on the same
        # publisher lock.
        try:
            completed = subprocess.run(
                ["git", "-C", str(self.repo_root), "rev-parse", "--git-common-dir"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            completed = None
        if completed is not None and completed.returncode == 0:
            common = completed.stdout.strip()
            if common:
                path = Path(common)
                if not path.is_absolute():
                    path = (self.repo_root / path).resolve()
                return path / "mem-worker.lock"
        # Let the open fail with the real path so the operator can diagnose it.
        return dot_git / "mem-worker.lock"

    def claim_worker(self) -> None:
        """Permit only one publisher for this repository across processes."""
        lock_path = self._lock_path()
        try:
            handle = lock_path.open("a+b")
        except OSError as exc:
            raise MemError(
                "internal_error", f"cannot open publisher lock {lock_path}: {exc}"
            ) from exc
        try:
            handle.seek(0)
            if os.name == "nt":
                handle.write(b"0")
                handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise MemError("target_busy", "another server is already processing this write queue") from exc
        self._worker_handle = handle
        # A prior worker's process has stopped, so its interrupted job is safe
        # to retry. The publisher checks prepared_head before replaying it.
        with self._guard, self._connect() as db:
            db.execute("UPDATE jobs SET status='queued' WHERE status='running'")
            self._prune(db)

    def _prune(self, db: sqlite3.Connection, keep_days: int = 30) -> int:
        """Drop terminal jobs (and their targets) past a bounded age.

        `job_targets` is otherwise never pruned, and `progress()` scans it for
        every failed job; unbounded growth turns a routine flush into a table
        walk. Idempotency rows are kept for recent jobs only, which is the same
        window the queue is allowed to replay.
        """
        cutoff = time.time() - keep_days * 86400
        stale = [row["id"] for row in db.execute(
            "SELECT id FROM jobs WHERE status IN ('committed','failed') AND created < ?",
            (cutoff,),
        )]
        for receipt in stale:
            db.execute("DELETE FROM job_targets WHERE job_id=?", (receipt,))
            db.execute("DELETE FROM jobs WHERE id=?", (receipt,))
        return len(stale)

    def release_worker(self) -> None:
        handle = self._worker_handle
        self._worker_handle = None
        if handle is None:
            return
        handle.seek(0)
        if os.name == "nt":
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.db, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def submit(self, *, project: str, kind: str, targets: list[str],
               manifest: dict[str, Any], files: list[bytes | Path],
               idem: str | None) -> dict[str, Any]:
        if not targets or len(targets) != len(set(targets)):
            raise MemError("invalid_request", "write needs distinct affected IDs")
        size = sum(body.stat().st_size if isinstance(body, Path) else len(body)
                   for body in files)
        idem = _scoped_key(project, str(manifest.get("device_id", "")), idem)
        with self._guard:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                if idem:
                    prior = db.execute("SELECT id, status, seq FROM jobs WHERE idem=?", (idem,)).fetchone()
                    if prior:
                        if prior["status"] == "failed":
                            db.execute("UPDATE jobs SET idem=NULL WHERE id=?", (prior["id"],))
                        else:
                            original_targets = [r[0] for r in db.execute(
                                "SELECT id FROM job_targets WHERE job_id=? ORDER BY id", (prior["id"],))]
                            return {"status": prior["status"], "receipt": prior["id"],
                                    "seq": prior["seq"],
                                    "affected_ids": original_targets, "idempotent_replay": True}
                count, used = db.execute(
                    "SELECT COUNT(*), COALESCE(SUM(bytes),0) FROM jobs WHERE status IN ('queued','running')"
                ).fetchone()
                if count >= MAX_JOBS or used + size > MAX_PENDING_BYTES:
                    raise MemError("queue_full", "server write queue is full")
                busy = db.execute(
                    "SELECT id FROM targets WHERE project=? AND id IN (" + ",".join("?" for _ in targets) + ") LIMIT 1",
                    (project, *targets),
                ).fetchone()
                if busy:
                    raise MemError("target_busy", f"{busy['id']} already has an unfinished write",
                                   {"id": busy["id"]})
                receipt = "j_" + uuid.uuid4().hex
                temp = self.spool / (receipt + ".tmp")
                final = self.spool / receipt
                temp.mkdir()
                try:
                    for index, body in enumerate(files):
                        with (temp / str(index)).open("wb") as stream:
                            if isinstance(body, Path):
                                with body.open("rb") as source:
                                    shutil.copyfileobj(source, stream, 64 * 1024)
                            else:
                                stream.write(body)
                            stream.flush()
                            os.fsync(stream.fileno())
                    with (temp / "manifest.json").open("w", encoding="utf-8") as stream:
                        json.dump(manifest, stream)
                        stream.flush()
                        os.fsync(stream.fileno())
                    if os.name != "nt":
                        _sync_dir(temp)
                    _durable_replace(temp, final)
                    cursor = db.execute(
                        "INSERT INTO jobs(id,project,kind,status,bytes,idem,created) VALUES(?,?,?,'queued',?,?,?)",
                        (receipt, project, kind, size, idem, time.time()),
                    )
                    seq = int(cursor.lastrowid)
                    db.executemany("INSERT INTO targets(project,id,job_id) VALUES(?,?,?)",
                                   [(project, target, receipt) for target in targets])
                    db.executemany("INSERT INTO job_targets(project,id,job_id) VALUES(?,?,?)",
                                   [(project, target, receipt) for target in targets])
                    db.commit()
                except Exception:
                    db.rollback()
                    shutil.rmtree(temp, ignore_errors=True)
                    shutil.rmtree(final, ignore_errors=True)
                    raise
                return {"status": "queued", "receipt": receipt,
                        "seq": seq,
                        "affected_ids": targets, "idempotent_replay": False}

    def next_job(self) -> dict[str, Any] | None:
        with self._guard, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY seq LIMIT 1").fetchone()
            if row is None:
                return None
            db.execute("UPDATE jobs SET status='running' WHERE id=?", (row["id"],))
            return dict(row)

    def lookup(self, project: str, device_id: str, idem: str | None) -> dict[str, Any] | None:
        if not idem:
            return None
        idem = _scoped_key(project, device_id, idem)
        with self._guard, self._connect() as db:
            row = db.execute("SELECT id, status, seq FROM jobs WHERE idem=?", (idem,)).fetchone()
            if row is None or row["status"] == "failed":
                return None
            original_targets = [r[0] for r in db.execute(
                "SELECT id FROM job_targets WHERE job_id=? ORDER BY id", (row["id"],))]
            return {"status": row["status"], "receipt": row["id"], "seq": row["seq"],
                    "affected_ids": original_targets, "idempotent_replay": True}

    def payload(self, receipt: str) -> tuple[dict[str, Any], list[bytes]]:
        root = self.spool / receipt
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        files = [(root / str(index)).read_bytes() for index in range(manifest.get("file_count", 0))]
        return manifest, files

    def prepared(self, receipt: str, head: str, result: dict[str, Any]) -> None:
        with self._guard, self._connect() as db:
            db.execute("UPDATE jobs SET prepared_head=?, prepared_result=? WHERE id=?",
                       (head, json.dumps(result), receipt))

    # Terminal rows are pruned by `_prune` at publisher start, so `progress()`
    # stays bounded. A crash between the UPDATE and the `rmtree` can still leak
    # a spool directory; `__init__` only creates directories.
    def finish(self, receipt: str, *, result: dict[str, Any] | None = None,
               error: dict[str, Any] | None = None) -> None:
        with self._guard, self._connect() as db:
            db.execute("UPDATE jobs SET status=?, result=?, error=? WHERE id=?",
                       ("failed" if error else "committed", json.dumps(result) if result else None,
                        json.dumps(error) if error else None, receipt))
            db.execute("DELETE FROM targets WHERE job_id=?", (receipt,))
            self._finished += 1
            if self._finished % 64 == 0:
                # Amortised retention: a long-lived publisher keeps the table
                # bounded too, not only a restart.
                self._prune(db)
        shutil.rmtree(self.spool / receipt, ignore_errors=True)

    def state(self, project: str, note_id: str) -> dict[str, Any] | None:
        with self._guard, self._connect() as db:
            row = db.execute(
                "SELECT j.id, j.status, j.error FROM targets t JOIN jobs j ON j.id=t.job_id "
                "WHERE t.project=? AND t.id=?", (project, note_id),
            ).fetchone()
            if row:
                return {"status": "pending", "receipt": row["id"]}
            row = db.execute(
                "SELECT j.id, j.status, j.error FROM job_targets jt JOIN jobs j ON j.id=jt.job_id "
                "WHERE jt.project=? AND jt.id=? ORDER BY j.seq DESC LIMIT 1",
                (project, note_id),
            ).fetchone()
            if row and row["status"] == "failed":
                return {"status": "failed", "receipt": row["id"],
                        "error": json.loads(row["error"])}
            return None

    def pending_count(self, project: str | None = None) -> int:
        with self._guard, self._connect() as db:
            if project:
                return db.execute("SELECT COUNT(*) FROM jobs WHERE project=? AND status IN ('queued','running')",
                                  (project,)).fetchone()[0]
            return db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]

    def progress(self, through: int | None = None) -> dict[str, Any]:
        """A finite barrier over jobs accepted before the first poll."""
        with self._guard, self._connect() as db:
            if through is None:
                through = db.execute("SELECT COALESCE(MAX(seq), 0) FROM jobs").fetchone()[0]
            pending = db.execute(
                "SELECT COUNT(*) FROM jobs WHERE seq<=? AND status IN ('queued','running')",
                (through,),
            ).fetchone()[0]
            failed = [dict(row) for row in db.execute(
                "SELECT j.id AS receipt, j.project, j.kind, j.error FROM jobs j "
                "WHERE j.seq<=? AND j.status='failed' AND EXISTS ("
                "SELECT 1 FROM job_targets jt WHERE jt.job_id=j.id AND NOT EXISTS ("
                "SELECT 1 FROM job_targets later_target "
                "JOIN jobs later ON later.id=later_target.job_id "
                "WHERE later_target.project=jt.project AND later_target.id=jt.id "
                "AND later.seq>j.seq AND later.seq<=? AND later.status='committed'"
                ")) ORDER BY j.seq", (through, through),
            )]
            for row in failed:
                row["error"] = json.loads(row["error"])
            return {"through": through, "pending": pending, "failed": failed}
