"""`Program` -> bytes -- docs/MAHC_FORMAT.md #3/#4. Deterministic: encoding
the same `Program` twice (or the same source file twice, since `lower.py`
interns strings/constants/natives in a fixed first-use order) always
produces identical bytes -- tests/test_bytecode.py checks this directly.
"""

from __future__ import annotations

from .format import (
    MAGIC,
    MAJOR,
    OPCODES,
    SEC_CODE,
    SEC_CONSTANTS,
    SEC_DEBUG,
    SEC_FUNCTIONS,
    SEC_NATIVES,
    SEC_PARAMS,
    SEC_STRINGS,
    SEC_TYPES,
    TAG_DEC,
    TAG_INT,
    TAG_STR,
)
from .leb128 import write_varint, write_varuint
from .program import Program


def _u8(n: int) -> bytes:
    return bytes((n,))


def _u16(n: int) -> bytes:
    return n.to_bytes(2, "little")


def _str_index(idx: int) -> bytes:
    return write_varuint(idx)


def _opt_str_index(idx: int | None) -> bytes:
    return write_varuint(0) if idx is None else write_varuint(idx + 1)


def _encode_operand(kind: str, value) -> bytes:
    if kind == "A":
        depth, slot = value
        return write_varuint(depth) + write_varuint(slot)
    if kind == "A?":
        if value is None:
            return write_varuint(0)
        depth, slot = value
        return write_varuint(depth + 1) + write_varuint(slot)
    if kind == "A*":
        out = bytearray(write_varuint(len(value)))
        for item in value:
            out += _encode_operand("A", item)
        return bytes(out)
    if kind == "S*":
        # M16 (1.1): a plain string-index list (not "S?" per entry) --
        # keyword-argument names are always present, never absent.
        out = bytearray(write_varuint(len(value)))
        for item in value:
            out += write_varuint(item)
        return bytes(out)
    if kind in ("K", "S", "L", "F", "T", "N", "X"):
        return write_varuint(value)
    if kind == "S?":
        return _opt_str_index(value)
    if kind == "B":
        return _u8(1 if value else 0)
    raise AssertionError(f"unknown operand kind {kind!r}")


def _encode_instr(instr) -> bytes:
    code, kinds = OPCODES[instr.op]
    out = bytearray(_u8(code))
    for kind, value in zip(kinds, instr.args):
        out += _encode_operand(kind, value)
    return bytes(out)


def _section(section_id: int, payload: bytes) -> bytes:
    return _u8(section_id) + write_varuint(len(payload)) + payload


def encode(program: Program) -> bytes:
    out = bytearray()
    out += MAGIC
    out += _u16(MAJOR)
    out += _u16(program.minor)

    # STRINGS
    payload = bytearray(write_varuint(len(program.strings)))
    for s in program.strings:
        raw = s.encode("utf-8")
        payload += write_varuint(len(raw))
        payload += raw
    out += _section(SEC_STRINGS, bytes(payload))

    # CONSTANTS
    payload = bytearray(write_varuint(len(program.constants)))
    for c in program.constants:
        payload += _u8(c.tag)
        if c.tag == TAG_INT:
            payload += write_varint(c.value)
        elif c.tag in (TAG_DEC, TAG_STR):
            payload += _str_index(c.value)
        # else (none/false/true): no payload
    out += _section(SEC_CONSTANTS, bytes(payload))

    # TYPES
    payload = bytearray(write_varuint(len(program.types)))
    for t in program.types:
        payload += _u8(t.kind)
        payload += _str_index(t.name)
        if t.kind == 0:
            payload += write_varuint(len(t.fields))
            for f in t.fields:
                payload += _str_index(f)
        else:
            payload += write_varuint(len(t.variants))
            for vname, vfields in t.variants:
                payload += _str_index(vname)
                payload += write_varuint(len(vfields))
                for f in vfields:
                    payload += _str_index(f)
    out += _section(SEC_TYPES, bytes(payload))

    # NATIVES
    payload = bytearray(write_varuint(len(program.natives)))
    for n in program.natives:
        payload += _str_index(n.name)
        payload += write_varuint(n.arity)
    out += _section(SEC_NATIVES, bytes(payload))

    # FUNCTIONS
    payload = bytearray(write_varuint(len(program.functions)))
    for fn in program.functions:
        payload += write_varuint(fn.entry)
        payload += write_varuint(fn.slot_count)
        payload += write_varuint(fn.param_count)
        payload += _opt_str_index(fn.name)
    out += _section(SEC_FUNCTIONS, bytes(payload))

    # CODE
    payload = bytearray(write_varuint(len(program.code)))
    for instr in program.code:
        payload += _encode_instr(instr)
    out += _section(SEC_CODE, bytes(payload))

    # PARAMS (0x07, required from minor 1 -- docs/MAHC_FORMAT.md #4.5a)
    if program.minor >= 1:
        payload = bytearray()
        for fn in program.functions:
            params = fn.params if fn.params is not None else []
            payload += write_varuint(len(params))
            for name_idx, has_default in params:
                payload += _str_index(name_idx)
                payload += _u8(1 if has_default else 0)
        out += _section(SEC_PARAMS, bytes(payload))

    # DEBUG (optional; only present for a debug-target build)
    if program.debug is not None:
        payload = bytearray(write_varuint(len(program.debug.files)))
        for f in program.debug.files:
            payload += _str_index(f)
        payload += write_varuint(len(program.debug.runs))
        prev_pc = 0
        for pc, file_idx, line, col in program.debug.runs:
            payload += write_varuint(pc - prev_pc)
            prev_pc = pc
            payload += write_varuint(file_idx)
            payload += write_varuint(line)
            payload += write_varuint(col)
        out += _section(SEC_DEBUG, bytes(payload))

    return bytes(out)
