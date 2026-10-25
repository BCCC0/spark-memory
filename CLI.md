# `mem` CLI behavior

This is the user-facing contract for writes.

## Identifying the client and daemon

```text
$ mem --version
mem 0.2.0 (go, commit 3ad159e511a8)

$ mem --version
mem 0.2.0 (python, commit 3ad159e511a8)

$ memd --version
memd 0.2.0 (python, commit 3ad159e511a8)
```

The implementation name is part of the output because both the Go client and the
Python library install a command called `mem`. A released Go binary is installed
as `mem`; a development build is named `mem-go` so it does not silently shadow a
release on `PATH`. The Python CLI lives inside the project virtualenv and is
normally invoked with `uv run mem`.

## What waits, blocks, or returns a hint

| Command or path | What happens before it returns | Blocking | Visible result |
| --- | --- | --- | --- |
| `mem add` | Validate, write, commit, and advance the text index | **Yes**, until the commit returns | `committed` result with the new `head` |
| `mem rm` | Validate and version the deletion | **Yes**, until the commit returns | `committed` result (`changed`, `removed`) |
| `mem mv` | Plan and commit under the write lock, journaling first | **Yes**, until its own commit returns | Final move result |
| Git pre-commit hook | Check selected staged Markdown locally | **Yes** when selected Markdown has an invalid summary | Format error identifying the staged file |
| Git post-commit / post-merge hook | Upload each batch and wait for its commit | **Yes**, for the upload and commit; a failure is logged and never undoes the source commit | `added`, `receipts`, and `errors` |
| `mem read ID` | Read the committed snapshot from the catalog | No | Committed record |
| `mem read ID@REV` | Read the requested committed revision | No | Historical record |
| `mem search` | Search the latest text index; vectors may lag | No | Hits plus `index_lag` |
| `mem flush` | Compatibility command; writes are already committed | Trivially | `flushed` with the current head |

Writes are synchronous. A single in-process lock serialises them; a concurrent
writer waits up to `MEM_LOCK_TIMEOUT` (15 s) and then receives `lock_timeout`
(exit 3). There is no spool, no job database, no receipt to poll, and no `202`.
`mem flush` remains for scripts and returns immediately, because there is
nothing queued.

Embeddings are not on this path at all. They run in a background pass and report
their lag (`index_lag`, `status.hybrid.indexed_head`); a write is durably
committed and text-searchable before any vector is computed, and no command
waits for one.

## Hints, warnings, and errors

- **`committed`** means the write is in the head commit when the command
  returns. `changed: false` is a successful byte-identical no-op, not a failure.
- **`lock_timeout`** means another writer held the write lock for longer than
  `MEM_LOCK_TIMEOUT` (15 s). The action was refused; nothing was written.
- **Search index lag** is reported separately (`index_lag`); a committed write is
  text-searchable immediately, while vectors may follow in the background.
- **Hook warnings** are logged when a batch upload or commit fails. They never
  turn the source Git commit into a failure. A failed batch leaves the hook
  watermark behind; a later `mem hook update` retries it.
- **Hook update output** includes `receipts` and the per-batch result. Its
  `added` list names paths that were committed.
- **Format errors** are blocking: `mem add` prechecks authored YAML summaries
  locally and the server validates again before writing.

Read-after-write is immediate: no polling and no `mem flush` is needed. `flush`
remains as a compatibility command and returns the current head.

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

## Moving files and note IDs

There are two distinct moves:

```text
mem mv OLD_PATH NEW_PATH        # working file; note ID unchanged
mem mv --id OLD_ID NEW_ID       # note ID/topic; local file unchanged
```

Path mode is strictly in-project:

- both paths must resolve inside the directory containing `.mem.toml`;
- a nested `.mem.toml` in either path is another project and is refused;
- the old file must exist; a missing file is `local_file_missing`;
- the destination must not exist, except for a case-only rename;
- the destination parent must already exist;
- only `origin.path` changes; the body and note ID do not.

ID mode keeps the existing topic/reference semantics and never touches local
files. Use `--expect` for path mode and `--expect-head` for ID mode.

Both forms block until the server commits:

1. plan the move against the current HEAD;
2. submit it under the write lock (a move journals before touching the tree);
3. wait for its commit and report the final result.

`mem flush` is a compatibility no-op: writes are already committed when the
command returns. A path move renames the local file first and renames it back
if the server commit fails. There is still no cross-system transaction: a crash
between the local rename and the commit can leave them inconsistent.
`mem status --origins` reports that state.

Hooks do not auto-detect `git mv`; a `git mv` captured by `mem hook` appears as a
new path plus an orphaned old path, not as a move.

`mem status --origins` is the read-only audit: it reports this device's origins
as `ok`, `missing`, `outside_root`, `cross_project`, `ambiguous`, `remote`, or
`unresolved`. It exits 0 and is opt-in because it scans the project's notes and
local files.

## Restoring and exporting

`mem pull` restores missing local files in the current project. With no IDs it
selects every note this device owns; with IDs it restores only those. Existing
files are never overwritten. A missing/unusable origin is reported and left
alone.

Inline text files (`.txt`) store bytes without YAML frontmatter and therefore
have no `origin`; they are content-only and are reported as `missing_origin` by
`pull` and `status --origins`. Use a note if local freshness/pull matters.

```bash
mem pull
mem pull ops/backlog
```

`mem export PROJECT --dst DIR` writes every note/text in a named project into a
clean directory. It does not require `.mem.toml`, does not read `origin`, and
does not update `origin`.

```bash
mem export demo --dst D:\backup\demo-notes
```

`--dst` must be missing or empty and must not have a `.mem.toml` ancestor. Export
preserves the ID tree:

```text
notes/a.md
notes/b.txt
```

Neither command creates a server commit. Export leaves its files unregistered;
use `mem add` or `mem mv` if they should become authoring files. Both commands
are deliberately non-overwriting, so there is no `--force` or conflict-marker
session.

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
