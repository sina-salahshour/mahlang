"""Resolve pass: walks the AST, assigns each `let`/parameter a *slot number
within a frame level*, and resolves every identifier reference and call
site to a `(depth, slot)` address -- replacing M0's flat, single
shared-array addressing with the frame-level-aware scheme M1's heap
`Frame`/`Closure` calling convention needs (see docs/V2_DESIGN.md's M1
milestone and the compiler-construction skill for the general technique).

Frame levels vs lexical scopes: only a function body (`FnExpr`) starts a
*new frame level* -- `if`/`while`/bare `{ }` blocks share the enclosing
function's frame level (their variables still live in that function's
`Frame` at runtime) but do get their own *lexical scope* (`self.scopes`)
so shadowing/duplicate-name checks work the way they did in M0. A frame
level is represented by a `FrameLevel` object with its own slot counter;
codegen.py is handed these same `FrameLevel` objects and continues
allocating temporary slots from them, so slot numbers assigned here and
slot numbers assigned to expression temporaries later never collide.

Every resolved identifier/assignment-target address is a `(depth, slot)`
tuple: `depth` is how many `static_parent` hops the *currently executing*
frame (i.e. the frame for the frame level the reference occurs in) must
take to reach the frame level that owns `slot`. `depth` is computed here,
once, at resolve time, from the difference between the two `FrameLevel`s'
static nesting depths -- purely lexical, independent of how a closure
happens to get invoked at runtime (that's the entire point of a *static*,
as opposed to dynamic, chain).

Recursion (self-reference) -- the whole point of M1, and the *opposite*
of M0's deliberate behavior: M0 intentionally delayed inserting a
function's own name into its enclosing scope until after its body was
resolved (mirroring v1's `@init_fn_def`/`@save_fn_def` split), so a
function body could never see its own name and recursion was rejected.
Here, for a `LetStmt` whose value is a `FnExpr` (covers both the
desugared `fn foo(...) { ... }` and an explicit `let foo = fn(...) {
...}`), the name is declared in the *current* frame level and scope
*before* the function's params/body are resolved, so the body can look
itself up and call itself. Every other `LetStmt` keeps M0's rule of
resolving its value expression before declaring its own name (so
`let x = x + 1` still sees any outer `x`, never a fresh, uninitialized
slot of its own) -- that choice was about sane shadowing, not about
recursion, and nothing here changes it.

M2 adds a flat, non-scoped `self.struct_decls` registry (struct type name
-> declared field names) -- struct type names live in a namespace separate
from variable/function names, visible program-wide once declared. Struct
*literal* field validation (missing/extra/duplicate fields) is fully
static, done here, since a literal always names its struct type explicitly.
Field *access* (`p.x`) is deliberately NOT validated here -- that happens
at runtime in code_interpreter.py, since there's no static type inference
to know what struct type an arbitrary expression's value holds.

M3 adds an equivalent flat, non-scoped `self.enum_decls` registry (enum
type name -> {variant name -> declared field names}, `[]` for a unit
variant) -- its own separate namespace from both variables/functions and
struct type names, so a `struct Point` and an `enum Point` can coexist.
Pre-seeded with the built-in `Option` type (`none`/`some(x)`) so it
participates in the exact same validation machinery as a user `enum`.

M3's parsing puzzle: `Shape.Circle { r: 5 }` is unambiguous at parse time
(the parser directly builds an `EnumLit` when it sees `Ident DOT Ident
BRACE_OPEN`), but a bare unit-variant construction like `Shape.Empty` is
syntactically IDENTICAL to ordinary field access (`p.field`) -- the parser
has no way to know whether `Shape` names a variable or a declared enum
type. It therefore always parses to a `FieldAccess` node, and it's THIS
resolver that disambiguates: when resolving a `FieldAccess` whose `.obj` is
a plain `Ident`, try resolving it as an ordinary variable reference first
(a real variable always wins, so `let Shape = ...; Shape.Empty` binds to
the variable, not a hypothetical enum -- no surprises for the common case).
Only if that lookup fails (`NameError`, undefined variable) do we check
whether `expr.obj.name` is a declared enum type with a unit variant (empty
field list) matching `expr.field`; if so, this was actually
`Type.UnitVariant` construction all along, and we set
`expr.enum_unit_type` so codegen knows to skip generating `expr.obj`
(which was never actually resolved to an address in that case) and instead
emit an enum-construction opcode with zero fields. If neither resolves, a
clear error names whichever case applies (no such variable and no such
enum type/variant; or the variant exists but needs braces because it's
struct-shaped, not unit).

M5 makes `if`/`match`/bare `{ }` blocks resolvable as expressions, not just
statements: `resolve_block` now also resolves a populated `Block.tail`
(in the same pushed scope, after the block's own statements, so the tail
can see locals declared earlier in the block), and `resolve_expr` gains
`IfStmt`/`MatchStmt`/`Block` cases that simply delegate to `resolve_stmt`/
`resolve_block` -- resolving one of these node's structure never depended
on whether it appears in statement or expression position, so there is
exactly one resolution implementation for each, reachable from both
dispatches. `BlockStmt` (the old dedicated "bare block used as a
statement" wrapper) is retired: that's now just `ExprStmt(value=Block(...))`,
handled by the existing `ExprStmt` case.

M4 adds `MatchStmt`/pattern resolution (`resolve_pattern`, parallel to
`resolve_expr`). A `BindPat` allocates a fresh slot in the *current* frame
level exactly like a `LetStmt` would -- patterns never start a new frame
level, only `fn` bodies do. `StructPat`/`EnumPat` field validation
(undeclared type, undeclared variant, duplicate/missing/unknown field)
reuses the exact same `_check_no_duplicate_field`/`_check_field_set_matches`
helpers M2/M3's `StructLit`/`EnumLit` validation already established, so
there's exactly one implementation of "does this field list exactly match
that declared shape," shared by literals and patterns alike. Each match
arm gets its own scope layer (pushed/popped around
`resolve_pattern`+`resolve_block`) so one arm's bindings never leak into
the next arm's checks or a sibling arm's body.

M7 adds a real symbol table, built directly into this same resolve pass
instead of as a second, independent scope-scanning implementation: every
scope entry now also carries a `Symbol` (name, declaration position, kind
-- "let"/"fn"/"param"/"binding" -- and a list of every reference position
that resolved to it), and `self.position_index` maps any source position
straight to the `Symbol` declared or referenced there. `_declare`/`_lookup`
populate both. This is the resolver's own authoritative record of "this
identifier occurrence resolved to that declaration," which the LSP's
go-to-definition and rename features (lsp/analysis.py) read directly --
see docs/V2_DESIGN.md's M7 milestone. Struct/enum type names and field
names are NOT part of this symbol table -- they live in the separate
`struct_decls`/`enum_decls` registries above (M7 scoped rename to
variables/functions only; M11 later adds `type_position_index`/
`field_position_index`, parallel dedicated indexes for those two
namespaces -- see their own docstrings below).

M9 adds `DeferStmt` (see docs/V2_DESIGN.md's M9 milestone): its body is
already packaged by the parser as a synthesized, anonymous, zero-param
`FnExpr`, so resolving it is just `resolve_expr(stmt.closure_expr)` --
the existing `FnExpr` case creates a new frame level and resolves the
body, giving correct by-reference capture of enclosing variables with no
new resolve logic at all.

M12 adds `trait`/`impl` declarations and method calls (see
docs/V2_DESIGN.md's M12 milestone). Top-level resolution becomes a THREE
PHASE process instead of one linear walk, because a method body must be
able to reference ANY top-level name -- even one written later in the
file -- and a struct/enum/trait/impl must be usable before its own
declaration ("hoisting"):

  Phase 1: hoist top-level `struct`/`enum` declarations (so their types
  exist), then register every top-level `trait`'s method signatures
  (`_register_trait`) and every top-level `impl`'s headers -- method
  lookup tables, missing/extra/self-mismatch validation, one hidden
  global-frame slot per impl method -- (`_register_impl`). No method
  BODY is resolved yet.

  Phase 2: every other top-level statement, in original source order,
  exactly as before M12 (ordinary `let`s, top-level expressions, etc. can
  freely reference any struct/enum/trait/impl already registered in phase
  1, and any earlier phase-2 statement, same as always).

  Phase 3: every trait default method body and every impl method body,
  now that phase 2 has populated the top-level scope with every other
  top-level name -- this is what lets a method reference a `let` declared
  textually AFTER the `impl` block (`STEP` in the spec's ordering example)
  and what lets one impl's method call another impl fn declared later in
  the file. `self._self_type` is set to the impl's target type name while
  resolving that impl's own method bodies (`None` for trait default
  bodies and everywhere else), giving `_subst_self` something to resolve
  `Self` against; `self._self_positions` collects every position where
  `Self` got substituted, purely so this pass can scrub those positions
  back out of `type_position_index` afterward (a `Self` token must never
  look like a *real* use of the impl's target type name, or an LSP rename
  of that type would incorrectly try to rewrite the `Self` token itself).

  `resolve_stmt`'s own `TraitDecl`/`ImplDecl` cases exist only to raise "not
  allowed" errors -- `resolve_program` never routes a top-level trait/impl
  through them (phases 1/3 handle top-level ones directly), so reaching that
  branch at all means a NESTED trait/impl, which M12 disallows outright.

  `MethodCall` resolution (`x.m(args)`/`Type.m(args)`/`Trait.m(recv, ...)`)
  lives in `resolve_expr`'s own dispatch plus a dedicated `_resolve_path_call`
  helper for the `obj` names a type/trait rather than a real variable --
  exactly mirroring the existing bare-enum-unit-variant disambiguation
  precedent (a real in-scope variable always wins first). See `ast_nodes.py`'s
  `MethodCall` docstring for what `static_address`/`trait_name` mean to
  codegen.py.
"""

from __future__ import annotations

from .ast_nodes import (
    AssignStmt,
    Binary,
    BindPat,
    Block,
    BoolLit,
    BreakStmt,
    Call,
    ContinueStmt,
    CosExpr,
    DeferStmt,
    DetachExpr,
    EnumDecl,
    EnumLit,
    EnumPat,
    ErrorNode,
    ExprStmt,
    FieldAccess,
    FnExpr,
    Ident,
    IfStmt,
    ImplDecl,
    InputExpr,
    LetStmt,
    MatchStmt,
    MethodCall,
    NumberLit,
    PrintStmt,
    ReturnStmt,
    SinExpr,
    SleepAsyncExpr,
    StringLit,
    StructDecl,
    StructLit,
    StructPat,
    TraitDecl,
    Unary,
    WhileStmt,
    WildcardPat,
)
from ..runtime_values import BUILTIN_TYPE_NAMES, SYSTEM_TRAITS


class FrameLevel:
    def __init__(self, depth: int, parent):
        self.depth = depth
        self.parent = parent
        self.next_slot = 0

    def alloc(self) -> int:
        slot = self.next_slot
        self.next_slot += 1
        return slot


class Symbol:
    """M7: one entry in the resolver's symbol table -- a single declaration
    plus every position that resolved to it. Built directly into the normal
    resolve pass (`_declare`/`_lookup`) rather than as a second, independent
    scope-scanning implementation, so it can never drift from the compiler's
    own actual scoping rules -- see docs/V2_DESIGN.md's M7 milestone and its
    "LSP rename" design section."""

    __slots__ = ("name", "decl_position", "kind", "references", "type_hint")

    def __init__(self, name: str, decl_position: int, kind: str):
        self.name = name
        self.decl_position = decl_position
        self.kind = kind  # "let" | "fn" | "param" | "binding"
        self.references: list = []  # positions (ints) of every Ident that resolved here
        # M13: best-effort, purely advisory syntactic type-name guess (see
        # `Resolver._type_hint`/`_syntactic_type_hint`) -- `None` when
        # unknown or (for anything but a `let`) never computed. Never used
        # for codegen/dispatch, only the LSP's method hover/completion.
        self.type_hint: str | None = None


class Resolver:
    def __init__(self):
        self.global_frame = FrameLevel(depth=0, parent=None)
        self.frame_stack: list[FrameLevel] = [self.global_frame]
        self.scopes: list[dict] = [{}]
        # struct type name -> list[str] of declared field names. Flat,
        # non-scoped (not part of self.scopes/frame levels): struct *type*
        # names live in a separate namespace from variable/function names
        # and are visible program-wide once declared, regardless of the
        # lexical nesting depth of the `struct` statement itself -- see
        # docs/V2_DESIGN.md's M2 milestone.
        self.struct_decls: dict = {}
        # enum type name -> {variant name -> declared field names};
        # []-field-list variants are unit variants. Flat/non-scoped, exactly
        # like struct_decls, and in a separate namespace from it (a struct
        # and an enum may share a name). Pre-seeded with the built-in
        # `Option` type so `none`/`some(x)` validate through the same
        # machinery as a user-declared enum -- see module docstring. M10
        # adds the built-in `Promise` type the same way (`Pending`/
        # `Settled { value }`, see runtime_values.PromiseInstance) --
        # `detach`/`sleep_async` construct one directly at the interpreter
        # level, but pre-seeding it here means a `Promise` value still
        # pattern-matches and struct/enum-hovers through the exact same
        # generic machinery any other enum does.
        self.enum_decls: dict = {
            "Option": {"none": [], "some": ["value"]},
            "Promise": {"Pending": [], "Settled": ["value"]},
        }
        # M7: source position -> Symbol, for every position that either
        # declared or referenced a variable/parameter/function-binding/
        # match-binding name. Gives the LSP's go-to-definition/rename
        # features an O(1) "what symbol is at this exact position" lookup
        # without re-walking the AST -- see docs/V2_DESIGN.md's M7
        # milestone.
        self.position_index: dict = {}
        # LSP: parallel to position_index, but for the struct/enum/variant
        # namespace (a separate namespace from variables -- see struct_decls/
        # enum_decls above) -- maps a combined-text position to a tuple
        # describing what struct/enum/variant name is written there:
        #   ("struct", struct_name)
        #   ("enum", enum_name)
        #   ("variant", enum_name, variant_name)
        #   ("trait", trait_name)              -- M12

        # Populated at both DECLARATION sites (struct/enum statements) and every
        # USE site (struct/enum literals, patterns, bare enum-unit-variant field
        # access) so hovering/go-to-definition works uniformly at either. The
        # built-in `Option` type (`none`/`some(x)`) is deliberately never
        # registered here -- it has no user-written declaration to link to, and
        # already gets correct hover via the existing keyword path.
        self.struct_decl_positions: dict = {}       # struct name -> its own name-token position
        self.enum_decl_positions: dict = {}         # enum name -> its own name-token position
        self.enum_variant_decl_positions: dict = {} # enum name -> {variant name -> position}
        self.type_position_index: dict = {}         # position -> the tuple shapes above
        # LSP (M11): parallel to type_position_index, one level down -- maps a
        # combined-text position to a tuple describing what struct/enum FIELD
        # name is written there:
        #   ("struct_field", struct_name, field_name)
        #   ("variant_field", enum_name, variant_name, field_name)
        # Populated at declarations, literals, and EXPLICIT (non-shorthand)
        # patterns only -- see StructPat/EnumPat's field_name_positions docstring
        # in ast_nodes.py for why shorthand pattern fields are deliberately
        # excluded. Field ACCESS (`p.x`) is never registered here at all -- it's
        # not in scope (see docs/NEXT_PHASES.md's "Struct/enum/field rename"
        # section: unsound without a real type system to know what struct shape
        # `p` holds).
        self.field_position_index: dict = {}
        # LSP (M11): unlike field_position_index above (which records EVERY
        # occurrence -- declaration and uses alike -- under the same tuple
        # key, with no way to tell them apart), these two are dedicated
        # DECLARATION-only maps, exactly parallel to struct_decl_positions/
        # enum_decl_positions/enum_variant_decl_positions above -- so
        # go-to-definition/rename can look up a field's declaration site
        # unambiguously instead of scanning field_position_index and hoping
        # dict iteration order happens to put the declaration first.
        self.struct_field_decl_positions: dict = {}   # (struct_name, field_name) -> declaration position
        self.enum_variant_field_decl_positions: dict = {}  # (enum_name, variant_name, field_name) -> declaration position
        # M12: trait name -> {method name -> info}, info = {"params": [param
        # names], "is_method": bool, "default_slot": int | None}. Seeded
        # from SYSTEM_TRAITS (runtime_values.py) -- see this module's
        # docstring and `_register_trait`.
        self.trait_decls: dict = {
            trait_name: {
                method_name: {
                    "params": list(params),
                    "is_method": bool(params) and params[0] == "self",
                    "default_slot": None,
                    # M13: no user-written declaration for a system trait.
                    "decl_position": None,
                }
                for method_name, params in methods.items()
            }
            for trait_name, methods in SYSTEM_TRAITS.items()
        }
        self.system_traits: set = set(SYSTEM_TRAITS)
        # M12: type name -> {"inherent": {name: fninfo}, "traits": {trait:
        # {name: fninfo}}}, fninfo = {"slot": int | None, "is_method": bool,
        # "params": int, "native": bool}. Seeded so every built-in type
        # natively implements every system trait -- see `_register_impl`
        # and code_interpreter.py's `NATIVE_TRAIT_METHODS`. M13 adds three
        # more fninfo keys, purely advisory (LSP hover/go-to-definition/
        # completion, never codegen/dispatch): "decl_position" (source
        # position of the method's own declaration -- `None` here, natives
        # have no user-written declaration), "param_names" (list[str],
        # taken from the trait's own signature for a native), "return_hint"
        # (a best-effort syntactic guess at the method's return type --
        # `None` for every native except `Printable.to_string`, which
        # always returns a String).
        self.impls: dict = {
            builtin_type: {
                "inherent": {},
                "traits": {
                    trait_name: {
                        method_name: {
                            "slot": None,
                            "is_method": self.trait_decls[trait_name][method_name]["is_method"],
                            "params": len(self.trait_decls[trait_name][method_name]["params"]),
                            "native": True,
                            "decl_position": None,
                            "param_names": list(self.trait_decls[trait_name][method_name]["params"]),
                            "return_hint": "String" if (trait_name, method_name) == ("Printable", "to_string") else None,
                        }
                        for method_name in methods
                    }
                    for trait_name, methods in SYSTEM_TRAITS.items()
                },
            }
            for builtin_type in BUILTIN_TYPE_NAMES
        }
        self.trait_decl_positions: dict = {}  # trait name -> name-token position (user traits only)
        # M12: current impl target while resolving that impl's own method
        # bodies (None everywhere else, including while resolving a
        # trait's default method bodies) -- what `_subst_self` resolves a
        # `Self` occurrence against.
        self._self_type = None
        # M12: source positions where `Self` was substituted for the real
        # impl target type name -- scrubbed back out of type_position_index
        # at the end of resolve_program so a `Self` token is never treated
        # as a genuine use of that type name by the LSP (rename in
        # particular). See this module's docstring.
        self._self_positions: set = set()
        # M13: position of a method-name token at a CALL site -> info dict
        #   {"name": str, "candidates": [cand, ...], "receiver_type": str | None}
        # where cand is ("impl", type_name, trait_name_or_None) or
        # ("trait", trait_name) -- purely advisory (LSP hover/go-to-
        # definition/completion), never used for codegen/dispatch. See
        # `_record_dynamic_method_call`/`_resolve_path_call`.
        self.method_call_index: dict = {}
        # M13: position of a method-name token at a DECLARATION site ->
        # ("impl", type_name, trait_name_or_None, method_name) or
        # ("trait", trait_name, method_name).
        self.method_decl_index: dict = {}
        # M13: (start_pos, end_pos, kind, name) per top-level trait/impl
        # block -- kind "trait" (name = trait) or "impl" (name = target
        # type); used by the LSP to know what `self` means (which type/
        # trait's methods to complete) at a cursor position.
        self.member_block_ranges: list = []
        # M13: trait name while resolving that TRAIT's own default method
        # bodies (None everywhere else, including while resolving an impl's
        # method bodies) -- lets `_record_dynamic_method_call` recognize
        # `self.m(...)` inside a trait default body as a call restricted to
        # that trait (there's no concrete `_self_type` there to hang a type
        # hint off of).
        self._self_trait = None

    # -- name table helpers ----------------------------------------------

    def _declare(self, name: str, slot: int, position: int, kind: str = "let") -> "Symbol":
        # M12: `Self`/`self` are reserved names -- see this module's
        # docstring and docs/V2_DESIGN.md's M12 milestone. `self` is only
        # ever legitimately declared as a method's first parameter, which
        # `_resolve_fn_expr`'s own `allow_self` gate handles by calling
        # `_declare` with `kind="param"` for exactly that one case; any
        # other declaration attempt (a `let`, a match binding, a plain
        # function's parameter) is rejected here.
        if name == "Self":
            raise NameError(f"'Self' is reserved and cannot be declared at position {position}")
        if name == "self" and kind != "param":
            raise NameError(
                f"'self' is reserved (only valid as a method's first parameter) at position {position}"
            )
        scope = self.scopes[-1]
        if name in scope:
            raise NameError(f"Error at position {position}: variable is already defined {name}")
        symbol = Symbol(name, position, kind)
        self.position_index[position] = symbol
        scope[name] = (self.frame_stack[-1], slot, symbol)
        return symbol

    def _lookup(self, name: str, position: int):
        for scope in reversed(self.scopes):
            if name in scope:
                frame_level, slot, symbol = scope[name]
                symbol.references.append(position)
                self.position_index[position] = symbol
                return frame_level, slot
        raise NameError(f"Undefined variable '{name}' at position {position}")

    def _resolve_ident_address(self, name: str, position: int) -> tuple:
        frame_level, slot = self._lookup(name, position)
        depth = self.frame_stack[-1].depth - frame_level.depth
        return (depth, slot)

    @staticmethod
    def _check_no_duplicate_field(label: str, fields: list, position: int) -> None:
        """Shared by StructLit and EnumLit validation: raise if `fields`
        (a list of (name, value_expr) pairs, as literals keep them -- an
        ordered list rather than a dict specifically so a duplicate written
        twice in one literal is detectable instead of silently dropped)
        names the same field twice."""
        seen = set()
        for name, _value_expr in fields:
            if name in seen:
                raise Exception(f"Field '{name}' specified more than once in {label} at position {position}")
            seen.add(name)

    @staticmethod
    def _check_field_set_matches(label: str, provided: set, declared: list, position: int) -> None:
        """Shared by StructLit and EnumLit validation: raise unless
        `provided` (the field names actually written in the literal) is
        exactly `declared` (the field names the type/variant requires)."""
        declared_set = set(declared)
        missing = declared_set - provided
        unknown = provided - declared_set
        if missing or unknown:
            parts = []
            if missing:
                parts.append(f"missing field(s) {sorted(missing)}")
            if unknown:
                parts.append(f"unknown field(s) {sorted(unknown)}")
            raise Exception(f"{label} has {' and '.join(parts)} at position {position}")

    def _push(self) -> None:
        self.scopes.append({})

    def _pop(self) -> None:
        self.scopes.pop()

    # -- M12: trait/impl helpers -------------------------------------------

    def _is_user_type(self, name: str) -> bool:
        """A user-declared struct, or a user-declared enum (built-in enums
        -- Option/Promise -- live in `enum_decls` too, but are NOT user
        types for `impl`'s purposes -- see this module's docstring)."""
        return name in self.struct_decls or (name in self.enum_decls and name not in BUILTIN_TYPE_NAMES)

    def _is_type_name(self, name: str) -> bool:
        return name in self.struct_decls or name in self.enum_decls or name in BUILTIN_TYPE_NAMES

    def _subst_self(self, name: str, position: int) -> str:
        """Substitute `Self` for the enclosing impl's target type name --
        see this module's docstring. Called at every AST site `Self` can
        legally appear (struct/enum literal and pattern type names, static
        path calls, a bare `Type.Variant`-shaped FieldAccess) BEFORE the
        existing resolution logic for that node runs, so every other
        branch only ever sees a real type name, never the literal string
        `"Self"`."""
        if name != "Self":
            return name
        if self._self_type is None:
            raise Exception(f"'Self' is only valid inside an impl block at position {position}")
        self._self_positions.add(position)
        return self._self_type

    # -- M13: type hints (best effort, never used for codegen) ------------

    @staticmethod
    def _syntactic_type_hint(node, self_type):
        """M13: a purely syntactic, best-effort type-name guess for `node`
        -- used on an impl method's body TAIL in phase 1b (`_register_impl`),
        before anything has been resolved, so it can only look at the raw
        AST shape (in particular, `Self` is still the literal string
        `"Self"` at this point, not yet substituted -- this function does
        that substitution itself, purely textually). See `_type_hint` below
        for the equivalent used on an already-resolved expression."""
        if isinstance(node, StructLit):
            return self_type if node.type_name == "Self" else node.type_name
        if isinstance(node, EnumLit):
            return self_type if node.type_name == "Self" else node.type_name
        if isinstance(node, FieldAccess):
            if isinstance(node.obj, Ident) and node.obj.name == "Self":
                return self_type
            return None
        if isinstance(node, NumberLit):
            return "Number"
        if isinstance(node, StringLit):
            return "String"
        if isinstance(node, BoolLit):
            return "Bool"
        if isinstance(node, FnExpr):
            return "Function"
        return None

    def _type_hint(self, expr) -> str | None:
        """M13: a purely syntactic, best-effort type-name guess for an
        ALREADY-RESOLVED expression -- see `_syntactic_type_hint` above for
        the pre-resolution equivalent. Never used for codegen/dispatch,
        only to narrow the LSP's method call-site candidate list."""
        if isinstance(expr, (NumberLit, StringLit, BoolLit, FnExpr, StructLit, EnumLit)):
            return self._syntactic_type_hint(expr, self._self_type)
        if isinstance(expr, FieldAccess):
            # `Self` was already substituted by the time this expression
            # was resolved -- `enum_unit_type` (set by resolve_expr's own
            # FieldAccess case) already holds the real type name.
            return expr.enum_unit_type
        if isinstance(expr, Ident):
            if expr.name == "self" and self._self_type is not None:
                return self._self_type
            symbol = self.position_index.get(expr.position)
            return symbol.type_hint if symbol is not None else None
        if isinstance(expr, MethodCall):
            return expr.return_hint
        if isinstance(expr, DetachExpr):
            return "Promise"
        return None

    def _candidates_for_type(self, type_name: str, method_name: str) -> list:
        """M13: candidate impls providing `method_name` for `type_name` --
        an inherent method wins outright (matching runtime dispatch); other-
        wise every trait providing it (0, 1, or 2+ -- the ambiguous case),
        sorted by trait name. Purely advisory (LSP), never affects dispatch
        itself (that's `callmethod`'s own runtime lookup)."""
        entry = self.impls.get(type_name)
        if entry is None:
            return []
        if method_name in entry["inherent"]:
            return [("impl", type_name, None)]
        return [
            ("impl", type_name, tr)
            for tr in sorted(entry["traits"])
            if method_name in entry["traits"][tr]
        ]

    def _all_candidates(self, method_name: str) -> list:
        """M13: every `("impl", T, tr)` over all of `self.impls` whose
        inherent/trait fns contain `method_name` -- user types first, then
        built-ins, each group sorted by (type, trait or '') -- used when a
        dynamic call site's receiver type isn't known at all."""
        user: list = []
        builtin: list = []
        for type_name, entry in self.impls.items():
            group = user if self._is_user_type(type_name) else builtin
            if method_name in entry["inherent"]:
                group.append(("impl", type_name, None))
            for tr in entry["traits"]:
                if method_name in entry["traits"][tr]:
                    group.append(("impl", type_name, tr))
        user.sort(key=lambda c: (c[1], c[2] or ""))
        builtin.sort(key=lambda c: (c[1], c[2] or ""))
        return user + builtin

    def _fninfo_for_impl_candidate(self, cand, method_name: str):
        """M13: the fninfo dict an `("impl", T, tr_or_None)` candidate
        refers to -- shared by `_record_dynamic_method_call` (return-hint
        narrowing) and the LSP (`lsp/analysis.py`'s `_method_signature_lines`
        and friends)."""
        _kind, type_name, trait_name = cand
        entry = self.impls[type_name]
        if trait_name is None:
            return entry["inherent"][method_name]
        return entry["traits"][trait_name][method_name]

    def _record_dynamic_method_call(self, expr: MethodCall) -> None:
        """M13: LSP call-site recording for a dynamic `x.m(...)` (`expr.obj`
        a real variable/expression, neither `static_address` nor
        `trait_name` set) -- populates `method_call_index` and, when every
        candidate agrees on a non-None return type, `expr.return_hint` too.
        Purely advisory -- see this module's docstring."""
        hint = self._type_hint(expr.obj)
        if hint is not None:
            candidates = self._candidates_for_type(hint, expr.method)
        elif (
            isinstance(expr.obj, Ident)
            and expr.obj.name == "self"
            and self._self_trait is not None
            and expr.method in self.trait_decls.get(self._self_trait, {})
        ):
            candidates = [("trait", self._self_trait)]
        else:
            candidates = self._all_candidates(expr.method)
        self.method_call_index[expr.position] = {
            "name": expr.method,
            "candidates": candidates,
            "receiver_type": hint,
        }
        if candidates and all(c[0] == "impl" for c in candidates):
            hints = {self._fninfo_for_impl_candidate(c, expr.method)["return_hint"] for c in candidates}
            if len(hints) == 1:
                (only_hint,) = hints
                if only_hint is not None:
                    expr.return_hint = only_hint

    # -- entry point ---------------------------------------------------

    def resolve_program(self, stmts: list) -> None:
        # M12: three-phase top-level resolution -- see this module's
        # docstring for the full rationale (order-independence/"hoisting"
        # of struct/enum/trait/impl, and method bodies seeing every
        # top-level name regardless of textual order).
        hoisted = set()
        # Phase 1a: hoist top-level struct/enum declarations (registers
        # them via the existing resolve_stmt branches).
        for stmt in stmts:
            if isinstance(stmt, (StructDecl, EnumDecl)):
                self.resolve_stmt(stmt)
                hoisted.add(id(stmt))
        # Phase 1b: traits, then impl headers (impls may reference traits,
        # never the reverse).
        traits = [stmt for stmt in stmts if isinstance(stmt, TraitDecl)]
        impls = [stmt for stmt in stmts if isinstance(stmt, ImplDecl)]
        for trait in traits:
            self._register_trait(trait)
        for impl in impls:
            self._register_impl(impl)
        # Phase 2: every other top-level statement, in original order
        # (unchanged pre-M12 semantics).
        for stmt in stmts:
            if id(stmt) in hoisted or isinstance(stmt, (TraitDecl, ImplDecl)):
                continue
            self.resolve_stmt(stmt)
        # Phase 3: method bodies, with every top-level name now in scope.
        for trait in traits:
            # M13: `_self_trait` while resolving THIS trait's own default
            # bodies -- lets `self.m(...)` inside one recognize itself as
            # restricted to this trait (see `_record_dynamic_method_call`).
            self._self_trait = trait.name
            try:
                for method in trait.methods:
                    if method.fn is not None:
                        # _self_type stays None: a trait's own default body
                        # has no concrete target type to resolve `Self`
                        # against.
                        self._resolve_fn_expr(method.fn, allow_self=True)
            finally:
                self._self_trait = None
        for impl in impls:
            self._self_type = impl.type_name
            try:
                for method in impl.methods:
                    self._resolve_fn_expr(method.fn, allow_self=True)
            finally:
                self._self_type = None
        # LSP hygiene: a `Self` token must never look like a use of the
        # real type name (rename would otherwise rewrite `Self` itself).
        for pos in self._self_positions:
            self.type_position_index.pop(pos, None)

    # -- statements ------------------------------------------------------

    def resolve_stmt(self, stmt) -> None:
        if isinstance(stmt, LetStmt):
            # M7: symbol-table registration uses the *name*'s own position
            # (`name_position`), not `stmt.position` (the leading `let`/`fn`
            # keyword) -- go-to-definition/rename need the identifier's
            # exact span to build a correct edit range. Falls back to
            # `stmt.position` if `name_position` was never set (shouldn't
            # happen via the parser, but keeps this defensive).
            name_position = stmt.name_position if stmt.name_position is not None else stmt.position
            if isinstance(stmt.value, FnExpr):
                # Declare before resolving the body -- enables self-reference
                # (recursion) for named function bindings. See module docstring.
                slot = self.frame_stack[-1].alloc()
                symbol = self._declare(stmt.name, slot, name_position, kind="fn")
                symbol.type_hint = "Function"
                stmt.address = slot
                self._resolve_fn_expr(stmt.value)
            else:
                self.resolve_expr(stmt.value)
                slot = self.frame_stack[-1].alloc()
                symbol = self._declare(stmt.name, slot, name_position, kind="let")
                # M13: best-effort type hint, purely advisory (LSP) -- see
                # `_type_hint`'s docstring.
                symbol.type_hint = self._type_hint(stmt.value)
                stmt.address = slot
        elif isinstance(stmt, AssignStmt):
            self.resolve_expr(stmt.target)
            if isinstance(stmt.target, FieldAccess) and stmt.target.enum_unit_type is not None:
                # `stmt.target` looked like `p.field` but was actually a
                # bare enum unit-variant construction (e.g. `Shape.Empty`)
                # -- not a real reference to anything, so it can never be
                # a valid assignment target. Without this check codegen
                # would try to generate an address for `expr.obj` that was
                # never resolved (enum-unit construction skips that), which
                # fails with a confusing internal error instead of a clean
                # one -- see docs/V2_DESIGN.md's M3 milestone.
                raise Exception(
                    f"Cannot assign to enum variant '{stmt.target.enum_unit_type}."
                    f"{stmt.target.field}' at position {stmt.position}"
                )
            self.resolve_expr(stmt.value)
            if isinstance(stmt.target, Ident):
                # M13: a variable reassigned to something of a different
                # (or unknown) type loses its type hint -- conservative, see
                # this module's docstring / the M13 spec.
                symbol = self.position_index.get(stmt.target.position)
                if symbol is not None and self._type_hint(stmt.value) != symbol.type_hint:
                    symbol.type_hint = None
        elif isinstance(stmt, ExprStmt):
            self.resolve_expr(stmt.value)
        elif isinstance(stmt, PrintStmt):
            for arg in stmt.args:
                self.resolve_expr(arg)
        elif isinstance(stmt, IfStmt):
            self.resolve_expr(stmt.cond)
            self.resolve_block(stmt.then)
            for econd, eblock in stmt.elifs:
                self.resolve_expr(econd)
                self.resolve_block(eblock)
            if stmt.else_ is not None:
                self.resolve_block(stmt.else_)
        elif isinstance(stmt, WhileStmt):
            self.resolve_expr(stmt.cond)
            self.resolve_block(stmt.body)
        elif isinstance(stmt, MatchStmt):
            self.resolve_expr(stmt.scrutinee)
            for arm in stmt.arms:
                # Each arm's pattern bindings and its body share one scope
                # layer directly enclosing the arm -- resolve_block below
                # pushes its own additional nested scope for the body's own
                # statements, exactly like a function's params get their own
                # scope layer directly enclosing the body's block scope (M1
                # precedent).
                self._push()
                self.resolve_pattern(arm.pattern)
                self.resolve_block(arm.body)
                self._pop()
        elif isinstance(stmt, (BreakStmt, ContinueStmt)):
            pass
        elif isinstance(stmt, ReturnStmt):
            if stmt.value is not None:
                self.resolve_expr(stmt.value)
        elif isinstance(stmt, DeferStmt):
            # M9: the deferred body is a synthesized zero-param FnExpr --
            # resolve_expr's existing FnExpr case gives it a new frame
            # level and correct by-reference capture of enclosing
            # variables for free, with zero new resolve logic. See
            # docs/V2_DESIGN.md's M9 milestone.
            self.resolve_expr(stmt.closure_expr)
        elif isinstance(stmt, StructDecl):
            seen = set()
            for name in stmt.fields:
                if name in seen:
                    raise Exception(
                        f"Struct '{stmt.name}' declares field '{name}' more than once "
                        f"at position {stmt.position}"
                    )
                seen.add(name)
            if stmt.name in self.struct_decls:
                raise Exception(
                    f"Struct '{stmt.name}' is already declared at position {stmt.position}"
                )
            # M12: struct/enum names share a namespace with built-in type
            # names, `Self`, and trait names -- see this module's docstring.
            if stmt.name in BUILTIN_TYPE_NAMES or stmt.name == "Self":
                raise Exception(
                    f"'{stmt.name}' is a built-in type name and cannot be redeclared "
                    f"at position {stmt.position}"
                )
            if stmt.name in self.trait_decls:
                raise Exception(
                    f"'{stmt.name}' is already declared as a trait at position {stmt.position}"
                )
            self.struct_decls[stmt.name] = stmt.fields
            # LSP: register the declaration site in type_position_index --
            # see that dict's docstring above.
            name_pos = stmt.name_position if stmt.name_position is not None else stmt.position
            self.struct_decl_positions[stmt.name] = name_pos
            self.type_position_index[name_pos] = ("struct", stmt.name)
            # LSP (M11): register each field's own declaration site -- see
            # field_position_index's docstring above.
            for fname, fpos in zip(stmt.fields, stmt.field_positions):
                self.field_position_index[fpos] = ("struct_field", stmt.name, fname)
                self.struct_field_decl_positions[(stmt.name, fname)] = fpos
        elif isinstance(stmt, EnumDecl):
            seen_variants = set()
            for variant_name, variant_fields in stmt.variants:
                if variant_name in seen_variants:
                    raise Exception(
                        f"Enum '{stmt.name}' declares variant '{variant_name}' more than "
                        f"once at position {stmt.position}"
                    )
                seen_variants.add(variant_name)
                seen_fields = set()
                for field_name in variant_fields:
                    if field_name in seen_fields:
                        raise Exception(
                            f"Enum '{stmt.name}' variant '{variant_name}' declares field "
                            f"'{field_name}' more than once at position {stmt.position}"
                        )
                    seen_fields.add(field_name)
            if stmt.name in self.enum_decls:
                raise Exception(
                    f"Enum '{stmt.name}' is already declared at position {stmt.position}"
                )
            # M12: see the identical check in the StructDecl branch above.
            if stmt.name in BUILTIN_TYPE_NAMES or stmt.name == "Self":
                raise Exception(
                    f"'{stmt.name}' is a built-in type name and cannot be redeclared "
                    f"at position {stmt.position}"
                )
            if stmt.name in self.trait_decls:
                raise Exception(
                    f"'{stmt.name}' is already declared as a trait at position {stmt.position}"
                )
            self.enum_decls[stmt.name] = {
                variant_name: variant_fields for variant_name, variant_fields in stmt.variants
            }
            # LSP: register the enum's and each variant's declaration site
            # in type_position_index -- see that dict's docstring above.
            name_pos = stmt.name_position if stmt.name_position is not None else stmt.position
            self.enum_decl_positions[stmt.name] = name_pos
            self.type_position_index[name_pos] = ("enum", stmt.name)
            variant_positions_map = {}
            for (variant_name, _variant_fields), variant_pos in zip(stmt.variants, stmt.variant_positions):
                variant_positions_map[variant_name] = variant_pos
                self.type_position_index[variant_pos] = ("variant", stmt.name, variant_name)
            self.enum_variant_decl_positions[stmt.name] = variant_positions_map
            # LSP (M11): register each variant's own field declaration
            # sites -- see field_position_index's docstring above.
            for (vname, vfields), vfield_positions in zip(stmt.variants, stmt.variant_field_positions):
                for fname, fpos in zip(vfields, vfield_positions):
                    self.field_position_index[fpos] = ("variant_field", stmt.name, vname, fname)
                    self.enum_variant_field_decl_positions[(stmt.name, vname, fname)] = fpos
        elif isinstance(stmt, TraitDecl):
            # M12: `resolve_program` handles every TOP-LEVEL trait directly
            # (phases 1b/3) and never routes it through resolve_stmt -- so
            # reaching this branch at all means a nested `trait` (inside a
            # function body, an if/while block, etc.), which is disallowed
            # outright. See this module's docstring.
            raise Exception(f"'trait' declarations are only allowed at the top level at position {stmt.position}")
        elif isinstance(stmt, ImplDecl):
            # Same reasoning as the TraitDecl branch above.
            raise Exception(f"'impl' blocks are only allowed at the top level at position {stmt.position}")
        else:
            raise AssertionError(f"unhandled statement node {stmt!r}")

    def resolve_block(self, block: Block) -> None:
        self._push()
        for stmt in block.stmts:
            self.resolve_stmt(stmt)
        if block.tail is not None:
            # M5: resolve the tail last, in the same pushed scope, so it can
            # see locals declared earlier in this same block.
            self.resolve_expr(block.tail)
        self._pop()

    def _resolve_fn_expr(self, fn: FnExpr, allow_self: bool = False) -> None:
        new_frame = FrameLevel(depth=self.frame_stack[-1].depth + 1, parent=self.frame_stack[-1])
        self.frame_stack.append(new_frame)
        self._push()
        for index, param_name in enumerate(fn.params):
            param_position = (
                fn.param_positions[index] if index < len(fn.param_positions) else fn.position
            )
            # M12: `self` is only a legal parameter name as a trait/impl
            # method's very first parameter -- `allow_self` is only True
            # when this FnExpr is a trait/impl method's own body (see
            # `resolve_program`'s phase 3). Anywhere else (an ordinary
            # `fn`, or `self` past index 0 even inside a method) is an
            # error -- checked BEFORE declaring, since `_declare` itself
            # would only catch a bare `let self = ...`/binding, not a
            # parameter (parameters are declared with kind="param",
            # deliberately exempted from `_declare`'s own reserved-name
            # check for exactly the index-0 case this allows).
            if param_name == "self" and not (allow_self and index == 0):
                raise Exception(
                    f"'self' is only allowed as the first parameter of a trait/impl "
                    f"method at position {param_position}"
                )
            slot = new_frame.alloc()
            self._declare(param_name, slot, param_position, kind="param")
            fn.param_slots.append(slot)
        self.resolve_block(fn.body)
        self._pop()
        self.frame_stack.pop()
        fn.frame_level = new_frame

    # -- M12: trait/impl registration (resolve_program's phase 1b) --------

    def _register_trait(self, trait: TraitDecl) -> None:
        """Validate and register a top-level `TraitDecl`'s method
        signatures into `self.trait_decls` -- allocating a hidden global
        slot for every method that HAS a default body (a bodyless/required
        method gets no slot: there's no Closure to store). Does not touch
        method bodies at all (see resolve_program's phase 3)."""
        name = trait.name
        if name == "Self" or name in BUILTIN_TYPE_NAMES:
            raise Exception(
                f"'{name}' is a built-in type name and cannot be redeclared at position {trait.position}"
            )
        if name in self.trait_decls:
            raise Exception(f"Trait '{name}' is already declared at position {trait.position}")
        if name in self.struct_decls or name in self.enum_decls:
            raise Exception(f"'{name}' is already declared as a type at position {trait.position}")

        seen_methods: set = set()
        for method in trait.methods:
            if method.name in seen_methods:
                raise Exception(
                    f"Trait '{name}' declares '{method.name}' more than once at position {method.position}"
                )
            seen_methods.add(method.name)
            # Same `self`-placement rule `_resolve_fn_expr` enforces for a
            # method WITH a body -- needed here too because a bodyless
            # (required) trait method never reaches `_resolve_fn_expr` at
            # all (there's no FnExpr to resolve).
            for index, param_name in enumerate(method.params):
                if param_name == "self" and index != 0:
                    raise Exception(
                        f"'self' is only allowed as the first parameter of a trait/impl "
                        f"method at position {method.position}"
                    )
            if method.fn is not None:
                method.slot = self.global_frame.alloc()

        self.trait_decls[name] = {
            method.name: {
                "params": list(method.params),
                "is_method": method.is_method,
                "default_slot": method.slot,
                # M13: source position of this method's own declaration
                # (the "fn <name>" token) -- purely advisory (LSP hover/
                # go-to-definition), see `Resolver.method_decl_index`.
                "decl_position": method.name_position if method.name_position is not None else method.position,
            }
            for method in trait.methods
        }
        # LSP.
        pos = trait.name_position if trait.name_position is not None else trait.position
        self.trait_decl_positions[name] = pos
        self.type_position_index[pos] = ("trait", name)
        # M13: method-name declaration index + member-block range -- see
        # `method_decl_index`/`member_block_ranges`'s own docstrings above.
        for method in trait.methods:
            decl_pos = method.name_position if method.name_position is not None else method.position
            self.method_decl_index[decl_pos] = ("trait", name, method.name)
        self.member_block_ranges.append(
            (trait.position, trait.end_position if trait.end_position is not None else trait.position, "trait", name)
        )

    def _register_impl(self, impl: ImplDecl) -> None:
        """Validate and register a top-level `ImplDecl`'s method headers --
        arity/self-placement/missing-method checks against the trait (for a
        trait impl), duplicate-across-blocks checks (for an inherent impl),
        one hidden global slot per method this impl defines. Populates
        `impl.registrations` with every `(method_name, slot, is_method)`
        this impl block must `defmethod` at runtime -- its own fns PLUS, for
        a trait impl, every inherited (not overridden) trait default.
        Method BODIES are resolved later (resolve_program's phase 3)."""
        type_name = impl.type_name
        pos = impl.position
        if type_name == "Self":
            raise Exception(f"'Self' cannot be the target of an impl at position {pos}")
        if not self._is_type_name(type_name):
            raise NameError(f"Undefined type '{type_name}' in impl at position {pos}")
        if type_name in self.struct_decls and type_name in self.enum_decls:
            raise Exception(
                f"'{type_name}' is ambiguous in impl: it is both a struct and an enum at position {pos}"
            )

        self.impls.setdefault(type_name, {"inherent": {}, "traits": {}})
        entry = self.impls[type_name]

        if impl.trait_name is None:
            self._register_inherent_impl(impl, entry)
        else:
            self._register_trait_impl(impl, entry)

        # LSP: register the type-name / trait-name use sites in the impl
        # header (built-in types / system traits get nothing here -- they
        # have no user-written declaration to link to).
        if impl.type_name_position is not None:
            if type_name in self.struct_decls:
                self.type_position_index[impl.type_name_position] = ("struct", type_name)
            elif type_name in self.enum_decls and type_name not in BUILTIN_TYPE_NAMES:
                self.type_position_index[impl.type_name_position] = ("enum", type_name)
        if impl.trait_name is not None and impl.trait_name_position is not None:
            if impl.trait_name in self.trait_decl_positions:
                self.type_position_index[impl.trait_name_position] = ("trait", impl.trait_name)

        # M13: member-block range -- see `member_block_ranges`'s docstring.
        self.member_block_ranges.append(
            (impl.position, impl.end_position if impl.end_position is not None else impl.position, "impl", type_name)
        )

    def _register_inherent_impl(self, impl: ImplDecl, entry: dict) -> None:
        type_name = impl.type_name
        if not self._is_user_type(type_name):
            raise Exception(
                f"Cannot define inherent methods on built-in type '{type_name}'; declare a "
                f"trait and 'impl YourTrait for {type_name}' instead at position {impl.position}"
            )
        seen: set = set()
        for method in impl.methods:
            if method.name in seen or method.name in entry["inherent"]:
                raise Exception(
                    f"Duplicate definition of '{method.name}' for '{type_name}' at position {method.position}"
                )
            seen.add(method.name)
            method.slot = self.global_frame.alloc()
            decl_pos = method.name_position if method.name_position is not None else method.position
            entry["inherent"][method.name] = {
                "slot": method.slot,
                "is_method": method.is_method,
                "params": len(method.params),
                "native": False,
                # M13: purely advisory (LSP) -- see `self.impls`'s docstring.
                "decl_position": decl_pos,
                "param_names": list(method.params),
                "return_hint": (
                    self._syntactic_type_hint(method.fn.body.tail, type_name)
                    if method.fn.body.tail is not None
                    else None
                ),
            }
            impl.registrations.append((method.name, method.slot, method.is_method))
            # M13: method-name declaration index -- see its docstring above.
            self.method_decl_index[decl_pos] = ("impl", type_name, None, method.name)

    def _register_trait_impl(self, impl: ImplDecl, entry: dict) -> None:
        type_name = impl.type_name
        trait_name = impl.trait_name
        pos = impl.position
        if trait_name not in self.trait_decls:
            raise NameError(f"Undefined trait '{trait_name}' at position {pos}")
        # Orphan rule: at least one of trait/type must be user-defined.
        # Checked BEFORE the duplicate-impl check below.
        if trait_name in self.system_traits and not self._is_user_type(type_name):
            raise Exception(
                f"Cannot implement built-in trait '{trait_name}' for built-in type "
                f"'{type_name}' at position {pos}"
            )
        if trait_name in entry["traits"]:
            raise Exception(f"'{type_name}' already implements '{trait_name}' at position {pos}")

        trait_methods = self.trait_decls[trait_name]
        seen: set = set()
        provided: dict = {}
        for method in impl.methods:
            if method.name in seen:
                raise Exception(
                    f"Duplicate definition of '{method.name}' for '{type_name}' at position {method.position}"
                )
            seen.add(method.name)
            trait_info = trait_methods.get(method.name)
            if trait_info is None:
                raise Exception(
                    f"Method '{method.name}' is not a member of trait '{trait_name}' at position {method.position}"
                )
            if method.is_method != trait_info["is_method"]:
                kind = "method (with self)" if trait_info["is_method"] else "static function (without self)"
                raise Exception(
                    f"Trait '{trait_name}' declares '{method.name}' as a {kind} at position {method.position}"
                )
            if len(method.params) != len(trait_info["params"]):
                raise Exception(
                    f"Method '{method.name}' has {len(method.params)} parameter(s) but trait "
                    f"'{trait_name}' declares {len(trait_info['params'])} at position {method.position}"
                )
            method.slot = self.global_frame.alloc()
            decl_pos = method.name_position if method.name_position is not None else method.position
            provided[method.name] = {
                "slot": method.slot,
                "is_method": method.is_method,
                "params": len(method.params),
                "native": False,
                # M13: purely advisory (LSP) -- see `self.impls`'s docstring.
                "decl_position": decl_pos,
                "param_names": list(method.params),
                "return_hint": (
                    self._syntactic_type_hint(method.fn.body.tail, type_name)
                    if method.fn.body.tail is not None
                    else None
                ),
            }
            # M13: method-name declaration index -- see its docstring above.
            self.method_decl_index[decl_pos] = ("impl", type_name, trait_name, method.name)

        missing = [
            name
            for name, info in trait_methods.items()
            if info["default_slot"] is None and name not in provided
        ]
        if missing:
            raise Exception(
                f"'{type_name}' is missing trait method(s) {sorted(missing)} required by "
                f"'{trait_name}' at position {pos}"
            )

        trait_impl: dict = dict(provided)
        for name, info in trait_methods.items():
            if name in trait_impl or info["default_slot"] is None:
                continue
            trait_impl[name] = {
                "slot": info["default_slot"],
                "is_method": info["is_method"],
                "params": len(info["params"]),
                "native": False,
                # M13: an INHERITED (not overridden) trait default -- its
                # declaration is the trait's own method, and its return
                # type isn't guessed here at all (the spec doesn't ask for
                # syntactic analysis of a trait default's body at this
                # call site; it's still reachable via the trait's own
                # `method_decl_index`/hover).
                "decl_position": info["decl_position"],
                "param_names": list(info["params"]),
                "return_hint": None,
            }
        entry["traits"][trait_name] = trait_impl
        for method in impl.methods:
            impl.registrations.append((method.name, provided[method.name]["slot"], provided[method.name]["is_method"]))
        for name, info in trait_impl.items():
            if name in provided:
                continue
            impl.registrations.append((name, info["slot"], info["is_method"]))

    def _resolve_path_call(self, expr: MethodCall) -> None:
        """Resolve a `MethodCall` whose `obj` is a bare `Ident` that is NOT
        a real in-scope variable (or is the literal `Self`) -- i.e. a
        static path call, `Trait.method(recv, ...)` or `Type.method(...)`.
        Sets exactly one of `expr.trait_name`/`expr.static_address` (see
        `ast_nodes.py`'s `MethodCall` docstring) or raises a clean error."""
        name = self._subst_self(expr.obj.name, expr.obj.position)

        if name in self.trait_decls:
            info = self.trait_decls[name].get(expr.method)
            if info is None:
                raise Exception(f"Trait '{name}' has no method '{expr.method}' at position {expr.position}")
            if not info["is_method"]:
                raise Exception(
                    f"'{name}.{expr.method}' is a static trait function; call it on a "
                    f"concrete type, e.g. 'SomeType.{expr.method}(...)' at position {expr.position}"
                )
            if not expr.args:
                raise Exception(
                    f"'{name}.{expr.method}(...)' needs the receiver as its first argument "
                    f"at position {expr.position}"
                )
            expr.trait_name = name
            if name in self.trait_decl_positions:
                self.type_position_index[expr.obj.position] = ("trait", name)
            for arg in expr.args:
                self.resolve_expr(arg)
            # M13: LSP call-site recording -- Trait.m(recv, ...).
            self.method_call_index[expr.position] = {
                "name": expr.method,
                "candidates": [("trait", name)],
                "receiver_type": self._type_hint(expr.args[0]),
            }
            return
        if self._is_type_name(name):
            impl_entry = self.impls.get(name, {"inherent": {}, "traits": {}})
            inherent = impl_entry["inherent"].get(expr.method)
            if inherent is not None:
                fninfo = inherent
                trait_hit = None
            else:
                trait_hits = [
                    (tr, fns[expr.method]) for tr, fns in impl_entry["traits"].items() if expr.method in fns
                ]
                if len(trait_hits) > 1:
                    raise Exception(
                        f"'{name}.{expr.method}' is ambiguous: provided by traits "
                        f"{sorted(tr for tr, _ in trait_hits)} at position {expr.position}"
                    )
                if not trait_hits:
                    raise Exception(f"Type '{name}' has no function '{expr.method}' at position {expr.position}")
                trait_hit, fninfo = trait_hits[0]
            if fninfo["native"]:
                if not expr.args:
                    raise Exception(
                        f"'{name}.{expr.method}(...)' needs the receiver as its first argument "
                        f"at position {expr.position}"
                    )
                expr.trait_name = trait_hit
            else:
                expr.static_address = (self.frame_stack[-1].depth, fninfo["slot"])
            # M13: purely advisory return-type guess -- see `_type_hint`'s
            # docstring. Set regardless of which branch above ran (static
            # or native-trait): both found the same `fninfo`.
            expr.return_hint = fninfo.get("return_hint")
            if expr.obj.position is not None:
                if name in self.struct_decls:
                    self.type_position_index[expr.obj.position] = ("struct", name)
                elif name in self.enum_decls and name not in BUILTIN_TYPE_NAMES:
                    self.type_position_index[expr.obj.position] = ("enum", name)
            for arg in expr.args:
                self.resolve_expr(arg)
            # M13: LSP call-site recording -- Type.m(...) (static or native).
            self.method_call_index[expr.position] = {
                "name": expr.method,
                "candidates": [("impl", name, trait_hit)],
                "receiver_type": name,
            }
            return

        raise NameError(f"Undefined variable '{expr.obj.name}' at position {expr.obj.position}")

    # -- expressions -----------------------------------------------------

    def resolve_expr(self, expr) -> None:
        if isinstance(expr, ErrorNode):
            # M6: a syntax error the parser already recorded and recovered
            # from -- nothing to resolve. mah.py refuses to run/build a
            # program with any parser.errors regardless, so resolve never
            # needs to do anything smarter here than "skip it."
            return
        if isinstance(expr, (NumberLit, StringLit, BoolLit)):
            return
        if isinstance(expr, Ident):
            expr.address = self._resolve_ident_address(expr.name, expr.position)
            return
        if isinstance(expr, Unary):
            self.resolve_expr(expr.operand)
            return
        if isinstance(expr, Binary):
            self.resolve_expr(expr.lhs)
            self.resolve_expr(expr.rhs)
            return
        if isinstance(expr, Call):
            self.resolve_expr(expr.callee)
            for arg in expr.args:
                self.resolve_expr(arg)
            return
        if isinstance(expr, (SinExpr, CosExpr)):
            self.resolve_expr(expr.arg)
            return
        if isinstance(expr, InputExpr):
            return
        if isinstance(expr, DetachExpr):
            self.resolve_expr(expr.call)
            return
        if isinstance(expr, SleepAsyncExpr):
            self.resolve_expr(expr.arg)
            return
        if isinstance(expr, FnExpr):
            self._resolve_fn_expr(expr)
            return
        if isinstance(expr, StructLit):
            # M12: `Self { ... }` inside an impl method body -- substitute
            # the impl's own target type name before the existing
            # validation logic runs (see `_subst_self`).
            expr.type_name = self._subst_self(expr.type_name, expr.position)
            declared = self.struct_decls.get(expr.type_name)
            if declared is None:
                raise NameError(
                    f"Undefined struct type '{expr.type_name}' at position {expr.position}"
                )
            label = f"'{expr.type_name}' literal"
            self._check_no_duplicate_field(label, expr.fields, expr.position)
            provided = {name for name, _ in expr.fields}
            self._check_field_set_matches(f"Struct literal for '{expr.type_name}'", provided, declared, expr.position)
            for _name, value_expr in expr.fields:
                self.resolve_expr(value_expr)
            # LSP: register this use site -- see type_position_index's docstring.
            self.type_position_index[expr.position] = ("struct", expr.type_name)
            # LSP (M11): register each field label's use site -- see
            # field_position_index's docstring.
            for (fname, _value), fpos in zip(expr.fields, expr.field_name_positions):
                self.field_position_index[fpos] = ("struct_field", expr.type_name, fname)
            return
        if isinstance(expr, EnumLit):
            # M12: `Self.Circle { ... }` / `Self.Empty` (the latter parses
            # as an EnumLit only when braced -- see the parser; the bare,
            # unbraced form goes through the FieldAccess branch below).
            expr.type_name = self._subst_self(
                expr.type_name, expr.type_name_position if expr.type_name_position is not None else expr.position
            )
            variants = self.enum_decls.get(expr.type_name)
            if variants is None:
                raise NameError(f"Undefined enum type '{expr.type_name}' at position {expr.position}")
            declared = variants.get(expr.variant)
            if declared is None:
                raise Exception(
                    f"Enum '{expr.type_name}' has no variant '{expr.variant}' "
                    f"at position {expr.position}"
                )
            label = f"'{expr.type_name}.{expr.variant}' literal"
            self._check_no_duplicate_field(label, expr.fields, expr.position)
            provided = {name for name, _ in expr.fields}
            self._check_field_set_matches(
                f"Enum literal for '{expr.type_name}.{expr.variant}'", provided, declared, expr.position
            )
            for _name, value_expr in expr.fields:
                self.resolve_expr(value_expr)
            # LSP: register the variant use site, and the type name use
            # site when tracked -- see type_position_index's docstring.
            self.type_position_index[expr.position] = ("variant", expr.type_name, expr.variant)
            if expr.type_name_position is not None:
                self.type_position_index[expr.type_name_position] = ("enum", expr.type_name)
            # LSP (M11): register each field label's use site -- see
            # field_position_index's docstring.
            for (fname, _value), fpos in zip(expr.fields, expr.field_name_positions):
                self.field_position_index[fpos] = ("variant_field", expr.type_name, expr.variant, fname)
            return
        if isinstance(expr, FieldAccess):
            if isinstance(expr.obj, Ident):
                if expr.obj.name == "Self":
                    # M12: `Self.Empty` (bare unit-variant construction) --
                    # substitute before the existing lookup runs. A real
                    # variable can never be named `Self` (`_declare` rejects
                    # it outright), so this always falls through to the
                    # enum-unit-variant path below, never the ordinary
                    # variable-lookup success path.
                    expr.obj.name = self._subst_self(expr.obj.name, expr.obj.position)
                # See module docstring for the full disambiguation rule:
                # `Type.Variant` (no braces) parses identically to ordinary
                # field access, so a real in-scope variable always wins
                # first; only on an undefined-variable NameError do we
                # check whether this is actually a bare enum unit-variant
                # construction.
                try:
                    expr.obj.address = self._resolve_ident_address(expr.obj.name, expr.obj.position)
                    expr.enum_unit_type = None
                    return
                except NameError:
                    variants = self.enum_decls.get(expr.obj.name)
                    if variants is not None and expr.field in variants:
                        if variants[expr.field] == []:
                            expr.enum_unit_type = expr.obj.name
                            # LSP: register both the type-name and variant-name
                            # use sites for a bare enum-unit-variant
                            # construction -- see type_position_index's docstring.
                            self.type_position_index[expr.obj.position] = ("enum", expr.obj.name)
                            self.type_position_index[expr.position] = ("variant", expr.obj.name, expr.field)
                            return
                        raise Exception(
                            f"Enum variant '{expr.obj.name}.{expr.field}' requires fields "
                            f"(use '{expr.obj.name}.{expr.field} {{ ... }}') at position {expr.position}"
                        )
                    raise
            # Deliberate simplification: the field name itself is NOT
            # validated here against any struct/enum shape -- without a
            # real type system there's no reliable way to know what
            # struct/enum type a given expression's value will hold at
            # compile time (e.g. a function parameter has no static type
            # annotation). Field names are validated at *runtime* instead,
            # in code_interpreter.py's `getfield`/`setfield` handlers. See
            # docs/V2_DESIGN.md's M2 milestone.
            expr.enum_unit_type = None
            self.resolve_expr(expr.obj)
            return
        if isinstance(expr, MethodCall):
            # M12: `expr.obj.method(args)` -- three shapes, disambiguated
            # exactly like the existing bare-enum-unit-variant precedent (a
            # real in-scope variable always wins first): `p.m(...)` where
            # `p` is a real variable/expression is an ordinary dynamic
            # method call (neither `static_address` nor `trait_name` gets
            # set here -- codegen/the interpreter dispatch on the runtime
            # type of `expr.obj`'s value); `Type.m(...)`/`Trait.m(recv,
            # ...)` where the leading name is NOT a variable (or is the
            # literal `Self`, which can never be a variable) is a static
            # path call, resolved by `_resolve_path_call`.
            if isinstance(expr.obj, Ident):
                if expr.obj.name != "Self":
                    try:
                        expr.obj.address = self._resolve_ident_address(expr.obj.name, expr.obj.position)
                    except NameError:
                        pass  # not a variable -- a type/trait path, below
                    else:
                        for arg in expr.args:
                            self.resolve_expr(arg)
                        self._record_dynamic_method_call(expr)
                        return
                self._resolve_path_call(expr)
                return
            self.resolve_expr(expr.obj)
            for arg in expr.args:
                self.resolve_expr(arg)
            self._record_dynamic_method_call(expr)
            return
        if isinstance(expr, IfStmt):
            # M5: if/match/bare-block are usable as expressions (a let's
            # value, a block's tail, a call argument, ...) -- resolving
            # their structure doesn't depend on statement-vs-expression
            # context at all, so delegate to the exact same resolution
            # logic already used when they appear as statements.
            self.resolve_stmt(expr)
            return
        if isinstance(expr, MatchStmt):
            self.resolve_stmt(expr)
            return
        if isinstance(expr, Block):
            self.resolve_block(expr)
            return
        raise AssertionError(f"unhandled expression node {expr!r}")

    # -- patterns (M4) -----------------------------------------------------
    #
    # Parallel to resolve_expr, but a separate dispatch since pattern node
    # types (WildcardPat/BindPat/StructPat/EnumPat) don't otherwise exist as
    # expressions -- NumberLit/StringLit/BoolLit are the one overlap,
    # reused as-is for literal patterns (equality, no name to resolve).

    def resolve_pattern(self, pattern) -> None:
        if isinstance(pattern, WildcardPat):
            return
        if isinstance(pattern, (NumberLit, StringLit, BoolLit)):
            return
        if isinstance(pattern, BindPat):
            slot = self.frame_stack[-1].alloc()
            self._declare(pattern.name, slot, pattern.position, kind="binding")
            pattern.address = slot
            return
        if isinstance(pattern, StructPat):
            # M12: `Self { ... }` pattern inside an impl method body.
            pattern.type_name = self._subst_self(pattern.type_name, pattern.position)
            declared = self.struct_decls.get(pattern.type_name)
            if declared is None:
                raise NameError(
                    f"Undefined struct type '{pattern.type_name}' at position {pattern.position}"
                )
            label = f"'{pattern.type_name}' pattern"
            self._check_no_duplicate_field(label, pattern.fields, pattern.position)
            provided = {name for name, _ in pattern.fields}
            self._check_field_set_matches(
                f"Struct pattern for '{pattern.type_name}'", provided, declared, pattern.position
            )
            for _name, sub in pattern.fields:
                self.resolve_pattern(sub)
            # LSP: register this use site -- see type_position_index's docstring.
            self.type_position_index[pattern.position] = ("struct", pattern.type_name)
            # LSP (M11): register each EXPLICIT field label's use site
            # (shorthand fields have fpos=None -- see field_position_index's
            # docstring).
            for (fname, _sub), fpos in zip(pattern.fields, pattern.field_name_positions):
                if fpos is not None:
                    self.field_position_index[fpos] = ("struct_field", pattern.type_name, fname)
            return
        if isinstance(pattern, EnumPat):
            # M12: `Self.Circle { ... }` pattern inside an impl method body.
            pattern.type_name = self._subst_self(pattern.type_name, pattern.position)
            variants = self.enum_decls.get(pattern.type_name)
            if variants is None:
                raise NameError(
                    f"Undefined enum type '{pattern.type_name}' at position {pattern.position}"
                )
            declared = variants.get(pattern.variant)
            if declared is None:
                raise Exception(
                    f"Enum '{pattern.type_name}' has no variant '{pattern.variant}' "
                    f"at position {pattern.position}"
                )
            label = f"'{pattern.type_name}.{pattern.variant}' pattern"
            self._check_no_duplicate_field(label, pattern.fields, pattern.position)
            provided = {name for name, _ in pattern.fields}
            self._check_field_set_matches(
                f"Enum pattern for '{pattern.type_name}.{pattern.variant}'",
                provided,
                declared,
                pattern.position,
            )
            for _name, sub in pattern.fields:
                self.resolve_pattern(sub)
            # LSP: register the type-name and variant-name use sites when
            # tracked -- see type_position_index's docstring.
            self.type_position_index[pattern.position] = ("enum", pattern.type_name)
            if pattern.variant_position is not None:
                self.type_position_index[pattern.variant_position] = ("variant", pattern.type_name, pattern.variant)
            # LSP (M11): register each EXPLICIT field label's use site --
            # see field_position_index's docstring.
            for (fname, _sub), fpos in zip(pattern.fields, pattern.field_name_positions):
                if fpos is not None:
                    self.field_position_index[fpos] = ("variant_field", pattern.type_name, pattern.variant, fname)
            return
        raise AssertionError(f"unhandled pattern node {pattern!r}")
