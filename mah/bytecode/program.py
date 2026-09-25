"""`Program` -- a plain data model mirroring the `.mahc` file 1:1 (raw
indices, no resolution to strings/runtime values -- that happens in
code_interpreter.py's own link step). See docs/MAHC_FORMAT.md #4 for what
each field means; field shapes here mirror M14_SPEC.md's `program.py`
section exactly.

Every dataclass uses value equality (`eq=True`, the dataclass default) --
`decode(encode(p)) == p` (tests/test_bytecode.py) depends on it.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Const:
    tag: int
    # None for none/false/true; int for an INT constant's value; int
    # (a string-table index) for a DEC constant's decimal text or a STR
    # constant's text -- see docs/MAHC_FORMAT.md #4.2.
    value: object = None


@dataclass
class TypeDecl:
    kind: int  # 0 = struct, 1 = enum
    name: int  # string index
    fields: list | None = None  # kind 0: list[int] (field name string indices)
    variants: list | None = None  # kind 1: list[tuple[int, list[int]]] (name, field names)


@dataclass
class NativeRef:
    name: int  # string index
    arity: int


@dataclass
class FunctionDecl:
    entry: int
    slot_count: int
    param_count: int
    name: int | None = None  # string index, or None (anonymous)
    # M16 (1.1, PARAMS section, docs/MAHC_FORMAT.md #4.5a): list[tuple[int,
    # bool]] -- (parameter name string index, has_default), in parameter
    # order, length always == param_count. `None` for a 1.0 file (no PARAMS
    # section at all -- every parameter is unnamed and required).
    params: list | None = None


@dataclass
class Instr:
    op: str
    args: tuple = ()


@dataclass
class DebugInfo:
    files: list = field(default_factory=list)  # list[int] (string indices); file 0 = entry file
    # Absolute (pc, file, line, col) -- delta-encoding pc against the
    # previous run is purely an encoding-time detail (encode.py/decode.py),
    # not part of this in-memory model.
    runs: list = field(default_factory=list)


@dataclass
class Program:
    strings: list
    constants: list
    types: list
    natives: list
    functions: list
    code: list
    debug: object = None  # DebugInfo | None
    minor: int = 0
