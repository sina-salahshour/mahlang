#!/usr/bin/python
from compiler.lexer import Lexer
from compiler.parser import Parser
from compiler.ir_generator import IRGenerator
from actions import register_actions
import sys
from code_interpreter import run_code

# sys.tracebacklimit = 0

USAGE_HELP_MESSGE = """Usage:
    pz run\t <input file>\t\t# to run file
    pz build\t <input file>\t\t# to see the program instructions"""


def print_code_block(ir: IRGenerator):
    for index, code in enumerate(ir.sstack[:400]):
        if not code:
            break
        print(
            f"{index}:\t\t{'\t|\t'.join(map(lambda x: '-' if x is None else str(x),code))}"
        )
        print("\t  " + "- " * 32)


def generate_code(file_name: str):
    try:
        with open(file_name) as f:
            input_str = f.read()
    except FileNotFoundError:
        print(f"Error: file not found '{file_name}'\n")
        print(USAGE_HELP_MESSGE)
        exit(-3)

    lexer = Lexer(input_str)
    parser = Parser(lexer)
    ir = IRGenerator(parser)

    register_actions(ir)

    ir.generate()

    assert len(ir.stack) == 0, "Error: Stack is not empty"

    return ir


def main():
    if len(sys.argv) != 3:
        print(USAGE_HELP_MESSGE)
        exit(-1)
    file_name = sys.argv[2]

    match sys.argv[1]:
        case "build":
            ir = generate_code(file_name)
            print_code_block(ir)
        case "run":
            ir = generate_code(file_name)
            run_code(ir.sstack[:400])
        case unknown_command:
            print(
                f"Error: command not found '{unknown_command}'.\navailable commands are 'build' and 'run'\n"
            )
            print(USAGE_HELP_MESSGE)
            exit(-2)


if __name__ == "__main__":
    main()
