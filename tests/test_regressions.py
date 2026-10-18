"""Regressions for the findings of the 2026-10-07 spec/implementation audit.

Every test here corresponds to a defect that shipped in the first commit
(``f882ec8``) with a green suite, or to a property the audit showed was asserted
but never actually verified. The point of this file is that these failures are
loud next time.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

from conftest import note_text
from mem.client import Client
from mem.keys import add_key


# -- F1: `updated` was the OLDEST commit date ---------------------------------


def test_updated_tracks_the_newest_commit(ready):
    ready.write("a.md", note_text(summary="alpha"))
    ready.cli_json("add", "a.md", "--as", "a")

    # git timestamps have one-second granularity, so the two commits must be
    # clearly separated or the comparison below cannot distinguish them.
    time.sleep(1.2)

    ready.write("a.md", note_text(summary="alpha revised"))
    ready.cli_json("add", "a.md", "--as", "a")

    row = next(n for n in ready.cli_json("ls", "--full")["notes"] if n["id"] == "a")
    assert row["updated"] > row["created"], (
        "a note revised twice must have updated > created; equal values mean the "
        "oldest commit is being reported as the modification time"
    )


def test_ls_default_order_is_newest_first(ready):
    ready.write("first.md", note_text(summary="first"))
    ready.cli_json("add", "first.md", "--as", "first")

    ready.write("second.md", note_text(summary="second"))
    ready.cli_json("add", "second.md", "--as", "second")

    ids = [n["id"] for n in ready.cli_json("ls")["notes"]]
    assert ids[0] == "second", f"newest must sort first, got {ids}"


def test_ls_since_includes_a_just_committed_note(ready):
    ready.write("old.md", note_text(summary="old"))
    ready.cli_json("add", "old.md", "--as", "old")

    ready.write("new.md", note_text(summary="new"))
    ready.cli_json("add", "new.md", "--as", "new")

    recent = [n["id"] for n in ready.cli_json("ls", "--since", "1h")["notes"]]
    assert "new" in recent, f"--since must not exclude a just-committed note: {recent}"


# -- S23: staging is gone, a write is a commit --------------------------------


def test_a_fresh_project_is_clean(ready):
    """`index_dirty` is the visibility that replaced the staging list (S26)."""
    status = ready.cli_json("status")
    assert "staging" not in status, "staging was deleted, not renamed"
    assert status["index_dirty"] is False, (
        "a fresh project has nothing uncommitted -- a dirty tree here would mean "
        "init left something behind"
    )


def test_add_commits_and_reports_the_path(ready):
    ready.write("n.md", note_text(summary="n"))
    out = ready.cli_json("add", "n.md", "--as", "n")
    assert out["path"] == "projects/demo/n.md"
    assert out["message"], "a derived message must still be recorded"
    # One write IS one commit: the tree is clean the instant it returns.
    assert ready.cli_json("status")["index_dirty"] is False


def test_derived_message_is_mechanical_but_truthful(ready):
    ready.write("m.md", note_text(summary="m"))
    out = ready.cli_json("add", "m.md", "--as", "m")
    assert out["message"] == "mem: add m"
    assert ready.cli_json("read", "m")["message"] == "mem: add m"


def test_explicit_message_lands_on_the_read_record(ready):
    ready.write("m.md", note_text(summary="m"))
    ready.cli_json("add", "m.md", "--as", "m", "-m", "explain the m note")
    assert ready.cli_json("read", "m")["message"] == "explain the m note"


# -- S23/S25/S28: `mem rm` is the other half of the write surface -------------


def test_rm_removes_and_versions(ready):
    ready.write("gone.md", note_text(summary="doomed"))
    ready.cli_json("add", "gone.md", "--as", "gone")

    out = ready.cli_json("rm", "gone", "-m", "remove the doomed note")
    assert out["removed"] is True
    assert out["message"] == "remove the doomed note"
    assert ready.cli_json("status")["index_dirty"] is False

    # The id no longer resolves -- which is what exit 9 was always for.
    after = ready.cli("read", "gone")
    assert after.returncode == 9, f"expected exit 9, got {after.returncode}"
    assert json.loads(after.stderr)["error"]["code"] == "target_moved"

    listed = [n["id"] for n in ready.cli_json("ls")["notes"]]
    assert "gone" not in listed


def test_rm_is_idempotent_by_rule(ready):
    """No key needed: an absent id with no precondition is the desired end state."""
    result = ready.cli_json("rm", "never-existed")
    assert result["already_absent"] is True
    assert result["removed"] is False
    assert result["message"] is None
    assert ready.cli_json("status")["index_dirty"] is False


def test_rm_with_a_stale_precondition_is_refused(ready):
    ready.write("p.md", note_text(summary="p"))
    ready.cli_json("add", "p.md", "--as", "p")
    stale = ready.cli_json("read", "p")["sha256"]

    ready.write("p.md", note_text(summary="p v2"))
    ready.cli_json("add", "p.md", "--as", "p")

    result = ready.cli("rm", "p", "--expect", stale)
    assert result.returncode == 7, f"expected exit 7, got {result.returncode}"
    assert ready.cli_json("read", "p")["summary"] == "p v2", "the note must survive"


def test_rm_of_an_absent_id_with_a_precondition_is_stale(ready):
    result = ready.cli("rm", "never-existed", "--expect", "0" * 64)
    assert result.returncode == 7, f"expected exit 7, got {result.returncode}"


def test_rm_leaves_the_note_recoverable_from_history(ready):
    """`id ≡ path` means a delete destroys a coordinate, not the content."""
    ready.write("h.md", note_text(summary="history keeps me", body="the payload"))
    ready.cli_json("add", "h.md", "--as", "h", "-m", "add h")
    revision = ready.cli_json("status")["head"]

    ready.cli_json("rm", "h")
    assert ready.cli("read", "h").returncode == 9

    # The revision still resolves -- this is the whole recovery story, and the
    # reason `mem restore` is not a verb.
    old = ready.cli_json("read", f"h@{revision}")
    assert "the payload" in old["body"]


# -- S26: a write is all-or-nothing -------------------------------------------


def test_index_dirty_is_visible(ready):
    """The deterministic half of S26: an unclean tree must be *observable*.

    Staging was deleted as a state, not as a thing a client can be told about --
    if the server can be left with uncommitted content, the client must be able
    to see it rather than discover it as a mystery note later.
    """
    assert ready.cli_json("status")["index_dirty"] is False
    (ready.repo / "projects" / "demo" / "stray.md").write_text("x", encoding="utf-8")
    assert ready.cli_json("status")["index_dirty"] is True


def test_killing_the_daemon_mid_write_leaves_the_tree_clean(subprocess_harness):
    """The race S23 claimed to delete was replaced by a crash race -- closed here.

    The lock and the commit both live in the daemon, so unlike the client-death
    test this kills the thing that can actually be caught mid-act. The assertion
    holds whatever the timing: never half-applied.

    Uses `subprocess_harness`, not `harness`: this test's whole subject is a real
    process dying, so it cannot run against the in-process threaded daemon.
    """
    harness = subprocess_harness
    harness.login()
    harness.init("demo")
    harness.write("crash.md", note_text(summary="interrupted"))

    proc = subprocess.Popen(
        [sys.executable, "-m", "mem.cli", "add", "crash.md", "--as", "crash"],
        env=harness.env,
        cwd=str(harness.workdir),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(0.05)
    harness.process.kill()
    harness.process.wait(timeout=10)
    proc.wait(timeout=30)

    # Restart and inspect: either the note is fully committed, or it is absent.
    # Never present-but-uncommitted.
    harness.start()
    assert harness.cli_json("status")["index_dirty"] is False, (
        "a killed write left the repository unclean -- the crash race S26 exists "
        "to close"
    )


# -- F3: `mem read <id>@<revision>` -------------------------------------------


def test_read_at_revision_pins_old_content(ready):
    ready.write("r.md", note_text(summary="v1", body="first version"))
    first = ready.cli_json("add", "r.md", "--as", "r", "-m", "v1")["head"]

    ready.write("r.md", note_text(summary="v2", body="second version"))
    ready.cli_json("add", "r.md", "--as", "r", "-m", "v2")

    old = ready.cli_json("read", f"r@{first}")
    assert "first version" in old["body"], "the @revision form must resolve the old content"
    assert old["revision"] == first
    assert old["message"] == "v1", "a pinned read reports ITS revision's message"
    assert "first version" in old["body"], "the @revision form must resolve the old content"
    assert old["revision"] == first

    current = ready.cli_json("read", "r")
    assert "second version" in current["body"]


# -- F4: --outline leaked the body --------------------------------------------


def test_outline_returns_headings_and_no_body(ready):
    body = "Intro text.\n\n## Alpha\n\nstuff\n\n### Beta\n\nmore\n"
    ready.write("o.md", note_text(summary="o", title="Outline", body=body))
    ready.cli_json("add", "o.md", "--as", "o")

    out = ready.cli_json("read", "o", "--outline")
    assert "body" not in out, "--outline must never carry the body"
    assert [h["text"] for h in out["headings"]] == ["Outline", "Alpha", "Beta"]
    assert [h["level"] for h in out["headings"]] == [1, 2, 3]


def test_outline_of_a_large_note_transfers_no_body(ready):
    ready.write("big.md", note_text(summary="big", body="w" * 9000))
    ready.cli_json("add", "big.md", "--as", "big")

    out = ready.cli_json("read", "big", "--outline")
    assert "body" not in out, "--outline must not download the note it is outlining"
    assert len(json.dumps(out)) < 1000, "an outline must stay small"


# -- F8: scores were synthetic; --mode hybrid silently downgraded -------------


def test_search_ranks_the_better_match_first(ready):
    ready.write(
        "many.md",
        note_text(summary="many", title="Many", body="zebra " * 8 + "filler " * 40),
    )
    ready.write(
        "one.md",
        note_text(summary="one", title="One", body="zebra " + "filler " * 40),
    )
    ready.cli_json("add", "many.md", "--as", "many")
    ready.cli_json("add", "one.md", "--as", "one")

    hits = ready.cli_json("search", "zebra")["hits"]
    assert len(hits) == 2, f"expected two hits, got {hits}"
    assert hits[0]["id"] == "many", "the higher term-frequency note must rank first"
    scores = [h["score"] for h in hits]
    assert scores == sorted(scores, reverse=True), f"scores must be best-first: {scores}"


def test_an_unknown_search_mode_is_refused_by_the_server(ready):
    """S16 still holds — hybrid is implemented now, but a mode that is *not* must
    never be answered with a different question."""
    request = urllib.request.Request(
        f"{ready.server}/v1/search?project=demo&q=x&mode=semantic",
        headers={"Authorization": f"Bearer {ready.token()}"},
    )
    try:
        urllib.request.urlopen(request, timeout=10)
    except urllib.error.HTTPError as exc:
        assert exc.code == 400, f"expected a typed 400, got {exc.code}"
        body = json.loads(exc.read())
        assert body["error"]["code"] == "invalid_request"
        assert "hybrid" in body["error"]["detail"].get("available", [])
    else:  # pragma: no cover
        raise AssertionError("an unknown search mode must be refused")


# -- F9/F10: untyped 500s; status index shape ---------------------------------


def test_bad_limit_is_a_typed_error(ready):
    request = urllib.request.Request(
        f"{ready.server}/v1/notes?project=demo&limit=abc",
        headers={"Authorization": f"Bearer {ready.token()}"},
    )
    try:
        urllib.request.urlopen(request, timeout=10)
    except urllib.error.HTTPError as exc:
        assert exc.code == 400, f"expected a typed 400, got {exc.code}"
        body = json.loads(exc.read())
        assert body["error"]["code"] == "invalid_request"
    else:  # pragma: no cover
        raise AssertionError("an unparseable limit must not succeed")


def test_status_reports_index_age(ready):
    ready.write("s.md", note_text(summary="s", body="indextoken"))
    ready.cli_json("add", "s.md", "--as", "s")
    ready.cli_json("search", "indextoken")  # forces the index to build

    index = ready.cli_json("status")["index"]
    assert "age" in index, "MEM-SERVER.md requires an index age, not just a flag"
    # `text` without the `embed` extra, `hybrid` with it — never claimed otherwise.
    assert index["kind"] in ("text", "hybrid")
    assert isinstance(index["semantic"], bool)
    assert index["present"] is True


# -- F13 / B2: the client threshold was inert ---------------------------------


def test_client_threshold_defers_even_when_the_server_inlines(ready):
    # The client's advisory limit is lower than the server's, so the server
    # inlines and the client must still decline the body.
    (ready.config_dir / "config.toml").write_text(
        f'server = "{ready.server}"\n[read]\nmax_bytes = 5\n', encoding="utf-8"
    )
    ready.write("c.md", note_text(summary="c", body="a body comfortably over five bytes"))
    ready.cli_json("add", "c.md", "--as", "c")

    out = ready.cli_json("read", "c")
    assert "body" not in out, "the client's own threshold must be consulted"
    assert out["truncated"] is True
    # The client sends its advisory limit to the server, so the SERVER withholds the
    # body here -- at this client's request. `deferred_by_client` records that the
    # decline was this client's, distinguishing it from a server-side default.
    assert out["deferred_by_client"] is True
    assert out["source"] in {"local", "server", "same", "none"}


# -- S12: the client never sent origin_path, so origin echoed the note id ------


def test_origin_records_the_client_path_not_the_note_id(ready):
    source = ready.write("origin.md", note_text(summary="origin"))
    ready.cli_json("add", "origin.md", "--as", "origin")

    from mem.server.frontmatter import parse
    origin = parse(Client(ready.server, ready.token()).raw("demo", "origin"))[0]["origin"]
    assert origin["path"] == str(source.resolve()), (
        "origin.path must be the file on the client; falling back to the note id "
        "makes the field say nothing the id did not already say"
    )
    assert origin["device"], "the device half is injected from the token"


# -- C6: a filesystem failure must not escape as a traceback ------------------


def test_unwritable_config_dir_is_a_typed_error(harness):
    """The CLI must fail with a code and one JSON object, never a traceback.

    This is not hypothetical: a real enrollment was lost this way -- the device
    was created server-side, the credentials write crashed, and the one-time
    token was gone.
    """
    blocker = harness.tmp / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    harness.env["MEM_CONFIG_DIR"] = str(blocker / "config")

    result = harness.cli(
        "login", "--server", harness.server, "--name", "x", "--secret", "test-secret"
    )
    assert result.returncode != 0
    assert "Traceback" not in result.stderr, f"a traceback escaped:\n{result.stderr}"
    payload = json.loads(result.stderr.splitlines()[0])
    assert payload["error"]["code"] == "internal_error"


# -- C7: the CAS was checked outside the write lock ---------------------------


def test_concurrent_expect_writes_allow_exactly_one(ready):
    """Two clients asserting the same revision: one wins, one is refused.

    With the compare-and-swap checked *outside* the write lock, both clients can
    read the same pre-write hash, both pass the check, and both apply — the lost
    update invariant C7 exists to prevent. The loser is refused either before the
    winner commits (the target is busy, exit 3) or after it (the hash changed, exit
    7); both are safe refusals, and which one wins the race is not the invariant.
    """
    ready.write("race.md", note_text(summary="original"))
    ready.cli_json("add", "race.md", "--as", "race")
    stale = ready.cli_json("read", "race")["sha256"]

    results: dict[str, int] = {}
    guard = threading.Lock()

    def one(tag: str) -> None:
        ready.write(f"race-{tag}.md", note_text(summary=f"from {tag}"))
        out = ready.cli("add", f"race-{tag}.md", "--as", "race", "--expect", stale)
        with guard:
            results[tag] = out.returncode

    threads = [threading.Thread(target=one, args=(tag,)) for tag in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    outcomes = sorted(results.values())
    assert outcomes[0] == 0, f"exactly one concurrent --expect write must win: {results}"
    assert outcomes[1] in (3, 7), (
        f"the loser must be refused as busy or stale, not accepted: {results}"
    )


# -- S5: idempotency was asserted by the code, not verified -------------------


def test_idempotency_replays_without_rewriting(ready):
    """A replayed key must return the original result *and* leave the note alone.

    Driven at the API level so the key is fixed and the second body genuinely
    differs -- the CLI would derive a different key from different content, which
    is why the original test could not detect a broken replay.
    """
    client = Client(ready.server, ready.token())

    first = client.add(
        "demo", "idem", note_text(summary="first").encode(),
        filename="idem.md", idempotency_key=add_key("k", "idem", None),
    )
    second = client.add(
        "demo", "idem", note_text(summary="second").encode(),
        filename="idem.md", idempotency_key=add_key("k", "idem", None),
    )

    assert second["idempotent_replay"] is True
    assert second["receipt"] == first["receipt"]
    ready.flush()
    stored = client.note("demo", "idem")
    assert stored["summary"] == "first", "a replayed stage must not overwrite the note"


def test_idempotency_key_distinguishes_message_and_origin(ready):
    """Metadata changes are new requests, not replays of an older commit."""
    client = Client(ready.server, ready.token())
    content = note_text(summary="same content").encode()
    digest = hashlib.sha256(content).hexdigest()

    first = client.add(
        "demo", "metadata", content,
        filename="metadata.md",
        idempotency_key=add_key(
            digest, "metadata", None,
            origin_path="C:/first/metadata.md", message="first message",
        ),
        origin_path="C:/first/metadata.md",
        message="first message",
    )
    ready.flush()
    second = client.add(
        "demo", "metadata", content,
        filename="metadata.md",
        idempotency_key=add_key(
            digest, "metadata", None,
            origin_path="C:/second/metadata.md", message="second message",
        ),
        origin_path="C:/second/metadata.md",
        message="second message",
    )
    ready.flush()

    assert second["idempotent_replay"] is False
    assert second["receipt"] != first["receipt"]
    from mem.server.frontmatter import parse
    stored = parse(client.raw("demo", "metadata"))[0]
    assert stored["origin"]["path"] == "C:/second/metadata.md"
    assert client.note("demo", "metadata")["message"] == "second message"


# -- F6: the device log was unreachable ---------------------------------------


def test_device_log_and_retirement_revokes(ready):
    rows = ready.cli_json("devices")
    assert rows, "at least the enrolling device must be listed"
    device_id = rows[0]["device_id"]
    assert rows[0]["retired_at"] is None

    assert ready.cli_json("devices", "--retire", device_id)["retired"] == device_id

    # Retirement is real revocation, not a cosmetic flag: the token stops working.
    after = ready.cli("devices")
    assert after.returncode == 8, f"a retired token must be refused, got {after.returncode}"


# -- minor: search --project ---------------------------------------------------


def test_search_accepts_an_explicit_project(ready):
    other = ready.tmp / "other"
    other.mkdir(exist_ok=True)
    ready.cli_json("init", "other", cwd=other)
    ready.write("o.md", note_text(summary="other note", body="specialtoken"), directory=other)
    ok = ready.cli("add", "o.md", "--as", "o", cwd=other)
    assert ok.returncode == 0, ok.stderr
    ready.flush()

    hits = ready.cli_json("search", "specialtoken", "--project", "other")
    assert [h["id"] for h in hits["hits"]] == ["o"]


# -- S30: exit 5 was emitted although three documents call it reserved --------


def test_readding_identical_content_is_a_no_op(ready):
    """Re-adding byte-identical content is success, not exit 5 (S30).

    This used to raise `staging_diverged` -- a code `MEM-CLI.md` §4 and
    `MEM-SERVER.md` §6 both declare reserved, and which no test covered -- from the
    one path that could reach it: a re-add whose idempotency key differs because
    `--expect` differs, so the replay is skipped, the CAS passes, and `git add -A`
    stages nothing.
    """
    ready.write("n.md", note_text(summary="no-op"))
    first = ready.cli_json("add", "n.md", "--as", "n")
    assert first["changed"] is True

    # A different --expect changes the key, so this is not a replay: it reaches the
    # commit layer and finds nothing to stage.
    second = ready.cli_json("add", "n.md", "--as", "n", "--expect", first["sha256"])
    assert second["changed"] is False
    assert second["head"] == first["head"], "a no-op must not move HEAD"
    assert second["sha256"] == first["sha256"]

    # The identical re-add (same key) is still reported as a replay, not a no-op.
    replay = ready.cli_json("add", "n.md", "--as", "n")
    assert replay["idempotent_replay"] is True


def test_rm_versions_a_deletion_that_head_still_holds(ready):
    """A tracked note missing from the worktree must still be versioned (S30).

    Reachable through S26's crash window -- a hard kill between the unlink and the
    commit -- or an operator deleting the file. The earlier code decided purely on
    `target.is_file()`, returned before ever committing, and reported
    `already_absent: true, changed: false` while HEAD still held the note and the
    tree stayed dirty: `rm`'s contract ("remove **and version**") was unmet.
    """
    ready.write("n.md", note_text(summary="crash window"))
    added = ready.cli_json("add", "n.md", "--as", "n")

    tracked = ready.repo / "projects" / "demo" / "n.md"
    assert tracked.is_file(), f"unexpected layout: {tracked}"
    tracked.unlink()  # the crash: worktree copy gone, HEAD untouched
    assert ready.cli_json("status")["index_dirty"] is True

    result = ready.cli_json("rm", "n")
    assert result["changed"] is True, "the deletion must be versioned"
    assert result["already_absent"] is False  # staged from the committed tree
    assert result["removed"] is True
    assert result["head"] != added["head"], "a real commit must move HEAD"

    # The tree is clean again, and the note is gone for good.
    assert ready.cli_json("status")["index_dirty"] is False
    assert ready.cli("read", "n").returncode == 9


def test_queue_idempotency_survives_restart(ready):
    """A committed queue receipt remains replayable after daemon restart."""
    ready.write("n.md", note_text(summary="durable replay"))
    first = json.loads(ready.cli("add", "n.md", "--as", "n").stdout)
    ready.flush()
    head = ready.cli_json("status")["head"]

    ready.stop()
    ready.start()

    replay = json.loads(ready.cli("add", "n.md", "--as", "n").stdout)
    assert replay["idempotent_replay"] is True
    assert replay["receipt"] == first["receipt"]
    assert ready.cli_json("status")["head"] == head
