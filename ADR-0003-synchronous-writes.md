# ADR-0003: synchronous writes instead of a durable write queue

**Status:** accepted. **Date:** 2026-10-21.
**Scope:** the write path, the `/v1/queue` contract, and crash recovery.
**Related:** [ADR-0002](ADR-0002-mv-path-and-identity.md), [CLI.md](CLI.md).

## 1. Context

Version 0.1 accepted every write into a durable queue (`write-queue/jobs.db`
plus a spool directory) and returned a `202` receipt. One consumer thread then
committed the job. That bought three things: acceptance survived a crash before
the commit, retries were idempotent, and Git-hook batches never blocked the
committing process.

It also cost a second write of every payload (upload spool + worktree), 100 ms
of client polling for a single note, a persistent `jobs.db` to prune, a spool to
sweep, and a mirror worktree checkout per job. Measured on armbox, a commit is
~60 ms; the queue's overhead was comparable to the work it deferred.

## 2. Decision

1. Every write executes inline under the single in-process `WriteLock` and
   returns the committed result. No queue, no spool, no receipts, no polling.
2. `add-many` and moves are inline too; a move still journals before touching
   the tree.
3. Idempotency moves to `IdempotencyStore`: a keyed map of the committed result,
   replayed on retry.
4. Crash recovery is `git reset --hard HEAD` at startup plus the move journal.
5. `/v1/queue` stays as a compatibility stub returning `pending: 0`; `mem flush`
   remains a no-op command for scripts.
6. The only deferred work is the vector index: an in-memory set of changed paths
   feeding a background worker, rebuildable from `git diff` at any time.
7. `WriteLock` becomes reentrant so the inline path and the per-verb commit
   helpers can nest in one thread.

## 3. Consequences

- A client waits for its own commit (~60–90 ms for one note; ~0.6 s for a
  1,000-file batch). Hooks block for the batch.
- A crash between write and commit leaves a dirty tree; startup repairs it and
  the client retries. A retry of an already-committed add is a byte-identical
  no-op; `mv`/`repath` replay through the idempotency store.
- Concurrent writers serialise on the lock; a 15 s timeout returns
  `lock_timeout` (exit 3).
- There is no cross-process queue, so a second daemon on the same repository is
  still unsupported (B1) — unchanged.
- Upgrade path: a 0.1.x `jobs.db` with pending jobs is drained inline once at
  the first 0.2.0 start, then ignored.
