"""The reindex trigger lives on the write path (`S32`).

Basic Memory schedules its vector sync from the write path and lets a read serve what is
already ready (`index/local_schedulers.py`); `mem` previously synced **inside the search
request**, on the single worker's event loop, which is what made a cold hybrid search
take `/health` down with it for ~98 s.

The embedding half of that needs the model and is verified on a host with the model cached. What is
verifiable anywhere is the *trigger*: a write must schedule the pass, and no read may be
required to start it.
"""

from __future__ import annotations

import time

from conftest import note_text


def test_a_write_schedules_a_reindex_pass(ready):
    """No request has to trigger the sync — the write path does (S32).

    `search` is deliberately never called. The assertion is on the *trigger* counter,
    not on completion: whether the pass then succeeds (model cached) or gets
    stuck failing to load a model (a machine without one) is a different question, and
    conflating the two is what made the first version of this test time out while the
    daemon was behaving correctly.
    """
    before = ready.cli_json("index")["reindex"]
    ready.write("n.md", note_text(summary="scheduled", body="scheduledtoken"))
    ready.cli_json("add", "n.md", "--as", "n")

    deadline = time.monotonic() + 30.0
    last: dict = {}
    while time.monotonic() < deadline:
        last = ready.cli_json("index")["reindex"]
        if last["requested"] > before["requested"]:
            return
        time.sleep(0.5)
    raise AssertionError(f"the write path never requested a reindex: {last}")


def test_a_text_search_still_sees_a_note_written_a_moment_ago(ready):
    """The property that was deliberately KEPT (S32).

    The text index still rebuilds on the read path — off the loop, with no model — so
    writing a note and searching for it immediately keeps working. Moving this to the
    background as well would have made text-only `mem` return nothing until a task
    happened to finish, which is a worse trade than the one the measured outage needed.
    """
    ready.write("n.md", note_text(summary="fresh", body="freshtoken"))
    ready.cli_json("add", "n.md", "--as", "n")

    hits = ready.cli_json("search", "freshtoken")["hits"]
    assert [hit["id"] for hit in hits] == ["n"]


def test_removing_a_note_schedules_a_pass_and_leaves_the_index(ready):
    ready.write("n.md", note_text(summary="doomed", body="doomedtoken"))
    ready.cli_json("add", "n.md", "--as", "n")
    before = ready.cli_json("index")["reindex"]
    ready.cli_json("rm", "n")

    deadline = time.monotonic() + 30.0
    last: dict = {}
    while time.monotonic() < deadline:
        last = ready.cli_json("index")["reindex"]
        if last["requested"] > before["requested"]:
            # The text index is maintained on the read path, so the removal is visible
            # without waiting for the background pass.
            assert ready.cli_json("search", "doomedtoken")["hits"] == []
            return
        time.sleep(0.5)
    raise AssertionError(f"rm never requested a reindex pass: {last}")
