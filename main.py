from compiler.lexer import Lexer, Token
from compiler.parser import Parser
from compiler.ir_generator import IRGenerator
import compiler

with open("./input.math") as f:
    input_str = f.read()

lexer = Lexer(input_str)
parser = Parser(lexer)
ir = IRGenerator(parser)


@ir.action("add")
def _(current_token: Token):
    print("action add", current_token)


@ir.action("mul")
def _(current_token: Token):
    print("action mul", current_token)


@ir.action("pid")
def _(current_token: Token):
    print("action pid", current_token)


ir.generate()
