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

from ..compiler.ast_nodes import BoolLit, EnumLit, FnExpr, FnType, NamedType, NumberLit, StringLit, Unary
from ..preprocessor import BUFFER_PATH, PRELUDE_PATH, demangle_message, source_label, std_module_name
from ..runtime_values import NONE_VALUE, PRIMITIVE_TYPE_NAMES
from .format import (
    METHOD_CALL_OPCODES,
    MINOR,
    NATIVE_ARITIES,
    NATIVE_METHOD_SINCE_MINOR,
    NATIVE_SINCE_MINOR,
    OPCODE_SINCE_MINOR,
    TAG_DEC,
    TAG_FALSE,
    TAG_INT,
    TAG_NONE,
    TAG_STR,
    TAG_TRUE,
)
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
        self.file_minor = 17  # set by `lower` before META is built
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
        # M41b: FUNCTIONS index by the FnExpr's identity, for `decorate`.
        self._fn_index_by_ast: dict[int, int] = {}
        # M41a: what each function's META entry is built from -- function
        # index -> (FnExpr, type parameter names in scope); function 0 (the
        # main program) has none.
        self._fn_meta_sources: dict[int, tuple] = {}
        # M41a: `("struct" | "enum", name)` per entry of `self.types`.
        self._type_keys: list = []

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
        # M25 (docs/MAHC_FORMAT.md #4.1): a minor-4 file's built-ins are
        # Option (0), Promise (1, now with `Failed`), RuntimeError (2) --
        # user types start at 3. `lower()` always writes the CURRENT
        # `MINOR` (4), so this is unconditional (no older-minor output
        # mode exists). Pre-seeded into resolver.enum_decls with the exact
        # same variant order as format.BUILTIN_TYPES_V4 (asserted here,
        # once, rather than trusted silently -- see this module's
        # docstring).
        assert list(self.resolver.enum_decls["Option"].items()) == [("none", []), ("some", ["value"])]
        assert list(self.resolver.enum_decls["Promise"].items()) == [
            ("Pending", []),
            ("Settled", ["value"]),
            ("Failed", ["error"]),
        ]
        assert list(self.resolver.enum_decls["RuntimeError"].items()) == [
            (name, ["message"])
            for name in (
                "DivisionByZero",
                "TypeMismatch",
                "NoSuchField",
                "NoSuchMethod",
                "ArgumentError",
                "IndexOutOfRange",
                "MatchFailed",
                "InputError",
                "Internal",
            )
        ]
        self.enum_index["Option"] = 0
        self.enum_index["Promise"] = 1
        self.enum_index["RuntimeError"] = 2
        skip = ("Option", "Promise", "RuntimeError")
        next_idx = 3
        for name, fields in self.resolver.struct_decls.items():
            self.struct_index[name] = next_idx
            next_idx += 1
            self._type_keys.append(("struct", name))
            self.types.append(TypeDecl(0, self.intern_str(name), [self.intern_str(f) for f in fields], None))
        for name, variants in self.resolver.enum_decls.items():
            if name in skip:
                continue
            self.enum_index[name] = next_idx
            next_idx += 1
            self._type_keys.append(("enum", name))
            variant_list = [
                (self.intern_str(vname), [self.intern_str(f) for f in vfields])
                for vname, vfields in variants.items()
            ]
            self.types.append(TypeDecl(1, self.intern_str(name), None, variant_list))

    def _variant_index(self, type_name: str, variant_name: str) -> int:
        return list(self.resolver.enum_decls[type_name].keys()).index(variant_name)

    def _type_index(self, type_name: str) -> int:
        """M25: the TYPES-section index for a `matchtype` -- `type_name`
        names either a struct or an enum (a `TypePat`'s target, validated
        by compiler/resolve.py against both namespaces). A struct and an
        enum may share a name (docs/MAHC_FORMAT.md #4.3); struct wins,
        exactly like compiler/resolve.py's own `type_position_index`
        registration for a `TypePat` does."""
        if type_name in self.struct_index:
            return self.struct_index[type_name]
        return self.enum_index[type_name]

    # -- FUNCTIONS / CODE ------------------------------------------------

    def _function_index_for(
        self, code_addr: int, slot_count: int, param_count: int, name, param_names, has_defaults, meta_source=None,
        rest: int = 0,
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
        self.functions.append(FunctionDecl(code_addr, slot_count, param_count, name_idx, params=params, rest=rest))
        if meta_source is not None:
            self._fn_meta_sources[idx] = meta_source
            self._fn_index_by_ast[id(meta_source[0])] = idx
        self._closure_function_index[code_addr] = idx
        return idx

    def lower_code(self) -> None:
        # M25: the single `halt` sentinel `Codegen.generate` emits is no
        # longer guaranteed to be the LAST instruction -- a top-level
        # `defer` gets an implicit F_HANDLER (docs/MAHC_FORMAT.md #5.5)
        # emitted right after it, reached only by unwinding, never by
        # fallthrough. Every unpatched `(None, None, None, None)`
        # placeholder that survives to here is `halt`, wherever it sits;
        # every OTHER placeholder (jumps, `jmpset`) is backpatched by
        # codegen before this ever runs.
        out = []
        decorations = []
        for pc, instr in enumerate(self.buf.code):
            if instr == (None, None, None, None):
                out.append(Instr("halt", ()))
                continue
            if instr[0] in ("decorate", "paramhooks") or (
                instr[0] == "defmethod" and isinstance(instr[2][0], FnExpr)
            ):
                # M41b/M41c: needs every closure lowered first (function indices)
                decorations.append(pc)
                out.append(None)
                continue
            out.append(self._lower_one(instr))
        for pc in decorations:
            instr = self.buf.code[pc]
            if instr[0] == "decorate":
                out[pc] = self._lower_decorate(instr)
            elif instr[0] == "paramhooks":
                # M41c: `paramhooks fn, index, dest` -- the function the code sits in
                fn_ast, index = instr[1]
                out[pc] = Instr("paramhooks", (self._fn_index_by_ast[id(fn_ast)], index, instr[3]))
            else:
                # M41c: `impl somefn { ... }` -- the item type's key `fn#<index>`
                (fn_ast, trait, name, is_method) = instr[2]
                out[pc] = Instr(
                    "defmethod",
                    (
                        instr[1],
                        self.intern_str(f"fn#{self._fn_index_by_ast[id(fn_ast)]}"),
                        self.intern_str(trait) if trait is not None else None,
                        self.intern_str(name),
                        bool(is_method),
                    ),
                )
        self.code = out

    def _lower_decorate(self, instr: tuple) -> Instr:
        """M41b: `decorate kind, a, b, values` -- the target as an index pair."""
        _op, (what, owner, index), addrs, _dest = instr
        if what == "fn":
            return Instr("decorate", (0, self._fn_index_by_ast[id(owner)], 0, addrs))
        if what == "param":
            return Instr("decorate", (1, self._fn_index_by_ast[id(owner)], index, addrs))
        if what == "struct":
            return Instr("decorate", (2, self.struct_index[owner], 0, addrs))
        if what == "enum":
            return Instr("decorate", (2, self.enum_index[owner], 0, addrs))
        if what == "field":
            return Instr("decorate", (3, self.struct_index[owner], index, addrs))
        return Instr("decorate", (4, self.enum_index[owner], index, addrs))

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
            slot_count, param_count, name, param_names, has_defaults, meta_source, rest = a2
            fn_idx = self._function_index_for(
                a1, slot_count, param_count, name, param_names, has_defaults, meta_source, rest
            )
            return Instr("closure", (fn_idx, a3))
        if op == "call":
            return Instr("call", (a1, a2))
        if op == "callkw":
            arg_addrs, kw_names = a2
            return Instr("callkw", (a1, arg_addrs, tuple(self.intern_str(n) for n in kw_names)))
        if op == "callspread":
            vec, kw_map = a2
            return Instr("callspread", (a1, vec, kw_map))
        if op == "callmethodspread":
            name, vec, kw_map, trait, _pos = a2
            return Instr(
                "callmethodspread",
                (a1, self.intern_str(name), vec, kw_map, self.intern_str(trait) if trait is not None else None),
            )
        if op == "spread":
            return Instr("spread", (a1, a2, bool(a3)))
        if op == "loadtype":
            what, name = a1
            if what == "prim":
                return Instr("loadtype", (1, PRIMITIVE_TYPE_NAMES.index(name), a3))
            index = self.struct_index[name] if what == "struct" else self.enum_index[name]
            return Instr("loadtype", (0, index, a3))
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
        if op in ("vector", "map"):
            # M19: `a1` is the item (or interleaved key/value) address list.
            return Instr(op, (a1, a3))
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
        if op == "matchtype":
            return Instr("matchtype", (a1, self._type_index(a2), a3))
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
        if op == "deferdepth":
            return Instr("deferdepth", (a3,))
        if op == "deferabove":
            return Instr("deferabove", (a1, a3))
        if op == "throw":
            return Instr("throw", (a1,))
        if op == "print":
            return Instr("native", (self.intern_native("io.print"), (a1,), None))
        if op == "write":
            # M16 (1.1): `print`'s own codegen now emits a `write` IR op per
            # piece (each arg's to_string, then sep, ..., then end) instead
            # of the old one-arg-per-line `print` IR op -- see codegen.py's
            # module docstring/`_gen_print`.
            return Instr("native", (self.intern_native("io.write"), (a1,), None))
        if op == "sin":
            return Instr("native", (self.intern_native("math.sin"), (a1,), a3))
        if op == "cos":
            return Instr("native", (self.intern_native("math.cos"), (a1,), a3))
        if op == "native":
            # M27: an `extern fn` body -- a1 is the native's name, a2 its
            # argument addresses.
            return Instr("native", (self.intern_native(a1), a2, a3))
        if op == "sleepasync":
            return Instr("native", (self.intern_native("time.sleep_async"), (a1,), a3))
        raise AssertionError(f"unhandled IR op {op!r}")

    # -- META (docs/MAHC_FORMAT.md #4.10) -------------------------------------

    def _type_ref(self, texpr, scope) -> TypeRef:
        """A written annotation as META records it: resolved to the
        declaration it names, never inferred. Anything that doesn't resolve
        (a misspelt name the checker flags) is `unknown` -- metadata never
        fails a compilation."""
        if texpr is None:
            return TypeRef(0)
        if isinstance(texpr, FnType):
            ret = self._type_ref(texpr.ret, scope) if texpr.ret is not None else TypeRef(1, kind=1, index=6)
            return TypeRef(
                2,
                args=[self._type_ref(p, scope) for p in texpr.params],
                ret=ret,
                throws=self._throws_refs(texpr.throws, scope),
            )
        if not isinstance(texpr, NamedType):
            return TypeRef(0)
        name = texpr.name
        if name == "Unknown":
            return TypeRef(0)
        if name == "Never":
            return TypeRef(5)
        if name == "Self":
            return TypeRef(4)
        if name in scope:
            return TypeRef(3, name=self.intern_str(name))
        args = [self._type_ref(a, scope) for a in texpr.args]
        if name in self.struct_index:
            return TypeRef(1, kind=0, index=self.struct_index[name], args=args)
        if name in self.enum_index:
            return TypeRef(1, kind=0, index=self.enum_index[name], args=args)
        if name in PRIMITIVE_TYPE_NAMES:
            index = PRIMITIVE_TYPE_NAMES.index(name)
            if index >= 8 and self.file_minor < 17:
                # M37: a 1.14-1.16 VM reading META refuses the code 8 (Bytes),
                # so a file that doesn't otherwise need 1.17 (one importing
                # std:fs for text only, say) describes it as Unknown.
                return TypeRef(0)
            return TypeRef(1, kind=1, index=index, args=args)
        if name in self.resolver.trait_decls:
            return TypeRef(6, name=self.intern_str(name), args=args)
        return TypeRef(0)

    def _throws_refs(self, throws, scope):
        if throws is None:
            return None
        return [self._type_ref(t, scope) for t in throws]

    def _constant_default(self, expr) -> int | None:
        """The CONSTANTS index of a parameter default that is a literal
        Number, String, Bool or `none`, or `-` applied to a Number literal;
        None for anything else."""
        if isinstance(expr, (NumberLit, StringLit, BoolLit)):
            return self.intern_const(expr.value)
        if isinstance(expr, EnumLit) and expr.type_name == "Option" and expr.variant == "none" and not expr.fields:
            return self.intern_const(NONE_VALUE)
        if isinstance(expr, Unary) and expr.op == "-" and isinstance(expr.operand, NumberLit):
            return self.intern_const(-expr.operand.value)
        return None

    def _fn_meta(self, fn, scope) -> FnMeta:
        params = []
        interesting = bool(fn.doc) or bool(fn.type_params) or fn.return_type is not None or fn.throws is not None
        for i in range(len(fn.params)):
            ptype = fn.param_types[i] if i < len(fn.param_types) else None
            pdoc = fn.param_docs[i] if i < len(fn.param_docs) else None
            default = fn.defaults[i] if i < len(fn.defaults) else None
            const = None
            if default is None:
                flag = 0
            else:
                const = self._constant_default(default)
                flag = 2 if const is not None else 1
            if ptype is not None or pdoc or default is not None:
                interesting = True
            params.append(
                ParamMeta(
                    self._type_ref(ptype, scope),
                    self.intern_str(pdoc) if pdoc else None,
                    flag,
                    const,
                )
            )
        if not interesting:
            return FnMeta()
        return FnMeta(
            True,
            self.intern_str(fn.doc) if fn.doc else None,
            [self.intern_str(tp.name) for tp in fn.type_params],
            params,
            self._type_ref(fn.return_type, scope),
            self._throws_refs(fn.throws, scope),
        )

    def _type_meta(self, key) -> TypeMeta:
        kind, name = key
        if kind == "struct":
            decl = self.buf.struct_asts.get(name)
            fields = self.resolver.struct_decls[name]
            if decl is None:
                return TypeMeta(None, [], [(TypeRef(0), None) for _ in fields])
            scope = frozenset(tp.name for tp in decl.type_params)
            body = []
            for i in range(len(fields)):
                ftype = decl.field_types[i] if i < len(decl.field_types) else None
                fdoc = decl.field_docs[i] if i < len(decl.field_docs) else None
                body.append((self._type_ref(ftype, scope), self.intern_str(fdoc) if fdoc else None))
        else:
            decl = self.buf.enum_asts.get(name)
            variants = self.resolver.enum_decls[name]
            if decl is None:
                return TypeMeta(None, [], [(None, [TypeRef(0) for _ in vf]) for vf in variants.values()])
            scope = frozenset(tp.name for tp in decl.type_params)
            body = []
            for i, (_vname, vfields) in enumerate(decl.variants):
                types = decl.variant_field_types[i] if i < len(decl.variant_field_types) else []
                vdoc = decl.variant_docs[i] if i < len(decl.variant_docs) else None
                body.append(
                    (
                        self.intern_str(vdoc) if vdoc else None,
                        [self._type_ref(types[j] if j < len(types) else None, scope) for j in range(len(vfields))],
                    )
                )
        return TypeMeta(
            self.intern_str(decl.doc) if decl.doc else None,
            [self.intern_str(tp.name) for tp in decl.type_params],
            body,
        )

    def build_meta(self) -> Meta:
        functions = []
        for idx in range(len(self.functions)):
            source = self._fn_meta_sources.get(idx)
            functions.append(FnMeta() if source is None else self._fn_meta(*source))
        return Meta(functions, [self._type_meta(key) for key in self._type_keys])

    # -- DEBUG ---------------------------------------------------------

    def _file_idx(self, path: str) -> int:
        idx = self._file_index.get(path)
        if idx is not None:
            return idx
        if path == PRELUDE_PATH or std_module_name(path) is not None:
            # M17: the prelude's DEBUG file name is the fixed sentinel
            # `"<prelude>"`, never its real on-disk path -- a runtime error
            # raised inside prelude code is then located at
            # `<prelude>#L:C` (docs/MAHC_FORMAT.md #6.8), independent of
            # where this Mah installation happens to keep prelude.mh. M27:
            # a standard library module is likewise `std:<name>`.
            name = source_label(path)
        elif path == self.pp.entry_path:
            name = "<buffer>" if path == BUFFER_PATH else os.path.basename(path)
        else:
            base_dir = os.path.dirname(self.pp.entry_path) if self.pp.entry_path != BUFFER_PATH else os.getcwd()
            name = os.path.relpath(path, base_dir)
        idx = len(self._files)
        self._files.append(self.intern_str(name))
        self._file_index[path] = idx
        return idx

    def line_of(self, offset: int) -> int:
        """M28: the line (in its own file) of a combined-text offset, for
        the TESTS table -- 0 when unknown."""
        path, src_offset = self.pp.map_to_source(offset)
        return line_col(self.pp.files.get(path, ""), src_offset)[0]

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
    minor = _file_minor(
        lowerer.natives, lowerer.strings, lowerer.code, buf.positions, pp.prelude_start, lowerer.functions
    )
    lowerer.file_minor = minor
    meta = lowerer.build_meta()
    debug = lowerer.build_debug() if target == "debug" else None
    return Program(
        strings=lowerer.strings,
        constants=lowerer.constants,
        types=lowerer.types,
        natives=lowerer.natives,
        functions=lowerer.functions,
        code=lowerer.code,
        debug=debug,
        minor=minor,
        handlers=list(buf.handlers),
        tests=[
            TestEntry(lowerer.intern_str(name), slot, lowerer.line_of(position)) for name, slot, position in buf.tests
        ],
        meta=meta,
    )


# The lowest minor version this lowering ever writes: its TYPES layout,
# opcodes and HANDLERS section are all 1.4's (see `build_types`).
_BASE_MINOR = 4


def _file_minor(
    natives: list, strings: list, code: list, positions: list, prelude_start, functions: list = ()
) -> int:
    """M27 (docs/MAHC_FORMAT.md #3/#4.4): the lowest minor version whose
    features this file uses -- 1.4, or higher only when it calls a native
    added later (the `std:math` ones are 1.5). So a program that doesn't use
    newer natives still runs on an older 1.4 VM, and one that does is
    refused by it with a message naming those natives. M29: likewise a call
    of a method named like a later native method (the String methods, 1.6),
    outside the prelude -- the prelude's own calls only happen through a
    prelude method the program calls, which is itself in that table (e.g.
    `to_number`, which calls `parse_number`)."""
    minor = _BASE_MINOR
    # M41c: a rest parameter (PARAMS flag bits 1/2) or a function-item impl
    # (`defmethod` with a `fn#<index>` type name) needs 1.16 (`paramhooks`
    # and the `hooks.*` natives do too, below, through their own tables).
    if any(fn.rest for fn in functions) or any(
        instr.op == "defmethod" and strings[instr.args[1]].startswith("fn#") for instr in code
    ):
        minor = max(minor, 16)
    for ref in natives:
        minor = max(minor, NATIVE_SINCE_MINOR.get(strings[ref.name], 0))
    for pc, instr in enumerate(code):
        # M41a: a 1.14 opcode (type values, spread calls) needs 1.14; META
        # doesn't -- it's optional, and older VMs skip it.
        minor = max(minor, OPCODE_SINCE_MINOR.get(instr.op, 0))
        if instr.op == "loadtype" and instr.args[0] == 1 and instr.args[1] >= 8:
            minor = max(minor, 17)  # M37: the primitive type code 8, Bytes
        position = positions[pc] if pc < len(positions) else None
        if prelude_start is not None and position is not None and position >= prelude_start:
            continue
        if instr.op in METHOD_CALL_OPCODES:
            minor = max(minor, NATIVE_METHOD_SINCE_MINOR.get(strings[instr.args[1]], 0))
    assert minor <= MINOR
    return minor
