"""`mem python` — the composition escape hatch (`MEM-ACCESS.md` Part A).

The point of these tests is the *constraint*, not the feature: composition lives
here precisely so that the verb set does not have to grow, and writes are absent
unless asked for.
"""

from __future__ import annotations

import json

from conftest import note_text


def test_python_composes_several_calls_in_one_shot(ready):
    ready.write("a.md", note_text(summary="alpha"))
    ready.write("b.md", note_text(summary="beta", body="findable token"))
    ready.cli_json("add", "a.md", "--as", "a")
    ready.cli_json("add", "b.md", "--as", "b")

    # Three round trips (ls, search, outline), one line of output.
    out = ready.cli_json(
        "python",
        "-c",
        "emit({'ids': sorted(n['id'] for n in ls()),"
        " 'hits': [h['id'] for h in search('findable')],"
        " 'headings': [h['text'] for h in outline('b')['headings']]})",
    )
    assert out["ids"] == ["PROJECT", "a", "b"]
    assert out["hits"] == ["b"]
    assert out["headings"] == ["A note"]


def test_python_is_read_only_by_default(ready):
    """`add` is ABSENT, not merely discouraged -- referencing it is a NameError."""
    result = ready.cli("python", "-c", "add('whatever.md')")
    assert result.returncode == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "internal_error"
    assert "NameError" in payload["error"]["message"]
    # Typed, not a bare traceback: the whole error is ONE json line, so nothing
    # multi-line leaked -- the traceback rides inside detail, escaped.
    assert len(result.stderr.strip().splitlines()) == 1
    assert "traceback" in payload["error"]["detail"]


def test_python_does_not_expose_the_token_as_a_name(ready):
    out = ready.cli_json(
        "python",
        "-c",
        "emit(sorted(k for k in globals() if not k.startswith('_')))",
    )
    assert "token" not in out
    assert "credentials" not in out
    # The client is there, and it necessarily holds the token to make requests.
    assert "client" in out


def test_python_reads_a_script_file(ready):
    ready.write("s.md", note_text(summary="from a script"))
    ready.cli_json("add", "s.md", "--as", "s")
    script = ready.write("compose.py", "emit(project)\n")
    result = ready.cli("python", str(script))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == '"demo"'


def test_python_allow_write_binds_add_and_rm(ready):
    ready.write("w.md", note_text(summary="written from python"))
    added = ready.cli("python", "--allow-write", "-c", "add('w.md', as_id='w')")
    assert added.returncode == 0, added.stderr
    ready.flush()
    assert ready.cli_json("read", "w")["summary"] == "written from python"

    removed = ready.cli("python", "--allow-write", "-c", "rm('w')")
    assert removed.returncode == 0, removed.stderr
    ready.flush()
    assert ready.cli("read", "w").returncode == 9


def test_python_local_error_is_typed_not_a_traceback(ready):
    result = ready.cli("python", "-c", "raise ValueError('boom')")
    assert result.returncode == 1
    payload = json.loads(result.stderr)
    assert "ValueError: boom" in payload["error"]["message"]


def test_python_prints_the_last_expression(ready):
    """`exec` discards a trailing expression, which made `-c "ls()"` silent.

    The composition runner is only useful if the caller sees the result, so the
    final expression is evaluated and printed (`MEM-ACCESS.md` §3.1: "the script's
    own return value (if any) is printed").
    """
    ready.write("a.md", note_text(summary="alpha"))
    ready.cli_json("add", "a.md", "--as", "a")

    assert ready.cli_json("python", "-c", "1 + 1") == 2
    # The case that matters: a real call, whose value used to be thrown away.
    assert ready.cli_json("python", "-c", "sorted(n['id'] for n in ls())") == [
        "PROJECT",
        "a",
    ]


def test_python_print_still_works(ready):
    """The last-expression rule does not replace `print()`."""
    result = ready.cli("python", "-c", "print('hello')\nprint('world')")
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["hello", "world"]


def test_python_prints_nothing_when_there_is_no_value(ready):
    """A statement or a `None` result prints nothing -- not a noisy `null`."""
    for program in ("None", "x = 1", "if True:\n    pass"):
        result = ready.cli("python", "-c", program)
        assert result.returncode == 0, (program, result.stderr)
        assert result.stdout.strip() == "", f"{program!r} printed {result.stdout!r}"


def test_python_fails_before_running_side_effects_on_a_compile_error(ready):
    """A program that cannot compile must not run part of itself first.

    `ast.parse` accepts `await` outside a function; the SyntaxError only surfaces
    at compile time. Compiling the trailing expression lazily would execute the
    whole prefix -- side effects included, and a real write under `--allow-write`
    -- before reporting a program that could never have started.
    """
    result = ready.cli("python", "-c", "print('side-effect-happened')\nawait x")
    assert result.returncode == 1, result.stderr
    assert "side-effect-happened" not in result.stdout
    assert "SyntaxError" in result.stderr


def test_python_with_an_empty_program_runs_nothing(ready):
    """`-c ""` is an empty program, not "read stdin".

    The old truthiness test fell through to `sys.stdin.read()`, which blocks on a
    TTY and looks like a hang rather than an empty program.
    """
    result = ready.cli("python", "-c", "")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""
