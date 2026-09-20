"""Shared runtime value types for Mah's heap-frame calling convention
(see docs/V2_DESIGN.md's M1 milestone) plus M2's `StructInstance` and M3's
`EnumInstance`. Imported by both compiler/codegen.py (only needs
NONE_VALUE, to emit it as a literal) and code_interpreter.py (constructs
Frame/Closure/StructInstance/EnumInstance instances at runtime).
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


# Mah's `none` -- a single shared singleton, not reallocated per use (see
# docs/V2_DESIGN.md's "Built-in `some`/`none`" design note). Reused
# everywhere the value `none` is produced: the implicit value of a function
# that falls off its end without an explicit `return` (M1), and every
# explicit `none` literal (M3) -- compiler/codegen.py special-cases the
# `none` EnumLit to `ld` this exact object rather than emitting a generic
# `enum` construction instruction, precisely so identity checks like
# `val is NONE_VALUE` elsewhere keep working.
NONE_VALUE = EnumInstance("Option", "none", {})
