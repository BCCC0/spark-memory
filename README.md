# spark-memory

`spark-memory` is a server-owned memory service for notes, project knowledge, and
operational records. It gives people and applications on multiple devices a
single durable, versioned place to store and share context without turning each
device into another copy of the repository.

## Design philosophy

spark-memory treats memory as shared infrastructure rather than application-local
state:

- One service owns the repository, history, authentication, and write lock.
- Multiple devices connect to the same service and see the same current memory.
- Clients keep credentials and local configuration, not competing copies of the
  repository.
- Writes are explicit, atomic, and versioned.
- Reads favor identifiers, metadata, and search results; full note bodies are
  retrieved when they are actually needed.
- The core client stays lightweight, while server capabilities can be extended
  independently.

## Why is it different?

Many memory tools are either local folders wrapped in a search interface or
opaque hosted databases. spark-memory takes a middle path: notes remain ordinary,
versioned files owned by a service, while clients get a consistent API and
controlled access to them.

That separation makes shared memory easier to inspect, back up, audit, and use
across workstations, servers, and other connected devices. Everyone works from
the same source of truth instead of merging or reconciling separate local
memory stores. It also keeps the write path small and predictable: a successful
write is immediately part of the repository history, and a failed write does
not leave a half-applied change behind.

## What it can do

- Store structured Markdown notes with metadata and stable identifiers.
- Organize nested topics, store allowed UTF-8 files, and catalog other files through pointers.
- Move a record or topic in one Git commit, updating recognized shared references.
- Create an immutable history of changes through the service-owned repository.
- Authenticate devices and control access to shared projects.
- List notes and inspect their status without downloading every body.
- Read individual notes, including outlines for large notes.
- Search with SQLite full-text search.
- Optionally add semantic search with embeddings.
- Provide a standalone Go client for every supported platform, plus a Python
  client library for automation and composition.

## Installation

There are two halves: the **service** (`memd`) runs on one host and owns the
repository; the **client** (`mem`) runs on every device that talks to it.

The server needs Python 3.11+ and [uv](https://docs.astral.sh/uv/). The
recommended client is the standalone Go binary, published for Windows, Linux
amd64, and Linux arm64 on the GitHub Releases page. Both can also be built from
source.

**[INSTALL.md](INSTALL.md) is the complete zero-to-working guide** — server
setup, running it as a service, enrollment, client install, project setup, Git
hooks, backup/upgrade, exit codes, and troubleshooting.

## Development

```bash
uv sync --extra dev --extra embed
uv run pytest
```

For the Go client:

```bash
cd client
go test ./...
go vet ./...
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidelines. GitHub
Actions runs the Python suite on 3.11, 3.12, and 3.13, and the Go client plus
acceptance tests on every push.

See [PRIOR_ART.md](PRIOR_ART.md) for the design comparison and trade-offs.

## Using it with an AI agent

An agent should read **[INSTALL.md](INSTALL.md)** first: it covers standing up
the service and the client, which is what every command depends on.

Then install the skill, which teaches the agent the working rules — search
before answering from memory, keep reads to coordinates, and write only
deliberate, reusable knowledge:

```bash
cp -r skills/spark-memory ~/.claude/skills/
```

[skills/spark-memory/SKILL.md](skills/spark-memory/SKILL.md) is an ordinary
Agent Skill (`SKILL.md` in a named directory), so it works with any harness that
loads that format. INSTALL.md §3 covers project-scoped installs and the other
skill directories.

The short version to put in an agent's instructions:

> Use the spark-memory service as the shared memory source for this project.
> Search memory before making decisions, write durable decisions and lessons as
> notes, and cite note ids when relying on stored context. Do not create a second
> local memory database. Use the CLI for both retrieval and writes.

## License

MIT — see [LICENSE](LICENSE).
