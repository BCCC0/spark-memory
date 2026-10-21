"""The flush barrier is bounded, like the Go client's."""

from __future__ import annotations

import argparse

import pytest

from mem import cli
from mem.errors import MemError


def test_flush_times_out_with_a_typed_error(monkeypatch):
    class StuckClient:
        def queue_progress(self, through=None):
            return {
                "through": 7,
                "pending": 1,
                "head": "new",
                "indexed_head": "old",
                "failed": [],
            }

    monkeypatch.setattr(cli.cfg, "load", lambda **_kwargs: object())
    monkeypatch.setattr(cli, "_client", lambda _config, _args: StuckClient())
    monkeypatch.setattr(cli, "FLUSH_TIMEOUT", 0.01)

    with pytest.raises(MemError) as expired:
        cli.cmd_flush(argparse.Namespace(human=False))

    assert expired.value.code == "lock_timeout"
    assert expired.value.exit_code == 3
    assert expired.value.detail == {"through": 7}


def test_flush_does_not_wait_for_the_index(monkeypatch, capsys):
    """A drained queue is a successful flush even when the index is behind."""

    class IndexBehindClient:
        def queue_progress(self, through=None):
            return {
                "through": 9,
                "pending": 0,
                "head": "new",
                "indexed_head": "old",
                "failed": [],
            }

    monkeypatch.setattr(cli.cfg, "load", lambda **_kwargs: object())
    monkeypatch.setattr(cli, "_client", lambda _config, _args: IndexBehindClient())
    monkeypatch.setattr(cli, "FLUSH_TIMEOUT", 5.0)

    assert cli.cmd_flush(argparse.Namespace(human=False)) == 0
    assert '"status":"flushed"' in capsys.readouterr().out
