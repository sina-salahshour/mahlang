"""Static analysis for the Mah language, built on top of the existing
compiler pipeline (lexer -> parser -> IR generator).

This module is dependency-free (standard library only) and is used by the
Mah language server (``lsp/server.py``) to produce diagnostics, hover
information, completions and document symbols.

The Mah compiler modules live in the repository root, so we make sure the
repository root is importable before importing them.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from typing import Optional

# --- make the Mah compiler importable -------------------------------------
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from actions import register_actions  # noqa: E402
from compiler.ir_generator import IRGenerator  # noqa: E402
from compiler.lexer import (  # noqa: E402
    IGNORED_TOKENS,
    TOKEN_RULES,
    Lexer,
    Token,
    TokenType,
)
from compiler.parser import Parser  # noqa: E402

# --------------------------------------------------------------------------
# Language metadata (used for hover + completion)
# --------------------------------------------------------------------------

KEYWORD_TOKENS = {
    TokenType.Let,
    TokenType.If,
    TokenType.Elif,
    TokenType.Else,
    TokenType.While,
    TokenType.Break,
    TokenType.Continue,
    TokenType.Return,
    TokenType.Def,
}

BUILTIN_TOKENS = {
    TokenType.Print,
    TokenType.Input,
    TokenType.Sin,
    TokenType.Cos,
}

KEYWORD_DOCS = {
    "let": "Declare a new variable in the current scope.\n\n```mah\nlet name = expr\n```",
    "if": "Conditional branch. Runs the block when the condition is truthy.\n\n"
    "```mah\nif cond {\n\t# ...\n}\n```",
    "elif": "Additional conditional branch, checked when previous `if`/`elif` "
    "conditions were false.",
    "else": "Fallback branch, runs when every preceding condition was false.",
    "while": "Loop while the condition is truthy.\n\n```mah\nwhile cond {\n\t# ...\n}\n```",
    "break": "Exit the innermost enclosing `while` loop.",
    "continue": "Skip to the next iteration of the innermost `while` loop.",
    "return": "Return from a function, optionally with a value.\n\n"
    "```mah\nreturn expr\n```",
    "def": "Define a function.\n\n```mah\ndef name(a, b) {\n\treturn a + b\n}\n```",
}

BUILTIN_DOCS = {
    "print": "Print one or more values, each on its own line.\n\n`print(a, b, ...)`",
    "input": "Read an integer from standard input.\n\n`input()`",
    "sin": "Sine of a number, in radians.\n\n`sin(x)`",
    "cos": "Cosine of a number, in radians.\n\n`cos(x)`",
}

# LSP enum values ----------------------------------------------------------
SEVERITY_ERROR = 1

COMPLETION_KEYWORD = 14
COMPLETION_FUNCTION = 3
COMPLETION_VARIABLE = 6

SYMBOL_FUNCTION = 12
SYMBOL_VARIABLE = 13


# --------------------------------------------------------------------------
# Position helpers (character offset <-> LSP line/character)
# LSP positions are 0-based and use UTF-16 code units for `character`.
# --------------------------------------------------------------------------

def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def offset_to_position(text: str, offset: int) -> dict:
    if offset < 0:
        offset = 0
    if offset > len(text):
        offset = len(text)
    line = text.count("\n", 0, offset)
    line_start = text.rfind("\n", 0, offset) + 1
    character = _utf16_len(text[line_start:offset])
    return {"line": line, "character": character}


def make_range(text: str, start: int, end: int) -> dict:
    return {
        "start": offset_to_position(text, start),
        "end": offset_to_position(text, end),
    }


def _line_start_offset(text: str, line: int) -> int:
    if line <= 0:
        return 0
    idx = -1
    for _ in range(line):
        idx = text.find("\n", idx + 1)
        if idx == -1:
            return len(text)
    return idx + 1


def position_to_offset(text: str, line: int, character: int) -> int:
    """Convert an LSP (line, character) position into a character offset."""
    start = _line_start_offset(text, line)
    rest = text[start:]
    newline = rest.find("\n")
    line_text = rest if newline == -1 else rest[:newline]

    units = 0
    offset = 0
    for ch in line_text:
        if units >= character:
            break
        units += _utf16_len(ch)
        offset += 1
    return start + offset


# --------------------------------------------------------------------------
# Tokenizing
# --------------------------------------------------------------------------

class TrackingLexer(Lexer):
    """A lexer that remembers the last token it produced.

    Some semantic errors raised by the compiler do not embed a position in
    their message; in that case we fall back to the location of the most
    recently consumed token, which is usually close to the real problem.
    """

    def __init__(self, input_str: str) -> None:
        super().__init__(input_str)
        self.last_token: Optional[Token] = None

    def get_next_token(self):
        token = super().get_next_token()
        self.last_token = token
        return token


def tokenize(text: str):
    """Return ``(tokens, lex_error)``.

    ``tokens`` excludes the internal EOF token and ignored tokens (comments).
    ``lex_error`` is the :class:`SyntaxError` raised on an invalid token, if
    any (tokenization stops at that point).
    """
    lexer = Lexer(text)
    tokens: list[Token] = []
    lex_error: Optional[SyntaxError] = None
    try:
        while True:
            token = lexer.get_next_token()
            if token.type == TokenType.EOF:
                break
            tokens.append(token)
    except SyntaxError as error:
        lex_error = error
    return tokens, lex_error


def _token_length_at(text: str, offset: int) -> int:
    """Length of the token that starts at ``offset`` (mirrors the lexer)."""
    if offset >= len(text):
        return 0
    remaining = text[offset:]
    for _token_type, pattern in TOKEN_RULES.items():
        match = re.match(pattern, remaining)
        if match and match.end() > 0:
            return match.end()
    return 1


def _token_at_offset(tokens: list[Token], offset: int) -> Optional[Token]:
    index = _token_index_at_offset(tokens, offset)
    return tokens[index] if index is not None else None


def _token_index_at_offset(tokens: list[Token], offset: int) -> Optional[int]:
    for index, token in enumerate(tokens):
        start = token.position
        end = start + len(token.literal)
        if start <= offset < end:
            return index
    # Accept the position right after a token (cursor at end of word).
    for index, token in enumerate(tokens):
        end = token.position + len(token.literal)
        if offset == end:
            return index
    return None


# --------------------------------------------------------------------------
# Diagnostics
# --------------------------------------------------------------------------

_POSITION_RE = re.compile(r"\s*at position '?(\d+)'?")


def _extract_offset(message: str) -> Optional[int]:
    match = _POSITION_RE.search(message)
    return int(match.group(1)) if match else None


def _clean_message(message: str) -> str:
    return _POSITION_RE.sub("", message).strip()


def get_diagnostics(text: str) -> list[dict]:
    """Compile ``text`` and return a list of LSP diagnostics."""
    # An empty / comment-only document is not an error while editing.
    tokens, lex_error = tokenize(text)
    if not tokens and lex_error is None:
        return []

    lexer = TrackingLexer(text)
    parser = Parser(lexer)
    ir = IRGenerator(parser)
    register_actions(ir)

    try:
        ir.generate()
        if len(ir.stack) != 0:
            raise AssertionError("internal error: expression stack is not empty")
    except SystemExit:
        raise
    except BaseException as error:  # noqa: BLE001 - report every compiler error
        args = getattr(error, "args", None)
        message = args[0] if args else str(error)
        if not isinstance(message, str):
            message = str(error)

        offset = _extract_offset(message)
        if offset is None:
            last = lexer.last_token
            offset = last.position if last is not None else 0
            length = len(last.literal) if last is not None else 1
        else:
            length = _token_length_at(text, offset)

        length = max(length, 1)
        return [
            {
                "range": make_range(text, offset, offset + length),
                "severity": SEVERITY_ERROR,
                "source": "mah",
                "message": _clean_message(message) or message,
            }
        ]

    return []


# --------------------------------------------------------------------------
# Symbols (functions / variables) discovered by a light token scan
# --------------------------------------------------------------------------

@dataclass
class Symbol:
    name: str
    kind: int
    token: Token
    detail: str = ""


def collect_symbols(tokens: list[Token]) -> list[Symbol]:
    """Find top-level function and variable declarations via a token scan.

    This is deliberately independent of a successful compile so that symbols
    and completions keep working while the file has errors elsewhere.
    """
    symbols: list[Symbol] = []
    seen_names: set[str] = set()

    index = 0
    count = len(tokens)
    while index < count:
        token = tokens[index]

        if token.type == TokenType.Def and index + 1 < count:
            name_token = tokens[index + 1]
            if name_token.type == TokenType.ID:
                params = _read_params(tokens, index + 2)
                detail = f"def {name_token.literal}({', '.join(params)})"
                key = ("fn", name_token.literal)
                if key not in seen_names:
                    seen_names.add(key)
                    symbols.append(
                        Symbol(name_token.literal, SYMBOL_FUNCTION, name_token, detail)
                    )
                # expose parameters as variables for completion
                for param, param_token in _read_param_tokens(tokens, index + 2):
                    pkey = ("var", param)
                    if pkey not in seen_names:
                        seen_names.add(pkey)
                        symbols.append(
                            Symbol(param, SYMBOL_VARIABLE, param_token, "parameter")
                        )

        elif token.type == TokenType.Let and index + 1 < count:
            name_token = tokens[index + 1]
            if name_token.type == TokenType.ID:
                key = ("var", name_token.literal)
                if key not in seen_names:
                    seen_names.add(key)
                    symbols.append(
                        Symbol(
                            name_token.literal,
                            SYMBOL_VARIABLE,
                            name_token,
                            f"let {name_token.literal}",
                        )
                    )

        index += 1

    return symbols


def _read_param_tokens(tokens: list[Token], start: int):
    """Yield ``(name, token)`` for parameters of ``def name( ... )``."""
    if start >= len(tokens) or tokens[start].type != TokenType.ParenOpen:
        return
    index = start + 1
    while index < len(tokens):
        token = tokens[index]
        if token.type == TokenType.ParenClose:
            return
        if token.type == TokenType.ID:
            yield token.literal, token
        index += 1


def _read_params(tokens: list[Token], start: int) -> list[str]:
    return [name for name, _token in _read_param_tokens(tokens, start)]


def get_document_symbols(text: str) -> list[dict]:
    tokens, _lex_error = tokenize(text)
    result = []
    for symbol in collect_symbols(tokens):
        if symbol.detail == "parameter":
            continue  # parameters are not reported as document symbols
        token_range = make_range(
            text,
            symbol.token.position,
            symbol.token.position + len(symbol.token.literal),
        )
        result.append(
            {
                "name": symbol.name,
                "detail": symbol.detail,
                "kind": symbol.kind,
                "range": token_range,
                "selectionRange": token_range,
            }
        )
    return result


# --------------------------------------------------------------------------
# Completion
# --------------------------------------------------------------------------

def get_completions(text: str) -> list[dict]:
    items: list[dict] = []

    for keyword, doc in KEYWORD_DOCS.items():
        items.append(
            {
                "label": keyword,
                "kind": COMPLETION_KEYWORD,
                "detail": "keyword",
                "documentation": {"kind": "markdown", "value": doc},
            }
        )

    for builtin, doc in BUILTIN_DOCS.items():
        items.append(
            {
                "label": builtin,
                "kind": COMPLETION_FUNCTION,
                "detail": "builtin",
                "documentation": {"kind": "markdown", "value": doc},
            }
        )

    tokens, _lex_error = tokenize(text)
    for symbol in collect_symbols(tokens):
        kind = (
            COMPLETION_FUNCTION
            if symbol.kind == SYMBOL_FUNCTION
            else COMPLETION_VARIABLE
        )
        items.append(
            {
                "label": symbol.name,
                "kind": kind,
                "detail": symbol.detail,
            }
        )

    return items


# --------------------------------------------------------------------------
# Hover
# --------------------------------------------------------------------------

def get_hover(text: str, line: int, character: int) -> Optional[dict]:
    tokens, _lex_error = tokenize(text)
    offset = position_to_offset(text, line, character)
    token = _token_at_offset(tokens, offset)
    if token is None:
        return None

    token_range = make_range(
        text, token.position, token.position + len(token.literal)
    )

    value: Optional[str] = None
    if token.type in KEYWORD_TOKENS:
        value = f"**keyword** `{token.literal}`\n\n" + KEYWORD_DOCS.get(
            token.literal, ""
        )
    elif token.type in BUILTIN_TOKENS:
        value = f"**builtin** `{token.literal}`\n\n" + BUILTIN_DOCS.get(
            token.literal, ""
        )
    elif token.type == TokenType.ID:
        symbols = {s.name: s for s in collect_symbols(tokens)}
        symbol = symbols.get(token.literal)
        if symbol is not None and symbol.kind == SYMBOL_FUNCTION:
            value = f"**function** `{symbol.name}`\n\n```mah\n{symbol.detail}\n```"
        elif symbol is not None:
            note = "parameter" if symbol.detail == "parameter" else "variable"
            value = f"**{note}** `{token.literal}`"
        else:
            value = f"**identifier** `{token.literal}`"
    elif token.type == TokenType.Number:
        value = f"**number** `{token.literal}`"
    elif token.type == TokenType.String:
        value = f"**string** `{token.literal}`"

    if value is None:
        return None

    return {
        "contents": {"kind": "markdown", "value": value},
        "range": token_range,
    }


# --------------------------------------------------------------------------
# Go to definition (scope-aware)
# --------------------------------------------------------------------------

# A declaration is stored as ``(kind, token)`` where kind is one of
# "var" | "param" | "fn".
_Declaration = tuple


@dataclass
class _Scope:
    id: int
    parent: Optional[int]
    declarations: dict  # name -> (kind, Token)


def _build_scopes(tokens: list[Token]):
    """Build a lexical scope tree from a token scan.

    Returns ``(scopes, token_scope)`` where ``scopes`` is a list indexed by
    scope id and ``token_scope[i]`` is the id of the scope that lexically
    contains ``tokens[i]``.

    Scoping mirrors the Mah grammar closely enough for editor navigation:

      * the whole file is the global scope (id 0);
      * every ``{ ... }`` block opens a nested scope (matching the
        ``@scopestart`` / ``@scopeend`` actions the compiler emits);
      * a function's parameters live in the same scope as its body;
      * ``let`` declares a variable in the current scope and ``def`` declares
        a function in the enclosing scope.

    Known limitation: a body-level ``let x`` that shadows a same-named
    parameter ``x`` collapses onto the parameter here (the real language nests
    them). This is rare and does not affect ordinary navigation.
    """
    scopes: list[_Scope] = [_Scope(0, None, {})]
    stack: list[int] = [0]
    token_scope: list[int] = [0] * len(tokens)
    reuse_next_brace = False

    count = len(tokens)
    index = 0
    while index < count:
        token = tokens[index]
        current = stack[-1]
        token_scope[index] = current

        if token.type == TokenType.Def and index + 1 < count and tokens[index + 1].type == TokenType.ID:
            name_token = tokens[index + 1]
            scopes[current].declarations.setdefault(
                name_token.literal, ("fn", name_token)
            )
            token_scope[index + 1] = current

            # Open the function scope now so parameters and body share it.
            fn_scope = _Scope(len(scopes), current, {})
            scopes.append(fn_scope)
            stack.append(fn_scope.id)
            reuse_next_brace = True

            # Declare parameters found in the following (...) group.
            cursor = index + 2
            if cursor < count and tokens[cursor].type == TokenType.ParenOpen:
                cursor += 1
                while cursor < count and tokens[cursor].type != TokenType.ParenClose:
                    if tokens[cursor].type == TokenType.ID:
                        fn_scope.declarations.setdefault(
                            tokens[cursor].literal, ("param", tokens[cursor])
                        )
                    cursor += 1

            index += 2
            continue

        if token.type == TokenType.Let and index + 1 < count and tokens[index + 1].type == TokenType.ID:
            name_token = tokens[index + 1]
            scopes[current].declarations.setdefault(
                name_token.literal, ("var", name_token)
            )
            token_scope[index + 1] = current
            index += 2
            continue

        if token.type == TokenType.BraceOpen:
            if reuse_next_brace:
                # Function body reuses the scope opened at `def`.
                reuse_next_brace = False
            else:
                block_scope = _Scope(len(scopes), current, {})
                scopes.append(block_scope)
                stack.append(block_scope.id)
        elif token.type == TokenType.BraceClose:
            if len(stack) > 1:
                stack.pop()

        index += 1

    return scopes, token_scope


def _resolve_declaration(scopes: list[_Scope], scope_id: int, name: str):
    """Walk from ``scope_id`` outward to global, returning the declaring token."""
    current: Optional[int] = scope_id
    while current is not None:
        declaration = scopes[current].declarations.get(name)
        if declaration is not None:
            return declaration[1]
        current = scopes[current].parent
    return None


def get_definition(text: str, line: int, character: int) -> Optional[dict]:
    """Return the LSP range of the declaration for the identifier at the cursor.

    Only identifiers resolve; keywords, builtins and literals return ``None``.
    """
    tokens, _lex_error = tokenize(text)
    offset = position_to_offset(text, line, character)
    token_index = _token_index_at_offset(tokens, offset)
    if token_index is None:
        return None

    token = tokens[token_index]
    if token.type != TokenType.ID:
        return None

    scopes, token_scope = _build_scopes(tokens)
    declaration_token = _resolve_declaration(
        scopes, token_scope[token_index], token.literal
    )
    if declaration_token is None:
        return None

    return make_range(
        text,
        declaration_token.position,
        declaration_token.position + len(declaration_token.literal),
    )
