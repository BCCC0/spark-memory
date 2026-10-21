"""The git layer — the repository is the only authority (``MEM.md`` I1/C2).

Everything else in the daemon is derived from this module. The ported logic is
Decap's ``localGit`` shape (``MEM-SERVER.md`` §8): receive bytes, write into the
tree, ``git add``, commit — all under one lock, which the caller holds.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import threading
import contextlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..errors import MemError, not_found
from ..ids import note_relpath, split_public_path, text_relpath

IGNORED_DIRS = {".mem", ".git"}


def _is_utf8_text(body: bytes | None) -> bool:
    """True when *body* is a UTF-8 record without NUL bytes."""
    if body is None or b"\0" in body:
        return False
    try:
        body.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _epoch(value: str) -> float:
    """Parse a git ISO timestamp for ordering; unparseable sorts oldest."""
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return float("-inf")


@dataclass
class NoteMeta:
    path: str  # repo-relative, posix
    project: str
    id: str
    created: str | None = None
    updated: str | None = None
    author: str | None = None
    message: str | None = None  # subject of the newest commit touching this path


class MemoryRepo:
    def __init__(self, root: Path, inline_extensions: tuple[str, ...] = (".md", ".txt")) -> None:
        self.root = Path(root)
        self.inline_extensions = frozenset(inline_extensions)
        self._paths_cache: dict[str, list[str]] = {}
        # `commit_index` is the expensive half of `notes()`: one `git log
        # --name-only` over the whole history. It only depends on the revision,
        # so memoise it per head instead of re-running it on every call.
        self._commit_index_cache: dict[str, dict[str, tuple[str, str, str, str]]] = {}
        self._message_cache: dict[str, str] = {}
        # Spawn accounting for tests and `/v1/status`: every git invocation in
        # this process goes through `git_bytes`, so this is the true count.
        self.git_calls = 0
        # A move changes several worktree paths before one Git commit. Current
        # readers take this gate around their complete projection.
        self.snapshot_lock = threading.Lock()
        self._read_context = threading.local()

    @contextlib.contextmanager
    def pinned(self):
        """Read one immutable commit throughout a request, without blocking writers."""
        previous = getattr(self._read_context, "revision", None)
        self._read_context.revision = self.git("rev-parse", "HEAD").strip()
        try:
            yield self._read_context.revision
        finally:
            self._read_context.revision = previous

    def _revision(self) -> str:
        return getattr(self._read_context, "revision", None) or self.git("rev-parse", "HEAD").strip()

    # -- plumbing ----------------------------------------------------------

    def git(
        self,
        *args: str,
        env: dict[str, str] | None = None,
        check: bool = True,
        stdin: bytes | None = None,
    ) -> str:
        return self.git_bytes(*args, env=env, check=check, stdin=stdin).decode("utf-8", "replace")

    def _run(
        self,
        *args: str,
        env: dict[str, str] | None = None,
        check: bool = True,
        stdin: bytes | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        command = ["git", "-C", str(self.root), *args]
        full_env = os.environ.copy()
        # A headless box must never hang on a hook or a gpg agent
        # (MEM-SERVER.md §8.3).
        full_env.setdefault("GIT_TERMINAL_PROMPT", "0")
        if env:
            full_env.update(env)
        self.git_calls += 1
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                env=full_env,
                input=stdin,
                timeout=60,
            )
        except FileNotFoundError as exc:  # pragma: no cover
            raise MemError("internal_error", "git is not installed") from exc
        except subprocess.TimeoutExpired as exc:
            # Not a lock timeout: this is git itself stalling.
            raise MemError("internal_error", "git operation timed out") from exc
        if check and completed.returncode != 0:
            stderr = completed.stderr.decode("utf-8", "replace").strip()
            raise MemError("internal_error", f"git {args[0]} failed: {stderr}")
        return completed

    def git_bytes(
        self,
        *args: str,
        env: dict[str, str] | None = None,
        check: bool = True,
        stdin: bytes | None = None,
    ) -> bytes:
        return self._run(*args, env=env, check=check, stdin=stdin).stdout

    def cat_file_batch(self, specs: list[str]) -> dict[str, bytes | None]:
        """Read many objects with **one** `git cat-file --batch` process.

        ``specs`` are object names such as ``<rev>:<path>``. Missing objects are
        returned as ``None`` instead of raising, because listing and indexing
        both need to skip a record that vanished between the path listing and
        the read. Parsing is byte-accurate: the batch protocol encodes object
        bodies as ``<sha> <type> <size>\\n<bytes>``, and a body may contain
        newlines.
        """
        if not specs:
            return {}
        payload = ("\n".join(specs) + "\n").encode("utf-8")
        raw = self.git_bytes("cat-file", "--batch", stdin=payload, check=False)
        result: dict[str, bytes | None] = {}
        position = 0
        for spec in specs:
            newline = raw.find(b"\n", position)
            if newline < 0:
                raise MemError("internal_error", "truncated output from git cat-file")
            header = raw[position:newline].decode("utf-8", "replace")
            parts = header.rsplit(" ", 2)
            if len(parts) == 3 and parts[1] in {"blob", "tree", "commit", "tag"} and parts[2].isdigit():
                size = int(parts[2])
                start = newline + 1
                # Only a blob is a note body; a tree or commit at the same name
                # must look absent to `note_bytes`, as it did before.
                result[spec] = raw[start : start + size] if parts[1] == "blob" else None
                position = start + size + 1
            else:
                # "<spec> missing", "<spec> ambiguous", ...
                result[spec] = None
                position = newline + 1
        return result

    def read_notes(
        self, relpaths: list[str], revision: str | None = None
    ) -> dict[str, bytes | None]:
        """Blob bytes for many paths, batched; ``None`` for a missing path."""
        if not relpaths:
            return {}
        chosen = revision or self._revision()
        specs = [f"{chosen}:{relpath}" for relpath in relpaths]
        blobs = self.cat_file_batch(specs)
        return {
            relpath: blobs[spec]
            for relpath, spec in zip(relpaths, specs)
        }

    def _identity_present(self) -> bool:
        """True when the commit identity is already set, so `ensure()` can skip it.

        Reading `.git/config` costs nothing; two `git config` subprocesses cost
        ~100 ms on Windows and are redundant on every start after the first. A
        false negative is harmless -- it just falls through to setting them
        again.
        """
        config = self.root / ".git" / "config"
        try:
            text = config.read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            return False
        return "[user]" in text and "name = mem" in text and "email = mem@mem.local" in text

    def ensure(self) -> None:
        """Create the repository and a first commit if it does not exist."""
        self.root.mkdir(parents=True, exist_ok=True)
        fresh = not (self.root / ".git").exists()
        if fresh:
            self.git("init", "-q")
        # A fresh `init` cannot have the identity yet; an existing repository
        # usually does, and re-setting it is pure subprocess cost.
        if fresh or not self._identity_present():
            self.git("config", "user.name", "mem", check=False)
            self.git("config", "user.email", "mem@mem.local", check=False)

        gitignore = self.root / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text(".mem/\n.mem.toml\n", encoding="utf-8")

        if fresh:
            # Commit .gitignore as part of the initial commit. Left untracked it
            # showed up in `git ls-files --others`, which an earlier status field
            # reported as pending content on a fresh install.
            self.git("add", "--", ".gitignore")
            self.git(
                "commit", "-q", "--no-verify", "--no-gpg-sign",
                "-m", "mem: initialise repository",
            )

    # -- state -------------------------------------------------------------

    def head(self) -> str | None:
        out = self._revision()
        return out or None

    def head_message(self) -> str | None:
        """Subject of the head commit — the last change made (S24)."""
        revision = self._revision()
        cached = self._message_cache.get(revision)
        if cached is not None:
            return cached or None
        out = self.git("log", "-1", "--format=%s", revision, check=False).strip()
        self._message_cache = {revision: out}
        return out or None

    def note_message(self, relpath: str) -> str | None:
        """Subject of the newest commit touching this path (S24)."""
        out = self.git("log", "-1", "--format=%s", self._revision(), "--", relpath, check=False).strip()
        return out or None

    def note_updated(self, relpath: str, revision: str | None = None) -> str | None:
        """Commit date of the newest commit touching this path, ISO-8601.

        This is what a client compares against its working file's mtime to decide
        which copy is fresher. It is the *commit* time, not the file's: a working
        file edited after the last submit is legitimately newer than memory, and
        reporting the committed time is what makes that observable.
        """
        args = ["log", "-1", "--format=%cI"]
        args.append(revision or self._revision())
        args.extend(["--", relpath])
        out = self.git(*args, check=False).strip()
        return out or None

    def path_revision(self, relpath: str) -> str | None:
        """Newest commit touching a path or topic subtree."""
        out = self.git("log", "-1", "--format=%H", self._revision(), "--", relpath, check=False).strip()
        return out or None

    def revision_message(self, revision: str) -> str | None:
        """Subject of a specific revision, for a pinned read (S24)."""
        out = self.git("log", "-1", "--format=%s", revision, check=False).strip()
        return out or None

    # -- reading -----------------------------------------------------------

    # One `git cat-file --batch` classifies every candidate, so the cost is a
    # single spawn per revision instead of one per path (the old BUG here).
    def list_note_paths(self) -> list[str]:
        revision = self._revision()
        cached = self._paths_cache.get(revision)
        if cached is not None:
            return list(cached)
        tracked = self.git_bytes("ls-tree", "-r", "-z", "--name-only", revision, "--", "projects/", check=False).decode("utf-8", "replace")
        candidates = [
            rel for rel in tracked.split("\0")
            if rel and not any(part in IGNORED_DIRS for part in rel.split("/"))
            and Path(rel).suffix.lower() in self.inline_extensions
        ]
        bodies = self.read_notes(candidates, revision) if candidates else {}
        paths = sorted(
            rel for rel in candidates
            if _is_utf8_text(bodies.get(rel))
        )
        self._paths_cache = {revision: paths}
        return list(paths)

    def _is_utf8_record(self, relpath: str, revision: str | None = None) -> bool:
        body = self.read_notes([relpath], revision).get(relpath)
        return _is_utf8_text(body)

    def is_ancestor(self, older: str, newer: str) -> bool:
        """True when *older* is reachable from *newer* (including equality)."""
        if not older or not newer:
            return False
        return self.git("merge-base", older, newer, check=False).strip() == older

    def changed_paths(self, older: str, newer: str) -> list[str]:
        """Paths under ``projects/`` added, changed, or deleted in a range.

        ``--no-renames`` makes a move a delete plus an add, which is exactly
        what an incremental index needs: remove the old path, add the new one.
        """
        raw = self.git_bytes(
            "diff", "--name-status", "-z", "--no-renames",
            older, newer, "--", "projects/",
        )
        fields = raw.decode("utf-8", "replace").split("\0")
        paths: list[str] = []
        for index in range(0, len(fields) - 1, 2):
            status, path = fields[index], fields[index + 1]
            if status and path:
                paths.append(path)
        return sorted(set(paths))

    def is_inline_note(self, relpath: str, revision: str | None = None) -> bool:
        """True when *relpath* is a current, inline note at *revision*."""
        if not relpath.startswith("projects/"):
            return False
        if Path(relpath).suffix.lower() not in self.inline_extensions:
            return False
        if any(part in IGNORED_DIRS for part in relpath.split("/")):
            return False
        return self._is_utf8_record(relpath, revision)

    def read_note(self, relpath: str, revision: str | None = None) -> str:
        return self.note_bytes(relpath, revision).decode("utf-8", "replace")

    def public_path(self, project: str, item_id: str, revision: str | None = None,
                    *, allow_missing: bool = False) -> str:
        """Find an existing item without conflating text bytes and note records."""
        note = note_relpath(project, item_id)
        text = text_relpath(project, item_id)
        if revision:
            tree = self.git("ls-tree", "-r", "--name-only", revision, "--", note, text, check=False)
            found = set(tree.splitlines())
            if note in found and self._is_historical_record(note, revision):
                return note
            if text in found and self._is_historical_record(text, revision):
                return text
            raise MemError("target_moved", f"no note at id {project}/{item_id}")
        tracked = set(self.list_note_paths())
        if note in tracked:
            return note
        if text != note and text in tracked:
            return text
        if allow_missing:
            return note
        raise MemError("target_moved", f"no note at id {project}/{item_id}")

    def _is_historical_record(self, relpath: str, revision: str) -> bool:
        if Path(relpath).suffix.lower() not in self.inline_extensions:
            return False
        entry = self.git("ls-tree", revision, "--", relpath, check=False)
        if not entry.startswith("100644 blob ") and not entry.startswith("100755 blob "):
            return False
        body = self.git_bytes("show", f"{revision}:{relpath}", check=False)
        if b"\0" in body:
            return False
        try:
            body.decode("utf-8")
        except UnicodeDecodeError:
            return False
        return True

    def note_bytes(self, relpath: str, revision: str | None = None) -> bytes:
        chosen = revision or self._revision()
        body = self.read_notes([relpath], chosen).get(relpath)
        if body is None:
            raise not_found(f"{relpath} does not exist at revision {chosen}")
        return body

    @staticmethod
    def sha256_bytes(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def note_sha256(self, relpath: str) -> str | None:
        """Content hash of the committed copy, or ``None`` if absent."""
        try:
            return self.sha256_bytes(self.note_bytes(relpath))
        except MemError as exc:
            if exc.code == "not_found":
                return None
            raise

    def is_tracked(self, relpath: str) -> bool:
        """True when the path is in the index (``git ls-files``).

        Distinguishes a legitimate no-op -- the tracked blob is already identical
        -- from a write git silently refused to stage, such as one covered by an
        operator-supplied ignore rule (`S30`).
        """
        return bool(
            self.git("ls-files", "--error-unmatch", "--", relpath, check=False).strip()
        )

    # -- metadata ----------------------------------------------------------

    def commit_index(self) -> dict[str, tuple[str, str, str, str]]:
        """One pass over ``git log``: ``path -> (created, updated, author, message)``.

        ``git log`` emits **newest first**. The first sighting of a path is
        therefore its most recent change and the last is its creation, so the
        dates are folded explicitly rather than by assignment order -- an
        earlier version overwrote ``updated`` on every chunk and silently
        reported the *oldest* commit as the modification time.

        ``message`` is the **subject of the newest commit touching the path**
        (``MEM-RESOLUTIONS.md`` S24): the description of the change, derived from
        git and never authored into frontmatter.
        """
        revision = self._revision()
        cached = self._commit_index_cache.get(revision)
        if cached is not None:
            return cached
        raw = self.git(
            "log",
            "--no-renames",
            "--format=%x1e%cI%x1f%an%x1f%s",
            "--name-only",
            self._revision(),
            check=False,
        )
        history: dict[str, list[tuple[str, str, str]]] = {}
        for chunk in raw.split("\x1e"):
            chunk = chunk.strip("\n")
            if not chunk:
                continue
            lines = chunk.split("\n")
            header = lines[0]
            if "\x1f" not in header:
                continue
            parts = header.split("\x1f", 2)
            when = parts[0].strip()
            who = parts[1].strip() if len(parts) > 1 else ""
            subject = parts[2].strip() if len(parts) > 2 else ""
            for path in lines[1:]:
                path = path.strip()
                if path:
                    history.setdefault(path, []).append((when, who, subject))

        result: dict[str, tuple[str, str, str, str]] = {}
        for path, entries in history.items():
            newest = max(entries, key=lambda e: _epoch(e[0]))
            oldest = min(entries, key=lambda e: _epoch(e[0]))
            result[path] = (oldest[0], newest[0], newest[1], newest[2])
        self._commit_index_cache = {revision: result}
        return result

    def notes(self) -> list[NoteMeta]:
        times = self.commit_index()
        result: list[NoteMeta] = []
        for relpath in self.list_note_paths():
            split = split_public_path(relpath)
            if not split:
                continue
            project, note_id = split
            made, changed, who, subject = times.get(relpath, ("", "", "", ""))
            result.append(
                NoteMeta(
                    path=relpath,
                    project=project,
                    id=note_id,
                    created=made or None,
                    updated=changed or None,
                    author=who or None,
                    message=subject or None,
                )
            )
        return result

    # -- writing -----------------------------------------------------------

    def _contained(self, relpath: str) -> Path:
        """Resolve relpath and refuse anything outside the repository root.

        A prefix comparison is not enough: `/srv/mem-evil/x` starts with
        `/srv/mem` but is a sibling, not a child. Resolve both paths first so
        symlinks cannot escape either.
        """
        target = (self.root / relpath).resolve()
        if not target.is_relative_to(self.root.resolve()):
            raise MemError("schema_invalid", f"refusing to write outside the repo: {relpath}")
        return target

    def write_note(self, relpath: str, content: bytes) -> None:
        """Write bytes into the working tree, creating directories as needed."""
        target = self._contained(relpath)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    def add(self, relpaths: list[str]) -> None:
        self.git("add", "--", *relpaths)

    def commit_paths(
        self,
        message: str,
        relpaths: list[str],
        *,
        device_id: str | None = None,
    ) -> dict[str, object]:
        """Stage and commit **exactly** ``relpaths``, and nothing else.

        The pathspec is load-bearing (``MEM-RESOLUTIONS.md`` S26). An earlier
        version ran ``git commit`` with **no pathspec**, which commits the *whole
        index*: a process that died between ``git add`` and ``git commit`` left an
        orphan that the next unrelated write then committed under its own message
        — the very risk S23 claimed to delete, with the concurrency race swapped
        for a crash race.

        Returns ``changed: False``, the current head and an empty ``committed``
        list when the pathspec stages nothing: a byte-identical re-write is a
        successful no-op, not an error (S30).
        """
        if not relpaths:
            raise MemError("invalid_request", "a commit needs at least one path")

        # `-A` stages additions, modifications and deletions for exactly these
        # paths (S26). A path-limited `git commit` alone does not stage a new
        # file, so this one call stays; the saves come from dropping the
        # `diff --cached` probe and the follow-up `show --name-only`.
        self.git("add", "-A", "--", *relpaths, check=False)

        env: dict[str, str] = {}
        if device_id:
            env = {
                "GIT_AUTHOR_NAME": device_id,
                "GIT_AUTHOR_EMAIL": f"{device_id}@mem.local",
                "GIT_COMMITTER_NAME": device_id,
                "GIT_COMMITTER_EMAIL": f"{device_id}@mem.local",
            }
        completed = self._run(
            "commit",
            "-q",
            "--no-verify",
            "--no-gpg-sign",
            "-m",
            message,
            "--",
            *relpaths,
            env=env,
            check=False,
        )
        if completed.returncode != 0:
            output = (completed.stdout + completed.stderr).decode("utf-8", "replace").strip()
            if any(
                marker in output
                for marker in ("nothing to commit", "nothing added to commit", "no changes added")
            ):
                # Byte-identical re-write: success, not a failure (S30).
                return {
                    "head": self.head(),
                    "committed": [],
                    "message": message,
                    "changed": False,
                }
            if "did not match any file(s) known to git" in output:
                # A remove of a path that never existed: nothing to commit is
                # success (S25). A path that exists on disk but is untracked is
                # the ignore-rule failure S30 wants loud.
                if all(
                    not (self.root / relpath).exists() and not self.is_tracked(relpath)
                    for relpath in relpaths
                ):
                    return {
                        "head": self.head(),
                        "committed": [],
                        "message": message,
                        "changed": False,
                    }
            raise MemError("internal_error", f"git commit failed: {output}")
        committed = sorted(
            line.strip()
            for line in self.git(
                "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD", check=False
            ).splitlines()
            if line.strip()
        )
        return {
            "head": self.head(),
            "committed": committed,
            "message": message,
            "changed": True,
        }

    def remove_note(self, relpath: str) -> None:
        """Remove the note from the working tree.

        Staging is deliberately left to :meth:`commit_paths`, which stages with
        ``-A`` for exactly these paths. Staging here as well made git fail on the
        *second* ``add -A``: once the deletion is in the index, the pathspec
        matches nothing and git exits non-zero.
        """
        target = self._contained(relpath)
        if target.exists():
            target.unlink()

    def rollback_path(self, relpath: str, *, restore: bytes | None = None) -> None:
        """Undo a partial write so a failed act leaves the tree clean.

        ``restore`` is the previous content (an update); ``None`` means the note
        did not exist before, so the file is removed. Never raises — a rollback
        that fails must not mask the original error.
        """
        self.git("reset", "-q", "HEAD", "--", relpath, check=False)
        try:
            target = self._contained(relpath)
        except MemError:
            return
        try:
            if restore is None:
                if target.exists():
                    target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(restore)
        except OSError:
            pass

    def index_dirty(self) -> bool:
        """True when the tree holds content that is **not committed**.

        This is *visibility*, not the staging state S23 deleted. The commit window
        is narrowed but not eliminated, so a client must be able to see that the
        repository is not clean (``MEM-RESOLUTIONS.md`` S26).
        """
        return bool(self.git("status", "--porcelain", check=False).strip())

    def project_names(self) -> list[str]:
        paths = self.git("ls-tree", "-r", "--name-only", self._revision(),
                         "--", "projects/", check=False).splitlines()
        return sorted({path.split("/")[1] for path in paths
                       if path.startswith("projects/") and len(path.split("/")) > 2})

    def relpath_for(self, project: str, note_id: str) -> str:
        return note_relpath(project, note_id)
