# ADR-0002: `mem mv` moves either a working file or a note identity

**Status:** accepted. **Date:** 2026-10-19.
**Scope:** the meaning of `mem mv`, the origin-path lookup it uses, and the
blocking queue semantics of a move.
**Related:** [CLI.md](CLI.md), [ADR-0001](ADR-0001-go-client-python-server.md).

## 1. Context

A note has two coordinates:

```text
note ID      = shared, project-scoped identity
origin.path  = this device's authoring file
```

They are deliberately independent. A note can be authored as
`notes/backlog.md` but identified as `ops/backlog`; the same ID can live at
different paths on Windows, WSL, and Linux. The previous `mem mv` moved only the
note ID, so a local `git mv` or a filesystem rename left `origin.path` stale.

A separate `retarget` verb was proposed to repair origin without changing the
body. It works, but it adds a second move vocabulary and still does not move the
local file. Path-first `mem mv` gives callers one obvious command for a file
move while keeping `--id` for the logical move.

## 2. Decision

1. `mem mv OLD_PATH NEW_PATH` moves the working file. The note ID is unchanged.
2. `mem mv --id OLD_ID NEW_ID` moves the note identity and references. Local
   files are unchanged.
3. Path mode is strictly in-project: both paths must resolve inside the current
   `.mem.toml` root, and neither may belong to a nested/other project.
4. Path mode updates only `origin.path`. The body, title, frontmatter values
   other than `origin`, and note ID do not change.
5. The server resolves the note by `origin.device == caller` and
   `origin.path == old_path`; the client does not discover IDs.
6. `old_path == new_path` is a successful no-op.
7. Both forms block: wait for the write queue to drain, submit an exclusive
   move, then wait for its job to commit.
8. A path move renames the local file before the durable commit and renames it
   back if submission or the job fails.
9. Legacy outside-root origins are not a path-mode exception. They are reported
   by `mem status --origins` and may be repaired manually.
10. Hooks do not auto-detect `git mv`; `mem hook` continues to add the new path
    and report the old path as orphaned.

## 3. Consequences

- Existing `mem mv ID ID` calls must become `mem mv --id ID ID`.
- A missing old file is a typed failure, not a server-only move.
- Cross-project moves are refused.
- A crash between the local rename and the server commit can leave the two
  states inconsistent; `mem status --origins` is the audit.
- `mem mv` is slower than `add`/`rm` because it is a queue barrier, not a
  fire-and-forget write.
- The four historical `%TEMP%` notes are a one-time manual migration, not a
  reason to keep a legacy path exception.

## 4. Alternatives rejected

- **Keep `mem mv` server-only and add `retarget`:** two verbs, and still no
  first-class local move.
- **Let old paths be outside the root:** reopens the policy hole the Phase 1
  guard closed.
- **Derive the new file path from the new ID:** IDs and filesystem layout are
  independent; custom IDs and gitignored `notes/` directories break it.
- **Run `git mv` inside `mem mv`:** stages the user’s repository and fails for
  untracked/gitignored files.
- **Fire-and-forget queued move:** local and server state can diverge before the
  caller sees a result.
