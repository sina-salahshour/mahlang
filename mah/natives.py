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

from .runtime_values import MahRuntimeError, NONE_VALUE, PromiseInstance


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
                raise MahRuntimeError("input: end of input")
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
}
