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

M11 adds the same tracking one level down, for struct/enum FIELD names
(not just type/variant names): `StructDecl.field_positions`,
`EnumDecl.variant_field_positions`, `StructLit.field_name_positions`,
`EnumLit.field_name_positions`, `StructPat.field_name_positions`,
`EnumPat.field_name_positions` -- see each field's own comment below and
`compiler/resolve.py`'s `field_position_index` docstring. This powers
struct/enum field-name rename/hover/go-to-definition in declarations,
literals, and *explicit* (non-shorthand) patterns -- plain field access
(`p.x`) is deliberately never tracked here at all (unsound without a real
type system, see `docs/NEXT_PHASES.md`'s "Struct/enum/field rename"
section), and a *shorthand* pattern field (`{ x }`, no colon) is
deliberately excluded too, since that single token is simultaneously the
field name AND the local variable it binds -- see `StructPat.field_name_positions`
below for the full reasoning.

M12 adds `trait`/`impl` declarations and method calls: `MethodDecl` (one
`fn` item inside a `trait`/`impl` block -- required/default trait methods
have `fn=None`/a body respectively, every `impl` method has a body),
`TraitDecl`, `ImplDecl` (inherent when `trait_name is None`), and
`MethodCall` (`obj.method(args)`, plus the resolver-set `static_address`/
`trait_name` disambiguation for `Type.method(...)`/`Trait.method(recv,
...)` -- see `compiler/resolve.py`'s module docstring for the full
three-phase top-level resolution these introduce, and `compiler/codegen.py`
for the `defmethod`/`callmethod` opcodes they compile to).

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
    # M16: keyword arguments (`name: expr`), in source order, after every
    # positional arg -- list[tuple[str, object, int]] (name, value_expr,
    # the NAME token's own position, for error messages/LSP). Empty for an
    # ordinary all-positional call.
    kwargs: list = field(default_factory=list, repr=False)


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
    # M11: set by the parser -- the source position of each field NAME
    # token, one per `.fields` entry, in the same order. Lets the resolver
    # register a field-rename/hover/go-to-definition target for this exact
    # use site (`field_position_index`) -- see `compiler/resolve.py`.
    field_name_positions: list = field(default_factory=list, repr=False)


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
    # M11: set by the parser -- one list per entry in `.variants`, same
    # order, each inner list itself being one position per that variant's
    # own field NAME tokens (parallel to that variant's own field-name
    # list). E.g. for `enum Shape { Circle { r }, Empty }`, this is
    # `[[pos_of_r], []]`. See `compiler/resolve.py`'s `field_position_index`.
    variant_field_positions: list = field(default_factory=list, repr=False)


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
    # M11: set by the parser -- the source position of each field NAME
    # token, one per `.fields` entry, same order (parallel to `StructLit`'s
    # own `field_name_positions`). See `compiler/resolve.py`'s
    # `field_position_index`. NOT set for the built-in `none`/`some(x)`
    # construction sites (they build `.fields` directly, never through
    # `_parse_field_list`) -- left at the default empty list, same as
    # `type_name_position` is also left unset there.
    field_name_positions: list = field(default_factory=list, repr=False)


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
    call: object  # M13: a Call node, a MethodCall node, or (for `detach
                   # sleep_async(ms)`) a SleepAsyncExpr node -- the operand
                   # `detach` wraps; see the parser's `_parse_detach_operand`
                   # for why this must already be one of those three shapes
                   # by construction, and codegen.py for why each compiles
                   # completely differently (an ordinary Call/MethodCall
                   # spins up a real Task; sleep_async isn't a real Closure
                   # call at all, so "detaching" it just means skipping the
                   # auto-await a bare sleep_async(ms) gets)
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
    # M16: keyword-only options -- expressions, or `None` when not written
    # (codegen substitutes the default: `" "` for sep, `"\n"` for end).
    sep: Optional[object] = field(default=None, repr=False)
    end: Optional[object] = field(default=None, repr=False)


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
    # M16: parallel to `params` -- each entry is the default-value
    # expression for that parameter, or `None` if it has none. Set by the
    # parser (`_parse_param_list`); once a parameter has a default, every
    # later one must too (resolve.py enforces this).
    defaults: list = field(default_factory=list, repr=False)


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
    # M11: set by the parser -- one position per entry in `.fields`, same
    # order. See `compiler/resolve.py`'s `field_position_index` docstring.
    field_positions: list = field(default_factory=list, repr=False)


# -- M12: traits / impls / method calls ----------------------------------


@dataclass
class MethodDecl:
    """M12: one `fn` item inside a `trait` or `impl` block."""

    name: str
    params: list  # list[str]
    fn: Optional[object]  # FnExpr (name=method name, same params) or None for a
    # required (bodyless) trait method
    position: int  # the `fn` keyword's position
    name_position: Optional[int] = field(default=None, repr=False)
    param_positions: list = field(default_factory=list, repr=False)
    # set by Resolver: hidden global-frame slot holding this fn's Closure
    # (only when `fn` is not None)
    slot: Optional[int] = field(default=None, repr=False)
    # M16: parallel to `params`, set by the parser -- see `FnExpr.defaults`.
    # Recorded even for a bodyless (required) trait method (`fn is None`) so
    # the resolver can reject a default there with a clean message.
    defaults: list = field(default_factory=list, repr=False)

    @property
    def is_method(self) -> bool:
        return bool(self.params) and self.params[0] == "self"


@dataclass
class TraitDecl:
    name: str
    methods: list  # list[MethodDecl]
    position: int  # `trait` keyword position
    name_position: Optional[int] = field(default=None, repr=False)
    # M13: set by the parser -- the position of this trait block's closing
    # `}` token. Used by the resolver's `member_block_ranges` (LSP: what
    # `self` means at a cursor position inside a trait default body) --
    # see `compiler/resolve.py`'s module docstring.
    end_position: Optional[int] = field(default=None, repr=False)


@dataclass
class ImplDecl:
    type_name: str
    trait_name: Optional[str]  # None for an inherent `impl T { }`
    methods: list  # list[MethodDecl], every one has a body
    position: int  # `impl` keyword position
    type_name_position: Optional[int] = field(default=None, repr=False)
    trait_name_position: Optional[int] = field(default=None, repr=False)
    # M13: set by the parser -- the position of this impl block's closing
    # `}` token. See `TraitDecl.end_position` above.
    end_position: Optional[int] = field(default=None, repr=False)
    # set by Resolver: every (method_name, slot, is_method) this impl
    # registers at runtime -- its own fns PLUS inherited trait defaults
    # (for trait impls). Codegen emits one `defmethod` per entry.
    registrations: list = field(default_factory=list, repr=False)


@dataclass
class MethodCall:
    obj: object  # receiver expression, or an Ident naming a type/trait
    method: str
    args: list
    position: int  # the method-name token's position
    # M16: keyword arguments -- see `Call.kwargs`'s docstring.
    kwargs: list = field(default_factory=list, repr=False)
    # set by Resolver (at most one of these two is set):
    # static_address: `Type.fn(args)` resolved at compile time to the hidden
    #   global slot of that impl fn -> compiled as an ordinary `call`.
    static_address: Optional[tuple] = field(default=None, repr=False)
    # trait_name: dynamic dispatch on args[0] (the receiver) restricted to
    #   this trait -- used for `Trait.m(x, ...)` and for `BuiltinType.m(x)`
    #   when the target is a native impl.
    trait_name: Optional[str] = field(default=None, repr=False)
    # When neither is set, it's an ordinary dynamic method call on `obj`.
    # M13: set by Resolver -- a best-effort, purely advisory syntactic
    # guess at this call's return type (see `compiler/resolve.py`'s
    # `_syntactic_type_hint`/`_type_hint`), used only by the LSP for
    # further hover/completion type-hint propagation. Never used for
    # codegen/dispatch.
    return_hint: Optional[str] = field(default=None, repr=False)
    # M17: set by Resolver -- True when this is a static path call
    # (`Type.method(...)`) whose target is a native INHERENT method (no
    # trait involved at all, e.g. `String.len(s)`, `Function.arity(f)`).
    # There's no trait name to carry this through `trait_name` (that field
    # means "dispatch on args[0], restricted to this trait" and can't
    # represent "no trait" without colliding with the unset/ordinary-call
    # meaning of `None`) -- codegen instead emits a plain dynamic
    # `callmethod`/`callmethodkw` on `args[0]` with the remaining args and
    # `trait=None`, exactly mirroring the interpreter's own
    # inherent-before-trait dispatch order.
    native_inherent: bool = field(default=False, repr=False)


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
    # M11: set by the parser -- one entry per `.fields` entry, but `None`
    # for a SHORTHAND field (`{ x }`, no colon) and a real position for an
    # explicit one (`{ x: sub }`). Deliberate: for shorthand `{ x }`, the
    # single token `x` is *simultaneously* the field name being matched AND
    # the local variable being bound (`BindPat(name="x", ...)`, already
    # registered as an ordinary variable in `resolver.position_index` via
    # `_declare`) -- renaming "the field" vs. "the local variable it's
    # bound to" are two different, independent intents at that exact
    # position, so shorthand fields are deliberately NOT registered as
    # field-rename targets at all; renaming there stays exactly today's
    # variable-rename behavior. Only the unambiguous explicit form
    # (`x: sub`, two separate tokens) gets a field-rename registration --
    # see `compiler/resolve.py`'s `field_position_index`.
    field_name_positions: list = field(default_factory=list, repr=False)


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
    # M11: same `None`-for-shorthand rule as `StructPat.field_name_positions`
    # above -- see that field's comment for the full reasoning.
    field_name_positions: list = field(default_factory=list, repr=False)


@dataclass
class RangePat:
    """M17: a range pattern (`1..10`, `10..=15`, `..1`, `15..`) -- `lo`/`hi`
    are literal-pattern nodes (`NumberLit`/`StringLit`), or `None` when that
    bound is absent (a one-sided range). Bounds are literals only (no
    identifiers/expressions) -- see the parser's `_parse_pattern`. Compiles
    to the `matchrange` opcode (docs/MAHC_FORMAT.md #4.6/#6.3)."""

    lo: Optional[object]
    hi: Optional[object]
    inclusive: bool
    position: int


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
