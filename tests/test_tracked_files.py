"""The tracked-files contract: nested topics, local pointers, and no MCP route."""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from conftest import note_text


def test_nested_topics_and_subtree_manifest(ready):
    ready.write("a.md", note_text(summary="nested"))
    ready.cli_json("add", "a.md", "--as", "tools/install/guide")
    first = ready.cli_json("manifest", "tools")
    assert [row["id"] for row in first["entries"]] == ["tools/install/guide"]
    assert first["revision"] == first["entries"][0]["revision"]
    listing = ready.cli_json("ls", "tools/")
    assert [(row["id"], row["kind"]) for row in listing["notes"]] == [("tools/install", "topic")]

    ready.write("b.md", note_text(summary="elsewhere"))
    ready.cli_json("add", "b.md", "--as", "other/note")
    assert ready.cli_json("manifest", "tools")["revision"] == first["revision"]


def test_pointer_checks_target_on_cli_only(ready):
    target = ready.workdir / "script.py"
    target.write_text("SECRET TARGET BYTES", encoding="utf-8")
    alias = ready.cli_json("devices")[0]["alias"]
    pointer = f"@file({alias}:{target})"
    ready.cli_json("add", "--pointer", pointer, "tools/script")
    record = ready.cli_json("read", "tools/script")
    assert record["kind"] == "file"
    assert record["local_resolvable"] is True
    assert record["local_size"] == target.stat().st_size
    assert record["local_path"] == str(target)
    assert "SECRET TARGET BYTES" not in json.dumps(record)
    from mem.client import Client
    server_record = Client(ready.server, ready.token()).note("demo", "tools/script")
    assert server_record["local_resolvable"] is None
    assert "local_size" not in server_record
    assert "local_path" not in server_record

    target.unlink()
    assert ready.cli_json("read", "tools/script")["local_resolvable"] is False
    assert ready.cli_json("manifest", "tools")["entries"][0]["size"] == len((pointer + "\n").encode())


def test_foreign_pointer_is_inert_and_locked_target_is_never_read(ready):
    from mem.client import Client
    Client(ready.server).enroll("other", "test-secret", alias="other-device",
                                hostname="other-host", os_name="linux", arch="amd64")
    ready.cli_json("add", "--pointer", "@file(other-device:/tmp/thing)", "foreign")
    foreign = ready.cli_json("read", "foreign")
    assert foreign["points_at"] == "other-device"
    assert foreign["local_resolvable"] is None
    assert "local_path" not in foreign

    alias = ready.cli_json("devices")[0]["alias"]
    ready.cli_json("add", "--pointer", f"@locked({alias}:/private/token)", "secret")
    locked = ready.cli_json("read", "secret")
    assert locked["local_path"] == "/private/token"
    assert locked["local_resolvable"] is True
    assert "local_size" not in locked
    assert "/private/token" not in locked["summary"]


def test_pointer_bytes_are_not_served_and_mcp_is_absent(ready):
    alias = ready.cli_json("devices")[0]["alias"]
    ready.cli_json("add", "--pointer", f"@file({alias}:/tmp/tool)", "tool")
    for path, expected in (("/raw/demo/tool", 400), ("/mcp", 404)):
        request = urllib.request.Request(ready.server + path, headers={"Authorization": f"Bearer {ready.token()}"})
        try:
            urllib.request.urlopen(request, timeout=5)
            assert False, f"{path} unexpectedly succeeded"
        except urllib.error.HTTPError as exc:
            assert exc.code == expected


def test_non_inline_extension_tracks_only_a_pointer(ready):
    target = ready.write("tool.py", "print('hello')\n")
    result = ready.cli_json("add", str(target))
    assert result["content_stored"] is False
    assert result["record_tracked"] is True
    assert "content was not stored" in result["warning"]
    assert ready.cli_json("read", "tool.py")["kind"] == "file"
    assert "print('hello')" not in (ready.repo / result["path"]).read_text(encoding="utf-8")


def test_txt_content_keeps_its_filename_and_exact_bytes(ready):
    from mem.client import Client

    source = ready.write("guides/setup.txt", "Plain setup notes\nsecond line\n")
    original_bytes = source.read_bytes()
    policy = Client(ready.server, ready.token()).policy()
    assert policy["inline_extensions"] == [".md", ".txt"]
    added = ready.cli_json("add", str(source))
    assert added["storage"] == "content"
    assert added["content_stored"] is True
    assert added["path"] == "projects/demo/guides/setup.txt"
    assert (ready.repo / added["path"]).read_bytes() == source.read_bytes()
    record = ready.cli_json("read", "guides/setup.txt")
    assert record["kind"] == "text"
    assert record["body"] == source.read_bytes().decode("utf-8")
    assert "guides/setup.txt" in [hit["id"] for hit in ready.cli_json("search", "setup")["hits"]]
    assert ready.cli_json("manifest", "guides")["entries"][0]["kind"] == "text"
    first_revision = record["revision"]
    source.write_text("Changed setup notes\n", encoding="utf-8")
    ready.cli_json("add", str(source))
    assert ready.cli_json("read", "guides/setup.txt", "--revision", first_revision)["body"] == original_bytes.decode("utf-8")
    raw_request = urllib.request.Request(
        Client(ready.server, ready.token()).raw_url("demo", "guides/setup.txt", first_revision),
        headers={"Authorization": f"Bearer {ready.token()}"},
    )
    with urllib.request.urlopen(raw_request, timeout=5) as response:
        assert response.read() == original_bytes
    ready.cli_json("rm", "guides/setup.txt")
    assert not (ready.repo / added["path"]).exists()


@pytest.mark.parametrize("name", ["private.txt", "private.md"])
def test_untracked_project_file_is_not_public(ready, name):
    from mem.client import Client
    from mem.errors import MemError
    import pytest

    private = ready.repo / "projects" / "demo" / name
    private.parent.mkdir(parents=True, exist_ok=True)
    private.write_text("untracked secret", encoding="utf-8")
    client = Client(ready.server, ready.token())
    public_id = "private" if name.endswith(".md") else name
    assert public_id not in [row["id"] for row in ready.cli_json("manifest", "")["entries"]]
    with pytest.raises(MemError):
        client.note("demo", public_id)
    request = urllib.request.Request(client.raw_url("demo", public_id),
                                     headers={"Authorization": f"Bearer {ready.token()}"})
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(request, timeout=5)


def test_empty_text_file_survives_pinned_read(ready):
    source = ready.write("empty.txt", "")
    ready.cli_json("add", str(source))
    revision = ready.cli_json("read", "empty.txt")["revision"]
    assert ready.cli_json("read", "empty.txt", "--revision", revision)["body"] == ""


def test_committed_unsupported_file_is_not_public(ready):
    from mem.client import Client

    target = ready.repo / "projects" / "demo" / "private.bin"
    target.write_bytes(b"private bytes")
    subprocess.run(["git", "-C", str(ready.repo), "add", "--", "projects/demo/private.bin"], check=True)
    subprocess.run(["git", "-C", str(ready.repo), "commit", "-m", "local binary"],
                   check=True, capture_output=True)
    assert "private.bin" not in [row["id"] for row in ready.cli_json("manifest", "")["entries"]]
    client = Client(ready.server, ready.token())
    request = urllib.request.Request(client.raw_url("demo", "private.bin"),
                                     headers={"Authorization": f"Bearer {ready.token()}"})
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(request, timeout=5)


def test_pinned_binary_text_blob_is_not_public(ready):
    from mem.client import Client

    target = ready.repo / "projects" / "demo" / "old.txt"
    target.write_bytes(b"private\0bytes")
    subprocess.run(["git", "-C", str(ready.repo), "add", "--", "projects/demo/old.txt"], check=True)
    subprocess.run(["git", "-C", str(ready.repo), "commit", "-m", "old binary"],
                   check=True, capture_output=True)
    revision = subprocess.run(["git", "-C", str(ready.repo), "rev-parse", "HEAD"],
                              check=True, capture_output=True, text=True).stdout.strip()
    client = Client(ready.server, ready.token())
    request = urllib.request.Request(client.raw_url("demo", "old.txt", revision),
                                     headers={"Authorization": f"Bearer {ready.token()}"})
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(request, timeout=5)


def test_unqualified_and_unknown_project_pointers_are_rejected(ready):
    for value in ("@file(self:/tmp/tool)", "@file(unknown-device:/tmp/tool)"):
        result = ready.cli("add", "--pointer", value, "tools/rejected")
        assert result.returncode == 4
    assert ready.cli("read", "tools/rejected").returncode == 9


def test_server_extension_policy_is_shared_with_cli(tmp_path, repo_template):
    from conftest import Harness
    from mem.client import Client
    from mem.errors import MemError
    import pytest

    h = Harness(tmp_path, template=repo_template, inline_extensions=(".md", ".txt", ".json"))
    h.start()
    try:
        h.login()
        h.init("demo")
        source = h.write("config/data.json", '{"enabled": true}\n')
        assert Client(h.server, h.token()).policy()["inline_extensions"] == [".md", ".txt", ".json"]
        added = h.cli_json("add", str(source))
        assert added["storage"] == "content"
        assert added["path"] == "projects/demo/config/data.json"
        assert (h.repo / added["path"]).read_bytes() == source.read_bytes()
        client = Client(h.server, h.token())
        for kind in ("text", "note"):
            with pytest.raises(MemError) as error:
                client.add("demo", "blocked.py", b"print('no')\n", filename="blocked.py",
                           idempotency_key=f"blocked-{kind}", kind=kind)
            assert error.value.exit_code == 4
    finally:
        h.stop()


def test_device_home_is_private_and_identity_checked(ready):
    result = ready.cli_json("device", "init")
    target = Path(result["path"])
    assert target.is_file()
    assert target.parent == ready.home / ".device"
    assert ready.cli_json("device", "init")["path"] == str(target)

    text = target.read_text(encoding="utf-8")
    target.write_text(text.replace(result["alias"], "other-device", 1), encoding="utf-8")
    failure = ready.cli("device", "init")
    assert failure.returncode == 4
    assert "not this device's home" in failure.stderr
    private = target.parent / "notes.txt"
    private.write_text("private machine fact", encoding="utf-8")
    upload = ready.cli("add", str(private), "--as", "private.txt")
    assert upload.returncode == 4
    assert "device-home files are private" in upload.stderr


def test_device_home_expands_tilde_in_global_gitignore(ready):
    """`core.excludesFile = ~/...` must be expanded, not treated literally."""
    subprocess.run(
        ["git", "config", "--global", "core.excludesFile", "~/.gitignore_global"],
        env=ready.env, check=True,
    )
    ready.cli_json("device", "init")
    ignore = ready.home / ".gitignore_global"
    assert ignore.is_file()
    assert ".device/" in ignore.read_text(encoding="utf-8")
    assert not (ready.workdir / "~").exists(), "a literal ~ directory was created"


def test_device_arch_matches_python(ready):
    ready.cli_json("device", "init")
    rows = ready.cli_json("devices")
    import platform

    assert rows[0]["arch"] == platform.machine(), rows[0]


def test_credentials_are_private_on_posix(ready):
    import os
    import stat

    if os.name == "nt":
        pytest.skip("Windows does not expose POSIX mode bits")
    credentials = ready.config_dir / "credentials"
    credentials.chmod(0o644)
    ready.cli_json(
        "login", "--server", ready.server, "--name", "second",
        "--alias", "second-device",
    )
    mode = stat.S_IMODE(credentials.stat().st_mode)
    assert mode == 0o600, oct(mode)
