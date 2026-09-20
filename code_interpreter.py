"""Executes the flat IR compiler/codegen.py emits (see docs/RUNTIME.md and
docs/V2_DESIGN.md's M1 milestone).

M1 replaces the M0 flat shared-array/`sp`-stack model with heap-allocated
`Frame`s linked by a static-chain pointer (`Frame.static_parent`), plus a
`Closure` value for first-class/nested functions -- see runtime_values.py.
Every IR operand is now a `(depth, slot)` tuple: `_read`/`_write` walk
`depth` `static_parent` hops from the *currently executing* frame, then
index `.slots[slot]`. Calls push `(return_pc, caller_frame)` onto an
explicit Python-list return stack and switch `current_frame` to a brand
new `Frame` (so recursive calls never alias each other's locals, and a
closure's captured frame lives exactly as long as something still
references it -- ordinary Python object lifetime, no reuse/popping of a
shared stack). `ret` copies its value into an interpreter-local "return
register" (not a `Frame` slot) and pops the return stack; the `retval`
opcode immediately following a `call` in the caller's code copies that
register into the caller's destination slot, since the destination can't
be known until after the call returns.

M2 adds `struct`/`getfield`/`setfield` opcodes for `StructInstance` values
(see docs/V2_DESIGN.md's M2 milestone and compiler/codegen.py's module
docstring for the exact opcode shapes). Field-name validation for
`getfield`/`setfield` happens here, at runtime -- deliberately, since
there's no static type system yet to check a field access against ahead of
time (struct *literal* field validation, by contrast, is fully static and
lives in compiler/resolve.py, since a literal always names its struct type
explicitly).
"""

from decimal import Decimal
import math
import sys
from typing import Any

from runtime_values import Closure, Frame, NONE_VALUE, StructInstance


def _to_str(val: Any) -> str:
    if val is NONE_VALUE:
        return "none"
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, (int, float, Decimal)):
        if val % 1 == 0:
            return str(int(val))
        return str(val)
    if isinstance(val, StructInstance):
        inner = ", ".join(f"{k}: {_to_str(v)}" for k, v in val.fields.items())
        return f"{val.type_name} {{ {inner} }}"
    return str(val)


def _read(frame, addr):
    depth, slot = addr
    for _ in range(depth):
        frame = frame.static_parent
    return frame.slots[slot]


def _write(frame, addr, value):
    depth, slot = addr
    for _ in range(depth):
        frame = frame.static_parent
    frame.slots[slot] = value


def run_code(code_block: list, global_slot_count: int):
    current_frame = Frame(slots=[None] * global_slot_count, static_parent=None)
    return_stack = []  # list[tuple[int, Frame]]  (return_pc, caller_frame)
    return_register = NONE_VALUE
    pc = 0
    while True:
        operation = code_block[pc]
        pc += 1

        match operation:
            case (None, None, None, None):
                # print("program ended")
                break
            case ("print", arg, None, None):
                val = _read(current_frame, arg)
                print(_to_str(val))
            case ("input", None, None, dest):
                raw_num = ""
                has_num_started = False
                while True:
                    char = sys.stdin.read(1)
                    if char.isdigit():
                        has_num_started = True
                        raw_num += char
                    else:
                        if not has_num_started:
                            continue
                        else:
                            break
                num = int(raw_num)
                _write(current_frame, dest, num)
            case ("jmpf", cond_addr, None, loc):
                if not _read(current_frame, cond_addr):
                    pc = loc
            case ("jmp", None, None, loc):
                pc = loc
            # Binary-op tuples are `(op, left_addr, right_addr, dest)` in
            # plain left-to-right order (see codegen.py's module docstring
            # for why this differs from v1's reversed convention).
            case ("+", left, right, dest):
                a = _read(current_frame, left)
                b = _read(current_frame, right)
                if isinstance(a, str) or isinstance(b, str):
                    _write(current_frame, dest, _to_str(a) + _to_str(b))
                else:
                    _write(current_frame, dest, a + b)
            case ("sin", arg, None, dest):
                _write(current_frame, dest, math.sin(_read(current_frame, arg)))
            case ("cos", arg, None, dest):
                _write(current_frame, dest, math.cos(_read(current_frame, arg)))
            case ("neg", arg, None, dest):
                _write(current_frame, dest, -_read(current_frame, arg))
            case ("**", left, right, dest):
                a = _read(current_frame, left)
                b = _read(current_frame, right)
                _write(current_frame, dest, a**b)
            case ("*", left, right, dest):
                a = _read(current_frame, left)
                b = _read(current_frame, right)
                if isinstance(a, str) and isinstance(b, (int, Decimal)) and not isinstance(b, bool):
                    _write(current_frame, dest, a * int(b))
                elif isinstance(b, str) and isinstance(a, (int, Decimal)) and not isinstance(a, bool):
                    _write(current_frame, dest, b * int(a))
                else:
                    _write(current_frame, dest, a * b)
            case ("-", left, right, dest):
                a = _read(current_frame, left)
                b = _read(current_frame, right)
                _write(current_frame, dest, a - b)
            case ("/", left, right, dest):
                a = _read(current_frame, left)
                b = _read(current_frame, right)
                _write(current_frame, dest, a / b)
            case ("//", left, right, dest):
                a = _read(current_frame, left)
                b = _read(current_frame, right)
                _write(current_frame, dest, a // b)
            case ("%", left, right, dest):
                a = _read(current_frame, left)
                b = _read(current_frame, right)
                _write(current_frame, dest, a % b)
            case ("lt", left, right, dest):
                _write(current_frame, dest, _read(current_frame, left) < _read(current_frame, right))
            case ("gt", left, right, dest):
                _write(current_frame, dest, _read(current_frame, left) > _read(current_frame, right))
            case ("and", left, right, dest):
                _write(
                    current_frame,
                    dest,
                    bool(_read(current_frame, left) and _read(current_frame, right)),
                )
            case ("or", left, right, dest):
                _write(
                    current_frame,
                    dest,
                    bool(_read(current_frame, left) or _read(current_frame, right)),
                )
            case ("neq", left, right, dest):
                _write(current_frame, dest, _read(current_frame, left) != _read(current_frame, right))
            case ("eq", left, right, dest):
                _write(current_frame, dest, _read(current_frame, left) == _read(current_frame, right))
            case ("=", src, None, dest):
                _write(current_frame, dest, _read(current_frame, src))
            case ("ld", value, None, dest):
                _write(current_frame, dest, value)
            case ("closure", code_addr, meta, dest):
                slot_count, param_count, name = meta
                _write(current_frame, dest, Closure(code_addr, current_frame, slot_count, param_count, name))
            case ("call", callee_addr, arg_addrs, None):
                closure = _read(current_frame, callee_addr)
                if not isinstance(closure, Closure):
                    raise Exception(f"Tried to call a non function at position {pc}")
                if len(arg_addrs) != closure.param_count:
                    label = f"'{closure.name}'" if closure.name else "function"
                    raise Exception(
                        f"Argument Count is invalid. {label} accepts {closure.param_count} "
                        f"arguments but {len(arg_addrs)} was given at position {pc}"
                    )
                arg_values = [_read(current_frame, a) for a in arg_addrs]
                new_frame = Frame(slots=[None] * closure.slot_count, static_parent=closure.defining_frame)
                for i, v in enumerate(arg_values):
                    new_frame.slots[i] = v
                return_stack.append((pc, current_frame))
                current_frame = new_frame
                pc = closure.code_address
            case ("ret", value_addr, None, None):
                return_register = _read(current_frame, value_addr)
                pc, current_frame = return_stack.pop()
            case ("retval", None, None, dest):
                _write(current_frame, dest, return_register)
            case ("struct", type_name, field_pairs, dest):
                fields = {name: _read(current_frame, addr) for name, addr in field_pairs}
                _write(current_frame, dest, StructInstance(type_name, fields))
            case ("getfield", obj_addr, field_name, dest):
                obj = _read(current_frame, obj_addr)
                if not isinstance(obj, StructInstance):
                    raise Exception(
                        f"Tried to access field '{field_name}' on a non-struct value at position {pc}"
                    )
                if field_name not in obj.fields:
                    raise Exception(f"'{obj.type_name}' has no field '{field_name}' at position {pc}")
                _write(current_frame, dest, obj.fields[field_name])
            case ("setfield", obj_addr, field_name, src_addr):
                obj = _read(current_frame, obj_addr)
                if not isinstance(obj, StructInstance):
                    raise Exception(
                        f"Tried to access field '{field_name}' on a non-struct value at position {pc}"
                    )
                if field_name not in obj.fields:
                    raise Exception(f"'{obj.type_name}' has no field '{field_name}' at position {pc}")
                obj.fields[field_name] = _read(current_frame, src_addr)
            case catchall:
                raise RuntimeError(f"invalid operation {catchall}")
