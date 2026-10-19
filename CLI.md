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

## Reading a note: whole, slice, link-only

`mem read ID` asks for the whole note. The server inlines the body when it fits
the read cap; otherwise it returns metadata plus a signed `fetch_url`.

`mem read ID:5-10` and `mem read ID#Section` ask for a bounded slice. A slice
always inlines that fragment, even when the whole note is over the cap.

`mem read ID --link-only` asks the server to omit the whole body even when it
would fit. The response has `link_only: true`, `truncated` still describes the
cap honestly (usually `false`), and `fetch_url` is present. `--link-only` cannot
be combined with a slice or `--outline`.

Every non-pointer read with a resolvable local origin reports:

| field | meaning |
| --- | --- |
| `origin_path` | the working file on this device |
| `local_file` | whether that file exists and was read |
| `body_matches` | whether the local prose body matches the committed prose body |
| `source` | `same`, `local`, `server`, or `none` |
| `note_updated` | the committed revision's timestamp |
| `timestamp_basis` | present only when an mtime comparison decided `local` vs `server` |

`sha256` identifies the committed file and is the value `mem add --expect`
takes. It is never used to judge freshness: the server owns frontmatter and
rewrites it on upload, so a working file can differ in bytes while its prose is
unchanged. The body-only hash and the local hashes are implementation detail and
are not printed by the CLI.

A slice's `body_matches` compares the whole local file's prose with the whole
committed body, not with the returned fragment. This is deliberate: a partial
view must not hide whether the authoring copy is current.

## Local origin policy

An inline upload records where its content came from on **this** device. That
path must be inside the project root — the directory containing `.mem.toml`.

```text
mem add notes/design.md                 # accepted: inside the root
mem add %TEMP%\draft.md --as design     # refused: origin_outside_root (exit 4)
mem add %TEMP%\draft.md --as design --no-origin
```

The default refusal is a hard error, not a warning. The escape hatches are
distinct:

- copy the file under the project root;
- use `mem pointer add` for an outside file that is a **reference**, not
  content;
- use `--no-origin` to store content without a local authoring link. Outside
  content still needs `--as` because the id cannot be derived from a path
  outside the root.

Paths are resolved before comparison, so `..` and symlinks that escape the root
are refused. UNC origins are refused on Windows. `mem hook` applies the same
rule to captured files.
