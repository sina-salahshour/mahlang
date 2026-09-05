#!/usr/bin/env python3
import os
import re
import sys
from pathlib import Path

from actions import register_actions
from code_interpreter import run_code
from compiler.ir_generator import IRGenerator
from compiler.lexer import Lexer
from compiler.parser import Parser
from preprocessor import demangle_message, preprocess

sys.tracebacklimit = 0

USAGE_HELP_MESSGE = """Usage:
    mah run\t <input file>\t\t# to run file
    mah build\t <input file>\t\t# to see the program instructions"""


def print_code_block(ir: IRGenerator, should_save_to_file=False):
    code_block = "\n"
    for index, code in enumerate(ir.sstack[:400]):
        if not code:
            break
        code_block += f"{index}:\t{'|'.join(map(lambda x: ' '.center(9) if x is None else str(x).center(9),code))}\n"
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


def generate_code(input_str: str):
    lexer = Lexer(input_str)
    parser = Parser(lexer)
    ir = IRGenerator(parser)

    register_actions(ir)

    ir.generate()

    assert len(ir.stack) == 0, "Error: Stack is not empty" + str(ir.stack)

    return ir


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
                ir = generate_code(file_str)
                print_code_block(ir, should_save_to_file=should_save_to_file)
            case "run":
                ir = generate_code(file_str)
                run_code(ir.sstack[:400])
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
