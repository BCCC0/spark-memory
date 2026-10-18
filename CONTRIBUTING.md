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
```

Run the Python acceptance suite against the Go binary:

```bash
cd ..
MEM_TEST_CLIENT_BINARY="$PWD/client/bin/mem" uv run pytest tests \
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

## Pull requests

Please describe the problem, the approach, and how you verified the change.
All tests must pass before merging.
