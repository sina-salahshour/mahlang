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

M12 adds `method_table`/`defmethod`/`callmethod` for traits/`impl`/method
calls (see docs/V2_DESIGN.md's M12 milestone and compiler/codegen.py's
module docstring for the opcode shapes): `method_table` is a runtime dict
keyed `(type_name, method_name) -> {"inherent": target_or_None, "traits":
{trait_name: target}}` where a `target` is `(fn, is_method)`, `fn` either a
`Closure` (a Mah-code method) or a plain Python callable (a native system-
trait method, see `NATIVE_TRAIT_METHODS` -- currently just
`Printable.to_string`, seeded for every built-in type). `defmethod` fills
one entry at program start (before the first ordinary top-level statement
runs, via compiler/codegen.py's `generate`-time hoisting); `callmethod`
looks a receiver's runtime type + method name up in it, applying the same
inherent-wins/single-trait-else-ambiguous/static-fn-is-an-error rules
compiler/resolve.py's `MethodCall` docstring describes for the compile-time
`Type.method(...)` case, but at runtime (dynamic `x.m(...)` dispatch has no
static type to resolve ahead of time). Two small refactors support this:
`enter_closure` factors the frame-building half of the `call` case out into
a reusable helper (`callmethod`'s Closure branch calls it too, after its
own arity check with a method-shaped message); `invoke_sync` runs a
Closure to completion via a brand-new, independent `Task` and
`step_task` (re-entrant already, same precedent as `detach`), used by
`to_str` to call a user `Printable.to_string` impl synchronously from
regular (non-async) code paths like `print`/string concatenation --
raising a clean error if that call ever tries to suspend (there is no
Promise anybody is watching for it). `to_str` replaces the old, non-
Printable-aware `_to_str` (renamed `_format_value`, still used standalone
outside a running program, e.g. by `main.py`'s error formatting) as the
formatting function `print`/`+` actually call: it checks
`method_table[(type_name_of(val), "to_string")]["traits"].get("Printable")`
first, calling a user (Mah-code) impl if present and requiring it to
return a `String`; otherwise it falls back to `_format_value(val, to_str)`
-- the existing structural formatting, itself recursing through `to_str`
(not `_format_value` again) for nested values, so a struct field or
`some(...)` payload whose own type has a user `Printable` impl formats
through that impl too.

M13 lifts two M12 trait limitations. (1) `p.f(args)` now also accepts a
closure stored in a struct/enum FIELD named `f`, not just a real method:
`find_method` (a helper closed over `method_table`, factored out of the old
inline `callmethod` case) does the ordinary M12 lookup first, and only when
that finds no target -- or finds only a static function -- AND the receiver
is a `StructInstance`/`EnumInstance` with a field named `f`, falls back to
that field's value (which must be a `Closure`); `callmethod`'s Closure
branch is invoked with the field value directly, no receiver prepended to
its args (`include_self=False`, vs. `True` for a real method). Ambiguity
(2+ traits providing the method) is still raised before the fallback is
even considered, and a trait-qualified call (`trait` argument set) never
falls back to a field at all -- both match M12's existing "does not
implement trait" error exactly. (2) `detach` now also accepts a method call
operand (`detach obj.m(args)`, `detach Type.m(args)`, `detach Trait.m(recv,
...)`) -- see compiler/codegen.py's module docstring for the new
`detachmethod` opcode. `spawn_detached` (factored out of the old inline
`detach` case) builds the frame/Promise/Task and drives it, shared by both
`detach`'s Closure branch and the new `detachmethod` case (which first runs
the same `find_method` lookup `callmethod` uses, then either spawns a
detached task for a Closure target or synchronously resolves a Promise
around a native target's return value, mirroring `callmethod`'s own
Closure-vs-native split).

Three new opcodes (M10): `detach` builds a fresh `Task` and `PromiseInstance`
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

from .runtime_values import (
    BUILTIN_TYPE_NAMES,
    Closure,
    EnumInstance,
    Frame,
    NONE_VALUE,
    PromiseInstance,
    StructInstance,
    Task,
    type_name_of,
)


def _format_value(val: Any, recurse) -> str:
    """The structural (non-Printable-aware) formatting logic shared by
    `_to_str` (module-level, no Printable awareness -- used outside a
    running program) and `run_code`'s own `to_str` (Printable-aware,
    recurses through itself instead of straight back into
    `_format_value`) -- see this module's docstring. `recurse` is called
    for every nested value (an enum payload, a struct field) so each
    caller's own notion of "how do I format a value" applies uniformly at
    every nesting depth, not just the top level."""
    if val is NONE_VALUE:
        return "none"
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, Closure):
        # M12: a function value prints as `<fn NAME>` (or `<fn>` for an
        # anonymous closure) instead of the Python object repr.
        return f"<fn {val.name}>" if val.name else "<fn>"
    if isinstance(val, EnumInstance):
        # PromiseInstance is a subclass of EnumInstance (see
        # runtime_values.py) and needs no special-casing here at all --
        # it prints as "Promise.Pending" / "Promise.Settled { value: ... }"
        # through this exact same generic formatting any other enum gets.
        if val.type_name == "Option" and val.variant == "some":
            return f"some({recurse(val.fields['value'])})"
        if val.fields:
            inner = ", ".join(f"{k}: {recurse(v)}" for k, v in val.fields.items())
            return f"{val.type_name}.{val.variant} {{ {inner} }}"
        return f"{val.type_name}.{val.variant}"
    if isinstance(val, StructInstance):
        inner = ", ".join(f"{k}: {recurse(v)}" for k, v in val.fields.items())
        return f"{val.type_name} {{ {inner} }}"
    if isinstance(val, (int, float, Decimal)):
        if val % 1 == 0:
            return str(int(val))
        return str(val)
    return str(val)


def _to_str(val: Any) -> str:
    """No Printable awareness (see `_format_value`'s docstring) -- used
    only outside a running program (there is no `method_table` to consult
    without one)."""
    return _format_value(val, _to_str)


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

    # M12: runtime method table -- (type_name, method_name) -> {"inherent":
    # target_or_None, "traits": {trait_name: target}}, a target being
    # `(fn, is_method)` with `fn` a Closure (a Mah-code method, registered
    # by `defmethod`) or a plain Python callable (a native system-trait
    # method -- see NATIVE_TRAIT_METHODS below). See this module's
    # docstring.
    method_table: dict = {}

    # M12: native implementations of this milestone's one system trait,
    # Printable -- every built-in type implements it natively (see
    # runtime_values.BUILTIN_TYPE_NAMES/SYSTEM_TRAITS), so `5.to_string()`,
    # `Printable.to_string(true)`, `Number.to_string(3)`, `none.to_string()`
    # all work with no user-written `impl`. Defined here (inside run_code)
    # since it needs `to_str`, itself defined below.
    NATIVE_TRAIT_METHODS = {("Printable", "to_string"): lambda v: to_str(v)}
    for builtin_type in BUILTIN_TYPE_NAMES:
        for (trait_name, method_name), native_fn in NATIVE_TRAIT_METHODS.items():
            method_table.setdefault((builtin_type, method_name), {"inherent": None, "traits": {}})[
                "traits"
            ][trait_name] = (native_fn, True)

    def enter_closure(task, closure, arg_values) -> None:
        """M12: the frame-building half of the `call` opcode's own logic,
        factored out so `callmethod`'s Closure branch can reuse it after
        its own (method-shaped) arity check -- see this module's
        docstring. Builds a fresh Frame, fills its parameter slots, pushes
        the caller's own (pc, frame) onto the return stack, and switches
        `task` to the new frame/pc -- the exact same steps the `call` case
        below used to do inline."""
        new_frame = Frame(slots=[None] * closure.slot_count, static_parent=closure.defining_frame)
        for i, v in enumerate(arg_values):
            new_frame.slots[i] = v
        task.return_stack.append((task.pc, task.current_frame))
        task.current_frame = new_frame
        task.pc = closure.code_address

    def find_method(recv, name, trait, position):
        """M13: shared lookup for `callmethod`/`detachmethod`. Returns
        `(fn, include_self)` -- `fn` a Closure or native callable;
        `include_self` is False only for the field-closure fallback (the
        field's own value is called with exactly the call's own args, no
        receiver prepended). Raises the same errors `callmethod` always
        has for ambiguity / "does not implement trait" / "no method" /
        "static function called as a method" -- see this module's
        docstring."""
        tname = type_name_of(recv)
        entry = method_table.get((tname, name))
        target = None
        if entry is not None:
            if trait is not None:
                target = entry["traits"].get(trait)
            elif entry["inherent"] is not None:
                target = entry["inherent"]
            elif len(entry["traits"]) == 1:
                target = next(iter(entry["traits"].values()))
            elif len(entry["traits"]) > 1:
                raise Exception(
                    f"Method '{name}' on '{tname}' is ambiguous: provided by traits "
                    f"{sorted(entry['traits'])}; call it as 'Trait.{name}(value, ...)' "
                    f"at position {position}"
                )
        if trait is None and (target is None or not target[1]):
            if isinstance(recv, (StructInstance, EnumInstance)) and name in recv.fields:
                value = recv.fields[name]
                if not isinstance(value, Closure):
                    raise Exception(
                        f"Field '{name}' of '{tname}' is not a function (it holds a "
                        f"{type_name_of(value)}) at position {position}"
                    )
                return value, False
        if target is None:
            if trait is not None:
                raise Exception(
                    f"'{tname}' does not implement trait '{trait}' (no method '{name}') "
                    f"at position {position}"
                )
            raise Exception(f"'{tname}' has no method '{name}' at position {position}")
        fn, is_method = target
        if not is_method:
            raise Exception(
                f"'{name}' is a static function of '{tname}', not a method; call it as "
                f"'{tname}.{name}(...)' at position {position}"
            )
        return fn, True

    def method_call_args_or_raise(recv, fn, include_self, name, arg_addrs, frame, position):
        """M13: shared by `callmethod`/`detachmethod` -- reads `arg_addrs`
        (plus `recv` when `include_self`), raising the same arity-mismatch
        messages each opcode always has (a method-shaped message when
        `include_self`, the field-closure message from `find_method`'s
        spec otherwise)."""
        args = ([recv] if include_self else []) + [_read(frame, a) for a in arg_addrs]
        if isinstance(fn, Closure) and len(args) != fn.param_count:
            if include_self:
                raise Exception(
                    f"Argument Count is invalid. method '{name}' accepts "
                    f"{fn.param_count - 1} arguments but {len(args) - 1} was given "
                    f"at position {position}"
                )
            raise Exception(
                f"Argument Count is invalid. '{name}' accepts {fn.param_count} "
                f"arguments but {len(args)} was given at position {position}"
            )
        if not isinstance(fn, Closure) and len(args) != 1:
            # Every native method in this milestone is (self) only -- see
            # NATIVE_TRAIT_METHODS below. A native target is only ever
            # reached with include_self=True (find_method never falls back
            # to a field for a target that came from method_table).
            raise Exception(
                f"Argument Count is invalid. method '{name}' accepts 0 arguments "
                f"but {len(args) - 1} was given at position {position}"
            )
        return args

    def spawn_detached(closure, arg_values):
        """M13: the task-spawning half of the `detach` opcode's own logic,
        factored out so the new `detachmethod` opcode (a detached method
        call whose target is a Closure) can reuse it -- builds the frame,
        wraps it in a `Task` watched by a fresh `Promise`, drives it, and
        returns that (possibly still-pending) Promise. See this module's
        docstring."""
        new_frame = Frame(slots=[None] * closure.slot_count, static_parent=closure.defining_frame)
        for i, v in enumerate(arg_values):
            new_frame.slots[i] = v
        promise = PromiseInstance()
        new_task = Task(pc=closure.code_address, current_frame=new_frame, watching_promise=promise)
        drive(new_task)
        return promise

    def invoke_sync(closure, arg_values, label: str):
        """M12: run `closure` to completion synchronously, from ordinary
        (non-async) interpreter code -- used by `to_str` to call a user
        `Printable.to_string` impl. Spins up a brand-new, independent
        `Task` (own pc/frame/return_stack/defer_stack) and drives it via
        `step_task` -- re-entrant already (the `detach` case below relies
        on the exact same reentrancy), so this works correctly even when
        called from deep inside another task's own execution. Raises if
        the call ever genuinely suspends (awaits a still-pending Promise)
        -- there is no Promise anybody is watching for an implicit,
        synchronous call like this one."""
        if len(arg_values) != closure.param_count:
            call_label = f"'{closure.name}'" if closure.name else "function"
            raise Exception(
                f"Argument Count is invalid. {call_label} accepts {closure.param_count} "
                f"arguments but {len(arg_values)} was given"
            )
        frame = Frame(slots=[None] * closure.slot_count, static_parent=closure.defining_frame)
        for i, v in enumerate(arg_values):
            frame.slots[i] = v
        sub_task = Task(pc=closure.code_address, current_frame=frame)
        status, value = step_task(sub_task)
        if status == "suspended":
            raise Exception(
                f"'{label}' cannot suspend (it awaited a pending Promise) when called "
                "implicitly by the runtime"
            )
        return value

    def to_str(val) -> str:
        """M12: Printable-aware formatting -- the function `print` and
        string concatenation (`+`) actually call, in place of the old,
        non-Printable-aware `_to_str`. If `val`'s runtime type has a user
        (Mah-code) `Printable` impl, calls its `to_string` (via
        `invoke_sync`) and requires it to return a `String`; otherwise
        falls back to the existing structural formatting
        (`_format_value`), itself recursing through `to_str` (not
        `_format_value` directly) for nested values, so a struct field or
        `some(...)` payload whose own type has a user impl formats through
        that impl too, at any nesting depth."""
        entry = method_table.get((type_name_of(val), "to_string"))
        target = entry["traits"].get("Printable") if entry else None
        if target is not None and isinstance(target[0], Closure):
            result = invoke_sync(target[0], [val], "to_string")
            if not isinstance(result, str):
                raise Exception(
                    f"Printable.to_string for '{type_name_of(val)}' must return a String, "
                    f"got {type_name_of(result)}"
                )
            return result
        return _format_value(val, to_str)

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
                    print(to_str(val))
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
                        _write(task.current_frame, dest, to_str(a) + to_str(b))
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
                    enter_closure(task, closure, arg_values)
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
                    promise = spawn_detached(closure, arg_values)
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
                case ("defmethod", closure_addr, meta, None):
                    closure = _read(task.current_frame, closure_addr)
                    type_name, trait, name, is_method = meta
                    entry = method_table.setdefault((type_name, name), {"inherent": None, "traits": {}})
                    if trait is None:
                        entry["inherent"] = (closure, is_method)
                    else:
                        entry["traits"][trait] = (closure, is_method)
                case ("callmethod", recv_addr, call_info, None):
                    name, arg_addrs, trait, position = call_info
                    recv = _read(task.current_frame, recv_addr)
                    fn, include_self = find_method(recv, name, trait, position)
                    args = method_call_args_or_raise(
                        recv, fn, include_self, name, arg_addrs, task.current_frame, position
                    )
                    if isinstance(fn, Closure):
                        enter_closure(task, fn, args)
                    else:
                        return_register = fn(*args)
                case ("detachmethod", recv_addr, call_info, dest):
                    name, arg_addrs, trait, position = call_info
                    recv = _read(task.current_frame, recv_addr)
                    fn, include_self = find_method(recv, name, trait, position)
                    args = method_call_args_or_raise(
                        recv, fn, include_self, name, arg_addrs, task.current_frame, position
                    )
                    if isinstance(fn, Closure):
                        promise = spawn_detached(fn, args)
                    else:
                        promise = PromiseInstance()
                        promise.resolve(fn(*args))
                    _write(task.current_frame, dest, promise)
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
