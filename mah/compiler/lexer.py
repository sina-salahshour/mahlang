"""Hand-written lexer for Mah (replaces the LL(1)-generator's output --
see docs/V2_DESIGN.md's M0 milestone and docs/GRAMMAR_DSL.md, which
describes the now-retired mah.lang/compiler-generator pipeline this
replaces).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TokenType(Enum):
    # punctuation
    SEMICOLON = ";"
    BRACE_OPEN = "{"
    BRACE_CLOSE = "}"
    BRACKET_OPEN = "["
    BRACKET_CLOSE = "]"
    PAREN_OPEN = "("
    PAREN_CLOSE = ")"
    COMMA = ","
    ASSIGN = "="
    DOT = "."
    COLON = ":"
    # M17: `..`/`..=` -- range expressions/patterns. Checked before the
    # single `.` (and `..=` before `..`) in `get_next_token`.
    DOTDOT = ".."
    DOTDOT_EQ = "..="
    # operators
    ADD = "+"
    SUB = "-"
    MUL = "*"
    POW = "**"
    DIV = "/"
    TRUEDIV = "//"
    MOD = "%"
    EQ = "=="
    NEQ = "!="
    FAT_ARROW = "=>"
    LT = "<"
    GT = ">"
    LE = "<="
    GE = ">="
    BANG = "!"
    AND = "&"
    OR = "|"
    # keywords
    LET = "let"
    PRINT = "print"
    INPUT = "input"
    SIN = "sin"
    COS = "cos"
    IF = "if"
    ELIF = "elif"
    ELSE = "else"
    WHILE = "while"
    BREAK = "break"
    CONTINUE = "continue"
    RETURN = "return"
    DEFER = "defer"
    DETACH = "detach"
    SLEEP_ASYNC = "sleep_async"
    FN = "fn"
    STRUCT = "struct"
    ENUM = "enum"
    MATCH = "match"
    SOME = "some"
    NONE = "none"
    TRUE = "true"
    FALSE = "false"
    TRAIT = "trait"
    IMPL = "impl"
    FOR = "for"
    IN = "in"
    # literals / identifiers
    STRING = "STRING"
    NUMBER = "NUMBER"
    ID = "ID"
    # internal
    EOF = "EOF"

    def __str__(self) -> str:
        return self.name


KEYWORDS = {
    "let": TokenType.LET,
    "print": TokenType.PRINT,
    "input": TokenType.INPUT,
    "sin": TokenType.SIN,
    "cos": TokenType.COS,
    "if": TokenType.IF,
    "elif": TokenType.ELIF,
    "else": TokenType.ELSE,
    "while": TokenType.WHILE,
    "break": TokenType.BREAK,
    "continue": TokenType.CONTINUE,
    "return": TokenType.RETURN,
    "defer": TokenType.DEFER,
    "detach": TokenType.DETACH,
    "sleep_async": TokenType.SLEEP_ASYNC,
    "fn": TokenType.FN,
    "struct": TokenType.STRUCT,
    "enum": TokenType.ENUM,
    "match": TokenType.MATCH,
    "some": TokenType.SOME,
    "none": TokenType.NONE,
    "true": TokenType.TRUE,
    "false": TokenType.FALSE,
    "trait": TokenType.TRAIT,
    "impl": TokenType.IMPL,
    "for": TokenType.FOR,
    "in": TokenType.IN,
}

_SINGLE_CHAR = {
    ";": TokenType.SEMICOLON,
    "{": TokenType.BRACE_OPEN,
    "}": TokenType.BRACE_CLOSE,
    "[": TokenType.BRACKET_OPEN,
    "]": TokenType.BRACKET_CLOSE,
    "(": TokenType.PAREN_OPEN,
    ")": TokenType.PAREN_CLOSE,
    ",": TokenType.COMMA,
    ".": TokenType.DOT,
    ":": TokenType.COLON,
    "+": TokenType.ADD,
    "-": TokenType.SUB,
    "*": TokenType.MUL,
    "/": TokenType.DIV,
    "%": TokenType.MOD,
    "=": TokenType.ASSIGN,
    "<": TokenType.LT,
    ">": TokenType.GT,
    "!": TokenType.BANG,
    "&": TokenType.AND,
    "|": TokenType.OR,
}

_TWO_CHAR = {
    "**": TokenType.POW,
    "//": TokenType.TRUEDIV,
    "==": TokenType.EQ,
    "!=": TokenType.NEQ,
    "=>": TokenType.FAT_ARROW,
    "<=": TokenType.LE,
    ">=": TokenType.GE,
}


@dataclass
class Token:
    type: TokenType
    literal: str
    position: int

    def __str__(self) -> str:
        return f"<{self.type},'{self.literal}'>"


class Lexer:
    def __init__(self, input_str: str) -> None:
        self.input_str = input_str
        self.position = 0

    def _skip_trivia(self) -> None:
        text = self.input_str
        n = len(text)
        while self.position < n:
            ch = text[self.position]
            if ch.isspace():
                self.position += 1
            elif ch == "#":
                while self.position < n and text[self.position] != "\n":
                    self.position += 1
            else:
                break

    def peek_token(self) -> Token:
        """M16: look at the next token without consuming it -- used by the
        parser to decide whether an argument-list item is a keyword
        argument (`ID COLON`) or an ordinary positional expression (which
        may itself start with an `ID`, e.g. a bare variable reference or a
        struct literal)."""
        saved_position = self.position
        tok = self.get_next_token()
        self.position = saved_position
        return tok

    def get_next_token(self) -> Token:
        self._skip_trivia()
        text = self.input_str
        n = len(text)
        start = self.position
        if start >= n:
            return Token(TokenType.EOF, "$", start)

        ch = text[start]

        if ch.isdigit():
            end = start
            while end < n and text[end].isdigit():
                end += 1
            if end < n and text[end] == "." and end + 1 < n and text[end + 1].isdigit():
                end += 1
                while end < n and text[end].isdigit():
                    end += 1
            self.position = end
            return Token(TokenType.NUMBER, text[start:end], start)

        if ch == '"':
            end = start + 1
            while end < n and text[end] != '"':
                if text[end] == "\\" and end + 1 < n:
                    end += 2
                else:
                    end += 1
            if end >= n:
                raise SyntaxError(f"Unterminated string literal at position {start}")
            end += 1  # consume closing quote
            self.position = end
            return Token(TokenType.STRING, text[start:end], start)

        if ch.isalpha() or ch in "_$":
            end = start
            while end < n and (text[end].isalnum() or text[end] in "_$"):
                end += 1
            self.position = end
            literal = text[start:end]
            return Token(KEYWORDS.get(literal, TokenType.ID), literal, start)

        # M17: `..=` (three chars) before `..` (two) before a plain `.`
        # (one) -- the number rule above already stops before either (a
        # digit run never continues into a second `.`), so `1..5` still
        # lexes as NUMBER `1`, DOTDOT, NUMBER `5`.
        three = text[start : start + 3]
        if three == "..=":
            self.position += 3
            return Token(TokenType.DOTDOT_EQ, three, start)

        two = text[start : start + 2]
        if two == "..":
            self.position += 2
            return Token(TokenType.DOTDOT, two, start)
        if two in _TWO_CHAR:
            self.position += 2
            return Token(_TWO_CHAR[two], two, start)

        if ch in _SINGLE_CHAR:
            self.position += 1
            return Token(_SINGLE_CHAR[ch], ch, start)

        raise SyntaxError(f"Invalid token at position {start}: '{ch}'")
