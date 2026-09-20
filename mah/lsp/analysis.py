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

from ..compiler.codegen import Codegen  # noqa: E402
from ..compiler.lexer import KEYWORDS, Lexer, Token, TokenType  # noqa: E402
from ..compiler.parser import Parser  # noqa: E402
from ..compiler.resolve import Resolver  # noqa: E402
from ..preprocessor import BUFFER_PATH, demangle_message, preprocess  # noqa: E402

# --------------------------------------------------------------------------
# Language metadata (used for hover + completion)
# --------------------------------------------------------------------------

KEYWORD_TOKENS = {
    TokenType.LET,
    TokenType.IF,
    TokenType.ELIF,
    TokenType.ELSE,
    TokenType.WHILE,
    TokenType.BREAK,
    TokenType.CONTINUE,
    TokenType.RETURN,
    TokenType.FN,
    TokenType.STRUCT,
    TokenType.ENUM,
    TokenType.MATCH,
    TokenType.SOME,
    TokenType.NONE,
}

BUILTIN_TOKENS = {
    TokenType.PRINT,
    TokenType.INPUT,
    TokenType.SIN,
    TokenType.COS,
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
    "fn": "Define a function. With a name, it's sugar for binding a closure "
    "value to that name; without one, it's an anonymous closure expression "
    "usable anywhere (assigned, returned, passed as an argument).\n\n"
    "```mah\nfn name(a, b) {\n\treturn a + b\n}\n\nlet add = fn(a, b) { a + b }\n```",
    "struct": "Declare a fixed-shape struct type (field names only, no types).\n\n"
    "```mah\nstruct Point { x, y }\nlet p = Point { x: 1, y: 2 }\n```",
    "enum": "Declare an enum type: each variant is either a unit (no payload) "
    "or struct-shaped (named fields).\n\n"
    "```mah\nenum Shape {\n\tCircle { r },\n\tEmpty\n}\n```",
    "match": "Pattern-match a value against a sequence of patterns, running "
    "the first arm whose pattern matches.\n\n"
    "```mah\nmatch value {\n\tsome(x) => { print(x) }\n\tnone => { print(\"nothing\") }\n}\n```",
    "some": "Construct a value wrapping `x` in the built-in `Option` type -- "
    "the `Some`-equivalent, always truthy regardless of `x`.\n\n`some(x)`",
    "none": "The built-in `Option` type's empty value -- Mah's null "
    "equivalent. Falsy (the only enum value that is).\n\n`none`",
    "export": "Make a top-level declaration visible to files that `import` this "
    "one.\n\n```mah\nexport fn name(a) { ... }\nexport let value = 1\nexport name  # export something declared elsewhere\n```",
    "import": "Inline another file's `export`ed declarations. The path is "
    "resolved relative to this file.\n\n```mah\nimport \"lib.mh\"\n```",
}

BUILTIN_DOCS = {
    "print": "Print one or more values, each on its own line.\n\n`print(a, b, ...)`",
    "input": "Read an integer from standard input.\n\n`input()`",
    "sin": "Sine of a number, in radians.\n\n`sin(x)`",
    "cos": "Cosine of a number, in radians.\n\n`cos(x)`",
}

# LSP enum values ----------------------------------------------------------
SEVERITY_ERROR = 1

# Used by get_definition's import-directive handling: jumping to a whole
# imported *file* has no specific symbol position to point at, so land at
# the very top of it -- a zero-width range at line 0, character 0.
_FILE_START_RANGE = {
    "start": {"line": 0, "character": 0},
    "end": {"line": 0, "character": 0},
}

COMPLETION_KEYWORD = 14
COMPLETION_FUNCTION = 3
COMPLETION_VARIABLE = 6
COMPLETION_MODULE = 9

SYMBOL_FUNCTION = 12
SYMBOL_VARIABLE = 13

# `import` / `export` are not lexer keywords (they tokenize as identifiers);
# the preprocessor gives them meaning. Treat them as soft keywords for editor
# features when they appear in the right position.
SOFT_KEYWORDS = {"import", "export"}


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

    Resilient tokenizer for editor features: characters the compiler lexer
    rejects (notably ``.`` used by namespaced imports, which the compiler has
    no token for) are skipped rather than aborting tokenization, so features
    keep working on the whole buffer. ``tokens`` excludes EOF and ignored
    tokens (comments). ``lex_error`` is kept for signature compatibility and is
    always ``None`` here (diagnostics use a strict pass on preprocessed text).
    """
    lexer = Lexer(text)
    tokens: list[Token] = []
    length = len(text)
    while True:
        try:
            token = lexer.get_next_token()
        except SyntaxError:
            # Skip the offending character and resume.
            lexer.position += 1
            if lexer.position > length:
                break
            continue
        if token.type == TokenType.EOF:
            break
        tokens.append(token)
    return tokens, None


def _token_length_at(text: str, offset: int) -> int:
    """Length of the token that starts at ``offset`` (mirrors the lexer).

    M6 note: the old generated lexer exposed a `TOKEN_RULES` regex table
    this used to scan directly; the current hand-written `Lexer` has no
    such table, so this drives the real lexer instead -- seek it to
    `offset` and read one token. Falls back to a length of 1 (a single
    character diagnostic underline) for anything the lexer can't tokenize
    there, or when `offset` actually sits on skipped trivia (whitespace/a
    comment) rather than a real token's first character."""
    if offset >= len(text):
        return 0
    lexer = Lexer(text)
    lexer.position = offset
    try:
        token = lexer.get_next_token()
    except SyntaxError:
        return 1
    if token.position != offset:
        return 1
    return max(len(token.literal), 1)


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


def _is_soft_keyword(token: Token, tokens: list[Token]) -> bool:
    """True when ``token`` is an ``import``/``export`` acting as a keyword.

    ``export`` counts when followed by ``def``/``let``/an identifier; ``import``
    counts when followed by a string (``import "x"``) or ``<id> from "x"``.
    """
    if token.type != TokenType.ID or token.literal not in SOFT_KEYWORDS:
        return False
    # Token.__eq__ compares by type only, so locate by position instead.
    pos = next(
        (i for i, t in enumerate(tokens) if t.position == token.position), None
    )
    if pos is None:
        return False
    nxt = tokens[pos + 1] if pos + 1 < len(tokens) else None
    if token.literal == "import":
        if nxt is not None and nxt.type == TokenType.STRING:
            return True
        # import <ns> from "..."
        return (
            nxt is not None
            and nxt.type == TokenType.ID
            and pos + 3 < len(tokens)
            and tokens[pos + 2].type == TokenType.ID
            and tokens[pos + 2].literal == "from"
            and tokens[pos + 3].type == TokenType.STRING
        )
    # export
    return nxt is not None and nxt.type in (
        TokenType.FN,
        TokenType.LET,
        TokenType.ID,
    )


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

    M6: this reports *every* diagnostic the current pipeline can find in
    one pass -- unresolved imports, every collected parse error (the whole
    point of the parser now being forgiving instead of stopping at the
    first mistake), and, only when parsing was completely clean, the
    first resolve-time error (resolve stays single-exception/stop-at-first
    on purpose -- see docs/V2_DESIGN.md's M6 milestone; making resolve
    forgiving too is explicitly out of scope here). Codegen is never run:
    diagnostics only need parse + resolve, not full compilation.
    """
    pp = preprocess(path, text)
    diagnostics: list[dict] = []

    # 1. Unresolved / unreadable imports (unchanged from pre-M6).
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

    lexer = Lexer(combined)
    parser = Parser(lexer)
    program = parser.parse_program()

    # 2. Every syntax error the parser collected, not just the first --
    # this is the M6 feature: the parser recovers and keeps going instead
    # of aborting on the first mistake, so an editor can show every
    # mistake in the file in one round trip.
    for message, offset in parser.errors:
        length = _token_length_at(combined, offset)
        diagnostics.append(
            _diagnostic_for_combined_offset(
                pp, text, combined, offset, length, demangle_message(_clean_message(message))
            )
        )

    # 3. Resolve-time error -- only attempted when parsing itself was
    # completely clean. A program with parser.errors has structural holes
    # (ErrorNodes) resolve can't meaningfully diagnose past, and mah.py
    # refuses to build/run such a program regardless -- so there is
    # nothing extra to gain by resolving it anyway, matching today's
    # non-forgiving resolve behavior of reporting exactly one error.
    if not parser.errors:
        try:
            Resolver().resolve_program(program)
        except SystemExit:
            raise
        except BaseException as error:  # noqa: BLE001 - report the one resolve error
            args = getattr(error, "args", None)
            message = args[0] if args else str(error)
            if not isinstance(message, str):
                message = str(error)

            combined_offset = _extract_offset(message)
            if combined_offset is None:
                combined_offset = 0
                length = 1
            else:
                length = _token_length_at(combined, combined_offset)
            length = max(length, 1)

            diagnostics.append(
                _diagnostic_for_combined_offset(
                    pp,
                    text,
                    combined,
                    combined_offset,
                    length,
                    demangle_message(_clean_message(message)),
                )
            )

    return diagnostics


def _diagnostic_for_combined_offset(
    pp, text: str, combined: str, combined_offset: int, length: int, message: str
) -> dict:
    """Build a diagnostic in the buffer for an error at a combined-text offset."""
    origin_path, src_offset = pp.map_to_source(combined_offset)
    # Map the end through the source map too: a renamed/mangled token is longer
    # in the combined text than in the source, so a raw length would overshoot.
    _end_path, src_end = pp.map_to_source(combined_offset + max(length, 1) - 1)
    src_length = max(1, src_end - src_offset + 1)

    if origin_path == pp.entry_path:
        # Error is in the buffer itself; the entry source equals the buffer.
        return {
            "range": make_range(text, src_offset, src_offset + src_length),
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
    """Exported top-level symbols brought into scope by *flat* imports.

    Only names a file marks with ``export`` are surfaced (scoped imports), and
    only flat ``import "path"`` directives contribute names at top level;
    namespaced imports expose their members through the namespace instead (see
    :func:`collect_namespaces`). Returned symbols carry positions *within their
    own file* (``symbol.file``), suitable for cross-file locations.
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
        exported = pp.exported_names(import_site.resolved)
        file_tokens, _err = tokenize(source)
        for symbol in collect_symbols(file_tokens, source, import_site.resolved):
            if symbol.detail == "parameter":
                continue
            if symbol.name not in exported:
                continue  # scoped: only exported names are importable
            key = (symbol.name, symbol.kind)
            if key in seen:
                continue
            seen.add(key)
            results.append(symbol)
    return results


@dataclass
class Namespace:
    name: str
    file: str
    token: Optional[Token]      # the namespace identifier token in the buffer
    members: list               # list[Symbol] for exported members


def collect_namespaces(text: str, path: Optional[str]) -> list[Namespace]:
    """Namespaces introduced by ``import ns from "path"`` in the buffer."""
    if path is None:
        return []
    pp = preprocess(path, text)
    result: list[Namespace] = []
    for ns in pp.entry_namespaces:
        if not ns.exists or ns.resolved is None:
            result.append(Namespace(ns.name, "", None, []))
            continue
        source = pp.files.get(ns.resolved, "")
        exported = pp.exported_names(ns.resolved)
        file_tokens, _err = tokenize(source)
        members = [
            s
            for s in collect_symbols(file_tokens, source, ns.resolved)
            if s.detail != "parameter" and s.name in exported
        ]
        # Token for the namespace name in the buffer (for hover/goto-def).
        buf_tokens, _e = tokenize(text)
        ns_token = None
        for tok in buf_tokens:
            if tok.type == TokenType.ID and tok.position == ns.name_offset:
                ns_token = tok
                break
        result.append(Namespace(ns.name, ns.resolved, ns_token, members))
    return result


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

def _symbol_completion_item(symbol: Symbol) -> dict:
    kind = (
        COMPLETION_FUNCTION
        if symbol.kind == SYMBOL_FUNCTION
        else COMPLETION_VARIABLE
    )
    detail = symbol.detail
    if symbol.file is not None:
        detail = f"{detail}  (from {os.path.basename(symbol.file)})"
    item = {"label": symbol.name, "kind": kind, "detail": detail}
    if symbol.doc:
        item["documentation"] = {"kind": "markdown", "value": symbol.doc}
    return item


def _namespace_prefix_at(text: str, offset: int) -> Optional[str]:
    """If the cursor is positioned right after ``ident.`` return ``ident``.

    Handles ``math.`` and ``math.par`` (partway through a member name).
    """
    i = offset
    # Skip an in-progress member identifier immediately before the cursor.
    while i > 0 and (text[i - 1].isalnum() or text[i - 1] in "_$"):
        i -= 1
    if i == 0 or text[i - 1] != ".":
        return None
    j = i - 1  # index of the dot
    end = j
    k = j
    while k > 0 and (text[k - 1].isalnum() or text[k - 1] in "_$"):
        k -= 1
    name = text[k:end]
    return name or None


def get_completions(
    text: str,
    path: Optional[str] = None,
    line: Optional[int] = None,
    character: Optional[int] = None,
) -> list[dict]:
    namespaces = collect_namespaces(text, path)

    # Namespaced member completion: when the cursor follows `ns.`, offer only
    # that namespace's exported members.
    if line is not None and character is not None:
        offset = position_to_offset(text, line, character)
        ns_name = _namespace_prefix_at(text, offset)
        if ns_name is not None:
            for ns in namespaces:
                if ns.name == ns_name:
                    return [_symbol_completion_item(m) for m in ns.members]
            return []

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

    # Namespaces themselves are completable identifiers.
    for ns in namespaces:
        items.append(
            {
                "label": ns.name,
                "kind": COMPLETION_MODULE,
                "detail": f"namespace (from {os.path.basename(ns.file)})"
                if ns.file
                else "namespace",
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
        items.append(_symbol_completion_item(symbol))

    return items


# --------------------------------------------------------------------------
# Hover
# --------------------------------------------------------------------------

def get_hover(text: str, line: int, character: int, path: Optional[str] = None) -> Optional[dict]:
    """Hover text for the token under the cursor.

    Keyword/builtin/soft-keyword/literal hover is a lightweight, tokenize-
    only lookup (no need to resolve anything). Identifier hover is built on
    the resolver's real symbol table (`compiler/resolve.py`'s `Symbol`/
    `position_index`, see docs/V2_DESIGN.md's M7 milestone) -- the same
    source of truth go-to-definition/rename already use -- rather than the
    old `collect_symbols`/`collect_namespaces`/`collect_imported_symbols`
    token-scanning helpers below, which predate M0's pipeline and are no
    longer maintained (left as dead code; do not call them from here).

    Scope, matching M7's go-to-definition (not rename): identifier hover
    covers variables/parameters/function bindings only, and *is* allowed to
    describe a symbol whose declaration lives in an imported file (unlike
    rename, which refuses cross-file symbols outright) -- it just notes
    where the declaration actually lives. Struct/enum type names and field
    names, and doc-comment extraction, are not covered (future work, see
    docs/NEXT_PHASES.md)."""
    tokens, _lex_error = tokenize(text)
    offset = position_to_offset(text, line, character)
    token = _token_at_offset(tokens, offset)
    if token is None:
        return None

    token_range = make_range(text, token.position, token.position + len(token.literal))

    if token.type in KEYWORD_TOKENS:
        value = f"**keyword** `{token.literal}`\n\n" + KEYWORD_DOCS.get(token.literal, "")
    elif token.type in BUILTIN_TOKENS:
        value = f"**builtin** `{token.literal}`\n\n" + BUILTIN_DOCS.get(token.literal, "")
    elif token.type is TokenType.ID and _is_soft_keyword(token, tokens):
        value = f"**keyword** `{token.literal}`\n\n" + KEYWORD_DOCS.get(token.literal, "")
    elif token.type is TokenType.NUMBER:
        value = f"**number** `{token.literal}`"
    elif token.type is TokenType.STRING:
        value = f"**string** `{token.literal}`"
    elif token.type is TokenType.ID:
        found = _symbol_at_position(text, line, character, path)
        if found is None:
            return None
        pp, _resolver, symbol = found
        kind_label = {
            "let": "variable",
            "fn": "function",
            "param": "parameter",
            "binding": "binding",
        }.get(symbol.kind, symbol.kind)
        value = f"**{kind_label}** `{symbol.name}`"
        decl_path, _decl_offset = pp.map_to_source(symbol.decl_position)
        if decl_path != pp.entry_path:
            value += f"\n\n*declared in `{os.path.basename(decl_path)}`*"
    else:
        return None

    return {
        "contents": {"kind": "markdown", "value": value},
        "range": token_range,
    }


def _member_owner(text: str, token: Token) -> Optional[str]:
    """If ``token`` is the ``member`` in ``owner.member``, return ``owner``."""
    i = token.position
    # scan backwards over whitespace to a dot
    j = i - 1
    while j >= 0 and text[j] in " \t":
        j -= 1
    if j < 0 or text[j] != ".":
        return None
    end = j
    k = j
    while k > 0 and (text[k - 1].isalnum() or text[k - 1] in "_$"):
        k -= 1
    owner = text[k:end]
    return owner or None


def _is_namespace_use(text: str, token: Token) -> bool:
    """True when ``token`` is an identifier followed by ``.`` (namespace use)."""
    i = token.position + len(token.literal)
    while i < len(text) and text[i] in " \t":
        i += 1
    return i < len(text) and text[i] == "."


# --------------------------------------------------------------------------
# Go to definition + rename (M7: built on the resolver's real symbol table)
# --------------------------------------------------------------------------
#
# M7 retires the independent token-scanning scope model that used to live
# here (`_build_scopes`/`_resolve_declaration`/`_Scope`) in favor of running
# the real compiler pipeline through `Resolver` and reading its
# `position_index`/`Symbol` records directly -- see compiler/resolve.py's
# module docstring and docs/V2_DESIGN.md's M7 milestone. This is the single
# source of truth for "what does this identifier occurrence refer to," so
# it can never drift from the compiler's own actual scoping rules the way
# the old re-derived scope tree could.
#
# Scope, deliberately not covered here (see docs/V2_DESIGN.md's M7 entry):
#   - struct/enum type names and struct/enum field names are NOT part of
#     the resolver's symbol table (they live in `Resolver.struct_decls`/
#     `enum_decls`, a separate namespace with different reference-tracking
#     needs) -- renaming them is a distinct, larger future piece of work.
#   - rename is single-file only: a symbol whose declaration or any
#     reference falls outside the entry file's own text segment (i.e. it
#     touches an inlined import) is refused outright rather than performed
#     partially -- see `get_rename_edits` below.
#   - hover/completion/document-symbols are NOT revived by this milestone;
#     `get_hover`/`get_completions`/`get_document_symbols` above are left
#     exactly as M6 found them (disabled/non-advertised, still referencing
#     the old `TokenType` spellings).


def _resolve_for_navigation(text: str, path: Optional[str]):
    """Run the full pipeline through resolve for navigation features
    (go-to-definition, rename).

    Returns ``(pp, resolver, combined_tokens)``, or ``None`` if the file
    doesn't resolve cleanly enough to have a usable symbol table -- resolve
    errors are NOT forgiving (only parsing is, since M6), so a file with
    e.g. a reference to an undefined variable anywhere has no symbol table
    available at all, not a partial one.
    """
    pp = preprocess(path, text)
    if pp.errors:
        return None
    combined = pp.text
    lexer = Lexer(combined)
    parser = Parser(lexer)
    program = parser.parse_program()
    resolver = Resolver()
    try:
        resolver.resolve_program(program)
    except Exception:  # noqa: BLE001 - any resolve failure means "no symbol table"
        return None
    tokens, _lex_error = tokenize(combined)
    return pp, resolver, tokens


def _combined_offset_for_position(pp, text: str, line: int, character: int) -> Optional[int]:
    """Convert an LSP (line, character) position in the *buffer* (entry-file)
    text into an offset in the preprocessor's combined text, via the same
    entry<->combined segment mapping `preprocessor.py` already exposes.
    Returns ``None`` when the position doesn't land inside any entry-file
    segment of the combined text (e.g. it's inside an `import` directive's
    own text, which is dropped from the combined output entirely)."""
    entry_offset = position_to_offset(text, line, character)
    return pp.entry_to_combined(entry_offset)


def _symbol_at_position(text: str, line: int, character: int, path: Optional[str]):
    """Shared lookup for go-to-definition and rename: run the pipeline,
    locate the combined-text token under the cursor, and resolve it to its
    `Symbol` via the resolver's `position_index`. Returns
    ``(pp, resolver, symbol)`` or ``None``."""
    result = _resolve_for_navigation(text, path)
    if result is None:
        return None
    pp, resolver, tokens = result

    combined_offset = _combined_offset_for_position(pp, text, line, character)
    if combined_offset is None:
        return None

    token = _token_at_offset(tokens, combined_offset)
    if token is None or token.type is not TokenType.ID:
        return None

    symbol = resolver.position_index.get(token.position)
    if symbol is None:
        return None

    return pp, resolver, symbol


def _identifier_length_at(source: str, offset: int) -> int:
    """Length of the identifier written in `source` starting at `offset`
    (mirrors `compiler/lexer.py`'s identifier-scanning rule). Used instead
    of `len(symbol.name)` when locating a symbol's span in a *source* file's
    own original text: `symbol.name` reflects the combined/preprocessed
    text, which the preprocessor mangles for declarations (and some
    references) belonging to an imported (non-entry) file
    (`__mah_m{idx}_{name}`, see preprocessor.py's module docstring) -- using
    its length against the original, un-mangled source text would overshoot
    past the real identifier. Falls back to `0` (caller should then fall
    back to `len(symbol.name)`) if `offset` doesn't actually sit on an
    identifier's first character -- shouldn't happen for a resolved decl/
    reference position, but this is a navigation feature, not the compiler
    itself, so it degrades gracefully rather than raising."""
    n = len(source)
    if offset >= n:
        return 0
    ch = source[offset]
    if not (ch.isalpha() or ch in "_$"):
        return 0
    end = offset + 1
    while end < n and (source[end].isalnum() or source[end] in "_$"):
        end += 1
    return end - offset


def get_definition(
    text: str, line: int, character: int, path: Optional[str] = None
) -> Optional[dict]:
    """Resolve the declaration for the variable/parameter/function symbol
    under the cursor, using the resolver's real symbol table.

    Returns ``{"path": <abs path or None>, "range": <lsp range>}`` where a
    ``path`` of ``None`` means "the current document" -- the shape
    `lsp/server.py`'s `_on_textDocument_definition` handler expects.
    Covers variables/parameters/function bindings only (not struct/enum
    type or field names, which aren't in the symbol table at all -- see
    module notes above). Returns ``None`` when the file doesn't resolve
    cleanly, the cursor isn't on an identifier, or that identifier never
    resolved to anything (a keyword, a struct/enum name, a field name, ...).

    Also handles the cursor sitting on an `import` directive itself -- the
    path string (`import "mathlib"`) or the namespace identifier
    (`import math from "mathlib"`) -- jumping straight to the imported
    file, using `preprocessor.py`'s own already-computed `entry_imports`/
    entry_namespaces` (an `ImportSite`/`NamespaceImport` per directive,
    with the resolved path and the directive's own entry-file position),
    checked before falling through to the symbol-table lookup above since
    import directives aren't part of the real grammar at all (the
    preprocessor consumes them before the real lexer ever runs) and so
    could never resolve to a `Symbol`.
    """
    pp_for_imports = preprocess(path, text)
    entry_offset = position_to_offset(text, line, character)
    for imp in pp_for_imports.entry_imports:
        if imp.offset <= entry_offset < imp.offset + imp.length:
            if imp.resolved is None:
                return None  # unresolved import path -- nothing to jump to
            return {"path": imp.resolved, "range": _FILE_START_RANGE}
    for ns in pp_for_imports.entry_namespaces:
        in_path_string = ns.offset <= entry_offset < ns.offset + ns.length
        in_namespace_name = ns.name_offset <= entry_offset < ns.name_offset + ns.name_length
        if in_path_string or in_namespace_name:
            if ns.resolved is None:
                return None
            return {"path": ns.resolved, "range": _FILE_START_RANGE}

    found = _symbol_at_position(text, line, character, path)
    if found is None:
        return None
    pp, _resolver, symbol = found

    decl_path, decl_offset = pp.map_to_source(symbol.decl_position)
    source = text if decl_path == pp.entry_path else pp.files.get(decl_path, "")
    length = _identifier_length_at(source, decl_offset) or len(symbol.name)
    if decl_path == pp.entry_path:
        return {
            "path": None,
            "range": make_range(text, decl_offset, decl_offset + length),
        }
    # Declaration lives in an inlined import -- still a valid jump for
    # go-to-definition (unlike rename, which refuses cross-file symbols
    # outright, see `get_rename_edits`): build the range against that
    # file's own source text.
    return {
        "path": decl_path,
        "range": make_range(source, decl_offset, decl_offset + length),
    }


def _is_valid_mah_identifier(name: str) -> bool:
    """Mirrors `compiler/lexer.py`'s identifier-scanning rule exactly:
    first character a letter/`_`/`$`, remaining characters alphanumeric/
    `_`/`$`. Does not check for a scope collision with an existing binding
    -- that's a known, documented limitation of M7's rename (see
    docs/V2_DESIGN.md's M7 milestone)."""
    if not name:
        return False
    first = name[0]
    if not (first.isalpha() or first in "_$"):
        return False
    return all(ch.isalnum() or ch in "_$" for ch in name[1:])


def get_rename_edits(
    text: str,
    line: int,
    character: int,
    new_name: str,
    path: Optional[str] = None,
) -> Optional[dict]:
    """Rename the variable/parameter/function symbol under the cursor.

    Returns ``{"changes": {<key>: [TextEdit, ...]}}`` where ``<key>`` is the
    entry file's own path (or `preprocessor.BUFFER_PATH` for an unsaved,
    path-less buffer) -- `lsp/server.py`'s rename handler swaps this for the
    real document URI before responding to the client, the same way
    `get_definition`'s ``path`` key is translated to a URI there. Returns
    ``None`` (refusing the rename outright) when:

      - the file doesn't resolve cleanly (no symbol table available at all);
      - the cursor isn't on an identifier that resolved to a symbol;
      - `new_name` isn't a syntactically valid Mah identifier, or is a
        reserved keyword;
      - the symbol's declaration or ANY of its references falls outside the
        entry file's own text (i.e. it touches an inlined import) --
        renaming it correctly would require rewriting the preprocessor's
        name-mangling across multiple files in one atomic edit, which is
        out of scope for this milestone (see module notes above and
        docs/V2_DESIGN.md's M7 milestone). A partial, single-file-only
        rename that silently leaves other files using the old name would be
        worse than refusing outright.

    Does not check whether `new_name` would collide with an unrelated
    existing binding already in scope -- a known, documented limitation.
    Covers variables/parameters/function bindings only, not struct/enum
    type or field names (see module notes above).
    """
    if not _is_valid_mah_identifier(new_name) or new_name in KEYWORDS:
        return None

    found = _symbol_at_position(text, line, character, path)
    if found is None:
        return None
    pp, _resolver, symbol = found

    positions = [symbol.decl_position] + list(symbol.references)
    edits = []
    for position in positions:
        src_path, src_offset = pp.map_to_source(position)
        if src_path != pp.entry_path:
            # Cross-file: refuse the whole rename rather than perform a
            # partial, single-file-only edit that silently misses other
            # files -- see docstring above.
            return None
        length = _identifier_length_at(text, src_offset) or len(symbol.name)
        edits.append(
            {
                "range": make_range(text, src_offset, src_offset + length),
                "newText": new_name,
            }
        )

    key = pp.entry_path
    return {"changes": {key: edits}}


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
