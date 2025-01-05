import re
from dataclasses import dataclass
from enum import Enum


class TokenString(str):
    def __eq__(self, pattern):
        return re.fullmatch(pattern, self) is not None
    def __hash__(self) -> int:
        return super().__hash__()


class TokenType(Enum):
    STRING = 'STRING'
    TOKEN_IGNORE_FLAG = 'TOKEN_IGNORE_FLAG'
    EPSILON = 'EPSILON'
    SEP = 'SEP'
    TRANSITION = 'TRANSITION'
    PIPE = 'PIPE'
    TOKEN_NAME = 'TOKEN_NAME'
    ID = 'ID'
    COLON = 'COLON'
    ACTION = 'ACTION'
    SEMICOLON = 'SEMICOLON'

    # --- internals

    EOF = "EOF"  # internal token that don't need rule

    def __str__(self) -> str:
        return self.name


INTERNAL_TOKEN_TYPE_COUNT = 1

TOKEN_RULES = {
    TokenType.STRING : r"\"([^\"\\]|\\.)*\"",
    TokenType.TOKEN_IGNORE_FLAG : r"!",
    TokenType.EPSILON : r"#e",
    TokenType.SEP : r"--",
    TokenType.TRANSITION : r"->",
    TokenType.PIPE : r"\|",
    TokenType.TOKEN_NAME : r"#[a-zA-Z_]\w*",
    TokenType.ID : r"[a-zA-Z_](?:\w|')*",
    TokenType.COLON : r":",
    TokenType.ACTION : r"@[a-zA-Z_]\w*",
    TokenType.SEMICOLON : r";",
}


assert (
    len(TOKEN_RULES) == len(TokenType) - INTERNAL_TOKEN_TYPE_COUNT
), "Error: specify rules for all tokens"


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


IGNORED_TOKENS = [
    
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

    def _get_next_token(self):
        self.skip_whitespaces()
        if len(self.input_str) == self.position:
            return Token(TokenType.EOF, "$", self.position)
        remaining_input = self.input_str[self.position :]
        start_position = self.position
        for type, pattern in self.rules.items():
            res = re.match(pattern, remaining_input)
            if res:
                match_end = res.span()[1]
                if match_end is not None:
                    self.position += match_end
                    return Token(type, str(remaining_input[:match_end]), start_position)
        else:
            raise SyntaxError(
                f"Invalid token at position {start_position}: '{remaining_input[0]}'"
            )

    def get_next_token(self):
        while True:
            token = self._get_next_token()
            if token.type not in IGNORED_TOKENS:
                return token
