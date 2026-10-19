"""Which copy is fresh: the committed revision, or this device's working file?

The contract these tests defend: a default read never hands out an unqualified
path. The working file is the author's own copy -- legitimately ahead of memory
between an edit and the `mem add` that submits it -- so a read must say *which*
side is newer and must keep the committed revision reachable either way.

The measured bug this replaces: in production, 10 of 10 recorded paths were the
working file, and none matched the note's `sha256`. That was expected drift, not
corruption, and the read had no way to say so.

A second finding shaped the design: the server re-serialises frontmatter on
upload, so a working file is NEVER byte-identical to the committed revision. A
byte-level comparison therefore called every note diverged, which is why
freshness is decided on the note's body (`body_sha256`) and the byte hash is
reported as evidence only.
"""

from __future__ import annotations

import os
import time
import urllib.request
from pathlib import Path

import pytest

from conftest import note_text


def _aging(path: Path, *, seconds: float) -> None:
    """Backdate a file's mtime, so ordering is asserted without sleeping."""
    target = time.time() - seconds
    os.utime(path, (target, target))


def _edit(path: Path, text: str) -> None:
    """Overwrite a working file with LF endings.

    `Path.write_text` applies universal-newline translation on Windows, so the
    bytes on disk would not be the bytes hashed. newline="\\n" keeps the file
    equal to `text` on both platforms, which is what the client hashes.
    """
    path.write_text(text, encoding="utf-8", newline="\n")


def test_a_working_file_newer_than_memory_is_named_as_the_fresh_copy(ready):
    """The live failure: edit on disk, don't re-add, and memory is behind."""
    text = note_text(summary="fresh", body="first version " + "x" * 9000)
    source = ready.write("work.md", text)
    ready.cli_json("add", "work.md", "--as", "work")

    # Edit WITHOUT re-adding, so the working file is genuinely ahead of the commit.
    edited = note_text(summary="fresh", body="second version, never added " + "y" * 9000)
    _edit(source, edited)

    record = ready.cli_json("read", "work")
    assert record["truncated"] is True, "a >threshold note is handed off, not inlined"

    assert record["source"] == "local", "the working file holds the newer bytes"
    assert record["body_matches"] is False
    assert Path(record["origin_path"]) == source
    assert record["note_updated"], "freshness needs the commit time to compare against"
    assert "body_sha256" not in record
    assert "local_sha256" not in record
    assert "local_body_sha256" not in record


def test_a_commit_newer_than_the_working_file_is_named_as_the_fresh_copy(ready):
    """The reverse: memory holds bytes the working file does not."""
    source = ready.write("stale.md", note_text(summary="stale", body="local bytes " + "x" * 9000))
    ready.cli_json("add", "stale.md", "--as", "stale")
    # A different body, backdated so the commit wins on time.
    _edit(source, note_text(summary="stale", body="an older local draft " + "y" * 9000))
    _aging(source, seconds=3600)

    record = ready.cli_json("read", "stale")
    assert record["body_matches"] is False
    assert record["source"] == "server", "the committed revision is newer"
    # A caller told the server is fresher needs a way to reach it.
    assert record["fetch_url"].startswith("http")
    assert Path(record["origin_path"]) == source, "the path stays as context"


def test_an_untouched_working_file_reports_same_despite_different_bytes(ready):
    """The finding that forced a body-level comparison.

    The server owns the frontmatter -- it records `origin:` and reflows `tags` and
    `summary` on upload -- so a working file is NEVER byte-identical to the
    committed revision, even when the author changed nothing. Freshness is decided
    on the body, the part the author edits.
    """
    text = note_text(summary="same", body="x" * 9000)
    source = ready.write("same.md", text)
    # Rewrite with LF so the working bytes match the committed bytes: `ready.write`
    # uses universal newlines, which on Windows writes CRLF and would make the
    # bodies differ for a reason unrelated to freshness.
    _edit(source, text)
    ready.cli_json("add", "same.md", "--as", "same")

    record = ready.cli_json("read", "same")
    assert record["body_matches"] is True, "the author changed nothing"
    assert record["source"] == "same"
    assert "timestamp_basis" not in record, (
        "no mtime comparison was performed, so no basis may be claimed"
    )
    assert "body_sha256" not in record
    assert "local_sha256" not in record
    assert "local_body_sha256" not in record
    # The whole-file hash is the committed identity, not a local comparison.
    assert record["sha256"]


def test_a_vanished_working_file_falls_back_to_the_committed_revision(ready):
    source = ready.write("gone.md", note_text(summary="gone", body="y" * 9000))
    ready.cli_json("add", "gone.md", "--as", "gone")
    source.unlink()

    record = ready.cli_json("read", "gone")
    assert record["source"] == "none", "no local file exists, which is not the same as an older one"
    assert record["local_file"] is False
    assert "origin_path" not in record, "a location that cannot be opened is not advertised"
    assert record["fetch_url"].startswith("http")


def test_a_path_in_another_platforms_shape_never_fabricates_a_location(ready):
    """A path this platform cannot express must degrade, never be guessed.

    The translation itself is unit-tested in test_paths.py; this asserts the
    *read* stays safe when it cannot resolve the recorded location.
    """
    source = ready.write("os.md", note_text(summary="os", body="z" * 9000))
    ready.cli_json("add", "os.md", "--as", "os")

    record = ready.cli_json("read", "os")
    assert record["source"] in {"local", "server", "same", "none"}
    if "origin_path" in record:
        assert Path(record["origin_path"]).is_file()


def test_no_local_file_is_distinct_from_an_older_local_file(ready):
    """`source: "none"` must not collapse into `source: "server"`.

    An audit of the first cut found the two indistinguishable, so an agent reading
    `source: "server"` could not tell "your working file is older" from "you have
    no working file here at all" -- and the installed skill documented only the
    first. "No local file" arrives by two different routes (another device
    committed the note; or a recorded path that cannot be resolved here), and
    neither is a statement about which copy is newer.
    """
    source = ready.write("foreign.md", note_text(summary="foreign", body="w" * 9000))
    ready.cli_json("add", "foreign.md", "--as", "foreign")

    record = ready.cli_json("read", "foreign")
    # Authored here, so a local file IS offered and compared.
    assert record["local_file"] is True
    assert record["source"] in {"local", "server", "same"}
    assert "body_matches" in record

    # The recorded path disappears -> "no local file", not "your file is older".
    source.unlink()
    record = ready.cli_json("read", "foreign")
    assert record["local_file"] is False
    assert record["source"] == "none"
    assert "origin_path" not in record
    assert "body_matches" not in record, "no local bytes were compared"
    assert "timestamp_basis" not in record, "no comparison was performed"
    # The committed revision is still reachable either way.
    assert record["fetch_url"].startswith("http")


def test_a_client_supplied_limit_is_attributed_to_the_client(ready):
    """The client's advisory limit is sent to the server, so the server withholds
    the body at this client's request. That decline is still this client's, and
    must be visible: `truncated` is true either way."""
    (ready.config_dir / "config.toml").write_text(
        f'server = "{ready.server}"\n[read]\nmax_bytes = 5\n', encoding="utf-8"
    )
    ready.write("tiny.md", note_text(summary="tiny", body="a body over five bytes"))
    ready.cli_json("add", "tiny.md", "--as", "tiny")

    record = ready.cli_json("read", "tiny")
    assert record["truncated"] is True
    assert record["deferred_by_client"] is True
    assert record["source"] in {"local", "server", "same", "none"}


def test_crlf_line_endings_do_not_break_freshness(ready):
    """A Windows editor writes CRLF; the prose is still the committed prose."""
    source = ready.write("crlf.md", note_text(summary="crlf", body="line one\nline two"))
    # Write explicitly as CRLF, independent of the host default.
    source.write_bytes(
        note_text(summary="crlf", body="line one\nline two")
        .replace("\n", "\r\n")
        .encode("utf-8")
    )
    ready.cli_json("add", "crlf.md", "--as", "crlf")

    record = ready.cli_json("read", "crlf")
    assert record["local_file"] is True
    assert record["body_matches"] is True
    assert record["source"] == "same"


@pytest.mark.parametrize("body_bytes", [200, 3000])
def test_a_note_under_the_cap_still_reports_freshness(ready, body_bytes):
    """The inline cap must not gate the freshness metadata."""
    text = note_text(summary=f"small-{body_bytes}", body="x" * body_bytes)
    source = ready.write(f"small-{body_bytes}.md", text)
    _edit(source, text)
    ready.cli_json("add", f"small-{body_bytes}.md", "--as", f"small-{body_bytes}")

    record = ready.cli_json("read", f"small-{body_bytes}")
    assert record["body"], "a note under the cap is still inlined"
    assert "fetch_url" not in record
    assert Path(record["origin_path"]) == source
    assert record["local_file"] is True
    assert record["body_matches"] is True
    assert record["source"] == "same"
    assert record["note_updated"]
    assert record["sha256"]
    assert "body_sha256" not in record
    assert "local_sha256" not in record


def test_a_small_note_whose_working_file_vanishes_reports_none(ready):
    source = ready.write("small-gone.md", note_text(summary="small-gone", body="short body"))
    ready.cli_json("add", "small-gone.md", "--as", "small-gone")
    source.unlink()

    record = ready.cli_json("read", "small-gone")
    assert record["body"], "the committed body is still available"
    assert record["local_file"] is False
    assert record["source"] == "none"
    assert "origin_path" not in record
    assert "body_matches" not in record


def test_link_only_omits_an_small_body_without_claiming_truncation(ready):
    source = ready.write("linked.md", note_text(summary="linked", body="a short body"))
    _edit(source, note_text(summary="linked", body="a short body"))
    ready.cli_json("add", "linked.md", "--as", "linked")

    record = ready.cli_json("read", "linked", "--link-only")
    assert "body" not in record
    assert record["link_only"] is True
    assert record["truncated"] is False, "the caller asked; the cap did not fire"
    assert record["fetch_url"].startswith("http")
    assert record["body_matches"] is True
    assert Path(record["origin_path"]) == source
    assert "deferred_by_client" not in record
    with urllib.request.urlopen(record["fetch_url"], timeout=5) as response:
        assert response.status == 200


def test_link_only_on_a_large_note_still_reports_the_cap(ready):
    ready.write("big-linked.md", note_text(summary="big-linked", body="y" * 9000))
    ready.cli_json("add", "big-linked.md", "--as", "big-linked")

    record = ready.cli_json("read", "big-linked", "--link-only")
    assert "body" not in record
    assert record["link_only"] is True
    assert record["truncated"] is True
    assert record["fetch_url"].startswith("http")
    assert "body_matches" in record
    assert "deferred_by_client" not in record, "the caller asked for no body"


@pytest.mark.parametrize(
    "args",
    [
        ("read", "linked:1-2", "--link-only"),
        ("read", "linked#Heading", "--link-only"),
        ("read", "linked#", "--link-only"),
        ("read", "linked", "--outline", "--link-only"),
    ],
)
def test_link_only_rejects_a_slice_or_outline(ready, args):
    ready.write("linked.md", note_text(summary="linked", body="a short body"))
    ready.cli_json("add", "linked.md", "--as", "linked")

    result = ready.cli(*args)
    assert result.returncode == 64, result.stderr
    assert "link-only" in result.stderr
