import re
from dataclasses import dataclass
from enum import Enum


class TokenString(str):
    def __eq__(self, pattern):
        return re.fullmatch(pattern, self) is not None
    def __hash__(self) -> int:
        return super().__hash__()


class TokenType(Enum):
    PAREN_OPEN = 'PAREN_OPEN'
    PAREN_CLOSE = 'PAREN_CLOSE'
    OP_ADD = 'OP_ADD'
    OP_SUB = 'OP_SUB'
    OP_DIV = 'OP_DIV'
    OP_MUL = 'OP_MUL'
    EQ = 'EQ'
    ASSIGN = 'ASSIGN'
    BRACE_OPEN = 'BRACE_OPEN'
    BRACE_CLOSE = 'BRACE_CLOSE'
    IF = 'IF'
    ELIF = 'ELIF'
    ELSE = 'ELSE'
    WHILE = 'WHILE'
    BREAK = 'BREAK'
    CONTINUE = 'CONTINUE'
    IDENTIFIER = 'IDENTIFIER'
    NUMBER = 'NUMBER'
    COLON = 'COLON'

    # --- internals

    EOF = "EOF"  # internal token that don't need rule

    def __str__(self) -> str:
        return self.name


INTERNAL_TOKEN_TYPE_COUNT = 1

TOKEN_RULES = {
    TokenType.PAREN_OPEN : r"[(]",
    TokenType.PAREN_CLOSE : r"[)]",
    TokenType.OP_ADD : r"[+]",
    TokenType.OP_SUB : r"-",
    TokenType.OP_DIV : r"/",
    TokenType.OP_MUL : r"[*]",
    TokenType.EQ : r"[=]{2}",
    TokenType.ASSIGN : r":[=]",
    TokenType.BRACE_OPEN : r"[{]",
    TokenType.BRACE_CLOSE : r"[}]",
    TokenType.IF : r"if",
    TokenType.ELIF : r"elif",
    TokenType.ELSE : r"else",
    TokenType.WHILE : r"while",
    TokenType.BREAK : r"break",
    TokenType.CONTINUE : r"continue",
    TokenType.IDENTIFIER : r"[a-zA-Z_]\w*",
    TokenType.NUMBER : r"\d+",
    TokenType.COLON : r":",
}

assert (
    len(TOKEN_RULES) == len(TokenType) - INTERNAL_TOKEN_TYPE_COUNT
), "Error specify rules for all tokens"


@dataclass
class Token:
    type: TokenType
    literal: str
    position: int

    def __str__(self) -> str:
        return f"<{self.type},'{self.literal}'>"

    def __eq__(self, value: object, /) -> bool:
        if isinstance(value, TokenType):
            return self.type == value
        if not isinstance(value, Token):
            return False
        return self.type == value.type


RULE_SEPARATORS = [
    TOKEN_RULES[TokenType.PAREN_OPEN],
    TOKEN_RULES[TokenType.PAREN_CLOSE],
    TOKEN_RULES[TokenType.OP_ADD],
    TOKEN_RULES[TokenType.OP_SUB],
    TOKEN_RULES[TokenType.OP_DIV],
    TOKEN_RULES[TokenType.OP_MUL],
    TOKEN_RULES[TokenType.EQ],
    TOKEN_RULES[TokenType.ASSIGN],
    TOKEN_RULES[TokenType.BRACE_OPEN],
    TOKEN_RULES[TokenType.BRACE_CLOSE],
    TOKEN_RULES[TokenType.COLON],
]


class Lexer:
    rules = TOKEN_RULES

    def __init__(self, input_str: str) -> None:
        self.input_str = input_str
        self.position = 0

    def skip_whitespaces(self):
        while (
            self.position < len(self.input_str)
            and self.input_str[self.position].isspace()
        ):
            self.position += 1

    def peek_char(self):
        current_char = self.input_str[self.position]
        return current_char

    def read_char(self):
        current_char = self.peek_char()
        self.position += 1
        return current_char

    def get_next_token(self):
        self.skip_whitespaces()
        if len(self.input_str) == self.position:
            return Token(TokenType.EOF, "$", self.position)
        start_position = self.position
        char = self.read_char()
        while True:
            next_char = self.peek_char()
            if (
                next_char.isspace()
                or (
                    TokenString(char) in RULE_SEPARATORS
                    and TokenString(char + next_char) not in RULE_SEPARATORS
                )
                or (
                    TokenString(char) not in RULE_SEPARATORS
                    and TokenString(next_char) in RULE_SEPARATORS
                )
            ):
                break
            char += self.read_char()

        char = TokenString(char)

        for type, pattern in self.rules.items():
            if char == pattern:
                return Token(type, str(char), start_position)
        else:
            raise SyntaxError(f"Invalid token at position {start_position}: '{char}'")
