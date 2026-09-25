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
from ..runtime_values import BUILTIN_TYPE_NAMES  # noqa: E402

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
    TokenType.DEFER,
    TokenType.DETACH,
    TokenType.TRAIT,
    TokenType.IMPL,
    TokenType.FOR,
}

BUILTIN_TOKENS = {
    TokenType.PRINT,
    TokenType.INPUT,
    TokenType.SIN,
    TokenType.COS,
    TokenType.SLEEP_ASYNC,
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
    "usable anywhere (assigned, returned, passed as an argument). A "
    "parameter may have a default value (`h = 1`), evaluated at call time "
    "whenever it's left unbound; every parameter after the first defaulted "
    "one needs a default too. Any parameter can also be passed by keyword "
    "at the call site (`name: value`), in any order, after the positional "
    "arguments.\n\n"
    "```mah\nfn name(a, b) {\n\treturn a + b\n}\n\nlet add = fn(a, b) { a + b }\n\n"
    "fn area(w, h = 1) { w * h }\narea(2)        # 2\narea(2, h: 3)  # 6\n```",
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
    "defer": "Schedule a statement to run when the enclosing block exits "
    "(falling through, or via return/break/continue) -- LIFO order among "
    "multiple defers in the same block, Zig-style, not function-scoped "
    "like Go.\n\n```mah\nfn process(name) {\n\tlet r = open(name)\n\tdefer close(r)\n\t...\n}\n```",
    "detach": "Start a function call running immediately, synchronously -- "
    "runs to completion in place unless it hits a real suspension (like a "
    "bare `sleep_async` inside it), in which case it hands back a "
    "still-pending `Promise` instead of blocking. Never itself a "
    "scheduling boundary. `.await` is only needed once you've opted out "
    "of blocking this way -- a bare, non-detached call never needs it.\n\n"
    "```mah\nlet p = detach fetch_thing()\n...\nlet result = p.await\n```",
    "await": "Suspend the current execution until this `Promise` settles "
    "(`Promise.Settled { value }`), then yield `value`. Written as a "
    "postfix pseudo-field (`value.await`), not a prefix keyword. Only "
    "meaningful on a `Promise` you got from an explicit `detach` -- a "
    "bare, non-detached call already blocks on its own, with nothing to "
    "await.\n\n`value.await`",
    "trait": "Declare a set of methods a type can implement. A method with "
    "no body is required; one with a body is a default, inherited unless "
    "the implementing type overrides it. A method whose first parameter "
    "isn't `self` is a static function (called as `Type.fn(...)`, not on "
    "a value).\n\n"
    "```mah\ntrait Shape {\n\tfn area(self)\n\tfn name(self) { \"shape\" }\n\tfn unit()\n}\n```",
    "impl": "Implement a trait for a type (`impl Trait for Type { ... }` "
    "-- `Type` may be a built-in type like `Promise` as long as `Trait` "
    "is your own), or give a user type its own inherent methods/functions "
    "(`impl Type { ... }`). `Self` refers to the target type inside an "
    "`impl` block. Call a method as `value.method()`, a static function "
    "as `Type.function(...)`.\n\n"
    "```mah\nstruct Point { x, y }\nimpl Point {\n\tfn new(x, y) { Self { x: x, y: y } }\n}\n```",
    "for": "Used in `impl Trait for Type`. (Reserved for the future `for` loop.)",
}

BUILTIN_DOCS = {
    "print": "Print zero or more values' `to_string`, joined by `sep` "
    "(default: a single space), followed by `end` (default: a newline). "
    "`print()` alone just prints `end`.\n\n"
    "`print(a, b, ..., sep: \" \", end: \"\\n\")`",
    "input": "Read an integer from standard input.\n\n`input()`",
    "sin": "Sine of a number, in radians.\n\n`sin(x)`",
    "cos": "Cosine of a number, in radians.\n\n`cos(x)`",
    "sleep_async": "Waits `ms` milliseconds -- the first genuinely "
    "scheduled (suspend-capable) operation. Called bare, it just blocks, "
    "exactly like an ordinary synchronous call -- no `.await` needed. "
    "`detach sleep_async(ms)` is the exception: it hands back a "
    "still-pending `Promise` (`Promise.Pending` / `Promise.Settled "
    "{ value }`, a real built-in enum) instead of blocking, for you to "
    "`.await` whenever you're ready.\n\n`sleep_async(ms)`",
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
COMPLETION_STRUCT = 22   # LSP CompletionItemKind.Struct
COMPLETION_ENUM = 13     # LSP CompletionItemKind.Enum
COMPLETION_ENUM_MEMBER = 20  # LSP CompletionItemKind.EnumMember
COMPLETION_FILE = 17     # LSP CompletionItemKind.File
COMPLETION_FOLDER = 19   # LSP CompletionItemKind.Folder
COMPLETION_METHOD = 2    # LSP CompletionItemKind.Method (M13)
COMPLETION_FIELD = 5     # LSP CompletionItemKind.Field (M13)

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


def _is_await_field(token: Token, tokens: list[Token]) -> bool:
    """True when ``token`` is the ``await`` in a `.await` postfix access
    (M10 async -- see docs/V2_DESIGN.md's M10 milestone). `"await"` is not
    a lexer keyword at all -- it parses as an ordinary `FieldAccess` field
    name (see compiler/ast_nodes.py's M10 note) -- so, mirroring
    `_is_soft_keyword` above, hover has to recognize it positionally: an
    ID token spelled exactly "await" immediately preceded by a `.`."""
    if token.type != TokenType.ID or token.literal != "await":
        return False
    pos = next(
        (i for i, t in enumerate(tokens) if t.position == token.position), None
    )
    if pos is None or pos == 0:
        return False
    return tokens[pos - 1].type == TokenType.DOT


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


def _leading_doc_comment(text: str, position: int) -> str:
    """Collect contiguous ``#`` comment lines directly above a declaration.

    Comments are ignored by the lexer, so they are not in the token stream;
    we read them straight from the source. A blank or code line ends the
    doc block. ``position`` is either a declaration's own position, or (for
    hover on a *use* site) that use's resolved declaration position.
    """
    line_start = text.rfind("\n", 0, position) + 1
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
        return _leading_doc_comment(text, decl_token.position) if text is not None else ""

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


def _import_string_context(text: str, offset: int):
    """If `offset` sits inside a (possibly still being typed, unclosed)
    string literal that immediately follows `import` or `from` on the
    same line, return `(partial_path_typed_so_far, quote_offset)` --
    `quote_offset` is the position of the opening `"`. Returns `None`
    otherwise. Deliberately a simple same-line textual scan, NOT based on
    `preprocessor.py`'s own import-directive matching (which requires a
    complete, well-formed, closed string token) -- completion needs to
    work at the exact moment someone is mid-typing an unclosed path, which
    the preprocessor's own machinery isn't designed to tolerate."""
    line_start = text.rfind("\n", 0, offset) + 1
    line_end = text.find("\n", offset)
    if line_end == -1:
        line_end = len(text)
    line = text[line_start:line_end]
    cursor_col = offset - line_start
    quote_col = line.rfind('"', 0, cursor_col)
    if quote_col == -1:
        return None
    before = line[:quote_col].rstrip()
    if not (before.endswith("import") or before.endswith("from")):
        return None
    partial = line[quote_col + 1 : cursor_col]
    if '"' in partial:
        return None  # already past a closed string
    return partial, line_start + quote_col + 1


def _import_path_completions(text: str, path: Optional[str], offset: int):
    """Completion items for `.mh` files/directories, when the cursor is
    inside an `import "..."` / `from "..."` string -- see
    `_import_string_context`. Returns `None` (meaning "not this kind of
    completion, caller should fall through to normal completion") when
    the cursor isn't in such a string; returns a (possibly empty) list
    otherwise."""
    ctx = _import_string_context(text, offset)
    if ctx is None:
        return None
    partial, _quote_offset = ctx
    base_dir = os.path.dirname(path) if path else os.getcwd()
    typed_dir, _sep, typed_prefix = partial.rpartition("/")
    search_dir = os.path.join(base_dir, typed_dir) if typed_dir else base_dir
    try:
        entries = os.listdir(search_dir)
    except OSError:
        return []
    items = []
    for entry in sorted(entries):
        if not entry.startswith(typed_prefix):
            continue
        full = os.path.join(search_dir, entry)
        if os.path.isdir(full):
            items.append({"label": entry, "kind": COMPLETION_FOLDER, "detail": "directory"})
        elif entry.endswith(".mh"):
            items.append(
                {
                    "label": entry[: -len(".mh")],
                    "kind": COMPLETION_FILE,
                    "detail": entry,
                }
            )
    return items


def _member_access_context(text: str, offset: int):
    """M13: detect `receiver.partial` immediately before the cursor, for
    method/field completion (`r.`, `Type.`, `Trait.`, `self.`, `5.` ...) --
    see the M13 spec's 'Completion' section. Returns `(receiver, dot_offset)`
    or `None` when the cursor isn't right after a `.` at all. `receiver` may
    be empty (e.g. right after `)` or a string literal -- there's no
    identifier immediately before the dot)."""
    i = offset
    while i > 0 and (text[i - 1].isalnum() or text[i - 1] in "_$"):
        i -= 1
    if i == 0 or text[i - 1] != ".":
        return None
    dot_offset = i - 1
    k = dot_offset
    while k > 0 and (text[k - 1].isalnum() or text[k - 1] in "_$"):
        k -= 1
    receiver = text[k:dot_offset]
    return receiver, dot_offset


def _member_access_mode(receiver: str, combined_dot: int, resolver):
    """M13: decide what `receiver.` means at `combined_dot` -- returns
    `(mode, target)` where `mode` is `"instance"` (methods callable on a
    VALUE of type `target`, plus fields for a struct), `"type"` (every fn
    on the type `target`, static included, plus variants for an enum),
    `"trait"` (every fn of the trait `target`), or `"unknown"` (no idea --
    every method name anywhere). See the M13 spec's 'Decide the receiver'
    step."""
    if receiver == "self":
        best = None
        for start, end, kind, name in resolver.member_block_ranges:
            if start <= combined_dot <= end:
                if best is None or (end - start) < (best[1] - best[0]):
                    best = (start, end, kind, name)
        if best is not None:
            _start, _end, kind, name = best
            return ("instance", name) if kind == "impl" else ("trait", name)
        return ("unknown", None)
    if receiver in resolver.trait_decls:
        return ("trait", receiver)
    if receiver in resolver.struct_decls or receiver in resolver.enum_decls or receiver in BUILTIN_TYPE_NAMES:
        return ("type", receiver)
    if receiver.isdigit():
        return ("instance", "Number")
    if receiver:
        best_symbol = None
        for symbol in resolver.position_index.values():
            if symbol.name != receiver or symbol.decl_position > combined_dot:
                continue
            if best_symbol is None or symbol.decl_position > best_symbol.decl_position:
                best_symbol = symbol
        if best_symbol is not None and best_symbol.type_hint is not None:
            return ("instance", best_symbol.type_hint)
    return ("unknown", None)


def _method_completion_item(resolver, cand, name: str, fninfo: dict) -> dict:
    kind = COMPLETION_METHOD if fninfo.get("is_method") else COMPLETION_FUNCTION
    return {"label": name, "kind": kind, "detail": _method_signature_lines(resolver, cand, name)}


def _dedupe_completion_items(items: list[dict]) -> list[dict]:
    """M13: dedupe by label (first wins), then sort by label for a
    deterministic order -- see the M13 spec's 'Completion' section."""
    seen: set = set()
    deduped = []
    for item in items:
        if item["label"] in seen:
            continue
        seen.add(item["label"])
        deduped.append(item)
    deduped.sort(key=lambda it: it["label"])
    return deduped


def _instance_member_items(resolver, type_name: str) -> list[dict]:
    """M13: every METHOD (not static fn) callable on a value of type
    `type_name` (inherent + every trait, natives included), plus -- for a
    struct -- its field names (callable via M13 part 1's `p.f(args)`)."""
    items = []
    entry = resolver.impls.get(type_name, {"inherent": {}, "traits": {}})
    for method_name, fninfo in entry["inherent"].items():
        if fninfo.get("is_method"):
            items.append(_method_completion_item(resolver, ("impl", type_name, None), method_name, fninfo))
    for trait_name, fns in entry["traits"].items():
        for method_name, fninfo in fns.items():
            if fninfo.get("is_method"):
                items.append(_method_completion_item(resolver, ("impl", type_name, trait_name), method_name, fninfo))
    if type_name in resolver.struct_decls:
        for field_name in resolver.struct_decls[type_name]:
            items.append({"label": field_name, "kind": COMPLETION_FIELD, "detail": f"field of {type_name}"})
    return _dedupe_completion_items(items)


def _type_member_items(resolver, type_name: str) -> list[dict]:
    """M13: every fn (method or static) on the TYPE `type_name` (inherent +
    every trait, natives included), plus -- for an enum -- every variant
    name."""
    items = []
    entry = resolver.impls.get(type_name, {"inherent": {}, "traits": {}})
    for method_name, fninfo in entry["inherent"].items():
        items.append(_method_completion_item(resolver, ("impl", type_name, None), method_name, fninfo))
    for trait_name, fns in entry["traits"].items():
        for method_name, fninfo in fns.items():
            items.append(_method_completion_item(resolver, ("impl", type_name, trait_name), method_name, fninfo))
    if type_name in resolver.enum_decls:
        for variant_name in resolver.enum_decls[type_name]:
            items.append(
                {"label": variant_name, "kind": COMPLETION_ENUM_MEMBER, "detail": f"variant of {type_name}"}
            )
    return _dedupe_completion_items(items)


def _trait_member_items(resolver, trait_name: str) -> list[dict]:
    """M13: every fn of `trait_name` (methods AND static functions)."""
    items = []
    for method_name, info in resolver.trait_decls.get(trait_name, {}).items():
        kind = COMPLETION_METHOD if info.get("is_method") else COMPLETION_FUNCTION
        items.append(
            {
                "label": method_name,
                "kind": kind,
                "detail": _method_signature_lines(resolver, ("trait", trait_name), method_name),
            }
        )
    return _dedupe_completion_items(items)


def _unknown_receiver_method_items(resolver) -> list[dict]:
    """M13: every method name (`is_method` True) across every entry of
    `resolver.impls`, deduplicated by name -- the receiver type isn't known
    at all, so this is every possibility; `detail` lists the owning types
    (user types first, then built-ins, each group alphabetical) so the
    editor at least shows what it could be."""
    owners: dict = {}
    for type_name, entry in resolver.impls.items():
        names: set = set()
        for method_name, fninfo in entry["inherent"].items():
            if fninfo.get("is_method"):
                names.add(method_name)
        for fns in entry["traits"].values():
            for method_name, fninfo in fns.items():
                if fninfo.get("is_method"):
                    names.add(method_name)
        for method_name in names:
            owners.setdefault(method_name, set()).add(type_name)
    items = []
    for method_name in sorted(owners):
        types = sorted(owners[method_name], key=lambda t: (not _is_user_type_name(resolver, t), t))
        items.append({"label": method_name, "kind": COMPLETION_METHOD, "detail": ", ".join(types)})
    return items


def _member_access_completions(text: str, path: Optional[str], offset: int):
    """M13: member-access completion (`r.`, `Type.`, `Trait.`, `self.` ...)
    -- see the M13 spec's 'Completion' section. Returns a list of
    completion items (possibly empty), returned EXCLUSIVELY (no keywords/
    symbols mixed in), when the cursor sits in this context; `None` when it
    doesn't (caller falls through to ordinary completion)."""
    ctx = _member_access_context(text, offset)
    if ctx is None:
        return None
    receiver, dot_offset = ctx

    result = _resolve_for_navigation(text, path)
    if result is None:
        return []
    pp, resolver, _tokens = result
    combined_dot = pp.entry_to_combined(dot_offset)
    if combined_dot is None:
        return []

    mode, target = _member_access_mode(receiver, combined_dot, resolver)
    if mode == "unknown":
        return _unknown_receiver_method_items(resolver)
    if mode == "trait":
        return _trait_member_items(resolver, target)
    if mode == "type":
        return _type_member_items(resolver, target)
    return _instance_member_items(resolver, target)


def _resolver_symbol_completion_item(symbol) -> dict:
    """Completion item for a `compiler/resolve.py` `Symbol` (variable/
    parameter/function binding) -- parallel to hover's kind labels."""
    kind = COMPLETION_FUNCTION if symbol.kind == "fn" else COMPLETION_VARIABLE
    detail = {"let": "variable", "fn": "function", "param": "parameter", "binding": "binding"}.get(
        symbol.kind, symbol.kind
    )
    return {"label": demangle_message(symbol.name), "kind": kind, "detail": detail}


def get_completions(
    text: str,
    path: Optional[str] = None,
    line: Optional[int] = None,
    character: Optional[int] = None,
) -> list[dict]:
    """Completion items for the cursor position, built on the same
    resolver-based foundation as hover/go-to-definition/rename (see
    `compiler/resolve.py`'s `position_index`/`type_position_index`) rather
    than a second, independent token scan.

    Deliberate scope limitation: symbol completion (item 4 below) offers
    every declared name in the file, not a precisely lexically-scoped
    subset for the exact cursor position -- `Resolver` doesn't retain
    scope-interval information after resolving, only a flat position ->
    Symbol index (see docs/NEXT_PHASES.md for the broader type-system work
    a real scope-interval model would likely piggyback on). This means
    completion can slightly over-suggest (a name declared later in the
    file, or in a sibling branch) rather than ever under-suggest -- an
    accepted tradeoff, not a bug.
    """
    if line is not None and character is not None:
        offset = position_to_offset(text, line, character)

        # 1. Import path string: an entirely different completion context,
        # returned exclusively (never merged with keywords/symbols/etc).
        import_items = _import_path_completions(text, path, offset)
        if import_items is not None:
            return import_items

        # 2. Namespace member completion (`math.` -> square/cube/...):
        # exclusive too, reusing the preprocessor's own already-computed
        # import metadata (not a second re-tokenization).
        ns_name = _namespace_prefix_at(text, offset)
        if ns_name is not None:
            pp = preprocess(path, text)
            for ns in pp.entry_namespaces:
                if ns.name != ns_name or ns.resolved is None:
                    continue
                return [
                    {"label": member, "kind": COMPLETION_VARIABLE, "detail": f"(from {os.path.basename(ns.resolved)})"}
                    for member in sorted(pp.exported_names(ns.resolved))
                ]
            # `ns_name` isn't an actual namespace import -- not this
            # context after all; fall through (M13: it may still be
            # `receiver.partial` member-access completion, step 2b below).

        # 2b. M13: method/field member-access completion (`r.`, `Type.`,
        # `Trait.`, `self.`, `5.` ...) -- exclusive too, see
        # `_member_access_completions`.
        member_items = _member_access_completions(text, path, offset)
        if member_items is not None:
            return member_items

    items: list[dict] = []

    # 3. Keywords + builtins -- always offered.
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

    # 4. Every declared variable/function/parameter/pattern-binding in the
    # file, plus struct/enum type names -- only available when the file
    # resolves cleanly (same limitation hover/go-to-definition/rename
    # already have -- see `_resolve_for_navigation`'s docstring).
    result = _resolve_for_navigation(text, path)
    if result is not None:
        _pp, resolver, _tokens = result
        seen_decls = set()
        for symbol in resolver.position_index.values():
            if symbol.decl_position in seen_decls:
                continue
            seen_decls.add(symbol.decl_position)
            items.append(_resolver_symbol_completion_item(symbol))
        for struct_name in resolver.struct_decls:
            items.append({"label": struct_name, "kind": COMPLETION_STRUCT, "detail": "struct"})
        for enum_name in resolver.enum_decls:
            if enum_name in ("Option", "Promise"):
                # Built-ins: Option is reached via `some`/`none` keywords,
                # Promise via `detach`/`sleep_async` -- not something users
                # normally type the bare type name of.
                continue
            items.append({"label": enum_name, "kind": COMPLETION_ENUM, "detail": "enum"})

    # 5. Namespaces and flat-imported exported names -- sourced directly
    # from the preprocessor's own import metadata (works even when the
    # rest of the file doesn't resolve cleanly, since imports are
    # processed before the real parser/resolver ever run).
    pp = preprocess(path, text)
    for ns in pp.entry_namespaces:
        items.append(
            {
                "label": ns.name,
                "kind": COMPLETION_MODULE,
                "detail": f"namespace (from {os.path.basename(ns.resolved)})" if ns.resolved else "namespace",
            }
        )
    for imp in pp.entry_imports:
        if imp.resolved is None:
            continue
        for name in sorted(pp.exported_names(imp.resolved)):
            items.append(
                {
                    "label": name,
                    "kind": COMPLETION_FUNCTION,
                    "detail": f"(from {os.path.basename(imp.resolved)})",
                }
            )

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
    covers variables/parameters/function bindings, struct/enum type names,
    and enum variant names, and *is* allowed to describe a symbol whose
    declaration lives in an imported file (unlike rename, which refuses
    cross-file symbols outright) -- it just notes where the declaration
    actually lives, and (for variables/functions/params/bindings) shows
    that declaration's own leading doc comment and demangled display name.
    M11 adds struct/enum *field* names too, but only in declarations,
    literals, and explicit (non-shorthand) patterns -- plain field
    *access* (`p.x`) still is not covered, deliberately: without a real
    type system there's no sound way to know what struct shape an
    arbitrary expression's value holds, so a field named `x` on `p` isn't
    necessarily the same `x` -- see docs/NEXT_PHASES.md's "Struct/enum/field
    rename" section. M13 adds *method* names (`_method_at_position`, tried
    before the type-namespace fallback below) -- both call sites
    (`p.m(...)`/`Type.m(...)`/`Trait.m(x, ...)`) and declaration sites
    (a trait's own `fn`, an impl's own `fn`), using the resolver's
    best-effort, purely syntactic type hints (`compiler/resolve.py`'s
    `method_call_index`) to narrow a dynamic call's candidate list when
    possible, and listing every possibility (with a note that the receiver
    type isn't known statically) when it can't."""
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
    elif token.type is TokenType.ID and _is_await_field(token, tokens):
        value = f"**keyword** `.{token.literal}`\n\n" + KEYWORD_DOCS.get(token.literal, "")
    elif token.type is TokenType.NUMBER:
        value = f"**number** `{token.literal}`"
    elif token.type is TokenType.STRING:
        value = f"**string** `{token.literal}`"
    elif token.type is TokenType.ID:
        found = _symbol_at_position(text, line, character, path)
        if found is not None:
            pp, _resolver, symbol = found
            kind_label = {
                "let": "variable",
                "fn": "function",
                "param": "parameter",
                "binding": "binding",
            }.get(symbol.kind, symbol.kind)
            display_name = demangle_message(symbol.name)
            value = f"**{kind_label}** `{display_name}`"
            decl_path, decl_offset = pp.map_to_source(symbol.decl_position)
            doc_source = text if decl_path == pp.entry_path else pp.files.get(decl_path, "")
            doc = _leading_doc_comment(doc_source, decl_offset) if doc_source else ""
            if doc:
                value += f"\n\n{doc}"
            if decl_path != pp.entry_path:
                value += f"\n\n*declared in `{os.path.basename(decl_path)}`*"
        else:
            method_found = _method_at_position(text, line, character, path)
            if method_found is not None:
                mpp, mresolver, mkind, mpayload = method_found
                value = _method_hover_value(mpp, mresolver, mkind, mpayload, text)
            else:
                type_found = _type_symbol_at_position(text, line, character, path)
                if type_found is not None:
                    value = _type_hover_value(*type_found)
                else:
                    field_found = _field_symbol_at_position(text, line, character, path)
                    if field_found is None:
                        return None
                    _resolver, kind, payload = field_found
                    if kind == "struct_field":
                        struct_name, field_name = payload
                        value = f"**field** `{field_name}` of struct `{struct_name}`"
                    else:
                        enum_name, variant_name, field_name = payload
                        value = f"**field** `{field_name}` of `{enum_name}.{variant_name}`"
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
#   - struct/enum type names and enum variant names ARE now covered by
#     go-to-definition and hover, via a parallel index
#     (`Resolver.type_position_index`/`struct_decl_positions`/
#     `enum_decl_positions`/`enum_variant_decl_positions` -- a separate
#     namespace from the ordinary variable/function `position_index`).
#     Struct/enum *field* names (e.g. `x`/`y` in `Point { x, y }`) still are
#     not -- renaming/navigating those is a distinct, larger future piece
#     of work.
#   - rename is single-file only: a symbol whose declaration or any
#     reference falls outside the entry file's own text segment (i.e. it
#     touches an inlined import) is refused outright rather than performed
#     partially -- see `get_rename_edits` below.
#   - document-symbols/code-actions are NOT revived by this milestone;
#     `get_document_symbols`/`get_code_actions` above are left exactly as
#     they were found (disabled/non-advertised, still referencing the old
#     `TokenType` spellings) -- completion, however, now IS revived (see
#     `get_completions` below), rebuilt on the resolver rather than the old
#     dead token-scanning helpers.


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


def _type_symbol_at_position(text: str, line: int, character: int, path: Optional[str]):
    """Like `_symbol_at_position`, but looks up the resolver's struct/enum/
    variant/trait namespace (`type_position_index`) instead of the ordinary
    variable/function one (`position_index`) -- see
    `compiler/resolve.py`'s `type_position_index` docstring. Returns
    `(resolver, kind, payload)` where `kind` is `"struct"`/`"enum"`/
    `"variant"`/`"trait"` (M12) and `payload` is the struct/enum/trait name
    (a str) or, for a variant, an `(enum_name, variant_name)` tuple -- or
    `None`."""
    result = _resolve_for_navigation(text, path)
    if result is None:
        return None
    _pp, resolver, tokens = result
    combined_offset = _combined_offset_for_position(_pp, text, line, character)
    if combined_offset is None:
        return None
    token = _token_at_offset(tokens, combined_offset)
    if token is None or token.type is not TokenType.ID:
        return None
    entry = resolver.type_position_index.get(token.position)
    if entry is None:
        return None
    kind = entry[0]
    if kind == "variant":
        return resolver, "variant", (entry[1], entry[2])
    return resolver, kind, entry[1]


def _field_symbol_at_position(text: str, line: int, character: int, path: Optional[str]):
    """Like `_type_symbol_at_position`, but looks up the resolver's
    struct/enum FIELD namespace (`field_position_index`) instead --
    field names in declarations/literals/explicit patterns only, never
    plain field access (`p.x`) -- see `compiler/resolve.py`'s
    `field_position_index` docstring. Returns `(resolver, kind, payload)`
    where `kind` is `"struct_field"`/`"variant_field"` and `payload` is
    `(struct_name, field_name)` or `(enum_name, variant_name, field_name)`
    respectively -- or `None`."""
    result = _resolve_for_navigation(text, path)
    if result is None:
        return None
    _pp, resolver, tokens = result
    combined_offset = _combined_offset_for_position(_pp, text, line, character)
    if combined_offset is None:
        return None
    token = _token_at_offset(tokens, combined_offset)
    if token is None or token.type is not TokenType.ID:
        return None
    entry = resolver.field_position_index.get(token.position)
    if entry is None:
        return None
    if entry[0] == "struct_field":
        return resolver, "struct_field", (entry[1], entry[2])
    return resolver, "variant_field", (entry[1], entry[2], entry[3])


def _is_user_type_name(resolver, name: str) -> bool:
    """M13: mirrors `Resolver._is_user_type` (a user-declared struct, or a
    user-declared enum -- built-in enums Option/Promise don't count) --
    reimplemented here rather than reaching into that resolver-private
    helper from across the module boundary."""
    return name in resolver.struct_decls or (name in resolver.enum_decls and name not in BUILTIN_TYPE_NAMES)


def _method_at_position(text: str, line: int, character: int, path: Optional[str]):
    """M13: like `_type_symbol_at_position`, but looks up the resolver's
    method-name namespaces (`method_call_index`/`method_decl_index` --
    see `compiler/resolve.py`'s module docstring, 'method indexes') instead
    of the struct/enum/variant/trait one. Returns `(pp, resolver, kind,
    payload)` where `kind` is `"call"` (payload = the `method_call_index`
    info dict) or `"decl"` (payload = the `method_decl_index` tuple) -- or
    `None`."""
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
    call_info = resolver.method_call_index.get(token.position)
    if call_info is not None:
        return pp, resolver, "call", call_info
    decl_info = resolver.method_decl_index.get(token.position)
    if decl_info is not None:
        return pp, resolver, "decl", decl_info
    return None


def _method_fninfo(resolver, cand, name: str):
    """M13: the fninfo dict an `("impl", T, tr_or_None)` hover/completion
    candidate refers to -- mirrors `Resolver._fninfo_for_impl_candidate`
    (kept as a separate, LSP-side copy since it tolerates a missing entry,
    returning `None` instead of raising, for defensive rendering)."""
    _kind, type_name, trait_name = cand
    entry = resolver.impls.get(type_name, {"inherent": {}, "traits": {}})
    if trait_name is None:
        return entry["inherent"].get(name)
    return entry["traits"].get(trait_name, {}).get(name)


def _method_signature_lines(resolver, cand, name: str) -> str:
    """M13: one-line description of a method/static-fn hover/completion
    candidate -- `("impl", T, tr_or_None)` or `("trait", Tr)` -- see the
    M13 spec's 'LSP' section."""
    if cand[0] == "impl":
        _kind, type_name, trait_name = cand
        fninfo = _method_fninfo(resolver, cand, name)
        params = ", ".join(fninfo["param_names"]) if fninfo else ""
        if trait_name is None:
            return f"impl {type_name}: fn {name}({params})"
        line = f"impl {trait_name} for {type_name}: fn {name}({params})"
        if fninfo and fninfo.get("native"):
            line += "  # built-in"
        return line
    _kind, trait_name = cand
    info = resolver.trait_decls.get(trait_name, {}).get(name, {})
    params = ", ".join(info.get("params", []))
    line = f"trait {trait_name}: fn {name}({params})"
    if info.get("default_slot") is not None:
        line += " { ... }"
    return line


def _method_is_method(resolver, cand, name: str) -> bool:
    """M13: whether `cand` (see `_method_signature_lines`) is a method
    (`value.m()`) rather than a static function (`Type.f()`)."""
    if cand[0] == "impl":
        fninfo = _method_fninfo(resolver, cand, name)
        return bool(fninfo and fninfo.get("is_method"))
    _kind, trait_name = cand
    info = resolver.trait_decls.get(trait_name, {}).get(name, {})
    return bool(info.get("is_method"))


def _method_decl_position(resolver, cand, name: str) -> Optional[int]:
    """M13: the declaration position `cand` (see `_method_signature_lines`)
    refers to -- `None` for a native (no user-written declaration)."""
    if cand[0] == "impl":
        fninfo = _method_fninfo(resolver, cand, name)
        return fninfo.get("decl_position") if fninfo else None
    _kind, trait_name = cand
    info = resolver.trait_decls.get(trait_name, {}).get(name, {})
    return info.get("decl_position")


def _method_hover_value(pp, resolver, kind: str, payload, text: str) -> str:
    """M13: render hover markdown for a method/static-fn call site or
    declaration -- see `_method_at_position` and the M13 spec's 'LSP'
    section."""
    if kind == "decl":
        entry_kind = payload[0]
        if entry_kind == "impl":
            _kind, type_name, trait_name, method_name = payload
            header = f"**method** `{method_name}` of `{type_name}`"
            if trait_name is not None:
                header += f" -- implements `{trait_name}.{method_name}`"
            sig = _method_signature_lines(resolver, ("impl", type_name, trait_name), method_name)
            return f"{header}\n\n```mah\n{sig}\n```"
        # entry_kind == "trait"
        _kind, trait_name, method_name = payload
        info = resolver.trait_decls.get(trait_name, {}).get(method_name, {})
        req = "required" if info.get("default_slot") is None else "default"
        header = f"**trait method** `{trait_name}.{method_name}` ({req})"
        sig = _method_signature_lines(resolver, ("trait", trait_name), method_name)
        value = f"{header}\n\n```mah\n{sig}\n```"
        implementers = sorted(
            type_name
            for type_name, entry in resolver.impls.items()
            if trait_name in entry["traits"] and _is_user_type_name(resolver, type_name)
        )
        if implementers:
            value += f"\n\nImplemented by: {', '.join(implementers)}"
        return value

    # kind == "call"
    info = payload
    name = info["name"]
    candidates = info["candidates"]
    receiver_type = info["receiver_type"]

    if not candidates:
        if (
            receiver_type is not None
            and receiver_type in resolver.struct_decls
            and name in resolver.struct_decls[receiver_type]
        ):
            return f"**field** `{name}` of struct `{receiver_type}` (called as a function)"
        return f"**method** `{name}`\n\nNo known implementation."

    all_static = all(not _method_is_method(resolver, c, name) for c in candidates)
    header = f"**function** `{name}`" if all_static else f"**method** `{name}`"
    if receiver_type is not None:
        header += f" on `{receiver_type}`"

    shown = candidates[:8]
    lines = [_method_signature_lines(resolver, c, name) for c in shown]
    if len(candidates) > 8:
        lines.append(f"... and {len(candidates) - 8} more")
    block = "\n".join(lines)
    prefix = ""
    if receiver_type is None and len(candidates) >= 2:
        prefix = "*Receiver type isn't known statically -- possible implementations:*\n\n"
    value = f"{header}\n\n{prefix}```mah\n{block}\n```"

    if len(candidates) == 1:
        decl_pos = _method_decl_position(resolver, candidates[0], name)
        if decl_pos is not None:
            decl_path, decl_offset = pp.map_to_source(decl_pos)
            doc_source = text if decl_path == pp.entry_path else pp.files.get(decl_path, "")
            doc = _leading_doc_comment(doc_source, decl_offset) if doc_source else ""
            if doc:
                value += f"\n\n{doc}"
    return value


def _type_hover_value(resolver, kind: str, payload) -> str:
    """Render hover markdown for a struct/enum/variant/trait -- see
    `_type_symbol_at_position`."""
    def _shape(name: str, fields: list) -> str:
        return name if not fields else f"{name} {{ {', '.join(fields)} }}"

    if kind == "trait":
        # M12: read straight from `resolver.trait_decls[name]` -- {method
        # name -> {"params": [...], "is_method": bool, "default_slot": int
        # | None}} -- one `fn` line per method, with ` { ... }` appended
        # for a method that has a default body (default_slot is not None).
        name = payload
        methods = resolver.trait_decls.get(name, {})
        lines = [f"trait {name} {{"]
        for method_name, info in methods.items():
            params = ", ".join(info["params"])
            suffix = " { ... }" if info["default_slot"] is not None else ""
            lines.append(f"\tfn {method_name}({params}){suffix}")
        lines.append("}")
        body = "\n".join(lines)
        return f"**trait** `{name}`\n\n```mah\n{body}\n```"
    if kind == "struct":
        name = payload
        fields = resolver.struct_decls.get(name, [])
        return f"**struct** `{name}`\n\n```mah\nstruct {_shape(name, fields)}\n```"
    if kind == "enum":
        name = payload
        variants = resolver.enum_decls.get(name, {})
        variant_text = ", ".join(_shape(v, f) for v, f in variants.items())
        return f"**enum** `{name}`\n\n```mah\nenum {name} {{ {variant_text} }}\n```"
    enum_name, variant_name = payload
    fields = resolver.enum_decls.get(enum_name, {}).get(variant_name, [])
    return (
        f"**enum variant** `{enum_name}.{variant_name}`\n\n"
        f"```mah\n{_shape(variant_name, fields)}\n```"
    )


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


def _location_for_combined_pos(pp, text: str, pos: int, fallback_name: str) -> dict:
    """Build a `{"path", "range"}` go-to-definition location for a
    combined-text position `pos`. `fallback_name` supplies the span's
    length when `pos` doesn't sit on a real identifier's first character
    (via `_identifier_length_at`) -- shouldn't happen for a validly
    resolved declaration position, but this is a navigation feature, not
    the compiler itself, so it degrades gracefully. Factored out of what
    used to be four independent, near-identical copies of this exact
    "map to source, pick the buffer vs. an imported file's own text,
    measure the identifier, build the range" dance (the ordinary-symbol,
    type-name, field-name, and -- M13 -- method-name branches of
    `get_definition`)."""
    decl_path, decl_offset = pp.map_to_source(pos)
    source = text if decl_path == pp.entry_path else pp.files.get(decl_path, "")
    length = _identifier_length_at(source, decl_offset) or len(fallback_name)
    if decl_path == pp.entry_path:
        return {"path": None, "range": make_range(text, decl_offset, decl_offset + length)}
    # Declaration lives in an inlined import -- still a valid jump for
    # go-to-definition (unlike rename, which refuses cross-file symbols
    # outright, see `get_rename_edits`): build the range against that
    # file's own source text.
    return {"path": decl_path, "range": make_range(source, decl_offset, decl_offset + length)}


def get_definition(
    text: str, line: int, character: int, path: Optional[str] = None
) -> Optional[dict] | list[dict]:
    """Resolve the declaration for the variable/parameter/function symbol
    under the cursor, using the resolver's real symbol table.

    Returns ``{"path": <abs path or None>, "range": <lsp range>}`` where a
    ``path`` of ``None`` means "the current document" -- the shape
    `lsp/server.py`'s `_on_textDocument_definition` handler expects. M13:
    for a method call site with 2+ candidate implementations (the receiver
    type isn't known statically -- see `compiler/resolve.py`'s
    `method_call_index`), this returns a LIST of such dicts instead --
    `_on_textDocument_definition` responds with a list of LSP Locations in
    that case.

    Covers variables/parameters/function bindings, and (via
    `_type_symbol_at_position`/`type_position_index`) struct/enum type
    names and enum variant names. M11 adds struct/enum *field* names too
    (via `_field_symbol_at_position`/`field_position_index`), but only in
    declarations, literals, and explicit (non-shorthand) patterns -- plain
    field *access* (`p.x`) is still never covered, deliberately (unsound
    without a real type system -- see docs/NEXT_PHASES.md's "Struct/enum/
    field rename" section). M13 adds *method* names (`_method_at_position`)
    -- both call sites (jumping to one or every candidate implementation)
    and declaration sites (only when the declaration itself is an impl
    method that implements a trait method -- jumps to that trait method's
    own declaration; a trait's own required/default method, or an impl's
    own inherent method, has nowhere further to jump). Returns ``None``
    when the file doesn't resolve cleanly, the cursor isn't on an
    identifier, or that identifier never resolved to anything (a keyword,
    a field-access use, ...).

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
    if found is not None:
        pp, _resolver, symbol = found
        return _location_for_combined_pos(pp, text, symbol.decl_position, symbol.name)

    # Not an ordinary variable/function symbol -- try the method-name
    # namespace next (M13), before the struct/enum/variant/field ones
    # below (a method name is never also a type/field name).
    method_found = _method_at_position(text, line, character, path)
    if method_found is not None:
        pp, resolver, kind, payload = method_found
        if kind == "call":
            name = payload["name"]
            positions = []
            seen: set = set()
            for cand in payload["candidates"]:
                decl_pos = _method_decl_position(resolver, cand, name)
                if decl_pos is None or decl_pos in seen:
                    continue
                seen.add(decl_pos)
                positions.append(decl_pos)
            if len(positions) == 1:
                return _location_for_combined_pos(pp, text, positions[0], name)
            if len(positions) >= 2:
                return [_location_for_combined_pos(pp, text, pos, name) for pos in positions]
            # 0 decl positions (every candidate is native, or there are no
            # candidates at all) -- fall through to the branches below,
            # which will find nothing for a method-name token either, so
            # this ends up returning None, same as "not found".
        else:
            entry_kind = payload[0]
            if entry_kind == "impl":
                _kind, _type_name, trait_name, method_name = payload
                if trait_name is not None:
                    decl_pos = resolver.trait_decls.get(trait_name, {}).get(method_name, {}).get("decl_position")
                    if decl_pos is not None:
                        return _location_for_combined_pos(pp, text, decl_pos, method_name)
            return None

    # Not a type/variant name either -- try the struct/enum/variant
    # namespace instead (see `_type_symbol_at_position`).
    type_found = _type_symbol_at_position(text, line, character, path)
    if type_found is not None:
        resolver, kind, payload = type_found
        result = _resolve_for_navigation(text, path)
        if result is None:
            return None
        pp, _resolver2, _tokens = result
        if kind == "struct":
            decl_pos = resolver.struct_decl_positions.get(payload)
            name_for_fallback_length = payload
        elif kind == "enum":
            decl_pos = resolver.enum_decl_positions.get(payload)
            name_for_fallback_length = payload
        elif kind == "trait":
            # M12: fallback-length name = payload (the trait's own name).
            decl_pos = resolver.trait_decl_positions.get(payload)
            name_for_fallback_length = payload
        else:
            enum_name, variant_name = payload
            decl_pos = resolver.enum_variant_decl_positions.get(enum_name, {}).get(variant_name)
            name_for_fallback_length = variant_name
        if decl_pos is None:
            return None
        return _location_for_combined_pos(pp, text, decl_pos, name_for_fallback_length)

    # Not a type/variant name either -- try the struct/enum FIELD namespace
    # (M11). Field declarations are always single-file today (structs/enums
    # can't be exported/imported at all -- see get_rename_edits's module
    # notes), but the code shape below is kept identical to the type-name
    # case above for consistency.
    field_found = _field_symbol_at_position(text, line, character, path)
    if field_found is None:
        return None
    resolver, kind, payload = field_found
    result = _resolve_for_navigation(text, path)
    if result is None:
        return None
    pp, _resolver2, _tokens = result
    if kind == "struct_field":
        struct_name, field_name = payload
        decl_pos = resolver.struct_field_decl_positions.get((struct_name, field_name))
    else:
        enum_name, variant_name, field_name = payload
        decl_pos = resolver.enum_variant_field_decl_positions.get((enum_name, variant_name, field_name))
    if decl_pos is None:
        # Shouldn't happen for a validly-resolved program -- defensive only.
        return None
    return _location_for_combined_pos(pp, text, decl_pos, field_name)


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


def _find_workspace_root(path: Optional[str]) -> Optional[str]:
    """Walk upward from `path`'s directory looking for a project marker
    (`.git` or `Makefile`), mirroring `editors/nvim/ftplugin/mah.lua`'s
    own root-detection heuristic (`vim.fs.find({"mah.lang", ".git",
    "Makefile"}, {upward = true, ...})`) for consistency between the two.
    Falls back to the file's own directory if no marker is found up to
    the filesystem root. Returns `None` for a path-less buffer (nothing
    to anchor a workspace search from)."""
    if path is None or path == BUFFER_PATH:
        return None
    current = os.path.dirname(os.path.abspath(path))
    start = current
    while True:
        if os.path.exists(os.path.join(current, ".git")) or os.path.exists(os.path.join(current, "Makefile")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return start
        current = parent


def _find_mh_files(root: str) -> list:
    """Every `*.mh` file under `root`, skipping dot-directories."""
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fname in filenames:
            if fname.endswith(".mh"):
                found.append(os.path.join(dirpath, fname))
    return found


def _build_reverse_import_graph(mh_files: list) -> dict:
    """resolved_path -> set of files that directly import it, discovered
    via the preprocessor's own tolerant scanner/import-matcher -- no real
    lex/parse/resolve needed just to find import edges (see
    preprocessor.py's `scan`/`_match_import`/`_resolve_import`, already
    written for exactly this kind of lightweight directive detection)."""
    from ..preprocessor import scan as pp_scan, _match_import as pp_match_import, _resolve_import as pp_resolve_import

    graph: dict = {}
    for fpath in mh_files:
        try:
            with open(fpath, encoding="utf-8") as f:
                source = f.read()
        except OSError:
            continue
        tokens = pp_scan(source)
        base_dir = os.path.dirname(fpath)
        i = 0
        count = len(tokens)
        while i < count:
            tok = tokens[i]
            if tok.kind == "id" and tok.value == "import":
                directive = pp_match_import(tokens, i)
                if directive is not None:
                    _kind, _ns_tok, str_tok, end_i = directive
                    literal = str_tok.value[1:-1]
                    resolved, exists = pp_resolve_import(base_dir, literal)
                    if exists:
                        graph.setdefault(resolved, set()).add(fpath)
                    i = end_i + 1
                    continue
            i += 1
    return graph


def _rename_variable_cross_file(found, new_name: str, entry_text: str) -> Optional[dict]:
    """Cross-file variable/function/parameter/binding rename: `found` is
    `_symbol_at_position`'s own `(pp, resolver, symbol)` result for the
    CURRENTLY OPEN document, and `entry_text` is that document's own live
    buffer content (which may have unsaved edits, so it's used in place of
    re-reading `pp.entry_path` from disk whenever that file is one of the
    ones scanned -- including the buffer-less/path-less case, where
    `pp.entry_path` is `BUFFER_PATH`, not a real file at all). Scans the
    declaring file plus every file that directly imports it (structs/enums
    can't be exported/imported at all today -- see docs/NEXT_PHASES.md's
    "Cross-file rename" section -- so only this symbol kind ever needs
    this). Refuses (`None`) outright, matching M7's own established safety
    philosophy, the moment ANY relevant file fails to preprocess/parse/
    resolve cleanly -- a partial cross-file rename that silently misses a
    file is worse than refusing entirely."""
    pp, _resolver, symbol = found
    decl_path, decl_offset = pp.map_to_source(symbol.decl_position)

    root = _find_workspace_root(decl_path)
    importers: set = set()
    if root is not None:
        mh_files = _find_mh_files(root)
        graph = _build_reverse_import_graph(mh_files)
        importers = graph.get(decl_path, set())
    files_to_scan = {decl_path} | importers

    all_edits: dict = {}
    seen_positions: set = set()

    for fpath in files_to_scan:
        if fpath == pp.entry_path:
            # The currently open document: use its live buffer text rather
            # than the disk (may be unsaved, or -- for a path-less buffer
            # -- not a real file on disk at all).
            fsource = entry_text
            preprocess_path = None if pp.entry_path == BUFFER_PATH else fpath
        else:
            try:
                with open(fpath, encoding="utf-8") as f:
                    fsource = f.read()
            except OSError:
                return None
            preprocess_path = fpath

        fpp = preprocess(preprocess_path, fsource)
        if fpp.errors:
            return None
        flexer = Lexer(fpp.text)
        fparser = Parser(flexer)
        fprogram = fparser.parse_program()
        if fparser.errors:
            return None
        fresolver = Resolver()
        try:
            fresolver.resolve_program(fprogram)
        except Exception:
            return None

        for fsymbol in fresolver.position_index.values():
            fdecl_path, fdecl_offset = fpp.map_to_source(fsymbol.decl_position)
            if (fdecl_path, fdecl_offset) != (decl_path, decl_offset):
                continue
            for position in [fsymbol.decl_position] + list(fsymbol.references):
                src_path, src_offset = fpp.map_to_source(position)
                key = (src_path, src_offset)
                if key in seen_positions:
                    continue
                seen_positions.add(key)
                source_text = fsource if src_path == fpath else fpp.files.get(src_path, "")
                length = _identifier_length_at(source_text, src_offset) or len(symbol.name)
                all_edits.setdefault(src_path, []).append(
                    {
                        "range": make_range(source_text, src_offset, src_offset + length),
                        "newText": new_name,
                    }
                )

    if not all_edits:
        return None
    return {"changes": all_edits}


def get_rename_edits(
    text: str,
    line: int,
    character: int,
    new_name: str,
    path: Optional[str] = None,
) -> Optional[dict]:
    """Rename the symbol under the cursor -- variable/parameter/function
    (M7, now cross-file, M11), struct/enum type name or enum variant name
    (M11, single-file only), or struct/enum field name in a declaration/
    literal/explicit pattern (M11, single-file only). Tries each in turn,
    falling through to the next on `None`, and returns `None` outright if
    none of them match.

    Returns ``{"changes": {<key>: [TextEdit, ...]}}`` where ``<key>`` is a
    filesystem path (or `preprocessor.BUFFER_PATH` for an unsaved,
    path-less buffer) -- `lsp/server.py`'s rename handler translates every
    key to its own document URI before responding to the client (M11:
    there can now be MORE than one key, for a cross-file variable/function
    rename -- see `_rename_variable_cross_file`). Returns ``None``
    (refusing the rename outright) when:

      - the file doesn't resolve cleanly (no symbol table available at all);
      - the cursor isn't on an identifier that resolved to anything;
      - `new_name` isn't a syntactically valid Mah identifier, or is a
        reserved keyword;
      - (variable/function case) any relevant file (the declaring file, or
        any file that imports it) fails to preprocess/parse/resolve
        cleanly -- see `_rename_variable_cross_file`'s docstring;
      - (struct/enum type/variant/field case) these are always single-file
        today -- structs/enums can't be exported/imported at all (the
        preprocessor's export-detection only recognizes `fn`/`let`) -- so
        there is no cross-file concept to extend here at all.

    Does not check whether `new_name` would collide with an unrelated
    existing binding already in scope -- a known, documented limitation.
    Renaming through plain field *access* (`p.x`) is deliberately never
    attempted -- unsound without a real type system, see
    docs/NEXT_PHASES.md's "Struct/enum/field rename" section.
    """
    if not _is_valid_mah_identifier(new_name) or new_name in KEYWORDS:
        return None

    # 1. Variable/parameter/function/binding -- now cross-file (M11).
    found = _symbol_at_position(text, line, character, path)
    if found is not None:
        return _rename_variable_cross_file(found, new_name, text)

    # 2. Struct/enum type name or enum variant name -- single-file only,
    # since structs/enums can't cross files at all (see docstring above).
    type_found = _type_symbol_at_position(text, line, character, path)
    if type_found is not None:
        resolver, kind, payload = type_found
        result = _resolve_for_navigation(text, path)
        if result is None:
            return None
        pp, _resolver2, _tokens = result
        target = ("variant", payload[0], payload[1]) if kind == "variant" else (kind, payload)
        edits = []
        for pos, entry in resolver.type_position_index.items():
            if entry != target:
                continue
            src_path, src_offset = pp.map_to_source(pos)
            if src_path != pp.entry_path:
                return None
            fallback_name = payload[1] if kind == "variant" else payload
            length = _identifier_length_at(text, src_offset) or len(fallback_name)
            edits.append(
                {
                    "range": make_range(text, src_offset, src_offset + length),
                    "newText": new_name,
                }
            )
        if not edits:
            return None
        return {"changes": {pp.entry_path: edits}}

    # 3. Struct/enum field name -- single-file only, same reasoning as (2).
    field_found = _field_symbol_at_position(text, line, character, path)
    if field_found is not None:
        resolver, kind, payload = field_found
        result = _resolve_for_navigation(text, path)
        if result is None:
            return None
        pp, _resolver2, _tokens = result
        target = (kind,) + payload
        edits = []
        for pos, entry in resolver.field_position_index.items():
            if entry != target:
                continue
            src_path, src_offset = pp.map_to_source(pos)
            if src_path != pp.entry_path:
                return None
            length = _identifier_length_at(text, src_offset) or len(payload[-1])
            edits.append(
                {
                    "range": make_range(text, src_offset, src_offset + length),
                    "newText": new_name,
                }
            )
        if not edits:
            return None
        return {"changes": {pp.entry_path: edits}}

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
