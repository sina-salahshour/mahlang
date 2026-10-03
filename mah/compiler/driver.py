"""The compiler driver: source text/file -> `mah.bytecode.program.Program`
(or straight to encoded bytes) -- preprocess -> lex -> parse -> resolve ->
codegen -> lower, in one place, so `mah run`/`mah build` and
`tests/support.py` share exactly one front-end pipeline instead of each
re-implementing it (M14_SPEC.md #4).

Compile-time errors are raised the same way they always were: a
`SyntaxError` whose message already has its position resolved to a
`#line:col`/`file#line:col` label (`mah/cli/main.py`'s existing
`generate_code` used the same convention; kept here so both share it).
"""

from __future__ import annotations

import os
import re

from ..bytecode.encode import encode
from ..bytecode.lower import line_col, lower
from ..bytecode.program import Program
from ..preprocessor import demangle_message, preprocess, source_label
from . import typecheck
from .codegen import Codegen
from .lexer import Lexer
from .parser import Parser
from .ast_nodes import EnumDecl, ImplDecl, LetStmt, StructDecl, TestDecl, TraitDecl
from .resolve import Resolver


def _location_label(pp, entry_path: str, combined_offset: int) -> str | None:
    path, src_offset = pp.map_to_source(combined_offset)
    text = pp.files.get(path, "")
    if src_offset > len(text):
        return None
    line, col = line_col(text, src_offset)
    if path == entry_path:
        return f"#{line}:{col}"
    return f"{source_label(path)}#{line}:{col}"


def _format_located_messages(pp, items: list[tuple[str, int]], prefix: str = "") -> str:
    """Shared by `_format_parser_errors` and the type-checker's diagnostic
    formatting: demangle each message, resolve its position to a
    `#line:col`/`file#line:col` label, and join one per line. `items` is a
    list of `(message, position)` pairs; `position` is only used as a
    fallback when `message` doesn't itself cite a position (see the
    `cited` comment below)."""
    lines = []
    for message, position in items:
        message = demangle_message(message)
        # Map the position the message itself cites, which isn't always the
        # token the parser recorded (e.g. a range error cites its `..`
        # operator, a duplicate-keyword error the keyword's name); fall back
        # to the recorded position.
        cited = re.search(r"at position '?(\d+)'?", message)
        label = _location_label(pp, pp.entry_path, int(cited.group(1)) if cited else position)
        if label:
            if cited:
                message = message[: cited.start()] + f"at position {label}" + message[cited.end():]
            else:
                message = f"{message} at position {label}"
        lines.append(f"{prefix}{message}")
    return "\n".join(lines)


def _format_parser_errors(pp, errors: list) -> str:
    return _format_located_messages(pp, errors)


def _format_type_diagnostics(pp, diagnostics: list) -> str:
    """One `type error: ...` line per diagnostic, located and demangled
    exactly like `_format_parser_errors` (same helper, so the two can't
    drift), for the `SyntaxError` `compile_to_program` raises in
    strict/explicit mode."""
    return _format_located_messages(pp, [(d.text(), d.position) for d in diagnostics], prefix="type error: ")


def format_diagnostic(pp, diag) -> str:
    """The located, demangled form of one `TypeDiagnostic`, with no
    `type error: ` prefix -- for `mah check`, which prints its own
    `warning: `/`error: ` prefix per diagnostic."""
    return _format_located_messages(pp, [(diag.text(), diag.position)])


TEST_SUFFIX = ".test.mh"

# M28 (docs/MAH_TEST.md): what a test file's own top level may hold -- no
# statement that would run by itself.
_TEST_FILE_DECLARATIONS = (LetStmt, StructDecl, EnumDecl, TraitDecl, ImplDecl, TestDecl)


def is_test_file(path: str | None) -> bool:
    return path is not None and path.endswith(TEST_SUFFIX)


def _check_test_file(pp, program: list) -> None:
    """M28: in a test file, only declarations and `test` blocks at the top
    level (imports are already gone), and `test` blocks only in the test
    file itself, not in something it imports."""
    for stmt in program:
        position = getattr(stmt, "position", None)
        in_entry = position is not None and pp.map_to_source(position)[0] == pp.entry_path
        if isinstance(stmt, TestDecl) and not in_entry:
            raise SyntaxError(
                _format_parser_errors(pp, [("'test' blocks are only allowed in the test file itself", position)])
            )
        if in_entry and not isinstance(stmt, _TEST_FILE_DECLARATIONS):
            raise SyntaxError(
                _format_parser_errors(
                    pp,
                    [
                        (
                            "A test file may only contain declarations and 'test' blocks; "
                            "move this statement into a test",
                            position,
                        )
                    ],
                )
            )


def _parse_and_resolve(*, path: str | None, text: str | None, test: bool = False):
    """preprocess -> lex -> parse -> resolve, shared by `compile_to_program`
    and `type_check` so the two front ends can't drift. Raises `SyntaxError`
    for a preprocess or parse error (already located); a resolve error
    propagates as whatever `Resolver.resolve_program` raises, unformatted
    (the caller is expected to locate it the same way every other compile-
    time error is, via `_location_label`)."""
    if path is not None and text is None:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()

    pp = preprocess(path, text)
    if pp.errors:
        message, offset, _length = pp.errors[0]
        entry_text = pp.files.get(pp.entry_path, text or "")
        line, col = line_col(entry_text, offset)
        raise SyntaxError(f"{message} at position #{line}:{col}")

    lexer = Lexer(pp.text)
    parser = Parser(lexer, allow_tests=test)
    program = parser.parse_program()
    if parser.errors:
        raise SyntaxError(_format_parser_errors(pp, parser.errors))
    if test:
        _check_test_file(pp, program)

    resolver = Resolver(prelude_start=pp.prelude_start, pp=pp)
    resolver.resolve_program(program)

    return pp, program, resolver


def compile_to_program(
    *,
    path: str | None = None,
    text: str | None = None,
    target: str = "debug",
    check: str = "loose",
    test: bool = False,
) -> Program:
    """`test=True` (M28, `mah test`) compiles a test file: its `test`
    blocks, and a TESTS table in the output. Otherwise a `.test.mh` file is
    refused -- tests never end up in `mah run`/`mah build` output."""
    if not test and is_test_file(path):
        raise SyntaxError(f"'{os.path.basename(path)}' is a test file; run it with `mah test`")
    pp, program, resolver = _parse_and_resolve(path=path, text=text, test=test)

    # M22: zero cost under "loose" -- the checker never runs at all.
    if check != "loose":
        diagnostics = typecheck.check_program(program, resolver)
        # M26: a warning-only diagnostic (a `catch` arm for an error that's
        # never thrown) never fails a build.
        reportable = [d for d in typecheck.reportable(diagnostics, check) if not typecheck.is_warning(d, check)]
        if reportable:
            raise SyntaxError(_format_type_diagnostics(pp, reportable))

    codegen = Codegen(resolver.global_frame, pp)
    buf = codegen.generate(program)

    return lower(buf, resolver, pp, target=target)


def compile_to_bytes(
    *,
    path: str | None = None,
    text: str | None = None,
    target: str = "debug",
    check: str = "loose",
    test: bool = False,
) -> bytes:
    return encode(compile_to_program(path=path, text=text, target=target, check=check, test=test))


def type_check(*, path: str | None = None, text: str | None = None):
    """preprocess -> lex -> parse -> resolve -> the full, unfiltered
    checker output, for `mah check` (which filters/labels the result
    itself with `typecheck.reportable`/`format_diagnostic`). Unlike
    `compile_to_program`, this never raises for a type diagnostic -- only
    for a preprocess/parse error (`SyntaxError`, exactly as
    `compile_to_program` raises it) or a resolve error (propagated
    unchanged, exactly as `compile_to_program` lets it propagate).

    Returns `(pp, diagnostics)`: `pp` (the `Preprocessed` result) is
    needed by the caller to locate each diagnostic (`format_diagnostic`)."""
    pp, program, resolver = _parse_and_resolve(path=path, text=text, test=is_test_file(path))
    diagnostics = typecheck.check_program(program, resolver)
    return pp, diagnostics
