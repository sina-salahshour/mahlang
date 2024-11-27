from compiler.ir_generator import IRGenerator
from compiler.lexer import Token


def register_actions(ir: IRGenerator):
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
        addr = ir.variable_table.get(current_token.literal)
        if addr is None:
            raise NameError(f"Undefined variable '{current_token.literal}'")
        ir.stack.append(addr)

    @ir.action("setpid")
    def _(current_token: Token):
        addr = ir.declare_variable(current_token.literal)
        ir.stack.append(addr)

    @ir.action("assign")
    def _(_: Token):
        src = ir.stack.pop()
        dest = ir.stack.pop()

        code = ("=", src, None, dest)
        ir.write_code(code)

    @ir.action("num")
    def _(current_token: Token):
        tmp = ir.get_temp_address()
        ir.stack.append(tmp)

        code = ("ld", int(current_token.literal), None, tmp)
        ir.write_code(code)

    @ir.action("save")
    def _(_: Token):
        addr = ir.write_code((None, None, None, None))
        ir.stack.append(addr)

    @ir.action("jmpfalse")
    def _(_: Token):
        addr: int = ir.stack.pop()
        cond_addr = ir.stack.pop()

        code = ("jmpf", cond_addr, None, ir.code_pointer + 1)

        ir.write_code(code, addr)

    @ir.action("jmp")
    def _(_: Token):
        addr: int = ir.stack.pop()

        code = ("jmp", None, None, ir.code_pointer)

        ir.write_code(code, addr)

    @ir.action("omit")
    def _(_: Token):
        ir.stack.pop()

    @ir.action("jmpwhile")
    def _(_: Token):
        post_condition_addr = ir.stack.pop()
        cond_addr = ir.stack.pop()
        pre_cond_addr_2 = ir.stack.pop()
        pre_cond_addr_1 = ir.stack.pop()

        code = ("jmpf", cond_addr, None, ir.code_pointer + 1)
        ir.write_code(code, post_condition_addr)

        code = ("jmp", None, None, ir.code_pointer + 1)
        ir.write_code(code, pre_cond_addr_2)

        code = ("jmp", None, None, pre_cond_addr_2 + 1)
        ir.write_code(code, pre_cond_addr_1)

        code = ("jmp", None, None, pre_cond_addr_2 + 1)
        ir.write_code(code)

    @ir.action("break")
    def _(_: Token):
        pre_cond_addr_2 = ir.stack[-3]
        code = ("jmp", None, None, pre_cond_addr_2)
        ir.write_code(code)

    @ir.action("continue")
    def _(_: Token):
        pre_cond_addr_1 = ir.stack[-4]
        code = ("jmp", None, None, pre_cond_addr_1)
        ir.write_code(code)

    @ir.action("nop")
    def _(_: Token):
        code = (None, None, None, None)
        ir.write_code(code)
