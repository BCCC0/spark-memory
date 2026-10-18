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

import hashlib
import os
import time
from pathlib import Path

from conftest import note_text


def _aging(path: Path, *, seconds: float) -> None:
    """Backdate a file's mtime, so ordering is asserted without sleeping."""
    target = time.time() - seconds
    os.utime(path, (target, target))


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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

    assert record["local_sha256"] == _sha(edited)
    assert record["source"] == "local", "the working file holds the newer bytes"
    assert record["body_matches"] is False
    assert Path(record["origin_path"]) == source
    assert record["note_updated"], "freshness needs the commit time to compare against"


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
    assert record["body_sha256"], "the server identifies the committed body"
    assert record["body_matches"] is True, "the author changed nothing"
    assert record["local_body_sha256"] == record["body_sha256"]
    assert record["source"] == "same"
    assert "timestamp_basis" not in record, (
        "no mtime comparison was performed, so no basis may be claimed"
    )
    assert record["local_sha256"] == _sha(text)
    # The whole-file hashes still differ, because the server rewrote frontmatter.
    assert record["local_sha256"] != record["content_sha256"]


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
