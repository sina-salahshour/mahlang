#!/usr/bin/python
import re
import sys

from actions import register_actions
from code_interpreter import run_code
from compiler.ir_generator import IRGenerator
from compiler.lexer import Lexer
from compiler.parser import Parser

sys.tracebacklimit = 0

USAGE_HELP_MESSGE = """Usage:
    mah run\t <input file>\t\t# to run file
    mah build\t <input file>\t\t# to see the program instructions"""


def print_code_block(ir: IRGenerator):
    for index, code in enumerate(ir.sstack[:400]):
        if not code:
            break
        print(
            f"{index}:\t\t{'\t|\t'.join(map(lambda x: '-' if x is None else str(x),code))}"
        )
        print("\t  " + "- " * 32)


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

    assert len(ir.stack) == 0, "Error: Stack is not empty"

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


def main():
    if len(sys.argv) == 3:
        command = sys.argv[1]
        file_name = sys.argv[2]
    elif len(sys.argv) == 2:
        command = "run"
        file_name = sys.argv[1]
    else:
        print(USAGE_HELP_MESSGE)
        exit(-1)
    file_str = read_file(file_name)

    try:
        match command:
            case "build":
                ir = generate_code(file_str)
                print_code_block(ir)
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
        pos = re.findall(r"at position (\d+)", message)
        if not len(pos):
            raise e
        [pos] = pos
        line_info = find_error_line(file_str, int(pos))
        if not (line_info):
            raise e
        line, row = line_info
        message = message.replace(pos, f"#{line}:{row}")

        e.args = (message,)

        raise e


if __name__ == "__main__":
    main()
