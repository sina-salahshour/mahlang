"""The Mah VM -- executes a `.mahc` `Program` exactly as specified by
docs/MAHC_FORMAT.md #5/#6. This module (and everything it imports) may
depend only on `mah.runtime_values`, `mah.bytecode.*`, `mah.natives`, and
the stdlib -- **never** `mah.compiler`, `mah.preprocessor`, or `mah.lsp` --
so a `.mahc` file is a complete, self-describing program: nothing about
how it got compiled leaks into how it runs (see
`tests/test_bytecode.py`'s VM-independence subprocess check, and
docs/MAHC_FORMAT.md #1's "portable" design goal).

Two public entry points: `run_bytes(data)` (decode + run) and
`run_program(program)` (run an already-decoded/already-built `Program`
directly -- used by `mah run`'s in-process path once it already has bytes
it just encoded, and by tests that build a `Program` by hand).

Pipeline: `_link(program)` resolves every instruction's string/constant/
type/function/native indices into direct Python values ONCE at load time
(see `_link_instr` and friends below), producing a flat list of ready-to-
execute tuples the step loop just pattern-matches on -- mirroring the
pre-M14 interpreter's own IR tuples (same idea, new opcode names, and the
address-operand shape -- `(depth, slot)` pairs read by walking
`Frame.static_parent` -- is unchanged from that design). Native names are
validated against `mah.natives.NATIVES` during linking, and validation
happens for every declared native regardless of whether the program's
control flow ever reaches a use of it -- so a file naming an unsupported
native is rejected before a single instruction runs (see
docs/MAHC_FORMAT.md #3's "fail fast" and `tests/test_bytecode.py`'s
`test_unsupported_native_...` case, which prints something first and
checks stdout is still empty).

Frames/tasks/promises/defer/methods/the scheduler are otherwise a direct
port of the pre-M14 interpreter (see git history / docs/V2_DESIGN.md's
M1/M9/M10/M12/M13 milestones for the *design* rationale, unchanged here) --
M14 only changes: opcode names/shapes (the bytecode ones instead of
codegen.py's IR tuples), frame slots start filled with the real `none`
value (`NONE_VALUE`) instead of Python `None`, every Number is a
`Decimal`, `eq`/`neq`/`lt`/`gt`/arithmetic/truthiness follow
docs/MAHC_FORMAT.md #5/#6.2 exactly (a Bool is never `==` a Number, a
struct/enum/Function/Promise compares by identity, `lt`/`gt` reject
anything but two Numbers or two Strings), and every runtime error is a
`MahRuntimeError` with a clean, location-free message that the step loop
itself appends `at position ...` to exactly once (see `_step`/`_locate`
below) instead of instructions baking a (meaningless, pre-M14) raw `pc`
into their own error text.
"""

from __future__ import annotations

import bisect
import heapq
import itertools
import time
from decimal import Decimal
from typing import Any, NamedTuple

from .bytecode.decode import decode
from .bytecode.format import MahcFormatError
from .bytecode.program import Program
from .natives import NATIVES, NativeContext
from .runtime_values import (
    BUILTIN_TYPE_NAMES,
    Closure,
    EnumInstance,
    Frame,
    MahRuntimeError,
    NONE_VALUE,
    PromiseInstance,
    StructInstance,
    Task,
    type_name_of,
)

_BINOP_SYMBOLS = {
    "add": "+", "sub": "-", "mul": "*", "div": "/",
    "idiv": "//", "mod": "%", "pow": "**", "lt": "<", "gt": ">",
    "le": "<=", "ge": ">=",  # M17 (1.2)
}


class _Absent:
    """M16: the sentinel a defaulted-but-unbound parameter slot holds until
    the callee's own `jmpset`-guarded default-computation code runs (see
    docs/MAHC_FORMAT.md #6.1) -- never a real Mah value, never observed by
    anything outside `_bind_params`/`jmpset` as long as an encoder emits
    correct `jmpset` guards (the VM's own responsibility ends at providing
    the mechanism)."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<absent>"


ABSENT = _Absent()


class NativeMethod(NamedTuple):
    """M17: a native (VM-builtin) method-table target that isn't
    `Printable.to_string` -- i.e. one that may take arguments after the
    receiver (docs/MAHC_FORMAT.md #6.7: `String.char_at(i)`). `arity` is
    the argument count EXCLUDING the receiver (0 for `to_string`/`len`/
    `arity`, 1 for `char_at`); `impl` is a plain Python callable taking the
    receiver followed by that many positional values. Wrapping every
    native target this way (even the old arity-0 ones) keeps
    `_bind_method_call`'s native branch uniform instead of hardcoding
    \"natives always take zero extra arguments\", which stopped being true
    the moment `char_at` was added."""

    arity: int
    impl: object


def _bind_params(param_count: int, params, values: list, kwargs: list, label: str) -> list:
    """docs/MAHC_FORMAT.md #6.1's argument-binding algorithm, exactly --
    shared by every call-shaped opcode (`call`, `callkw`, `callmethod(kw)`,
    `detach(kw)`, `detachmethod(kw)`, and `invoke_sync`). `params` is
    `None` (a 1.0 file: every parameter unnamed and required) or a list of
    `(name, has_default)` parallel to param slots `0..param_count-1`.
    `values` are the positional arguments, in order; `kwargs` is
    `list[(name, value)]`, already evaluated, in call-site order. Returns a
    list of length `param_count` (each slot's bound value, or `ABSENT` for
    a still-unbound defaulted parameter) or raises `MahRuntimeError` with
    the exact wording that section specifies. `label` is the fully
    formatted subject of every message (`"'f'"`, `"function"`, or
    `"method 'm'"`) -- callers decide that, since it depends on context
    (plain call vs. method call) this function has no way to know."""
    m = len(values)
    n = param_count
    has_any_default = params is not None and any(has_default for _name, has_default in params)
    if not kwargs and not has_any_default:
        # Old (pre-M16) wording, unconditionally, for the common case with
        # no keyword arguments and no defaulted parameters -- existing
        # tests assert this exact string.
        if m != n:
            raise MahRuntimeError(f"Argument Count is invalid. {label} accepts {n} arguments but {m} was given")
        return list(values)
    if m > n:
        raise MahRuntimeError(f"{label} takes at most {n} positional arguments but {m} were given")
    bound: list = list(values) + [ABSENT] * (n - m)
    bound_flags = [True] * m + [False] * (n - m)
    name_to_index = {pname: i for i, (pname, _has_default) in enumerate(params)} if params else {}
    for k, w in kwargs:
        idx = name_to_index.get(k)
        if idx is None:
            raise MahRuntimeError(f"{label} got an unexpected keyword argument '{k}'")
        if bound_flags[idx]:
            raise MahRuntimeError(f"{label} got multiple values for argument '{k}'")
        bound[idx] = w
        bound_flags[idx] = True
    for i in range(n):
        if bound_flags[i]:
            continue
        has_default = params[i][1] if params else False
        if not has_default:
            pname = params[i][0] if params else f"#{i}"
            raise MahRuntimeError(f"{label} is missing required argument '{pname}'")
    return bound


def _bind_method_call(recv, fn, include_self: bool, name: str, values: list, kwargs: list) -> list:
    """M16: `_bind_params` wrapper for `callmethod(kw)`/`detachmethod(kw)`
    (and the field-closure fallback) -- docs/MAHC_FORMAT.md #6.1's "for
    method calls the counts exclude the receiver and the label is
    `method 'f'`". `recv` is prepended back onto the bound list afterward
    (it's always positionally bound, slot 0, never named in `kwargs`,
    never defaulted) so the caller gets a plain `param_count`-long list
    exactly like a non-method call's, ready for `enter_closure`/
    `spawn_detached`."""
    label = f"method '{name}'" if include_self else f"'{name}'"
    if isinstance(fn, Closure):
        if include_self:
            rest_params = fn.params[1:] if fn.params is not None else None
            bound_rest = _bind_params(fn.param_count - 1, rest_params, values, kwargs, label)
            return [recv] + bound_rest
        return _bind_params(fn.param_count, fn.params, values, kwargs, label)
    # A native target -- always a system-trait/inherent method (`is_method`
    # true), so `include_self` is always true here; never accepts keyword
    # arguments (there are no declared parameter names to bind them to --
    # docs/MAHC_FORMAT.md #6.7). M17: a native's arity (excluding the
    # receiver) may be nonzero (`String.char_at`'s `i`), so this validates
    # against `fn.arity` instead of hardcoding 0.
    if kwargs:
        raise MahRuntimeError(f"{label} got an unexpected keyword argument '{kwargs[0][0]}'")
    arity = fn.arity if isinstance(fn, NativeMethod) else 0
    if len(values) != arity:
        raise MahRuntimeError(
            f"Argument Count is invalid. {label} accepts {arity} arguments but {len(values)} was given"
        )
    return [recv] + list(values)


def _call_native(fn, bound: list):
    """Invoke a native method-table target (`fn`, from `find_method`, once
    `isinstance(fn, Closure)` has already been ruled out) -- `fn` is a
    `NativeMethod(arity, impl)` (M17+) or, for exactly the pre-M17
    `Printable.to_string` shape kept for backward source-compatibility
    with any other module still constructing a target tuple by hand, a
    plain callable taking only the receiver."""
    impl = fn.impl if isinstance(fn, NativeMethod) else fn
    return impl(*bound)


# ---------------------------------------------------------------------------
# Linking: Program -> a flat list of directly-executable instruction tuples
# ---------------------------------------------------------------------------

class TypeInfo(NamedTuple):
    kind: int  # 0 = struct, 1 = enum
    name: str
    fields: list | None       # kind 0: declared field names, in order
    variants: list | None     # kind 1: [(variant_name, [field_name, ...]), ...]


class FunctionInfo(NamedTuple):
    entry: int
    slot_count: int
    param_count: int
    name: str | None
    # M16: parallel to param slots 0..param_count-1 -- [(name, has_default),
    # ...], or `None` for a 1.0 file (unnamed, all-required parameters) --
    # see docs/MAHC_FORMAT.md #4.5a.
    params: list | None


class DebugIndex(NamedTuple):
    # Parallel arrays, sorted by pc (ascending), for a `bisect` lookup of
    # "which run covers this pc" -- see `_locate`.
    pcs: list
    runs: list           # (file_idx, line, col), aligned with `pcs`
    file_paths: list      # file index -> relative path string


class LinkedProgram(NamedTuple):
    constants: list
    types: list
    natives: list          # (name, arity, impl) aligned to native index
    functions: list        # FunctionInfo aligned to function index
    code: list              # directly-executable instruction tuples
    debug: DebugIndex | None


def _convert_const(const, strings: list) -> Any:
    tag = const.tag
    if tag == 0:
        return NONE_VALUE
    if tag == 1:
        return False
    if tag == 2:
        return True
    if tag == 3:
        return Decimal(const.value)
    if tag == 4:
        return Decimal(strings[const.value])
    if tag == 5:
        return strings[const.value]
    raise AssertionError(f"unknown constant tag {tag}")


def _build_types(type_decls: list, strings: list) -> list:
    infos = [
        TypeInfo(1, "Option", None, [("none", []), ("some", ["value"])]),
        TypeInfo(1, "Promise", None, [("Pending", []), ("Settled", ["value"])]),
    ]
    for t in type_decls:
        name = strings[t.name]
        if t.kind == 0:
            infos.append(TypeInfo(0, name, [strings[f] for f in t.fields], None))
        else:
            infos.append(
                TypeInfo(1, name, None, [(strings[vn], [strings[f] for f in vf]) for vn, vf in t.variants])
            )
    return infos


def _validate_natives(native_refs: list, strings: list) -> list:
    linked = []
    for ref in native_refs:
        name = strings[ref.name]
        entry = NATIVES.get(name)
        if entry is None or entry[0] != ref.arity:
            raise MahcFormatError(f"this VM does not support native '{name}' (arity {ref.arity})")
        linked.append((name, entry[0], entry[1]))
    return linked


def _link_instr(instr, strings: list, constants: list, types: list, natives: list, functions: list):
    op = instr.op
    a = instr.args
    if op == "halt":
        return ("halt",)
    if op == "move":
        return ("move", a[0], a[1])
    if op == "loadk":
        return ("loadk", constants[a[0]], a[1])
    if op == "jmp":
        return ("jmp", a[0])
    if op == "jmpf":
        return ("jmpf", a[0], a[1])
    if op == "jmpset":
        return ("jmpset", a[0], a[1])
    if op in ("add", "sub", "mul", "div", "idiv", "mod", "pow", "eq", "neq", "lt", "gt", "le", "ge", "and", "or"):
        return (op, a[0], a[1], a[2])
    if op == "neg":
        return ("neg", a[0], a[1])
    if op == "not":
        return ("not", a[0], a[1])
    if op == "closure":
        return ("closure", functions[a[0]], a[1])
    if op == "call":
        return ("call", a[0], a[1])
    if op == "callkw":
        callee, arg_addrs, kwnames = a
        return ("callkw", callee, arg_addrs, tuple(strings[i] for i in kwnames))
    if op == "ret":
        return ("ret", a[0])
    if op == "retval":
        return ("retval", a[0])
    if op == "callmethod":
        recv, name_idx, args, trait_idx = a
        return ("callmethod", recv, strings[name_idx], args, strings[trait_idx] if trait_idx is not None else None)
    if op == "callmethodkw":
        recv, name_idx, args, kwnames, trait_idx = a
        return (
            "callmethodkw",
            recv,
            strings[name_idx],
            args,
            tuple(strings[i] for i in kwnames),
            strings[trait_idx] if trait_idx is not None else None,
        )
    if op == "defmethod":
        closure_addr, type_idx, trait_idx, name_idx, is_method = a
        return (
            "defmethod",
            closure_addr,
            strings[type_idx],
            strings[trait_idx] if trait_idx is not None else None,
            strings[name_idx],
            is_method,
        )
    if op == "detach":
        return ("detach", a[0], a[1], a[2])
    if op == "detachkw":
        callee, arg_addrs, kwnames, dest = a
        return ("detachkw", callee, arg_addrs, tuple(strings[i] for i in kwnames), dest)
    if op == "detachmethod":
        recv, name_idx, args, trait_idx, dest = a
        return (
            "detachmethod",
            recv,
            strings[name_idx],
            args,
            strings[trait_idx] if trait_idx is not None else None,
            dest,
        )
    if op == "detachmethodkw":
        recv, name_idx, args, kwnames, trait_idx, dest = a
        return (
            "detachmethodkw",
            recv,
            strings[name_idx],
            args,
            tuple(strings[i] for i in kwnames),
            strings[trait_idx] if trait_idx is not None else None,
            dest,
        )
    if op == "await":
        return ("await", a[0], a[1])
    if op == "struct":
        t_idx, values, dest = a
        return ("struct", types[t_idx], values, dest)
    if op == "enum":
        t_idx, variant_idx, values, dest = a
        return ("enum", types[t_idx], variant_idx, values, dest)
    if op == "getfield":
        obj, field_idx, dest = a
        return ("getfield", obj, strings[field_idx], dest)
    if op == "setfield":
        obj, field_idx, src = a
        return ("setfield", obj, strings[field_idx], src)
    if op == "matchstruct":
        value, t_idx, dest = a
        return ("matchstruct", value, types[t_idx], dest)
    if op == "matchenum":
        value, t_idx, variant_idx, dest = a
        return ("matchenum", value, types[t_idx], variant_idx, dest)
    if op == "matchrange":
        value, lo, hi, inclusive, dest = a
        return ("matchrange", value, lo, hi, inclusive, dest)
    if op == "matchfail":
        return ("matchfail",)
    if op == "deferpush":
        return ("deferpush",)
    if op == "deferadd":
        return ("deferadd", a[0])
    if op == "deferpeek":
        return ("deferpeek", a[0])
    if op == "deferpop":
        return ("deferpop", a[0])
    if op == "deferscopepop":
        return ("deferscopepop",)
    if op == "native":
        native_idx, args, dest = a
        _name, _arity, impl = natives[native_idx]
        return ("native", impl, args, dest)
    raise AssertionError(f"unknown linked opcode {op!r}")


def _link_debug(debug, strings: list) -> DebugIndex:
    file_paths = [strings[i] for i in debug.files]
    pcs = [run[0] for run in debug.runs]
    runs = [(run[1], run[2], run[3]) for run in debug.runs]
    return DebugIndex(pcs, runs, file_paths)


def _link(program: Program) -> LinkedProgram:
    strings = program.strings
    constants = [_convert_const(c, strings) for c in program.constants]
    types = _build_types(program.types, strings)
    natives = _validate_natives(program.natives, strings)
    functions = [
        FunctionInfo(
            fn.entry,
            fn.slot_count,
            fn.param_count,
            strings[fn.name] if fn.name is not None else None,
            [(strings[name_idx], has_default) for name_idx, has_default in fn.params]
            if fn.params is not None
            else None,
        )
        for fn in program.functions
    ]
    code = [_link_instr(instr, strings, constants, types, natives, functions) for instr in program.code]
    debug = _link_debug(program.debug, strings) if program.debug is not None else None
    return LinkedProgram(constants, types, natives, functions, code, debug)


# ---------------------------------------------------------------------------
# Value helpers -- docs/MAHC_FORMAT.md #5/#6.2/#6.6
# ---------------------------------------------------------------------------

def _is_number(v: Any) -> bool:
    return type_name_of(v) == "Number"


def truthy(v: Any) -> bool:
    """docs/MAHC_FORMAT.md #5: exactly four falsy values -- `false`,
    `none`, the Number `0`, and the empty String -- everything else
    (including every struct/enum instance other than `none` itself, e.g.
    `some(false)`) is truthy."""
    if v is NONE_VALUE:
        return False
    if isinstance(v, bool):
        return v
    if _is_number(v):
        return v != 0
    if isinstance(v, str):
        return v != ""
    return True


def _values_equal(a: Any, b: Any) -> bool:
    """docs/MAHC_FORMAT.md #6.2: Numbers/Strings/Bools compare by value,
    `none` equals only `none`; every other value (struct/enum instances --
    including `some(x)` -- Function, Promise) is equal only to itself. A
    plain identity check (`a is b`) already gives the right answer for
    `none` (the shared `NONE_VALUE` singleton) and for every "identity
    only" case, so only Number/String/Bool need their own branch -- and the
    type-name check up front is what keeps a Bool from ever `==` a Number
    (Python's `bool` is an `int`/`Decimal`-comparable subclass of `int`)."""
    ta, tb = type_name_of(a), type_name_of(b)
    if ta != tb:
        return False
    if ta in ("Number", "String", "Bool"):
        return a == b
    return a is b


def _matchrange(val: Any, lo: Any, hi: Any, inclusive: bool) -> bool:
    """docs/MAHC_FORMAT.md #6.3: `matchrange` -- true iff `val` and every
    PRESENT bound (`lo`/`hi` is the Python `None` "absent" marker set by
    `_decode_operand`'s `A?` kind, never a real Mah value -- a genuine Mah
    `none` is `runtime_values.NONE_VALUE`, a distinct object) are all
    Numbers or all Strings, and `lo` absent or `lo <= val`, and `hi`
    absent, or `val < hi`, or `val <= hi` when `inclusive`. Never raises: a
    value of another type (or a type mismatch against a bound) simply
    doesn't match."""
    if _is_number(val):
        kind_ok = _is_number
    elif isinstance(val, str):
        kind_ok = lambda v: isinstance(v, str)  # noqa: E731
    else:
        return False
    if lo is not None and not kind_ok(lo):
        return False
    if hi is not None and not kind_ok(hi):
        return False
    if lo is not None and not (lo <= val):
        return False
    if hi is not None:
        if inclusive:
            return val <= hi
        return val < hi
    return True


def _format_number(v: Decimal) -> str:
    if v == v.to_integral_value():
        return str(int(v))
    return format(v.normalize(), "f")


def _string_char_at(s: str, i: Any) -> str:
    """M17: `String.char_at(i)` -- docs/MAHC_FORMAT.md #6.7. Unicode code
    points, one per index (Python `str` indexing already is code-point
    based) -- `0 <= i < len(s)`, `i` an integer Number."""
    if not _is_number(i):
        raise MahRuntimeError(f"char_at index must be a Number, got {type_name_of(i)}")
    if i != i.to_integral_value() or i < 0 or i >= len(s):
        raise MahRuntimeError(f"char_at index {_format_number(i)} is out of range for a String of length {len(s)}")
    return s[int(i)]


def _format_value(val: Any, recurse) -> str:
    """Structural (non-`Printable`-aware) formatting -- `recurse` is called
    for every nested value (an enum payload, a struct field) so `to_str`
    (below) can thread `Printable` dispatch through nested values too."""
    if val is NONE_VALUE:
        return "none"
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, Closure):
        return f"<fn {val.name}>" if val.name else "<fn>"
    if isinstance(val, EnumInstance):
        if val.type_name == "Option" and val.variant == "some":
            return f"some({recurse(val.fields['value'])})"
        if val.fields:
            inner = ", ".join(f"{k}: {recurse(v)}" for k, v in val.fields.items())
            return f"{val.type_name}.{val.variant} {{ {inner} }}"
        return f"{val.type_name}.{val.variant}"
    if isinstance(val, StructInstance):
        inner = ", ".join(f"{k}: {recurse(v)}" for k, v in val.fields.items())
        return f"{val.type_name} {{ {inner} }}"
    if isinstance(val, Decimal):
        return _format_number(val)
    if isinstance(val, str):
        return val
    return str(val)


# ---------------------------------------------------------------------------
# Frame slot access -- `A` operands are `(depth, slot)` pairs (unchanged
# from the pre-M14 interpreter): walk `static_parent` `depth` times from
# the currently executing frame, then index `.slots[slot]`.
# ---------------------------------------------------------------------------

def _read(frame: Frame, addr):
    depth, slot = addr
    for _ in range(depth):
        frame = frame.static_parent
    return frame.slots[slot]


def _write(frame: Frame, addr, value) -> None:
    depth, slot = addr
    for _ in range(depth):
        frame = frame.static_parent
    frame.slots[slot] = value


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def run_bytes(data: bytes):
    """Decode `data` as a `.mahc` file and run it -- `decode` performs the
    full structural validation of docs/MAHC_FORMAT.md #3/#4 before this
    even gets called."""
    return run_program(decode(data))


def run_program(program: Program):
    linked = _link(program)  # raises MahcFormatError for an unsupported native, before anything runs
    _execute(linked)


def _locate_factory(debug: DebugIndex | None):
    if debug is None:
        return lambda pc, message: message

    def locate(pc: int, message: str) -> str:
        idx = bisect.bisect_right(debug.pcs, pc) - 1
        if idx < 0:
            return message
        file_idx, line, col = debug.runs[idx]
        if line == 0:
            return message
        if file_idx == 0:
            return f"{message} at position #{line}:{col}"
        return f"{message} at position {debug.file_paths[file_idx]}#{line}:{col}"

    return locate


def _execute(linked: LinkedProgram) -> None:
    code = linked.code
    locate = _locate_factory(linked.debug)
    return_register = NONE_VALUE
    timers: list = []  # heap of (wake_time, seq, promise)
    timer_seq = itertools.count()

    # method_table: (type_name, method_name) -> {"inherent": target_or_None,
    # "traits": {trait_name: target}}; target = (fn, is_method), fn either a
    # Closure (a Mah-code method, via `defmethod`) or a plain Python
    # callable (a native system-trait method) -- see docs/MAHC_FORMAT.md
    # #6.7. Every built-in type natively implements Printable.to_string.
    method_table: dict = {}
    for builtin_type in BUILTIN_TYPE_NAMES:
        method_table.setdefault((builtin_type, "to_string"), {"inherent": None, "traits": {}})["traits"][
            "Printable"
        ] = (NativeMethod(0, lambda v: to_str(v)), True)
    # M17: native INHERENT methods (docs/MAHC_FORMAT.md #6.7) -- unlike
    # to_string above, these have no trait behind them at all.
    method_table.setdefault(("String", "len"), {"inherent": None, "traits": {}})["inherent"] = (
        NativeMethod(0, lambda s: Decimal(len(s))),
        True,
    )
    method_table.setdefault(("String", "char_at"), {"inherent": None, "traits": {}})["inherent"] = (
        NativeMethod(1, lambda s, i: _string_char_at(s, i)),
        True,
    )
    method_table.setdefault(("Function", "arity"), {"inherent": None, "traits": {}})["inherent"] = (
        NativeMethod(0, lambda fn: Decimal(fn.param_count)),
        True,
    )

    ctx = NativeContext(to_string=lambda v: to_str(v), schedule_timer=lambda secs, p: schedule_timer(secs, p))

    def enter_closure(task: Task, closure: Closure, arg_values: list) -> None:
        new_frame = Frame(slots=[NONE_VALUE] * closure.slot_count, static_parent=closure.defining_frame)
        for i, v in enumerate(arg_values):
            new_frame.slots[i] = v
        task.return_stack.append((task.pc, task.current_frame))
        task.current_frame = new_frame
        task.pc = closure.code_address

    def find_method(recv, name: str, trait: str | None):
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
                raise MahRuntimeError(
                    f"Method '{name}' on '{tname}' is ambiguous: provided by traits "
                    f"{sorted(entry['traits'])}; call it as 'Trait.{name}(value, ...)'"
                )
        if trait is None and (target is None or not target[1]):
            if isinstance(recv, (StructInstance, EnumInstance)) and name in recv.fields:
                value = recv.fields[name]
                if not isinstance(value, Closure):
                    raise MahRuntimeError(
                        f"Field '{name}' of '{tname}' is not a function (it holds a {type_name_of(value)})"
                    )
                return value, False
        if target is None:
            if trait is not None:
                raise MahRuntimeError(f"'{tname}' does not implement trait '{trait}' (no method '{name}')")
            raise MahRuntimeError(f"'{tname}' has no method '{name}'")
        fn, is_method = target
        if not is_method:
            raise MahRuntimeError(
                f"'{name}' is a static function of '{tname}', not a method; call it as '{tname}.{name}(...)'"
            )
        return fn, True

    def spawn_detached(closure: Closure, arg_values: list):
        new_frame = Frame(slots=[NONE_VALUE] * closure.slot_count, static_parent=closure.defining_frame)
        for i, v in enumerate(arg_values):
            new_frame.slots[i] = v
        promise = PromiseInstance()
        new_task = Task(pc=closure.code_address, current_frame=new_frame, watching_promise=promise)
        drive(new_task)
        return promise

    def invoke_sync(closure: Closure, arg_values: list, label: str):
        call_label = f"'{closure.name}'" if closure.name else "function"
        bound = _bind_params(closure.param_count, closure.params, arg_values, [], call_label)
        frame = Frame(slots=[NONE_VALUE] * closure.slot_count, static_parent=closure.defining_frame)
        for i, v in enumerate(bound):
            frame.slots[i] = v
        sub_task = Task(pc=closure.code_address, current_frame=frame)
        status, value = step_task(sub_task)
        if status == "suspended":
            raise MahRuntimeError(
                f"'{label}' cannot suspend (it awaited a pending Promise) when called implicitly by the runtime"
            )
        return value

    def to_str(val) -> str:
        entry = method_table.get((type_name_of(val), "to_string"))
        target = entry["traits"].get("Printable") if entry else None
        if target is not None and isinstance(target[0], Closure):
            result = invoke_sync(target[0], [val], "to_string")
            if not isinstance(result, str):
                raise MahRuntimeError(
                    f"Printable.to_string for '{type_name_of(val)}' must return a String, got "
                    f"{type_name_of(result)}"
                )
            return result
        return _format_value(val, to_str)

    def schedule_timer(delay_seconds: float, promise: PromiseInstance) -> None:
        heapq.heappush(timers, (time.monotonic() + delay_seconds, next(timer_seq), promise))

    def drain_next_timer() -> bool:
        if not timers:
            return False
        wake_time, _seq, promise = heapq.heappop(timers)
        remaining = wake_time - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        promise.resolve(NONE_VALUE)
        return True

    def _op_add(a, b):
        if isinstance(a, str) or isinstance(b, str):
            return to_str(a) + to_str(b)
        if _is_number(a) and _is_number(b):
            return a + b
        raise MahRuntimeError(f"Cannot apply '+' to {type_name_of(a)} and {type_name_of(b)}")

    def _op_mul(a, b):
        if _is_number(a) and _is_number(b):
            return a * b
        if isinstance(a, str) and _is_number(b) and b == b.to_integral_value():
            n = int(b)
            return a * n if n > 0 else ""
        if isinstance(b, str) and _is_number(a) and a == a.to_integral_value():
            n = int(a)
            return b * n if n > 0 else ""
        raise MahRuntimeError(f"Cannot apply '*' to {type_name_of(a)} and {type_name_of(b)}")

    def _numeric_binop(op: str, a, b):
        if not (_is_number(a) and _is_number(b)):
            raise MahRuntimeError(f"Cannot apply '{_BINOP_SYMBOLS[op]}' to {type_name_of(a)} and {type_name_of(b)}")
        if op == "sub":
            return a - b
        if op == "div":
            if b == 0:
                raise MahRuntimeError("Division by zero")
            return a / b
        if op == "idiv":
            if b == 0:
                raise MahRuntimeError("Division by zero")
            return a // b
        if op == "mod":
            if b == 0:
                raise MahRuntimeError("Division by zero")
            return a % b
        if op == "pow":
            return a**b
        raise AssertionError(op)

    def _compare(op: str, a, b):
        if (_is_number(a) and _is_number(b)) or (isinstance(a, str) and isinstance(b, str)):
            if op == "lt":
                return a < b
            if op == "gt":
                return a > b
            if op == "le":
                return a <= b
            return a >= b  # "ge"
        raise MahRuntimeError(f"Cannot compare {type_name_of(a)} and {type_name_of(b)} with '{_BINOP_SYMBOLS[op]}'")

    def step_task(task: Task):
        """Advance `task` until it finishes (`("done", value)`) or genuinely
        suspends (`("suspended", None)`, having already arranged for
        `drive` to be called again once whatever it awaited resolves).
        Reentrant: `invoke_sync`/`spawn_detached` call this again, for a
        brand new `Task`, while an outer call is still on the Python stack.

        Wraps any exception raised while executing the instruction most
        recently fetched (`current_pc`) into a located `MahRuntimeError`,
        exactly once -- an exception that already passed through some
        OTHER `step_task` call (its own nested step loop, e.g. inside
        `invoke_sync`) is already marked `.located` and passes through
        here untouched, so a nested failure gets exactly one location
        suffix, not one per step loop it unwinds through."""
        nonlocal return_register
        while True:
            current_pc = task.pc
            instr = code[current_pc]
            task.pc = current_pc + 1
            try:
                result = _exec(task, instr)
            except MahRuntimeError as exc:
                if exc.located:
                    raise
                new_exc = MahRuntimeError(locate(current_pc, str(exc)))
                new_exc.located = True
                raise new_exc from None
            except Exception as exc:  # noqa: BLE001 -- wrap any non-Mah Python exception too
                new_exc = MahRuntimeError(locate(current_pc, str(exc)))
                new_exc.located = True
                raise new_exc from None
            if result is not None:
                return result

    def _exec(task: Task, instr):
        frame = task.current_frame
        nonlocal return_register
        match instr:
            case ("halt",):
                return "done", NONE_VALUE
            case ("move", src, dest):
                _write(frame, dest, _read(frame, src))
            case ("loadk", value, dest):
                _write(frame, dest, value)
            case ("jmp", target):
                task.pc = target
            case ("jmpf", cond, target):
                if not truthy(_read(frame, cond)):
                    task.pc = target
            case ("add", a_addr, b_addr, dest):
                _write(frame, dest, _op_add(_read(frame, a_addr), _read(frame, b_addr)))
            case ("mul", a_addr, b_addr, dest):
                _write(frame, dest, _op_mul(_read(frame, a_addr), _read(frame, b_addr)))
            case ("sub" | "div" | "idiv" | "mod" | "pow" as op, a_addr, b_addr, dest):
                _write(frame, dest, _numeric_binop(op, _read(frame, a_addr), _read(frame, b_addr)))
            case ("eq", a_addr, b_addr, dest):
                _write(frame, dest, _values_equal(_read(frame, a_addr), _read(frame, b_addr)))
            case ("neq", a_addr, b_addr, dest):
                _write(frame, dest, not _values_equal(_read(frame, a_addr), _read(frame, b_addr)))
            case ("lt" | "gt" | "le" | "ge" as op, a_addr, b_addr, dest):
                _write(frame, dest, _compare(op, _read(frame, a_addr), _read(frame, b_addr)))
            case ("and", a_addr, b_addr, dest):
                _write(frame, dest, bool(truthy(_read(frame, a_addr)) and truthy(_read(frame, b_addr))))
            case ("or", a_addr, b_addr, dest):
                _write(frame, dest, bool(truthy(_read(frame, a_addr)) or truthy(_read(frame, b_addr))))
            case ("neg", a_addr, dest):
                v = _read(frame, a_addr)
                if not _is_number(v):
                    raise MahRuntimeError(f"Cannot negate {type_name_of(v)}")
                _write(frame, dest, -v)
            case ("not", a_addr, dest):
                _write(frame, dest, bool(not truthy(_read(frame, a_addr))))
            case ("closure", function_info, dest):
                _write(
                    frame,
                    dest,
                    Closure(
                        function_info.entry,
                        frame,
                        function_info.slot_count,
                        function_info.param_count,
                        function_info.name,
                        function_info.params,
                    ),
                )
            case ("call", callee_addr, arg_addrs):
                closure = _read(frame, callee_addr)
                if not isinstance(closure, Closure):
                    raise MahRuntimeError(f"Tried to call a non-function value ({type_name_of(closure)})")
                label = f"'{closure.name}'" if closure.name else "function"
                values = [_read(frame, a) for a in arg_addrs]
                bound = _bind_params(closure.param_count, closure.params, values, [], label)
                enter_closure(task, closure, bound)
            case ("callkw", callee_addr, arg_addrs, kwnames):
                closure = _read(frame, callee_addr)
                if not isinstance(closure, Closure):
                    raise MahRuntimeError(f"Tried to call a non-function value ({type_name_of(closure)})")
                label = f"'{closure.name}'" if closure.name else "function"
                npos = len(arg_addrs) - len(kwnames)
                values = [_read(frame, a) for a in arg_addrs[:npos]]
                kwargs = [(kwnames[j], _read(frame, arg_addrs[npos + j])) for j in range(len(kwnames))]
                bound = _bind_params(closure.param_count, closure.params, values, kwargs, label)
                enter_closure(task, closure, bound)
            case ("jmpset", param_addr, target):
                if _read(frame, param_addr) is not ABSENT:
                    task.pc = target
            case ("ret", value_addr):
                return_register = _read(frame, value_addr)
                if not task.return_stack:
                    return "done", return_register
                task.pc, task.current_frame = task.return_stack.pop()
            case ("retval", dest):
                _write(frame, dest, return_register)
            case ("detach", callee_addr, arg_addrs, dest):
                closure = _read(frame, callee_addr)
                if not isinstance(closure, Closure):
                    raise MahRuntimeError(f"Tried to detach a non-function value ({type_name_of(closure)})")
                label = f"'{closure.name}'" if closure.name else "function"
                values = [_read(frame, a) for a in arg_addrs]
                bound = _bind_params(closure.param_count, closure.params, values, [], label)
                _write(frame, dest, spawn_detached(closure, bound))
            case ("detachkw", callee_addr, arg_addrs, kwnames, dest):
                closure = _read(frame, callee_addr)
                if not isinstance(closure, Closure):
                    raise MahRuntimeError(f"Tried to detach a non-function value ({type_name_of(closure)})")
                label = f"'{closure.name}'" if closure.name else "function"
                npos = len(arg_addrs) - len(kwnames)
                values = [_read(frame, a) for a in arg_addrs[:npos]]
                kwargs = [(kwnames[j], _read(frame, arg_addrs[npos + j])) for j in range(len(kwnames))]
                bound = _bind_params(closure.param_count, closure.params, values, kwargs, label)
                _write(frame, dest, spawn_detached(closure, bound))
            case ("await", promise_addr, dest):
                value = _read(frame, promise_addr)
                if not isinstance(value, PromiseInstance):
                    raise MahRuntimeError(f"'.await' used on a non-Promise value ({type_name_of(value)})")
                if value.variant == "Settled":
                    _write(frame, dest, value.fields["value"])
                else:
                    resume_pc = task.pc

                    def _resume(resolved_value, task=task, dest=dest, resume_pc=resume_pc):
                        _write(task.current_frame, dest, resolved_value)
                        task.pc = resume_pc
                        drive(task)

                    value.callbacks.append(_resume)
                    return "suspended", None
            case ("struct", type_info, values, dest):
                fields = {name: _read(frame, addr) for name, addr in zip(type_info.fields, values)}
                _write(frame, dest, StructInstance(type_info.name, fields))
            case ("enum", type_info, variant_idx, values, dest):
                variant_name, variant_fields = type_info.variants[variant_idx]
                if type_info.name == "Option" and variant_name == "none":
                    _write(frame, dest, NONE_VALUE)
                else:
                    fields = {name: _read(frame, addr) for name, addr in zip(variant_fields, values)}
                    _write(frame, dest, EnumInstance(type_info.name, variant_name, fields))
            case ("getfield", obj_addr, field_name, dest):
                obj = _read(frame, obj_addr)
                if not isinstance(obj, (StructInstance, EnumInstance)):
                    raise MahRuntimeError(f"Tried to access field '{field_name}' on a non-struct value ({type_name_of(obj)})")
                if field_name not in obj.fields:
                    raise MahRuntimeError(f"'{obj.type_name}' has no field '{field_name}'")
                _write(frame, dest, obj.fields[field_name])
            case ("setfield", obj_addr, field_name, src_addr):
                obj = _read(frame, obj_addr)
                if not isinstance(obj, (StructInstance, EnumInstance)):
                    raise MahRuntimeError(f"Tried to access field '{field_name}' on a non-struct value ({type_name_of(obj)})")
                if field_name not in obj.fields:
                    raise MahRuntimeError(f"'{obj.type_name}' has no field '{field_name}'")
                obj.fields[field_name] = _read(frame, src_addr)
            case ("matchstruct", value_addr, type_info, dest):
                val = _read(frame, value_addr)
                _write(frame, dest, isinstance(val, StructInstance) and val.type_name == type_info.name)
            case ("matchenum", value_addr, type_info, variant_idx, dest):
                val = _read(frame, value_addr)
                variant_name = type_info.variants[variant_idx][0]
                _write(
                    frame, dest,
                    isinstance(val, EnumInstance) and val.type_name == type_info.name and val.variant == variant_name,
                )
            case ("matchrange", value_addr, lo_addr, hi_addr, inclusive, dest):
                val = _read(frame, value_addr)
                lo = _read(frame, lo_addr) if lo_addr is not None else None
                hi = _read(frame, hi_addr) if hi_addr is not None else None
                _write(frame, dest, _matchrange(val, lo, hi, inclusive))
            case ("matchfail",):
                raise MahRuntimeError("No pattern in 'match' matched the value")
            case ("deferpush",):
                task.defer_stack.append([])
            case ("deferadd", closure_addr):
                task.defer_stack[-1].append(_read(frame, closure_addr))
            case ("deferpeek", dest):
                _write(frame, dest, bool(task.defer_stack[-1]))
            case ("deferpop", dest):
                _write(frame, dest, task.defer_stack[-1].pop())
            case ("deferscopepop",):
                task.defer_stack.pop()
            case ("defmethod", closure_addr, type_name, trait, name, is_method):
                closure = _read(frame, closure_addr)
                entry = method_table.setdefault((type_name, name), {"inherent": None, "traits": {}})
                if trait is None:
                    entry["inherent"] = (closure, is_method)
                else:
                    entry["traits"][trait] = (closure, is_method)
            case ("callmethod", recv_addr, name, arg_addrs, trait):
                recv = _read(frame, recv_addr)
                fn, include_self = find_method(recv, name, trait)
                values = [_read(frame, a) for a in arg_addrs]
                bound = _bind_method_call(recv, fn, include_self, name, values, [])
                if isinstance(fn, Closure):
                    enter_closure(task, fn, bound)
                else:
                    return_register = _call_native(fn, bound)
            case ("callmethodkw", recv_addr, name, arg_addrs, kwnames, trait):
                recv = _read(frame, recv_addr)
                fn, include_self = find_method(recv, name, trait)
                npos = len(arg_addrs) - len(kwnames)
                values = [_read(frame, a) for a in arg_addrs[:npos]]
                kwargs = [(kwnames[j], _read(frame, arg_addrs[npos + j])) for j in range(len(kwnames))]
                bound = _bind_method_call(recv, fn, include_self, name, values, kwargs)
                if isinstance(fn, Closure):
                    enter_closure(task, fn, bound)
                else:
                    return_register = _call_native(fn, bound)
            case ("detachmethod", recv_addr, name, arg_addrs, trait, dest):
                recv = _read(frame, recv_addr)
                fn, include_self = find_method(recv, name, trait)
                values = [_read(frame, a) for a in arg_addrs]
                bound = _bind_method_call(recv, fn, include_self, name, values, [])
                if isinstance(fn, Closure):
                    promise = spawn_detached(fn, bound)
                else:
                    promise = PromiseInstance()
                    promise.resolve(_call_native(fn, bound))
                _write(frame, dest, promise)
            case ("detachmethodkw", recv_addr, name, arg_addrs, kwnames, trait, dest):
                recv = _read(frame, recv_addr)
                fn, include_self = find_method(recv, name, trait)
                npos = len(arg_addrs) - len(kwnames)
                values = [_read(frame, a) for a in arg_addrs[:npos]]
                kwargs = [(kwnames[j], _read(frame, arg_addrs[npos + j])) for j in range(len(kwnames))]
                bound = _bind_method_call(recv, fn, include_self, name, values, kwargs)
                if isinstance(fn, Closure):
                    promise = spawn_detached(fn, bound)
                else:
                    promise = PromiseInstance()
                    promise.resolve(_call_native(fn, bound))
                _write(frame, dest, promise)
            case ("native", impl, arg_addrs, dest):
                args = [_read(frame, a) for a in arg_addrs]
                result = impl(ctx, args)
                if dest is not None:
                    _write(frame, dest, result)
                else:
                    return_register = result
            case other:
                raise AssertionError(f"invalid linked instruction {other!r}")
        return None

    def drive(task: Task) -> None:
        status, value = step_task(task)
        if status == "done" and task.watching_promise is not None:
            task.watching_promise.resolve(value)

    if not linked.functions:
        raise MahcFormatError("FUNCTIONS section must declare at least one function")
    main_fn = linked.functions[0]
    main_frame = Frame(slots=[NONE_VALUE] * main_fn.slot_count, static_parent=None)
    main_promise = PromiseInstance()
    main_task = Task(pc=main_fn.entry, current_frame=main_frame, watching_promise=main_promise)
    drive(main_task)

    while main_promise.variant != "Settled" or timers:
        if not drain_next_timer():
            break
