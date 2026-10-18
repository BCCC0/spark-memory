# Go `mem` client

This directory contains the Go client for the Python `memd` server. The Go
binary implements the regular `mem` commands, including Git hooks; `mem python`
stays Python-only and is deliberately absent from the Go help and command table.

## Install a release

Download the asset for your platform from GitHub Releases:

| Platform | Asset |
|---|---|
| Windows amd64 | `mem-vX.Y.Z-windows-amd64.exe` |
| Linux amd64 | `mem-vX.Y.Z-linux-amd64` |
| Linux arm64 | `mem-vX.Y.Z-linux-arm64` |

Verify the asset against `SHA256SUMS`, make it executable, and place it on
`PATH` as `mem` (or `mem.exe` on Windows). Git hooks resolve `mem` from `PATH`
at run time, so the binary must remain installed there.

The runtime dependencies are `gopkg.in/yaml.v3`, `github.com/BurntSushi/toml`,
and `golang.org/x/term` (the last only for the no-echo enrollment prompt). All
are pure Go, so `CGO_ENABLED=0` builds remain static.

Build from this directory with Go 1.27.2:

```powershell
go build -o bin/mem-go.exe ./cmd/mem
go test ./...
go vet ./...
```

The development build is intentionally named `mem-go.exe` so it cannot be
confused with a released `mem.exe` on `PATH`. Release assets are installed as
`mem.exe`; `mem --version` identifies the implementation and commit.

For Windows cross-platform builds, set `CGO_ENABLED=0`, `GOOS`, and `GOARCH`
before each `go build`. Supported targets are `windows/amd64`, `linux/amd64`,
and `linux/arm64`. `git` must be on `PATH` for Git-related commands.
Installed Git hooks resolve `mem` from `PATH` when they run. Set `MEM_BIN` to
the Go binary path if both CLIs are installed and you need to choose one.

The first dependency fetch uses the normal `proxy.golang.org` default. If that
proxy is unreachable on your network, set `GOPROXY` to a module mirror you
trust. Runtime operation does not need Go, Python, or `uv`.
`mem login` prompts for the enrollment secret when `--secret` and
`MEM_ENROLL_SECRET` are absent.

`~/.device/DEVICE.md` is local-only. Its identity check uses the permanent
enrollment `device_id` plus the unique `alias`; the recorded hostname is
diagnostic only, because it changes across DHCP leases and domain renames.

Run the Python server acceptance tests against a built Go client by setting
`MEM_TEST_CLIENT_BINARY` to the absolute path of the binary, then running
`pytest tests`. Exclude `tests/test_python.py`, which exercises the Python-only
`mem python` verb. Python-oracle vectors are generated with
`python tools/gen_client_vectors.py` from the repository root; the generated
file is committed under `client/testdata/vectors/`. The Python CLI help fixture
is generated separately with `python tools/gen_client_help.py` (it omits the
Python-only `mem python` verb). Human-output goldens are updated with
`go test ./internal/cli -run TestHumanGolden -update`.
