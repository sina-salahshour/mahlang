"""Test helpers: run Mah source (or a file) through the real pipeline
in-process and capture its stdout, so tests exercise the actual
lexer -> parser -> resolve -> codegen -> interpreter path end to end
rather than mocking any of it.

Works whether invoked via `python3 -m unittest discover -s tests -t .`
(the repo root is already the top-level dir, so imports below just work)
or by running a test file directly (`python3 tests/test_language.py`) --
the sys.path bootstrap below covers the second case.
"""

from __future__ import annotations

import contextlib
import io
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

EXAMPLES_DIR = os.path.join(_REPO_ROOT, "examples")

from mah.code_interpreter import run_code  # noqa: E402
from mah.compiler.codegen import Codegen  # noqa: E402
from mah.compiler.lexer import Lexer  # noqa: E402
from mah.compiler.parser import Parser  # noqa: E402
from mah.compiler.resolve import Resolver  # noqa: E402
from mah.preprocessor import preprocess  # noqa: E402


def compile_source(*, path: str | None = None, text: str | None = None):
    """Run the full front end (preprocess -> lex -> parse -> resolve ->
    codegen) and return the resulting CodeBuffer. Pass `path` for a file
    on disk (imports resolve relative to it, matching `mah.py`) or `text`
    for an in-memory snippet with no imports."""
    pp = preprocess(path, text)
    if pp.errors:
        message, _offset, _length = pp.errors[0]
        raise SyntaxError(message)

    lexer = Lexer(pp.text)
    parser = Parser(lexer)
    program = parser.parse_program()

    resolver = Resolver()
    resolver.resolve_program(program)

    codegen = Codegen(resolver.global_frame)
    return codegen.generate(program)


def parse_source(text: str):
    """Lex + parse an in-memory snippet and return `(program, parser)`.

    Unlike `run_source`/`compile_source`, this stops after parsing and
    hands back the `Parser` instance itself -- needed for M6's
    forgiving-parser tests, which inspect `parser.errors` directly (a list
    collected during parsing, not raised) rather than only observing
    stdout or a raised exception. No import resolution (no `path`): a
    snippet-only helper, matching `run_source`."""
    lexer = Lexer(text)
    parser = Parser(lexer)
    program = parser.parse_program()
    return program, parser


def run_source(text: str, stdin: str = "") -> str:
    """Compile and run an in-memory Mah snippet, returning everything it
    printed to stdout."""
    return _run(compile_source(text=text), stdin)


def run_file(path: str, stdin: str = "") -> str:
    """Compile and run a Mah file from disk, returning everything it
    printed to stdout."""
    return _run(compile_source(path=path), stdin)


def _run(buf, stdin: str) -> str:
    # contextlib has no redirect_stdin (only stdout/stderr) -- swap
    # sys.stdin manually, since run_code's `input` opcode reads from it
    # directly via sys.stdin.read(1).
    out = io.StringIO()
    old_stdin = sys.stdin
    sys.stdin = io.StringIO(stdin)
    try:
        with contextlib.redirect_stdout(out):
            run_code(buf.code[:400], buf.global_slot_count)
    finally:
        sys.stdin = old_stdin
    return out.getvalue()


def example_path(name: str) -> str:
    return os.path.join(EXAMPLES_DIR, name)
