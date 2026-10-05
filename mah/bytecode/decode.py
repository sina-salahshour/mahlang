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
    NATIVE_ARITIES,
    NATIVE_SINCE_MINOR,
    OPCODE_SINCE_MINOR,
    OPCODES_BY_CODE,
    REQUIRED_SECTIONS,
    REQUIRED_SECTIONS_V1,
    REQUIRED_SECTIONS_V4,
    SEC_CODE,
    SEC_CONSTANTS,
    SEC_DEBUG,
    SEC_FUNCTIONS,
    SEC_HANDLERS,
    SEC_META,
    SEC_NATIVES,
    SEC_PARAMS,
    SEC_STRINGS,
    SEC_TESTS,
    SEC_TYPES,
    TAG_DEC,
    TAG_FALSE,
    TAG_INT,
    TAG_NONE,
    TAG_STR,
    TAG_TRUE,
    builtin_types_for,
    MahcFormatError,
)
from . import bundle
from .leb128 import read_varint, read_varuint
from .program import (
    Const,
    DebugInfo,
    FnMeta,
    FunctionDecl,
    Instr,
    Meta,
    NativeRef,
    ParamMeta,
    Program,
    TestEntry,
    TypeDecl,
    TypeMeta,
    TypeRef,
)


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


def _newer_minor_message(r: _Reader, minor: int) -> str:
    """M27 (docs/MAHC_FORMAT.md #3/#4.4): a file newer than this VM is still
    rejected, but when the reason is natives this VM doesn't have (the usual
    one: a program using a newer standard library), say which. STRINGS and
    NATIVES keep their 1.0 layout in every 1.x file, so they can be read
    from a newer file; anything unreadable just gives the plain message."""
    message = f"unsupported minor version {minor} (this VM supports up to minor version {MINOR})"
    try:
        payloads = {}
        while r.remaining() > 0:
            sec_id = r.u8()
            payloads[sec_id] = r.bytes(r.varuint())
        strings = _parse_strings(payloads[SEC_STRINGS])
        natives = _parse_natives(payloads[SEC_NATIVES], len(strings))
        missing = [strings[ref.name] for ref in natives if strings[ref.name] not in NATIVE_ARITIES]
    except (MahcFormatError, KeyError, IndexError):
        return message
    if not missing:
        return message
    names = ", ".join(f"'{name}'" for name in missing)
    return f"{message}: it uses natives this VM doesn't have ({names}); upgrade mah to run it"


def _read_sections(r: _Reader, minor: int) -> tuple[dict, bytes | None]:
    """Consume every `(id, length, payload)` section to EOF, enforcing
    docs/MAHC_FORMAT.md #3's ordering rules, and return `(required_payloads,
    debug_payload_or_None)`. Unknown OPTIONAL sections (0x81-0xFF) are
    consumed (so their bytes don't corrupt the next section) and otherwise
    ignored.

    M16: which sections are required depends on `minor` -- a minor >= 1
    file must also contain PARAMS (0x07), right after CODE; a minor 0 file
    must NOT contain it at all (docs/MAHC_FORMAT.md #4.5a/#7) -- both
    directions fall out of picking the right `required` tuple up front: a
    0x07 section in a minor-0 file is simply not the next expected required
    section and not itself a known required id, so it's rejected by the
    existing "unknown required section" branch below with no special-casing
    needed."""
    if minor >= 4:
        required = REQUIRED_SECTIONS_V4
    elif minor >= 1:
        required = REQUIRED_SECTIONS_V1
    else:
        required = REQUIRED_SECTIONS
    payloads: dict[int, bytes] = {}
    debug_payload: bytes | None = None
    expect_idx = 0
    while r.remaining() > 0:
        sec_id = r.u8()
        length = r.varuint()
        payload = r.bytes(length)
        if expect_idx < len(required):
            expected = required[expect_idx]
            if sec_id != expected:
                if sec_id in required:
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
            if sec_id in required:
                raise MahcFormatError(f"duplicate required section 0x{sec_id:02x}")
            if sec_id < 0x80:
                raise MahcFormatError(f"unknown required section 0x{sec_id:02x}")
            if sec_id == SEC_DEBUG:
                if debug_payload is not None:
                    raise MahcFormatError("duplicate DEBUG section")
                debug_payload = payload
            elif sec_id == SEC_TESTS:
                # M28: kept alongside the required payloads.
                if SEC_TESTS in payloads:
                    raise MahcFormatError("duplicate TESTS section")
                payloads[SEC_TESTS] = payload
            elif sec_id == SEC_META:
                # M41a: kept alongside the required payloads.
                if SEC_META in payloads:
                    raise MahcFormatError("duplicate META section")
                payloads[SEC_META] = payload
            # else: an unknown optional section -- already consumed, skip it.
    if expect_idx < len(required):
        raise MahcFormatError(f"missing required section 0x{required[expect_idx]:02x}")
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


def _parse_params(payload: bytes, functions: list, nstrings: int, minor: int = 0) -> list:
    """M16 (1.1): PARAMS section, docs/MAHC_FORMAT.md #4.5a -- one entry per
    function, in FUNCTIONS order, each `nparams` required to equal that
    function's own `param_count`. Returns a new `functions` list with
    `.params` filled in (FunctionDecl is otherwise unchanged)."""
    pr = _Reader(payload)
    out = []
    for fn_index, fn in enumerate(functions):
        nparams = pr.varuint()
        if nparams != fn.param_count:
            raise MahcFormatError(
                f"PARAMS: function declares {fn.param_count} parameter(s) but PARAMS lists {nparams}"
            )
        params = []
        flag_bytes = []
        for _ in range(nparams):
            name = pr.varuint()
            if name >= nstrings:
                raise MahcFormatError(f"PARAMS: name string index {name} out of range")
            flags = pr.u8()
            if minor < 16:
                if flags & ~1:
                    raise MahcFormatError(f"PARAMS: invalid flags byte {flags} (only bit 0 is defined)")
            elif flags & ~7:
                raise MahcFormatError(f"PARAMS: invalid flags byte {flags} (only bits 0-2 are defined)")
            params.append((name, bool(flags & 1)))
            flag_bytes.append(flags)
        rest = _rest_flags(flag_bytes, fn_index)
        out.append(FunctionDecl(fn.entry, fn.slot_count, fn.param_count, fn.name, params=params, rest=rest))
    _check_consumed(pr, "PARAMS")
    return out


def _rest_flags(flag_bytes: list, fn_index: int) -> int:
    """M41c (1.16, docs/MAHC_FORMAT.md #4.5a): the rest-parameter bits of one
    function's PARAMS flags -- bit 1 (positional rest) and bit 2 (keyword
    rest). A rest parameter has no default and carries one bit; the
    positional one is the last parameter, or the one before the keyword one
    (which is the last); each at most once. Returns bit 0 = positional rest,
    bit 1 = keyword rest. Same messages as runtime/src/decode.rs."""
    n = len(flag_bytes)
    rest = 0
    for i, flags in enumerate(flag_bytes):
        pos, kw = bool(flags & 2), bool(flags & 4)
        if not (pos or kw):
            continue
        if pos and kw:
            raise MahcFormatError(
                f"PARAMS: function {fn_index} parameter {i} is flagged as both a '...' and a '**' rest parameter"
            )
        if flags & 1:
            raise MahcFormatError(
                f"PARAMS: function {fn_index} parameter {i} is a rest parameter and can't have a default"
            )
        if kw:
            if i != n - 1:
                raise MahcFormatError(
                    f"PARAMS: function {fn_index}: the '**' rest parameter must be the last parameter"
                )
            rest |= 2
        else:
            last_ok = i == n - 1 or (i == n - 2 and flag_bytes[n - 1] & 4)
            if not last_ok:
                raise MahcFormatError(
                    f"PARAMS: function {fn_index}: the '...' rest parameter must be the last parameter "
                    f"or the one before the '**' rest parameter"
                )
            rest |= 1
    return rest


def _type_variants(t_index: int, ctx: dict):
    """The `[(variant_name_idx_or_str, fields), ...]` list for type index
    `t_index` -- built-in (`< len(ctx["builtin_types"])`) or user (the rest,
    `ctx["types"]`). Returns `None` if `t_index` doesn't name an enum
    (kind 1) type at all. M25: how many built-in types there are (and
    where user types start numbering from) depends on the file's minor
    version -- see `format.builtin_types_for`."""
    builtin = ctx["builtin_types"]
    if t_index < len(builtin):
        return builtin[t_index][1]
    decl = ctx["types"][t_index - len(builtin)]
    return decl.variants if decl.kind == 1 else None


def _type_fields(t_index: int, ctx: dict):
    """The declared field-name list for STRUCT type index `t_index`, or
    `None` if it doesn't name a struct (kind 0) type."""
    builtin = ctx["builtin_types"]
    if t_index < len(builtin):
        return None  # every built-in type (Option/Promise/RuntimeError) is an enum
    decl = ctx["types"][t_index - len(builtin)]
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
    if kind == "S*":
        # M16 (1.1): a plain string-index list (keyword-argument names) --
        # always present, no "S?"-style absence marker per entry.
        n = pr.varuint()
        out = []
        for _ in range(n):
            s = pr.varuint()
            if s >= ctx["nstrings"]:
                raise MahcFormatError(f"string index {s} out of range")
            out.append(s)
        return tuple(out)
    if kind == "L":
        return pr.varuint()  # validated once the full instruction count is known
    if kind == "F":
        f = pr.varuint()
        if f >= ctx["nfunctions"]:
            raise MahcFormatError(f"function index {f} out of range")
        return f
    if kind == "T":
        t = pr.varuint()
        if t >= len(ctx["builtin_types"]) + ctx["ntypes"]:
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
    minor = ctx["minor"]
    pr = _Reader(payload)
    count = pr.varuint()
    instrs = []
    for i in range(count):
        opcode = pr.u8()
        found = OPCODES_BY_CODE.get(opcode)
        if found is None:
            raise MahcFormatError(f"unknown opcode 0x{opcode:02x} at instruction {i}")
        name, kinds = found
        since = OPCODE_SINCE_MINOR.get(name)
        if since is not None and minor < since:
            raise MahcFormatError(
                f"opcode '{name}' at instruction {i} requires minor version >= {since}, "
                f"but this file's minor version is {minor}"
            )
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
        elif op == "jmpset":
            # M16: `param` is an ordinary `A` operand (already range-checked
            # by `_decode_operand`) -- only `L` needs a jump-target check,
            # same as `jmpf`. (docs/MAHC_FORMAT.md's own note: validating
            # that `param`'s depth is 0 and its slot belongs to the
            # enclosing function isn't done here -- the decoder has no way
            # to know which function an instruction belongs to.)
            _param, target = instr.args
            if target >= ncode:
                raise MahcFormatError(f"jump target {target} out of range at instruction {i}")
        elif op in ("callkw", "callmethodkw", "detachkw", "detachmethodkw"):
            _validate_kwnames(op, instr.args, ctx, i)
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
        elif op == "loadtype":
            kind, index, _dest = instr.args
            if kind == 0:
                if index >= len(ctx["builtin_types"]) + ctx["ntypes"]:
                    raise MahcFormatError(f"'loadtype' at instruction {i}: type index {index} out of range")
            elif kind == 1:
                # M37 (1.17): code 8, Bytes
                if index >= (9 if ctx["minor"] >= 17 else 8):
                    raise MahcFormatError(f"'loadtype' at instruction {i}: primitive type code {index} out of range")
            else:
                raise MahcFormatError(f"'loadtype' at instruction {i}: unknown kind {kind}")
        elif op == "decorate":
            _validate_decorate(instr.args, ctx, i)
        elif op == "paramhooks":
            # M41c: the function and the parameter exist
            fn_index, param_index, _dest = instr.args
            if fn_index >= ctx["nfunctions"]:
                raise MahcFormatError(f"'paramhooks' at instruction {i}: function index {fn_index} out of range")
            if param_index >= ctx["param_counts"][fn_index]:
                raise MahcFormatError(
                    f"'paramhooks' at instruction {i}: parameter index {param_index} out of range for function {fn_index}"
                )
        elif op == "defmethod" and minor >= 16:
            # M41c: a function-item impl's type is spelled `fn#<function index>`
            type_name = ctx["strings"][instr.args[1]]
            if type_name.startswith("fn#"):
                digits = type_name[3:]
                if not (digits.isascii() and digits.isdigit() and str(int(digits)) == digits) or int(digits) >= ctx["nfunctions"]:
                    raise MahcFormatError(
                        f"'defmethod' at instruction {i}: '{type_name}' is not a valid function-item key"
                    )
        elif op == "map":
            items, _dest = instr.args
            if len(items) % 2 != 0:
                raise MahcFormatError(f"'map' at instruction {i}: needs an even number of addresses, got {len(items)}")
        elif op == "matchrange":
            _value, lo, hi, _incl, _dest = instr.args
            if lo is None and hi is None:
                raise MahcFormatError(f"'matchrange' at instruction {i}: at least one of lo/hi must be present")
        elif op == "native":
            fn, args, _dest = instr.args
            native = ctx["natives"][fn]
            if len(args) != native.arity:
                raise MahcFormatError(
                    f"'native' at instruction {i}: {len(args)} arg(s) given, native declares arity {native.arity}"
                )
            native_name = ctx["strings"][native.name]
            since = NATIVE_SINCE_MINOR.get(native_name)
            if since is not None and minor < since:
                raise MahcFormatError(
                    f"native '{native_name}' at instruction {i} requires minor version >= {since}, "
                    f"but this file's minor version is {minor}"
                )
    return instrs


def _validate_decorate(args: tuple, ctx: dict, i: int) -> None:
    """M41b (docs/MAHC_FORMAT.md #4.6): `decorate kind, a, b, values` -- kind
    <= 4; a function (0) / a parameter (1) exists; a type (2), field (3) or
    variant (4) target is a *user* type of the right shape. Messages are the
    same on both VMs (runtime/src/decode.rs)."""
    kind, a, b, _values = args
    if kind > 4:
        raise MahcFormatError(f"'decorate' at instruction {i}: unknown kind {kind}")
    if kind <= 1:
        if a >= ctx["nfunctions"]:
            raise MahcFormatError(f"'decorate' at instruction {i}: function index {a} out of range")
        if kind == 1 and b >= ctx["param_counts"][a]:
            raise MahcFormatError(
                f"'decorate' at instruction {i}: parameter index {b} out of range for function {a}"
            )
        return
    builtin = len(ctx["builtin_types"])
    if a < builtin or a >= builtin + ctx["ntypes"]:
        raise MahcFormatError(f"'decorate' at instruction {i}: type index {a} is not a user type")
    decl = ctx["types"][a - builtin]
    if kind == 3:
        if decl.kind != 0:
            raise MahcFormatError(f"'decorate' at instruction {i}: type index {a} is not a struct")
        if b >= len(decl.fields):
            raise MahcFormatError(f"'decorate' at instruction {i}: field index {b} out of range for type {a}")
    elif kind == 4:
        if decl.kind != 1:
            raise MahcFormatError(f"'decorate' at instruction {i}: type index {a} is not an enum")
        if b >= len(decl.variants):
            raise MahcFormatError(f"'decorate' at instruction {i}: variant index {b} out of range for type {a}")


def _validate_kwnames(op: str, args: tuple, ctx: dict, i: int) -> None:
    """M16: shared by every `*kw` opcode -- docs/MAHC_FORMAT.md #4.6's rule
    that `kwnames` names the LAST `len(kwnames)` entries of `args` (so it
    can never exceed `len(args)`), and that the names are distinct."""
    if op == "callkw":
        _callee, arg_addrs, kw_names = args
    elif op == "callmethodkw":
        _recv, _name, arg_addrs, kw_names, _trait = args
    elif op == "detachkw":
        _callee, arg_addrs, kw_names, _dest = args
    else:  # detachmethodkw
        _recv, _name, arg_addrs, kw_names, _trait, _dest = args
    if len(kw_names) > len(arg_addrs):
        raise MahcFormatError(
            f"'{op}' at instruction {i}: {len(kw_names)} keyword name(s) but only {len(arg_addrs)} arg(s)"
        )
    seen: set = set()
    strings = ctx["strings"]
    for idx in kw_names:
        text = strings[idx]
        if text in seen:
            raise MahcFormatError(
                f"'{op}' at instruction {i}: keyword argument name '{text}' given more than once"
            )
        seen.add(text)


def _parse_handlers(payload: bytes, ncode: int) -> list:
    """M25 (1.4, docs/MAHC_FORMAT.md #4.8): HANDLERS section, required iff
    minor >= 4. `ncode` (the CODE section's own instruction count, already
    known since CODE -- id 0x06 -- decodes before HANDLERS -- id 0x08 --
    in section-id order) lets every range/target be validated immediately,
    the same way jump targets are validated in `_parse_code`."""
    pr = _Reader(payload)
    count = pr.varuint()
    handlers = []
    for _ in range(count):
        start = pr.varuint()
        end = pr.varuint()
        handler = pr.varuint()
        slot = pr.varuint()
        if not (start < end <= ncode):
            raise MahcFormatError(
                f"HANDLERS: invalid range [{start}, {end}) for {ncode} instruction(s)"
            )
        if handler >= ncode:
            raise MahcFormatError(f"HANDLERS: handler {handler} out of range for {ncode} instruction(s)")
        handlers.append((start, end, handler, slot))
    _check_consumed(pr, "HANDLERS")
    return handlers


def _parse_meta(payload: bytes, functions: list, types: list, ctx: dict) -> Meta:
    """M41a (docs/MAHC_FORMAT.md #4.10): the META section -- one entry per
    function (FUNCTIONS order) and per user type (TYPES order, built-ins
    excluded), each validated against what it describes."""
    pr = _Reader(payload)
    nstrings = ctx["nstrings"]
    nbuiltin = len(ctx["builtin_types"])
    ntypes_total = nbuiltin + len(types)
    minor = ctx["minor"]

    def str_idx() -> int:
        idx = pr.varuint()
        if idx >= nstrings:
            raise MahcFormatError(f"META: string index {idx} out of range")
        return idx

    def opt_str() -> int | None:
        v = pr.varuint()
        if v == 0:
            return None
        if v - 1 >= nstrings:
            raise MahcFormatError(f"META: string index {v - 1} out of range")
        return v - 1

    def count() -> int:
        n = pr.varuint()
        if n > pr.remaining():
            raise MahcFormatError("META: count larger than the remaining payload")
        return n

    def throws_clause():
        flag = pr.u8()
        if flag == 0:
            return None
        if flag != 1:
            raise MahcFormatError(f"META: invalid throws flag {flag}")
        return [typeref() for _ in range(count())]

    def typeref() -> TypeRef:
        tag = pr.u8()
        if tag in (0, 4, 5):
            return TypeRef(tag)
        if tag == 1:
            kind = pr.u8()
            index = pr.varuint()
            if kind == 0:
                if index >= ntypes_total:
                    raise MahcFormatError(f"META: type index {index} out of range")
            elif kind == 1:
                if index >= (9 if minor >= 17 else 8):  # M37: code 8, Bytes
                    raise MahcFormatError(f"META: primitive type code {index} out of range")
            else:
                raise MahcFormatError(f"META: unknown named-type kind {kind}")
            return TypeRef(1, kind=kind, index=index, args=[typeref() for _ in range(count())])
        if tag == 2:
            params = [typeref() for _ in range(count())]
            ret = typeref()
            return TypeRef(2, args=params, ret=ret, throws=throws_clause())
        if tag == 3:
            return TypeRef(3, name=str_idx())
        if tag == 6:
            name = str_idx()
            return TypeRef(6, name=name, args=[typeref() for _ in range(count())])
        raise MahcFormatError(f"META: unknown type tag {tag}")

    nfunctions = pr.varuint()
    if nfunctions != len(functions):
        raise MahcFormatError(f"META: describes {nfunctions} function(s) but FUNCTIONS declares {len(functions)}")
    fn_metas = []
    for fn in functions:
        flags = pr.u8()
        if flags & ~1:
            raise MahcFormatError(f"META: invalid flags byte {flags} (only bit 0 is defined)")
        if not flags & 1:
            fn_metas.append(FnMeta())
            continue
        doc = opt_str()
        type_params = [str_idx() for _ in range(count())]
        nparams = pr.varuint()
        if nparams != fn.param_count:
            raise MahcFormatError(
                f"META: function declares {fn.param_count} parameter(s) but META lists {nparams}"
            )
        params = []
        for _ in range(nparams):
            ptype = typeref()
            pdoc = opt_str()
            default = pr.u8()
            const = None
            if default == 2:
                const = pr.varuint()
                if const >= ctx["nconsts"]:
                    raise MahcFormatError(f"META: constant index {const} out of range")
            elif default not in (0, 1):
                raise MahcFormatError(f"META: invalid default flag {default}")
            params.append(ParamMeta(ptype, pdoc, default, const))
        returns = typeref()
        fn_metas.append(FnMeta(True, doc, type_params, params, returns, throws_clause()))
    ntypes = pr.varuint()
    if ntypes != len(types):
        raise MahcFormatError(f"META: describes {ntypes} type(s) but TYPES declares {len(types)}")
    type_metas = []
    for decl in types:
        doc = opt_str()
        type_params = [str_idx() for _ in range(count())]
        if decl.kind == 0:
            body = [(typeref(), opt_str()) for _ in decl.fields]
        else:
            body = []
            for _vname, vfields in decl.variants:
                vdoc = opt_str()
                body.append((vdoc, [typeref() for _ in vfields]))
        type_metas.append(TypeMeta(doc, type_params, body))
    _check_consumed(pr, "META")
    return Meta(fn_metas, type_metas)


def _parse_tests(payload: bytes, nstrings: int, nslots: int) -> list:
    """M28 (docs/MAHC_FORMAT.md #4.9): `count x (name str, slot varuint,
    line varuint)`; `slot` must be a slot of the main function's frame."""
    pr = _Reader(payload)
    tests = []
    for _ in range(pr.varuint()):
        name = pr.varuint()
        if name >= nstrings:
            raise MahcFormatError(f"TESTS: name string index {name} out of range")
        slot = pr.varuint()
        if slot >= nslots:
            raise MahcFormatError(f"TESTS: slot {slot} out of range")
        tests.append(TestEntry(name, slot, pr.varuint()))
    _check_consumed(pr, "TESTS")
    return tests


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
    # a self-contained executable (mah/bytecode/bundle.py) carries the
    # program's bytes after its stub and runtime
    _info, data = bundle.split(data)
    if data.startswith(b"#!"):
        # an optional shebang line (see format.SHEBANG) -- not part of the
        # format proper, so skip it before looking for MAGIC
        newline = data.find(b"\n")
        if newline < 0:
            raise MahcFormatError("truncated file: shebang line without a newline")
        data = data[newline + 1 :]
    r = _Reader(data)
    magic = r.bytes(4)
    if magic != MAGIC:
        raise MahcFormatError(f"bad magic: expected {MAGIC!r}, got {magic!r}")
    major = r.u16()
    if major != MAJOR:
        raise MahcFormatError(f"unsupported major version {major} (this VM implements major version {MAJOR})")
    minor = r.u16()
    if minor > MINOR:
        raise MahcFormatError(_newer_minor_message(r, minor))

    payloads, debug_payload = _read_sections(r, minor)

    strings = _parse_strings(payloads[SEC_STRINGS])
    constants = _parse_constants(payloads[SEC_CONSTANTS], len(strings))
    types = _parse_types(payloads[SEC_TYPES], len(strings))
    natives = _parse_natives(payloads[SEC_NATIVES], len(strings))
    functions = _parse_functions(payloads[SEC_FUNCTIONS], len(strings))
    if minor >= 1:
        # M16: PARAMS is required from minor 1 -- `_read_sections` already
        # guarantees `payloads[SEC_PARAMS]` exists whenever we get here.
        functions = _parse_params(payloads[SEC_PARAMS], functions, len(strings), minor)

    ctx = {
        "nstrings": len(strings),
        "strings": strings,
        "nconsts": len(constants),
        "ntypes": len(types),
        "types": types,
        "nfunctions": len(functions),
        "param_counts": [f.param_count for f in functions],
        "nnatives": len(natives),
        "natives": natives,
        "minor": minor,
        "builtin_types": builtin_types_for(minor),
    }
    code = _parse_code(payloads[SEC_CODE], ctx)

    handlers = []
    if minor >= 4:
        # M25: HANDLERS is required from minor 4 -- `_read_sections` already
        # guarantees `payloads[SEC_HANDLERS]` exists whenever we get here.
        handlers = _parse_handlers(payloads[SEC_HANDLERS], len(code))

    debug = _parse_debug(debug_payload, len(strings)) if debug_payload is not None else None

    tests = []
    if SEC_TESTS in payloads:
        if not functions:
            raise MahcFormatError("TESTS section in a file with no functions")
        tests = _parse_tests(payloads[SEC_TESTS], len(strings), functions[0].slot_count)

    meta = _parse_meta(payloads[SEC_META], functions, types, ctx) if SEC_META in payloads else None

    return Program(
        strings,
        constants,
        types,
        natives,
        functions,
        code,
        debug,
        minor=minor,
        handlers=handlers,
        tests=tests,
        meta=meta,
    )
