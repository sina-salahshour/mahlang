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
"""

from __future__ import annotations

from .ast_nodes import (
    AssignStmt,
    Binary,
    Block,
    BlockStmt,
    BoolLit,
    BreakStmt,
    Call,
    ContinueStmt,
    CosExpr,
    ExprStmt,
    FieldAccess,
    FnExpr,
    Ident,
    IfStmt,
    InputExpr,
    LetStmt,
    NumberLit,
    PrintStmt,
    ReturnStmt,
    SinExpr,
    StringLit,
    StructDecl,
    StructLit,
    Unary,
    WhileStmt,
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

    # -- name table helpers ----------------------------------------------

    def _declare(self, name: str, slot: int, position: int) -> None:
        scope = self.scopes[-1]
        if name in scope:
            raise NameError(f"Error at position {position}: variable is already defined {name}")
        scope[name] = (self.frame_stack[-1], slot)

    def _lookup(self, name: str, position: int):
        for scope in reversed(self.scopes):
            if name in scope:
                return scope[name]
        raise NameError(f"Undefined variable '{name}' at position {position}")

    def _resolve_ident_address(self, name: str, position: int) -> tuple:
        frame_level, slot = self._lookup(name, position)
        depth = self.frame_stack[-1].depth - frame_level.depth
        return (depth, slot)

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
            if isinstance(stmt.value, FnExpr):
                # Declare before resolving the body -- enables self-reference
                # (recursion) for named function bindings. See module docstring.
                slot = self.frame_stack[-1].alloc()
                self._declare(stmt.name, slot, stmt.position)
                stmt.address = slot
                self._resolve_fn_expr(stmt.value)
            else:
                self.resolve_expr(stmt.value)
                slot = self.frame_stack[-1].alloc()
                self._declare(stmt.name, slot, stmt.position)
                stmt.address = slot
        elif isinstance(stmt, AssignStmt):
            self.resolve_expr(stmt.target)
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
        elif isinstance(stmt, (BreakStmt, ContinueStmt)):
            pass
        elif isinstance(stmt, ReturnStmt):
            if stmt.value is not None:
                self.resolve_expr(stmt.value)
        elif isinstance(stmt, BlockStmt):
            self.resolve_block(stmt.block)
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
        else:
            raise AssertionError(f"unhandled statement node {stmt!r}")

    def resolve_block(self, block: Block) -> None:
        self._push()
        for stmt in block.stmts:
            self.resolve_stmt(stmt)
        self._pop()

    def _resolve_fn_expr(self, fn: FnExpr) -> None:
        new_frame = FrameLevel(depth=self.frame_stack[-1].depth + 1, parent=self.frame_stack[-1])
        self.frame_stack.append(new_frame)
        self._push()
        for param_name in fn.params:
            slot = new_frame.alloc()
            self._declare(param_name, slot, fn.position)
            fn.param_slots.append(slot)
        self.resolve_block(fn.body)
        self._pop()
        self.frame_stack.pop()
        fn.frame_level = new_frame

    # -- expressions -----------------------------------------------------

    def resolve_expr(self, expr) -> None:
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
            seen = set()
            for name, _value_expr in expr.fields:
                if name in seen:
                    raise Exception(
                        f"Field '{name}' specified more than once in "
                        f"'{expr.type_name}' literal at position {expr.position}"
                    )
                seen.add(name)
            provided = {name for name, _ in expr.fields}
            declared_set = set(declared)
            missing = declared_set - provided
            unknown = provided - declared_set
            if missing or unknown:
                parts = []
                if missing:
                    parts.append(f"missing field(s) {sorted(missing)}")
                if unknown:
                    parts.append(f"unknown field(s) {sorted(unknown)}")
                raise Exception(
                    f"Struct literal for '{expr.type_name}' has {' and '.join(parts)} "
                    f"at position {expr.position}"
                )
            for _name, value_expr in expr.fields:
                self.resolve_expr(value_expr)
            return
        if isinstance(expr, FieldAccess):
            # Deliberate simplification: the field name itself is NOT
            # validated here against any struct shape -- without a real
            # type system there's no reliable way to know what struct type
            # a given expression's value will hold at compile time (e.g. a
            # function parameter has no static type annotation). Field
            # names are validated at *runtime* instead, in
            # code_interpreter.py's `getfield`/`setfield` handlers. See
            # docs/V2_DESIGN.md's M2 milestone.
            self.resolve_expr(expr.obj)
            return
        raise AssertionError(f"unhandled expression node {expr!r}")
