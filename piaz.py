from compiler.lexer import Lexer
from compiler.parser import Parser
from compiler.ir_generator import IRGenerator
from actions import register_actions
import sys
from code_interpreter import run_code


def print_code_block(ir: IRGenerator):
    for index, code in enumerate(ir.sstack[:400]):
        if not code:
            break
        print(
            f"{index}:\t\t{'\t|\t'.join(map(lambda x: '-' if x is None else str(x),code))}"
        )
        print("\t  " + "- " * 32)


def generate_code(file_name: str):
    with open(file_name) as f:
        input_str = f.read()

    lexer = Lexer(input_str)
    parser = Parser(lexer)
    ir = IRGenerator(parser)

    register_actions(ir)

    ir.generate()

    assert len(ir.stack) == 0, "Error Stack is not empty"

    return ir


def main():
    file_name = sys.argv[1]

    ir = generate_code(file_name)
    match sys.argv[2]:
        case "build":
            print_code_block(ir)
        case "run":
            run_code(ir.sstack[:400])


if __name__ == "__main__":
    main()
