from compiler.ir_generator import IRGenerator
from compiler.lexer import Token


def register_actions(ir: IRGenerator):
    def operator(op: str):
        lhs = ir.stack.pop()
        rhs = ir.stack.pop()
        tmp = ir.get_temp_address()
        ir.stack.append(tmp)

        code = (op, lhs, rhs, tmp)
        ir.write_code(code)

    @ir.action("add")
    def _(_: Token):
        operator("+")

    @ir.action("sub")
    def _(_: Token):
        operator("-")

    @ir.action("eq")
    def _(_: Token):
        operator("eq")

    @ir.action("lt")
    def _(_: Token):
        operator("lt")

    @ir.action("gt")
    def _(_: Token):
        operator("gt")

    @ir.action("and")
    def _(_: Token):
        operator("and")

    @ir.action("or")
    def _(_: Token):
        operator("or")

    @ir.action("neq")
    def _(_: Token):
        operator("neq")

    @ir.action("mul")
    def _(_: Token):
        operator("*")

    @ir.action("div")
    def _(_: Token):
        operator("/")

    @ir.action("mod")
    def _(_: Token):
        operator("%")

    @ir.action("pid")
    def _(current_token: Token):
        addr = ir.get_variable_address(current_token.literal)
        if addr is None:
            raise NameError(
                f"Undefined variable '{current_token.literal}' at position {current_token.position}"
            )
        ir.stack.append(addr)

    @ir.action("setpid")
    def _(current_token: Token):
        tmp = ir.get_variable_address_in_scope(current_token.literal)
        if tmp is not None:
            raise NameError(
                f"Error at position {current_token.position}: variable is already defined {current_token.literal}"
            )
        addr = ir.declare_variable(current_token.literal)
        ir.stack.append(addr)

    @ir.action("savefn")
    def _(current_token: Token):
        tmp = ir.get_variable_address_in_scope(current_token.literal)
        if tmp is not None:
            raise NameError(
                f"Error at position {current_token.position}: function is already defined {current_token.literal}"
            )
        ir.declare_variable(current_token.literal, ir.code_pointer)

    @ir.action("ret")
    def _(_: Token):
        code = ("ret", None, None, None)
        ir.write_code(code)

    @ir.action("call")
    def _(current_token: Token):
        arg_list = []
        while True:
            current_stack_item = ir.stack.pop()

            match current_stack_item:
                case ["function_arg_stack_base", function_name]:
                    break
                case item:
                    arg_list.append(item)

        # ignore args for now
        if len(arg_list):
            raise Exception(
                f"Error at position {current_token.position}: Args are not supported for now."
            )

        addr = ir.get_variable_address(function_name)
        if addr is None:
            raise NameError(
                f"Undefined function '{current_token.literal}' at position {current_token.position}"
            )
        code = ("call", None, None, addr)
        ir.write_code(code)

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

    @ir.action("savewhile")
    def _(_: Token):
        addr = ir.write_code(("while", None, None, None))
        ir.stack.append(addr)

    @ir.action("freeze")
    def _(_: Token):
        addr = ir.stack.pop()
        tmp = ir.get_temp_address()

        code = ("=", addr, None, tmp)
        ir.write_code(code)

        ir.stack.append(tmp)

    @ir.action("jmpfalse")
    def _(_: Token):
        addr: int = ir.stack.pop()
        cond_addr = ir.stack[-1]

        code = ("jmpf", cond_addr, None, ir.code_pointer)

        ir.write_code(code, addr)

    @ir.action("jmptrue")
    def _(_: Token):
        addr: int = ir.stack.pop()
        cond_addr = ir.stack[-1]

        code = ("jmpt", cond_addr, None, ir.code_pointer)

        ir.write_code(code, addr)

    @ir.action("jmp")
    def _(_: Token):
        addr: int = ir.stack.pop()

        code = ("jmp", None, None, ir.code_pointer)

        ir.write_code(code, addr)

    @ir.action("omit")
    def _(_: Token):
        ir.stack.pop()

    @ir.action("scopestart")
    def _(_: Token):
        ir.push_scope()

    @ir.action("scopeend")
    def _(_: Token):
        ir.pop_scope()

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
        save_counter = 1
        for addr in ir.stack[::-1]:
            if isinstance(ir.sstack[addr], tuple) and ir.sstack[addr][0] == "while":
                if save_counter == 1:
                    code = ("jmp", None, None, addr)
                    break
                else:
                    save_counter += 1
        else:
            raise Exception("Error while backpatching while loop")
        ir.write_code(code)

    @ir.action("continue")
    def _(_: Token):
        save_counter = 1
        for addr in ir.stack[::-1]:
            if isinstance(ir.sstack[addr], tuple) and ir.sstack[addr][0] == "while":
                if save_counter == 2:
                    code = ("jmp", None, None, addr)
                    break
                else:
                    save_counter += 1
        else:
            raise Exception("Error while backpatching while loop")
        ir.write_code(code)

    @ir.action("nop")
    def _(_: Token):
        code = (None, None, None, None)
        ir.write_code(code)

    @ir.action("init_call")
    def _(current_token: Token):
        function_name = current_token.literal

        ir.stack.append(["function_arg_stack_base", function_name])

    @ir.action("print")
    def _(_: Token):
        arg_list = []
        while True:
            current_stack_item = ir.stack.pop()

            match current_stack_item:
                case ["function_arg_stack_base", function_name]:
                    break
                case item:
                    arg_list.append(item)

        for arg in arg_list[::-1]:
            code = (function_name, arg, None, None)
            ir.write_code(code)
