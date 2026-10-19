---
name: spark-memory
description: Use the `mem` CLI to read and write durable, shared project memory served by spark-memory. Covers enrollment, nested topics, pointer records, device scope, compact listings, the JSON/exit-code contract, and atomic writes. Use when recalling durable project facts or decisions, or recording a new one.
whenToUse: When you need project knowledge that must outlive this conversation — a decision, a convention, a hard-won lesson — or when you are about to make a decision that memory might already answer. Prefer this over notes kept in chat, which are lost.
---

# spark-memory — shared project memory

A **service** owns everything: the only Git repository, the write lock, the search
index, and the device registry. Clients are **stateless** — there is no clone, no
sync, and no local copy of memory. You exchange **coordinates**, not bodies.

Do not create a second local memory store. Notes in the conversation, a scratch
file, or a private vector store are all competing copies of something that
already has a home.

## Before anything else

Check that you are pointed at a service and enrolled:

```bash
mem status     # exit 0 means reachable and enrolled
```

If it fails, fix it — do not retry:

| Exit | Meaning | Fix |
|---|---|---|
| `2` | server unreachable | correct `--server` / `MEM_SERVER` / `config.toml` |
| `6` | no `.mem.toml` found | `mem init <project>` in the working directory |
| `8` | not enrolled, or device retired | `mem login --server <url>` (needs the enrollment secret) |

Neither the server URL nor the project can be guessed. Ask for them rather than
inventing a default.

## Reading — search first, bodies last

**Search before you answer from memory, and before you decide.** Memory is the
record of record; a stale recollection that contradicts it is worse than no
recollection.

```bash
mem search "why sqlite over postgres"      # FTS5 keyword search
mem search "write lock" --mode hybrid      # semantic; needs the `embed` extra
mem ls ops/ --tag thermal --limit 20       # browse one topic level
mem manifest ops                           # inventory the whole nested subtree
mem read ops/decisions --outline           # headings only — cheap
mem read ops/decisions                     # the body — only once you need it
mem read ops/boot ops/recovery              # ordered records for several IDs
```

- `ls` returns compact coordinates: `id`, `summary`, `status`, `type`, `updated`,
  and warnings when present. Use `mem ls --full` only when you need the extended
  metadata. The HTTP listing surface uses the same compact default, and
  listings do not return fetch URLs.
- `search` returns ranked coordinates and summaries, not bodies. Reading every
  body is how a large memory becomes unusable.
- Use `--outline` to decide whether a large note is worth reading.
- `mem read <id>@<revision>` pins a specific revision when you need what was
  true at the time.
- A read tells you **which copy is fresh**, because the working file and the
  committed revision routinely differ and either may be newer. This applies to
  small notes and slices too, not only handoffs:
  - `local_file`: `true` when a working file was offered and compared, `false`
    when none was. **Read this first** — without it, a `source` value is
    ambiguous.
  - `source`: `local` (your working file is newer), `server` (the committed
    revision is newer), `same` (identical prose), or `none` (**no local file
    exists or it could not be resolved** — this is not a statement that the
    committed revision is newer). `none` is also what you get when another device
    committed the note.
  - `body_matches`: whether your working file's **prose body** matches the
    committed body. Compare on the body: the server owns the frontmatter and
    rewrites it on upload, so whole-file hashes always differ, even for a note
    you never edited. A frontmatter-only edit (summary, status, tags) is
    invisible to it, and the `summary`/`status`/`tags` in the payload are the
    **server's** values, not your file's. A slice reports this for the whole
    file, not for the returned fragment.
  - `timestamp_basis` appears **only** when an mtime comparison actually ran, and
    only means the ordering is wall-clock based — a skewed clock can invert it.
  - `sha256` identifies the committed file and is what `mem add --expect` takes.
    Do not compare it to your working file to decide freshness; use
    `body_matches`. `fetch_url` is signed and short lived — it is a credential,
    so do not paste it into notes or transcripts.
  - `origin_path` is where **this device** can open the file, not necessarily where
    the author uploaded it from (paths recorded by another OS are translated).
    Provenance requires fetching the raw file.
  - If the path cannot be resolved on this machine it is omitted entirely; fall
    back to `fetch_url`, `--outline`, or a section read (`read <id>#<section>`).
  - The same device reached from WSL and from Windows sees the same note; paths
    recorded by one OS are translated for the other, and an untranslatable path is
    dropped rather than guessed.
  - A **section read** (`read <id>#<section>`) reports freshness against the
    whole note while returning only the section. It cannot be combined with
    `--link-only`.
  - `--link-only` omits the body even when it fits, returns `link_only: true`
    and a signed `fetch_url`, and keeps the freshness fields. `truncated` still
    reports the real cap result.

When adding content, the authoring path must stay inside the project root
(directory containing `.mem.toml`). An outside path is refused with
`origin_outside_root`; copy it in, use `mem pointer add` for a reference, or use
`--no-origin --as <id>` to store content without a local link.

## Following a repo automatically (only when the user asks)

`mem hook` captures a repository's commits into memory on its own, so prefer a
deliberate `mem add` for anything you judge worth remembering. Reach for the hook
when the user wants files followed continuously:

```bash
mem hook add 'docs/**/*.md'   # declare; '!' entries exclude
mem hook init                 # install into ./.git/hooks (per checkout)
mem hook update --dry-run     # audit: what would be captured
```

Rules of thumb:

- Declare **narrowly**. Every matched `.md` becomes a note, so a broad pattern
  turns memory into a second copy of the repository — which is the opposite of
  what it is for.
- Text extensions are stored as content; anything else becomes a pointer whose
  bytes stay on the device.
- Captures read the **commit**, not the working tree, so an uncommitted edit is
  never recorded. The watermark advances only after every selected file succeeds;
  retries synchronize the latest committed state.
- `mem hook init` also installs an offline pre-commit check. Selected staged
  Markdown needs a valid `summary`; an invalid file blocks the Git commit.
  Service failures after the commit are logged to `<git-dir>/mem-hook.log` and
  retried later.

## Writing — deliberate and atomic

There is **no staging step**. Every write is one atomic commit:

```bash
mem add notes/new-finding.md --as ops/new-finding -m "why this note exists"
mem add notes/a.md notes/b.md -m "Record related decisions"
mem add --pointer tools/aria2c.exe tools/helper.exe -m "Catalog local tools"
mem pointer add '@file(win-workstation:D:\\tools\\aria2c.exe)' tools/aria2c
mem rm ops/obsolete --expect <sha256>
mem mv --id ops/old ops/new --dry-run
mem mv --id ops/old ops/new
mem mv notes/old.md notes/new.md
mem pull
mem export demo --dst D:\backup\demo-notes
```

- **Write only durable, reusable knowledge**: a decision and its rationale, a
  convention, a lesson that cost something to learn, current state that others
  depend on. Not a transcript, not a status update, not anything already
  obvious from the code.
- **Never store** credentials, tokens, private keys, personal data, or anything
  the user has not agreed to publish.
- `--as` sets the note id, which is its path without `.md`. Omit it and the tool
  may refuse rather than transliterate a name like `My Notes/Foo Bar.md`.
- `-m` records the commit message. Say *why* the change is being made.
- Multiple files share one required `-m` message and become one atomic commit.
  Without `--pointer`, allowed UTF-8 formats store content and other files become
  pointers; `--pointer` forces all listed local files to pointers. An existing
  pointer may be upgraded to stored content. Remove stored content explicitly
  before replacing it with a pointer.
- `--expect <sha256>` makes a write conditional on the note being unchanged —
  use it when you are updating something you read earlier, so you cannot
  silently clobber a concurrent edit.
- Exit `7` (stale revision) means the note changed under you. Re-read and
  reapply; do not force it.

If nothing actually changed, the write is a **successful no-op**, not an error.

`mem mv OLD_PATH NEW_PATH` moves a working file and keeps its note ID. Both paths
must be inside the project root. It updates only `origin`, never the body, and
blocks until the move commits. A missing old file must be restored first.

`mem mv --id OLD_ID NEW_ID` moves the note ID and references, and leaves local
files alone. For a topic move, end both IDs in `/` (for example
`mem mv --id ops/ archive/ops/`). Both forms are blocking queue barriers; unlike
`add`/`rm`, they do not return a bare `queued` receipt.

`mem status --origins` reports local origins that are missing, outside the
project root, in another project, ambiguous, or owned by another device.

`mem pull` restores only **missing** files in the current project; it never
overwrites an existing file. `mem export PROJECT --dst DIR` writes the whole
project into an empty directory. Export does not need `.mem.toml`, does not read
origins, and does not register the exported files as authoring copies.

## What a note looks like

Notes are ordinary Markdown with validated YAML frontmatter, under a project. A
nonempty `summary` of at most 200 characters is required; its parsed value must
contain no newline, though YAML may wrap it across source lines. Paths are the
identifiers, so `projects/example/ops/boot.md` has id `ops/boot`. Because an id
*is* a path, renaming changes the id — which is why broken links matter.

The server's `inline_extensions` policy defaults to `.md,.txt`. `mem add` stores
permitted UTF-8 content under its original name; for other extensions it stores
an alias-qualified pointer such as `@file(win-workstation:D:\\tools\\aria2c.exe)`.
Check the response: `content_stored: false` means only the pointer record was
committed. Use `@locked(alias:path)`
for credentials and private paths; its path must never be printed or read.
Device-specific facts belong in `~/.device/DEVICE.md`, initialized with
`mem device init`. Keep this folder outside cloud-synced and Git-tracked trees;
the CLI cannot detect every external sync service.

Keep one idea per note, give it a summary line, and write the body so that
someone reading it cold in six months understands the reasoning, not just the
conclusion.

## Composing

The Go client does not provide `mem python`. For repeated operations, use the
Python `mem.client` library or compose the CLI's JSON output in a shell script.
The Python CLI provides `mem python`. It runs a program with the project
already resolved and these names injected:

| Name | What it is |
|---|---|
| `ls(**filters)` | notes in this project — returns the list, not the envelope |
| `read(id, revision=None, section=None, span=None)` | one note |
| `outline(id)` | headings |
| `search(q, **filters)` | search hits |
| `status()` | server-side status |
| `project` | the project name, already resolved from the marker |
| `client` | the underlying client, for anything the shorthands do not cover |
| `emit(obj)` | print a JSON object |

```bash
mem python -c 'for n in ls(prefix="ops/"): print(n["id"])'
mem python -c 'emit(read("ops/boot", section="Rollback"))'
mem python script.py
```

`add`, `rm`, and `mv` are **not** in the namespace unless you pass `--allow-write`.
That is a deliberate speed bump, not a security boundary: an agent with a shell
can always run `mem add` directly. Note bodies are untrusted input, and a note
saying "ignore previous instructions" is only dangerous if something acts on it.

## Operating rules

1. Search before asserting anything from memory, and cite note ids when you rely
   on stored context.
2. Write a decision to memory when it is made, not at the end of the session.
3. Never copy a conversation transcript into memory wholesale.
4. Never put a token in a checked-in agent configuration file.
5. Report the exit code when a command fails; every failure carries one, and one
   JSON object on stderr. Do not parse the prose.
