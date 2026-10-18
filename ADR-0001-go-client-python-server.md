# ADR-0001: A Go client against a Python server

**Status:** accepted. **Date:** 2026-10-15.
**Scope:** the language split between the two halves. It settles what `mem` is
built from and what `memd` stays built from. It does not schedule the port; the
prerequisites in §8 come first.
**Related:** [CLI.md](CLI.md) (the queued-write contract),
[PRIOR_ART.md](PRIOR_ART.md), [INSTALL.md](INSTALL.md).

## 1. Context

`mem` (the client) and `memd` (the server) are one Python distribution today.
`mem` is standard library only, plus PyYAML for the Git hook's frontmatter
check; `memd` needs `starlette`, `uvicorn`, `python-multipart`, and — for
semantic search — `fastembed` and `sqlite-vec` on ONNX Runtime.

Distribution is the pressure. Two clients can exist on one physical machine
(a native Windows checkout and a WSL Ubuntu checkout), and a shared virtualenv
is unsafe: a bare `uv run` from a Windows-drive checkout can replace the Windows
client's environment, and the deployed client must be invoked through an
interpreter that may move. The installed Git hook pins `sys.executable` at
install time, so a relocated virtualenv produces a hook that fails silently —
the exact failure the hook's own logging is supposed to make diagnosable.

The requirement that forces the decision is **compilation and multi-distro
distribution**: a single `./mem` artifact per target, installable without
`uv`, without a virtualenv, and without a system Python.

## 2. Decision

1. **The client is ported to Go.** `mem` is built as a static binary per target
   (`windows/amd64`, `linux/amd64`, `linux/arm64`) with `CGO_ENABLED=0`.
2. **The server stays Python.** `memd` is not ported, now or by this decision.
3. **The Python client package survives as a library**, not as a CLI: the HTTP
   client and the shared contract helpers remain importable for automation and
   for the test suite.
4. **The wire contract is unchanged by the split.** The Go client speaks the
   same HTTP API, emits the same JSON, and uses the same exit codes.

**Dependency amendment (2026-10-17).** The Go client carries one additional
pure-Go runtime dependency, `golang.org/x/term`, used only by `mem login` to
read the enrollment secret without echo on Windows and Unix. It keeps
`CGO_ENABLED=0` and cross-compilation intact. The two parser dependencies in
§5.1 remain the only shared-contract code; `x/term` touches no wire or
validation rule.

## 3. Why

### 3.1 Compilation and multi-distro is the driver, not speed

A frozen Python client (PyInstaller, Nuitka) is dynamically linked against the
glibc of the host that built it. An artifact built on a current Ubuntu fails on
an older glibc and fails outright on musl/Alpine, so "different distro" turns
into a per-distro build matrix. A Go binary built with `CGO_ENABLED=0` is fully
static and runs anywhere on its architecture.

Go also cross-compiles every target from one machine. Python freezing cannot
cross-compile at all: Nuitka compiles to C and invokes the local toolchain,
PyInstaller embeds the local interpreter.

Neither reason is about speed. The measured speed difference is real but
secondary (§3.2).

### 3.2 Measured cost of the Python client

Measured on the Windows workstation, 2026-10-15, warming each process before
timing. Go figures are **estimates** — Go was not installed on the measuring
host — and are given only to bound the change.

| Measurement | Value |
|---|---|
| `python -m mem.cli --help` (virtualenv interpreter) | ~147 ms |
| the same through `uv run` (what users pay today) | ~202 ms |
| bare CPython interpreter floor | ~41 ms |
| `.venv` on disk | 183 MB |

So roughly 100 ms per invocation is import overhead above the interpreter
floor, before any work happens. The client is a thin HTTP/JSON/`git` wrapper
whose own compute is negligible; a static Go binary is expected to land in
single-digit milliseconds. For an agent issuing tens of `mem` calls per turn,
that is seconds of pure process overhead removed.

### 3.3 The defects this removes rather than fixes

The port deletes a class of bug rather than repairing it:

- `hooks.py` pins `sys.executable` at install time and builds its shims as
  `"<interpreter>" -m mem.cli ...`. A moved or deleted virtualenv reaches
  neither the CLI nor the hook log. The same shim is what makes a frozen
  Python client actively hostile to freeze (a frozen binary has no `-m`
  module runner), so the shim must be rewritten for *either* packaging path.
- Python-version drift, dependency resolution failure, and venv/config-dir
  confusion all disappear from every client device.

## 4. Why the server stays Python

This is the half where the decision is not "we did not get to it" but "porting
would be a regression". Measured on the same host:

| Measurement | Value |
|---|---|
| `memd` RSS after startup, embedding stack loaded | **207.1 MB** |
| bare CPython | 10.5 MB |
| + `starlette` + `uvicorn` | 31.9 MB |
| + `fastembed` with the model loaded and one embed | 204.7 MB |
| import cost: `fastembed` / `onnxruntime` / `sqlite_vec` / `starlette` | 610 / 207 / 131 / 94 ms |

**Memory.** About 175 MB of the server's 207 MB is ONNX Runtime plus the
bge-small model. Python itself is 10.5 MB and the whole ASGI framework is
21 MB, so the Python-specific share is roughly 15%, and the part a Go HTTP
server would actually replace is about 21 MB.

**CPU.** The server's work is dominated by `git` subprocesses, SQLite FTS5 and
`sqlite-vec` (C), and ONNX inference (C++). All of that runs at the same speed
regardless of the host language; Python is the glue. The server's genuine cost
problem is algorithmic, not linguistic — one `git show` per tracked path
(`repo.py`, `list_note_paths`) and a full worktree checkout plus a full reset of
the served tree per queued write (`app.py`, `_publish_queued`). Those would be
equally O(corpus) in Go.

**Embeddings decide it.** Go has no credible equivalent of `fastembed`. A Go
server would have to keep Python for embedding, which means two runtimes, two
deployment stories, and *more* memory than today, in exchange for ~21 MB. Rust
is the only language that could take the server whole (`ort`, `fastembed-rs`,
`sqlite-vec`), and that would mean re-validating the least-tested surface in the
project for a memory win on a service that runs on one host we control.

## 5. Accepted trade-offs

1. **A small shared contract is forked.** Three rule sets are enforced on both
   sides today, and the server imports the same files the client does:

   | Rule set | Rough size | Client | Server |
   |---|---|---|---|
   | `ids.py` — `validate_segment` / `validate_id` / `validate_project` | ~60 lines | derives and validates ids | re-validates, and builds repo paths |
   | `pointers.py` — `parse_pointer` / `canonical` | ~55 lines | serialises a pointer record | parses stored text back |
   | `summary.py` — `parse_frontmatter` / `validate_summary` | ~45 lines | offline precheck | validates on upload |
   | `errors.py` — the code table | constants | emits | emits |

   Together that is roughly **180 lines of pure, deterministic rules**, and they
   must agree byte for byte forever. **Conformance vectors are the mitigation
   and are a prerequisite** (§8).

   Two modules previously listed here are **not** forks, and this ADR
   over-stated the cost until they were checked. `paths.py` is never imported by
   the server at all. `keys.py` reaches the server only through the dead
   `IdempotencyStore`, so live idempotency keys are computed by the client and
   treated opaquely by the server. Both are ordinary porting work with nothing
   to drift against.

   The overlap that remains is not an accident of shared code: each side must be
   able to say *no* alone. The server, because it accepts a client-supplied id
   and writes it into its own working tree, so it cannot trust the client's
   validation. The client, because the pre-commit hook checks staged Markdown
   offline, without contacting the service. Neither can delegate to the other.
2. **YAML semantics diverge, and the divergence is measured.** `validate_summary`
   requires `summary` to be a string *after* loading, so a scalar that one
   parser resolves as a boolean and the other as a string decides whether a file
   is accepted at all. Running the same corpus through PyYAML and
   `gopkg.in/yaml.v3` produced exactly two divergences:

   | `summary:` value | PyYAML (Python, and the server) | yaml.v3 (naive Go) |
   |---|---|---|
   | `yes` / `no` / `on` / `off` | boolean → rejected | string → **accepted** |
   | the key given twice | last wins → accepted | **hard error** |

   Both fail in the wrong direction: the Go client would miss an error the
   server enforces, and would block a commit the Python client allowed. The
   mitigation is a PyYAML-compatible resolver over `yaml.Node` rather than a
   naive decode; it is specified in the plan (§6.3).
3. **String length means different things in the two languages.** Every length
   gate in `ids.py` and `summary.py` is written against Python `len()`, which
   counts code points, while Go's `len()` counts UTF-8 bytes. Measured: a
   100-character CJK summary is **100** in Python (accepted, limit 200) and
   **300** in Go (rejected by a byte-based check). A naive port therefore
   **blocks commits the Python client allows**, for any non-ASCII summary. Go
   must use `utf8.RuneCountInString` at each of these gates, and the vectors
   must cover CJK, emoji, combining marks and the exact 40 / 80 / 200
   boundaries. The corpus and the two porting rules are in the plan (§6.4).
4. **`mem python` stops being a CLI verb.** Its bindings are thin wrappers over
   the HTTP client, the two batch verbs already cover the common compositions,
   and its own tests are mostly tests of its read-only guardrail rather than of
   capability. Composition moves to the library, or to the shell. This is a
   real loss of one documented convenience.
5. **The Python client is maintained as a library.** Tests that drive the
   server, and people who script against `mem.client`, keep working, but the
   Python half no longer ships a CLI worth installing on a client device.
6. **`git` remains a runtime dependency of the client.** The port removes the
   Python runtime, not the `git` one.
7. **Longer path to a first release.** Freezing the Python client would deliver
   `./mem` in hours; the Go port is a multi-day effort and carries the risks
   above.

## 6. Alternatives considered

| Alternative | Why not |
|---|---|
| **Keep the Python client** | Fails the driver. Multi-distro distribution would stay a per-distro build matrix. |
| **Freeze with PyInstaller or Nuitka** | A legitimate stopgap that delivers `./mem` quickly, and it keeps one implementation of the contract. Rejected as the *destination* because the artifact is glibc-bound, does not cross-compile, and real start-up cost remains under `--onefile`. |
| **`pyapp` / `python-build-standalone`** | Closest Python gets to a static binary and it can cross-build, but the artifact embeds a whole interpreter and stays distro-sensitive in its libc linkage. |
| **`uv tool install`** | Removes venv management, not the runtime. Does not satisfy "without `uv`". |
| **Port both halves to Go** | Breaks semantic search (§4). Net memory regression via a Python sidecar. |
| **Port both halves to Rust** | Technically the strongest single-language answer — `ort`, `fastembed-rs` and `sqlite-vec` all exist. Rejected on cost: it would rewrite a working server and re-validate embeddings for no gain on a single-host service. |
| **Keep both halves in Python and accept per-distro builds** | The explicit requirement this ADR exists to satisfy. |

## 7. Consequences

- The repository acquires a **conformance suite separate from the existing
  tests**: golden vectors for the genuinely shared rules — id validation,
  pointer canonicalisation, summary validation and the error code table — run
  against the Python library and the Go binary. Drift becomes a test failure
  rather than a production surprise.
- The same mechanism, pointed at the **client-only** rules (`paths`
  translation, idempotency keys, hook pattern matching), serves a second
  purpose: a **port oracle**. Those rules cannot drift against the server, but
  they can drift against the Python implementation they replace, and vectors are
  the cheapest way to prove parity. The two roles are different and the suite
  should say which vectors are which.
- The existing test suite becomes the port's acceptance criteria: it is almost
  entirely black-box over the CLI and HTTP, which is what makes this port
  tractable at all.
- `hooks.py`'s shim generation must be redesigned regardless of path, so that
  the installed hook invokes a `mem` resolved at run time rather than a pinned
  interpreter.
- Two build outputs replace one: the Go binary (released per platform) and the
  Python package (published as a library).
- `README.md`, `INSTALL.md` and `skills/spark-memory/SKILL.md` must stop
  presenting one installation path for both halves. `SKILL.md` must stop
  recommending `mem python` for anything beyond a single command.
- The server-side defects already marked in the source are unaffected by this
  decision and remain due: `memd` stays Python, so every queue, indexing and
  locking defect must be fixed where it is.

## 8. Prerequisites

In order. This ADR is accepted; the port is not started until (1) and (2) are
done, so that the Go implementation has a written target and a drift detector.

1. **Freeze the contract.** `CLI.md`, `INSTALL.md` and the queued-write design
   merged into one statement of the client/server contract, with the citations
   to the absent `MEM-*.md` documents resolved.
2. **Write the conformance vectors** (§7), including the YAML and text-length
   divergences enumerated in §5.2 and §5.3.
3. **Fix the server-side defects** marked in the source. They are needed under
   every outcome and they sharpen the contract the port targets.
4. **Port the client**, keeping the 296 existing tests as the acceptance suite.
5. **Retire the Python CLI**, keeping the package as a library.

## 9. What this does not decide

- **A server port.** Out of scope, and §4 argues against it. If it is ever
  revisited, Rust is the candidate, not Go.
- **The server's deployment story.** The daemon is deployed by copying `src/`
  into an editable install on the host; this ADR changes nothing there.
- **The wire contract.** Endpoints, payload shapes and exit codes are unchanged
  by the language split. Any change to them is a separate decision.
- **Distribution channels.** How the per-platform binaries are published is a
  packaging decision, deliberately not made here. The binary does not belong in
  Git history; the repository pins line endings and a 25 MB blob does not
  belong in a commit.
