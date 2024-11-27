from compiler.lexer import Lexer, Token
from compiler.parser import Parser
from compiler.ir_generator import IRGenerator

with open("./input.math") as f:
    input_str = f.read()

lexer = Lexer(input_str)
parser = Parser(lexer)
ir = IRGenerator(parser)


@ir.action("add")
def _(_: Token):
    lhs = ir.stack.pop()
    rhs = ir.stack.pop()
    tmp = ir.get_temp_address()
    ir.stack.append(tmp)

    code = ("+", lhs, rhs, tmp)
    ir.write_code(code)


@ir.action("mul")
def _(_: Token):
    lhs = ir.stack.pop()
    rhs = ir.stack.pop()
    tmp = ir.get_temp_address()
    ir.stack.append(tmp)

    code = ("*", lhs, rhs, tmp)
    ir.write_code(code)


@ir.action("pid")
def _(current_token: Token):
    addr = ir.variable_table[current_token.literal]
    ir.stack.append(addr)


@ir.action("setpid")
def _(current_token: Token):
    addr = ir.declare_variable(current_token.literal)
    ir.stack.append(addr)


@ir.action("assign")
def _(_: Token):
    src = ir.stack.pop()
    dest = ir.stack.pop()

    code = (":=", src, dest)
    ir.write_code(code)


@ir.action("num")
def _(current_token: Token):
    tmp = ir.get_temp_address()
    ir.stack.append(tmp)

    code = ("ld", int(current_token.literal), tmp)
    ir.write_code(code)


ir.generate()

for code in ir.sstack[:400]:
    if not code:
        break
    print(code)
