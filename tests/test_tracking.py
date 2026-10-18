"""Auto-track: pattern matching, git introspection, and the capture watermark.

The contract: declaring patterns selects exactly the paths meant, a capture reads
what git recorded rather than what is on disk, and the watermark makes a repeated
run a no-op so a hook that fires twice cannot double-write memory.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mem import tracking
from mem.config import read_marker_patterns, read_marker_sections, write_marker_patterns
from mem.errors import MemError


# -- pattern matching ------------------------------------------------------


@pytest.mark.parametrize(
    "patterns, path, expected",
    [
        (["docs/**/*.md"], "docs/a.md", True),
        (["docs/**/*.md"], "docs/sub/deep/a.md", True),
        (["docs/**/*.md"], "other/a.md", False),
        (["docs/**/*.md"], "docs/a.txt", False),
        (["**/*.md"], "a.md", True),
        (["**/*.md"], "deep/a.md", True),
        (["*.md"], "deep/a.md", True),
        (["ADR-*.md"], "docs/ADR-0001.md", True),
        (["docs/"], "docs/deep/a.md", True),
        (["docs/"], "docsx/a.md", False),
        (["notes/*.md"], "notes/deep/a.md", False),
        (["docs/**/*.md", "!docs/drafts/**"], "docs/drafts/x.md", False),
        (["docs/**/*.md", "!docs/drafts/**"], "docs/keep.md", True),
        (["!*.md"], "a.md", False),
        (["docs/**/*.md"], "docs/a b.md", True),
    ],
)
def test_pattern_matching(patterns, path, expected):
    assert tracking.matches_patterns(patterns, path) is expected


def test_declaring_nothing_tracks_nothing():
    """An empty list must not mean "everything" — that would capture a whole repo."""
    assert tracking.matches_patterns([], "README.md") is False
    assert tracking.matches_patterns([""], "README.md") is False


def test_tracked_paths_preserves_order():
    paths = ["b.md", "keep.txt", "a.md"]
    assert tracking.tracked_paths(["*.md"], paths) == ["b.md", "a.md"]


# -- git introspection -----------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", ".")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    return repo


def test_changed_paths_lists_committed_files(git_repo):
    (git_repo / "a.md").write_text("a\n", encoding="utf-8")
    (git_repo / "b.txt").write_text("b\n", encoding="utf-8")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "first")
    rev = _git(git_repo, "rev-parse", "HEAD").strip()

    assert sorted(tracking.changed_paths(rev, git_repo)) == ["a.md", "b.txt"]


def test_changed_paths_handles_the_root_commit(git_repo):
    """The very first commit has no parent, which a naive `rev^..rev` cannot express."""
    (git_repo / "a.md").write_text("a\n", encoding="utf-8")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "root")
    rev = _git(git_repo, "rev-parse", "HEAD").strip()

    assert tracking.is_root_commit(rev, git_repo) is True
    assert tracking.changed_paths(rev, git_repo) == ["a.md"]


def test_changed_paths_over_a_range(git_repo):
    (git_repo / "a.md").write_text("a\n", encoding="utf-8")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "one")
    first = _git(git_repo, "rev-parse", "HEAD").strip()

    (git_repo / "b.md").write_text("b\n", encoding="utf-8")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "two")
    second = _git(git_repo, "rev-parse", "HEAD").strip()

    assert tracking.changed_paths(second, git_repo, since=first) == ["b.md"]


def test_changed_paths_tolerates_awkward_filenames(git_repo):
    """`-z` exists so a space or a quote cannot silently break matching."""
    name = "a file with spaces.md"
    (git_repo / name).write_text("x\n", encoding="utf-8")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "spaces")
    rev = _git(git_repo, "rev-parse", "HEAD").strip()

    assert tracking.changed_paths(rev, git_repo) == [name]
    assert tracking.matches_patterns(["*.md"], name) is True


def test_committed_bytes_reads_the_revision_not_the_disk(git_repo):
    """The whole point: an uncommitted edit must not reach memory."""
    target = git_repo / "a.md"
    target.write_text("committed\n", encoding="utf-8")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "one")
    rev = _git(git_repo, "rev-parse", "HEAD").strip()

    target.write_text("uncommitted draft\n", encoding="utf-8", newline="\n")

    assert tracking.committed_bytes(rev, "a.md", git_repo) == b"committed\n"
    assert target.read_bytes() == b"uncommitted draft\n"


def test_committed_bytes_raises_a_typed_error_for_an_absent_path(git_repo):
    (git_repo / "a.md").write_text("a\n", encoding="utf-8")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "one")
    rev = _git(git_repo, "rev-parse", "HEAD").strip()

    with pytest.raises(MemError) as caught:
        tracking.committed_bytes(rev, "nope.md", git_repo)
    assert caught.value.code == "not_found"


# -- marker round-trip -----------------------------------------------------


def test_marker_patterns_round_trip(tmp_path):
    marker = tmp_path / ".mem.toml"
    marker.write_text('project = "p"\nserver = "http://x:8321"\n', encoding="utf-8")

    write_marker_patterns(marker, ["docs/**/*.md", "!docs/drafts/**"])
    assert read_marker_patterns(marker) == ["docs/**/*.md", "!docs/drafts/**"]
    assert read_marker_sections(marker)["project"] == "p"
    assert read_marker_sections(marker)["server"] == "http://x:8321"

    write_marker_patterns(marker, [])
    assert read_marker_patterns(marker) == []
    assert read_marker_sections(marker)["project"] == "p"


def test_marker_writer_preserves_comments_and_foreign_sections(tmp_path):
    """Regression: the marker used to be rebuilt from scratch, losing everything else."""
    marker = tmp_path / ".mem.toml"
    marker.write_text(
        '# a comment worth keeping\nproject = "p"\n\n[other]\nkey = "value"\n',
        encoding="utf-8",
    )

    write_marker_patterns(marker, ["*.md"])
    text = marker.read_text(encoding="utf-8")

    assert "# a comment worth keeping" in text
    assert "[other]" in text and 'key = "value"' in text
    assert read_marker_sections(marker)["other"] == {"key": "value"}
    assert read_marker_patterns(marker) == ["*.md"]


# -- watermark -------------------------------------------------------------


def test_watermark_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("MEM_CONFIG_DIR", str(tmp_path / "cfg"))
    repo = tmp_path / "r"
    repo.mkdir()

    assert tracking.read_watermark(repo) is None
    tracking.write_watermark(repo, "abc123")
    assert tracking.read_watermark(repo) == "abc123"

    tracking.write_watermark(repo, "def456")
    assert tracking.read_watermark(repo) == "def456"


def test_watermark_is_per_repository(tmp_path, monkeypatch):
    monkeypatch.setenv("MEM_CONFIG_DIR", str(tmp_path / "cfg"))
    one = tmp_path / "one"
    two = tmp_path / "two"
    one.mkdir()
    two.mkdir()

    tracking.write_watermark(one, "aaa")
    assert tracking.read_watermark(two) is None
    tracking.write_watermark(two, "bbb")
    assert tracking.read_watermark(one) == "aaa"
    assert tracking.read_watermark(two) == "bbb"


def test_a_corrupt_watermark_is_recoverable(tmp_path, monkeypatch):
    """Refusing to run on a bad state file would strand the hook with no way out."""
    config = tmp_path / "cfg"
    monkeypatch.setenv("MEM_CONFIG_DIR", str(config))
    config.mkdir(parents=True)
    (config / tracking.STATE_FILE).write_text("{not json", encoding="utf-8")

    repo = tmp_path / "r"
    repo.mkdir()
    assert tracking.read_watermark(repo) is None
    tracking.write_watermark(repo, "fresh")
    assert tracking.read_watermark(repo) == "fresh"


def test_repo_root_is_none_outside_a_repository(tmp_path):
    assert tracking.repo_root(tmp_path) is None
