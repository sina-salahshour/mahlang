"""AST node types for Mah v2.

M1 note: `fn` is now a single unified expression node, `FnExpr` (see
docs/V2_DESIGN.md's M1 milestone) -- there is no more `FnDeclStmt`. A
named `fn foo(...) { ... }` at statement level is parsed as sugar for
`let foo = fn(...) { ... }` (a `LetStmt` whose `value` is a `FnExpr`);
an anonymous `fn(...) { ... }` is a normal expression usable anywhere
(call argument, return value, assigned to a variable, etc).

M2 adds `struct` declarations/literals/field access (`StructDecl`,
`StructLit`, `FieldAccess`) -- see docs/V2_DESIGN.md's M2 milestone.
`AssignStmt.target` is now an expression node (`Ident` or `FieldAccess`)
instead of a bare name string.

M3 adds `enum` declarations/literals (`EnumDecl`, `EnumLit`) -- see
docs/V2_DESIGN.md's M3 milestone. The built-in `none`/`some(x)` desugar
straight into `EnumLit` at parse time (type_name="Option"), no separate AST
nodes. `FieldAccess` gains a resolver-set `enum_unit_type` field: a bare
`Type.Variant` (no braces) parses identically to ordinary field access
(`FieldAccess(Ident("Type"), "Variant")`) since the two are syntactically
indistinguishable at parse time -- the resolver disambiguates them (see
resolve.py's module docstring) and sets `enum_unit_type` when it turns out
to be a unit-variant construction rather than a real field access.

M4 adds `match` statements and patterns (`WildcardPat`, `BindPat`,
`StructPat`, `EnumPat`, `MatchArm`, `MatchStmt`) -- see docs/V2_DESIGN.md's
M4 milestone. `match` is a **statement** in M4 (like `if`/`while` still
are), not yet an expression -- each arm's body is a `Block`, not a single
expression. No separate `LiteralPat`/`SomePat`/`NonePat` nodes: literal
patterns reuse `NumberLit`/`StringLit`/`BoolLit` directly, and
`some(pattern)`/`none` in pattern position desugar straight into `EnumPat`
at parse time, exactly mirroring `some(x)`/`none` in expression position.

M5 makes `if`/`match`/bare `{ }` blocks into expressions: `Block.tail`
(reserved since M0) is now actually populated by the parser, and a bare
`{ }` block used as a statement is just `ExprStmt(value=Block(...), ...)`
-- the old dedicated `BlockStmt` wrapper node is retired, since it added
nothing `ExprStmt` doesn't already say. `IfStmt`/`MatchStmt` are unchanged
structurally; only their *codegen* differs by context (statement position
vs. used as an expression/tail) -- see docs/V2_DESIGN.md's M5 milestone.

M6 adds `ErrorNode` (see its own docstring below) -- the parser's forgiving-
error-recovery mechanism, usable wherever an expression is expected.

M9 adds `DeferStmt` (see docs/V2_DESIGN.md's M9 milestone) -- its
`closure_expr` is always a synthesized, anonymous, zero-param `FnExpr`
wrapping the deferred statement's body, letting M1's existing closure
machinery handle capture with no new resolve logic.

M10 adds `DetachExpr`/`SleepAsyncExpr` for async (`detach`/`sleep_async`,
see docs/V2_DESIGN.md's M10 milestone) -- `.await` needs no new AST node
at all, it's an ordinary `FieldAccess` with `field="await"`, special-cased
only in codegen.

LSP note: `StructDecl.name_position`, `EnumDecl.name_position`/
`variant_positions`, `EnumLit.type_name_position`, and
`EnumPat.variant_position` add enum/struct declaration + reference
position tracking for the LSP, landing alongside the corresponding
`compiler/resolve.py` additions (`type_position_index` and friends).

Every node carries `position` (a source offset into the *combined*,
preprocessed text) so error messages can point mah.py at a `file:line:col`
the same way v1's did.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional


# -- expressions -------------------------------------------------------


@dataclass
class NumberLit:
    value: Decimal
    position: int


@dataclass
class StringLit:
    value: str
    position: int


@dataclass
class BoolLit:
    value: bool
    position: int


@dataclass
class Ident:
    name: str
    position: int
    # set by Resolver: the resolved (depth, slot) address -- depth is the
    # number of static_parent hops from the currently executing frame to
    # reach the frame owning `slot`.
    address: Optional[tuple] = field(default=None, repr=False)


@dataclass
class Unary:
    op: str  # "-"
    operand: object
    position: int


@dataclass
class Binary:
    op: str  # "+" "-" "*" "/" "//" "%" "**" "eq" "neq" "lt" "gt" "and" "or"
    lhs: object
    rhs: object
    position: int


@dataclass
class Call:
    callee: object  # an expression, in practice always an Ident
    args: list
    position: int


@dataclass
class StructLit:
    type_name: str
    fields: list  # list[tuple[str, object]] -- (field_name, value_expr) pairs,
                   # in the order written in the literal (NOT necessarily
                   # declaration order) -- kept as an ordered list, not a
                   # dict, so the resolver can detect a duplicate field name
                   # written twice in one literal (a dict would silently
                   # drop it)
    position: int


@dataclass
class FieldAccess:
    obj: object  # an expression (an Ident, another FieldAccess, a Call, etc.)
    field: str
    position: int
    # set by Resolver, explicitly, on every FieldAccess node (never left at
    # this default and hoped): None for ordinary field access (the common
    # case, exactly as M2 produced); otherwise the enum type name (same
    # string as `obj.name`) when this node is actually a bare enum
    # unit-variant construction like `Shape.Empty` -- syntactically
    # identical to `p.field` at parse time, disambiguated only once the
    # resolver knows `obj.name` isn't a variable in scope but is a declared
    # enum type with a matching unit variant. See resolve.py's module
    # docstring for the full disambiguation rule.
    enum_unit_type: Optional[str] = field(default=None, repr=False)


@dataclass
class EnumDecl:
    name: str
    variants: list  # list[tuple[str, list[str]]] -- (variant_name, field_names);
                      # field_names is [] for a unit variant (uniform
                      # representation, no separate None-vs-list case)
    position: int
    # set by the parser: the source position of the enum's own name token
    # (`position` above is the `enum` keyword's position) -- see
    # `StructDecl.name_position` for the exact same reasoning.
    name_position: Optional[int] = field(default=None, repr=False)
    # set by the parser: the source position of each variant's own name
    # token, in the same order as `variants` -- mirrors `FnExpr`'s
    # `param_positions`-parallel-to-`params` convention.
    variant_positions: list = field(default_factory=list, repr=False)


@dataclass
class EnumLit:
    type_name: str
    variant: str
    fields: list  # list[tuple[str, expr]] -- [] for a unit variant literal
    position: int
    # set by the parser: the source position of the type name token (e.g.
    # "Shape" in `Shape.Circle { r: 5 }`) -- `position` above is already
    # the *variant* name's position, set via `field_tok.position` in
    # `_parse_postfix_from`.
    type_name_position: Optional[int] = field(default=None, repr=False)


@dataclass
class SinExpr:
    arg: object
    position: int


@dataclass
class CosExpr:
    arg: object
    position: int


@dataclass
class InputExpr:
    position: int


@dataclass
class DetachExpr:
    call: object  # a Call node, or (for `detach sleep_async(ms)`) a
                   # SleepAsyncExpr node -- the operand `detach` wraps; see
                   # the parser for why this must already be one of those
                   # two shapes by construction, and codegen.py for why
                   # each compiles completely differently (an ordinary
                   # Call spins up a real Task; sleep_async isn't a real
                   # Closure call at all, so "detaching" it just means
                   # skipping the auto-await a bare sleep_async(ms) gets)
    position: int


@dataclass
class SleepAsyncExpr:
    arg: object  # milliseconds, an expression
    position: int


@dataclass
class ErrorNode:
    """M6: produced by the parser in place of an expression it couldn't
    parse, instead of aborting the whole parse -- see docs/V2_DESIGN.md's
    M6 milestone ("Forgiving errors"). Always arrives wrapped in an
    `ExprStmt` (the parser's per-item recovery granularity is a whole
    top-level item, never a sub-expression -- see `Parser._parse_block_items`),
    so `resolve.py`/`codegen.py` only need a case in their expression
    dispatch, not a separate statement dispatch. Both passes treat it
    exactly like a missing/implicit `none` (the same default already used
    for a function's implicit return and a semicolon-terminated block's
    value)."""

    message: str
    position: int


# -- blocks / statements ------------------------------------------------


@dataclass
class Block:
    stmts: list
    position: int
    # M5: a block's value is this trailing expression (populated by the
    # parser whenever the last item in the block has no trailing `;`), or
    # `None` when the block ends in a semicolon-terminated statement or is
    # empty -- codegen treats a `None` tail as `none` (see
    # docs/V2_DESIGN.md's M5 milestone).
    tail: Optional[object] = None


@dataclass
class LetStmt:
    name: str
    value: object
    position: int  # position of the leading `let`/`fn` keyword token -- kept
                    # as-is (pre-M7 error messages point here); NOT the name's
                    # own position, see `name_position` below.
    # set by the parser: the source position of the name token itself
    # (`name_tok.position` for a plain `let`, or the desugared `fn`'s own
    # name-token position for a named `fn foo(...) { ... }` binding) --
    # M7 needs this exact span for go-to-definition/rename (`position`
    # above points at the `let`/`fn` keyword, not the identifier, so it
    # can't be used to compute a correct rename TextEdit range). Falls
    # back to `position` when unset (should not happen via the parser, but
    # keeps any other LetStmt construction site safe).
    name_position: Optional[int] = field(default=None, repr=False)
    # set by Resolver: the slot number within the frame level this
    # LetStmt was declared in (depth is always implicitly 0 from its own
    # declaration site).
    address: Optional[int] = field(default=None, repr=False)


@dataclass
class AssignStmt:
    target: object  # an Ident or a FieldAccess, constructed by the parser --
                     # an Ident target's own `.address` (set by resolve_expr)
                     # is reused as-is; a FieldAccess target's codegen reads
                     # `target.obj`/`target.field` directly.
    value: object
    position: int


@dataclass
class ExprStmt:
    value: object
    position: int


@dataclass
class PrintStmt:
    args: list
    position: int


@dataclass
class IfStmt:
    cond: object
    then: Block
    elifs: list  # list[tuple[Expr, Block]]
    else_: Optional[Block]
    position: int


@dataclass
class WhileStmt:
    cond: object
    body: Block
    position: int


@dataclass
class BreakStmt:
    position: int


@dataclass
class ContinueStmt:
    position: int


@dataclass
class ReturnStmt:
    value: Optional[object]
    position: int


@dataclass
class DeferStmt:
    """M9: `defer <stmt>` -- see docs/V2_DESIGN.md's M9 milestone and this
    module's docstring. `closure_expr` is a synthesized, always-anonymous,
    zero-param `FnExpr` wrapping the deferred statement's body (built by
    the parser's `_parse_defer_stmt`), reusing M1's closure/frame
    machinery entirely unchanged for correct by-reference variable
    capture, with zero new resolve logic."""

    closure_expr: object  # a synthesized, always-anonymous, zero-param FnExpr
                           # wrapping the deferred statement's body
    position: int


@dataclass
class FnExpr:
    name: Optional[str]
    params: list  # list[str]
    body: Block
    position: int  # position of the leading `fn` keyword token, NOT the
                    # name token -- see `name_position` below.
    # set by the parser: the source position of `name`'s own token (`None`
    # for an anonymous `fn`) -- when the parser desugars a named
    # `fn foo(...) { ... }` into `LetStmt(name="foo", value=FnExpr(...))`
    # (see `_parse_block_items`), the wrapping `LetStmt` needs this exact
    # position (not `position` above, the `fn` keyword's) for its own
    # `name_position` -- M7's go-to-definition/rename need the identifier's
    # exact span, the same reasoning `param_positions` below exists for.
    name_position: Optional[int] = field(default=None, repr=False)
    # set by the parser: the source position of each parameter token, in
    # the same order as `params` -- M7 needs each parameter's own
    # declaration position to register it in the resolver's symbol table
    # (see resolve.py's `_resolve_fn_expr`), which `params` alone (bare
    # strings) can't provide.
    param_positions: list = field(default_factory=list, repr=False)
    # set by Resolver: slot numbers (within the fn's own frame level) for
    # each parameter, in order.
    param_slots: list = field(default_factory=list, repr=False)
    # set by Resolver: the resolve.FrameLevel for this function's body --
    # codegen.py continues allocating temp slots from this same object.
    frame_level: object = field(default=None, repr=False)


@dataclass
class StructDecl:
    name: str
    fields: list  # list[str] -- declared field names, in declaration order
    position: int  # position of the `struct` keyword token, NOT the name's --
                    # see `name_position` below.
    # set by the parser: the source position of the struct's own name token
    # -- M-LSP needs this exact span for hover/go-to-definition, the same
    # reasoning `LetStmt.name_position`/`FnExpr.name_position` exist for.
    name_position: Optional[int] = field(default=None, repr=False)


# -- M4: patterns / match ------------------------------------------------
#
# No separate LiteralPat node: NumberLit/StringLit/BoolLit are reused
# directly as patterns (they're structurally identical -- a literal value
# to compare against). No separate SomePat/NonePat either: `some(pattern)`/
# `none` in pattern position desugar straight into EnumPat(type_name=
# "Option", ...) at parse time, mirroring how docs/V2_DESIGN.md already
# documents `some(x)`/`none` desugaring into EnumLit in expression position.


@dataclass
class WildcardPat:
    position: int


@dataclass
class BindPat:
    name: str
    position: int
    # set by Resolver: the slot number (within the enclosing frame level)
    # this binding's value is stored into.
    address: Optional[int] = field(default=None, repr=False)


@dataclass
class StructPat:
    type_name: str
    fields: list  # list[tuple[str, pattern]]
    position: int


@dataclass
class EnumPat:
    type_name: str
    variant: str
    fields: list  # list[tuple[str, pattern]] -- [] for a unit variant
    position: int  # position of the *type* name token (e.g. "Shape" in
                    # `Shape.Circle { r }`) -- set via `tok.position` in
                    # `_parse_pattern`; see `variant_position` below.
    # set by the parser: the source position of the variant name token --
    # `position` above is already the type name's position, this fills the
    # gap for the variant part (mirrors `EnumLit.type_name_position`).
    variant_position: Optional[int] = field(default=None, repr=False)


@dataclass
class MatchArm:
    pattern: object
    body: Block
    position: int


@dataclass
class MatchStmt:
    scrutinee: object  # Expr
    arms: list  # list[MatchArm]
    position: int
