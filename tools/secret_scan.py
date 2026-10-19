#!/usr/bin/env python3
"""Reject high-confidence secrets before they enter a commit.

The pre-commit hook calls this with ``--staged``. ``--all`` scans the tracked
working tree plus untracked, non-ignored files, which is useful before a commit
and in CI.

This is deliberately conservative: it reports a finding and exits non-zero, but
it never prints the matched value. A line containing
``secret-scan: allow`` (or ``pragma: allowlist secret``) is skipped so a reviewed
test fixture can stay in the tree.

It also rejects machine-specific home paths that embed a local account name,
because those leak a local account even when the path contains no credential.
Placeholder account names such as ``alice``, ``user``, and ``sparkmem`` are
allowed so portable examples and the documented service account do not trip the
hook.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# (label, compiled pattern). These are formats with little legitimate reason to
# appear in this repository; keep them tight to avoid turning the hook into a
# nuisance that people bypass.
PATTERNS = [
    ("private key", re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |DSA |PGP )?PRIVATE KEY-----")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("GitHub fine-grained token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("OpenAI-style key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("Anthropic key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b")),
    ("Stripe live key", re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{20,}\b")),
    ("Bearer token", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{20,}=*")),
]

# Generic assignments only match a quoted literal. A variable assigned from
# an environment lookup (`secret = os.Getenv(...)`) is how a secret should be
# handled, not a finding.
GENERIC = re.compile(
    r"""(?ix)
    \b(password|passwd|secret|api[_-]?key|apikey|access[_-]?key|
       client[_-]?secret|enroll[_-]?secret|auth[_-]?token)\b
    \s*[:=]\s*
    ["']([^"']{8,})["']
    """
)

PLACEHOLDER = re.compile(
    r"(?i)^(?:test|dummy|example|sample|fake|placeholder|changeme|"
    r"not[-_]?a[-_]?real|your[-_]|xxx+|\*+|\.\.\.|<[^>]+>|\$\{[^}]+\})$"
)

# A home path carries an account name. These names appear in portable examples
# and the documented service account; a real numeric or personal account is not
# exempt.
HOME_PATH = re.compile(r"(?i)[\\/](?:Users|home)[\\/]+([^\\/\s\"'<>]+)")
ALLOWED_ACCOUNT_NAMES = {
    "alice",
    "bob",
    "default",
    "default user",
    "all users",
    "example",
    "me",
    "name",
    "public",
    "runner",
    "sparkmem",
    "test",
    "tester",
    "user",
    "username",
    "x",
    "yourname",
}

SUSPICIOUS_NAMES = {
    "credentials",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    "id_dsa",
}
SUSPICIOUS_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}
ENV_EXAMPLES = {".env.example", ".env.sample", ".env.template"}
SKIP_GENERIC_PREFIXES = ("tests/", "testdata/", "docs/", "skills/", "examples/")
MAX_BYTES = 1_000_000
ALLOW_MARKERS = ("secret-scan: allow", "pragma: allowlist secret", "gitleaks:allow")


def git(*args: str) -> bytes:
    return subprocess.run(
        ["git", *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ).stdout


def staged_files() -> list[str]:
    raw = git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z")
    return [item for item in raw.decode("utf-8", "surrogateescape").split("\0") if item]


def all_files() -> list[str]:
    tracked = git("ls-files", "-z").decode("utf-8", "surrogateescape").split("\0")
    untracked = git(
        "ls-files", "--others", "--exclude-standard", "-z"
    ).decode("utf-8", "surrogateescape").split("\0")
    return sorted({item for item in (*tracked, *untracked) if item})


def read_staged(path: str) -> bytes:
    return git("show", f":{path}")


def filename_finding(path: str) -> str | None:
    base = Path(path).name.lower()
    if base in ENV_EXAMPLES:
        return None
    if base == ".env" or base.startswith(".env."):
        return "environment file"
    if base in SUSPICIOUS_NAMES:
        return "credential or private-key filename"
    if Path(base).suffix in SUSPICIOUS_SUFFIXES and not base.endswith(".pub"):
        return "private-key material"
    return None


def scan(path: str, data: bytes) -> list[str]:
    findings: list[str] = []
    reason = filename_finding(path)
    if reason:
        findings.append(f"{path}: {reason} (file name)")

    if b"\0" in data[:4096] or len(data) > MAX_BYTES:
        return findings
    text = data.decode("utf-8", "replace")
    generic_ok = not path.startswith(SKIP_GENERIC_PREFIXES)

    for number, line in enumerate(text.splitlines(), 1):
        lowered = line.lower()
        if any(marker in lowered for marker in ALLOW_MARKERS):
            continue
        for label, pattern in PATTERNS:
            if pattern.search(line):
                findings.append(f"{path}:{number}: possible {label} (value redacted)")
        for match in HOME_PATH.finditer(line):
            account = match.group(1).rstrip(".,;:)]}`")
            if account.lower() in ALLOWED_ACCOUNT_NAMES:
                continue
            if any(token in account for token in ("<", ">", "%", "$", "{", "[", "]")):
                continue
            findings.append(
                f"{path}:{number}: possible machine-specific home path (value redacted)"
            )
            break
        if generic_ok:
            match = GENERIC.search(line)
            if match:
                value = match.group(2).strip()
                if not PLACEHOLDER.match(value):
                    findings.append(
                        f"{path}:{number}: possible {match.group(1)} assignment (value redacted)"
                    )
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--staged", action="store_true", help="scan the index (default)")
    group.add_argument("--all", action="store_true", help="scan tracked and untracked working files")
    args = parser.parse_args()

    files = all_files() if args.all else staged_files()
    findings: list[str] = []
    for path in files:
        try:
            data = Path(path).read_bytes() if args.all else read_staged(path)
        except (OSError, subprocess.CalledProcessError):
            # A path removed between listing and reading is not a secret.
            continue
        findings.extend(scan(path, data))

    if findings:
        print("secret-scan: refusing to continue; possible secrets found:", file=sys.stderr)
        for finding in findings:
            print("  " + finding, file=sys.stderr)
        print(
            "If a match is a reviewed, non-secret fixture, add "
            "`secret-scan: allow` to that line.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
