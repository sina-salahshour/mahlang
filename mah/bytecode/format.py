"""Format constants for `.mahc` version 1.3 -- the single source of truth
mirroring docs/MAHC_FORMAT.md. Every other module in this package (and the
VM, `mah/code_interpreter.py`) imports its constants from here rather than
hard-coding a section id/opcode/tag number a second time.

M16 bumped MINOR to 1: default parameter values + keyword-argument calls
(the PARAMS section, `jmpset`, the `*kw` opcodes, operand kind `S*`) and
the `io.write` native -- see docs/MAHC_FORMAT.md #4.5a/#4.6/#7.

M17 bumps MINOR to 2: `matchrange` (range patterns), the `le`/`ge`/`not`
operators, and the native inherent methods `String.len`/`String.char_at`/
`Function.arity` (§6.7) -- see docs/MAHC_FORMAT.md #4.6/#6.3/#6.7/#7.
Ranges, iterators, and their adapters aren't VM features at all: they're
ordinary Mah code (the compiler's prelude, `mah/std/prelude.mh`) compiled
like any user program.

M19 bumps MINOR to 3: Vectors and Maps -- the `vector`/`map` opcodes, the
built-in types `Vector`/`Map`, their native inherent methods, and the
`Index`/`IndexAssign` system traits (docs/MAHC_FORMAT.md #6.7/#6.9).
"""

from __future__ import annotations

MAGIC = b"MAHC"
MAJOR = 1
MINOR = 3

# -- section ids (docs/MAHC_FORMAT.md #3) -----------------------------------
SEC_STRINGS = 0x01
SEC_CONSTANTS = 0x02
SEC_TYPES = 0x03
SEC_NATIVES = 0x04
SEC_FUNCTIONS = 0x05
SEC_CODE = 0x06
SEC_PARAMS = 0x07  # M16 (1.1): required iff minor >= 1 -- docs/MAHC_FORMAT.md #4.5a
SEC_DEBUG = 0x80

REQUIRED_SECTIONS = (SEC_STRINGS, SEC_CONSTANTS, SEC_TYPES, SEC_NATIVES, SEC_FUNCTIONS, SEC_CODE)
# M16: PARAMS joins the required-section list only for minor >= 1 files --
# see decode.py's `_read_sections`, which picks between this and
# `REQUIRED_SECTIONS` once it has read the file's own minor version.
REQUIRED_SECTIONS_V1 = REQUIRED_SECTIONS + (SEC_PARAMS,)

# -- constant tags (docs/MAHC_FORMAT.md #4.2) -------------------------------
TAG_NONE = 0
TAG_FALSE = 1
TAG_TRUE = 2
TAG_INT = 3
TAG_DEC = 4
TAG_STR = 5


class MahcFormatError(Exception):
    """Raised by decode.py for any structurally invalid `.mahc` file, and by
    code_interpreter.py's loader when a file declares a native the running
    VM doesn't implement (or implements with a different arity) -- see
    docs/MAHC_FORMAT.md #3 ("fail fast")."""


# -- built-in types (docs/MAHC_FORMAT.md #4.3) ------------------------------
# Type indices 0/1 are implicit -- never written in the TYPES section -- and
# every conforming VM/encoder must agree on this exact layout. Each entry:
# (type_name, [(variant_name, [field_name, ...]), ...]).
BUILTIN_TYPES = (
    ("Option", (("none", ()), ("some", ("value",)))),
    ("Promise", (("Pending", ()), ("Settled", ("value",)))),
)

# -- natives (docs/MAHC_FORMAT.md #4.4) -------------------------------------
NATIVE_ARITIES = {
    "io.print": 1,
    "io.write": 1,  # M16 (1.1)
    "io.input": 0,
    "math.sin": 1,
    "math.cos": 1,
    "time.sleep_async": 1,
}

# M16: which minor version introduced each 1.1+ native -- a 1.0 file
# ('minor' == 0) using one is rejected at load (docs/MAHC_FORMAT.md #4.6's
# "must not appear in a file whose minor version is 0"). Absent = present
# since 1.0.
NATIVE_SINCE_MINOR = {
    "io.write": 1,
}

# -- opcodes (docs/MAHC_FORMAT.md #4.6) --------------------------------------
# name -> (code, operand_kinds); operand kinds use the letters of #4.6's
# table ("A", "A?", "A*", "K", "S", "S?", "L", "F", "T", "N", "X", "B"), in
# encoding order, exactly mirroring the opcode table there.
OPCODES: dict[str, tuple[int, tuple[str, ...]]] = {
    "halt": (0x00, ()),
    "move": (0x01, ("A", "A")),
    "loadk": (0x02, ("K", "A")),
    "jmp": (0x03, ("L",)),
    "jmpf": (0x04, ("A", "L")),
    "jmpset": (0x05, ("A", "L")),  # M16 (1.1)
    "add": (0x10, ("A", "A", "A")),
    "sub": (0x11, ("A", "A", "A")),
    "mul": (0x12, ("A", "A", "A")),
    "div": (0x13, ("A", "A", "A")),
    "idiv": (0x14, ("A", "A", "A")),
    "mod": (0x15, ("A", "A", "A")),
    "pow": (0x16, ("A", "A", "A")),
    "eq": (0x17, ("A", "A", "A")),
    "neq": (0x18, ("A", "A", "A")),
    "lt": (0x19, ("A", "A", "A")),
    "gt": (0x1A, ("A", "A", "A")),
    "and": (0x1B, ("A", "A", "A")),
    "or": (0x1C, ("A", "A", "A")),
    "neg": (0x1D, ("A", "A")),
    "le": (0x1E, ("A", "A", "A")),  # M17 (1.2)
    "ge": (0x1F, ("A", "A", "A")),  # M17 (1.2)
    "not": (0x0F, ("A", "A")),  # M17 (1.2)
    "closure": (0x20, ("F", "A")),
    "call": (0x21, ("A", "A*")),
    "ret": (0x22, ("A",)),
    "retval": (0x23, ("A",)),
    "callkw": (0x26, ("A", "A*", "S*")),  # M16 (1.1)
    "callmethod": (0x24, ("A", "S", "A*", "S?")),
    "defmethod": (0x25, ("A", "S", "S?", "S", "B")),
    "callmethodkw": (0x27, ("A", "S", "A*", "S*", "S?")),  # M16 (1.1)
    "detach": (0x28, ("A", "A*", "A")),
    "detachmethod": (0x29, ("A", "S", "A*", "S?", "A")),
    "await": (0x2A, ("A", "A")),
    "detachkw": (0x2B, ("A", "A*", "S*", "A")),  # M16 (1.1)
    "detachmethodkw": (0x2C, ("A", "S", "A*", "S*", "S?", "A")),  # M16 (1.1)
    "struct": (0x30, ("T", "A*", "A")),
    "enum": (0x31, ("T", "N", "A*", "A")),
    "getfield": (0x32, ("A", "S", "A")),
    "setfield": (0x33, ("A", "S", "A")),
    "matchstruct": (0x34, ("A", "T", "A")),
    "matchenum": (0x35, ("A", "T", "N", "A")),
    "matchfail": (0x36, ()),
    "matchrange": (0x37, ("A", "A?", "A?", "B", "A")),  # M17 (1.2)
    "vector": (0x38, ("A*", "A")),  # M19 (1.3)
    "map": (0x39, ("A*", "A")),  # M19 (1.3)
    "deferpush": (0x40, ()),
    "deferadd": (0x41, ("A",)),
    "deferpeek": (0x42, ("A",)),
    "deferpop": (0x43, ("A",)),
    "deferscopepop": (0x44, ()),
    "native": (0x50, ("X", "A*", "A?")),
}

OPCODES_BY_CODE: dict[int, tuple[str, tuple[str, ...]]] = {
    code: (name, kinds) for name, (code, kinds) in OPCODES.items()
}

# M16: which minor version introduced each opcode -- mirrors
# NATIVE_SINCE_MINOR above; a 1.0 file using one of these is rejected at
# load (docs/MAHC_FORMAT.md #4.6). Absent = present since 1.0.
OPCODE_SINCE_MINOR: dict[str, int] = {
    "jmpset": 1,
    "callkw": 1,
    "callmethodkw": 1,
    "detachkw": 1,
    "detachmethodkw": 1,
    "le": 2,
    "ge": 2,
    "not": 2,
    "matchrange": 2,
    "vector": 3,
    "map": 3,
}
