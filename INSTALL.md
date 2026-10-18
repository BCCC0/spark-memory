# Installing spark-memory

This is the complete installation guide. It takes one host from an empty
directory to a running memory service, then connects a client device, creates a
project, and verifies a first write.

There are two halves:

- **The service (`memd`)** — one host owns the memory Git repository, the write
  queue and lock, the search index, and the device registry. Run it once.
- **The client (`mem`)** — every device that reads or writes memory. It is
  stateless: it holds credentials and a project marker, never a copy of the
  repository.

The supported deployment is **one server on a trusted private network or VPN**.
Do not expose `memd` directly to the public internet.

---

## 1. Before you start

Server requirements:

- Linux or macOS (Windows is supported for the client; the server is normally
  Linux).
- Python 3.11 or newer.
- [uv](https://docs.astral.sh/uv/).
- `git`.
- Optional: network access the first time semantic search downloads
  `BAAI/bge-small-en-v1.5` (~127 MB).

Client requirements:

- One of the published Go binaries (`windows/amd64`, `linux/amd64`,
  `linux/arm64`), or Go 1.27.2 to build it.
- `git` only if you use the Git hooks.

> **One worker only.** `memd` runs a single process and a single worker by
> design. The write lock is an in-process mutex; a second `memd` against the same
> repository would take its own lock and silently break the atomic-write
> guarantee.

---

## 2. Install the server

Clone the repository and install the Python environment:

```bash
git clone https://github.com/BCCC0/spark-memory.git
cd spark-memory
uv sync --extra embed
```

`--extra embed` adds `fastembed` and `sqlite-vec` for semantic search. Without
it, text search works and `--mode hybrid` is refused rather than silently
downgraded.

Create the memory repository and start the daemon in the foreground for a first
test:

```bash
mkdir -p ~/memory
uv run memd --repo ~/memory --host 127.0.0.1 --port 8321
```

In another terminal:

```bash
curl -s http://127.0.0.1:8321/health
```

You should get JSON with `"ok": true`.

The first start creates:

| Path | Purpose |
|---|---|
| `~/memory` | the memory Git repository |
| `~/mem/devices.db` | device registry |
| `~/mem/enroll.secret` | the secret new devices use to enroll |
| `~/mem/link.secret` | signs short-lived read URLs |
| `~/memory/.mem` | queue and index state |

The last three are not regenerable; back them up with the repository.

### Run it as a service

The simplest permanent setup is a **systemd user unit** for the account that
owns the checkout.

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/mem.service <<'EOF'
[Unit]
Description=spark-memory daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/spark-memory
Environment=MEM_EMBED_CACHE=%h/.cache/spark-memory/fastembed
ExecStart=%h/spark-memory/.venv/bin/memd --repo %h/memory --host 127.0.0.1 --port 8321 --base-url http://127.0.0.1:8321
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now mem.service
loginctl enable-linger "$USER"
```

Check it:

```bash
systemctl --user status mem.service --no-pager
journalctl --user -u mem.service -n 50 --no-pager
curl -s http://127.0.0.1:8321/health
```

For a system-wide service under a dedicated account, use the same arguments in
a system unit:

```ini
[Service]
User=sparkmem
Group=sparkmem
WorkingDirectory=/opt/spark-memory
EnvironmentFile=/etc/spark-memory.env
ExecStart=/opt/spark-memory/.venv/bin/memd \
  --repo /srv/memory --host 127.0.0.1 --port 8321 \
  --base-url http://127.0.0.1:8321
Restart=always
RestartSec=5
```

Ownership must allow `sparkmem` to write the repository, the state directory,
and the registry/secrets directory (`/home/sparkmem/mem` by default).

### Serve other devices

`127.0.0.1` is only reachable on the server itself. For remote clients, bind a
private interface or VPN address and tell the daemon which URL clients use:

```bash
uv run memd --repo ~/memory --host 0.0.0.0 --port 8321 \
  --base-url http://mem.example.internal:8321
```

`--base-url` is not optional on a wildcard bind. It is the address used in
client-facing signed URLs; with a loopback default, remote callers cannot fetch
large notes.

Recommended network options:

- A private WireGuard/Tailscale network between server and clients.
- An SSH tunnel (`ssh -L 8321:127.0.0.1:8321 user@server`) with the client
  pointed at `http://127.0.0.1:8321`.
- A firewall that allows port 8321 only from the trusted network.

**Do not put an ordinary HTTP reverse proxy in front of `memd`.** The server
uses the direct client address for its local-path safety check; a proxy makes
every caller look local. Terminate TLS in the VPN layer, or use an SSH tunnel.

### Embedding cache

If the server cannot reach the model host, use an existing cache:

```bash
mkdir -p ~/.cache/spark-memory/fastembed
export MEM_EMBED_CACHE=~/.cache/spark-memory/fastembed
```

Put that line in the service environment. Without a cache and without network,
the first write that needs embeddings fails fast; text search still works.

---

## 3. Read the enrollment secret

New devices prove they are allowed to join with the enrollment secret:

```bash
cat ~/mem/enroll.secret
```

Treat it like a password. A device that has it can enroll, and the token it
receives is returned only once.

---

## 4. Install the client

### From a GitHub release (recommended)

Download the asset for the platform:

| Platform | Asset |
|---|---|
| Windows amd64 | `mem-vX.Y.Z-windows-amd64.exe` |
| Linux amd64 | `mem-vX.Y.Z-linux-amd64` |
| Linux arm64 | `mem-vX.Y.Z-linux-arm64` |

For example, on Linux:

```bash
VERSION=v0.1.0
BASE="https://github.com/BCCC0/spark-memory/releases/download/$VERSION"
curl -fLO "$BASE/mem-$VERSION-linux-amd64"
curl -fLO "$BASE/SHA256SUMS"
sha256sum --ignore-missing -c SHA256SUMS
install -m 755 "mem-$VERSION-linux-amd64" ~/.local/bin/mem
```

On Windows PowerShell:

```powershell
$Version = "v0.1.0"
$Base = "https://github.com/BCCC0/spark-memory/releases/download/$Version"
Invoke-WebRequest "$Base/mem-$Version-windows-amd64.exe" -OutFile mem.exe
Invoke-WebRequest "$Base/SHA256SUMS" -OutFile SHA256SUMS
# Compare the hash of mem.exe with its line in SHA256SUMS.
$env:Path = "$PWD;$env:Path"
```

Git hooks resolve `mem` from `PATH` at run time, so keep the binary installed
there.

### From source

```bash
cd client
CGO_ENABLED=0 go build -o bin/mem ./cmd/mem
install -m 755 bin/mem ~/.local/bin/mem
```

The build is static; no Go toolchain is needed on client devices at runtime.

### Python CLI (optional)

The Python package still exposes a `mem` entry point for development and
`mem python` composition:

```bash
uv sync
uv run mem --help
```

Do not install both clients on `PATH` under the same name unless you intend to
choose one. The Go binary is the supported production client.

---

## 5. Enroll the device

The server URL must be reachable from the device:

```bash
mem login --server http://mem.example.internal:8321 \
  --name my-laptop --alias my-laptop
```

The CLI prompts for the enrollment secret. `--secret` or `MEM_ENROLL_SECRET`
avoids the prompt. The alias is immutable and unique; use a stable, lowercase
slug.

Credentials are written to `~/.config/mem/credentials` with mode `0600`. The
token is shown only once; if the write fails, enroll again rather than trying to
recover the old token.

Create the private device home:

```bash
mem device init
```

This writes `~/.device/DEVICE.md`, refuses a symlinked or cloud-synced home, and
adds `.device/` to the global Git ignore file. The file is local-only and is
never uploaded to project memory.

---

## 6. Create a project

Run this in the directory you actually work from:

```bash
cd ~/work/my-project
mem init my-project
```

This writes `.mem.toml`; the CLI finds it by walking up from the current
directory, the same way Git finds `.git`. The marker is the only thing that
tells the client which project it is in. On the server, a new project also gets
a `PROJECT.md` note.

---

## 7. First real workflow

Create a note:

```markdown
---
summary: Why this project uses SQLite
status: active
type: decision
---

# SQLite decision

We chose SQLite because the service is single-host and the data fits on disk.
```

Then:

```bash
mem add note.md --as decisions/sqlite -m "Record the SQLite decision"
mem flush
mem read decisions/sqlite
mem search "sqlite"
```

`add` returns a **queued** receipt; `flush` waits for the write and the text
index to reach the resulting head, but gives up after 3 minutes with
`lock_timeout` (exit 3) if the index cannot catch up. A subsequent `read`
returns the committed record.

Useful commands:

```bash
mem status
mem projects
mem ls
mem manifest ops
mem read ops/decisions --outline
mem read ops/decisions@<revision>
mem mv ops/old ops/new
mem rm ops/obsolete --expect <sha256>
mem devices
```

A device may retire **itself** with `mem devices --retire <device-id>`; fleet
administration is an operator action on the server.

---

## 8. Configuration reference

### Server flags and environment

| Flag | Environment | Default |
|---|---|---|
| `--repo` | `MEM_REPO` | `~/memory` |
| `--host` | `MEM_HOST` | `127.0.0.1` |
| `--port` | `MEM_PORT` | `8321` |
| `--base-url` | `MEM_BASE_URL` | derived from host/port |
| `--devices-db` | `MEM_DEVICES_DB` | `~/mem/devices.db` |
| `--enroll-secret` | `MEM_ENROLL_SECRET_FILE` | `~/mem/enroll.secret` |
| `--link-secret` | `MEM_LINK_SECRET_FILE` | `~/mem/link.secret` |
| `--link-ttl` | `MEM_LINK_TTL` | `86400` seconds |
| `--state-dir` | `MEM_STATE_DIR` | `<repo>/.mem` |
| `--max-bytes` | `MEM_MAX_BYTES` | `4096` |
| `--inline-extensions` | `MEM_INLINE_EXTENSIONS` | `.md,.txt` |
| `--lock-timeout` | `MEM_LOCK_TIMEOUT` | `15` seconds |
| embedding cache | `MEM_EMBED_CACHE` | fastembed default temp cache |

### Client files

| Path | Purpose |
|---|---|
| `~/.config/mem/config.toml` | machine defaults: `server`, `[read] max_bytes` |
| `~/.config/mem/credentials` | device token, mode `0600` |
| `<project>/.mem.toml` | project marker: `project`, optional `server` |

Set `MEM_CONFIG_DIR` to relocate the first two.

Server URL precedence, later wins:

1. compiled-in default `http://127.0.0.1:8321`
2. `server` in `config.toml`
3. `MEM_SERVER`
4. `server` in credentials
5. `server` in the nearest `.mem.toml`
6. `--server` on the command line

---

## 9. Git hooks

`mem hook` keeps selected committed files in sync with memory.

```bash
mem hook add 'docs/**/*.md'
mem hook add '!docs/drafts/**'   # exclusions always win
mem hook init                    # install into ./.git/hooks
mem hook check                   # validate staged Markdown offline
mem hook update --dry-run        # preview a capture
```

A pre-commit hook validates staged, selected Markdown and blocks the commit if
the required `summary` is missing or invalid. Post-commit and post-merge hooks
capture committed bytes; they never read the working tree.

- A watermark advances only after every selected file is captured.
- Capture failures are logged to `<git-dir>/mem-hook.log` and do not fail the
  Git operation.
- Deleted tracked files are reported as `orphaned`, not removed from memory.
- Patterns live in `.mem.toml`, so they are device-local.
- The hook is per checkout; run `mem hook init` in each clone.

---

## 10. Backup, restore, and upgrade

Back up together:

```bash
tar czf spark-memory-backup-$(date +%F).tar.gz \
  ~/memory ~/mem/devices.db ~/mem/enroll.secret ~/mem/link.secret
```

The memory repository is ordinary Markdown plus Git history. The registry and
both secrets cannot be rebuilt.

Restore with the service stopped; put the paths back with the same ownership and
permissions, then start the service and check `/health`.

Upgrade the server:

```bash
cd ~/spark-memory
git pull --ff-only
uv sync --extra embed
systemctl --user restart mem.service
curl -s http://127.0.0.1:8321/health
```

Upgrade the client by replacing the binary on `PATH`; clients are stateless, so
no data migration is needed. Back up before a server upgrade and keep the
previous checkout or tag for rollback.

---

## 11. Troubleshooting

Every failure is one JSON object on stderr with a code and exit status:

| Exit | Meaning | What to do |
|---|---|---|
| `1` | internal error | check server logs and the queue state |
| `2` | server unreachable | check URL, VPN/firewall, `/health` |
| `3` | lock held or timed out | wait, or inspect a stuck/busy write |
| `4` | invalid request or schema | read the message; fix the id/summary/TOML |
| `6` | no `.mem.toml` | run `mem init <project>` here |
| `7` | stale revision | re-read, then retry with the current `--expect` |
| `8` | unauthorized | `mem login` again, or the device was retired |
| `9` | not found or moved | list the topic; the id changed |
| `64` | usage error | run the command with `--help` |

Useful checks:

```bash
mem status
curl -s http://127.0.0.1:8321/health
journalctl --user -u mem.service -n 100 --no-pager
mem flush
```

If `mem add` returns queued but `mem read` keeps saying pending, run `mem flush`
and inspect the server queue/logs. If a write failed permanently, the failed
receipt remains visible until a later successful write for the affected id.

---

## 12. Security notes

- Keep `memd` on a private network or VPN. It has device-token authentication,
  not public-internet hardening.
- Treat `enroll.secret`, `link.secret`, `devices.db`, and every client
  `credentials` file as secrets. Back them up privately; never commit them.
- The server needs write access to the memory repository and state directory,
  but no other user should be able to modify them.
- `~/.device/DEVICE.md` is private and local; the CLI does not upload it.
- Report vulnerabilities privately through GitHub security advisories, not
  public issues.

## 13. Install the agent skill

`skills/spark-memory/SKILL.md` teaches an agent to search before answering,
read coordinates rather than bodies, and write only durable knowledge.

```bash
mkdir -p ~/.claude/skills
cp -r skills/spark-memory ~/.claude/skills/
```

For a project-scoped skill, copy it under `.claude/skills/` (or the equivalent
directory for your harness). Keep the directory and `SKILL.md` filename intact.

The agent still needs two values it cannot guess:

- the server URL (`--server`, `MEM_SERVER`, or `config.toml`)
- the project marker (`mem init <project>` in the working directory)

---

## 14. Development

See [CONTRIBUTING.md](CONTRIBUTING.md). The short version:

```bash
uv sync --extra dev --extra embed
uv run pytest

cd client
go test ./...
go vet ./...
```

The default Python suite replaces the real embedder with a deterministic fake;
tests needing the real model are marked `real_embedder` and are opt-in.
