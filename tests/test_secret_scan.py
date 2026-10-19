"""The local secret scanner must catch tokens and user-home paths.

The fixtures are assembled at runtime so this test file does not itself contain
the literal patterns the scanner is meant to reject.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "secret_scan", ROOT / "tools" / "secret_scan.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_a_high_confidence_token_is_reported():
    scanner = _load()
    token = "AKIA" + "1234567890ABCDEF"
    findings = scanner.scan("example.py", f'aws_key = "{token}"\n'.encode())
    assert any("AWS access key" in finding for finding in findings)
    assert all(token not in finding for finding in findings), "the value must stay redacted"


def test_a_windows_user_path_is_reported():
    scanner = _load()
    # Synthetic numeric account, not a real local user name.
    path = "C:\\Users\\" + "12345" + "\\AppData\\Local\\Temp\\x"
    findings = scanner.scan("example.py", f'path = r"{path}"\n'.encode())
    assert any("machine-specific home path" in finding for finding in findings)


def test_a_posix_user_path_is_reported():
    scanner = _load()
    path = "/home/" + "12345" + "/project"
    findings = scanner.scan("example.py", f'path = "{path}"\n'.encode())
    assert any("machine-specific home path" in finding for finding in findings)


def test_placeholder_accounts_are_not_reported():
    scanner = _load()
    for path in ("/home/alice/x", "/home/user/x", "/home/sparkmem/mem", "C:\\Users\\Public\\x"):
        findings = scanner.scan("README.md", f"`{path}`\n".encode())
        assert not findings, path


def test_an_allow_marker_skips_a_reviewed_line():
    scanner = _load()
    token = "ghp_" + "a" * 40
    findings = scanner.scan(
        "fixture.py", f'token = "{token}"  # secret-scan: allow\n'.encode()
    )
    assert findings == []
