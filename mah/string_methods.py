"""M29 (docs/STDLIB.md "String methods", docs/MAHC_FORMAT.md #6.7): the
native String methods and `Vector.join`, as the Python VM implements them.
`runtime/src/vm/methods.rs` mirrors every rule and message here exactly, so
each is defined explicitly rather than borrowed from Python's own `str`
methods, whose edge cases differ from Rust's:

- Positions and lengths count Unicode code points.
- "Whitespace" is exactly Unicode's White_Space property (`WHITESPACE`
  below), which is what Rust's `char::is_whitespace` tests.
- A String argument that isn't a String is a `TypeMismatch`; a count that
  isn't a whole number >= 0 is an `ArgumentError`.

Like the VM, this module never imports the compiler.
"""

from __future__ import annotations

import re
from decimal import Decimal

from .runtime_values import NONE_VALUE, EnumInstance, MahRuntimeError, VectorValue, type_name_of

# Unicode's White_Space property.
WHITESPACE = frozenset(
    "\t\n\x0b\x0c\r \x85\xa0 "
    + "".join(chr(c) for c in range(0x2000, 0x200B))
    + "    　"
)


def _string(method: str, what: str, value) -> str:
    if not isinstance(value, str):
        raise MahRuntimeError(
            f"{method}: {what} must be a String, got {type_name_of(value)}", kind="TypeMismatch"
        )
    return value


def _count(method: str, what: str, value) -> int:
    if isinstance(value, bool) or not isinstance(value, Decimal):
        raise MahRuntimeError(
            f"{method}: {what} must be a Number, got {type_name_of(value)}", kind="TypeMismatch"
        )
    if value != value.to_integral_value() or value < 0:
        raise MahRuntimeError(
            f"{method}: {what} must be a whole number of at least 0, got {_format_decimal(value)}",
            kind="ArgumentError",
        )
    return int(value)


def _format_decimal(n: Decimal) -> str:
    # The same text `print` shows (code_interpreter's `_format_number`).
    from .code_interpreter import _format_number

    return _format_number(n)


def _vector(items) -> VectorValue:
    return VectorValue(list(items))


def split(s: str, sep=NONE_VALUE, limit=NONE_VALUE):
    """`split(sep = none, limit = none)`: with no `sep`, the runs of
    non-whitespace; otherwise the pieces between occurrences of `sep`. At
    most `limit` splits: the last piece is then the rest of the String."""
    max_splits = None if limit is NONE_VALUE else _count("split", "limit", limit)
    if sep is NONE_VALUE:
        parts = []
        i, n = 0, len(s)
        while True:
            while i < n and s[i] in WHITESPACE:
                i += 1
            if i >= n:
                break
            if max_splits is not None and len(parts) == max_splits:
                parts.append(s[i:])
                break
            j = i
            while j < n and s[j] not in WHITESPACE:
                j += 1
            parts.append(s[i:j])
            i = j
        return _vector(parts)
    sep = _string("split", "sep", sep)
    if sep == "":
        raise MahRuntimeError("split: sep can't be empty", kind="ArgumentError")
    return _vector(s.split(sep, -1 if max_splits is None else max_splits))


def trim(s: str) -> str:
    return s.strip("".join(WHITESPACE))


def trim_start(s: str) -> str:
    return s.lstrip("".join(WHITESPACE))


def trim_end(s: str) -> str:
    return s.rstrip("".join(WHITESPACE))


def _padding(method: str, s: str, width, fill) -> str:
    width = _count(method, "width", width)
    fill = _string(method, "fill", fill)
    if fill == "":
        raise MahRuntimeError(f"{method}: fill can't be empty", kind="ArgumentError")
    need = width - len(s)
    if need <= 0:
        return ""
    return (fill * (need // len(fill) + 1))[:need]


def pad_start(s: str, width, fill=" ") -> str:
    """`s` with `fill` repeated in front until it's `width` long (the last
    repetition cut short to fit exactly); unchanged if already that long."""
    return _padding("pad_start", s, width, fill) + s


def pad_end(s: str, width, fill=" ") -> str:
    return s + _padding("pad_end", s, width, fill)


def replace(s: str, old, new) -> str:
    """The first occurrence of `old` replaced (an empty `old` matches at
    the start)."""
    return s.replace(_string("replace", "from", old), _string("replace", "to", new), 1)


def replace_all(s: str, old, new) -> str:
    """Every occurrence of `old` replaced (an empty `old` matches between
    every two code points and at both ends)."""
    return s.replace(_string("replace_all", "from", old), _string("replace_all", "to", new))


def starts_with(s: str, prefix) -> bool:
    return s.startswith(_string("starts_with", "prefix", prefix))


def ends_with(s: str, suffix) -> bool:
    return s.endswith(_string("ends_with", "suffix", suffix))


def contains(s: str, part) -> bool:
    return _string("contains", "part", part) in s


def index_of(s: str, part):
    """`some(i)`, the code-point position of the first occurrence of
    `part` (`some(0)` for an empty `part`), or `none`."""
    i = s.find(_string("index_of", "part", part))
    if i < 0:
        return NONE_VALUE
    return EnumInstance("Option", "some", {"value": Decimal(i)})


def repeat(s: str, count) -> str:
    return s * _count("repeat", "count", count)


def to_upper(s: str) -> str:
    return s.upper()


def to_lower(s: str) -> str:
    return s.lower()


def lines(s: str) -> VectorValue:
    """The lines of `s`: split at each `\\n`, dropping a `\\r` right
    before it; a final line ending doesn't start another line, and "" has
    no lines. (A `\\r` not followed by `\\n` is kept.)"""
    parts = s.split("\n")
    terminated = len(parts) - 1  # parts[:terminated] each ended with "\n"
    out = [p[:-1] if i < terminated and p.endswith("\r") else p for i, p in enumerate(parts)]
    if parts[-1] == "":
        out.pop()
    return _vector(out)


# What `parse_number`/`to_number` accept, after trimming whitespace: an
# optional sign, digits with an optional fraction (or just a fraction), and
# an optional exponent of at most 5 digits -- ASCII only.
_NUMBER_RE = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]{1,5})?")


def parse_number(s: str):
    """The Number `s` spells (surrounding whitespace allowed), or `none`.
    The prelude's `to_number` throws `NumberParseError` on `none`."""
    text = trim(s)
    if _NUMBER_RE.fullmatch(text) is None:
        return NONE_VALUE
    return Decimal(text)


def join(items: VectorValue, sep, to_str) -> str:
    """`Vector.join(sep = "")`: every item's `to_string`, with `sep` between."""
    sep = _string("join", "sep", sep)
    return sep.join(to_str(item) for item in items.items)
