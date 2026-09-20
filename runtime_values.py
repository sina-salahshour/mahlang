"""Shared runtime value types for Mah's heap-frame calling convention
(see docs/V2_DESIGN.md's M1 milestone) plus M2's `StructInstance`.
Imported by both compiler/codegen.py (only needs NONE_VALUE, to emit it as
a literal) and code_interpreter.py (constructs Frame/Closure/StructInstance
instances at runtime).
"""


class Frame:
    __slots__ = ("slots", "static_parent")

    def __init__(self, slots, static_parent):
        self.slots = slots
        self.static_parent = static_parent


class Closure:
    __slots__ = ("code_address", "defining_frame", "slot_count", "param_count", "name")

    def __init__(self, code_address, defining_frame, slot_count, param_count, name=None):
        self.code_address = code_address
        self.defining_frame = defining_frame
        self.slot_count = slot_count
        self.param_count = param_count
        self.name = name


class StructInstance:
    """A heap object for a `struct` value (see docs/V2_DESIGN.md's M2
    milestone) -- reference semantics, same as Frame/Closure: a Mah struct
    variable holds a reference to this object, never a copy."""

    __slots__ = ("type_name", "fields")

    def __init__(self, type_name, fields):
        self.type_name = type_name
        self.fields = fields  # dict[str, Any]


class _NoneValue:
    """Singleton for Mah's `none` -- a minimal stand-in ahead of the full
    Option/some/none enum (a later milestone). Only used as the implicit
    value of a function that falls off its end without an explicit
    `return`. Do not add `some`/`none` syntax anywhere else -- out of
    scope for this milestone."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self):
        return "none"

    def __bool__(self):
        # `none` is falsy -- needed so existing programs' `if is_prime(x)
        # { ... }`-style checks against a bare/implicit `return` (which is
        # now `none` rather than M0's `Decimal(0)`) keep behaving as they
        # did before M1. Not mandated by any `none`/`some` semantics yet
        # (those land in a later milestone) -- just the natural, minimal
        # choice so a value that means "nothing" reads as false.
        return False


NONE_VALUE = _NoneValue()
