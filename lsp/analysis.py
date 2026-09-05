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
from preprocessor import BUFFER_PATH, preprocess  # noqa: E402

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


def get_diagnostics(text: str, path: Optional[str] = None) -> list[dict]:
    """Compile ``text`` (resolving imports) and return LSP diagnostics.

    ``path`` is the on-disk path of the buffer, used to resolve ``import``
    directives relative to it. Errors originating in an imported file are
    attributed to the ``import`` directive that pulled it in, since we can only
    place diagnostics inside the file being edited.
    """
    pp = preprocess(path, text)
    diagnostics: list[dict] = []

    # 1. Unresolved / unreadable imports.
    for message, offset, length in pp.errors:
        diagnostics.append(
            {
                "range": make_range(text, offset, offset + max(length, 1)),
                "severity": SEVERITY_ERROR,
                "source": "mah",
                "message": message,
            }
        )

    combined = pp.text

    # An empty / comment-only document (after import resolution) is fine.
    tokens, lex_error = tokenize(combined)
    if not tokens and lex_error is None:
        return diagnostics

    lexer = TrackingLexer(combined)
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

        combined_offset = _extract_offset(message)
        if combined_offset is None:
            last = lexer.last_token
            combined_offset = last.position if last is not None else 0
            length = len(last.literal) if last is not None else 1
        else:
            length = _token_length_at(combined, combined_offset)
        length = max(length, 1)

        diagnostics.append(
            _diagnostic_for_combined_offset(
                pp, text, combined, combined_offset, length, _clean_message(message)
            )
        )

    return diagnostics


def _diagnostic_for_combined_offset(
    pp, text: str, combined: str, combined_offset: int, length: int, message: str
) -> dict:
    """Build a diagnostic in the buffer for an error at a combined-text offset."""
    origin_path, src_offset = pp.map_to_source(combined_offset)

    if origin_path == pp.entry_path:
        # Error is in the buffer itself; the entry source equals the buffer.
        return {
            "range": make_range(text, src_offset, src_offset + length),
            "severity": SEVERITY_ERROR,
            "source": "mah",
            "message": message or "error",
        }

    # Error is inside an imported file: attribute it to the import directive.
    import_site = pp.root_import_for(combined_offset)
    origin_source = pp.files.get(origin_path, "")
    line_info = _line_col(origin_source, src_offset)
    where = os.path.basename(origin_path)
    location = f"{where}:{line_info[0]}:{line_info[1]}" if line_info else where

    if import_site is not None:
        rng = make_range(
            text, import_site.offset, import_site.offset + import_site.length
        )
    else:
        rng = make_range(text, 0, 1)

    return {
        "range": rng,
        "severity": SEVERITY_ERROR,
        "source": "mah",
        "message": f"in imported file {location}: {message or 'error'}",
    }


def _line_col(source: str, offset: int):
    """1-based ``(line, column)`` of ``offset`` within ``source``."""
    if offset < 0:
        offset = 0
    if offset > len(source):
        offset = len(source)
    line = source.count("\n", 0, offset) + 1
    line_start = source.rfind("\n", 0, offset) + 1
    return line, (offset - line_start) + 1


# --------------------------------------------------------------------------
# Symbols (functions / variables) discovered by a light token scan
# --------------------------------------------------------------------------

@dataclass
class Symbol:
    name: str
    kind: int
    token: Token
    detail: str = ""
    doc: str = ""
    file: Optional[str] = None


def _leading_doc_comment(text: str, token: Token) -> str:
    """Collect contiguous ``#`` comment lines directly above a declaration.

    Comments are ignored by the lexer, so they are not in the token stream;
    we read them straight from the source. A blank or code line ends the
    doc block.
    """
    line_start = text.rfind("\n", 0, token.position) + 1
    lines_above = text[:line_start].splitlines()

    collected: list[str] = []
    for raw in reversed(lines_above):
        stripped = raw.strip()
        if stripped.startswith("#"):
            collected.append(stripped[1:].strip())
        else:
            break
    if not collected:
        return ""
    return "\n".join(reversed(collected)).strip()


def collect_symbols(
    tokens: list[Token], text: Optional[str] = None, file: Optional[str] = None
) -> list[Symbol]:
    """Find top-level function and variable declarations via a token scan.

    This is deliberately independent of a successful compile so that symbols
    and completions keep working while the file has errors elsewhere. When
    ``text`` is supplied, leading ``#`` doc comments are attached to each
    symbol.
    """
    symbols: list[Symbol] = []
    seen_names: set[str] = set()

    def doc_for(decl_token: Token) -> str:
        return _leading_doc_comment(text, decl_token) if text is not None else ""

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
                        Symbol(
                            name_token.literal,
                            SYMBOL_FUNCTION,
                            name_token,
                            detail,
                            doc_for(name_token),
                            file,
                        )
                    )
                # expose parameters as variables for completion
                for param, param_token in _read_param_tokens(tokens, index + 2):
                    pkey = ("var", param)
                    if pkey not in seen_names:
                        seen_names.add(pkey)
                        symbols.append(
                            Symbol(
                                param,
                                SYMBOL_VARIABLE,
                                param_token,
                                "parameter",
                                "",
                                file,
                            )
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
                            doc_for(name_token),
                            file,
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


def collect_imported_symbols(text: str, path: Optional[str]) -> list[Symbol]:
    """Top-level symbols contributed by files imported from the buffer.

    Returns symbols whose ``token`` positions are offsets *within their own
    file* (``symbol.file``), suitable for building cross-file locations.
    """
    if path is None:
        return []
    pp = preprocess(path, text)
    results: list[Symbol] = []
    seen: set = set()
    for import_site in pp.entry_imports:
        if not import_site.exists or import_site.resolved is None:
            continue
        source = pp.files.get(import_site.resolved)
        if source is None:
            continue
        file_tokens, _err = tokenize(source)
        for symbol in collect_symbols(file_tokens, source, import_site.resolved):
            if symbol.detail == "parameter":
                continue
            key = (symbol.name, symbol.kind)
            if key in seen:
                continue
            seen.add(key)
            results.append(symbol)
    return results


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

def get_completions(text: str, path: Optional[str] = None) -> list[dict]:
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
    local_symbols = collect_symbols(tokens, text)
    imported_symbols = collect_imported_symbols(text, path)

    seen: set = set()
    for symbol in local_symbols + imported_symbols:
        key = (symbol.name, symbol.kind)
        if key in seen:
            continue
        seen.add(key)
        kind = (
            COMPLETION_FUNCTION
            if symbol.kind == SYMBOL_FUNCTION
            else COMPLETION_VARIABLE
        )
        detail = symbol.detail
        if symbol.file is not None:
            detail = f"{detail}  (from {os.path.basename(symbol.file)})"
        item = {
            "label": symbol.name,
            "kind": kind,
            "detail": detail,
        }
        if symbol.doc:
            item["documentation"] = {"kind": "markdown", "value": symbol.doc}
        items.append(item)

    return items


# --------------------------------------------------------------------------
# Hover
# --------------------------------------------------------------------------

def get_hover(text: str, line: int, character: int, path: Optional[str] = None) -> Optional[dict]:
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
        # Prefer local declarations, then symbols pulled in via imports.
        symbols = {s.name: s for s in collect_symbols(tokens, text)}
        for imported in collect_imported_symbols(text, path):
            symbols.setdefault(imported.name, imported)
        symbol = symbols.get(token.literal)
        if symbol is not None and symbol.kind == SYMBOL_FUNCTION:
            value = f"**function** `{symbol.name}`\n\n```mah\n{symbol.detail}\n```"
        elif symbol is not None:
            note = "parameter" if symbol.detail == "parameter" else "variable"
            value = f"**{note}** `{token.literal}`"
        else:
            value = f"**identifier** `{token.literal}`"

        if symbol is not None and symbol.file is not None:
            value += f"\n\n*imported from `{os.path.basename(symbol.file)}`*"
        if symbol is not None and symbol.doc:
            value += "\n\n---\n\n" + symbol.doc
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


def get_definition(
    text: str, line: int, character: int, path: Optional[str] = None
) -> Optional[dict]:
    """Resolve the declaration for the symbol under the cursor.

    Returns ``{"path": <abs path or None>, "range": <lsp range>}`` where a
    ``path`` of ``None`` means "the current buffer". Resolution order:

      1. cursor on an ``import "..."`` path  -> the imported file (line 0);
      2. identifier resolved in local lexical scope  -> in-buffer declaration;
      3. identifier matching a top-level symbol from an imported file  -> that
         file's declaration (cross-file jump).

    Keywords, builtins and literals return ``None``.
    """
    offset = position_to_offset(text, line, character)

    # 1. Jump to file when the cursor is on an import path.
    if path is not None:
        pp = preprocess(path, text)
        for import_site in pp.entry_imports:
            if import_site.offset <= offset <= import_site.offset + import_site.length:
                if import_site.exists and import_site.resolved is not None:
                    return {
                        "path": import_site.resolved,
                        "range": {
                            "start": {"line": 0, "character": 0},
                            "end": {"line": 0, "character": 0},
                        },
                    }
                return None

    tokens, _lex_error = tokenize(text)
    token_index = _token_index_at_offset(tokens, offset)
    if token_index is None:
        return None

    token = tokens[token_index]
    if token.type != TokenType.ID:
        return None

    # 2. Local lexical resolution.
    scopes, token_scope = _build_scopes(tokens)
    declaration_token = _resolve_declaration(
        scopes, token_scope[token_index], token.literal
    )
    if declaration_token is not None:
        return {
            "path": None,
            "range": make_range(
                text,
                declaration_token.position,
                declaration_token.position + len(declaration_token.literal),
            ),
        }

    # 3. Cross-file resolution against imported top-level symbols.
    for symbol in collect_imported_symbols(text, path):
        if symbol.name == token.literal and symbol.file is not None:
            source = ""
            _pp = preprocess(path, text)
            source = _pp.files.get(symbol.file, "")
            return {
                "path": symbol.file,
                "range": make_range(
                    source,
                    symbol.token.position,
                    symbol.token.position + len(symbol.token.literal),
                ),
            }

    return None


# --------------------------------------------------------------------------
# Comment toggling (code action)
# --------------------------------------------------------------------------

COMMENT_PREFIX = "#"


def _split_lines_keepends(text: str) -> list[str]:
    return text.splitlines(keepends=True)


def toggle_line_comment(text: str, start_line: int, end_line: int) -> list[dict]:
    """Return TextEdits that comment or uncomment ``[start_line, end_line]``.

    Behaviour mirrors most editors: if every non-blank line in the range is
    already commented, the whole range is uncommented; otherwise every
    non-blank line is commented. Comments are inserted at the minimum common
    indentation so block structure is preserved.
    """
    lines = _split_lines_keepends(text)
    total = len(lines)
    if total == 0:
        lines = [""]
        total = 1

    if start_line > end_line:
        start_line, end_line = end_line, start_line
    start_line = max(0, start_line)
    end_line = min(end_line, total - 1)

    targets = list(range(start_line, end_line + 1))
    content = {i: lines[i].rstrip("\n").rstrip("\r") for i in targets}
    non_blank = [i for i in targets if content[i].strip()]
    if not non_blank:
        return []  # nothing but blank lines selected

    comment_re = re.compile(r"^(\s*)" + re.escape(COMMENT_PREFIX) + r" ?")
    all_commented = all(comment_re.match(content[i]) for i in non_blank)

    edits: list[dict] = []
    if all_commented:
        # Uncomment: strip the first `# ` (and an optional single space).
        for i in non_blank:
            match = comment_re.match(content[i])
            indent = match.group(1)
            removed_len = match.end() - len(indent)
            edits.append(
                {
                    "range": {
                        "start": {"line": i, "character": _utf16_len(indent)},
                        "end": {
                            "line": i,
                            "character": _utf16_len(indent) + removed_len,
                        },
                    },
                    "newText": "",
                }
            )
    else:
        # Comment: insert `# ` at the common minimum indentation.
        indent_width = min(
            len(content[i]) - len(content[i].lstrip()) for i in non_blank
        )
        for i in non_blank:
            edits.append(
                {
                    "range": {
                        "start": {"line": i, "character": _utf16_len(content[i][:indent_width])},
                        "end": {"line": i, "character": _utf16_len(content[i][:indent_width])},
                    },
                    "newText": COMMENT_PREFIX + " ",
                }
            )

    return edits


def get_code_actions(
    uri: str, text: str, start_line: int, end_line: int
) -> list[dict]:
    """Offer a comment-toggle code action for the selected line range."""
    edits = toggle_line_comment(text, start_line, end_line)
    if not edits:
        return []

    lines = _split_lines_keepends(text)
    non_blank = [
        i
        for i in range(start_line, min(end_line, len(lines) - 1) + 1)
        if 0 <= i < len(lines) and lines[i].strip()
    ]
    comment_re = re.compile(r"^\s*" + re.escape(COMMENT_PREFIX))
    all_commented = bool(non_blank) and all(
        comment_re.match(lines[i]) for i in non_blank
    )
    title = "Uncomment line(s)" if all_commented else "Comment line(s)"

    return [
        {
            "title": title,
            "kind": "source.toggleComment",
            "edit": {"changes": {uri: edits}},
        }
    ]
