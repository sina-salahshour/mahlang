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
    display_name,
    type_name_of,
)


class NativeContext:
    __slots__ = ("_to_string", "_schedule_timer", "_read_line", "_cancel_timer", "_started", "io", "reflect")

    def __init__(self, to_string, schedule_timer, read_line=None, cancel_timer=None, io=None, reflect=None):
        import time

        self._to_string = to_string
        self._schedule_timer = schedule_timer
        self._read_line = read_line
        self._cancel_timer = cancel_timer
        self._started = time.monotonic()
        # M35: code_interpreter's `_IoHub` -- `submit(promise, job)` and the
        # open-file table (mah/fs_natives.py).
        self.io = io
        # M41a: mah/reflect_natives.py's `ReflectData` -- the program's
        # types, functions, META and method table, for `std:reflect`.
        self.reflect = reflect

    def to_string(self, value) -> str:
        return self._to_string(value)

    def schedule_timer(self, seconds: float, promise) -> None:
        self._schedule_timer(seconds, promise)

    def cancel_timer(self, promise) -> bool:
        """M34: drop the pending timer that would settle `promise`."""
        return self._cancel_timer(promise)

    def monotonic_ms(self) -> int:
        """M34: whole milliseconds since this VM started."""
        import time

        return int((time.monotonic() - self._started) * 1000)

    def read_line(self, promise) -> None:
        """M33: settle `promise` with the next line of standard input (or
        fail it with EndOfInput), from the scheduler, later."""
        self._read_line(promise)

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


def _io_read_line(ctx: NativeContext, args) -> object:
    """M33 (1.10): `input(prompt)`'s native. Writes `prompt` (flushed, no
    newline added), then returns a Promise of the next line of standard
    input without its line ending (`\\n` or `\\r\\n`), which fails with an
    `EndOfInput` struct when there are no more lines."""
    (prompt,) = args
    if not isinstance(prompt, str):
        raise MahRuntimeError(f"input: the prompt must be a String, got {type_name_of(prompt)}", kind="TypeMismatch")
    if prompt:
        ctx.stdout.write(prompt)
    ctx.stdout.flush()
    promise = PromiseInstance()
    ctx.read_line(promise)
    return promise


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
    return "None" if value is NONE_VALUE else display_name(type_name_of(value))


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


# -- M32 (1.9): std:regex's matcher ------------------------------------------
#
# std:regex parses every pattern itself (in Mah, so its errors are the same
# on every VM) into a canonical form in which each construct means the same
# thing to Python's `re` and Rust's `regex` crate (docs/MAHC_FORMAT.md
# #4.4). Its syntax is Rust's; `_python_pattern` rewrites the only two
# spellings `re` doesn't share.

_REGEX_CACHE: dict = {}


# `re`'s own `\\B` never matches in an empty string (Rust's does, as there's
# no word boundary there), so it's spelled out as "both sides alike".
_PYTHON_NOT_BOUNDARY = "(?:(?<=\\w)(?=\\w)|(?<!\\w)(?!\\w))"


def _python_pattern(source: str) -> str:
    """`(?-u:\\b)` -> `\\b` (ASCII, from `re.ASCII`), `(?-u:\\B)` -> its
    lookaround definition, and `\\z` -> `\\Z`; everything else is shared."""
    out = []
    i = 0
    while i < len(source):
        if source.startswith("(?-u:\\b)", i):
            out.append("\\b")
            i += 8
            continue
        if source.startswith("(?-u:\\B)", i):
            out.append(_PYTHON_NOT_BOUNDARY)
            i += 8
            continue
        c = source[i]
        if c == "\\":
            nxt = source[i + 1]
            out.append("\\Z" if nxt == "z" else c + nxt)
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _compiled(name: str, source) -> object:
    import re

    if not isinstance(source, str):
        raise MahRuntimeError(f"{name}: expected a String, got {type_name_of(source)}", kind="TypeMismatch")
    compiled = _REGEX_CACHE.get(source)
    if compiled is None:
        try:
            compiled = re.compile(_python_pattern(source), re.ASCII)
        except (re.error, IndexError, RecursionError, OverflowError):
            raise MahRuntimeError(f"{name}: not a canonical pattern", kind="ArgumentError") from None
        if len(_REGEX_CACHE) >= 256:
            _REGEX_CACHE.clear()
        _REGEX_CACHE[source] = compiled
    return compiled


def _spans(m) -> VectorValue:
    """[start0, end0, start1, end1, ...]: every group's span (`none, none`
    for one that didn't take part), in code points."""
    out = []
    for g in range(m.re.groups + 1):
        s, e = m.span(g)
        out.extend((NONE_VALUE, NONE_VALUE) if s < 0 else (Decimal(s), Decimal(e)))
    return VectorValue(out)


def _regex_find(ctx: NativeContext, args) -> object:
    """The first match starting at or after code point `start` (anchors and
    `\\b` still see the text before it), as `_spans`, or `none`."""
    source, text, start = args
    compiled = _compiled("find", source)
    if not isinstance(text, str):
        raise MahRuntimeError(f"find: expected a String, got {type_name_of(text)}", kind="TypeMismatch")
    pos = _whole(start) if isinstance(start, Decimal) and not isinstance(start, bool) else None
    if pos is None or not 0 <= pos <= len(text):
        raise MahRuntimeError("find: start must be a position in the text", kind="ArgumentError")
    m = compiled.search(text, pos)
    return NONE_VALUE if m is None else _spans(m)


def _regex_find_all(ctx: NativeContext, args) -> object:
    """Every match, left to right: after a match ending at `e`, the next
    search starts at `e` -- or at `e + 1` after an empty one."""
    source, text = args
    compiled = _compiled("find_all", source)
    if not isinstance(text, str):
        raise MahRuntimeError(f"find_all: expected a String, got {type_name_of(text)}", kind="TypeMismatch")
    out = []
    pos = 0
    while pos <= len(text):
        m = compiled.search(text, pos)
        if m is None:
            break
        out.append(_spans(m))
        pos = m.end() + 1 if m.end() == m.start() else m.end()
    return VectorValue(out)


# -- M34 (1.11): clocks, timer cancellation, and hand-settled Promises -------


def _time_now_ms(ctx: NativeContext, args) -> object:
    """Wall-clock time: whole milliseconds since 1970-01-01 00:00 UTC."""
    import time

    return Decimal(time.time_ns() // 1_000_000)


def _time_monotonic_ms(ctx: NativeContext, args) -> object:
    """Whole milliseconds since the VM started, never going backwards."""
    return Decimal(ctx.monotonic_ms())


def _promise_arg(name: str, value) -> PromiseInstance:
    if not isinstance(value, PromiseInstance):
        raise MahRuntimeError(f"{name}: expected a Promise, got {type_name_of(value)}", kind="TypeMismatch")
    return value


def _time_cancel(ctx: NativeContext, args) -> object:
    """Cancels the pending `sleep_async` timer behind a Promise, which then
    never settles (and no longer keeps the program running). Whether there
    was such a timer."""
    (promise,) = args
    return ctx.cancel_timer(_promise_arg("cancel", promise))


def _promise_new(ctx: NativeContext, args) -> object:
    """A new pending Promise, settled by `promise.resolve`/`promise.fail`."""
    return PromiseInstance()


def _promise_resolve(ctx: NativeContext, args) -> object:
    """Settles a pending Promise with a value, resuming the tasks awaiting
    it right away; whether it was pending (a settled one is left alone)."""
    promise, value = args
    promise = _promise_arg("resolve", promise)
    if promise.variant != "Pending":
        return False
    promise.resolve(value)
    return True


def _promise_fail(ctx: NativeContext, args) -> object:
    """Fails a pending Promise with an error, which each task awaiting it
    throws; whether it was pending."""
    promise, error = args
    promise = _promise_arg("fail", promise)
    if promise.variant != "Pending":
        return False
    promise.fail(error)
    return True


def _time_sleep_async(ctx: NativeContext, args) -> object:
    (ms,) = args
    promise = PromiseInstance()
    ctx.schedule_timer(float(ms) / 1000.0, promise)
    return promise


# name -> (arity, impl) -- docs/MAHC_FORMAT.md #4.4's version 1.0 table.
NATIVES: dict[str, tuple[int, object]] = {
    "io.print": (1, _io_print),
    "io.write": (1, _io_write),
    "io.input": (0, _io_input),  # pre-1.10 files only; see _io_read_line
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
    # M32 (1.9): std:regex's matcher.
    "regex.find": (3, _regex_find),
    "regex.find_all": (2, _regex_find_all),
    # M33 (1.10): the async `input`.
    "io.read_line": (1, _io_read_line),
    # M34 (1.11): std:time and std:async.
    "time.now_ms": (0, _time_now_ms),
    "time.monotonic_ms": (0, _time_monotonic_ms),
    "time.cancel": (1, _time_cancel),
    "promise.new": (0, _promise_new),
    "promise.resolve": (2, _promise_resolve),
    "promise.fail": (2, _promise_fail),
}

# M35 (1.12): std:fs -- mah/fs_natives.py.
from .fs_natives import NATIVES as _FS_NATIVES  # noqa: E402

NATIVES.update(_FS_NATIVES)

# M36 (1.13): std:process -- mah/process_natives.py.
from .process_natives import NATIVES as _PROCESS_NATIVES  # noqa: E402

NATIVES.update(_PROCESS_NATIVES)

# M41a (1.14): std:reflect -- mah/reflect_natives.py.
from .reflect_natives import NATIVES as _REFLECT_NATIVES  # noqa: E402

NATIVES.update(_REFLECT_NATIVES)

# M37 (1.17): std:bytes -- mah/bytes_methods.py.
from .bytes_methods import NATIVES as _BYTES_NATIVES  # noqa: E402

NATIVES.update(_BYTES_NATIVES)

# M38 (1.18): std:socket -- mah/socket_natives.py.
from .socket_natives import NATIVES as _SOCKET_NATIVES  # noqa: E402

NATIVES.update(_SOCKET_NATIVES)
