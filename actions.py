from decimal import Decimal

from compiler.ir_generator import IRGenerator
from compiler.lexer import Token


def register_actions(ir: IRGenerator):
    def assert_not_function(value):
        match value:
            case {"name": name}:
                raise Exception(f"tried to use function '{name}' as variable.")

    def operator(op: str):
        lhs = ir.stack.pop()
        rhs = ir.stack.pop()
        for item in (lhs, rhs):
            assert_not_function(item)

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

    @ir.action("pow")
    def _(_: Token):
        operator("**")

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

    @ir.action("truediv")
    def _(_: Token):
        operator("//")

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

    @ir.action("init_fn_def")
    def _(current_token: Token):
        tmp = ir.get_variable_address_in_scope(current_token.literal)
        if tmp is not None:
            raise NameError(
                f"Error at position {current_token.position}: a function or variable is already defined with name {current_token.literal}"
            )
        return_address = ir.get_temp_address()
        ir.stack.append(
            ["function_def", current_token.literal, return_address, ir.code_pointer]
        )

    @ir.action("save_fn_def")
    def _(_: Token):
        args = []

        while True:
            current_stack_item = ir.stack.pop()

            match current_stack_item:
                case ["function_def", function_name, return_address, code_address]:
                    break
                case item:
                    args.append(item)

        tmp = ir.pop_scope()
        ir.declare_function(function_name, args, code_address, return_address)
        ir.push_scope(tmp)

    @ir.action("ret")
    def _(_: Token):
        for stack_item in ir.stack[::-1]:
            match stack_item:
                case ["function_def", _, return_address, _]:
                    break
        else:
            raise Exception("return keyword used outside function")

        tmp = ir.stack.pop()
        code = ("=", tmp, None, return_address)
        ir.write_code(code)

        code = ("ret", None, None, None)
        ir.write_code(code)

    @ir.action("fn_ret_inject_zero")
    def _(_: Token):
        tmp = ir.get_temp_address()
        code = ("ld", 0, None, tmp)
        ir.write_code(code)
        ir.stack.append(tmp)

    @ir.action("call")
    def _(_: Token):
        arg_list = []
        while True:
            current_stack_item = ir.stack.pop()

            match current_stack_item:
                case ["function_arg_stack_base", function]:
                    break
                case item:
                    arg_list.append(item)
        if function is None or isinstance(function, int):
            raise NameError(f"Tried to call a non function.")
        if len(arg_list) != len(function["args"]):
            raise Exception(
                f"Argument Count is invalid. '{function['name']}' accepts {len(function['args'])} arguments but {len(arg_list)} was given at position {_.position - 1}"
            )

        for arg, arg_addr in zip(arg_list, function["args"]):
            code = ("=", arg, None, arg_addr)
            ir.write_code(code)
        code = ("call", None, None, function["address"])
        ir.write_code(code)

    @ir.action("callwithvalue")
    def _(_: Token):
        arg_list = []
        while True:
            current_stack_item = ir.stack.pop()

            match current_stack_item:
                case ["function_arg_stack_base", function]:
                    break
                case item:
                    arg_list.append(item)
        if function is None or isinstance(function, int):
            raise NameError(f"Tried to call a non function.")
        if len(arg_list) != len(function["args"]):
            raise Exception(
                f"Argument Count is invalid. '{function['name']}' accepts {len(function['args'])} arguments but {len(arg_list)} was given at position {_.position - 1}"
            )

        for arg, arg_addr in zip(arg_list, function["args"]):
            code = ("=", arg, None, arg_addr)
            ir.write_code(code)
        code = ("call", None, None, function["address"])
        ir.write_code(code)
        ir.stack.append(function["return_address"])

    @ir.action("assign")
    def _(_: Token):
        src = ir.stack.pop()
        dest = ir.stack.pop()

        for item in (src, dest):
            assert_not_function(item)

        code = ("=", src, None, dest)
        ir.write_code(code)

    @ir.action("negate")
    def _(_: Token):
        src = ir.stack.pop()
        tmp = ir.get_temp_address()

        assert_not_function(src)

        code = ("neg", src, None, tmp)
        ir.write_code(code)
        ir.stack.append(tmp)

    @ir.action("num")
    def _(current_token: Token):
        tmp = ir.get_temp_address()
        ir.stack.append(tmp)

        code = ("ld", Decimal(current_token.literal), None, tmp)
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

    @ir.action("init_call_local")
    def _(_: Token):
        function_addr = ir.stack.pop()

        ir.stack.append(["function_arg_stack_base", function_addr])

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
            assert_not_function(arg)
            code = (function_name, arg, None, None)
            ir.write_code(code)

    def builtin_single_arg_function(name: str):
        arg_list = []
        while True:
            current_stack_item = ir.stack.pop()

            match current_stack_item:
                case ["function_arg_stack_base", function_name]:
                    break
                case item:
                    arg_list.append(item)

        if len(arg_list) > 1:
            raise SyntaxError(f"'{name}' can only have one argument")

        [arg] = arg_list
        assert_not_function(arg)
        tmp = ir.get_temp_address()

        code = (function_name, arg, None, tmp)
        ir.write_code(code)
        ir.stack.append(tmp)

    @ir.action("sin")
    def _(_: Token):
        builtin_single_arg_function("sin")

    @ir.action("cos")
    def _(_: Token):
        builtin_single_arg_function("cos")

    @ir.action("input")
    def _(current_token: Token):
        arg_list = []
        while True:
            current_stack_item = ir.stack.pop()

            match current_stack_item:
                case ["function_arg_stack_base", function_name]:
                    break
                case item:
                    arg_list.append(item)

        if len(arg_list):
            raise Exception(
                f"Error at position {current_token.position}: input command doesn't take any arguments."
            )
        tmp = ir.get_temp_address()

        code = (function_name, None, None, tmp)
        ir.write_code(code)
        ir.stack.append(tmp)
