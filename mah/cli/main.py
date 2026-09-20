#!/usr/bin/env python3
import argparse
import os
import re
import sys

from ..code_interpreter import run_code
from ..compiler.codegen import Codegen, CodeBuffer
from ..compiler.lexer import Lexer
from ..compiler.parser import Parser
from ..compiler.resolve import Resolver
from ..preprocessor import demangle_message, preprocess

sys.tracebacklimit = 0

_SUBCOMMANDS = {"run", "build", "lsp"}


_DUMP_COLUMN_WIDTH = 9
_DUMP_MAX_CELL = 24  # M8: some M1-M4 opcode payloads (closure/struct/enum/
# matchtag's nested-tuple args, an EnumInstance/StructInstance repr for a
# literal `ld`) are far wider than the fixed 9-char column this dump was
# designed around -- truncate instead of letting one long cell blow out a
# single row's alignment; still readable, just not necessarily complete.


def _dump_cell(value) -> str:
    if value is None:
        return " ".center(_DUMP_COLUMN_WIDTH)
    text = str(value)
    if len(text) > _DUMP_MAX_CELL:
        text = text[: _DUMP_MAX_CELL - 1] + "…"
    return text.center(_DUMP_COLUMN_WIDTH)


def print_code_block(buf: CodeBuffer, output_path: str | None = None):
    code_block = "\n"
    for index, code in enumerate(buf.code[:400]):
        if not code:
            break
        code_block += f"{index}:\t{'|'.join(map(_dump_cell, code))}\n"
        code_block += ("\t " + "-" * 36) + "\n"

    if output_path is None:
        print(code_block)
    else:
        with open(output_path, "w") as f:
            f.write(code_block)


def read_file(file_name):
    try:
        with open(file_name) as f:
            input_str = f.read()
    except FileNotFoundError:
        print(f"Error: file not found '{file_name}'", file=sys.stderr)
        sys.exit(2)
    return input_str


def generate_code(input_str: str, pp) -> CodeBuffer:
    lexer = Lexer(input_str)
    parser = Parser(lexer)
    program = parser.parse_program()

    if parser.errors:
        # M6: the parser is forgiving (it recovers and keeps going so a
        # tool -- the LSP -- can report every syntax mistake in one pass),
        # but `mah.py build`/`run` still refuse outright to build/execute a
        # program with any parse error, collected or not -- see
        # docs/V2_DESIGN.md's M6 milestone. Fold every collected error into
        # one SyntaxError message (each position already substituted with
        # its real file#line:col label) so it composes with the single-
        # exception handling `main()`'s outer `except` block already does.
        raise SyntaxError(_format_parser_errors(pp, parser.errors))

    resolver = Resolver()
    resolver.resolve_program(program)

    codegen = Codegen(resolver.global_frame)
    return codegen.generate(program)


def find_error_line(input_str: str, pos: int):
    line_number = 1
    row_number = 1

    for index, char in enumerate(input_str):
        row_number += 1
        if char == "\n":
            line_number += 1
            row_number = 1
        if index == pos:
            return line_number, row_number


def _format_parser_errors(pp, errors: list) -> str:
    """M6: render every collected `parser.errors` entry as one combined
    message, each with its raw combined-text position already resolved to
    a real `#line:col`/`file#line:col` label -- so the result, once handed
    to `main()`'s outer `except` as a single `SyntaxError`, has no more
    bare `at position <digits>` patterns left for that handler's own
    position-substitution regex to find (it just passes the message
    through unchanged, exactly as it already does for a plain single-error
    message with no position at all)."""
    lines = []
    for message, position in errors:
        message = demangle_message(message)
        label = _location_label(pp, pp.entry_path, position)
        if label:
            message = re.sub(rf"at position '?{position}'?", f"at position {label}", message, count=1)
        lines.append(message)
    return "\n".join(lines)


def _location_label(pp, entry_path: str, combined_offset: int) -> str:
    """Render a ``#line:row`` (entry file) or ``file#line:row`` location."""
    path, src_offset = pp.map_to_source(combined_offset)
    line_info = find_error_line(pp.files.get(path, ""), src_offset)
    if not line_info:
        return None
    line, row = line_info
    if path == entry_path:
        return f"#{line}:{row}"
    return f"{os.path.basename(path)}#{line}:{row}"


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mah",
        description="The Mah programming language toolchain.",
    )
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="compile and run a .mh file")
    run_parser.add_argument("file", help="path to the .mh file to run")

    build_parser = subparsers.add_parser("build", help="compile a .mh file and print its bytecode")
    build_parser.add_argument("file", help="path to the .mh file to compile")
    build_parser.add_argument(
        "-o", "--output", metavar="PATH",
        help="write the bytecode dump to PATH instead of printing it",
    )

    lsp_parser = subparsers.add_parser("lsp", help="start the Mah language server (speaks LSP over stdio)")
    lsp_parser.add_argument(
        "--version", action="store_true",
        help="print the language server's version and exit",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point -- see bin/mah (installed) and mah/__main__.py
    (`python -m mah`, e.g. from a repo checkout) for how this gets
    invoked.

    Preserves one piece of the old CLI's UX: a bare file argument with no
    subcommand word (`mah foo.mh`) is shorthand for `mah run foo.mh` --
    only kicks in when the first token isn't a known subcommand name or a
    help/version flag, so it never shadows `mah run ...` etc. (Known,
    accepted limitation: a real file literally named `run`/`build`/`lsp`
    can't be run via this shorthand -- use `mah run ./run` instead.)
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] not in _SUBCOMMANDS and argv[0] not in ("-h", "--help", "--version"):
        argv = ["run", *argv]

    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 1

    if args.command == "lsp":
        from ..lsp.server import main as lsp_main
        return lsp_main(["--version"] if args.version else [])

    entry_str = read_file(args.file)
    pp = preprocess(args.file, entry_str)

    if pp.errors:
        message, offset, _length = pp.errors[0]
        line_info = find_error_line(entry_str, offset)
        if line_info:
            line, row = line_info
            message = f"{message} at position #{line}:{row}"
        raise SyntaxError(message)

    file_str = pp.text

    try:
        if args.command == "build":
            buf = generate_code(file_str, pp)
            print_code_block(buf, output_path=args.output)
        elif args.command == "run":
            buf = generate_code(file_str, pp)
            run_code(buf.code[:400], buf.global_slot_count)
    except Exception as e:
        (message, *_) = e.args
        message = demangle_message(message)
        pos = re.findall(r"at position '?(\d+)'?", message)
        if not len(pos):
            e.args = (message,)
            raise e
        [pos] = pos
        label = _location_label(pp, pp.entry_path, int(pos))
        if not label:
            e.args = (message,)
            raise e
        message = message.replace(pos, label)
        e.args = (message,)
        raise e

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
