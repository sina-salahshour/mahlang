from compiler.lexer import Lexer
from compiler.parser import Parser
from compiler.ir_generator import IRGenerator
from actions import register_actions
import sys


def print_code_block(ir: IRGenerator):
    for index, code in enumerate(ir.sstack[:400]):
        if not code:
            break
        print(
            f"{index}:\t\t{'\t|\t'.join(map(lambda x: '-' if x is None else str(x),code))}"
        )
        print("\t  " + "- " * 32)


def main():
    file_name = sys.argv[1]
    with open(file_name) as f:
        input_str = f.read()

    lexer = Lexer(input_str)
    parser = Parser(lexer)
    ir = IRGenerator(parser)

    register_actions(ir)

    ir.generate()

    print(ir.stack)
    assert len(ir.stack) == 0, "Error Stack is not empty"

    print_code_block(ir)


if __name__ == "__main__":
    main()
