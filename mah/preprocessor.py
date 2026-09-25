"""Import/export preprocessor for the Mah language.

Mah's compiler has no ``.`` token and no ``import`` / ``export`` keywords,
so module support is implemented here as a source-to-source preprocessor
that runs *before* the lexer -- true regardless of which compiler pipeline
sits behind it (see docs/V2_DESIGN.md's M0 milestone; this file didn't need
to change for that port). Two import forms are supported:

    import "mathlib.mh"                 # flat: bring exported names into scope
    import math from "mathlib.mh"       # namespaced: access via math.answer
    import math from "mathlib"          # the .mh extension is optional

Only names a file marks with ``export`` are visible to importers:

    export fn square(n) { return n ** 2 }
    export let answer = 42
    export helper                       # export a name declared elsewhere

How scoping is enforced against Mah's single flat global scope:

  * Every imported file ("module") is inlined at most once (include-guard,
    so cycles/diamonds are safe) and assigned a unique module index.
  * *All* top-level names of an inlined module are consistently alpha-renamed
    to a private global name (``__mah_m{idx}_{name}``), and every in-module
    reference is renamed too. Modules therefore never leak bare names.
  * In the importing file, references to imported names are rewritten to the
    module's mangled names:
      - flat import:      bare ``square``      -> ``__mah_m{idx}_square``
      - namespaced:       ``math.answer``      -> ``__mah_m{idx}_answer``
    Only *exported* names get a rewrite; a reference to a non-exported member
    is rewritten to a sentinel that does not exist, producing a clean
    "undefined" error.

The result is a single combined source string plus a per-segment *source map*
translating a combined-text offset back to the original ``(file, offset)`` --
enabling cross-file diagnostics and go-to-definition.

Dependency-free -- it has its own tolerant scanner (below) and never
imports the ``compiler`` package.

M12 adds two rewrite-avoidance fixes that traits/impl/method calls expose
(struct/enum/trait/impl names are global and never renamed -- that stays
unchanged -- but a module's own top-level `fn`/`let` names ARE still
alpha-renamed, and this is where the two new blind spots showed up): an
``id`` immediately preceded by a ``.`` is always a field/method NAME, never
a variable reference, so it's never rewritten even if it happens to spell
some unrelated top-level binding; and a method's own declared name (the
``id`` right after ``fn`` directly inside a top-level ``trait``/``impl``
block body) is similarly never rewritten, so it can't be corrupted into
some other top-level binding's mangled name just because the two happen to
share a spelling.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Optional

BUFFER_PATH = "<buffer>"
DEFAULT_EXT = ".mh"

# M17: the Mah-source prelude (ranges/iterators, see mah/std/prelude.mh's
# own docstring) -- an absolute path computed from `__file__` (not
# relative to the current working directory), so it resolves correctly
# once installed too (`make install-mah` copies the `mah/` package
# wholesale, prelude.mh included, keeping this same relative layout).
PRELUDE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "std", "prelude.mh")

# Prefix used when mangling a module's top-level names.
_MODULE_PREFIX = "__mah_m"
# Sentinel prefix for references to non-exported members (forces an error).
_NOEXPORT_PREFIX = "__mah_noexport_"

# Matches an internal mangled name so messages can be turned back into the
# names the user actually wrote.
_MANGLED_RE = re.compile(r"__mah_m\d+_(?P<name>\w+)")
_NOEXPORT_RE = re.compile(r"__mah_noexport_(?P<name>\w+)")


def demangle_message(message: str) -> str:
    """Rewrite internal mangled names in an error message back to source names.

    ``__mah_noexport_foo`` becomes ``foo (not exported)`` and
    ``__mah_m3_bar`` becomes ``bar``, so compiler errors never leak the
    preprocessor's internal identifiers.
    """
    message = _NOEXPORT_RE.sub(lambda m: f"{m.group('name')} (not exported)", message)
    message = _MANGLED_RE.sub(lambda m: m.group("name"), message)
    return message


# --------------------------------------------------------------------------
# Tolerant scanner (understands `.`, unlike the compiler lexer)
# --------------------------------------------------------------------------

_SCAN_RE = re.compile(
    r"""
      (?P<ws>\s+)
    | (?P<comment>\#[^\n]*)
    | (?P<string>"(?:[^"\\]|\\.)*")
    | (?P<number>\d+(?:\.\d+)?)
    | (?P<id>[A-Za-z_$][\w$]*)
    | (?P<dot>\.)
    | (?P<punct>[\s\S])
    """,
    re.VERBOSE,
)

_MEANINGFUL = ("string", "number", "id", "dot", "punct")


@dataclass
class Tok:
    kind: str   # id | string | number | dot | punct
    value: str
    start: int

    @property
    def end(self) -> int:
        return self.start + len(self.value)


def scan(source: str) -> list:
    """Return meaningful tokens (whitespace and comments are skipped)."""
    tokens: list[Tok] = []
    for match in _SCAN_RE.finditer(source):
        kind = match.lastgroup
        if kind in ("ws", "comment"):
            continue
        tokens.append(Tok(kind, match.group(), match.start()))
    return tokens


def _compute_prelude_triggers() -> frozenset:
    """M17: every `id` token immediately after a `struct`/`enum`/`trait`/
    `fn` `id` token in the prelude's own source -- i.e. every type/trait/
    method name it declares (`Iterator`, `Iterable`, `map`, `filter`,
    `skip`, `take`, `reduce`, `iter`, `next`, `Range`, `FromRange`,
    `ToRange`, ...). Computed once, at import time, by scanning the
    prelude with this module's own tolerant `scan()` (never the real
    compiler lexer -- this module stays dependency-free from `compiler`)."""
    with open(PRELUDE_PATH, encoding="utf-8") as handle:
        source = handle.read()
    tokens = scan(source)
    triggers: set = set()
    for i in range(len(tokens) - 1):
        tok, nxt = tokens[i], tokens[i + 1]
        if tok.kind == "id" and tok.value in ("struct", "enum", "trait", "fn") and nxt.kind == "id":
            triggers.add(nxt.value)
    return frozenset(triggers)


PRELUDE_TRIGGERS: frozenset = _compute_prelude_triggers()


def _uses_prelude(token_lists: list) -> bool:
    """M17: whether the program -- `token_lists` holds one already-scanned
    token list per file (entry + every import) -- looks like it might use
    anything the prelude declares. A sound OVER-approximation (a false
    positive just includes the prelude unnecessarily; there are no false
    negatives, since every real use of prelude functionality has to name
    it): two adjacent `.` characters (`..`/`..=`, which this tolerant
    scanner -- unlike the real lexer -- sees as two separate `dot` tokens,
    maybe followed by a `=` `punct`), or an `id` token spelling one of
    `PRELUDE_TRIGGERS`, or `for let` (a `for` loop) -- except names the program declares itself (the
    `id` right after `struct`/`enum`/`trait`, in any of its files). A
    program with its own `struct Taken` that never iterates must compile
    without the prelude, whose own `Taken` would otherwise clash with it;
    if the program also iterates, the clash is reported as a clear
    "built-in name" error by the resolver. A lone `.` is never a trigger."""
    declared = set()
    for tokens in token_lists:
        for i in range(1, len(tokens)):
            prev, tok = tokens[i - 1], tokens[i]
            if tok.kind == "id" and prev.kind == "id" and prev.value in ("struct", "enum", "trait"):
                declared.add(tok.value)
    triggers = PRELUDE_TRIGGERS - declared
    for tokens in token_lists:
        for tok in tokens:
            if tok.kind == "id" and tok.value in triggers:
                return True
        for i in range(len(tokens) - 1):
            a, b = tokens[i], tokens[i + 1]
            if a.kind == "dot" and b.kind == "dot" and b.start == a.end:
                return True
            # a `for` loop (`for let x in ...`) iterates via the prelude's
            # Iterable/Iterator; `impl Tr for T` is never followed by `let`.
            if a.kind == "id" and a.value == "for" and b.kind == "id" and b.value == "let":
                return True
    return False


def _decode_path(literal: str) -> str:
    try:
        return bytes(literal, "utf-8").decode("unicode_escape")
    except Exception:  # noqa: BLE001
        return literal


# --------------------------------------------------------------------------
# Data types
# --------------------------------------------------------------------------

@dataclass
class ImportSite:
    """A flat ``import "path"`` directive found in the *entry* file."""

    literal: str
    resolved: Optional[str]
    exists: bool
    offset: int             # entry-file offset of the opening quote
    length: int             # length covering both quotes
    line: int


@dataclass
class NamespaceImport:
    """A namespaced ``import ns from "path"`` directive in the *entry* file."""

    name: str               # the namespace identifier, e.g. "math"
    literal: str
    resolved: Optional[str]
    exists: bool
    name_offset: int        # offset of the namespace identifier
    name_length: int
    offset: int             # offset of the opening quote (for diagnostics)
    length: int
    line: int


@dataclass
class Segment:
    start: int
    length: int
    path: str
    src_offset: int
    src_length: int
    root_import: object     # ImportSite | NamespaceImport | None


@dataclass
class ModuleInfo:
    exported: set = field(default_factory=set)
    top_level: set = field(default_factory=set)

    @property
    def private(self) -> set:
        return self.top_level - self.exported


@dataclass
class Preprocessed:
    text: str
    segments: list
    files: dict                 # path -> original source text
    entry_path: str
    entry_imports: list         # flat ImportSite (entry only)
    entry_namespaces: list      # NamespaceImport (entry only)
    errors: list                # (message, entry_offset, length)
    exports: dict               # path -> set of exported names
    module_index: dict          # path -> int
    # M17: the combined-text offset where the prelude begins, or `None` if
    # it wasn't included (see `_uses_prelude`/`preprocess`'s tail). Always
    # appended after every real segment (entry file + every import), so
    # entry-file offsets are unchanged for programs that don't trigger it
    # -- the LSP relies on that.
    prelude_start: Optional[int] = None

    # -- source map -------------------------------------------------------
    def map_to_source(self, offset: int):
        for segment in self.segments:
            if segment.start <= offset < segment.start + segment.length:
                if segment.src_length:
                    delta = min(offset - segment.start, segment.src_length - 1)
                else:
                    delta = 0
                return segment.path, segment.src_offset + delta
        if self.segments:
            last = self.segments[-1]
            return last.path, last.src_offset + last.src_length
        return self.entry_path, offset

    def root_import_for(self, offset: int):
        for segment in self.segments:
            if segment.start <= offset < segment.start + segment.length:
                return segment.root_import
        return None

    def entry_to_combined(self, entry_offset: int) -> Optional[int]:
        for segment in self.segments:
            if segment.path != self.entry_path:
                continue
            if segment.src_offset <= entry_offset < segment.src_offset + segment.src_length:
                return segment.start + (entry_offset - segment.src_offset)
        for segment in self.segments:
            if segment.path != self.entry_path:
                continue
            if entry_offset == segment.src_offset + segment.src_length:
                return segment.start + segment.length
        return None

    def exported_names(self, path: Optional[str]) -> set:
        return self.exports.get(path, set()) if path else set()


# --------------------------------------------------------------------------
# Per-file analysis
# --------------------------------------------------------------------------

def _next_meaningful(tokens: list, i: int) -> Optional[int]:
    return i + 1 if i + 1 < len(tokens) else None


def analyze_module(tokens: list) -> ModuleInfo:
    """Determine a file's exported and top-level names via a token scan.

    ``export`` is only recognized at module (brace depth 0) level.
    """
    exported: set = set()
    top_level: set = set()

    depth = 0
    i = 0
    count = len(tokens)
    while i < count:
        tok = tokens[i]
        if tok.kind == "punct" and tok.value == "{":
            depth += 1
            i += 1
            continue
        if tok.kind == "punct" and tok.value == "}":
            depth = max(0, depth - 1)
            i += 1
            continue

        if depth == 0 and tok.kind == "id" and tok.value == "export":
            nxt = tokens[i + 1] if i + 1 < count else None
            if nxt is not None and nxt.kind == "id" and nxt.value in ("fn", "let"):
                if i + 2 < count and tokens[i + 2].kind == "id":
                    name = tokens[i + 2].value
                    exported.add(name)
                    top_level.add(name)
                i += 1
                continue
            if nxt is not None and nxt.kind == "id":
                exported.add(nxt.value)
                i += 2
                continue
            i += 1
            continue

        if depth == 0 and tok.kind == "id" and tok.value in ("fn", "let"):
            if i + 1 < count and tokens[i + 1].kind == "id":
                top_level.add(tokens[i + 1].value)

        i += 1

    return ModuleInfo(exported=exported, top_level=top_level)


def _resolve_import(base_dir: str, literal: str):
    """Resolve an import path; the ``.mh`` extension is optional.

    Returns ``(resolved_path, exists)``.
    """
    decoded = _decode_path(literal)
    candidate = os.path.abspath(os.path.join(base_dir, decoded))
    if os.path.isfile(candidate):
        return candidate, True
    if not decoded.endswith(DEFAULT_EXT):
        with_ext = candidate + DEFAULT_EXT
        if os.path.isfile(with_ext):
            return with_ext, True
        # Prefer showing the .mh variant as the intended path in errors.
        return with_ext, False
    return candidate, False


# --------------------------------------------------------------------------
# Preprocessing
# --------------------------------------------------------------------------

def preprocess(path: Optional[str], text: Optional[str] = None) -> Preprocessed:
    """Resolve imports/exports starting from ``path`` (or in-memory ``text``)."""
    entry_path = os.path.abspath(path) if path else BUFFER_PATH
    base_dir = os.path.dirname(entry_path) if path else os.getcwd()

    if text is None:
        with open(entry_path, encoding="utf-8") as handle:
            text = handle.read()

    files: dict[str, str] = {entry_path: text}
    exports: dict[str, set] = {}
    module_index: dict[str, int] = {entry_path: 0}
    segments: list[Segment] = []
    entry_imports: list[ImportSite] = []
    entry_namespaces: list[NamespaceImport] = []
    errors: list[tuple] = []
    included: set[str] = {entry_path}
    combined: list[str] = []
    state = {"len": 0}
    # M17: every processed file's token list, so the prelude decision (see
    # `_uses_prelude`) can look at the whole program at once -- a type the
    # entry declares may only be *used* in an import, or vice versa.
    program_tokens: list = []

    def emit(piece: str, fpath: str, src_offset: int, src_length: int, root) -> None:
        if not piece:
            return
        segments.append(
            Segment(state["len"], len(piece), fpath, src_offset, src_length, root)
        )
        combined.append(piece)
        state["len"] += len(piece)

    def directory_of(fpath: str) -> str:
        return os.path.dirname(fpath) if fpath != BUFFER_PATH else base_dir

    def module_name(idx: int, name: str) -> str:
        return f"{_MODULE_PREFIX}{idx}_{name}"

    def is_line_leading(source: str, tokens: list, i: int) -> bool:
        if i == 0:
            return True
        prev = tokens[i - 1]
        return "\n" in source[prev.end : tokens[i].start]

    def inline_module(resolved: str, root) -> None:
        """Recursively process and emit an imported module (once)."""
        if resolved in included:
            return
        included.add(resolved)
        module_index[resolved] = len(module_index)
        try:
            with open(resolved, encoding="utf-8") as handle:
                sub_source = handle.read()
        except OSError:
            return
        files[resolved] = sub_source
        process(resolved, sub_source, root, is_entry=False)

    def process(fpath: str, source: str, root, is_entry: bool) -> None:
        tokens = scan(source)
        count = len(tokens)
        program_tokens.append(tokens)

        info = analyze_module(tokens)
        exports[fpath] = info.exported
        idx = module_index[fpath]

        # References to this module's own top-level names get mangled (unless
        # this is the entry file, whose names stay as the user wrote them).
        name_rewrite: dict[str, str] = {}
        if not is_entry:
            for name in info.top_level:
                name_rewrite[name] = module_name(idx, name)

        # Filled as import directives are encountered (imports precede use):
        #   flat alias:      bare name -> mangled name in target module
        #   namespace:       ns -> (target_idx, exported set)
        ns_map: dict[str, tuple] = {}
        ns_names: set[str] = set()

        cursor = 0
        depth = 0
        i = 0
        # M12: `trait`/`impl` blocks introduce a new spot where an `id`
        # token is a DECLARATION, not a reference -- a method's own name in
        # `fn NAME(...) { ... }` directly inside a top-level trait/impl
        # body. Without special-casing it, a method name that happens to
        # collide with some unrelated top-level `fn`/`let` of this same
        # module would get silently rewritten to that other binding's
        # mangled name, corrupting the method's registered name (it would
        # never again match how callers spell it). `pending_trait_impl`
        # is True right after seeing a depth-0 `trait`/`impl` id, until its
        # own opening `{` is reached; `trait_impl_body_depth` is then the
        # brace depth of that block's own body (its direct children, not
        # anything nested deeper inside e.g. a method's own `{ }`), reset
        # to `None` once that block's closing `}` is reached.
        pending_trait_impl = False
        trait_impl_body_depth = None

        def emit_gap(upto: int) -> None:
            nonlocal cursor
            if upto > cursor:
                emit(source[cursor:upto], fpath, cursor, upto - cursor, root)
                cursor = upto

        while i < count:
            tok = tokens[i]

            # -- import directive (line-leading, depth 0) -------------------
            if (
                depth == 0
                and tok.kind == "id"
                and tok.value == "import"
                and is_line_leading(source, tokens, i)
            ):
                directive = _match_import(tokens, i)
                if directive is not None:
                    kind, ns_tok, str_tok, end_i = directive
                    emit_gap(tok.start)

                    literal = str_tok.value[1:-1]
                    resolved, exists = _resolve_import(
                        directory_of(fpath), literal
                    )
                    root_record = root

                    if is_entry:
                        if kind == "ns":
                            record = NamespaceImport(
                                name=ns_tok.value,
                                literal=literal,
                                resolved=resolved if exists else None,
                                exists=exists,
                                name_offset=ns_tok.start,
                                name_length=len(ns_tok.value),
                                offset=str_tok.start,
                                length=len(str_tok.value),
                                line=source.count("\n", 0, str_tok.start),
                            )
                            entry_namespaces.append(record)
                        else:
                            record = ImportSite(
                                literal=literal,
                                resolved=resolved if exists else None,
                                exists=exists,
                                offset=str_tok.start,
                                length=len(str_tok.value),
                                line=source.count("\n", 0, str_tok.start),
                            )
                            entry_imports.append(record)
                        root_record = record

                    if not exists:
                        if is_entry:
                            errors.append(
                                (
                                    f"cannot find imported file '{literal}'",
                                    str_tok.start,
                                    len(str_tok.value),
                                )
                            )
                    else:
                        inline_module(resolved, root_record)
                        target_idx = module_index[resolved]
                        target_exports = exports.get(resolved, set())
                        if kind == "ns":
                            ns_map[ns_tok.value] = (target_idx, target_exports)
                            ns_names.add(ns_tok.value)
                        else:
                            for name in target_exports:
                                name_rewrite.setdefault(
                                    name, module_name(target_idx, name)
                                )
                        # Separator so tokens can't merge across the splice.
                        emit("\n", fpath, str_tok.start, 0, root_record)

                    # Skip the whole directive (and any trailing ';').
                    cursor = tokens[end_i].end
                    i = end_i + 1
                    continue

            # -- export keyword (depth 0) -----------------------------------
            if depth == 0 and tok.kind == "id" and tok.value == "export":
                nxt = tokens[i + 1] if i + 1 < count else None
                if nxt is not None and nxt.kind == "id" and nxt.value in ("fn", "let"):
                    emit_gap(tok.start)
                    cursor = tok.end  # drop the `export` keyword only
                    i += 1
                    continue
                if nxt is not None and nxt.kind == "id":
                    # bare `export name [;]` -> drop entirely
                    emit_gap(tok.start)
                    end = nxt.end
                    j = i + 2
                    if j < count and tokens[j].kind == "punct" and tokens[j].value == ";":
                        end = tokens[j].end
                        j += 1
                    cursor = end
                    i = j
                    continue

            # -- namespace member access: ns . member -----------------------
            if (
                tok.kind == "id"
                and tok.value in ns_names
                and i + 2 < count
                and tokens[i + 1].kind == "dot"
                and tokens[i + 2].kind == "id"
            ):
                member_tok = tokens[i + 2]
                target_idx, target_exports = ns_map[tok.value]
                emit_gap(tok.start)
                if member_tok.value in target_exports:
                    replacement = module_name(target_idx, member_tok.value)
                else:
                    replacement = _NOEXPORT_PREFIX + member_tok.value
                emit(replacement, fpath, member_tok.start, len(member_tok.value), root)
                cursor = member_tok.end
                i += 3
                continue

            # -- ordinary token ---------------------------------------------
            if tok.kind == "id" and tok.value in ("trait", "impl") and depth == 0:
                pending_trait_impl = True

            if tok.kind == "punct" and tok.value == "{":
                depth += 1
                if pending_trait_impl and trait_impl_body_depth is None:
                    trait_impl_body_depth = depth
                    pending_trait_impl = False
            elif tok.kind == "punct" and tok.value == "}":
                depth = max(0, depth - 1)
                if trait_impl_body_depth is not None and depth < trait_impl_body_depth:
                    trait_impl_body_depth = None

            # M12 fix 1: an `id` immediately preceded by a `dot` is always a
            # field/method NAME (`p.field`, `p.method(...)`), never a
            # variable reference -- never rewrite it, even if it happens to
            # spell some unrelated top-level name of this module. (The
            # `ns . member` namespace case above is handled earlier in this
            # loop -- by the time a plain member id could reach here, that
            # branch has already consumed it via `i += 3`, so this can only
            # ever fire for a non-namespace `.`.)
            prev_is_dot = i > 0 and tokens[i - 1].kind == "dot"
            # M12 fix 2: an `id` immediately preceded by the `id` `fn`,
            # directly inside a top-level trait/impl body, is that method's
            # own declared NAME -- see `trait_impl_body_depth` above.
            prev_is_method_decl_name = (
                trait_impl_body_depth is not None
                and depth == trait_impl_body_depth
                and i > 0
                and tokens[i - 1].kind == "id"
                and tokens[i - 1].value == "fn"
            )

            if tok.kind == "id" and tok.value in name_rewrite and not prev_is_dot and not prev_is_method_decl_name:
                emit_gap(tok.start)
                emit(name_rewrite[tok.value], fpath, tok.start, len(tok.value), root)
                cursor = tok.end
            # (other tokens are covered by gap emission)
            i += 1

        if cursor < len(source):
            emit(source[cursor:], fpath, cursor, len(source) - cursor, root)

    process(entry_path, text, root=None, is_entry=True)

    # M17: the prelude is inlined LAST, after everything else -- as a
    # module with its own index, exactly the way an ordinary import is
    # processed (mangling rules etc; it declares no top-level `fn`/`let`,
    # so nothing actually gets mangled) -- iff the entry file or any import
    # looked like it might use anything the prelude declares. Appended
    # (not prepended) so entry-file offsets are unchanged for a program
    # that doesn't trigger it, which the LSP relies on.
    prelude_start = None
    if _uses_prelude(program_tokens):
        emit("\n", entry_path, len(text), 0, None)
        prelude_start = state["len"]
        inline_module(PRELUDE_PATH, None)

    return Preprocessed(
        text="".join(combined),
        segments=segments,
        files=files,
        entry_path=entry_path,
        entry_imports=entry_imports,
        entry_namespaces=entry_namespaces,
        errors=errors,
        exports=exports,
        module_index=module_index,
        prelude_start=prelude_start,
    )


def _match_import(tokens: list, i: int):
    """Match an import directive starting at ``tokens[i]`` (``import``).

    Returns ``(kind, ns_tok, str_tok, end_index)`` or ``None``. ``kind`` is
    ``"flat"`` or ``"ns"``; ``end_index`` is the last token of the directive
    (including any trailing ``;``).
    """
    count = len(tokens)

    # import "path"
    if i + 1 < count and tokens[i + 1].kind == "string":
        end = i + 1
        if end + 1 < count and tokens[end + 1].kind == "punct" and tokens[end + 1].value == ";":
            end += 1
        return "flat", None, tokens[i + 1], end

    # import <ns> from "path"
    if (
        i + 3 < count
        and tokens[i + 1].kind == "id"
        and tokens[i + 2].kind == "id"
        and tokens[i + 2].value == "from"
        and tokens[i + 3].kind == "string"
    ):
        end = i + 3
        if end + 1 < count and tokens[end + 1].kind == "punct" and tokens[end + 1].value == ";":
            end += 1
        return "ns", tokens[i + 1], tokens[i + 3], end

    return None
