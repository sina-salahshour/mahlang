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

M3 adds the `enum` opcode for `EnumInstance` values (see
docs/V2_DESIGN.md's M3 milestone), including the built-in `Option` type
(`none`/`some(x)`) which now shares this same representation --
`NONE_VALUE` (runtime_values.py) is a genuine `EnumInstance`, not a bespoke
class. `getfield`/`setfield` are generalized to accept either a
`StructInstance` or an `EnumInstance` -- both expose the identical
`.fields` dict shape, so `some(5).value` and `some(5).value = 6` work
through the exact same mechanism M2 built for structs, with no new opcode
needed for enum field access/mutation.

M4 adds `matchtag`/`matchfail` for `match` statements (see
docs/V2_DESIGN.md's M4 milestone): `matchtag` tests whether a value is a
`StructInstance`/`EnumInstance` of the expected type (and, for an enum,
variant), writing a boolean result that compiler/codegen.py's
backpatched jump chain then branches on; `matchfail` is reached only when
no arm's pattern matched (M4 does no exhaustiveness checking) and always
raises a clean runtime error naming the source position.

M9 adds `defer_stack: list[list[Closure]]` -- a stack of "scopes," each
scope a list of pending zero-arg `Closure`s for one currently-active
block that directly contains a `defer` (see docs/V2_DESIGN.md's M9
milestone and compiler/codegen.py's module docstring). Four small
opcodes drive it: `deferpush` opens a new empty scope; `deferadd` (whose
`case` lives in the ordinary opcode dispatch below, right next to these)
pushes one closure onto the top scope; `deferpeek` writes whether the top
scope is non-empty; `deferpopclosure` pops and returns its
most-recently-pushed closure; `deferscopepop` discards the (by then
empty) top scope. Deferred closures are invoked through the ordinary
`call`/`ret`/`retval` opcodes already implemented above (M1's calling
convention, unchanged) -- `compiler/codegen.py`'s
`_emit_drain_one_defer_scope` emits a small loop of `deferpeek` /
`deferpopclosure` / `call` / `retval` (return value discarded) /
`deferscopepop`, so no new call mechanism exists here at all.

M10 adds multi-task scheduling for async (`detach`/`.await`/
`sleep_async` -- see docs/V2_DESIGN.md's M10 milestone and
docs/NEXT_PHASES.md's "Async" section for the full design rationale).
Previously there was exactly one `(pc, current_frame, return_stack,
defer_stack)` -- one flat instruction stream, one call chain. Async needs
several of these live at once, one per `Task` (runtime_values.py): the
main program is task 0, and `detach` spins up one more per detached call.
`defer_stack` moves from a single interpreter-local list onto each
`Task`, because a suspended task's own pending defers must never leak
into whichever task runs next once tasks can genuinely interleave;
`return_register` (the single-value handoff between `ret` and the
immediately-following `retval`) stays a single interpreter-local
variable, since nothing can ever switch tasks between those two adjacent
instructions.

Three new opcodes: `detach` builds a fresh `Task` and `PromiseInstance`
and drives the new task synchronously (via `step_task`, reentrant --
nested calls happen only as deep as concurrent `detach`-in-progress
nesting, not per ordinary Mah call) until it finishes or suspends,
writing the (possibly still-pending) Promise to `dest` either way, and
never suspending its own caller. `await` (`.await`, compiled specially by
codegen.py rather than as `getfield`) checks a Promise's state: if
already resolved, keeps stepping with no scheduling; if still pending,
this is a genuine suspension -- the running task's `step_task` call
returns `("suspended", None)` up the (possibly nested) Python call stack,
having first registered a resume callback on the Promise that restores
this task's `pc`/`dest` and re-drives it once the Promise resolves.
`sleepasync` returns a pending Promise immediately and schedules a timer
in a `heapq`-based queue; resolving a Promise (a timer firing, or a
`detach`ed task finishing) runs its callbacks synchronously, which is
what actually resumes suspended tasks with no separate microtask-queue
data structure needed. The top-level scheduling loop keeps draining
timers -- Node-like process lifetime -- until the main task has both
finished and nothing is left scheduled, rather than exiting the instant
the main program's own top-level code finishes and abandoning any
still-pending detached work nobody ever awaited.
"""

from decimal import Decimal
import heapq
import itertools
import math
import sys
import time
from typing import Any

from .runtime_values import Closure, EnumInstance, Frame, NONE_VALUE, PromiseInstance, StructInstance, Task


def _to_str(val: Any) -> str:
    if val is NONE_VALUE:
        return "none"
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, EnumInstance):
        # PromiseInstance is a subclass of EnumInstance (see
        # runtime_values.py) and needs no special-casing here at all --
        # it prints as "Promise.Pending" / "Promise.Settled { value: ... }"
        # through this exact same generic formatting any other enum gets.
        if val.type_name == "Option" and val.variant == "some":
            return f"some({_to_str(val.fields['value'])})"
        if val.fields:
            inner = ", ".join(f"{k}: {_to_str(v)}" for k, v in val.fields.items())
            return f"{val.type_name}.{val.variant} {{ {inner} }}"
        return f"{val.type_name}.{val.variant}"
    if isinstance(val, StructInstance):
        inner = ", ".join(f"{k}: {_to_str(v)}" for k, v in val.fields.items())
        return f"{val.type_name} {{ {inner} }}"
    if isinstance(val, (int, float, Decimal)):
        if val % 1 == 0:
            return str(int(val))
        return str(val)
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
    return_register = NONE_VALUE
    timers: list = []  # heap of (wake_time, seq, promise)
    timer_seq = itertools.count()

    def schedule_timer(delay_seconds: float, promise) -> None:
        wake_time = time.monotonic() + delay_seconds
        heapq.heappush(timers, (wake_time, next(timer_seq), promise))

    def drain_next_timer() -> bool:
        """Pop and resolve the earliest-firing timer, blocking (sleeping)
        for whatever time remains until it's due. Returns False if there
        were no timers to drain. Resolving a Promise runs its callbacks
        synchronously (see PromiseInstance.resolve), which is what
        actually resumes whatever task was awaiting it."""
        if not timers:
            return False
        wake_time, _seq, promise = heapq.heappop(timers)
        remaining = wake_time - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        promise.resolve(NONE_VALUE)
        return True

    def step_task(task):
        """Advance `task` until it either finishes -- its own
        return_stack empties right after a `ret` at that task's outermost
        level, or it hits the top-level halt sentinel (task 0 only) --
        returning ("done", value), or hits a real suspension (an `await`
        on a still-pending Promise), returning ("suspended", None) having
        already arranged (via a callback on that Promise) for `drive` to
        be called again once it resolves. Reentrant: the `detach` case
        below calls `step_task` again, for a brand new Task, while this
        very call is still on the Python stack -- bounded by how many
        tasks are actually mid-detach at once, not by Mah call depth (see
        docs/NEXT_PHASES.md's "Task-based scheduling" section for why
        that's fine)."""
        nonlocal return_register
        while True:
            operation = code_block[task.pc]
            task.pc += 1

            match operation:
                case (None, None, None, None):
                    return "done", NONE_VALUE
                case ("print", arg, None, None):
                    val = _read(task.current_frame, arg)
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
                    _write(task.current_frame, dest, num)
                case ("jmpf", cond_addr, None, loc):
                    if not _read(task.current_frame, cond_addr):
                        task.pc = loc
                case ("jmp", None, None, loc):
                    task.pc = loc
                # Binary-op tuples are `(op, left_addr, right_addr, dest)` in
                # plain left-to-right order (see codegen.py's module docstring
                # for why this differs from v1's reversed convention).
                case ("+", left, right, dest):
                    a = _read(task.current_frame, left)
                    b = _read(task.current_frame, right)
                    if isinstance(a, str) or isinstance(b, str):
                        _write(task.current_frame, dest, _to_str(a) + _to_str(b))
                    else:
                        _write(task.current_frame, dest, a + b)
                case ("sin", arg, None, dest):
                    _write(task.current_frame, dest, math.sin(_read(task.current_frame, arg)))
                case ("cos", arg, None, dest):
                    _write(task.current_frame, dest, math.cos(_read(task.current_frame, arg)))
                case ("neg", arg, None, dest):
                    _write(task.current_frame, dest, -_read(task.current_frame, arg))
                case ("**", left, right, dest):
                    a = _read(task.current_frame, left)
                    b = _read(task.current_frame, right)
                    _write(task.current_frame, dest, a**b)
                case ("*", left, right, dest):
                    a = _read(task.current_frame, left)
                    b = _read(task.current_frame, right)
                    if isinstance(a, str) and isinstance(b, (int, Decimal)) and not isinstance(b, bool):
                        _write(task.current_frame, dest, a * int(b))
                    elif isinstance(b, str) and isinstance(a, (int, Decimal)) and not isinstance(a, bool):
                        _write(task.current_frame, dest, b * int(a))
                    else:
                        _write(task.current_frame, dest, a * b)
                case ("-", left, right, dest):
                    a = _read(task.current_frame, left)
                    b = _read(task.current_frame, right)
                    _write(task.current_frame, dest, a - b)
                case ("/", left, right, dest):
                    a = _read(task.current_frame, left)
                    b = _read(task.current_frame, right)
                    _write(task.current_frame, dest, a / b)
                case ("//", left, right, dest):
                    a = _read(task.current_frame, left)
                    b = _read(task.current_frame, right)
                    _write(task.current_frame, dest, a // b)
                case ("%", left, right, dest):
                    a = _read(task.current_frame, left)
                    b = _read(task.current_frame, right)
                    _write(task.current_frame, dest, a % b)
                case ("lt", left, right, dest):
                    _write(task.current_frame, dest, _read(task.current_frame, left) < _read(task.current_frame, right))
                case ("gt", left, right, dest):
                    _write(task.current_frame, dest, _read(task.current_frame, left) > _read(task.current_frame, right))
                case ("and", left, right, dest):
                    _write(
                        task.current_frame,
                        dest,
                        bool(_read(task.current_frame, left) and _read(task.current_frame, right)),
                    )
                case ("or", left, right, dest):
                    _write(
                        task.current_frame,
                        dest,
                        bool(_read(task.current_frame, left) or _read(task.current_frame, right)),
                    )
                case ("neq", left, right, dest):
                    _write(task.current_frame, dest, _read(task.current_frame, left) != _read(task.current_frame, right))
                case ("eq", left, right, dest):
                    _write(task.current_frame, dest, _read(task.current_frame, left) == _read(task.current_frame, right))
                case ("=", src, None, dest):
                    _write(task.current_frame, dest, _read(task.current_frame, src))
                case ("ld", value, None, dest):
                    _write(task.current_frame, dest, value)
                case ("closure", code_addr, meta, dest):
                    slot_count, param_count, name = meta
                    _write(task.current_frame, dest, Closure(code_addr, task.current_frame, slot_count, param_count, name))
                case ("call", callee_addr, arg_addrs, None):
                    closure = _read(task.current_frame, callee_addr)
                    if not isinstance(closure, Closure):
                        raise Exception(f"Tried to call a non function at position {task.pc}")
                    if len(arg_addrs) != closure.param_count:
                        label = f"'{closure.name}'" if closure.name else "function"
                        raise Exception(
                            f"Argument Count is invalid. {label} accepts {closure.param_count} "
                            f"arguments but {len(arg_addrs)} was given at position {task.pc}"
                        )
                    arg_values = [_read(task.current_frame, a) for a in arg_addrs]
                    new_frame = Frame(slots=[None] * closure.slot_count, static_parent=closure.defining_frame)
                    for i, v in enumerate(arg_values):
                        new_frame.slots[i] = v
                    task.return_stack.append((task.pc, task.current_frame))
                    task.current_frame = new_frame
                    task.pc = closure.code_address
                case ("ret", value_addr, None, None):
                    return_register = _read(task.current_frame, value_addr)
                    if not task.return_stack:
                        return "done", return_register
                    task.pc, task.current_frame = task.return_stack.pop()
                case ("retval", None, None, dest):
                    _write(task.current_frame, dest, return_register)
                case ("detach", callee_addr, arg_addrs, dest):
                    closure = _read(task.current_frame, callee_addr)
                    if not isinstance(closure, Closure):
                        raise Exception(f"Tried to detach a non function at position {task.pc}")
                    if len(arg_addrs) != closure.param_count:
                        label = f"'{closure.name}'" if closure.name else "function"
                        raise Exception(
                            f"Argument Count is invalid. {label} accepts {closure.param_count} "
                            f"arguments but {len(arg_addrs)} was given at position {task.pc}"
                        )
                    arg_values = [_read(task.current_frame, a) for a in arg_addrs]
                    new_frame = Frame(slots=[None] * closure.slot_count, static_parent=closure.defining_frame)
                    for i, v in enumerate(arg_values):
                        new_frame.slots[i] = v
                    promise = PromiseInstance()
                    new_task = Task(pc=closure.code_address, current_frame=new_frame, watching_promise=promise)
                    drive(new_task)
                    _write(task.current_frame, dest, promise)
                case ("await", promise_addr, None, dest):
                    value = _read(task.current_frame, promise_addr)
                    if not isinstance(value, PromiseInstance):
                        raise Exception(f"'.await' used on a non-Promise value at position {task.pc}")
                    if value.variant == "Settled":
                        _write(task.current_frame, dest, value.fields["value"])
                    else:
                        resume_pc = task.pc

                        def _resume(resolved_value, task=task, dest=dest, resume_pc=resume_pc):
                            _write(task.current_frame, dest, resolved_value)
                            task.pc = resume_pc
                            drive(task)

                        value.callbacks.append(_resume)
                        return "suspended", None
                case ("sleepasync", ms_addr, None, dest):
                    ms = _read(task.current_frame, ms_addr)
                    promise = PromiseInstance()
                    schedule_timer(float(ms) / 1000.0, promise)
                    _write(task.current_frame, dest, promise)
                case ("struct", type_name, field_pairs, dest):
                    fields = {name: _read(task.current_frame, addr) for name, addr in field_pairs}
                    _write(task.current_frame, dest, StructInstance(type_name, fields))
                case ("enum", type_name, variant_and_pairs, dest):
                    variant, field_pairs = variant_and_pairs
                    fields = {name: _read(task.current_frame, addr) for name, addr in field_pairs}
                    _write(task.current_frame, dest, EnumInstance(type_name, variant, fields))
                case ("getfield", obj_addr, field_name, dest):
                    obj = _read(task.current_frame, obj_addr)
                    if not isinstance(obj, (StructInstance, EnumInstance)):
                        raise Exception(
                            f"Tried to access field '{field_name}' on a non-struct value at position {task.pc}"
                        )
                    if field_name not in obj.fields:
                        raise Exception(f"'{obj.type_name}' has no field '{field_name}' at position {task.pc}")
                    _write(task.current_frame, dest, obj.fields[field_name])
                case ("setfield", obj_addr, field_name, src_addr):
                    obj = _read(task.current_frame, obj_addr)
                    if not isinstance(obj, (StructInstance, EnumInstance)):
                        raise Exception(
                            f"Tried to access field '{field_name}' on a non-struct value at position {task.pc}"
                        )
                    if field_name not in obj.fields:
                        raise Exception(f"'{obj.type_name}' has no field '{field_name}' at position {task.pc}")
                    obj.fields[field_name] = _read(task.current_frame, src_addr)
                case ("matchtag", value_addr, tag_info, dest):
                    kind, type_name, variant = tag_info
                    val = _read(task.current_frame, value_addr)
                    if kind == "struct":
                        matched = isinstance(val, StructInstance) and val.type_name == type_name
                    else:
                        matched = (
                            isinstance(val, EnumInstance)
                            and val.type_name == type_name
                            and val.variant == variant
                        )
                    _write(task.current_frame, dest, matched)
                case ("matchfail", None, None, position):
                    raise Exception(f"No pattern in 'match' matched the value at position {position}")
                case ("deferpush", None, None, None):
                    task.defer_stack.append([])
                case ("deferadd", closure_addr, None, None):
                    # Registers the closure compiled from a `defer <stmt>`'s
                    # body onto the innermost currently-open defer scope --
                    # only actually-executed `defer`s reach here at runtime,
                    # matching ordinary execution order (see
                    # compiler/codegen.py's module docstring).
                    task.defer_stack[-1].append(_read(task.current_frame, closure_addr))
                case ("deferpeek", None, None, dest):
                    _write(task.current_frame, dest, bool(task.defer_stack[-1]))
                case ("deferpopclosure", None, None, dest):
                    _write(task.current_frame, dest, task.defer_stack[-1].pop())
                case ("deferscopepop", None, None, None):
                    task.defer_stack.pop()
                case catchall:
                    raise RuntimeError(f"invalid operation {catchall}")

    def drive(task) -> None:
        """Step `task` forward; if it truly finishes (immediately, or
        later via a resumed `_resume` callback above) and something is
        watching it (`task.watching_promise`, set by `detach` -- or the
        top-level Promise for task 0, see below), resolve that Promise.
        If it suspends instead, does nothing further here -- the
        suspending `await`'s own callback (registered above) is what
        calls `drive` again once whatever it was waiting on resolves, so
        a task's eventual completion is always correctly propagated no
        matter how many times it suspends along the way."""
        status, value = step_task(task)
        if status == "done" and task.watching_promise is not None:
            task.watching_promise.resolve(value)

    main_frame = Frame(slots=[None] * global_slot_count, static_parent=None)
    main_promise = PromiseInstance()
    main_task = Task(pc=0, current_frame=main_frame, watching_promise=main_promise)
    drive(main_task)

    # Node-like process lifetime: keep draining timers -- which may
    # resume main_task itself if IT was what suspended, or any other
    # still-pending detached task -- until main_task has truly finished
    # (main_promise resolved) AND nothing else is scheduled, rather than
    # exiting the instant main_task's own top-level code finishes and
    # abandoning pending detached work nobody ever awaited.
    while main_promise.variant != "Settled" or timers:
        if not drain_next_timer():
            break
