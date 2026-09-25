"""compiler/codegen.py's flat IR tuples (`(op, arg1, arg2, dest)`, see that
module's docstring) -> `Program` (docs/MAHC_FORMAT.md). The mapping is
strictly **1:1 per instruction**: instruction `i` of `buf.code` becomes
instruction `i` of the resulting `Program.code`, so jump targets and
closure entry addresses need no remapping at all -- only their operands
change shape (an IR tuple's raw Python values become string/constant/type/
function *indices*).

Strings are interned in first-use order; constants are deduplicated by
`(tag, payload)`, keyed on the tag first specifically because Python's
`True == 1` would otherwise collide a Bool constant with an INT constant of
the same numeric value. Natives appear in the NATIVES table in first-use
order, only if actually used by some instruction.
"""

from __future__ import annotations

import os
from decimal import Decimal

from ..preprocessor import BUFFER_PATH, PRELUDE_PATH, demangle_message
from ..runtime_values import NONE_VALUE
from .format import MINOR, NATIVE_ARITIES, TAG_DEC, TAG_FALSE, TAG_INT, TAG_NONE, TAG_STR, TAG_TRUE
from .program import Const, DebugInfo, FunctionDecl, Instr, NativeRef, Program, TypeDecl

# IR op -> bytecode op, for the binary/comparison ops whose bytecode
# mnemonic differs from the IR's own operator spelling (M14_SPEC.md's
# lowering table).
_BINOP_NAMES = {
    "+": "add", "-": "sub", "*": "mul", "/": "div", "//": "idiv", "%": "mod", "**": "pow",
    "eq": "eq", "neq": "neq", "lt": "lt", "gt": "gt", "and": "and", "or": "or",
    "le": "le", "ge": "ge",  # M17 (1.2)
}


def line_col(text: str, offset: int) -> tuple[int, int]:
    """1-based `(line, col)` of `offset` within `text` -- the exact
    computation docs/MAHC_FORMAT.md #4.7's DEBUG section requires (and
    which `mah/cli/main.py`'s compile-time error labelling reuses, fixing
    the old `find_error_line`'s off-by-one column)."""
    if offset < 0:
        offset = 0
    line = text.count("\n", 0, offset) + 1
    last_nl = text.rfind("\n", 0, offset)
    col = offset - last_nl
    return line, col


class _Lowerer:
    def __init__(self, resolver, pp, target: str, buf):
        self.resolver = resolver
        self.pp = pp
        self.target = target
        self.buf = buf

        self.strings: list[str] = []
        self._string_index: dict[str, int] = {}
        self.constants: list[Const] = []
        self._const_index: dict[tuple, int] = {}
        self.natives: list[NativeRef] = []
        self._native_index: dict[str, int] = {}

        self.types: list[TypeDecl] = []
        self.struct_index: dict[str, int] = {}
        self.enum_index: dict[str, int] = {}

        self.functions: list[FunctionDecl] = [FunctionDecl(0, buf.global_slot_count, 0, None, params=[])]
        self._closure_function_index: dict[int, int] = {}

        self.code: list[Instr] = []

        self._files: list[int] = []
        self._file_index: dict[str, int] = {}

    # -- interning ---------------------------------------------------------

    def intern_str(self, s: str) -> int:
        idx = self._string_index.get(s)
        if idx is not None:
            return idx
        idx = len(self.strings)
        self.strings.append(s)
        self._string_index[s] = idx
        return idx

    def intern_const(self, value) -> int:
        if value is NONE_VALUE:
            tag, payload = TAG_NONE, None
        elif value is False:
            tag, payload = TAG_FALSE, None
        elif value is True:
            tag, payload = TAG_TRUE, None
        elif isinstance(value, str):
            tag, payload = TAG_STR, self.intern_str(value)
        elif isinstance(value, (Decimal, int)):
            d = value if isinstance(value, Decimal) else Decimal(value)
            if d == d.to_integral_value():
                tag, payload = TAG_INT, int(d)
            else:
                tag, payload = TAG_DEC, self.intern_str(format(d.normalize(), "f"))
        else:
            raise AssertionError(f"unsupported constant value {value!r}")
        key = (tag, payload)
        idx = self._const_index.get(key)
        if idx is not None:
            return idx
        idx = len(self.constants)
        self.constants.append(Const(tag, payload))
        self._const_index[key] = idx
        return idx

    def intern_native(self, name: str) -> int:
        idx = self._native_index.get(name)
        if idx is not None:
            return idx
        idx = len(self.natives)
        self.natives.append(NativeRef(self.intern_str(name), NATIVE_ARITIES[name]))
        self._native_index[name] = idx
        return idx

    # -- TYPES ---------------------------------------------------------

    def build_types(self) -> None:
        next_idx = 2
        for name, fields in self.resolver.struct_decls.items():
            self.struct_index[name] = next_idx
            next_idx += 1
            self.types.append(TypeDecl(0, self.intern_str(name), [self.intern_str(f) for f in fields], None))
        # Option/Promise are pre-seeded into resolver.enum_decls with the
        # exact same variant order as format.BUILTIN_TYPES (asserted here,
        # once, rather than trusted silently -- see this module's
        # docstring and docs/MAHC_FORMAT.md #4.3).
        assert list(self.resolver.enum_decls["Option"].items()) == [("none", []), ("some", ["value"])]
        assert list(self.resolver.enum_decls["Promise"].items()) == [("Pending", []), ("Settled", ["value"])]
        self.enum_index["Option"] = 0
        self.enum_index["Promise"] = 1
        for name, variants in self.resolver.enum_decls.items():
            if name in ("Option", "Promise"):
                continue
            self.enum_index[name] = next_idx
            next_idx += 1
            variant_list = [
                (self.intern_str(vname), [self.intern_str(f) for f in vfields])
                for vname, vfields in variants.items()
            ]
            self.types.append(TypeDecl(1, self.intern_str(name), None, variant_list))

    def _variant_index(self, type_name: str, variant_name: str) -> int:
        return list(self.resolver.enum_decls[type_name].keys()).index(variant_name)

    # -- FUNCTIONS / CODE ------------------------------------------------

    def _function_index_for(
        self, code_addr: int, slot_count: int, param_count: int, name, param_names, has_defaults
    ) -> int:
        idx = self._closure_function_index.get(code_addr)
        if idx is not None:
            return idx
        demangled = demangle_message(name) if name else None
        name_idx = self.intern_str(demangled) if demangled else None
        # M16: PARAMS section data for this function -- (name string index,
        # has_default) per parameter, in order.
        params = [
            (self.intern_str(pname), bool(has_default))
            for pname, has_default in zip(param_names, has_defaults)
        ]
        idx = len(self.functions)
        self.functions.append(FunctionDecl(code_addr, slot_count, param_count, name_idx, params=params))
        self._closure_function_index[code_addr] = idx
        return idx

    def lower_code(self) -> None:
        n = len(self.buf.code)
        out = []
        for i, instr in enumerate(self.buf.code):
            if instr == (None, None, None, None):
                if i != n - 1:
                    raise AssertionError(
                        f"unpatched placeholder instruction at index {i} (only valid as the last instruction)"
                    )
                out.append(Instr("halt", ()))
                continue
            out.append(self._lower_one(instr))
        self.code = out

    def _lower_one(self, instr: tuple) -> Instr:
        op, a1, a2, a3 = instr

        if op in _BINOP_NAMES:
            return Instr(_BINOP_NAMES[op], (a1, a2, a3))
        if op == "=":
            return Instr("move", (a1, a3))
        if op == "ld":
            return Instr("loadk", (self.intern_const(a1), a3))
        if op == "jmp":
            return Instr("jmp", (a3,))
        if op == "jmpf":
            return Instr("jmpf", (a1, a3))
        if op == "jmpset":
            # M16: codegen repurposes `jmpset`'s IR shape like `jmpf`'s --
            # arg1 the param slot address, dest the jump target.
            return Instr("jmpset", (a1, a3))
        if op == "neg":
            return Instr("neg", (a1, a3))
        if op == "not":
            return Instr("not", (a1, a3))
        if op == "closure":
            slot_count, param_count, name, param_names, has_defaults = a2
            fn_idx = self._function_index_for(a1, slot_count, param_count, name, param_names, has_defaults)
            return Instr("closure", (fn_idx, a3))
        if op == "call":
            return Instr("call", (a1, a2))
        if op == "callkw":
            arg_addrs, kw_names = a2
            return Instr("callkw", (a1, arg_addrs, tuple(self.intern_str(n) for n in kw_names)))
        if op == "ret":
            return Instr("ret", (a1,))
        if op == "retval":
            return Instr("retval", (a3,))
        if op == "callmethod":
            name, args, trait, _pos = a2
            return Instr(
                "callmethod",
                (a1, self.intern_str(name), args, self.intern_str(trait) if trait is not None else None),
            )
        if op == "callmethodkw":
            name, args, kw_names, trait, _pos = a2
            return Instr(
                "callmethodkw",
                (
                    a1,
                    self.intern_str(name),
                    args,
                    tuple(self.intern_str(n) for n in kw_names),
                    self.intern_str(trait) if trait is not None else None,
                ),
            )
        if op == "defmethod":
            type_name, trait, name, is_method = a2
            return Instr(
                "defmethod",
                (
                    a1,
                    self.intern_str(type_name),
                    self.intern_str(trait) if trait is not None else None,
                    self.intern_str(name),
                    bool(is_method),
                ),
            )
        if op == "detach":
            return Instr("detach", (a1, a2, a3))
        if op == "detachkw":
            arg_addrs, kw_names = a2
            return Instr("detachkw", (a1, arg_addrs, tuple(self.intern_str(n) for n in kw_names), a3))
        if op == "detachmethod":
            name, args, trait, _pos = a2
            return Instr(
                "detachmethod",
                (a1, self.intern_str(name), args, self.intern_str(trait) if trait is not None else None, a3),
            )
        if op == "detachmethodkw":
            name, args, kw_names, trait, _pos = a2
            return Instr(
                "detachmethodkw",
                (
                    a1,
                    self.intern_str(name),
                    args,
                    tuple(self.intern_str(n) for n in kw_names),
                    self.intern_str(trait) if trait is not None else None,
                    a3,
                ),
            )
        if op == "await":
            return Instr("await", (a1, a3))
        if op == "struct":
            type_name, pairs = a1, a2
            t = self.struct_index[type_name]
            declared = self.resolver.struct_decls[type_name]
            pairs_dict = dict(pairs)
            values = tuple(pairs_dict[f] for f in declared)
            return Instr("struct", (t, values, a3))
        if op == "enum":
            type_name = a1
            variant, pairs = a2
            t = self.enum_index[type_name]
            declared = self.resolver.enum_decls[type_name][variant]
            pairs_dict = dict(pairs)
            values = tuple(pairs_dict[f] for f in declared)
            n = self._variant_index(type_name, variant)
            return Instr("enum", (t, n, values, a3))
        if op == "getfield":
            return Instr("getfield", (a1, self.intern_str(a2), a3))
        if op == "setfield":
            # codegen repurposes the 4th slot as the SOURCE address here,
            # not a destination -- see codegen.py's module docstring.
            return Instr("setfield", (a1, self.intern_str(a2), a3))
        if op == "matchtag":
            kind, type_name, variant = a2
            if kind == "struct":
                return Instr("matchstruct", (a1, self.struct_index[type_name], a3))
            t = self.enum_index[type_name]
            n = self._variant_index(type_name, variant)
            return Instr("matchenum", (a1, t, n, a3))
        if op == "matchfail":
            return Instr("matchfail", ())
        if op == "matchrange":
            lo, hi, inclusive = a2
            return Instr("matchrange", (a1, lo, hi, bool(inclusive), a3))
        if op == "deferpush":
            return Instr("deferpush", ())
        if op == "deferadd":
            return Instr("deferadd", (a1,))
        if op == "deferpeek":
            return Instr("deferpeek", (a3,))
        if op == "deferpopclosure":
            return Instr("deferpop", (a3,))
        if op == "deferscopepop":
            return Instr("deferscopepop", ())
        if op == "print":
            return Instr("native", (self.intern_native("io.print"), (a1,), None))
        if op == "write":
            # M16 (1.1): `print`'s own codegen now emits a `write` IR op per
            # piece (each arg's to_string, then sep, ..., then end) instead
            # of the old one-arg-per-line `print` IR op -- see codegen.py's
            # module docstring/`_gen_print`.
            return Instr("native", (self.intern_native("io.write"), (a1,), None))
        if op == "input":
            return Instr("native", (self.intern_native("io.input"), (), a3))
        if op == "sin":
            return Instr("native", (self.intern_native("math.sin"), (a1,), a3))
        if op == "cos":
            return Instr("native", (self.intern_native("math.cos"), (a1,), a3))
        if op == "sleepasync":
            return Instr("native", (self.intern_native("time.sleep_async"), (a1,), a3))
        raise AssertionError(f"unhandled IR op {op!r}")

    # -- DEBUG ---------------------------------------------------------

    def _file_idx(self, path: str) -> int:
        idx = self._file_index.get(path)
        if idx is not None:
            return idx
        if path == PRELUDE_PATH:
            # M17: the prelude's DEBUG file name is the fixed sentinel
            # `"<prelude>"`, never its real on-disk path -- a runtime error
            # raised inside prelude code is then located at
            # `<prelude>#L:C` (docs/MAHC_FORMAT.md #6.8), independent of
            # where this Mah installation happens to keep prelude.mh.
            name = "<prelude>"
        elif path == self.pp.entry_path:
            name = "<buffer>" if path == BUFFER_PATH else os.path.basename(path)
        else:
            base_dir = os.path.dirname(self.pp.entry_path) if self.pp.entry_path != BUFFER_PATH else os.getcwd()
            name = os.path.relpath(path, base_dir)
        idx = len(self._files)
        self._files.append(self.intern_str(name))
        self._file_index[path] = idx
        return idx

    def _position_for(self, offset: int | None) -> tuple[int, int, int]:
        if offset is None:
            return (0, 0, 0)
        path, src_offset = self.pp.map_to_source(offset)
        text = self.pp.files.get(path, "")
        line, col = line_col(text, src_offset)
        return (self._file_idx(path), line, col)

    def build_debug(self) -> DebugInfo:
        self._file_idx(self.pp.entry_path)  # file 0 is always the entry file
        runs: list[tuple[int, int, int, int]] = []
        prev = None
        positions = self.buf.positions
        for i in range(len(self.buf.code)):
            pos = positions[i] if i < len(positions) else None
            cur = self._position_for(pos)
            if cur != prev:
                runs.append((i, *cur))
                prev = cur
        return DebugInfo(files=list(self._files), runs=runs)


def lower(buf, resolver, pp, target: str = "debug") -> Program:
    lowerer = _Lowerer(resolver, pp, target, buf)
    lowerer.build_types()
    lowerer.lower_code()
    debug = lowerer.build_debug() if target == "debug" else None
    return Program(
        strings=lowerer.strings,
        constants=lowerer.constants,
        types=lowerer.types,
        natives=lowerer.natives,
        functions=lowerer.functions,
        code=lowerer.code,
        debug=debug,
        minor=MINOR,
    )
