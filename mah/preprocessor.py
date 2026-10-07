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
    export struct Point { x: Number }   # M41s: types too (enum, trait)
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

M41s: struct/enum/trait names ARE renamed too. In every imported module
(not the entry file, and not the prelude, whose types stay global) a
top-level ``struct``/``enum``/``trait`` name goes through the same rewrite as
a ``fn``/``let`` name -- declaration, annotations, struct literals, patterns,
``impl`` targets and trait names, ``Name.Variant``, type values -- and
``export struct``/``export enum``/``export trait`` (or ``export Name``) make
one visible to importers, who write ``lib.Point`` (namespaced) or ``Point``
(flat) exactly like ``math.answer``. ``lib.Shape.Circle`` rewrites only the
``lib.Shape`` part. Runtime-visible type names show the declared name again
(docs/MAHC_FORMAT.md #4.3: ``__mah_m<digits>_<rest>`` displays as ``<rest>``).

M12 added rewrite-avoidance fixes that traits/impl/method calls expose (a
module's names are alpha-renamed, and this is where the blind spots showed
up): an ``id`` immediately preceded by a ``.`` is always a field/method NAME,
never a variable reference, so it's never rewritten even if it happens to
spell some unrelated top-level binding; and a method's own declared name (the
``id`` right after ``fn`` directly inside a top-level ``trait``/``impl``
block body) is similarly never rewritten, so it can't be corrupted into
some other top-level binding's mangled name just because the two happen to
share a spelling. M41s adds the third of the kind: a variant's own declared
name directly inside an ``enum`` body (``enum TypeRef { Param { ... } }`` next
to a ``struct Param``).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Optional

from .project.package_paths import PKG_PREFIX, package_label, package_of_path

BUFFER_PATH = "<buffer>"
DEFAULT_EXT = ".mh"

# M17: the Mah-source prelude (ranges/iterators, see mah/std/prelude.mh's
# own docstring) -- an absolute path computed from `__file__` (not
# relative to the current working directory), so it resolves correctly
# once installed too (`make install-mah` copies the `mah/` package
# wholesale, prelude.mh included, keeping this same relative layout).
PRELUDE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "std", "prelude.mh")
# M27 (docs/STDLIB.md, Phase 0): `import "std:<name>"` resolves to
# `mah/std/<name>.mh` -- the standard library ships inside the package, so
# it's found wherever mah is installed. `std:` is reserved: it never falls
# back to a user file.
STD_DIR = os.path.dirname(PRELUDE_PATH)
STD_PREFIX = "std:"
# M41c: the module a program with decorators implicitly imports.
REFLECT_PATH = os.path.join(STD_DIR, "reflect.mh")
# M28: test files (docs/MAH_TEST.md) -- run only by `mah test`.
TEST_SUFFIX = ".test.mh"
_STD_NAME_RE = re.compile(r"[a-z][a-z0-9_]*")


def std_module_name(path: str) -> Optional[str]:
    """`"math"` for `mah/std/math.mh`, None for anything that isn't a
    standard library module (the prelude included)."""
    if path == PRELUDE_PATH or os.path.dirname(path) != STD_DIR or not path.endswith(".mh"):
        return None
    return os.path.basename(path)[: -len(".mh")]


def source_label(path: str) -> str:
    """How locations name a file other than the entry file: `std:math` for
    a standard library module, `<prelude>` for the prelude, else its base
    name -- never a path into wherever mah happens to be installed."""
    if path == PRELUDE_PATH:
        return "<prelude>"
    name = std_module_name(path)
    if name is not None:
        return STD_PREFIX + name
    # M43: a file inside an installed package is `pkg:NAME/REL`.
    label = package_label(path)
    if label is not None:
        return label
    return os.path.basename(path)

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
    for tokens in token_lists:
        # M41s: per file -- a module's own `struct Range` is renamed, so it
        # only stops *that* file's `Range` tokens from being a trigger.
        declared = set()
        for i in range(1, len(tokens)):
            prev, tok = tokens[i - 1], tokens[i]
            if tok.kind == "id" and prev.kind == "id" and prev.value in ("struct", "enum", "trait"):
                declared.add(tok.value)
        triggers = PRELUDE_TRIGGERS - declared
        for tok in tokens:
            if tok.kind == "id" and tok.value in triggers:
                return True
            # M25 (docs/ERRORS.md): a `try`/`throw` token means the prelude's
            # `Error` trait (and `RuntimeError`'s `impl Error for
            # RuntimeError`) may be needed -- `try`/`throw` are real
            # keywords, not user-declarable names, so no `declared`-style
            # exclusion is needed the way `PRELUDE_TRIGGERS` names do.
            if tok.kind == "id" and tok.value in ("try", "throw"):
                return True
            # M33: `input` can throw the prelude's `EndOfInput`.
            if tok.kind == "id" and tok.value == "input":
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
    # M41s: the `struct`/`enum`/`trait` names among `top_level`.
    types: set = field(default_factory=set)

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
    # M41c: the combined-text range `(start, end)` of the std:reflect module
    # the preprocessor added because the program has decorators and doesn't
    # import it itself, or `None`. Like the prelude it sits after every real
    # segment; editor features treat it as implementation detail.
    hidden_reflect: Optional[tuple] = None

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


# M41s: the declaration keywords whose NAME is a top-level name (and can be
# exported); the last three declare types.
_TYPE_DECLS = frozenset({"struct", "enum", "trait"})
_EXPORTABLE_DECLS = frozenset({"fn", "let"}) | _TYPE_DECLS


def analyze_module(tokens: list) -> ModuleInfo:
    """Determine a file's exported and top-level names via a token scan.

    ``export`` is only recognized at module (brace depth 0) level.
    """
    exported: set = set()
    top_level: set = set()
    types: set = set()

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
            if nxt is not None and nxt.kind == "id" and nxt.value in _EXPORTABLE_DECLS:
                if i + 2 < count and tokens[i + 2].kind == "id":
                    name = tokens[i + 2].value
                    exported.add(name)
                    top_level.add(name)
                    if nxt.value in _TYPE_DECLS:
                        types.add(name)
                i += 1
                continue
            if _is_extern_fn(tokens, i + 1):
                # M27: `export extern fn NAME` (std modules only).
                if i + 3 < count and tokens[i + 3].kind == "id":
                    exported.add(tokens[i + 3].value)
                    top_level.add(tokens[i + 3].value)
                i += 1
                continue
            if nxt is not None and nxt.kind == "id":
                exported.add(nxt.value)
                i += 2
                continue
            i += 1
            continue

        if depth == 0 and tok.kind == "id" and tok.value in _EXPORTABLE_DECLS:
            if i + 1 < count and tokens[i + 1].kind == "id":
                top_level.add(tokens[i + 1].value)
                if tok.value in _TYPE_DECLS:
                    types.add(tokens[i + 1].value)

        i += 1

    return ModuleInfo(exported=exported, top_level=top_level, types=types)


def _is_punct(tokens: list, k: int, ch: str) -> bool:
    return 0 <= k < len(tokens) and tokens[k].kind == "punct" and tokens[k].value == ch


def _skip_balanced(tokens: list, k: int, opener: str, closer: str) -> int:
    """Index just past the `closer` matching the `opener` at `tokens[k]`
    (the end of the token list when it never closes)."""
    depth = 0
    count = len(tokens)
    while k < count:
        if _is_punct(tokens, k, opener):
            depth += 1
        elif _is_punct(tokens, k, closer):
            depth -= 1
            if depth == 0:
                return k + 1
        k += 1
    return count


def _skip_decorators_back(tokens: list, k: int) -> int:
    """M41b: the index of the token before the decorators (`@a`, `@a.b`,
    `@a(...)`, `@a.b(...)`, any number) that directly precede `tokens[k]`
    -- `k - 1` when there are none. A parameter, field or variant that has
    decorators is then recognized by what comes before *them*."""
    while True:
        p = k - 1
        if p < 0:
            return p
        if _is_punct(tokens, p, ")"):
            depth = 0
            m = p
            while m >= 0:
                if _is_punct(tokens, m, ")"):
                    depth += 1
                elif _is_punct(tokens, m, "("):
                    depth -= 1
                    if depth == 0:
                        break
                m -= 1
            p = m - 1
            if m < 1 or tokens[p].kind != "id":
                return k - 1
        elif tokens[p].kind != "id":
            return p
        while p >= 2 and tokens[p - 1].kind == "dot" and tokens[p - 2].kind == "id":
            p -= 2
        if _is_punct(tokens, p - 1, "@"):
            k = p - 1
            continue
        return k - 1


def _enum_variant_names(tokens: list, open_index: int) -> set:
    """M41b: the indices of the variant-name tokens of the enum body opened
    by `tokens[open_index]` (`{`): each is the first token of a comma-
    separated item after its decorators, if any (`@tag("x") Circle { r: N }`)."""
    out: set = set()
    count = len(tokens)
    j = open_index + 1
    while j < count:
        while _is_punct(tokens, j, "@"):
            j += 1
            if j < count and tokens[j].kind == "id":
                j += 1
            while j + 1 < count and tokens[j].kind == "dot" and tokens[j + 1].kind == "id":
                j += 2
            if _is_punct(tokens, j, "("):
                j = _skip_balanced(tokens, j, "(", ")")
        if j >= count or tokens[j].kind != "id":
            break
        out.add(j)
        j += 1
        if _is_punct(tokens, j, "{"):
            j = _skip_balanced(tokens, j, "{", "}")
        if _is_punct(tokens, j, ","):
            j += 1
            continue
        break
    return out


_DECLARATION_WORDS = frozenset({"struct", "enum", "trait", "impl", "let", "export", "extern", "test"})


def _shadowed_params(tokens: list, names) -> set:
    """M36: indices of the `id` tokens that must NOT be renamed because they
    are a function parameter spelled like one of the module's top-level
    `names`: the parameter's declaration and every use of it in the
    function's body. (Renaming them consistently would compile, but the
    parameter's name is also what callers write in `f(name: value)`.) Only
    `fn NAME?(params) ... { body }` with such a parameter is looked at."""
    out: set = set()
    count = len(tokens)

    def punct(k: int, ch: str) -> bool:
        return k < count and tokens[k].kind == "punct" and tokens[k].value == ch

    for i, tok in enumerate(tokens):
        if not (tok.kind == "id" and tok.value == "fn"):
            continue
        j = i + 1
        if j < count and tokens[j].kind == "id":
            j += 1
        while j < count and not punct(j, "(") and not punct(j, "{") and not punct(j, ";"):
            j += 1  # generic parameters
            if j - i > 40:
                break
        if not punct(j, "("):
            continue
        # the parameter list
        depth = 0
        params: list = []
        k = j
        while k < count:
            if tokens[k].kind == "punct" and tokens[k].value in "([{":
                depth += 1
            elif tokens[k].kind == "punct" and tokens[k].value in ")]}":
                depth -= 1
                if depth == 0:
                    break
            elif (
                depth == 1
                and tokens[k].kind == "id"
                and tokens[k].value in names
                and k + 1 < count
                and tokens[k + 1].kind == "punct"
                and tokens[k + 1].value in ":,)="
            ):
                # M41b: a parameter may have decorators before its name; M41c: and
                # a `...` / `**` rest marker right before it.
                marked = k
                while marked > 0 and (tokens[marked - 1].kind == "dot" or _is_punct(tokens, marked - 1, "*")):
                    marked -= 1
                before = _skip_decorators_back(tokens, marked)
                if punct(before, "(") or punct(before, ","):
                    params.append(k)
            k += 1
        if not params or k >= count:
            continue
        # the body: the first `{` after the signature, unless a declaration starts first
        m = k + 1
        body = None
        while m < count:
            t = tokens[m]
            if punct(m, "{"):
                body = m
                break
            if punct(m, "=") or punct(m, ";") or punct(m, "}"):
                break
            if t.kind == "id" and (
                t.value in _DECLARATION_WORDS
                or (t.value == "fn" and m + 1 < count and tokens[m + 1].kind == "id")
            ):
                break
            m += 1
        if body is None:
            continue
        pnames = {tokens[p].value for p in params}
        out.update(params)
        depth = 0
        for m in range(body, count):
            if punct(m, "{"):
                depth += 1
            elif punct(m, "}"):
                depth -= 1
                if depth == 0:
                    break
            elif tokens[m].kind == "id" and tokens[m].value in pnames:
                out.add(m)
    return out


def _call_labels(tokens: list, names) -> set:
    """M36: indices of the labels spelled like one of `names`: keyword-
    argument labels in calls, `f(a, name: v)` (the callee's parameter
    name), and field labels inside braces, `struct S { name: T }` and
    `S { name: v }` (a field name). Neither is a reference, so neither is
    renamed. (A shorthand field, `S { name }`, is still a reference.)"""
    out: set = set()
    stack: list = []  # (bracket, is_call) for every open bracket
    count = len(tokens)
    for i, tok in enumerate(tokens):
        if tok.kind != "punct":
            if (
                tok.kind == "id"
                and tok.value in names
                and stack
                and (stack[-1][0] == "{" or (stack[-1][0] == "(" and stack[-1][1]))
                and (
                    # M41b: a field may have decorators before its name
                    tokens[_skip_decorators_back(tokens, i)].kind == "punct"
                    and tokens[_skip_decorators_back(tokens, i)].value in "({,"
                )
                and i + 1 < count
                and tokens[i + 1].kind == "punct"
                and tokens[i + 1].value == ":"
            ):
                out.add(i)
            continue
        if tok.value in "([{":
            is_call = False
            if tok.value == "(" and i > 0:
                prev = tokens[i - 1]
                before = tokens[i - 2] if i > 1 else None
                is_call = (prev.kind == "id" and prev.value != "fn" and not (before is not None and before.kind == "id" and before.value == "fn")) or (
                    prev.kind == "punct" and prev.value in ")]"
                )
            stack.append((tok.value, is_call))
        elif tok.value in ")]}" and stack:
            stack.pop()
    return out


def _is_extern_fn(tokens: list, i: int) -> bool:
    """M27: whether tokens[i:] start `extern fn` (`extern` is contextual)."""
    return (
        i + 1 < len(tokens)
        and tokens[i].kind == "id"
        and tokens[i].value == "extern"
        and tokens[i + 1].kind == "id"
        and tokens[i + 1].value == "fn"
    )


def _resolve_import(base_dir: str, literal: str):
    """Resolve an import path; the ``.mh`` extension is optional.

    Returns ``(resolved_path, exists)``. M27: `std:<name>` is a standard
    library module, looked up only in `STD_DIR` (the prelude isn't one).
    M43: a `pkg:` literal is returned as-is, unresolved -- `preprocess`
    resolves those through its `PackageContext` (docs/PACKAGES.md).
    """
    if literal.startswith(PKG_PREFIX):
        return literal, False
    if literal.startswith(STD_PREFIX):
        name = literal[len(STD_PREFIX) :]
        candidate = os.path.join(STD_DIR, name + DEFAULT_EXT)
        exists = _STD_NAME_RE.fullmatch(name) is not None and name != "prelude" and os.path.isfile(candidate)
        return candidate, exists
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
    # M28: every module's top-level names, exported or not (for a sibling
    # test file's private access, see `visible_names`).
    top_levels: dict[str, set] = {}
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

    # M43: the package context, created the first time a `pkg:` import is
    # seen -- a program that imports no package never looks at packages.
    pkg_state: dict = {}

    def package_context():
        if "ctx" not in pkg_state:
            from .project.packages import PackageContext

            start = entry_path if path else os.path.join(os.getcwd(), BUFFER_PATH)
            pkg_state["ctx"] = PackageContext.find(start) or PackageContext(None)
        return pkg_state["ctx"]

    def module_name(idx: int, name: str) -> str:
        return f"{_MODULE_PREFIX}{idx}_{name}"

    def is_line_leading(source: str, tokens: list, i: int) -> bool:
        if i == 0:
            return True
        prev = tokens[i - 1]
        return "\n" in source[prev.end : tokens[i].start]

    def visible_names(importer: str, resolved: str) -> set:
        """M28 (docs/MAH_TEST.md): `<stem>.test.mh` sees every top-level
        name of `<stem>.mh` in the same directory; everyone else sees only
        its exports."""
        if importer.endswith(TEST_SUFFIX) and importer[: -len(TEST_SUFFIX)] + DEFAULT_EXT == resolved:
            return top_levels.get(resolved, set())
        return exports.get(resolved, set())

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
        # M27: only the standard library (and the prelude) may bind natives.
        may_extern = fpath == PRELUDE_PATH or std_module_name(fpath) is not None
        program_tokens.append(tokens)

        info = analyze_module(tokens)
        exports[fpath] = info.exported
        top_levels[fpath] = info.top_level
        idx = module_index[fpath]

        # References to this module's own top-level names get mangled (unless
        # this is the entry file, whose names stay as the user wrote them).
        name_rewrite: dict[str, str] = {}
        # M41s: types are renamed too -- except the prelude's, which stay
        # global (the VMs and the compiler know some of them by name).
        if not is_entry:
            for name in info.top_level:
                if fpath == PRELUDE_PATH and name in info.types:
                    continue
                name_rewrite[name] = module_name(idx, name)
        # M36: ids that spell a rewritten name but aren't references to it
        # (parameters, keyword-argument labels) -- recomputed when a flat
        # import adds names.
        shadowed: set = set()

        def recompute_shadowed() -> None:
            nonlocal shadowed
            shadowed = (
                _shadowed_params(tokens, name_rewrite) | _call_labels(tokens, name_rewrite) if name_rewrite else set()
            )

        recompute_shadowed()

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
        # M41s: the same for an `enum` body -- a variant's own declared NAME
        # (`enum TypeRef { Param { ... } }`) is never a reference to a
        # top-level type that happens to share its spelling (`struct Param`).
        pending_enum = False
        variant_names: set = set()

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
                    # M43 (docs/PACKAGES.md): `pkg:` imports, and the two
                    # extra rules for relative imports inside a package.
                    pkg_error = None
                    if literal.startswith(PKG_PREFIX):
                        resolved, pkg_error = package_context().resolve(fpath, literal)
                        exists = resolved is not None
                    elif not literal.startswith(STD_PREFIX) and fpath != BUFFER_PATH:
                        owner = package_of_path(fpath)
                        if owner is not None:
                            pkg_dir = os.path.join(owner[0], ".mah", "packages", owner[1])
                            try:
                                leaves = os.path.commonpath([os.path.abspath(resolved), pkg_dir]) != pkg_dir
                            except ValueError:  # another drive on Windows
                                leaves = True
                            if leaves:
                                pkg_error = (
                                    f"import '{literal}' leaves package '{owner[1]}'; "
                                    f'import other packages as "pkg:NAME"'
                                )
                                exists = False
                            elif not exists:
                                pkg_error = f"cannot find imported file '{literal}'"

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

                    if pkg_error is not None:
                        exists = False
                        if is_entry:
                            errors.append((pkg_error, str_tok.start, len(str_tok.value)))
                        elif root is not None:
                            errors.append((f"{pkg_error} (in '{source_label(fpath)}')", root.offset, root.length))
                    elif exists and resolved.endswith(TEST_SUFFIX):
                        # M28: tests only ever run through `mah test`.
                        exists = False
                        if is_entry:
                            errors.append(
                                (f"can't import the test file '{literal}'", str_tok.start, len(str_tok.value))
                            )
                    elif not exists:
                        if is_entry:
                            what = (
                                f"unknown standard library module '{literal}'"
                                if literal.startswith(STD_PREFIX)
                                else f"cannot find imported file '{literal}'"
                            )
                            errors.append(
                                (
                                    what,
                                    str_tok.start,
                                    len(str_tok.value),
                                )
                            )
                    else:
                        inline_module(resolved, root_record)
                        target_idx = module_index[resolved]
                        target_exports = visible_names(fpath, resolved)
                        if kind == "ns":
                            ns_map[ns_tok.value] = (target_idx, target_exports)
                            ns_names.add(ns_tok.value)
                        else:
                            for name in target_exports:
                                name_rewrite.setdefault(
                                    name, module_name(target_idx, name)
                                )
                            recompute_shadowed()
                        # Separator so tokens can't merge across the splice.
                        emit("\n", fpath, str_tok.start, 0, root_record)

                    # Skip the whole directive (and any trailing ';').
                    cursor = tokens[end_i].end
                    i = end_i + 1
                    continue

            # -- extern fn: std modules only (M27) ---------------------------
            if _is_extern_fn(tokens, i) and not may_extern:
                if is_entry:
                    errors.append(("'extern fn' is only allowed in standard library modules", tok.start, len(tok.value)))
                elif root is not None:
                    errors.append(
                        (
                            f"'extern fn' is only allowed in standard library modules (used in '{source_label(fpath)}')",
                            root.offset,
                            root.length,
                        )
                    )

            # -- export keyword (depth 0) -----------------------------------
            if depth == 0 and tok.kind == "id" and tok.value == "export":
                nxt = tokens[i + 1] if i + 1 < count else None
                if (nxt is not None and nxt.kind == "id" and nxt.value in _EXPORTABLE_DECLS) or _is_extern_fn(tokens, i + 1):
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
            if tok.kind == "id" and tok.value == "enum" and depth == 0:
                pending_enum = True

            if tok.kind == "punct" and tok.value == "{":
                depth += 1
                if pending_trait_impl and trait_impl_body_depth is None:
                    trait_impl_body_depth = depth
                    pending_trait_impl = False
                if pending_enum:
                    variant_names |= _enum_variant_names(tokens, i)
                    pending_enum = False
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

            prev_is_variant_decl_name = i in variant_names

            if (
                tok.kind == "id"
                and tok.value in name_rewrite
                and not prev_is_dot
                and not prev_is_method_decl_name
                and not prev_is_variant_decl_name
                and i not in shadowed
            ):
                emit_gap(tok.start)
                emit(name_rewrite[tok.value], fpath, tok.start, len(tok.value), root)
                cursor = tok.end
            # (other tokens are covered by gap emission)
            i += 1

        if cursor < len(source):
            emit(source[cursor:], fpath, cursor, len(source) - cursor, root)

    process(entry_path, text, root=None, is_entry=True)

    hidden_reflect = None
    # M41c: a program with any decorator implicitly imports std:reflect (its
    # hook helpers run the decorators' hooks). It is inlined like the prelude,
    # after everything else, as an ordinary module with its own index and
    # mangled names -- so it can't collide with a user's own `import reflect
    # from "std:reflect"` (the include guard makes that the same module), a
    # flat import of it, or any user variable named `reflect`: nothing is
    # bound in the user's namespace. The code generator reaches the
    # non-exported helpers through their mangled global names.
    if REFLECT_PATH not in included and any(
        tok.kind == "punct" and tok.value == "@" for tokens in program_tokens for tok in tokens
    ):
        emit(";\n", entry_path, len(text), 0, None)
        hidden_start = state["len"]
        inline_module(REFLECT_PATH, None)
        hidden_reflect = (hidden_start, state["len"])

    # M17: the prelude is inlined LAST, after everything else -- as a
    # module with its own index, exactly the way an ordinary import is
    # processed (mangling rules etc; it declares no top-level `fn`/`let`,
    # so nothing actually gets mangled) -- iff the entry file or any import
    # looked like it might use anything the prelude declares. Appended
    # (not prepended) so entry-file offsets are unchanged for a program
    # that doesn't trigger it, which the LSP relies on.
    prelude_start = None
    if _uses_prelude(program_tokens):
        # M25: a `;` (not just a newline) separates the user's own code
        # from the prelude's -- otherwise a trailing expression statement
        # that ISN'T one of the parser's block-shaped-exempt forms (e.g. a
        # bare `throw e` as literally the program's last line, spec test
        # #7) would need a semicolon it doesn't actually need from the
        # user's own point of view (nothing follows in THEIR file), since
        # the parser would otherwise see the prelude's own first token as
        # "more code after this statement, in the same block."
        emit(";\n", entry_path, len(text), 0, None)
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
        hidden_reflect=hidden_reflect,
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
