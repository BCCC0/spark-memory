"""Generate deterministic Python-oracle vectors for the Go mem client."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mem import ids, keys, paths, pointers, summary  # noqa: E402
from mem import config as cfg  # noqa: E402
from mem.errors import MemError, TABLE  # noqa: E402


def outcome(fn, *args, **kwargs):
    try:
        return {"ok": True, "value": fn(*args, **kwargs)}
    except MemError as exc:
        return {"ok": False, "code": exc.code, "message": exc.message, "detail": exc.detail}


def summary_outcome(text: str):
    def check(value: str):
        data, _ = summary.parse_frontmatter(value)
        return summary.validate_summary(data)

    return outcome(check, text)


def marker_outcome(text: str):
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False, encoding="utf-8") as handle:
            handle.write(text)
            path = Path(handle.name)
        try:
            patterns = cfg.read_marker_patterns(path)
            import tomllib

            data = tomllib.loads(text)
        finally:
            path.unlink(missing_ok=True)
        return {
            "ok": True,
            "value": {
                "project": str(data.get("project") or ""),
                "server": str(data.get("server") or ""),
                "patterns": patterns,
            },
        }
    except Exception:
        return {"ok": False, "code": "invalid_request"}


def revision() -> str:
    """A deterministic digest of the Python oracle, not a commit id.

    A commit id would change on every regeneration and make the CI drift check
    impossible; a content digest only changes when the oracle does.
    """
    import hashlib

    digest = hashlib.sha256()
    for relative in [
        "src/mem/ids.py", "src/mem/pointers.py", "src/mem/summary.py",
        "src/mem/paths.py", "src/mem/keys.py", "src/mem/errors.py",
        "src/mem/config.py",
    ]:
        # Normalise line endings so a Windows checkout and the Linux CI agree.
        digest.update((ROOT / relative).read_bytes().replace(b"\r\n", b"\n"))
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def main() -> None:
    cases = []

    def add(kind: str, value, expected, role: str) -> None:
        cases.append({"kind": kind, "role": role, "input": value, "expected": expected})

    id_inputs = [
        "PROJECT", "", "alpha", "ops/boot", "foo.md", "a_b", "A", "Ä", "a bC",
        "../x", ".hidden", "a//b", "a—b", "x" * 80, "x" * 81,
        "x" * 40 + "/file", "x" * 41 + "/file", "界" * 30,
        "😀", "á", "\\ops\\boot\\", "one two", "d--e",
    ]
    for value in id_inputs:
        add("id", value, outcome(ids.validate_id, value), "contract")
    for value in ["proj", "", "Mixed", "two words", "x" * 40, "x" * 41, "a/b"]:
        add("project", value, outcome(ids.validate_project, value), "contract")
    for value in [
        "@file(laptop:C:\\src\\x)", "@dir(home:/docs)", "@locked(home:/secret)",
        "@file(self:/a)", "@file(no_alias:/a)", "@file(home:)", "@file(home:/a)b)",
    ]:
        result = outcome(pointers.parse_pointer, value)
        if result["ok"]:
            result["value"] = result["value"].canonical
        add("pointer", value, result, "contract")

    summaries = [
        "hello", "yes", "no", "on", "off", "y", "n", "true", "123", "~",
        "yEs", "oN", "oFf", "Yes", "OFF", "\"yes\"", "'on'",
        "!!str yes", "!!str on", "!!bool yes",
        "第一行", "|\n  a\n  b", "界" * 200, "界" * 201, "😀" * 200, "á" * 100,
    ]
    for value in summaries:
        text = f"---\nsummary: {value}\n---\nbody\n"
        add("summary", text, summary_outcome(text), "contract")
    for text in [
        "---\nsummary: &a hello\nsummary: *a\n---\nbody\n",
        "---\ndefaults: &d\n  summary: merged\n<<: *d\n---\nbody\n",
        "---\ndefaults: &d\n  summary: merged\n<<: *d\nsummary: explicit\n---\nbody\n",
    ]:
        add("summary", text, summary_outcome(text), "contract")
        add("summary", text, summary_outcome(text), "contract")
    duplicate = "---\nsummary: first\nsummary: last\n---\n"
    add("summary", duplicate, summary_outcome(duplicate), "contract")

    for content, path, expect, origin, message in [
        ("abc", "n", "", "", ""), ("abc", "n", "rev", "D:/x", "change"),
    ]:
        add("add_key", [content, path, expect, origin, message],
            keys.add_key(content, path, expect, origin_path=origin, message=message),
            "port-oracle")
        add("rm_key", [path, expect, message],
            keys.rm_key(path, expect, message=message), "port-oracle")
        add("mv_key", [path, "dest", expect, message],
            keys.mv_key(path, "dest", expect, message=message), "port-oracle")
        add("repath_key", [path, "new", expect, message],
            keys.repath_key(path, "new", expect, message=message), "port-oracle")

    for value in ["/mnt/d/src/x.md", "/mnt/c", "/mnt/e/", "D:\\src\\x.md",
                  "C:/Work/a.md", "relative/x", "/home/x", "d:",
                  "D:relative", "d:foo", "C:/a//b", "C:/a/./b", "C:/a/../b",
                  "\\mnt\\d\\x", "/mnt/d/src/界 note.md"]:
        add("path", value,
            {"windows": paths.to_windows(value), "wsl": paths.to_wsl(value)},
            "port-oracle")

    for value in ["ops\\boot.md", "./notes/a.MD", "plain", "Ä.md", "a b.md"]:
        add("from_path", value, ids.id_from_path(value), "port-oracle")

    marker_texts = [
        "project = 'p'\nserver = 'http://x'\n",
        "project = 'p'\n[track]\npatterns = ['*.md', '!drafts/**']\n",
        "project = 'p'\n[track]\npatterns = '*.md'\n",
        "project = 'p'\n[track]\npatterns = [1, '*.md']\n",
        "project = 'p'\nproject = 'q'\n",
        "[track]\npatterns = [\n",
    ]
    for value in marker_texts:
        add("marker", value, marker_outcome(value), "port-oracle")

    output = {
        "generator": "tools/gen_client_vectors.py",
        "source_revision": revision(),
        "error_table": TABLE,
        "cases": cases,
    }
    target = ROOT / "client" / "testdata" / "vectors" / "rules.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
