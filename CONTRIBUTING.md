# Contributing

Thanks for contributing to spark-memory.

## Development setup

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/), and — for the Go
client — Go 1.27.2.

Server and Python client:

```bash
uv sync --extra dev --extra embed
uv run pytest
```

Go client:

```bash
cd client
gofmt -l .
go vet ./...
go test ./...
go build -o bin/mem-go ./cmd/mem
```

Run the Python acceptance suite against the Go binary:

```bash
cd ..
MEM_TEST_CLIENT_BINARY="$PWD/client/bin/mem-go" uv run pytest tests \
  -k 'not python and not test_hook_splits_more_than_100_files_into_server_sized_batches'
```

Regenerate the checked-in conformance vectors and help fixture after changing
the Python oracle or CLI parser:

```bash
uv run python tools/gen_client_vectors.py
uv run python tools/gen_client_help.py
```

Keep changes focused, add or update tests for behavior changes, and update the
README/INSTALL docs when the public CLI or deployment behavior changes.

## Secret scanning

`tools/secret_scan.py` rejects high-confidence credentials and machine-specific
home paths in a commit. Scan the whole working tree before a release with:

```bash
uv run python tools/secret_scan.py --all
```

The local pre-commit hook lives in the ignored `.githooks/` directory and scans
staged files. Enable it once per clone with:

```bash
git config core.hooksPath .githooks
```

Use `--no-verify` only for a reviewed false positive; prefer adding
`secret-scan: allow` to the matching line.

## Pull requests

Please describe the problem, the approach, and how you verified the change.
All tests must pass before merging.
