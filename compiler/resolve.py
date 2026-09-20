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
`struct_decls`/`enum_decls` registries above and are out of scope for M7's
rename (a distinct, larger piece of work; see the milestone entry).

M9 adds `DeferStmt` (see docs/V2_DESIGN.md's M9 milestone): its body is
already packaged by the parser as a synthesized, anonymous, zero-param
`FnExpr`, so resolving it is just `resolve_expr(stmt.closure_expr)` --
the existing `FnExpr` case creates a new frame level and resolves the
body, giving correct by-reference capture of enclosing variables with no
new resolve logic at all.
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
    EnumDecl,
    EnumLit,
    EnumPat,
    ErrorNode,
    ExprStmt,
    FieldAccess,
    FnExpr,
    Ident,
    IfStmt,
    InputExpr,
    LetStmt,
    MatchStmt,
    NumberLit,
    PrintStmt,
    ReturnStmt,
    SinExpr,
    StringLit,
    StructDecl,
    StructLit,
    StructPat,
    Unary,
    WhileStmt,
    WildcardPat,
)


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

    __slots__ = ("name", "decl_position", "kind", "references")

    def __init__(self, name: str, decl_position: int, kind: str):
        self.name = name
        self.decl_position = decl_position
        self.kind = kind  # "let" | "fn" | "param" | "binding"
        self.references: list = []  # positions (ints) of every Ident that resolved here


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
        # machinery as a user-declared enum -- see module docstring.
        self.enum_decls: dict = {"Option": {"none": [], "some": ["value"]}}
        # M7: source position -> Symbol, for every position that either
        # declared or referenced a variable/parameter/function-binding/
        # match-binding name. Gives the LSP's go-to-definition/rename
        # features an O(1) "what symbol is at this exact position" lookup
        # without re-walking the AST -- see docs/V2_DESIGN.md's M7
        # milestone.
        self.position_index: dict = {}

    # -- name table helpers ----------------------------------------------

    def _declare(self, name: str, slot: int, position: int, kind: str = "let") -> None:
        scope = self.scopes[-1]
        if name in scope:
            raise NameError(f"Error at position {position}: variable is already defined {name}")
        symbol = Symbol(name, position, kind)
        self.position_index[position] = symbol
        scope[name] = (self.frame_stack[-1], slot, symbol)

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

    # -- entry point ---------------------------------------------------

    def resolve_program(self, stmts: list) -> None:
        for stmt in stmts:
            self.resolve_stmt(stmt)

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
                self._declare(stmt.name, slot, name_position, kind="fn")
                stmt.address = slot
                self._resolve_fn_expr(stmt.value)
            else:
                self.resolve_expr(stmt.value)
                slot = self.frame_stack[-1].alloc()
                self._declare(stmt.name, slot, name_position, kind="let")
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
            self.struct_decls[stmt.name] = stmt.fields
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
            self.enum_decls[stmt.name] = {
                variant_name: variant_fields for variant_name, variant_fields in stmt.variants
            }
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

    def _resolve_fn_expr(self, fn: FnExpr) -> None:
        new_frame = FrameLevel(depth=self.frame_stack[-1].depth + 1, parent=self.frame_stack[-1])
        self.frame_stack.append(new_frame)
        self._push()
        for index, param_name in enumerate(fn.params):
            slot = new_frame.alloc()
            param_position = (
                fn.param_positions[index] if index < len(fn.param_positions) else fn.position
            )
            self._declare(param_name, slot, param_position, kind="param")
            fn.param_slots.append(slot)
        self.resolve_block(fn.body)
        self._pop()
        self.frame_stack.pop()
        fn.frame_level = new_frame

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
        if isinstance(expr, FnExpr):
            self._resolve_fn_expr(expr)
            return
        if isinstance(expr, StructLit):
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
            return
        if isinstance(expr, EnumLit):
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
            return
        if isinstance(expr, FieldAccess):
            if isinstance(expr.obj, Ident):
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
            return
        if isinstance(pattern, EnumPat):
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
            return
        raise AssertionError(f"unhandled pattern node {pattern!r}")
