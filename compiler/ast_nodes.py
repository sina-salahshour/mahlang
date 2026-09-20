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

No enums/pattern matching/blocks-as-expressions/defer yet -- those land in
later milestones and will extend this module rather than replace it.

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


# -- blocks / statements ------------------------------------------------


@dataclass
class Block:
    stmts: list
    position: int
    # Reserved for M5 (expression-blocks) -- a block is not yet a value in
    # M0, so the parser never populates this; kept here so the shape
    # doesn't need to change again later.
    tail: Optional[object] = None


@dataclass
class LetStmt:
    name: str
    value: object
    position: int
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
class FnExpr:
    name: Optional[str]
    params: list  # list[str]
    body: Block
    position: int
    # set by Resolver: slot numbers (within the fn's own frame level) for
    # each parameter, in order.
    param_slots: list = field(default_factory=list, repr=False)
    # set by Resolver: the resolve.FrameLevel for this function's body --
    # codegen.py continues allocating temp slots from this same object.
    frame_level: object = field(default=None, repr=False)


@dataclass
class BlockStmt:
    block: Block
    position: int


@dataclass
class StructDecl:
    name: str
    fields: list  # list[str] -- declared field names, in declaration order
    position: int
