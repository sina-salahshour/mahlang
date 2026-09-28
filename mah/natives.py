"""The native function registry -- docs/MAHC_FORMAT.md #4.4. This is the
module future natives (`fs.*`, `net.*`, `string.*`, `os.*`, ...) get added
to: a new native is one new `NATIVES` entry (a name, its declared arity,
and an `impl(ctx, args) -> value` callable), nothing else -- no opcode or
section changes (see that doc's "Extendable" design goal).

`ctx` is a small object `mah/code_interpreter.py` passes to every native
call, giving an implementation everything it might need without importing
the interpreter module directly: `to_string(value)` (the `Printable`
system trait, §6.6), `schedule_timer(seconds, promise)` (the async
scheduler, §6.4), and `stdout`/`stdin` (read fresh each time, so a test's
`contextlib.redirect_stdout`/swapped `sys.stdin` -- already in place by the
time a program runs -- is honored).
"""

from __future__ import annotations

import math
import sys
from decimal import Decimal

from .runtime_values import (
    NONE_VALUE,
    EnumInstance,
    MahRuntimeError,
    MapValue,
    PromiseInstance,
    StructInstance,
    VectorValue,
    map_key,
    type_name_of,
)


class NativeContext:
    __slots__ = ("_to_string", "_schedule_timer")

    def __init__(self, to_string, schedule_timer):
        self._to_string = to_string
        self._schedule_timer = schedule_timer

    def to_string(self, value) -> str:
        return self._to_string(value)

    def schedule_timer(self, seconds: float, promise) -> None:
        self._schedule_timer(seconds, promise)

    @property
    def stdout(self):
        return sys.stdout

    @property
    def stdin(self):
        return sys.stdin


def _io_print(ctx: NativeContext, args) -> object:
    (value,) = args
    ctx.stdout.write(ctx.to_string(value) + "\n")
    return NONE_VALUE


def _io_write(ctx: NativeContext, args) -> object:
    """M16 (1.1): like `io.print`, but with no trailing newline -- the
    building block `print(sep:, end:)` compiles to (docs/MAHC_FORMAT.md
    #4.4)."""
    (value,) = args
    ctx.stdout.write(ctx.to_string(value))
    return NONE_VALUE


def _io_input(ctx: NativeContext, args) -> object:
    raw = ""
    started = False
    while True:
        ch = ctx.stdin.read(1)
        if ch == "":
            if not started:
                raise MahRuntimeError("input: end of input", kind="InputError")
            break
        if ch.isdigit():
            started = True
            raw += ch
        elif started:
            break
        # else: skip characters until the first digit, per docs/MAHC_FORMAT.md #4.4
    return Decimal(raw)


def _math_sin(ctx: NativeContext, args) -> object:
    (value,) = args
    return Decimal(repr(math.sin(float(value))))


def _math_cos(ctx: NativeContext, args) -> object:
    (value,) = args
    return Decimal(repr(math.cos(float(value))))


def _float_math(name: str, fn):
    """M27 (1.5): a `math.*` native computed in IEEE-754 double precision,
    exactly like `math.sin`/`math.cos`: the arguments are converted to
    doubles, the result back to a Number via its shortest round-trip
    decimal text. A non-finite result (or Python's own domain/overflow
    error) is a `RuntimeError.ArgumentError` -- docs/MAHC_FORMAT.md #4.4."""
    short = name.split(".", 1)[1]

    def impl(ctx: NativeContext, args) -> object:
        floats = []
        for value in args:
            if not isinstance(value, Decimal) or isinstance(value, bool):
                raise MahRuntimeError(
                    f"{short}: expected a Number, got {type_name_of(value)}", kind="TypeMismatch"
                )
            floats.append(float(value))
        try:
            result = fn(*floats)
        except (ValueError, OverflowError):
            result = math.nan
        if math.isnan(result) or math.isinf(result):
            raise MahRuntimeError(f"{short}: argument out of range", kind="ArgumentError")
        return Decimal(repr(result))

    return impl


# -- M30 (1.7): reflection and characters, for std:json/std:csv ------------


def _value_type_name(ctx: NativeContext, args) -> object:
    """The value's runtime type name, with `none` as "None" (not "Option")."""
    (value,) = args
    return "None" if value is NONE_VALUE else type_name_of(value)


def _is_record(value) -> bool:
    # A struct, or an enum value other than none/Promise (whose fields are
    # VM-internal).
    if isinstance(value, StructInstance):
        return True
    return isinstance(value, EnumInstance) and value is not NONE_VALUE and not isinstance(value, PromiseInstance)


def _value_fields(ctx: NativeContext, args) -> object:
    """A struct's (or enum value's) fields as a new Map, in declaration
    order; `none` for anything else."""
    (value,) = args
    if not _is_record(value):
        return NONE_VALUE
    return MapValue({map_key(name): (name, field) for name, field in value.fields.items()})


def _value_variant(ctx: NativeContext, args) -> object:
    """An enum value's variant name; `none` for anything else (none too)."""
    (value,) = args
    if isinstance(value, EnumInstance) and _is_record(value):
        return value.variant
    return NONE_VALUE


def _string_arg(name: str, value) -> str:
    if not isinstance(value, str):
        raise MahRuntimeError(f"{name}: expected a String, got {type_name_of(value)}", kind="TypeMismatch")
    return value


def _string_chars(ctx: NativeContext, args) -> object:
    """The String's characters (code points), as a Vector of Strings."""
    (value,) = args
    return VectorValue(list(_string_arg("chars", value)))


def _string_code_point(ctx: NativeContext, args) -> object:
    (value,) = args
    text = _string_arg("code_point", value)
    if len(text) != 1:
        raise MahRuntimeError(f"code_point: expected one character, got {len(text)}", kind="ArgumentError")
    return Decimal(ord(text))


def _string_from_code_point(ctx: NativeContext, args) -> object:
    (value,) = args
    if isinstance(value, bool) or not isinstance(value, Decimal):
        raise MahRuntimeError(
            f"from_code_point: expected a Number, got {type_name_of(value)}", kind="TypeMismatch"
        )
    if value != value.to_integral_value() or not (0 <= value <= 0x10FFFF) or 0xD800 <= value <= 0xDFFF:
        raise MahRuntimeError("from_code_point: not a Unicode scalar value", kind="ArgumentError")
    return chr(int(value))


# -- M31 (1.8): the shared generator behind std:random ----------------------
#
# xoshiro256** (Blackman & Vigna), seeded through splitmix64, over a state
# of four 64-bit words kept in a Mah Vector of four whole Numbers -- so a
# seeded sequence is the same on every VM (runtime/src/vm/natives.rs ports
# this exactly).

_MASK64 = (1 << 64) - 1


def _rotl(x: int, k: int) -> int:
    return ((x << k) | (x >> (64 - k))) & _MASK64


def _splitmix64(x: int) -> tuple[int, int]:
    """(the next splitmix64 state, its output)."""
    x = (x + 0x9E3779B97F4A7C15) & _MASK64
    z = x
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _MASK64
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _MASK64
    return x, z ^ (z >> 31)


def _state_from_seed(seed: int) -> VectorValue:
    words = []
    x = seed & _MASK64
    for _ in range(4):
        x, out = _splitmix64(x)
        words.append(Decimal(out))
    return VectorValue(words)


def _whole(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, Decimal) or not value.is_finite():
        return None
    if value != value.to_integral_value():
        return None
    return int(value)


def _random_seed(ctx: NativeContext, args) -> object:
    """A new state from `seed`, a whole Number with |seed| < 2**64 (a
    negative one taken modulo 2**64)."""
    (seed,) = args
    if isinstance(seed, bool) or not isinstance(seed, Decimal):
        raise MahRuntimeError(f"seed: expected a Number, got {type_name_of(seed)}", kind="TypeMismatch")
    n = _whole(seed)
    if n is None or abs(n) >= 1 << 64:
        raise MahRuntimeError("seed: expected a whole number smaller than 2^64 in size", kind="ArgumentError")
    return _state_from_seed(n)


def _random_fresh(ctx: NativeContext, args) -> object:
    """A new state seeded from the operating system's randomness."""
    import os

    return _state_from_seed(int.from_bytes(os.urandom(8), "little"))


def _read_state(name: str, state) -> list[int]:
    if isinstance(state, VectorValue) and len(state.items) == 4:
        words = [_whole(w) for w in state.items]
        if all(w is not None and 0 <= w <= _MASK64 for w in words) and any(words):
            return words
    raise MahRuntimeError(f"{name}: not a generator state", kind="ArgumentError")


def _next_word(state: VectorValue, s: list[int]) -> int:
    """xoshiro256**'s next output; advances `s` and writes it back to `state`."""
    result = (_rotl((s[1] * 5) & _MASK64, 7) * 9) & _MASK64
    t = (s[1] << 17) & _MASK64
    s[2] ^= s[0]
    s[3] ^= s[1]
    s[1] ^= s[2]
    s[0] ^= s[3]
    s[2] ^= t
    s[3] = _rotl(s[3], 45)
    state.items[:] = [Decimal(w) for w in s]
    return result


def _random_next(ctx: NativeContext, args) -> object:
    """The next 64-bit output, a whole Number in [0, 2**64)."""
    (state,) = args
    return Decimal(_next_word(state, _read_state("next", state)))


def _random_below(ctx: NativeContext, args) -> object:
    """A uniform whole Number in [0, n), for a whole `n` in [1, 2**64]:
    outputs at or above the largest multiple of `n` are rejected."""
    state, n = args
    s = _read_state("below", state)
    if isinstance(n, bool) or not isinstance(n, Decimal):
        raise MahRuntimeError(f"below: expected a Number, got {type_name_of(n)}", kind="TypeMismatch")
    bound = _whole(n)
    if bound is None or not 1 <= bound <= 1 << 64:
        raise MahRuntimeError("below: expected a whole number from 1 to 2^64", kind="ArgumentError")
    limit = (1 << 64) - (1 << 64) % bound
    while True:
        x = _next_word(state, s)
        if x < limit:
            return Decimal(x % bound)


def _time_sleep_async(ctx: NativeContext, args) -> object:
    (ms,) = args
    promise = PromiseInstance()
    ctx.schedule_timer(float(ms) / 1000.0, promise)
    return promise


# name -> (arity, impl) -- docs/MAHC_FORMAT.md #4.4's version 1.0 table.
NATIVES: dict[str, tuple[int, object]] = {
    "io.print": (1, _io_print),
    "io.write": (1, _io_write),
    "io.input": (0, _io_input),
    "math.sin": (1, _math_sin),
    "math.cos": (1, _math_cos),
    "time.sleep_async": (1, _time_sleep_async),
    # M27 (1.5): the transcendental half of `std:math` (mah/std/math.mh).
    "math.tan": (1, _float_math("math.tan", math.tan)),
    "math.asin": (1, _float_math("math.asin", math.asin)),
    "math.acos": (1, _float_math("math.acos", math.acos)),
    "math.atan": (1, _float_math("math.atan", math.atan)),
    "math.atan2": (2, _float_math("math.atan2", math.atan2)),
    "math.exp": (1, _float_math("math.exp", math.exp)),
    "math.log": (1, _float_math("math.log", math.log)),
    "math.log10": (1, _float_math("math.log10", math.log10)),
    # M30 (1.7): for std:json/std:csv.
    "value.type_name": (1, _value_type_name),
    "value.fields": (1, _value_fields),
    "value.variant": (1, _value_variant),
    "string.chars": (1, _string_chars),
    "string.code_point": (1, _string_code_point),
    "string.from_code_point": (1, _string_from_code_point),
    # M31 (1.8): the shared generator, for std:random.
    "random.seed": (1, _random_seed),
    "random.fresh": (0, _random_fresh),
    "random.next": (1, _random_next),
    "random.below": (2, _random_below),
}
