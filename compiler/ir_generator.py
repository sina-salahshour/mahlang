from typing import Callable
from .parser import Parser
from .lexer import Token


class IRGenerator:
    def __init__(self, parser: Parser) -> None:
        self.sstack = [None] * 1000
        self.tmp_pointer = 600
        self.variable_pointer = 400
        self.code_pointer = 0
        self.parser = parser
        self.stack = []

        # self defined
        self.variable_table = {}

    def get_temp_address(self):
        tmp_address = self.tmp_pointer
        self.tmp_pointer += 1
        return tmp_address

    def get_variable_address(self, name: str):
        return self.variable_table[name]

    def declare_variable(self, name):
        variable_address = self.variable_pointer
        self.variable_pointer += 1
        self.variable_table[name] = variable_address
        return variable_address

    def write_code(self, code, address=None):
        if not address:
            self.sstack[self.code_pointer] = code
            self.code_pointer += 1
        else:
            self.sstack[address] = code

    def action(self, name: str):
        def action_fn(fn: Callable[[Token], None]):
            self.parser.register_actions(f"@{name}", fn)

        return action_fn

    def generate(self):
        self.parser.parse()
