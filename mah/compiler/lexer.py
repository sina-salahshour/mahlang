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
    # M41a: `...` -- a spread argument in a call's argument list
    # (`f(...xs)`). Checked before `..=`/`..` in `get_next_token`.
    ELLIPSIS = "..."
    # M21: `->` -- function return types / fn-type in type position (see
    # docs/TYPES.md). Nothing valid in Mah wrote `->` before this, so no
    # existing program's meaning changes.
    ARROW = "->"
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
    # M25: `try`/`throw` are reserved keywords (docs/ERRORS.md). `catch`,
    # `throws`, and `never` stay ordinary `ID` tokens (contextual --
    # matched by `literal` in the parser) so they remain usable as
    # identifiers everywhere else.
    TRY = "try"
    THROW = "throw"
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
    "try": TokenType.TRY,
    "throw": TokenType.THROW,
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
    "->": TokenType.ARROW,
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
        # M41a (docs/REFLECTION.md, "Doc comments"): the full-line `##`
        # comments seen so far, by the position of their `##` -- a side
        # table, never tokens, so the parser's token stream is unchanged.
        # `doc_above` reads it.
        self.doc_comments: dict[int, str] = {}

    def _skip_trivia(self) -> None:
        text = self.input_str
        n = len(text)
        while self.position < n:
            ch = text[self.position]
            if ch.isspace():
                self.position += 1
            elif ch == "#":
                start = self.position
                while self.position < n and text[self.position] != "\n":
                    self.position += 1
                if text.startswith("##", start) and start not in self.doc_comments:
                    line_start = text.rfind("\n", 0, start) + 1
                    if not text[line_start:start].strip():
                        body = text[start + 2 : self.position].rstrip()
                        if body.startswith(" "):
                            body = body[1:]
                        self.doc_comments[start] = body
            else:
                break

    def doc_above(self, position: int):
        """M41a: the doc comment (a run of full-line `##` comments directly
        above the line `position` starts, nothing but whitespace before it
        on that line) as text -- lines joined with `\n` -- or `None`. A
        blank line or a plain `#` comment ends the run, so a `#` comment
        never becomes documentation."""
        text = self.input_str
        line_start = text.rfind("\n", 0, position) + 1
        if text[line_start:position].strip():
            return None
        lines = []
        end = line_start
        while end > 0:
            prev_start = text.rfind("\n", 0, end - 1) + 1
            line = text[prev_start : end - 1]
            comment_at = prev_start + len(line) - len(line.lstrip())
            doc = self.doc_comments.get(comment_at)
            if doc is None:
                break
            lines.append(doc)
            end = prev_start
        if not lines:
            return None
        lines.reverse()
        return "\n".join(lines)

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
        if three == "...":
            self.position += 3
            return Token(TokenType.ELLIPSIS, three, start)
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
