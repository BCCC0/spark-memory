# `mem` CLI behavior

This is the user-facing contract for queued writes.

## Identifying the client and daemon

```text
$ mem --version
mem 0.1.2 (go, commit 5b1338c56dfb)

$ mem --version
mem 0.1.2 (python, commit 5b1338c56dfb)

$ memd --version
memd 0.1.2 (python, commit 5b1338c56dfb)
```

The implementation name is part of the output because both the Go client and the
Python library install a command called `mem`. A released Go binary is installed
as `mem`; a development build is named `mem-go` so it does not silently shadow a
release on `PATH`. The Python CLI lives inside the project virtualenv and is
normally invoked with `uv run mem`.

## What waits, blocks, or returns a hint

| Command or path | Before returning | Blocks the operation? | Visible result |
| --- | --- | --- | --- |
| `mem add` | Check every local Markdown summary, upload bytes, and receive durable server acceptance | **Yes** for invalid format, rejected upload, full queue, or an ID already in an unfinished job | `queued` receipt on acceptance; never claims a Git commit already happened |
| `mem rm`, `mem mv` | Validate the request and receive durable server acceptance | **Yes** for an invalid request or busy affected ID; a move also requires the project's queue to be empty | `queued` receipt; `mem mv --dry-run` remains an immediate preview |
| Git pre-commit hook | Check selected staged Markdown locally | **Yes** when selected Markdown has an invalid summary | Format error identifying the staged file |
| Git post-commit / post-merge hook | Upload selected committed state and receive durable server acceptance | **No**: a sync failure is logged and does not undo the source Git operation | Accepted receipt or logged warning; a busy file requires a later manual `mem hook update` |
| `mem read ID` | Check the committed Git snapshot and unfinished/failed writes affecting the ID | **No** waiting for the queue | Committed record, `pending` hint, or failed-write error with the last committed revision |
| `mem read ID@REV` | Read the requested committed revision | **No** waiting for the queue | Historical record, regardless of current queued work |
| `mem search` | Search the latest completed index | **No** waiting for writes or indexing | Hits plus a pending-write hint and index-lag indicator |
| `mem flush` | Wait for every job accepted before the command starts and for the text index to reach the resulting head, bounded to 3 minutes | **Yes**, intentionally waits | `flushed` with a Git head, a typed error listing failed jobs, or `lock_timeout` (exit 3) when the index cannot catch up |

“Blocks” in this table means *refuses the requested action*. Uploading a large
request and durably recording it still take time. No CLI read or search command
waits for the write queue to empty. `mem flush` is the explicit barrier; it
captures a finite set of already accepted jobs, so later writes do not extend
its wait. There is no `--wait` option on read or search and no separate job-status
command.

Failed receipts remain visible to `mem flush` after the queue drains. Its error
lists those receipts. A later successful write for every affected ID resolves
the earlier failure for future flushes. If the queue drains but the text index
cannot catch up within 3 minutes, `mem flush` fails with `lock_timeout` and exit
3 rather than waiting forever; the response and `mem index` show the reindexer's
last error.

## Hints, warnings, and errors

- **`queued`** means the server has durably accepted a write. It is not a
  committed or indexed result. The response includes a receipt and affected IDs,
  but no new Git head.
- **`pending`** on an ordinary read means that ID has an unfinished write. It
  includes the receipt and last committed revision; old content must not be
  presented as the new write. Multi-read gives each target its own state.
- **`failed`** on a read means an accepted write could not be committed. It
  includes the typed error and last committed revision. A caller can retry a
  new write after the failed job releases its ID reservation.
- **Search queue hint** reports how many accepted writes for the selected
  project remain unfinished. Search results may omit those writes. Index lag is
  reported separately because a completed Git commit may still be absent from
  the latest index.
- **Hook warning** is logged after a source Git commit when upload or queue
  acceptance fails. It does not turn the completed Git commit into a failure.
  A busy selected file leaves the hook watermark behind and requires a manual
  `mem hook update` after the file clears.
- **Hook update output** includes `receipts` and `status: "queued"` when writes
  were accepted. Its `added` list names paths submitted to the queue; it does
  not mean those paths have committed yet.
- **Format and busy errors** are blocking for direct write commands. `mem add`
  prechecks authored YAML summaries locally; the server validates again before
  accepting. A second operation touching an unfinished ID is rejected rather
  than queued behind it.

To observe read-after-write completion, use `mem flush` or poll `mem read ID`
until its `pending` hint disappears. A successful read then returns the
committed revision. To
observe search freshness, inspect both the queue hint and index-lag indicator;
an empty result alone is not proof that a queued addition is absent.
