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
from ..preprocessor import demangle_message, preprocess
from .codegen import Codegen
from .lexer import Lexer
from .parser import Parser
from .resolve import Resolver


def _location_label(pp, entry_path: str, combined_offset: int) -> str | None:
    path, src_offset = pp.map_to_source(combined_offset)
    text = pp.files.get(path, "")
    if src_offset > len(text):
        return None
    line, col = line_col(text, src_offset)
    if path == entry_path:
        return f"#{line}:{col}"
    return f"{os.path.basename(path)}#{line}:{col}"


def _format_parser_errors(pp, errors: list) -> str:
    lines = []
    for message, position in errors:
        message = demangle_message(message)
        label = _location_label(pp, pp.entry_path, position)
        if label:
            message = re.sub(rf"at position '?{position}'?", f"at position {label}", message, count=1)
        lines.append(message)
    return "\n".join(lines)


def compile_to_program(*, path: str | None = None, text: str | None = None, target: str = "debug") -> Program:
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
    parser = Parser(lexer)
    program = parser.parse_program()
    if parser.errors:
        raise SyntaxError(_format_parser_errors(pp, parser.errors))

    resolver = Resolver()
    resolver.resolve_program(program)

    codegen = Codegen(resolver.global_frame)
    buf = codegen.generate(program)

    return lower(buf, resolver, pp, target=target)


def compile_to_bytes(*, path: str | None = None, text: str | None = None, target: str = "debug") -> bytes:
    return encode(compile_to_program(path=path, text=text, target=target))
