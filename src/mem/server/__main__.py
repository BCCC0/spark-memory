"""``memd`` — run the daemon.

**One worker, always.** The write lock is an in-process mutex
(``MEM-RESOLUTIONS.md`` B1), so a second worker would silently get its own lock
and the atomicity guarantee would be false. This is not a performance knob.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .app import Settings, create_app


def build_settings(args: argparse.Namespace) -> Settings:
    repo_root = Path(args.repo).expanduser()
    state_dir = Path(args.state_dir).expanduser() if args.state_dir else repo_root / ".mem"
    devices_db = (
        Path(args.devices_db).expanduser()
        if args.devices_db
        else Path.home() / "mem" / "devices.db"
    )
    enroll_secret = (
        Path(args.enroll_secret).expanduser()
        if args.enroll_secret
        else Path.home() / "mem" / "enroll.secret"
    )
    # The capability-link secret (S31). Beside the registry on purpose: both are
    # non-rebuildable, and both must be backed up together.
    link_secret = (
        Path(args.link_secret).expanduser()
        if args.link_secret
        else Path.home() / "mem" / "link.secret"
    )

    # The address CLIENTS use. Binding 0.0.0.0 is not an address anyone can dial,
    # so fall back to loopback and let --base-url / MEM_BASE_URL say otherwise.
    wildcard = args.host in ("0.0.0.0", "::")
    host = "127.0.0.1" if wildcard else args.host
    base_url = args.base_url or f"http://{host}:{args.port}"
    if wildcard and not args.base_url:
        # Every client-facing signed capability link of
        # S31 — is built from base_url, so a loopback default on a wildcard bind hands
        # remote callers a URL they cannot fetch. The daemon cannot guess a reachable
        # address, so it says so rather than failing silently.
        print(
            "memd: warning: wildcard bind with no --base-url; client-facing URLs will "
            f"read {base_url} and may be unreachable from other hosts. "
            "Pass --base-url <the address clients use>.",
            file=sys.stderr,
        )

    return Settings(
        repo_root=repo_root,
        devices_db=devices_db,
        enroll_secret=enroll_secret,
        state_dir=state_dir,
        max_bytes=args.max_bytes,
        inline_extensions=tuple(ext.strip() for ext in args.inline_extensions.split(",") if ext.strip()),
        lock_timeout=args.lock_timeout,
        base_url=base_url,
        link_secret=link_secret,
        link_ttl=args.link_ttl,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="memd", description="the mem daemon")
    parser.add_argument(
        "--repo", default=os.environ.get("MEM_REPO", str(Path.home() / "memory"))
    )
    parser.add_argument("--host", default=os.environ.get("MEM_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("MEM_PORT", "8321"))
    )
    parser.add_argument("--devices-db", default=os.environ.get("MEM_DEVICES_DB"))
    parser.add_argument("--enroll-secret", default=os.environ.get("MEM_ENROLL_SECRET_FILE"))
    parser.add_argument(
        "--link-secret",
        default=os.environ.get("MEM_LINK_SECRET_FILE"),
        help="the capability-link secret (default: ~/mem/link.secret)",
    )
    parser.add_argument(
        "--link-ttl",
        type=int,
        default=int(os.environ.get("MEM_LINK_TTL", "86400")),
        help="seconds a signed read URL stays valid (default: 86400)",
    )
    parser.add_argument("--state-dir", default=os.environ.get("MEM_STATE_DIR"))
    parser.add_argument("--max-bytes", type=int, default=int(os.environ.get("MEM_MAX_BYTES", "4096")))
    parser.add_argument(
        "--inline-extensions", default=os.environ.get("MEM_INLINE_EXTENSIONS", ".md,.txt"),
        help="comma-separated UTF-8 extensions whose content is stored (default: .md,.txt)",
    )
    parser.add_argument("--lock-timeout", type=float, default=float(os.environ.get("MEM_LOCK_TIMEOUT", "15")))
    parser.add_argument(
        "--base-url",
        default=os.environ.get("MEM_BASE_URL"),
        help="the address clients use",
    )
    args = parser.parse_args(argv)

    try:
        import uvicorn
    except ImportError:  # pragma: no cover
        print("memd: uvicorn is required (uv sync)", file=sys.stderr)
        return 1

    settings = build_settings(args)
    app = create_app(settings)
    print(
        f"memd: repo={settings.repo_root} state={settings.state_dir} "
        f"devices={settings.devices_db} on {args.host}:{args.port} (workers=1)",
        file=sys.stderr,
    )
    # proxy_headers=False on purpose. uvicorn trusts X-Forwarded-For from a loopback
    # peer by default, which REWRITES `request.client.host` — the very value the
    # locality guard uses to decide whether to hand out a filesystem path (S31). There
    # is no reverse proxy in front of mem by design, so nothing legitimate needs that
    # rewriting; leaving it on would make the guard depend on a header if a proxy is
    # ever introduced.
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        workers=1,
        log_level="warning",
        proxy_headers=False,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
