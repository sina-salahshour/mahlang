import re
from dataclasses import dataclass
from enum import Enum


class TokenString(str):
    def __eq__(self, pattern):
        return re.fullmatch(pattern, self) is not None


class TokenType(Enum):
{% TOKEN_TYPES %}

    # --- internals

    EOF = "EOF"  # internal token that don't need rule

    def __str__(self) -> str:
        return self.name


INTERNAL_TOKEN_TYPE_COUNT = 1

TOKEN_RULES = {
{% TOKEN_RULES %}
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
{% TOKEN_SEPARATORS %}
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
                return Token(type, char, start_position)
        else:
            raise SyntaxError(f"Invalid token at position {start_position}: '{char}'")
