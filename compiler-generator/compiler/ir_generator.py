from typing import Callable

from .lexer import Token
from .parser import Parser


class IRGenerator:
    def __init__(self, parser: Parser) -> None:
        self.sstack = [None] * 1000
        self.tmp_pointer = 600
        self.variable_pointer = 400
        self.code_pointer = 0
        self.parser = parser
        self.stack = []

        # self defined
        self.scope_stack = []
        self.scope_stack.append({"variables": {}})

    def get_temp_address(self):
        tmp_address = self.tmp_pointer
        self.tmp_pointer += 1
        return tmp_address

    def get_variable_address(self, name: str):
        for scope in self.scope_stack[::-1]:
            addr = scope["variables"].get(name)
            if addr is not None:
                return addr

    def get_variable_address_in_scope(self, name: str):
        scope = self.scope_stack[-1]
        addr = scope["variables"].get(name)
        return addr

    def declare_variable(self, name, address=None):
        current_scope = self.scope_stack[-1]
        addr = address if address is not None else self.variable_pointer
        if address is None:
            self.variable_pointer += 1
        current_scope["variables"][name] = addr
        return addr

    def push_scope(self):
        self.scope_stack.append({"variables": {}})

    def pop_scope(self):
        self.scope_stack.pop()

    def write_code(self, code, address=None):
        addr = address or self.code_pointer
        if address is None:
            if self.code_pointer >= 400:
                raise RuntimeError("CodeBlock is full")
            self.sstack[addr] = code
            self.code_pointer += 1
        else:
            self.sstack[address] = code
        return addr

    def action(self, name: str):
        def action_fn(fn: Callable[[Token], None]):
            self.parser.register_actions(f"@{name}", fn)

        return action_fn

    def generate(self):
        self.parser.parse()
