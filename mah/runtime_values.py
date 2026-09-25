"""Shared runtime value types for Mah's heap-frame calling convention
(see docs/V2_DESIGN.md's M1 milestone) plus M2's `StructInstance` and M3's
`EnumInstance`. Imported by both compiler/codegen.py (only needs
NONE_VALUE, to emit it as a literal) and code_interpreter.py (constructs
Frame/Closure/StructInstance/EnumInstance instances at runtime).

M10 adds `PromiseInstance` (an `EnumInstance` subclass -- see its own
docstring) and `Task` for async (`detach`/`.await`/`sleep_async`, see
docs/V2_DESIGN.md's M10 milestone) -- both are only ever constructed/
consumed by code_interpreter.py.

M12 adds `BUILTIN_TYPE_NAMES`/`SYSTEM_TRAITS`/`type_name_of` for traits/
`impl`/method calls (see compiler/resolve.py's module docstring for the
full design): `BUILTIN_TYPE_NAMES` is the fixed set of type names built-in
values respond to for `impl Trait for BuiltinType`/dispatch purposes;
`SYSTEM_TRAITS` lists this milestone's one system trait (`Printable`,
implemented natively by every built-in type -- see
code_interpreter.py's `NATIVE_TRAIT_METHODS`); `type_name_of` is the single
source of truth for "what type does method/trait dispatch see this runtime
value as," used by both compiler/resolve.py (`Self` substitution needs
nothing from it, but the interpreter's `callmethod`/`to_str` do) and
code_interpreter.py.
"""

from decimal import Decimal

# M12: names of the built-in types, as seen by `impl` and method dispatch.
BUILTIN_TYPE_NAMES = ("Number", "String", "Bool", "Function", "Option", "Promise", "Vector", "Map")

# M12: system traits -- trait name -> {method name -> parameter names}.
# User types opt in with a normal `impl`. M19 adds `Index` (`x[k]`) and
# `IndexAssign` (`x[k] = v`).
SYSTEM_TRAITS = {
    "Printable": {"to_string": ["self"]},
    "Index": {"index": ["self", "key"]},
    "IndexAssign": {"index_assign": ["self", "key", "value"]},
}

# M19: which built-in types natively implement each system trait (the VM's
# initial method table, docs/MAHC_FORMAT.md #6.7). Every built-in type is
# Printable; only Vector and Map are indexable.
SYSTEM_TRAIT_NATIVE_TYPES = {
    "Printable": BUILTIN_TYPE_NAMES,
    "Index": ("Vector", "Map"),
    "IndexAssign": ("Vector", "Map"),
}


class Frame:
    __slots__ = ("slots", "static_parent")

    def __init__(self, slots, static_parent):
        self.slots = slots
        self.static_parent = static_parent


class Closure:
    __slots__ = ("code_address", "defining_frame", "slot_count", "param_count", "name", "params")

    def __init__(self, code_address, defining_frame, slot_count, param_count, name=None, params=None):
        self.code_address = code_address
        self.defining_frame = defining_frame
        self.slot_count = slot_count
        self.param_count = param_count
        self.name = name
        # M16: parallel to param slots 0..param_count-1 -- list[tuple[str,
        # bool]] (parameter name, has_default), or `None` for a function
        # from a 1.0 file (no PARAMS section: every parameter unnamed and
        # required) -- see docs/MAHC_FORMAT.md #4.5a/#6.1 and
        # code_interpreter.py's `_bind_params`.
        self.params = params


class StructInstance:
    """A heap object for a `struct` value (see docs/V2_DESIGN.md's M2
    milestone) -- reference semantics, same as Frame/Closure: a Mah struct
    variable holds a reference to this object, never a copy."""

    __slots__ = ("type_name", "fields")

    def __init__(self, type_name, fields):
        self.type_name = type_name
        self.fields = fields  # dict[str, Any]


class VectorValue:
    """M19: a Vector -- a growable, zero-indexed list of values, mutable and
    by reference like a struct."""

    __slots__ = ("items",)

    def __init__(self, items):
        self.items = items  # list[Any]


class MapValue:
    """M19: a Map -- String/Number/Bool keys to values, in insertion order,
    mutable and by reference. `entries` is keyed by `map_key(key)` (which
    keeps `true` and `1` apart, unlike a plain Python dict) and holds the
    original `(key, value)`."""

    __slots__ = ("entries",)

    def __init__(self, entries=None):
        self.entries = entries if entries is not None else {}  # dict[tuple, tuple[Any, Any]]


def map_key(key):
    """M19: the dict key a Mah Map stores `key` under, or `None` if `key`
    can't be a Map key (only Strings, Numbers, and Bools can)."""
    if isinstance(key, bool):
        return ("Bool", key)
    if isinstance(key, (int, float, Decimal)):
        return ("Number", key)
    if isinstance(key, str):
        return ("String", key)
    return None


class EnumInstance:
    """A heap object for an `enum` value (see docs/V2_DESIGN.md's M3
    milestone) -- reference semantics, same as Frame/Closure/StructInstance:
    a Mah enum variable holds a reference to this object, never a copy.
    Also backs the built-in `Option` type (`none`/`some(x)`), unified with
    the same representation rather than a bespoke class -- see NONE_VALUE
    below."""

    __slots__ = ("type_name", "variant", "fields")

    def __init__(self, type_name, variant, fields):
        self.type_name = type_name
        self.variant = variant
        self.fields = fields  # dict[str, Any]

    def __repr__(self):
        return f"EnumInstance({self.type_name!r}, {self.variant!r}, {self.fields!r})"

    def __bool__(self):
        # Only Option.none is falsy -- every other enum instance (including
        # some(x) for ANY x, even some(false) or some(0)) is truthy. This
        # matches every other language with an Option/Maybe type: presence
        # (Some/some) is always truthy regardless of the wrapped value. It
        # also preserves M1's pre-existing rule that `none` is falsy
        # (needed so existing programs' `if is_prime(x) { ... }`-style
        # checks against a bare/implicit `return` keep behaving as they did
        # before M1 -- see that milestone's note in docs/V2_DESIGN.md).
        return not (self.type_name == "Option" and self.variant == "none")


class PromiseInstance(EnumInstance):
    """Async: a `Promise`, represented as a real built-in Mah *enum* --
    `Promise.Pending` (unit) or `Promise.Settled { value }` (one field) --
    exactly the same "built-in enum backed by EnumInstance" pattern
    `Option`/`none`/`some(x)` already established (see `NONE_VALUE`
    below), rather than an opaque host-only type. This means a `Promise`
    prints, pattern-matches, and hovers through the exact same generic
    machinery every other enum already gets, for free -- see
    docs/NEXT_PHASES.md's "Async" section (M10).

    Only two variants: a Promise never rejects -- a scheduled operation
    that fails just raises a fatal Mah runtime error immediately, the same
    as any other error today, rather than needing a third "Rejected"
    variant here.

    `callbacks` is the one piece that ISN'T a normal enum field: purely
    interpreter-internal scheduling bookkeeping (never visible in
    `.fields`, never touched by ordinary Mah code), holding the callbacks
    to run -- synchronously -- once this promise settles. Resolving a
    Promise mutates `variant`/`fields` in place, exactly like any other
    enum's fields can already be mutated via `setfield` -- every reference
    to this same heap object (Mah's usual reference semantics) sees the
    transition from Pending to Settled."""

    __slots__ = ("callbacks",)

    def __init__(self):
        super().__init__(type_name="Promise", variant="Pending", fields={})
        self.callbacks = []  # list[Callable[[Any], None]], run synchronously on resolve

    def resolve(self, value):
        if self.variant == "Settled":
            return
        self.variant = "Settled"
        self.fields = {"value": value}
        callbacks, self.callbacks = self.callbacks, []
        for callback in callbacks:
            callback(value)


class Task:
    """Async: one independent (pc, frame, return_stack, defer_stack)
    stepping context -- the main program is task 0, `detach` creates one
    more per detached call. See docs/NEXT_PHASES.md's "Async" section
    (M10) for why M9's `defer_stack` -- previously a single list shared by
    the whole program -- has to move onto each Task (a suspended task's
    own pending defers must never leak into whichever task runs next),
    while a single shared `return_register` in code_interpreter.py stays
    correct (nothing can ever switch tasks between a `ret` and its
    immediately-following `retval`, so there's nothing to isolate there).
    `watching_promise` is the Promise (if any) that should be resolved
    once this task truly finishes -- set by `detach` for the task it
    creates; `None` for the main program (task 0), which nothing is
    watching."""

    __slots__ = ("pc", "current_frame", "return_stack", "defer_stack", "watching_promise")

    def __init__(self, pc, current_frame, watching_promise=None):
        self.pc = pc
        self.current_frame = current_frame
        self.return_stack = []   # list[tuple[int, Frame]]
        self.defer_stack = []    # list[list[Closure]]
        self.watching_promise = watching_promise


# Mah's `none` -- a single shared singleton, not reallocated per use (see
# docs/V2_DESIGN.md's "Built-in `some`/`none`" design note). Reused
# everywhere the value `none` is produced: the implicit value of a function
# that falls off its end without an explicit `return` (M1), and every
# explicit `none` literal (M3) -- compiler/codegen.py special-cases the
# `none` EnumLit to `ld` this exact object rather than emitting a generic
# `enum` construction instruction, precisely so identity checks like
# `val is NONE_VALUE` elsewhere keep working.
NONE_VALUE = EnumInstance("Option", "none", {})


class MahRuntimeError(Exception):
    """A `.mahc` program's own runtime error (docs/MAHC_FORMAT.md #6.8) --
    distinct from `mah.bytecode.format.MahcFormatError` (a malformed FILE)
    and from an ordinary Python exception (a VM-internal bug or a Python
    stdlib error, e.g. `decimal.InvalidOperation`, that the VM's step loop
    catches and wraps the same way -- see `mah/code_interpreter.py`).

    `located` (set to `True` once the step loop has appended a `at position
    ...` suffix, or decided no location is available) exists purely so an
    error raised deep inside a nested task (`invoke_sync`/`detach`'s own,
    independent step loop) gets exactly one location suffix, not one per
    step loop it passes through on its way back up."""

    located: bool = False


def type_name_of(value) -> str:
    """M12: the runtime type name `impl`/method dispatch sees `value` as --
    see this module's docstring and docs/V2_DESIGN.md's M12 milestone.
    `bool` is checked before `int`/`float`/`Decimal` since Python's `bool`
    is itself a subclass of `int`. A `StructInstance`/`EnumInstance`
    (including `PromiseInstance` and `NONE_VALUE`, both `EnumInstance`s)
    reports its own `.type_name`. Anything else (e.g. an uninitialized
    frame slot's Python `None`) reports `"Unknown"` rather than raising --
    dispatch on such a value then just fails its own "no such method"
    lookup cleanly, instead of `type_name_of` itself blowing up first."""
    if isinstance(value, bool):
        return "Bool"
    if isinstance(value, (int, float, Decimal)):
        return "Number"
    if isinstance(value, str):
        return "String"
    if isinstance(value, Closure):
        return "Function"
    if isinstance(value, (StructInstance, EnumInstance)):
        return value.type_name
    if isinstance(value, VectorValue):
        return "Vector"
    if isinstance(value, MapValue):
        return "Map"
    return "Unknown"
