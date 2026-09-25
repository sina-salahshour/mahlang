"""Codegen pass: walks the *resolved* AST and emits 3-address IR tuples
`(op, arg1, arg2, dest)` that code_interpreter.py runs (see
docs/RUNTIME.md and docs/V2_DESIGN.md's M1 milestone).

M1 IR shape: every operand that used to be a bare integer address is now
a `(depth, slot)` tuple -- read at runtime by starting at the currently
executing `Frame` and walking `.static_parent` `depth` times, then
indexing `.slots[slot]` (see runtime_values.py / code_interpreter.py's
`_read`/`_write`). Codegen never inspects `depth`/`slot` itself; it just
carries whatever the resolver (or `_temp()`, always `(0, slot)`) produced.

New/changed opcodes for the heap-frame calling convention:
- `closure`: `arg1` = code address to jump to, `arg2` = `(slot_count,
  param_count, name)` metadata, `dest` = where to store the freshly
  created `Closure` value (capturing the *current* frame as its
  `defining_frame`).
- `call`: `arg1` = address of the `Closure` value to invoke, `arg2` = a
  tuple of argument addresses, `dest` unused (`None`) -- the callee's
  frame is built at runtime from `Closure.slot_count`/`param_count`, and
  arity is checked there too, since the callee is not always statically
  known (it may be a closure value passed around at runtime).
- `ret`: `arg1` = address of the return value, which is copied into an
  interpreter-local "return register" (not a `Frame` slot -- mirrors a
  hardware calling convention's return register); `arg2`/`dest` unused.
- `retval`: (new) no operands but `dest` -- copies the return register
  into `dest`, which is only valid to run immediately after a `call`
  returns, once the caller's frame is current again (this two-instruction
  `call`/`retval` split exists because the destination-in-caller can't be
  known until after the call returns).

If/while backpatching is unchanged from M0: emit a placeholder, remember
its address, patch it once the jump target is known.

New opcodes for M2 (`struct`, see docs/V2_DESIGN.md's M2 milestone):
- `struct`: `arg1` = type name (string), `arg2` = tuple of `(field_name,
  value_addr)` pairs, `dest` = where to store the newly constructed
  `StructInstance`.
- `getfield`: `arg1` = address of the struct value, `arg2` = field name
  (string), `dest` = where to store the read value.
- `setfield`: `arg1` = address of the struct value, `arg2` = field name
  (string), and the 4th slot -- named `dest` for every other opcode -- is
  repurposed here to hold the address of the value being *stored*, not a
  destination: there is no actual destination, since the mutation happens
  in place on the heap object itself. Call this out so it doesn't read as
  a mistake later: `setfield`'s 4-tuple has two "source" operands and zero
  "dest" operands.
`StructDecl` emits no instructions at all -- it's purely a compile-time
declaration consumed by resolve.py's `struct_decls` registry.

New opcode for M3 (`enum`, see docs/V2_DESIGN.md's M3 milestone):
- `enum`: `arg1` = enum type name (string), `arg2` = `(variant_name,
  field_pairs)` where `field_pairs` is a tuple of `(field_name, value_addr)`
  pairs (empty tuple for a unit variant), `dest` = where to store the newly
  constructed `EnumInstance`. `EnumDecl` emits no instructions, exactly
  like `StructDecl` -- purely a compile-time declaration consumed by
  resolve.py's `enum_decls` registry.
`FieldAccess` codegen checks the resolver-set `expr.enum_unit_type` first:
when set, `expr.obj` was never actually resolved as a variable (its
`.address` is `None`, since the resolver determined this node is really a
bare `Type.Variant` unit-variant construction, not field access -- see
resolve.py's module docstring), so `gen_expr(expr.obj)` must NOT be called;
instead this emits the `enum` opcode directly with zero fields. The
built-in `none` literal is special-cased further still, to keep it a true
reused singleton rather than a freshly allocated (if value-equal) object
per use: `EnumLit(type_name="Option", variant="none")` emits a plain `ld`
of the shared `runtime_values.NONE_VALUE` object instead of the generic
`enum` construction instruction.

New opcodes for M4 (`match`, see docs/V2_DESIGN.md's M4 milestone):
- `matchtag`: `arg1` = address of the value being tested, `arg2` =
  `(kind, type_name, variant_or_none)` where `kind` is `"struct"` or
  `"enum"` (`variant_or_none` is `None` for a struct pattern), `dest` =
  boolean result (matched or not).
- `matchfail`: `arg1` = `None`, `arg2` = `None`, and the 4th slot -- `dest`
  for every other opcode -- is repurposed to hold the source position (for
  the error message), mirroring `setfield`'s precedent of repurposing that
  slot when there's no real destination: `matchfail` never writes anywhere,
  it always raises.
`_gen_match`/`_gen_pattern_check` compile each arm's pattern into a chain
of checks that all thread into one shared `failure_jumps` list per arm
(collected across arbitrarily nested sub-patterns, via one `failure_jumps`
list passed down the recursion) -- once every check in that arm's pattern
has been emitted and the arm's body compiled, every one of that arm's
placeholders gets patched to jump to the next arm's first instruction. This
gives correct short-circuit AND semantics (an earlier failing check skips
straight to the next arm, never reaching later checks in the same pattern
or the body) at arbitrary nesting depth, using the exact same
emit-placeholder-then-backpatch primitive `_gen_if`/`_gen_while` already
use -- no new control-flow mechanism. If no arm's pattern matches, control
falls through to a `matchfail` instruction emitted once at the end of the
whole `match` (M4 has no exhaustiveness checking -- see docs/NEXT_PHASES.md
-- so this is a genuine runtime possibility, not just a safety net).

M5 (see docs/V2_DESIGN.md's M5 milestone) unifies `_gen_if`/`_gen_match`
into destination-threading `_gen_if_into`/`_gen_match_into`: every branch/
arm body writes its value into a caller-supplied `dest` address via the new
`_gen_block_into` (a block's tail value, or `none` if it has none) instead
of only running for side effects. `gen_stmt`'s `IfStmt`/`MatchStmt` cases
are now thin callers of these, passing a throwaway `self._temp()` as
`dest`; `gen_expr` gains matching `IfStmt`/`MatchStmt`/`Block` cases that
do the same but return the `dest` address, so `if`/`match`/bare blocks work
as expressions (let value, call argument, function tail, ...) via the
exact same codegen, with no special-casing of nesting depth -- an `if`
inside a `match` arm's tail, a block inside an `if` branch, etc., all fall
out for free from `gen_expr`/`_gen_block_into`/`_gen_if_into`/
`_gen_match_into` recursively calling back into each other as needed.
`_gen_fn_expr`'s implicit-return trailer changes from unconditionally
loading `NONE_VALUE` to using `gen_block`'s returned tail address when
there is one -- generalizing "a function that falls off the end always
returns `none`" (M1) to "a function that falls off the end returns its
body block's tail value, which is `none` if there is none," a strict
superset since M0-M4 never had syntax that could produce a non-`None`
tail (see this milestone's writeup for why that's safe). `BlockStmt`'s old
`gen_stmt` case is retired -- a bare block used as a statement is now
`ExprStmt(value=Block(...))`, compiled via the ordinary `ExprStmt`
dispatch (`gen_expr` on a `Block` with a throwaway temp, discarded).

M12 (see docs/V2_DESIGN.md's M12 milestone) removes the fixed 400-
instruction cap -- `CodeBuffer.code` is now a plain growing list instead of
a pre-allocated `[None] * 1000` array (`CODE_LIMIT` was 400 before M12; it's
now a 1,000,000-instruction sanity ceiling, not a real program size limit).
New opcodes for traits/impl/method calls:
- `defmethod`: `arg1` = address of the Closure, `arg2` = `(type_name,
  trait_name_or_None, method_name, is_method)`, `dest` = None. Registers
  one entry in code_interpreter.py's runtime `method_table` -- emitted once
  per `ImplDecl.registrations` entry, at program start (before the first
  ordinary top-level statement runs), so every method is available for
  dispatch regardless of where in the file it's textually declared.
- `callmethod`: `arg1` = receiver address, `arg2` = `(method_name,
  arg_addrs_tuple_excluding_receiver, trait_name_or_None,
  source_position)`, `dest` = None; ALWAYS immediately followed by a
  `retval` (same call/retval split as `call` -- the callee's frame is only
  known/switched at runtime, so the destination-in-caller write can't
  happen until control returns).
`MethodCall` codegen picks one of three shapes depending on what the
resolver set on the node (see `ast_nodes.py`'s `MethodCall` docstring):
`static_address` set compiles to an ordinary `call`/`retval` pair (no
`callmethod` at all -- the resolver already pinned down exactly which
Closure this is, at compile time, so there's nothing left to dispatch on
at runtime); `trait_name` set or neither set both compile to
`callmethod`/`retval`, differing only in whether `trait_name` narrows
dispatch to one specific trait or leaves it to the interpreter's normal
inherent-then-single-trait-else-ambiguous lookup. `TraitDecl`/`ImplDecl`
themselves emit no instructions in `gen_stmt` (like `StructDecl`/
`EnumDecl`) -- all of their runtime effect is the `generate`-time
`defmethod` hoisting described above.

M13 extends `detach` to accept a `MethodCall` operand (`detach
obj.method(args)`, `detach Type.method(args)`, `detach Trait.method(recv,
...)`), not just a plain `Call` -- see the parser's `_parse_detach_operand`.
New opcode: `detachmethod` -- same operand shape as `callmethod` (`arg1` =
receiver address, `arg2` = `(method_name, arg_addrs_tuple_excluding_
receiver, trait_name_or_None, source_position)`), but `dest` receives a
(possibly still-pending) `Promise`, mirroring `detach`, and there is no
following `retval` (unlike `callmethod`, which is always paired with one).
`DetachExpr` codegen picks between three shapes for a `MethodCall` operand,
mirroring `_gen_method_call`: `static_address` set -> an ordinary `detach`
(the callee address is just the hidden global slot, exactly like a plain
`detach some_fn(...)`); `trait_name` set or neither set -> `detachmethod`,
the receiver being `args[0]` for the former and `obj` for the latter.

New opcodes for M9 (`defer`, see docs/V2_DESIGN.md's M9 milestone): a
`DeferStmt` is compiled as the body of a synthesized zero-arg `FnExpr`
(parser-built), so `defer <stmt>` codegen is just `gen_expr` on that
closure followed by `deferadd` (push it onto the current runtime defer
scope). `gen_block` conditionally emits `deferpush`/drains-and-pops a
defer scope around a block's normal statement/tail codegen -- but ONLY
when that block directly contains at least one `DeferStmt` in
`block.stmts` (not nested inside a sub-block), so a block with no direct
defer costs zero extra instructions (important: `CODE_LIMIT` is 400 and
no pre-M9 example uses `defer`). `_emit_drain_one_defer_scope` emits a
small runtime loop around three new opcodes -- `deferpeek` (any pending
closures left?), `deferpopclosure` (pop the most-recently-pushed one),
`deferscopepop` (discard the now-empty scope) -- and invokes each popped
closure through the ordinary `call`/`retval` opcodes, discarding its
return value, so no new call mechanism is needed. A compile-time counter,
`self._defer_depth`, tracks how many defer-scopes are open relative to
the start of the current function's (or top-level program's) own body;
`return`/`break`/`continue` call `_emit_defer_unwind` to drain exactly
the right number of scopes (all of them for `return`; only the ones
opened since loop entry for `break`/`continue`, via each loop context's
saved `defer_depth_at_entry`) before actually jumping, since either can
jump out of multiple nested blocks at once. `_gen_fn_expr` saves/resets/
restores `_defer_depth` around a function's own body, parallel to
`frame_stack`/`_fn_depth`, so a nested function's `return` never unwinds
an enclosing function's or loop's scopes. `generate` (the top-level
entry point) applies the same conditional push/drain to the top-level
statement list, so a top-level `defer` runs at program end.
"""

from __future__ import annotations

from decimal import Decimal

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
    ForStmt,
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
    RangePat,
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
from ..runtime_values import NONE_VALUE

# M12: was a hard 400-instruction program-size limit before M12; now just a
# sanity ceiling far beyond any real program, guarding against a genuine
# runaway/bug rather than rejecting ordinary-sized programs -- see this
# module's docstring.
CODE_LIMIT = 1_000_000


class CodeBuffer:
    """Holds the emitted instruction array -- the direct replacement for
    v1's `IRGenerator.sstack`/`write_code`/`get_temp_address`."""

    def __init__(self):
        # M12: a growing list instead of a pre-allocated `[None] * 1000`
        # array -- see this module's docstring and `CODE_LIMIT` above.
        self.code: list = []
        self.code_pointer = 0
        self.global_slot_count = 0
        # M14: parallel to `code` -- the source position (a combined-text
        # offset, or None) in effect when each instruction was emitted, for
        # `mah/bytecode/lower.py`'s DEBUG section. Appended on a normal
        # emit; a backpatch-emit (an explicit `address`) overwrites an
        # existing slot's *code*, not its position, which was already
        # correctly recorded when that placeholder was first emitted -- see
        # `Codegen._pos`/`current_pos` below.
        self.positions: list = []
        # M14: the position `Codegen` wants attributed to the next normal
        # emit -- set by `Codegen.gen_stmt`/`gen_expr`/`_gen_pattern_check`
        # on entry (see those methods), read here on every normal emit.
        self.current_pos = None

    def emit(self, code, address: int | None = None) -> int:
        if address is None:
            if self.code_pointer >= CODE_LIMIT:
                raise RuntimeError("CodeBlock is full")
            addr = self.code_pointer
            self.code.append(code)
            self.positions.append(self.current_pos)
            self.code_pointer += 1
            return addr
        self.code[address] = code
        return address


class Codegen:
    def __init__(self, global_frame_level):
        self.buf = CodeBuffer()
        self.frame_stack = [global_frame_level]
        self._while_stack: list[dict] = []
        self._fn_depth = 0
        # M9: how many defer-scopes are currently open, relative to the
        # start of the current function's (or the top-level program's) own
        # body -- see `_emit_defer_unwind`/`gen_block`/`_gen_fn_expr`.
        self._defer_depth = 0
        # M14: the source position (a combined-text offset) attributed to
        # whatever gets emitted next -- see `gen_stmt`/`gen_expr`/
        # `_gen_pattern_check` and `CodeBuffer.current_pos`/`positions`.
        self._pos = None

    def _temp(self) -> tuple:
        return (0, self.frame_stack[-1].alloc())

    def generate(self, stmts: list) -> CodeBuffer:
        # M12: hoist every trait-default/impl method closure and its
        # `defmethod` registration BEFORE anything else -- including the M9
        # defer push below -- so every method is registered and callable
        # from the very first top-level statement, regardless of where in
        # the file its `trait`/`impl` block is textually written (mirrors
        # resolve.py's phase-1/3 hoisting). Two separate loops: every
        # closure must exist (loop 1) before any `defmethod` runs (loop 2),
        # since a trait impl's registrations can reference an inherited
        # trait-default slot that a DIFFERENT statement (the `trait`
        # itself) populated.
        for stmt in stmts:
            if isinstance(stmt, (TraitDecl, ImplDecl)):
                for method in stmt.methods:
                    if method.fn is not None:
                        addr = self.gen_expr(method.fn)
                        self.buf.emit(("=", addr, None, (0, method.slot)))
        for stmt in stmts:
            if isinstance(stmt, ImplDecl):
                for name, slot, is_method in stmt.registrations:
                    self.buf.emit(
                        ("defmethod", (0, slot), (stmt.type_name, stmt.trait_name, name, is_method), None)
                    )
        # M9: the top-level statement list is treated exactly like a
        # block's own `stmts` -- push/drain a defer scope only when a
        # top-level `defer` is directly present, so a top-level defer
        # correctly runs at program end, right before the halt sentinel.
        has_defer = any(isinstance(s, DeferStmt) for s in stmts)
        if has_defer:
            self.buf.emit(("deferpush", None, None, None))
            self._defer_depth += 1
        for stmt in stmts:
            self.gen_stmt(stmt)
        if has_defer:
            self._emit_defer_unwind(1)
            self._defer_depth -= 1
        self.buf.emit((None, None, None, None))
        self.buf.global_slot_count = self.frame_stack[-1].next_slot
        return self.buf

    # -- statements ------------------------------------------------------

    def gen_stmt(self, stmt) -> None:
        # M14: track this node's source position for DEBUG (see `_pos`'s
        # docstring above) -- saved/restored around the whole dispatch so a
        # nested `gen_expr`/`gen_stmt`/`_gen_pattern_check` call temporarily
        # overriding `_pos` for its own subtree doesn't leak back out to
        # whatever this statement emits AFTER that nested call returns.
        saved_pos = self._pos
        position = getattr(stmt, "position", None)
        if position is not None:
            self._pos = position
        self.buf.current_pos = self._pos
        try:
            self._gen_stmt(stmt)
        finally:
            self._pos = saved_pos
            self.buf.current_pos = self._pos

    def _gen_stmt(self, stmt) -> None:
        if isinstance(stmt, LetStmt):
            src = self.gen_expr(stmt.value)
            self.buf.emit(("=", src, None, (0, stmt.address)))
        elif isinstance(stmt, AssignStmt):
            src = self.gen_expr(stmt.value)
            self._gen_store(stmt.target, src)
        elif isinstance(stmt, ExprStmt):
            self.gen_expr(stmt.value)
        elif isinstance(stmt, PrintStmt):
            self._gen_print(stmt)
        elif isinstance(stmt, IfStmt):
            self._gen_if_into(stmt, self._temp())
        elif isinstance(stmt, WhileStmt):
            self._gen_while_into(stmt, self._temp())
        elif isinstance(stmt, ForStmt):
            self._gen_for_into(stmt, self._temp())
        elif isinstance(stmt, MatchStmt):
            self._gen_match_into(stmt, self._temp())
        elif isinstance(stmt, BreakStmt):
            self._gen_break(stmt)
        elif isinstance(stmt, ContinueStmt):
            self._gen_continue(stmt)
        elif isinstance(stmt, ReturnStmt):
            self._gen_return(stmt)
        elif isinstance(stmt, StructDecl):
            pass  # purely a resolve-time/compile-time declaration; no runtime code
        elif isinstance(stmt, EnumDecl):
            pass  # purely a resolve-time/compile-time declaration; no runtime code
        elif isinstance(stmt, (TraitDecl, ImplDecl)):
            pass  # M12: method closures + defmethod are hoisted in `generate` above
        elif isinstance(stmt, DeferStmt):
            closure_addr = self.gen_expr(stmt.closure_expr)
            self.buf.emit(("deferadd", closure_addr, None, None))
        else:
            raise AssertionError(f"unhandled statement node {stmt!r}")

    def gen_block(self, block: Block):
        """Compile a block's statements, then its tail expression (if any)
        -- for its side effects, whether or not the caller cares about the
        resulting value. Returns the tail's resulting address if there is
        one, else None (existing statement-position callers that don't
        care about a value can keep ignoring the return value unchanged).

        M9: conditionally pushes/drains a defer scope around the above --
        only when this block DIRECTLY contains at least one `DeferStmt` in
        `block.stmts` (not nested inside a sub-block), so a block with no
        direct defer costs nothing (see `CODE_LIMIT`/no-regression
        requirement in docs/V2_DESIGN.md's M9 milestone). When present, a
        `deferpush` runs on entry and the block's own scope is drained via
        `_emit_defer_unwind(1)` on normal fallthrough exit; a `return`/
        `break`/`continue` that jumps out of this block drains it (and any
        other currently-open scopes) via `_gen_return`/`_gen_break`/
        `_gen_continue` instead, before actually jumping."""
        has_defer = any(isinstance(s, DeferStmt) for s in block.stmts)
        if has_defer:
            self.buf.emit(("deferpush", None, None, None))
            self._defer_depth += 1
        for stmt in block.stmts:
            self.gen_stmt(stmt)
        tail_addr = None
        if block.tail is not None:
            tail_addr = self.gen_expr(block.tail)
        if has_defer:
            self._emit_defer_unwind(1)
            self._defer_depth -= 1
        return tail_addr

    def _emit_defer_unwind(self, count: int) -> None:
        """M9: emit code to drain and discard `count` currently-open defer
        scopes, innermost (most recently pushed) first -- used before a
        `return`/`break`/`continue` jumps past that many block boundaries,
        and by `gen_block`'s own normal-fallthrough exit (count=1, its own
        scope)."""
        for _ in range(count):
            self._emit_drain_one_defer_scope()

    def _emit_drain_one_defer_scope(self) -> None:
        """M9: emit a small runtime loop: while the top defer scope has a
        pending closure, pop and call it (LIFO within the scope); once
        empty, discard the scope entirely. Reuses the ordinary
        call/ret/retval opcodes -- a deferred closure is invoked exactly
        like any other zero-arg call, its return value simply discarded."""
        loop_start = self.buf.code_pointer
        has_more = self._temp()
        self.buf.emit(("deferpeek", None, None, has_more))
        jmpf_placeholder = self.buf.emit((None, None, None, None))
        closure_addr = self._temp()
        self.buf.emit(("deferpopclosure", None, None, closure_addr))
        self.buf.emit(("call", closure_addr, (), None))
        discard = self._temp()
        self.buf.emit(("retval", None, None, discard))
        self.buf.emit(("jmp", None, None, loop_start))
        end_target = self.buf.code_pointer
        self.buf.emit(("jmpf", has_more, None, end_target), address=jmpf_placeholder)
        self.buf.emit(("deferscopepop", None, None, None))

    def _gen_block_into(self, block: Block, dest) -> None:
        """Like gen_block, but always writes the block's value (defaulting
        to `none` if there's no tail) into `dest`."""
        tail_addr = self.gen_block(block)
        if tail_addr is not None:
            self.buf.emit(("=", tail_addr, None, dest))
        else:
            self.buf.emit(("ld", NONE_VALUE, None, dest))

    def _gen_print(self, stmt: PrintStmt) -> None:
        """M16: `print(a, b, ..., sep: expr, end: expr)` -- evaluate every
        positional argument, then `sep` and `end` if written (left to right,
        all before any output, so side effects happen even when `sep` has
        nothing to separate), then compile to a `write` (native `io.write`,
        no newline) per piece: `a`, `sep`, `b`, `sep`, ..., last arg, `end`.
        `sep` defaults to `" "`, `end` to `"\n"`; an unwritten default that
        would never be written (`sep` with fewer than two arguments) isn't
        loaded at all."""
        arg_addrs = [self.gen_expr(arg) for arg in stmt.args]
        sep_addr = None
        if stmt.sep is not None or len(arg_addrs) > 1:
            sep_addr = self._gen_print_option(stmt.sep, " ")
        end_addr = self._gen_print_option(stmt.end, "\n")
        for index, addr in enumerate(arg_addrs):
            if index > 0:
                self.buf.emit(("write", sep_addr, None, None))
            self.buf.emit(("write", addr, None, None))
        self.buf.emit(("write", end_addr, None, None))

    def _gen_print_option(self, expr, default_text: str) -> tuple:
        if expr is not None:
            return self.gen_expr(expr)
        tmp = self._temp()
        self.buf.emit(("ld", default_text, None, tmp))
        return tmp

    def _gen_store(self, target, src_addr) -> None:
        if isinstance(target, Ident):
            self.buf.emit(("=", src_addr, None, target.address))
        elif isinstance(target, FieldAccess):
            obj_addr = self.gen_expr(target.obj)
            self.buf.emit(("setfield", obj_addr, target.field, src_addr))
        else:
            raise AssertionError(f"unhandled assignment target {target!r}")

    def _gen_if_into(self, stmt: IfStmt, dest) -> None:
        end_jumps = []
        branches = [(stmt.cond, stmt.then)] + list(stmt.elifs)
        for cond, block in branches:
            cond_addr = self.gen_expr(cond)
            jmpf_placeholder = self.buf.emit((None, None, None, None))
            self._gen_block_into(block, dest)
            end_jumps.append(self.buf.emit((None, None, None, None)))
            self.buf.emit(("jmpf", cond_addr, None, self.buf.code_pointer), address=jmpf_placeholder)
        if stmt.else_ is not None:
            self._gen_block_into(stmt.else_, dest)
        else:
            self.buf.emit(("ld", NONE_VALUE, None, dest))  # no else -> none if nothing matched
        end_target = self.buf.code_pointer
        for addr in end_jumps:
            self.buf.emit(("jmp", None, None, end_target), address=addr)

    def _gen_while_into(self, stmt: WhileStmt, dest) -> None:
        """A loop's value is the value of the `break` that ended it, or
        `none` -- `dest` starts as `none` and only a `break value` (see
        `_gen_break`) overwrites it on its way out."""
        self.buf.emit(("ld", NONE_VALUE, None, dest))
        cond_check_addr = self.buf.code_pointer
        loop_ctx = self._push_loop(cond_check_addr, dest)
        cond_addr = self.gen_expr(stmt.cond)
        jmpf_placeholder = self.buf.emit((None, None, None, None))
        self.gen_block(stmt.body)
        self.buf.emit(("jmp", None, None, cond_check_addr))
        end_target = self.buf.code_pointer
        self.buf.emit(("jmpf", cond_addr, None, end_target), address=jmpf_placeholder)
        self._pop_loop(loop_ctx, end_target)

    def _gen_for_into(self, stmt: ForStmt, dest) -> None:
        """`for let v, let i in xs { body }` -- no desugaring into other AST
        nodes, just the planned protocol calls (docs/TRAITS.md):

            dest = none; it = Iterable.iter(xs); counter = 0
          top:
            item = Iterator.next(it)
            if item isn't `some(_)`: goto end
            v = item.value; i = counter; counter = counter + 1
            body; goto top
          end:

        Both calls are trait-restricted `callmethod`s, so a non-Iterable
        gets "'X' does not implement trait 'Iterable'". `continue` jumps to
        `top`; `break` to `end`."""
        self.buf.emit(("ld", NONE_VALUE, None, dest))
        source = self.gen_expr(stmt.iterable)
        it = self._temp()
        self.buf.emit(("callmethod", source, ("iter", (), "Iterable", stmt.position), None))
        self.buf.emit(("retval", None, None, it))
        counter = one = None
        if stmt.index_address is not None:
            counter = self._temp()
            self.buf.emit(("ld", Decimal(0), None, counter))
            one = self._temp()
            self.buf.emit(("ld", Decimal(1), None, one))
        top = self.buf.code_pointer
        loop_ctx = self._push_loop(top, dest)
        item = self._temp()
        self.buf.emit(("callmethod", it, ("next", (), "Iterator", stmt.position), None))
        self.buf.emit(("retval", None, None, item))
        is_some = self._temp()
        self.buf.emit(("matchtag", item, ("enum", "Option", "some"), is_some))
        jmpf_placeholder = self.buf.emit((None, None, None, None))
        self.buf.emit(("getfield", item, "value", (0, stmt.value_address)))
        if counter is not None:
            self.buf.emit(("=", counter, None, (0, stmt.index_address)))
            self.buf.emit(("+", counter, one, counter))
        self.gen_block(stmt.body)
        self.buf.emit(("jmp", None, None, top))
        end_target = self.buf.code_pointer
        self.buf.emit(("jmpf", is_some, None, end_target), address=jmpf_placeholder)
        self._pop_loop(loop_ctx, end_target)

    def _push_loop(self, continue_target: int, dest) -> dict:
        # M9: snapshot the defer depth as of loop entry (before the body's
        # own possible push) so break/continue know exactly how many
        # scopes opened *inside* this loop iteration need draining --
        # never more than that, and never scopes belonging to an
        # enclosing block/function.
        loop_ctx = {
            "continue_target": continue_target,
            "break_placeholders": [],
            "defer_depth_at_entry": self._defer_depth,
            "dest": dest,
        }
        self._while_stack.append(loop_ctx)
        return loop_ctx

    def _pop_loop(self, loop_ctx: dict, end_target: int) -> None:
        for addr in loop_ctx["break_placeholders"]:
            self.buf.emit(("jmp", None, None, end_target), address=addr)
        self._while_stack.pop()

    def _gen_match_into(self, stmt: MatchStmt, dest) -> None:
        scrutinee_addr = self.gen_expr(stmt.scrutinee)
        end_jumps = []
        for arm in stmt.arms:
            failure_jumps = []  # list[(cond_addr, placeholder_addr)]
            self._gen_pattern_check(arm.pattern, scrutinee_addr, failure_jumps)
            self._gen_block_into(arm.body, dest)
            end_jumps.append(self.buf.emit((None, None, None, None)))
            next_arm_target = self.buf.code_pointer
            for cond_addr, placeholder in failure_jumps:
                self.buf.emit(("jmpf", cond_addr, None, next_arm_target), address=placeholder)
        self.buf.emit(("matchfail", None, None, stmt.position))
        end_target = self.buf.code_pointer
        for addr in end_jumps:
            self.buf.emit(("jmp", None, None, end_target), address=addr)

    def _gen_pattern_check(self, pattern, value_addr, failure_jumps: list) -> None:
        """Emit checks for `pattern` against the value at `value_addr`,
        appending `(cond_addr, placeholder_addr)` to `failure_jumps` for
        every check that can fail. Recurses into struct/enum sub-patterns
        using the SAME `failure_jumps` list threaded through the whole
        walk of one arm's pattern -- see this module's docstring for why
        that single shared list is what gives correct short-circuit AND
        semantics at arbitrary nesting depth."""
        # M14: see `gen_stmt`'s identical wrapper for why this saves/sets/
        # restores `_pos` around the whole dispatch (including the
        # recursive calls for struct/enum sub-patterns below).
        saved_pos = self._pos
        position = getattr(pattern, "position", None)
        if position is not None:
            self._pos = position
        self.buf.current_pos = self._pos
        try:
            self._gen_pattern_check_impl(pattern, value_addr, failure_jumps)
        finally:
            self._pos = saved_pos
            self.buf.current_pos = self._pos

    def _gen_pattern_check_impl(self, pattern, value_addr, failure_jumps: list) -> None:
        if isinstance(pattern, WildcardPat):
            return
        if isinstance(pattern, BindPat):
            self.buf.emit(("=", value_addr, None, (0, pattern.address)))
            return
        if isinstance(pattern, (NumberLit, StringLit, BoolLit)):
            lit_addr = self.gen_expr(pattern)
            cond = self._temp()
            self.buf.emit(("eq", value_addr, lit_addr, cond))
            placeholder = self.buf.emit((None, None, None, None))
            failure_jumps.append((cond, placeholder))
            return
        if isinstance(pattern, RangePat):
            # M17: `matchrange` -- see docs/MAHC_FORMAT.md #4.6/#6.3. Each
            # present bound is loaded into a temp first (like the literal-
            # pattern case above); an absent bound stays `None`, encoded as
            # operand kind `A?`'s "absent" marker.
            lo_addr = self.gen_expr(pattern.lo) if pattern.lo is not None else None
            hi_addr = self.gen_expr(pattern.hi) if pattern.hi is not None else None
            cond = self._temp()
            self.buf.emit(("matchrange", value_addr, (lo_addr, hi_addr, pattern.inclusive), cond))
            placeholder = self.buf.emit((None, None, None, None))
            failure_jumps.append((cond, placeholder))
            return
        if isinstance(pattern, (StructPat, EnumPat)):
            cond = self._temp()
            if isinstance(pattern, StructPat):
                self.buf.emit(("matchtag", value_addr, ("struct", pattern.type_name, None), cond))
            else:
                self.buf.emit(
                    ("matchtag", value_addr, ("enum", pattern.type_name, pattern.variant), cond)
                )
            placeholder = self.buf.emit((None, None, None, None))
            failure_jumps.append((cond, placeholder))
            for field_name, sub_pattern in pattern.fields:
                field_addr = self._temp()
                self.buf.emit(("getfield", value_addr, field_name, field_addr))
                self._gen_pattern_check(sub_pattern, field_addr, failure_jumps)
            return
        raise AssertionError(f"unhandled pattern node {pattern!r}")

    def _gen_break(self, stmt: BreakStmt) -> None:
        if not self._while_stack:
            raise Exception(f"'break' used outside a loop at position {stmt.position}")
        loop_ctx = self._while_stack[-1]
        if stmt.value is not None:
            # Evaluated before any defers run, like `return`'s value.
            src = self.gen_expr(stmt.value)
            self.buf.emit(("=", src, None, loop_ctx["dest"]))
        self._emit_defer_unwind(self._defer_depth - loop_ctx["defer_depth_at_entry"])
        placeholder = self.buf.emit((None, None, None, None))
        self._while_stack[-1]["break_placeholders"].append(placeholder)

    def _gen_continue(self, stmt: ContinueStmt) -> None:
        if not self._while_stack:
            raise Exception(f"'continue' used outside a loop at position {stmt.position}")
        self._emit_defer_unwind(self._defer_depth - self._while_stack[-1]["defer_depth_at_entry"])
        target = self._while_stack[-1]["continue_target"]
        self.buf.emit(("jmp", None, None, target))

    def _gen_return(self, stmt: ReturnStmt) -> None:
        if self._fn_depth == 0:
            raise Exception(f"return keyword used outside function at position {stmt.position}")
        if stmt.value is not None:
            src = self.gen_expr(stmt.value)
        else:
            src = self._temp()
            self.buf.emit(("ld", NONE_VALUE, None, src))
        # M9: unwind ALL currently-open scopes, relative to the current
        # function, after computing the return value but before actually
        # returning -- `self._defer_depth` is reset to 0 at the start of
        # each function's own body by `_gen_fn_expr`, so this never tries
        # to unwind an outer function's or enclosing while-loop's scopes.
        self._emit_defer_unwind(self._defer_depth)
        self.buf.emit(("ret", src, None, None))

    # -- expressions -------------------------------------------------------

    def gen_expr(self, expr) -> tuple:
        # M14: see `gen_stmt`'s identical wrapper for why this saves/sets/
        # restores `_pos` around the whole dispatch.
        saved_pos = self._pos
        position = getattr(expr, "position", None)
        if position is not None:
            self._pos = position
        self.buf.current_pos = self._pos
        try:
            return self._gen_expr(expr)
        finally:
            self._pos = saved_pos
            self.buf.current_pos = self._pos

    def _gen_expr(self, expr) -> tuple:
        if isinstance(expr, ErrorNode):
            # M6: substitute `none`, the same default already used for a
            # function's implicit return and a semicolon-terminated block's
            # value -- mah.py never actually reaches codegen with a
            # non-empty parser.errors list (it refuses to build/run first),
            # so this only matters for a caller (the LSP) that compiles
            # past parse errors on purpose for other analysis.
            tmp = self._temp()
            self.buf.emit(("ld", NONE_VALUE, None, tmp))
            return tmp
        if isinstance(expr, NumberLit):
            tmp = self._temp()
            self.buf.emit(("ld", expr.value, None, tmp))
            return tmp
        if isinstance(expr, StringLit):
            tmp = self._temp()
            self.buf.emit(("ld", expr.value, None, tmp))
            return tmp
        if isinstance(expr, BoolLit):
            tmp = self._temp()
            self.buf.emit(("ld", expr.value, None, tmp))
            return tmp
        if isinstance(expr, Ident):
            return expr.address
        if isinstance(expr, Unary):
            src = self.gen_expr(expr.operand)
            tmp = self._temp()
            op = "neg" if expr.op == "-" else "not"
            self.buf.emit((op, src, None, tmp))
            return tmp
        if isinstance(expr, Binary):
            left = self.gen_expr(expr.lhs)
            right = self.gen_expr(expr.rhs)
            tmp = self._temp()
            self.buf.emit((expr.op, left, right, tmp))
            return tmp
        if isinstance(expr, Call):
            return self._gen_call(expr)
        if isinstance(expr, SinExpr):
            src = self.gen_expr(expr.arg)
            tmp = self._temp()
            self.buf.emit(("sin", src, None, tmp))
            return tmp
        if isinstance(expr, CosExpr):
            src = self.gen_expr(expr.arg)
            tmp = self._temp()
            self.buf.emit(("cos", src, None, tmp))
            return tmp
        if isinstance(expr, InputExpr):
            tmp = self._temp()
            self.buf.emit(("input", None, None, tmp))
            return tmp
        if isinstance(expr, DetachExpr):
            if isinstance(expr.call, SleepAsyncExpr):
                # `detach sleep_async(ms)`: unlike a bare `sleep_async(ms)`
                # (see the SleepAsyncExpr case below, which auto-awaits
                # itself), detaching it explicitly opts back OUT of that --
                # sleep_async isn't a real Closure call in the first place
                # (it's a dedicated builtin opcode), so there's no Task to
                # spin up here; "detaching" it is purely a codegen-time
                # choice of which instructions to emit: just the raw
                # `sleepasync` opcode, handing back a still-pending Promise
                # for the caller to `.await` whenever it's ready to.
                src = self.gen_expr(expr.call.arg)
                dest = self._temp()
                self.buf.emit(("sleepasync", src, None, dest))
                return dest
            if isinstance(expr.call, MethodCall):
                # M13: `detach obj.method(args)` -- see this module's
                # docstring and `_gen_detach_method_call`.
                return self._gen_detach_method_call(expr.call)
            call = expr.call
            callee_addr = self.gen_expr(call.callee)
            arg_addrs = tuple(self.gen_expr(a) for a in call.args)
            dest = self._temp()
            if not call.kwargs:
                self.buf.emit(("detach", callee_addr, arg_addrs, dest))
            else:
                kw_names, kw_addrs = self._gen_kwargs(call.kwargs)
                self.buf.emit(("detachkw", callee_addr, (arg_addrs + kw_addrs, kw_names), dest))
            return dest
        if isinstance(expr, SleepAsyncExpr):
            # Bare (non-detached) `sleep_async(ms)` auto-awaits its own
            # Promise immediately -- `.await` is only needed once you've
            # explicitly `detach`ed something (see the DetachExpr case
            # above), matching how an ordinary function call already
            # blocks synchronously for its result unless detached. This is
            # a plain two-instruction sequence, not a new mechanism: the
            # exact same `sleepasync`+`await` opcodes a hand-written
            # `sleep_async(ms).await` would already compile to.
            src = self.gen_expr(expr.arg)
            promise_dest = self._temp()
            self.buf.emit(("sleepasync", src, None, promise_dest))
            dest = self._temp()
            self.buf.emit(("await", promise_dest, None, dest))
            return dest
        if isinstance(expr, FnExpr):
            return self._gen_fn_expr(expr)
        if isinstance(expr, StructLit):
            pairs = tuple((name, self.gen_expr(value_expr)) for name, value_expr in expr.fields)
            dest = self._temp()
            self.buf.emit(("struct", expr.type_name, pairs, dest))
            return dest
        if isinstance(expr, FieldAccess):
            if expr.enum_unit_type is not None:
                dest = self._temp()
                self.buf.emit(("enum", expr.enum_unit_type, (expr.field, ()), dest))
                return dest
            obj_addr = self.gen_expr(expr.obj)
            dest = self._temp()
            if expr.field == "await":
                self.buf.emit(("await", obj_addr, None, dest))
            else:
                self.buf.emit(("getfield", obj_addr, expr.field, dest))
            return dest
        if isinstance(expr, EnumLit):
            if expr.type_name == "Option" and expr.variant == "none":
                # Keep `none` a true, single, reused singleton (see
                # runtime_values.NONE_VALUE and docs/V2_DESIGN.md's
                # "Built-in `some`/`none`") rather than allocating a fresh
                # but value-equal EnumInstance("Option", "none", {}) every
                # time -- identity-based checks elsewhere (`val is
                # NONE_VALUE`, e.g. the implicit-return path just below)
                # depend on there being exactly one such object.
                dest = self._temp()
                self.buf.emit(("ld", NONE_VALUE, None, dest))
                return dest
            pairs = tuple((name, self.gen_expr(value_expr)) for name, value_expr in expr.fields)
            dest = self._temp()
            self.buf.emit(("enum", expr.type_name, (expr.variant, pairs), dest))
            return dest
        if isinstance(expr, MethodCall):
            return self._gen_method_call(expr)
        if isinstance(expr, IfStmt):
            dest = self._temp()
            self._gen_if_into(expr, dest)
            return dest
        if isinstance(expr, MatchStmt):
            dest = self._temp()
            self._gen_match_into(expr, dest)
            return dest
        if isinstance(expr, WhileStmt):
            dest = self._temp()
            self._gen_while_into(expr, dest)
            return dest
        if isinstance(expr, ForStmt):
            dest = self._temp()
            self._gen_for_into(expr, dest)
            return dest
        if isinstance(expr, Block):
            dest = self._temp()
            self._gen_block_into(expr, dest)
            return dest
        raise AssertionError(f"unhandled expression node {expr!r}")

    def _gen_fn_expr(self, fn: FnExpr) -> tuple:
        skip_placeholder = self.buf.emit((None, None, None, None))
        code_address = self.buf.code_pointer
        self.frame_stack.append(fn.frame_level)
        self._fn_depth += 1
        # M9: save/reset/restore the defer-depth counter around compiling
        # this function's own body, exactly parallel to frame_stack/
        # _fn_depth above -- essential so a nested function's own return
        # never tries to unwind an OUTER function's or enclosing
        # while-loop's defer scopes.
        saved_defer_depth = self._defer_depth
        self._defer_depth = 0
        # Pre-existing gap, found and fixed while landing M9: `break`/
        # `continue` compiled `self._while_stack[-1]` without ever
        # resetting that stack across a function boundary, so a `break`/
        # `continue` inside a nested `fn`'s body (already legal syntax
        # before M9 -- e.g. `let f = fn() { break; }; f();` inside a
        # `while`) silently targeted the ENCLOSING loop's jump target,
        # corrupting execution at runtime (the jump lands in the outer
        # loop's code with the inner closure's frame still current,
        # never popped via `ret`) instead of raising a clean error. A
        # loop can never actually span a function boundary in Mah, so
        # resetting to `[]` here (parallel to `_defer_depth` above) is
        # correct, not just defensive: it turns that silent corruption
        # into `_gen_break`/`_gen_continue`'s existing clean
        # "used outside a loop" error.
        saved_while_stack = self._while_stack
        self._while_stack = []
        # M16: prologue -- for each parameter with a default, right after
        # entering the function and before the body: emit a placeholder
        # `jmpset`, compute the default expression and move it into the
        # parameter's own slot, then backpatch the placeholder to skip
        # straight past that move when the parameter was already bound
        # (its slot holds anything but the ABSENT marker) -- see
        # docs/MAHC_FORMAT.md #6.1. Evaluation order is parameter order,
        # each default resolved (by resolve.py) to see only earlier
        # parameters and outer names, matching this left-to-right emission.
        for index, default_expr in enumerate(fn.defaults):
            if default_expr is None:
                continue
            param_slot = fn.param_slots[index]
            jmpset_placeholder = self.buf.emit((None, None, None, None))
            src = self.gen_expr(default_expr)
            self.buf.emit(("=", src, None, (0, param_slot)))
            self.buf.emit(
                ("jmpset", (0, param_slot), None, self.buf.code_pointer), address=jmpset_placeholder
            )
        # M5: implicit return of the body block's tail value if it falls
        # off the end -- `none` when there's no tail, a strict superset of
        # M1's "always none" trailer (unconditionally appended, dead code
        # after an explicit early return included, exactly as before).
        tail_addr = self.gen_block(fn.body)
        if tail_addr is None:
            tail_addr = self._temp()
            self.buf.emit(("ld", NONE_VALUE, None, tail_addr))
        self.buf.emit(("ret", tail_addr, None, None))
        self._while_stack = saved_while_stack
        self._defer_depth = saved_defer_depth
        self._fn_depth -= 1
        slot_count = self.frame_stack.pop().next_slot
        self.buf.emit(("jmp", None, None, self.buf.code_pointer), address=skip_placeholder)
        dest = self._temp()
        # M16: closure metadata gains the parameter names and per-parameter
        # has-default flags (parallel tuples) -- lower.py's PARAMS section
        # (docs/MAHC_FORMAT.md #4.5a) needs both to bind keyword arguments
        # and know which parameters may be left unbound.
        self.buf.emit(
            (
                "closure",
                code_address,
                (
                    slot_count,
                    len(fn.param_slots),
                    fn.name,
                    tuple(fn.params),
                    tuple(d is not None for d in fn.defaults),
                ),
                dest,
            )
        )
        return dest

    def _gen_kwargs(self, kwargs: list) -> tuple:
        """M16: `kwargs` (list[(name, value_expr, name_position)]) -> a
        `(names_tuple, value_addrs_tuple)` pair -- names in source order,
        values evaluated left to right (after every positional argument),
        matching `docs/MAHC_FORMAT.md #4.6`'s `*kw` convention (`kwnames`
        names the LAST `len(kwnames)` entries of the full `args` list)."""
        names = tuple(name for name, _value, _pos in kwargs)
        addrs = tuple(self.gen_expr(value) for _name, value, _pos in kwargs)
        return names, addrs

    def _gen_call(self, expr: Call) -> tuple:
        callee_addr = self.gen_expr(expr.callee)
        arg_addrs = tuple(self.gen_expr(a) for a in expr.args)
        dest = self._temp()
        if not expr.kwargs:
            self.buf.emit(("call", callee_addr, arg_addrs, None))
        else:
            kw_names, kw_addrs = self._gen_kwargs(expr.kwargs)
            self.buf.emit(("callkw", callee_addr, (arg_addrs + kw_addrs, kw_names), None))
        self.buf.emit(("retval", None, None, dest))
        return dest

    def _gen_method_call(self, expr: MethodCall) -> tuple:
        """M12: three shapes, chosen by what the resolver set on `expr` --
        see `ast_nodes.py`'s `MethodCall` docstring and this module's own
        docstring for the `defmethod`/`callmethod` opcode shapes."""
        if expr.static_address is not None:
            # `Type.fn(args)` resolved at compile time to a specific hidden
            # global slot -- an ordinary `call`/`callkw`, no runtime
            # dispatch at all.
            arg_addrs = tuple(self.gen_expr(a) for a in expr.args)
            dest = self._temp()
            if not expr.kwargs:
                self.buf.emit(("call", expr.static_address, arg_addrs, None))
            else:
                kw_names, kw_addrs = self._gen_kwargs(expr.kwargs)
                self.buf.emit(("callkw", expr.static_address, (arg_addrs + kw_addrs, kw_names), None))
            self.buf.emit(("retval", None, None, dest))
            return dest
        if expr.trait_name is not None:
            # `Trait.m(recv, ...)` / `BuiltinType.m(recv)` (native impl) --
            # the receiver is `args[0]` (always positional), dispatch
            # restricted to this trait.
            recv = self.gen_expr(expr.args[0])
            rest = tuple(self.gen_expr(a) for a in expr.args[1:])
            dest = self._temp()
            if not expr.kwargs:
                self.buf.emit(("callmethod", recv, (expr.method, rest, expr.trait_name, expr.position), None))
            else:
                kw_names, kw_addrs = self._gen_kwargs(expr.kwargs)
                self.buf.emit(
                    (
                        "callmethodkw",
                        recv,
                        (expr.method, rest + kw_addrs, kw_names, expr.trait_name, expr.position),
                        None,
                    )
                )
            self.buf.emit(("retval", None, None, dest))
            return dest
        if expr.native_inherent:
            # M17: `Type.method(args)` resolved to a native INHERENT method
            # (e.g. `String.len(s)`) -- there's no trait to dispatch
            # through, so this is a plain dynamic `callmethod` on
            # `args[0]`, trait `None` (see `ast_nodes.py`'s
            # `MethodCall.native_inherent` docstring).
            recv = self.gen_expr(expr.args[0])
            rest = tuple(self.gen_expr(a) for a in expr.args[1:])
            dest = self._temp()
            if not expr.kwargs:
                self.buf.emit(("callmethod", recv, (expr.method, rest, None, expr.position), None))
            else:
                kw_names, kw_addrs = self._gen_kwargs(expr.kwargs)
                self.buf.emit(
                    ("callmethodkw", recv, (expr.method, rest + kw_addrs, kw_names, None, expr.position), None)
                )
            self.buf.emit(("retval", None, None, dest))
            return dest
        # Ordinary dynamic method call: `expr.obj.method(args)`, dispatch on
        # the runtime type of `expr.obj`'s value.
        recv = self.gen_expr(expr.obj)
        arg_addrs = tuple(self.gen_expr(a) for a in expr.args)
        dest = self._temp()
        if not expr.kwargs:
            self.buf.emit(("callmethod", recv, (expr.method, arg_addrs, None, expr.position), None))
        else:
            kw_names, kw_addrs = self._gen_kwargs(expr.kwargs)
            self.buf.emit(
                ("callmethodkw", recv, (expr.method, arg_addrs + kw_addrs, kw_names, None, expr.position), None)
            )
        self.buf.emit(("retval", None, None, dest))
        return dest

    def _gen_detach_method_call(self, call: MethodCall) -> tuple:
        """M13: `detach obj.method(args)` / `detach Type.method(args)` /
        `detach Trait.method(recv, ...)` -- mirrors `_gen_method_call`'s
        three shapes, but emits `detach`/`detachmethod` (dest receives a
        Promise, no following `retval`) instead of `call`/`retval` or
        `callmethod`/`retval` -- see this module's docstring."""
        if call.static_address is not None:
            # `detach Type.fn(args)` resolved at compile time to a specific
            # hidden global slot -- an ordinary `detach`/`detachkw`, no
            # runtime dispatch at all (the callee address is just that
            # slot).
            arg_addrs = tuple(self.gen_expr(a) for a in call.args)
            dest = self._temp()
            if not call.kwargs:
                self.buf.emit(("detach", call.static_address, arg_addrs, dest))
            else:
                kw_names, kw_addrs = self._gen_kwargs(call.kwargs)
                self.buf.emit(("detachkw", call.static_address, (arg_addrs + kw_addrs, kw_names), dest))
            return dest
        if call.trait_name is not None:
            # `detach Trait.m(recv, ...)` / `detach BuiltinType.m(recv)`.
            recv = self.gen_expr(call.args[0])
            rest = tuple(self.gen_expr(a) for a in call.args[1:])
            dest = self._temp()
            if not call.kwargs:
                self.buf.emit(("detachmethod", recv, (call.method, rest, call.trait_name, call.position), dest))
            else:
                kw_names, kw_addrs = self._gen_kwargs(call.kwargs)
                self.buf.emit(
                    (
                        "detachmethodkw",
                        recv,
                        (call.method, rest + kw_addrs, kw_names, call.trait_name, call.position),
                        dest,
                    )
                )
            return dest
        if call.native_inherent:
            # M17: `detach Type.method(args)` where the target is a native
            # inherent method -- mirrors the `_gen_method_call` branch
            # above, but `detach`/`detachkw` (no following `retval`).
            recv = self.gen_expr(call.args[0])
            rest = tuple(self.gen_expr(a) for a in call.args[1:])
            dest = self._temp()
            if not call.kwargs:
                self.buf.emit(("detachmethod", recv, (call.method, rest, None, call.position), dest))
            else:
                kw_names, kw_addrs = self._gen_kwargs(call.kwargs)
                self.buf.emit(
                    ("detachmethodkw", recv, (call.method, rest + kw_addrs, kw_names, None, call.position), dest)
                )
            return dest
        # Ordinary dynamic method call: `detach obj.method(args)`.
        recv = self.gen_expr(call.obj)
        arg_addrs = tuple(self.gen_expr(a) for a in call.args)
        dest = self._temp()
        if not call.kwargs:
            self.buf.emit(("detachmethod", recv, (call.method, arg_addrs, None, call.position), dest))
        else:
            kw_names, kw_addrs = self._gen_kwargs(call.kwargs)
            self.buf.emit(
                ("detachmethodkw", recv, (call.method, arg_addrs + kw_addrs, kw_names, None, call.position), dest)
            )
        return dest
