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
from runtime_values import NONE_VALUE

CODE_LIMIT = 400


class CodeBuffer:
    """Holds the emitted instruction array -- the direct replacement for
    v1's `IRGenerator.sstack`/`write_code`/`get_temp_address`."""

    def __init__(self):
        self.code: list = [None] * 1000
        self.code_pointer = 0
        self.global_slot_count = 0

    def emit(self, code, address: int | None = None) -> int:
        if address is None:
            if self.code_pointer >= CODE_LIMIT:
                raise RuntimeError("CodeBlock is full")
            addr = self.code_pointer
            self.code[addr] = code
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

    def _temp(self) -> tuple:
        return (0, self.frame_stack[-1].alloc())

    def generate(self, stmts: list) -> CodeBuffer:
        for stmt in stmts:
            self.gen_stmt(stmt)
        self.buf.emit((None, None, None, None))
        self.buf.global_slot_count = self.frame_stack[-1].next_slot
        return self.buf

    # -- statements ------------------------------------------------------

    def gen_stmt(self, stmt) -> None:
        if isinstance(stmt, LetStmt):
            src = self.gen_expr(stmt.value)
            self.buf.emit(("=", src, None, (0, stmt.address)))
        elif isinstance(stmt, AssignStmt):
            src = self.gen_expr(stmt.value)
            self._gen_store(stmt.target, src)
        elif isinstance(stmt, ExprStmt):
            self.gen_expr(stmt.value)
        elif isinstance(stmt, PrintStmt):
            for arg in stmt.args:
                addr = self.gen_expr(arg)
                self.buf.emit(("print", addr, None, None))
        elif isinstance(stmt, IfStmt):
            self._gen_if(stmt)
        elif isinstance(stmt, WhileStmt):
            self._gen_while(stmt)
        elif isinstance(stmt, BreakStmt):
            self._gen_break(stmt)
        elif isinstance(stmt, ContinueStmt):
            self._gen_continue(stmt)
        elif isinstance(stmt, ReturnStmt):
            self._gen_return(stmt)
        elif isinstance(stmt, BlockStmt):
            self.gen_block(stmt.block)
        elif isinstance(stmt, StructDecl):
            pass  # purely a resolve-time/compile-time declaration; no runtime code
        else:
            raise AssertionError(f"unhandled statement node {stmt!r}")

    def gen_block(self, block: Block) -> None:
        for stmt in block.stmts:
            self.gen_stmt(stmt)

    def _gen_store(self, target, src_addr) -> None:
        if isinstance(target, Ident):
            self.buf.emit(("=", src_addr, None, target.address))
        elif isinstance(target, FieldAccess):
            obj_addr = self.gen_expr(target.obj)
            self.buf.emit(("setfield", obj_addr, target.field, src_addr))
        else:
            raise AssertionError(f"unhandled assignment target {target!r}")

    def _gen_if(self, stmt: IfStmt) -> None:
        end_jumps = []
        branches = [(stmt.cond, stmt.then)] + list(stmt.elifs)
        for cond, block in branches:
            cond_addr = self.gen_expr(cond)
            jmpf_placeholder = self.buf.emit((None, None, None, None))
            self.gen_block(block)
            end_jumps.append(self.buf.emit((None, None, None, None)))
            self.buf.emit(("jmpf", cond_addr, None, self.buf.code_pointer), address=jmpf_placeholder)
        if stmt.else_ is not None:
            self.gen_block(stmt.else_)
        end_target = self.buf.code_pointer
        for addr in end_jumps:
            self.buf.emit(("jmp", None, None, end_target), address=addr)

    def _gen_while(self, stmt: WhileStmt) -> None:
        cond_check_addr = self.buf.code_pointer
        loop_ctx = {"continue_target": cond_check_addr, "break_placeholders": []}
        self._while_stack.append(loop_ctx)
        cond_addr = self.gen_expr(stmt.cond)
        jmpf_placeholder = self.buf.emit((None, None, None, None))
        self.gen_block(stmt.body)
        self.buf.emit(("jmp", None, None, cond_check_addr))
        end_target = self.buf.code_pointer
        self.buf.emit(("jmpf", cond_addr, None, end_target), address=jmpf_placeholder)
        for addr in loop_ctx["break_placeholders"]:
            self.buf.emit(("jmp", None, None, end_target), address=addr)
        self._while_stack.pop()

    def _gen_break(self, stmt: BreakStmt) -> None:
        if not self._while_stack:
            raise Exception(f"'break' used outside a loop at position {stmt.position}")
        placeholder = self.buf.emit((None, None, None, None))
        self._while_stack[-1]["break_placeholders"].append(placeholder)

    def _gen_continue(self, stmt: ContinueStmt) -> None:
        if not self._while_stack:
            raise Exception(f"'continue' used outside a loop at position {stmt.position}")
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
        self.buf.emit(("ret", src, None, None))

    # -- expressions -------------------------------------------------------

    def gen_expr(self, expr) -> tuple:
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
            self.buf.emit(("neg", src, None, tmp))
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
        if isinstance(expr, FnExpr):
            return self._gen_fn_expr(expr)
        if isinstance(expr, StructLit):
            pairs = tuple((name, self.gen_expr(value_expr)) for name, value_expr in expr.fields)
            dest = self._temp()
            self.buf.emit(("struct", expr.type_name, pairs, dest))
            return dest
        if isinstance(expr, FieldAccess):
            obj_addr = self.gen_expr(expr.obj)
            dest = self._temp()
            self.buf.emit(("getfield", obj_addr, expr.field, dest))
            return dest
        raise AssertionError(f"unhandled expression node {expr!r}")

    def _gen_fn_expr(self, fn: FnExpr) -> tuple:
        skip_placeholder = self.buf.emit((None, None, None, None))
        code_address = self.buf.code_pointer
        self.frame_stack.append(fn.frame_level)
        self._fn_depth += 1
        self.gen_block(fn.body)
        # Implicit `return none` if the body falls off the end,
        # unconditionally appended -- dead code after an explicit early
        # return included (matches M0's equivalent trailer).
        none_addr = self._temp()
        self.buf.emit(("ld", NONE_VALUE, None, none_addr))
        self.buf.emit(("ret", none_addr, None, None))
        self._fn_depth -= 1
        slot_count = self.frame_stack.pop().next_slot
        self.buf.emit(("jmp", None, None, self.buf.code_pointer), address=skip_placeholder)
        dest = self._temp()
        self.buf.emit(("closure", code_address, (slot_count, len(fn.param_slots), fn.name), dest))
        return dest

    def _gen_call(self, expr: Call) -> tuple:
        callee_addr = self.gen_expr(expr.callee)
        arg_addrs = tuple(self.gen_expr(a) for a in expr.args)
        dest = self._temp()
        self.buf.emit(("call", callee_addr, arg_addrs, None))
        self.buf.emit(("retval", None, None, dest))
        return dest
