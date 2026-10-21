# Review brief for the next agent — spark-memory 0.2.0

You are reviewing a performance refactor and a write-path redesign in this
repository (`D:\src\sparkmem-public`). Work read-only: do not edit, commit, or
run anything against the production memory service on armbox.

## The change under review

- Baseline before the work: **`2a0a93c`** (v0.1.2: queued writes, `git show` per
  note, text index rebuilt on reads).
- Commits to review: **`3562801`**, **`5270e3e`**, **`2177aa2`**, **`f556dd3`**.
- Diff: `git diff 2a0a93c..HEAD -- src/ client/ tests/ CLI.md ADR-0003-synchronous-writes.md`

Two things changed at once, and they must be judged separately:

1. **Performance:** batched git reads (`cat-file --batch`), a derived catalog
   (metadata, links, hashes) serving `ls`, catalog-driven `mv` planning, and a
   reindex worker that reads only changed notes.
2. **Write path:** the durable write queue was deleted. Every write executes
   inline under one reentrant lock and returns the committed result; the vector
   index is the only deferred work.

## Invariants the review must protect

1. **Git is the record.** Every derived SQLite store (catalog, text index,
   vectors) must be rebuildable from a commit. Deleting them must lose no
   content.
2. **One act = one commit.** A single request commits exactly once; a batch
   commits all paths once; path-limited commits only (never `git commit -a`).
3. **No-op vs failure.** A byte-identical rewrite is a successful
   `changed: false`. An ignored path is a loud failure with the tree rolled
   back. A remove of a never-existing path is a successful no-op.
4. **Atomicity.** A failed write leaves the tree exactly as it was (move journal
   included).
5. **No command waits for indexing.** Writes commit and advance the text index;
   vectors run in the background and report `index_lag`.
6. **Read-after-write.** A committed write is immediately readable and
   text-searchable; no polling or `flush` is required.
7. **Idempotency.** A retried write with the same `Idempotency-Key` returns the
   original result and does not commit twice.

## What to check, by area

### Write path (`src/mem/server/app.py`, `lock.py`, `repo.py`)

- `_run_inline` is the only write entry: does every handler use it? Is the lock
  held across write → commit → index → catalog → enqueue, and released on every
  error path?
- `WriteLock` is now reentrant. Verify the depth counter cannot leak (a failed
  nested acquire, an exception inside `hold`), that `state()` stays accurate,
  and that a second thread still times out at `MEM_LOCK_TIMEOUT`.
- `rm`: existence is judged from HEAD, not the worktree. Reproduce the crash
  window (delete the worktree file, then `mem rm`) and check `changed`,
  `removed`, `already_absent`, rollback on failure.
- `commit_paths`: error mapping for "nothing to commit" vs "pathspec did not
  match" vs real failures. Confirm an ignored path still fails loudly and rolls
  back, and that a path-limited commit cannot pick up unrelated staged content.
- Idempotency: key scoping (`project:device:key`), replay returns the committed
  result, a failed write stores nothing, and the store is bounded.
- Startup: `reset --hard`, legacy `jobs.db` drain, `recover_move` ordering. A
  crash mid-write must leave no uncommitted content and no stuck journal.

### Reindex worker (`Reindexer`, `embedding.py`)

- The pending path set is in memory. Prove it converges after a lost set: commit
  with the worker stopped, start it, and confirm the vector head catches up via
  `git diff`.
- A commit during a pass must be picked up by the next pass; a model change must
  trigger a full rebuild; failure must not block writes or reads.
- The source cache is keyed by committed sha. Check it cannot serve a stale body
  after an edit (the catalog must be ensured first).
- Embedding batches must be non-overlapping and bounded (an earlier bug used
  `start:start+32` with step 8).

### Read model (`catalog.py`, `app.py`, `moves.py`)

- Catalog rows must match the listing fields, warnings, prefix/topic rollups,
  cursor, `full`, and manifest exactly (compare against `git show` by hand).
- `ensure()` incremental vs rebuild must produce identical rows; test delete,
  re-add, rename, broken YAML, non-UTF-8, and an external commit.
- Concurrency: two threads calling `ensure()` must not regress the stamped head.
- `mv --id` planning must fail loudly on unparseable notes rather than silently
  skipping them.

### Clients and docs

- Python CLI and Go client must not poll for a result the server no longer
  queues; check `flush`, `add`, `rm`, `mv`, and hook output.
- Hook batches can reach 1,000 files; the timeout must cover the commit. A hook
  failure must be logged and must not fail the source commit.
- `CLI.md` and `ADR-0003` must match actual behaviour (status codes, blocking,
  `lock_timeout`, `index_lag`).

### Tests — do not trust them blindly

Several tests were rewritten for the new semantics. Verify they still assert the
behaviour, not just the implementation: the batch test, the lock-contention
test, the replay test, the crash/rollback tests, and the hook batch test. Check
for deleted assertions (queue internals) that removed coverage of atomicity or
idempotency.

## How to run

```powershell
cd D:\src\sparkmem-public
.venv\Scripts\python.exe -m pytest -q              # full suite
cd client; go build ./...; go test ./...; cd ..
```

Performance claims: use a **copy** of a repository under a temp directory, never
the production memory repository on armbox. Useful probes:

```powershell
git -C <tmp> cat-file --batch          # one spawn for many blobs
git -C <tmp> add -A -- <paths>; git -C <tmp> commit -qm x -- <paths>
```

## Report format

Return a ranked list — **blocker / high / medium / low** — each with
`file:line`, a one-line description, why it matters, and a suggested fix.
Separate “verified by running X” from “read only”. End with a verdict: keep,
keep with fixes, or revert, naming the minimum fixes before merge.
