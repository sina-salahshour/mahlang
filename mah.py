#!/usr/bin/env python3
import os
import re
import sys
from pathlib import Path

from code_interpreter import run_code
from compiler.codegen import Codegen, CodeBuffer
from compiler.lexer import Lexer
from compiler.parser import Parser
from compiler.resolve import Resolver
from preprocessor import demangle_message, preprocess

sys.tracebacklimit = 0

USAGE_HELP_MESSGE = """Usage:
    mah run\t <input file>\t\t# to run file
    mah build\t <input file>\t\t# to see the program instructions"""


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


def print_code_block(buf: CodeBuffer, should_save_to_file=False):
    code_block = "\n"
    for index, code in enumerate(buf.code[:400]):
        if not code:
            break
        code_block += f"{index}:\t{'|'.join(map(_dump_cell, code))}\n"
        code_block += ("\t " + "-" * 36) + "\n"

    if not should_save_to_file:
        print(code_block)
    else:
        with open("output.txt", "w") as f:
            f.write(code_block)


def read_file(file_name):
    try:
        with open(file_name) as f:
            input_str = f.read()
    except FileNotFoundError:
        print(f"Error: file not found '{file_name}'\n")
        print(USAGE_HELP_MESSGE)
        exit(-3)
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


def main():
    should_save_to_file = False
    if len(sys.argv) == 3:
        command = sys.argv[1]
        file_name = sys.argv[2]
    elif len(sys.argv) == 2:
        command = "run"
        file_name = sys.argv[1]
    elif len(sys.argv) == 1 and Path("input.txt"):
        command = "build"
        file_name = "input.txt"
        should_save_to_file = True
    else:
        print(USAGE_HELP_MESSGE)
        exit(-1)

    # Read the entry file, then inline any `import "..."` directives.
    entry_str = read_file(file_name)
    pp = preprocess(file_name, entry_str)

    # Report unresolved imports before attempting to compile.
    if pp.errors:
        message, offset, _length = pp.errors[0]
        line_info = find_error_line(entry_str, offset)
        if line_info:
            line, row = line_info
            message = f"{message} at position #{line}:{row}"
        raise SyntaxError(message)

    file_str = pp.text

    try:
        match command:
            case "build":
                buf = generate_code(file_str, pp)
                print_code_block(buf, should_save_to_file=should_save_to_file)
            case "run":
                buf = generate_code(file_str, pp)
                run_code(buf.code[:400], buf.global_slot_count)
            case unknown_command:
                print(
                    f"Error: command not found '{unknown_command}'.\navailable commands are 'build' and 'run'\n"
                )
                print(USAGE_HELP_MESSGE)
                exit(-2)
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


if __name__ == "__main__":
    main()
