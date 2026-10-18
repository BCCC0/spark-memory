"""Capture argparse help as a standalone Go-client fixture.

The Go client deliberately does not implement ``mem python``, so the Python
parser's ``python`` subparser is removed before help is formatted. The fixture
therefore documents exactly the commands the Go binary accepts.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "src"))

from mem.cli import build_parser  # noqa: E402

commands = [
    (), ("login",), ("logout",), ("init",), ("status",), ("flush",),
    ("projects",), ("manifest",), ("device",), ("device", "init"),
    ("ls",), ("read",), ("devices",), ("add",), ("pointer",),
    ("pointer", "add"), ("rm",), ("mv",), ("search",), ("index",),
    ("hook",), ("hook", "add"), ("hook", "rm"),
    ("hook", "ls"), ("hook", "init"), ("hook", "update"),
    ("hook", "check"),
]


def subparsers(parser: argparse.ArgumentParser):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def parser_for(path: tuple[str, ...]):
    parser = build_parser()
    action = subparsers(parser)
    if action is not None:
        action.choices.pop("python", None)
        action._choices_actions = [
            item for item in action._choices_actions if getattr(item, "dest", None) != "python"
        ]
        # argparse caches the metavar used in the usage line.
        action._metavar = None
    for name in path:
        action = subparsers(parser)
        if action is None or name not in action.choices:
            raise SystemExit(f"unknown help path: {path!r}")
        parser = action.choices[name]
    return parser


output = {}
for args in commands:
    parser = parser_for(args)
    output[" ".join(args) or "root"] = parser.format_help()

target = root / "client" / "internal" / "cli" / "help.json"
target.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
