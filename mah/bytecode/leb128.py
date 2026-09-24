"""varuint/varint encoding -- docs/MAHC_FORMAT.md #2.

`varuint`: unsigned LEB128 (7 bits per byte, low groups first, high bit =
"more bytes follow"); arbitrary size. `varint`: a signed integer, zigzag-
mapped (`n >= 0 -> 2n`, `n < 0 -> -2n-1`) then `varuint`.
"""

from __future__ import annotations

from .format import MahcFormatError


def write_varuint(n: int) -> bytes:
    if n < 0:
        raise ValueError(f"write_varuint: negative value {n}")
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        if n:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def read_varuint(data: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise MahcFormatError("truncated file: expected a varuint, ran out of bytes")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return result, pos
        shift += 7


def write_varint(n: int) -> bytes:
    zigzag = (n << 1) if n >= 0 else (((-n) << 1) - 1)
    return write_varuint(zigzag)


def read_varint(data: bytes, pos: int) -> tuple[int, int]:
    zigzag, pos = read_varuint(data, pos)
    if zigzag & 1:
        return -((zigzag + 1) >> 1), pos
    return zigzag >> 1, pos
