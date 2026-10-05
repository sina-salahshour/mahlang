"""M37 (docs/STDLIB.md "Bytes", docs/MAHC_FORMAT.md #4.4/#6.7/#6.9): the
`Bytes` value's native methods, its `Index`/`IndexAssign`, `String.to_bytes`,
and the `bytes.*` natives behind std:bytes, as the Python VM implements them.
`runtime/src/vm/bytes.rs` mirrors every rule and message here exactly.

- A byte is a whole Number from 0 to 255. Anything else given as one is an
  `ArgumentError` (a non-Number, a `TypeMismatch`).
- Positions follow the Vector rules (`code_interpreter._seq_position`,
  `_slice_bounds`): negative indices count from the end, a read outside the
  Bytes is `none`, a slice is always a new Bytes.
- Hex is two digits per byte, lowercase when encoding, either case when
  decoding. Base64 is the standard alphabet (RFC 4648 section 4) with `=`
  padding, required when decoding; no whitespace is allowed, and the unused
  bits of the last group are ignored.
- Decoding gives `some(bytes)`, or `none` for text that isn't valid
  hex/base64 (std:bytes turns that into a `BytesError`); `to_text` gives `none` for invalid UTF-8.

Like the VM, this module never imports the compiler.
"""

from __future__ import annotations

from decimal import Decimal

from .runtime_values import NONE_VALUE, BytesValue, EnumInstance, MahRuntimeError, VectorValue, type_name_of

_B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
_B64_INDEX = {c: i for i, c in enumerate(_B64)}
_HEX_DIGITS = "0123456789abcdefABCDEF"


def _format_decimal(n: Decimal) -> str:
    # The same text `print` shows (code_interpreter's `_format_number`).
    from .code_interpreter import _format_number

    return _format_number(n)


def _is_number(v) -> bool:
    return type_name_of(v) == "Number"


def byte_arg(what: str, value) -> int:
    """`value` as a byte; `what` starts the error message ("push: the
    value", "Bytes item", ...)."""
    if not _is_number(value):
        raise MahRuntimeError(f"{what} must be a Number, got {type_name_of(value)}", kind="TypeMismatch")
    if value != value.to_integral_value() or value < 0 or value > 255:
        raise MahRuntimeError(
            f"{what} must be a whole number from 0 to 255, got {_format_decimal(value)}", kind="ArgumentError"
        )
    return int(value)


def _bytes_arg(what: str, value) -> BytesValue:
    if not isinstance(value, BytesValue):
        raise MahRuntimeError(f"{what} must be Bytes, got {type_name_of(value)}", kind="TypeMismatch")
    return value


def _string_arg(what: str, value) -> str:
    if not isinstance(value, str):
        raise MahRuntimeError(f"{what} must be a String, got {type_name_of(value)}", kind="TypeMismatch")
    return value


def _some(value) -> EnumInstance:
    return EnumInstance("Option", "some", {"value": value})


# -- formatting ------------------------------------------------------------------


def to_string(b: BytesValue) -> str:
    """`Bytes[68 69]`: each byte as two lowercase hex digits."""
    return "Bytes[" + " ".join(f"{x:02x}" for x in b.data) + "]"


def equal(a: BytesValue, b: BytesValue) -> bool:
    return a.data == b.data


def concat(a: BytesValue, b: BytesValue) -> BytesValue:
    """`a + b`: a new Bytes."""
    return BytesValue(a.data + b.data)


# -- Index / IndexAssign -----------------------------------------------------------


def index(b: BytesValue, i):
    from .code_interpreter import _is_range, _seq_position, _slice_bounds

    if _is_range(i):
        start, stop = _slice_bounds(len(b.data), i, "Bytes")
        return BytesValue(b.data[start:stop])
    pos = _seq_position(len(b.data), i, "Bytes")
    return NONE_VALUE if pos is None else Decimal(b.data[pos])


def index_assign(b: BytesValue, i, value):
    from .code_interpreter import _is_range, _seq_position

    if _is_range(i):
        raise MahRuntimeError(
            "Can't assign to a Bytes slice (b[a..b] = ...); assign items one at a time", kind="TypeMismatch"
        )
    pos = _seq_position(len(b.data), i, "Bytes")
    if pos is None:
        raise MahRuntimeError(
            f"Bytes index {_format_decimal(i)} is out of range for Bytes of length {len(b.data)} "
            f"(use push to add items)",
            kind="IndexOutOfRange",
        )
    b.data[pos] = byte_arg("Bytes item", value)
    return NONE_VALUE


# -- methods -------------------------------------------------------------------------


def length(b: BytesValue) -> Decimal:
    return Decimal(len(b.data))


def push(b: BytesValue, value):
    b.data.append(byte_arg("push: the value", value))
    return NONE_VALUE


def pop(b: BytesValue):
    return Decimal(b.data.pop()) if b.data else NONE_VALUE


def extend(b: BytesValue, other):
    b.data.extend(bytes(_bytes_arg("extend: other", other).data))
    return NONE_VALUE


def copy(b: BytesValue) -> BytesValue:
    return BytesValue(b.data)


def to_vector(b: BytesValue) -> VectorValue:
    return VectorValue([Decimal(x) for x in b.data])


def to_text(b: BytesValue):
    """`some(text)` when the bytes are valid UTF-8, else `none`."""
    try:
        return _some(bytes(b.data).decode("utf-8"))
    except UnicodeDecodeError:
        return NONE_VALUE


def to_text_lossy(b: BytesValue) -> str:
    """UTF-8, each invalid sequence (maximal subpart) becoming U+FFFD."""
    return bytes(b.data).decode("utf-8", errors="replace")


def to_hex(b: BytesValue) -> str:
    return bytes(b.data).hex()


def to_base64(b: BytesValue) -> str:
    data = b.data
    out = []
    for i in range(0, len(data), 3):
        chunk = data[i : i + 3]
        n = int.from_bytes(chunk + b"\0" * (3 - len(chunk)), "big")
        quad = [_B64[(n >> s) & 63] for s in (18, 12, 6, 0)]
        if len(chunk) < 3:
            quad[len(chunk) + 1 :] = "=" * (3 - len(chunk))
        out.extend(quad)
    return "".join(out)


def index_of(b: BytesValue, needle):
    """`some(i)`, the position of the first occurrence of `needle` (`some(0)`
    when it's empty), or `none`."""
    pos = b.data.find(bytes(_bytes_arg("index_of: needle", needle).data))
    return NONE_VALUE if pos < 0 else _some(Decimal(pos))


def string_to_bytes(s: str) -> BytesValue:
    """`String.to_bytes()`: the text's UTF-8 encoding."""
    return BytesValue(s.encode("utf-8"))


# -- decoding --------------------------------------------------------------------------


def decode_hex(text: str):
    if len(text) % 2 or any(c not in _HEX_DIGITS for c in text):
        return None
    return bytes.fromhex(text)


def decode_base64(text: str):
    if len(text) % 4:
        return None
    out = bytearray()
    for i in range(0, len(text), 4):
        quad = text[i : i + 4]
        last = i + 4 == len(text)
        pad = len(quad) - len(quad.rstrip("=")) if last else 0
        if pad > 2:
            return None
        digits = quad[: 4 - pad]
        if any(c not in _B64_INDEX for c in digits):
            return None
        n = 0
        for c in digits:
            n = (n << 6) | _B64_INDEX[c]
        n <<= 6 * pad
        out.extend(n.to_bytes(3, "big")[: 3 - pad])
    return bytes(out)


# -- the bytes.* natives (1.17) -------------------------------------------------------


def _native_new(ctx, args):
    size, fill = args
    if not _is_number(size):
        raise MahRuntimeError(f"new: size must be a Number, got {type_name_of(size)}", kind="TypeMismatch")
    if size != size.to_integral_value() or size < 0:
        raise MahRuntimeError(
            f"new: size must be a whole number of at least 0, got {_format_decimal(size)}", kind="ArgumentError"
        )
    return BytesValue(bytes([byte_arg("new: fill", fill)]) * int(size))


def _native_from_vector(ctx, args):
    (items,) = args
    if not isinstance(items, VectorValue):
        raise MahRuntimeError(f"from_vector: items must be a Vector, got {type_name_of(items)}", kind="TypeMismatch")
    return BytesValue(bytes(byte_arg(f"from_vector: item {i}", v) for i, v in enumerate(items.items)))


def _native_from_hex(ctx, args):
    data = decode_hex(_string_arg("from_hex: text", args[0]))
    return NONE_VALUE if data is None else _some(BytesValue(data))


def _native_from_base64(ctx, args):
    data = decode_base64(_string_arg("from_base64: text", args[0]))
    return NONE_VALUE if data is None else _some(BytesValue(data))


NATIVES = {
    "bytes.new": (2, _native_new),
    "bytes.from_vector": (1, _native_from_vector),
    "bytes.from_hex": (1, _native_from_hex),
    "bytes.from_base64": (1, _native_from_base64),
}
