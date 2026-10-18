"""Capability URLs — reads with no device token (``MEM-RESOLUTIONS.md`` S31).

The property under test is not "a URL exists" but "a caller with **no token** can read
a note over the inline threshold, and cannot read anything it was not granted": not
another note, not history, not past its expiry, not after a rotation.

Driven over real HTTP against the harness daemon, like ``test_protocol.py`` — the
things that break here (the middleware gate, the signature material, the socket peer
address) are exactly the things an in-process call would not exercise.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from pathlib import Path

from conftest import note_text
from mem.server.app import Settings, _link_signature, build_record
from mem.server.repo import MemoryRepo

BIG_BODY = "w" * 9000  # comfortably over the 4096-byte inline threshold


def _get(url: str, token: str | None = None) -> tuple[int, bytes]:
    request = urllib.request.Request(url)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _big_note(ready) -> None:
    ready.write("a.md", note_text(summary="a big note", body=BIG_BODY))
    ready.cli_json("add", "a.md", "--as", "big")


def _secret(ready) -> str:
    return ready.link_secret_file.read_text(encoding="utf-8").strip()


def _remote_record(ready, revision=None):
    settings = Settings(
        repo_root=ready.repo, devices_db=ready.tmp / "mem" / "devices.db",
        enroll_secret=ready.secret_file, state_dir=ready.state,
        base_url=ready.server, link_secret=ready.link_secret_file,
    )
    return build_record(MemoryRepo(ready.repo), "demo", "big", settings, revision=revision)


def test_a_signed_url_reads_a_large_note_with_no_token(ready):
    """The whole point: no header, no credentials file, just the URL."""
    _big_note(ready)
    record = _remote_record(ready)
    assert record["truncated"] is True, "a 9 KB note must be handed off, not inlined"
    uri = record["fetch_url"]

    assert uri.startswith(ready.server), "absolute, built from the configured base_url"
    assert "e=" in uri and "s=" in uri

    status, body = _get(uri)  # deliberately NO Authorization header
    assert status == 200

    # It serves exactly what an authenticated fetch serves.
    _, authed = _get(f"{ready.server}/raw/demo/big", ready.token())
    assert body == authed
    assert BIG_BODY[:50].encode() in body


def test_raw_still_requires_a_credential(ready):
    """No signature and no token is still a 401 — the gate is unchanged."""
    _big_note(ready)
    assert _get(f"{ready.server}/raw/demo/big")[0] == 401


def test_the_bearer_token_still_works(ready):
    """The capability path is additive: it removed no existing capability."""
    _big_note(ready)
    assert _get(f"{ready.server}/raw/demo/big", ready.token())[0] == 200


def test_an_expired_signature_is_refused(ready):
    """Signed with the real key, for a time that has already passed."""
    _big_note(ready)
    past = int(time.time()) - 10
    sig = _link_signature(_secret(ready), "demo", "big", past)
    assert _get(f"{ready.server}/raw/demo/big?exp={past}&sig={sig}")[0] == 401


def test_a_tampered_signature_is_refused(ready):
    _big_note(ready)
    uri = _remote_record(ready)["fetch_url"]
    # Changing the final base64url character may affect only unused padding bits.
    flipped = uri.replace("&s=", "&s=x", 1)
    assert flipped != uri
    assert _get(flipped)[0] == 401


def test_a_signature_for_another_note_is_refused(ready):
    _big_note(ready)
    ready.write("b.md", note_text(summary="another note"))
    ready.cli_json("add", "b.md", "--as", "other")

    exp = int(time.time()) + 600
    sig = _link_signature(_secret(ready), "demo", "other", exp)  # minted for `other`
    assert _get(f"{ready.server}/raw/demo/big?exp={exp}&sig={sig}")[0] == 401


def test_history_cannot_be_read_through_a_latest_link(ready):
    """``rev`` is inside the signature, so appending one invalidates the link.

    Without that, a holder could read every past revision through a URL that was only
    ever meant to expose the current note.
    """
    _big_note(ready)
    uri = _remote_record(ready)["fetch_url"]
    head = ready.cli_json("status")["head"]
    assert _get(f"{uri}&rev={head}")[0] == 401


def test_rotating_the_secret_invalidates_outstanding_urls(ready):
    _big_note(ready)
    uri = _remote_record(ready)["fetch_url"]
    assert _get(uri)[0] == 200

    ready.stop()
    ready.link_secret_file.write_text("rotated-" + "a" * 32, encoding="utf-8")
    ready.start()

    assert _get(uri)[0] == 401, "rotation is the kill switch for every outstanding link"
    # A fresh read mints a link that works, so the client is unaffected.
    fresh = _remote_record(ready)["fetch_url"]
    assert _get(fresh)[0] == 200


def test_an_origin_device_read_carries_the_source_path(ready):
    """The caller's own working file, *and* a way to reach the committed revision.

    These used to be mutually exclusive, which meant the one device most likely to
    host an agent could not reach what memory actually holds. The working file is a
    hint whose bytes may be ahead of or behind the commit, so the URL must survive
    alongside it.
    """
    _big_note(ready)
    record = ready.cli_json("read", "big")

    assert "origin_path" in record, "a caller on this box may read the file directly"
    assert Path(record["origin_path"]).is_file()
    assert Path(record["origin_path"]).name == "a.md"
    assert "fetch_url" in record, "the committed revision stays reachable"
    assert "s=" in record["fetch_url"]
    # The client hashed the working file and decided which copy is fresh.
    assert record["source"] in {"local", "server", "same", "none"}
    assert record["local_sha256"]
    assert record["note_updated"], "freshness needs the commit time to compare against"


def test_a_remote_read_gets_no_local_path(ready):
    """Unit-level, because the harness cannot originate a non-loopback connection.

    Every end-to-end read here arrives over 127.0.0.1, so this asserts the other
    branch directly against the projection function rather than pretending to be
    remote. The URL must be present in both shapes.
    """
    _big_note(ready)
    remote = _remote_record(ready)
    assert "origin_path" not in remote
    assert "s=" in remote["fetch_url"]
    assert remote["content_sha256"], "the committed bytes are always identified"

    local = build_record(MemoryRepo(ready.repo), "demo", "big", Settings(
        repo_root=ready.repo, devices_db=ready.tmp / "mem" / "devices.db",
        enroll_secret=ready.secret_file, state_dir=ready.state,
        base_url=ready.server, link_secret=ready.link_secret_file,
    ), caller_device_id=ready.cli_json("devices")[0]["device_id"])
    assert "origin_path" in local
    assert "fetch_url" in local, "the URL is no longer replaced by the path"
    assert local["content_sha256"] == local["sha256"]
    assert local["note_updated"]


def test_a_pinned_read_is_unsigned_and_needs_a_token(ready):
    """A latest-version capability cannot address a revision, so pinning stays bearer-only."""
    _big_note(ready)
    head = ready.cli_json("status")["head"]
    record = _remote_record(ready, revision=head)
    uri = record["fetch_url"]

    assert "r=" in uri, "a pinned read addresses a revision"
    assert "s=" not in uri, "a capability link covers the latest version only"
    assert _get(uri)[0] == 401
    assert _get(uri, ready.token())[0] == 200
