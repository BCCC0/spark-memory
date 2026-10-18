# Publishing spark-memory

Use this checklist before pushing a release or changing repository visibility.

## Review the contents

Publish only source code, tests, public documentation, package metadata, and the
lockfile. Do not publish:

- credentials, API keys, enrollment secrets, private keys, or `.mem-client/`;
- local configuration such as `.mem.toml`, credentials, or `.device/`;
- logs, downloaded models, virtual environments, or test databases;
- deployment-specific hostnames, private IP addresses, or filesystem paths;
- unrelated project or operational material.

Review the exact tracked files, not only the working tree:

```bash
git ls-files
git grep -n -I -i -E 'password|secret|token|api[_-]?key|private key|bearer'
git grep -n -I -E '/home/[^/]+|100\.[0-9]+\.|192\.168\.|tailscale|armbox'
```

Treat test credentials as non-public too. Use temporary values only, and never
reuse them in a deployed environment.

## Staging and public history

The original staging history is retained privately as the archived
`BCCC0/spark-memory-staging-private` repo. The public `BCCC0/spark-memory` repo is built
from a sanitized baseline commit so no operational or developer-specific path is
part of its history. Keep operational deployment material, real endpoints, and
secret-rotation records out of both repos.

## Release checks

Before tagging a release:

1. Run the full Python suite, the Go unit/L4 tests, and the Python acceptance
   suite against the Go binary.
2. Build the Go client for `windows/amd64`, `linux/amd64`, and `linux/arm64`
   with `CGO_ENABLED=0`.
3. Build the Python package with `uv build` in a clean environment.
4. Verify the `INSTALL.md` walkthrough from a clean host or fresh WSL distro.
5. Confirm CI is green on the commit being tagged.

## Go release

The release workflow is tag-driven:

```bash
git tag -a v0.1.0 -m "spark-memory v0.1.0"
git push origin v0.1.0
```

It builds `mem-<tag>-<os>-<arch>` assets and `SHA256SUMS`, then creates the
GitHub Release. Verify the release has all three binaries and that the Linux
binary starts on a clean host.

## Visibility

Do not change the repository to public until the final install and security
review is complete. Immediately after the flip, enable secret scanning with push
protection, Dependabot alerts and security updates, private vulnerability
reporting, a required-check ruleset on `main`, tag protection for `v*`, and
fork-PR approval for outside contributors. Keep operational deployment
instructions, backup procedures, real endpoints, and secret-rotation records
private.
