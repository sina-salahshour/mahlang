"""bytes -> `Program`, with full load-time structural validation --
docs/MAHC_FORMAT.md #3/#4 ("fail fast"). Everything a VM must reject before
running anything: bad magic/version, truncation, missing/duplicated/
out-of-order/unknown-required sections, a section payload not fully
consumed, an unknown opcode, any out-of-range index, a
`struct`/`enum` value count that doesn't match its declared field count, a
`native` arg count that doesn't match its declared arity.

Native *names* are deliberately NOT checked here -- whether a given VM
implements a given native (with a matching arity) is that VM's own
business (mah/code_interpreter.py's link step), not a property of whether
the file is well-formed (see docs/MAHC_FORMAT.md #3's "extendable" goal --
a file naming a native this VM doesn't have is still a perfectly valid
file, just not one this VM can run).
"""

from __future__ import annotations

from .format import (
    MAGIC,
    MAJOR,
    MINOR,
    OPCODES_BY_CODE,
    REQUIRED_SECTIONS,
    SEC_CODE,
    SEC_CONSTANTS,
    SEC_DEBUG,
    SEC_FUNCTIONS,
    SEC_NATIVES,
    SEC_STRINGS,
    SEC_TYPES,
    TAG_DEC,
    TAG_FALSE,
    TAG_INT,
    TAG_NONE,
    TAG_STR,
    TAG_TRUE,
    BUILTIN_TYPES,
    MahcFormatError,
)
from .leb128 import read_varint, read_varuint
from .program import Const, DebugInfo, FunctionDecl, Instr, NativeRef, Program, TypeDecl


class _Reader:
    __slots__ = ("data", "pos")

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def remaining(self) -> int:
        return len(self.data) - self.pos

    def bytes(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise MahcFormatError("truncated file: not enough bytes remaining")
        out = self.data[self.pos : self.pos + n]
        self.pos += n
        return out

    def u8(self) -> int:
        return self.bytes(1)[0]

    def u16(self) -> int:
        return int.from_bytes(self.bytes(2), "little")

    def varuint(self) -> int:
        value, pos = read_varuint(self.data, self.pos)
        self.pos = pos
        return value

    def varint(self) -> int:
        value, pos = read_varint(self.data, self.pos)
        self.pos = pos
        return value


def _check_consumed(reader: _Reader, section_name: str) -> None:
    if reader.remaining() != 0:
        raise MahcFormatError(f"{section_name} section payload not fully consumed (trailing garbage)")


def _read_sections(r: _Reader) -> tuple[dict, bytes | None]:
    """Consume every `(id, length, payload)` section to EOF, enforcing
    docs/MAHC_FORMAT.md #3's ordering rules, and return `(required_payloads,
    debug_payload_or_None)`. Unknown OPTIONAL sections (0x81-0xFF) are
    consumed (so their bytes don't corrupt the next section) and otherwise
    ignored."""
    payloads: dict[int, bytes] = {}
    debug_payload: bytes | None = None
    expect_idx = 0
    while r.remaining() > 0:
        sec_id = r.u8()
        length = r.varuint()
        payload = r.bytes(length)
        if expect_idx < len(REQUIRED_SECTIONS):
            expected = REQUIRED_SECTIONS[expect_idx]
            if sec_id != expected:
                if sec_id in REQUIRED_SECTIONS:
                    raise MahcFormatError(
                        f"required sections out of order or duplicated: expected section "
                        f"0x{expected:02x}, got 0x{sec_id:02x}"
                    )
                if sec_id < 0x80:
                    raise MahcFormatError(f"unknown required section 0x{sec_id:02x}")
                raise MahcFormatError(
                    f"missing required section 0x{expected:02x} (found optional section 0x{sec_id:02x} first)"
                )
            payloads[sec_id] = payload
            expect_idx += 1
        else:
            if sec_id < 0x80:
                raise MahcFormatError(f"unknown required section 0x{sec_id:02x}")
            if sec_id == SEC_DEBUG:
                if debug_payload is not None:
                    raise MahcFormatError("duplicate DEBUG section")
                debug_payload = payload
            # else: an unknown optional section -- already consumed, skip it.
    if expect_idx < len(REQUIRED_SECTIONS):
        raise MahcFormatError(f"missing required section 0x{REQUIRED_SECTIONS[expect_idx]:02x}")
    return payloads, debug_payload


def _parse_strings(payload: bytes) -> list:
    pr = _Reader(payload)
    count = pr.varuint()
    strings = []
    for _ in range(count):
        length = pr.varuint()
        raw = pr.bytes(length)
        try:
            strings.append(raw.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise MahcFormatError(f"invalid UTF-8 in STRINGS section: {exc}") from None
    _check_consumed(pr, "STRINGS")
    return strings


def _parse_constants(payload: bytes, nstrings: int) -> list:
    pr = _Reader(payload)
    count = pr.varuint()
    constants = []
    for _ in range(count):
        tag = pr.u8()
        if tag in (TAG_NONE, TAG_FALSE, TAG_TRUE):
            constants.append(Const(tag, None))
        elif tag == TAG_INT:
            constants.append(Const(tag, pr.varint()))
        elif tag == TAG_DEC:
            idx = pr.varuint()
            if idx >= nstrings:
                raise MahcFormatError(f"CONSTANTS: decimal-text string index {idx} out of range")
            constants.append(Const(tag, idx))
        elif tag == TAG_STR:
            idx = pr.varuint()
            if idx >= nstrings:
                raise MahcFormatError(f"CONSTANTS: string constant index {idx} out of range")
            constants.append(Const(tag, idx))
        else:
            raise MahcFormatError(f"unknown constant tag {tag}")
    _check_consumed(pr, "CONSTANTS")
    return constants


def _parse_types(payload: bytes, nstrings: int) -> list:
    pr = _Reader(payload)
    count = pr.varuint()
    types = []

    def _str_idx() -> int:
        idx = pr.varuint()
        if idx >= nstrings:
            raise MahcFormatError(f"TYPES: string index {idx} out of range")
        return idx

    for _ in range(count):
        kind = pr.u8()
        name = _str_idx()
        if kind == 0:
            nfields = pr.varuint()
            fields = [_str_idx() for _ in range(nfields)]
            types.append(TypeDecl(kind, name, fields, None))
        elif kind == 1:
            nvariants = pr.varuint()
            variants = []
            for _ in range(nvariants):
                vname = _str_idx()
                nfields = pr.varuint()
                vfields = [_str_idx() for _ in range(nfields)]
                variants.append((vname, vfields))
            types.append(TypeDecl(kind, name, None, variants))
        else:
            raise MahcFormatError(f"unknown TYPES kind {kind}")
    _check_consumed(pr, "TYPES")
    return types


def _parse_natives(payload: bytes, nstrings: int) -> list:
    pr = _Reader(payload)
    count = pr.varuint()
    natives = []
    for _ in range(count):
        name = pr.varuint()
        if name >= nstrings:
            raise MahcFormatError(f"NATIVES: name string index {name} out of range")
        arity = pr.varuint()
        natives.append(NativeRef(name, arity))
    _check_consumed(pr, "NATIVES")
    return natives


def _parse_functions(payload: bytes, nstrings: int) -> list:
    pr = _Reader(payload)
    count = pr.varuint()
    if count < 1:
        raise MahcFormatError("FUNCTIONS section must declare at least one function (function 0, the main program)")
    functions = []
    for _ in range(count):
        entry = pr.varuint()
        slot_count = pr.varuint()
        param_count = pr.varuint()
        name_flag = pr.varuint()
        name = None if name_flag == 0 else name_flag - 1
        if name is not None and name >= nstrings:
            raise MahcFormatError(f"FUNCTIONS: name string index {name} out of range")
        functions.append(FunctionDecl(entry, slot_count, param_count, name))
    if functions[0].entry != 0 or functions[0].param_count != 0:
        raise MahcFormatError("function 0 (the main program) must have entry=0 and param_count=0")
    _check_consumed(pr, "FUNCTIONS")
    return functions


def _type_variants(t_index: int, ctx: dict):
    """The `[(variant_name_idx_or_str, fields), ...]` list for type index
    `t_index` -- built-in (0/1) or user (>=2, `ctx["types"]`). Returns
    `None` if `t_index` doesn't name an enum (kind 1) type at all."""
    if t_index in (0, 1):
        return BUILTIN_TYPES[t_index][1]
    decl = ctx["types"][t_index - 2]
    return decl.variants if decl.kind == 1 else None


def _type_fields(t_index: int, ctx: dict):
    """The declared field-name list for STRUCT type index `t_index`, or
    `None` if it doesn't name a struct (kind 0) type."""
    if t_index in (0, 1):
        return None  # Option/Promise are enums, never a struct target
    decl = ctx["types"][t_index - 2]
    return decl.fields if decl.kind == 0 else None


def _decode_operand(pr: _Reader, kind: str, ctx: dict):
    if kind == "A":
        return (pr.varuint(), pr.varuint())
    if kind == "A?":
        d = pr.varuint()
        if d == 0:
            return None
        return (d - 1, pr.varuint())
    if kind == "A*":
        n = pr.varuint()
        return tuple(_decode_operand(pr, "A", ctx) for _ in range(n))
    if kind == "K":
        k = pr.varuint()
        if k >= ctx["nconsts"]:
            raise MahcFormatError(f"constant index {k} out of range")
        return k
    if kind == "S":
        s = pr.varuint()
        if s >= ctx["nstrings"]:
            raise MahcFormatError(f"string index {s} out of range")
        return s
    if kind == "S?":
        s = pr.varuint()
        if s == 0:
            return None
        idx = s - 1
        if idx >= ctx["nstrings"]:
            raise MahcFormatError(f"string index {idx} out of range")
        return idx
    if kind == "L":
        return pr.varuint()  # validated once the full instruction count is known
    if kind == "F":
        f = pr.varuint()
        if f >= ctx["nfunctions"]:
            raise MahcFormatError(f"function index {f} out of range")
        return f
    if kind == "T":
        t = pr.varuint()
        if t >= 2 + ctx["ntypes"]:
            raise MahcFormatError(f"type index {t} out of range")
        return t
    if kind == "N":
        return pr.varuint()  # validated per-instruction below (needs the type's variant count)
    if kind == "X":
        x = pr.varuint()
        if x >= ctx["nnatives"]:
            raise MahcFormatError(f"native index {x} out of range")
        return x
    if kind == "B":
        b = pr.u8()
        if b not in (0, 1):
            raise MahcFormatError(f"invalid boolean flag {b}")
        return bool(b)
    raise AssertionError(f"unknown operand kind {kind!r}")


def _parse_code(payload: bytes, ctx: dict) -> list:
    pr = _Reader(payload)
    count = pr.varuint()
    instrs = []
    for i in range(count):
        opcode = pr.u8()
        found = OPCODES_BY_CODE.get(opcode)
        if found is None:
            raise MahcFormatError(f"unknown opcode 0x{opcode:02x} at instruction {i}")
        name, kinds = found
        args = tuple(_decode_operand(pr, kind, ctx) for kind in kinds)
        instrs.append(Instr(name, args))
    _check_consumed(pr, "CODE")

    ncode = len(instrs)
    for i, instr in enumerate(instrs):
        op = instr.op
        if op == "jmp":
            (target,) = instr.args
            if target >= ncode:
                raise MahcFormatError(f"jump target {target} out of range at instruction {i}")
        elif op == "jmpf":
            _cond, target = instr.args
            if target >= ncode:
                raise MahcFormatError(f"jump target {target} out of range at instruction {i}")
        elif op == "struct":
            t, values, _dest = instr.args
            fields = _type_fields(t, ctx)
            if fields is None:
                raise MahcFormatError(f"'struct' at instruction {i} used with a non-struct type index {t}")
            if len(values) != len(fields):
                raise MahcFormatError(
                    f"'struct' at instruction {i}: {len(values)} value(s) given, type declares {len(fields)} field(s)"
                )
        elif op == "enum":
            t, variant, values, _dest = instr.args
            variants = _type_variants(t, ctx)
            if variants is None:
                raise MahcFormatError(f"'enum' at instruction {i} used with a non-enum type index {t}")
            if variant >= len(variants):
                raise MahcFormatError(f"'enum' at instruction {i}: variant index {variant} out of range for type {t}")
            if len(values) != len(variants[variant][1]):
                raise MahcFormatError(
                    f"'enum' at instruction {i}: {len(values)} value(s) given, variant declares "
                    f"{len(variants[variant][1])} field(s)"
                )
        elif op == "matchstruct":
            _value, t, _dest = instr.args
            if _type_fields(t, ctx) is None:
                raise MahcFormatError(f"'matchstruct' at instruction {i} used with a non-struct type index {t}")
        elif op == "matchenum":
            _value, t, variant, _dest = instr.args
            variants = _type_variants(t, ctx)
            if variants is None:
                raise MahcFormatError(f"'matchenum' at instruction {i} used with a non-enum type index {t}")
            if variant >= len(variants):
                raise MahcFormatError(
                    f"'matchenum' at instruction {i}: variant index {variant} out of range for type {t}"
                )
        elif op == "native":
            fn, args, _dest = instr.args
            native = ctx["natives"][fn]
            if len(args) != native.arity:
                raise MahcFormatError(
                    f"'native' at instruction {i}: {len(args)} arg(s) given, native declares arity {native.arity}"
                )
    return instrs


def _parse_debug(payload: bytes, nstrings: int) -> DebugInfo:
    pr = _Reader(payload)
    nfiles = pr.varuint()
    files = []
    for _ in range(nfiles):
        idx = pr.varuint()
        if idx >= nstrings:
            raise MahcFormatError(f"DEBUG: file string index {idx} out of range")
        files.append(idx)
    nruns = pr.varuint()
    runs = []
    prev_pc = 0
    for _ in range(nruns):
        delta = pr.varuint()
        pc = prev_pc + delta
        prev_pc = pc
        file_idx = pr.varuint()
        if file_idx >= len(files):
            raise MahcFormatError(f"DEBUG: run file index {file_idx} out of range")
        line = pr.varuint()
        col = pr.varuint()
        runs.append((pc, file_idx, line, col))
    _check_consumed(pr, "DEBUG")
    return DebugInfo(files, runs)


def decode(data: bytes) -> Program:
    r = _Reader(data)
    magic = r.bytes(4)
    if magic != MAGIC:
        raise MahcFormatError(f"bad magic: expected {MAGIC!r}, got {magic!r}")
    major = r.u16()
    if major != MAJOR:
        raise MahcFormatError(f"unsupported major version {major} (this VM implements major version {MAJOR})")
    minor = r.u16()
    if minor > MINOR:
        raise MahcFormatError(f"unsupported minor version {minor} (this VM supports up to minor version {MINOR})")

    payloads, debug_payload = _read_sections(r)

    strings = _parse_strings(payloads[SEC_STRINGS])
    constants = _parse_constants(payloads[SEC_CONSTANTS], len(strings))
    types = _parse_types(payloads[SEC_TYPES], len(strings))
    natives = _parse_natives(payloads[SEC_NATIVES], len(strings))
    functions = _parse_functions(payloads[SEC_FUNCTIONS], len(strings))

    ctx = {
        "nstrings": len(strings),
        "nconsts": len(constants),
        "ntypes": len(types),
        "types": types,
        "nfunctions": len(functions),
        "nnatives": len(natives),
        "natives": natives,
    }
    code = _parse_code(payloads[SEC_CODE], ctx)

    debug = _parse_debug(debug_payload, len(strings)) if debug_payload is not None else None

    return Program(strings, constants, types, natives, functions, code, debug, minor=minor)
