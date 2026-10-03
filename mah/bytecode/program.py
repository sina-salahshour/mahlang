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
    # M41c (1.16, PARAMS flag bits 1/2): which of the last parameters are
    # rest parameters -- bit 0 (1): the `...` positional rest, bit 1 (2): the
    # `**` keyword rest. 0 for every function without one.
    rest: int = 0


@dataclass
class TypeRef:
    """M41a (docs/MAHC_FORMAT.md #4.10): a written type annotation, resolved
    to the declarations it names. `tag`: 0 unknown, 1 named (`kind`, `index`,
    `args`), 2 fn (`args` = parameter types, `ret`, `throws`), 3 param
    (`name`), 4 self, 5 never, 6 trait (`name`, `args`). `name` is a string
    index; `throws` is None (no clause) or a list of TypeRef."""

    tag: int
    kind: int = 0
    index: int = 0
    name: int | None = None
    args: list = field(default_factory=list)
    ret: object = None
    throws: list | None = None


@dataclass
class ParamMeta:
    type: TypeRef
    doc: int | None = None  # string index
    # 0 = no default, 1 = a default that isn't constant, 2 = a constant
    # default (`const` is its CONSTANTS index).
    default: int = 0
    const: int | None = None


@dataclass
class FnMeta:
    """One function's entry in META. `has_meta` False: nothing else follows
    in the file (and the fields below are their empty values)."""

    has_meta: bool = False
    doc: int | None = None
    type_params: list = field(default_factory=list)  # string indices
    params: list = field(default_factory=list)  # list[ParamMeta]
    returns: TypeRef = field(default_factory=lambda: TypeRef(0))
    throws: list | None = None  # None = no clause, else list[TypeRef]


@dataclass
class TypeMeta:
    """A user type's entry in META: `body` is, for a struct, a list of
    `(TypeRef, doc str index or None)` per field; for an enum, a list of
    `(doc str index or None, [TypeRef per field])` per variant."""

    doc: int | None = None
    type_params: list = field(default_factory=list)
    body: list = field(default_factory=list)


@dataclass
class Meta:
    functions: list = field(default_factory=list)  # list[FnMeta], one per function
    types: list = field(default_factory=list)  # list[TypeMeta], one per user type


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
    # M25 (1.4, docs/MAHC_FORMAT.md #4.8): list[tuple[int, int, int, int]] --
    # (start, end, handler, slot), in section order (innermost-first --
    # see compiler/codegen.py's region-closing order). Always `[]` for
    # minor < 4 (no HANDLERS section at all).
    handlers: list = field(default_factory=list)
    # M28 (docs/MAHC_FORMAT.md #4.9): list[TestEntry] -- the optional TESTS
    # section, present only in a file built for `mah test`.
    tests: list = field(default_factory=list)
    # M41a (docs/MAHC_FORMAT.md #4.10): the optional META section, or None.
    meta: object = None  # Meta | None


@dataclass
class TestEntry:
    name: int  # string index
    slot: int  # the main frame slot holding the test's closure
    line: int  # the `test` keyword's line in the entry file (0 = unknown)
