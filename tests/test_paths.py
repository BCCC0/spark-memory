"""Cross-platform path translation for a working file recorded by another OS.

The contract these tests defend is not "the conversion is clever" but
**"an unusable path is never handed to a caller"**: a note authored on WSL and
read on Windows (or the reverse) must translate when the mount exists and fall
back to the committed revision when it does not. Fabricating a path would put a
dangling location in front of an agent, which is the failure this whole change
exists to remove.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from mem.paths import (
    looks_like_windows_path,
    looks_like_wsl_path,
    resolve_origin,
    to_windows,
    to_wsl,
    translate,
)


# -- shape detection -------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["D:\\src\\x.md", "d:/src/x.md", "C:\\Users\\alice\\a.md", "Z:\\"],
)
def test_windows_shapes_are_recognised(value):
    assert looks_like_windows_path(value)


@pytest.mark.parametrize(
    "value",
    ["/mnt/d/src/x.md", "/mnt/c/Users/alice/a.md", "/mnt/d", "/mnt/d/"],
)
def test_wsl_shapes_are_recognised(value):
    assert looks_like_wsl_path(value)


@pytest.mark.parametrize(
    "value",
    ["/home/user/x.md", "relative/x.md", "/tmp/x.md", "/media/d/x.md", "~/x.md"],
)
def test_non_mount_paths_are_not_claimed(value):
    """A Linux path outside /mnt is not a Windows drive, and vice versa."""
    assert not looks_like_windows_path(value)
    assert not looks_like_wsl_path(value)


# -- conversions -----------------------------------------------------------


def test_wsl_to_windows_basic():
    assert to_windows("/mnt/d/src/example/notes/a.md") == "D:\\src\\example\\notes\\a.md"


def test_windows_to_wsl_basic():
    assert to_wsl("D:\\src\\example\\notes\\a.md") == "/mnt/d/src/example/notes/a.md"


def test_conversion_is_case_insensitive_and_normalised():
    assert to_windows("/mnt/D/SRC/a.md") == "D:\\SRC\\a.md"
    assert to_wsl("d:/src/a.md") == "/mnt/d/src/a.md"


def test_bare_mount_round_trips():
    assert to_windows("/mnt/d") == "D:\\"
    assert to_wsl("D:\\") == "/mnt/d"


@pytest.mark.parametrize(
    "posix",
    [
        "/mnt/d/src/a.md",
        "/mnt/d/src/with space/a file.md",
        "/mnt/c/Users/alice/.config/mem/credentials",
        "/mnt/d/src/unicode-\u00e9\u00e8/\u4e2d\u6587.md",
    ],
)
def test_round_trip_is_stable(posix):
    windows = to_windows(posix)
    assert windows is not None
    assert to_wsl(windows) == posix


def test_unmapped_or_foreign_paths_yield_none():
    """The whole point: no guess, no fabricated location."""
    assert to_wsl("/home/user/x.md") is None       # POSIX path, no drive
    assert to_windows("relative/x.md") is None     # not a WSL mount
    assert to_windows("/media/d/x.md") is None     # looks mount-ish, is not
    assert to_wsl("\\\\server\\share\\x.md") is None  # UNC, no drive letter


# -- translate (platform-aware) -------------------------------------------


def test_translate_matches_the_current_platform():
    translated = translate("D:\\src\\a.md")
    if sys.platform == "win32":
        assert translated == "D:\\src\\a.md"
    else:
        assert translated == "/mnt/d/src/a.md"


def test_translate_returns_none_for_foreign_shape():
    if sys.platform == "win32":
        assert translate("/home/user/x.md") is None
    else:
        assert translate("\\\\server\\share\\x.md") is None


# -- resolve_origin --------------------------------------------------------


def test_resolve_origin_accepts_a_real_file(tmp_path):
    target = tmp_path / "note.md"
    target.write_text("body\n", encoding="utf-8")
    assert resolve_origin(str(target)) == target


def test_resolve_origin_rejects_directory_and_missing(tmp_path):
    assert resolve_origin(str(tmp_path)) is None
    assert resolve_origin(str(tmp_path / "absent.md")) is None


def test_resolve_origin_handles_absent_values():
    assert resolve_origin(None) is None
    assert resolve_origin("") is None
    assert resolve_origin("   ") is None


def test_resolve_origin_never_raises_on_nonsense():
    """A read must not fail because a recorded hint is odd."""
    for value in ("\x00bad", "C:\\does\\not\\exist.md", "/mnt/zz/x.md", "~"):
        assert resolve_origin(value) is None or isinstance(resolve_origin(value), Path)

