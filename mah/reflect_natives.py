"""M41a (1.14, docs/MAHC_FORMAT.md #4.4/#4.10, docs/REFLECTION.md):
std:reflect's natives, as the Python VM implements them.
`runtime/src/vm/reflect.rs` mirrors every rule here.

The natives return plain Vectors/Maps/values; `mah/std/reflect.mh` turns them
into its structs and enums. A **type descriptor** (`TypeRef` in reflect.mh)
crosses as a Vector `[tag, ...]`:

    [0]                             Unknown
    [1, type, args]                 Named (type: a Type value)
    [2, params, returns, throws]    Fn (throws: none, or a Vector of them)
    [3, name]                       Param
    [4]                             Self
    [5]                             Never
    [6, name, args]                 Trait

`signature` gives `[name or none, doc, type_params, params, returns,
throws or none]`, each parameter `[name, type, doc, has_default,
is_constant, constant]`. `schema` gives `none` for a primitive type, else
`["struct", type, doc, type_params, fields]` (a field `[name, type, doc]`) or
`["enum", type, doc, type_params, variants]` (a variant `[name, doc,
fields]`). `methods` gives `[name, function, is_method, trait or none]`
entries. `construct`/`construct_variant` give `[true, value]` or `[false,
message]`. Without META (or for a function/type it has no entry for) every
type is Unknown, docs are "" and defaults are non-constant.

Like the VM, this module never imports the compiler.
"""

from __future__ import annotations

from decimal import Decimal

from .runtime_values import (
    NONE_VALUE,
    PRIMITIVE_TYPE_NAMES,
    Closure,
    EnumInstance,
    MahRuntimeError,
    MapValue,
    PromiseInstance,
    StructInstance,
    TypeValue,
    VectorValue,
    map_key,
    type_name_of,
)

_PRIM = {name: index for index, name in enumerate(PRIMITIVE_TYPE_NAMES)}


class ReflectData:
    """What the reflect natives read: the linked program's types and
    functions, its META section, and the VM's method table."""

    def __init__(self, linked, method_table):
        self.types = linked.types
        self.functions = linked.functions
        self.strings = linked.strings
        self.constants = linked.constants
        self.method_table = method_table
        meta = linked.meta
        self.fn_meta = meta.functions if meta is not None else []
        nbuiltin = len(linked.types) - len(linked.program_types)
        self.type_meta = {}
        if meta is not None:
            for i, tm in enumerate(meta.types):
                self.type_meta[nbuiltin + i] = tm
        self.struct_index: dict = {}
        self.enum_index: dict = {}
        for i, t in enumerate(self.types):
            (self.struct_index if t.kind == 0 else self.enum_index).setdefault(t.name, i)

    def type_value(self, kind: int, index: int) -> TypeValue:
        name = self.types[index].name if kind == 0 else PRIMITIVE_TYPE_NAMES[index]
        return TypeValue(kind, index, name)


def _reflect(ctx) -> ReflectData:
    return ctx.reflect


def _vec(items) -> VectorValue:
    return VectorValue(list(items))


def _strings(items) -> VectorValue:
    return VectorValue(list(items))


# -- type descriptors --------------------------------------------------------------


def _ref_value(data: ReflectData, ref):
    tag = ref.tag
    if tag in (0, 4, 5):
        return _vec([Decimal(tag)])
    if tag == 1:
        return _vec(
            [Decimal(1), data.type_value(ref.kind, ref.index), _vec(_ref_value(data, a) for a in ref.args)]
        )
    if tag == 2:
        return _vec(
            [
                Decimal(2),
                _vec(_ref_value(data, p) for p in ref.args),
                _ref_value(data, ref.ret),
                _throws_value(data, ref.throws),
            ]
        )
    if tag == 3:
        return _vec([Decimal(3), data.strings[ref.name]])
    return _vec([Decimal(6), data.strings[ref.name], _vec(_ref_value(data, a) for a in ref.args)])


def _throws_value(data: ReflectData, throws):
    if throws is None:
        return NONE_VALUE
    return _vec(_ref_value(data, t) for t in throws)


_UNKNOWN_REF = _vec([Decimal(0)])


def _unknown_ref():
    return _vec([Decimal(0)])


def _doc(data: ReflectData, doc) -> str:
    return data.strings[doc] if doc is not None else ""


# -- natives -----------------------------------------------------------------------


def _type_of(ctx, args):
    (value,) = args
    data = _reflect(ctx)
    if value is NONE_VALUE:
        return data.type_value(1, _PRIM["None"])
    if isinstance(value, bool):
        return data.type_value(1, _PRIM["Bool"])
    if isinstance(value, Decimal):
        return data.type_value(1, _PRIM["Number"])
    if isinstance(value, str):
        return data.type_value(1, _PRIM["String"])
    if isinstance(value, Closure):
        return data.type_value(1, _PRIM["Function"])
    if isinstance(value, VectorValue):
        return data.type_value(1, _PRIM["Vector"])
    if isinstance(value, MapValue):
        return data.type_value(1, _PRIM["Map"])
    if isinstance(value, TypeValue):
        return data.type_value(1, _PRIM["Type"])
    if isinstance(value, StructInstance) and value.type_name in data.struct_index:
        return data.type_value(0, data.struct_index[value.type_name])
    if isinstance(value, EnumInstance) and value.type_name in data.enum_index:
        return data.type_value(0, data.enum_index[value.type_name])
    raise MahRuntimeError(f"reflect.type_of: can't tell the type of a {type_name_of(value)}", kind="TypeMismatch")


def _signature(ctx, args):
    (f,) = args
    data = _reflect(ctx)
    if not isinstance(f, Closure):
        raise MahRuntimeError(f"reflect.signature: expected a Function, got {type_name_of(f)}", kind="TypeMismatch")
    meta = data.fn_meta[f.index] if f.index < len(data.fn_meta) else None
    has_meta = meta is not None and meta.has_meta
    names = f.params if f.params is not None else [(f"#{i}", False) for i in range(f.param_count)]
    params = []
    for i, (pname, has_default) in enumerate(names):
        pm = meta.params[i] if has_meta else None
        constant = pm is not None and pm.default == 2
        params.append(
            _vec(
                [
                    pname,
                    _ref_value(data, pm.type) if pm is not None else _unknown_ref(),
                    _doc(data, pm.doc) if pm is not None else "",
                    bool(has_default),
                    constant,
                    data.constants[pm.const] if constant else NONE_VALUE,
                ]
            )
        )
    return _vec(
        [
            f.name if f.name is not None else NONE_VALUE,
            _doc(data, meta.doc) if has_meta else "",
            _strings(data.strings[t] for t in meta.type_params) if has_meta else _vec([]),
            _vec(params),
            _ref_value(data, meta.returns) if has_meta else _unknown_ref(),
            _throws_value(data, meta.throws) if has_meta else NONE_VALUE,
        ]
    )


def _type_arg(fn: str, value) -> TypeValue:
    if not isinstance(value, TypeValue):
        raise MahRuntimeError(f"{fn}: expected a Type, got {type_name_of(value)}", kind="TypeMismatch")
    return value


def _schema(ctx, args):
    (t,) = args
    data = _reflect(ctx)
    t = _type_arg("reflect.schema", t)
    if t.kind == 1:
        return NONE_VALUE
    info = data.types[t.index]
    tm = data.type_meta.get(t.index)
    doc = _doc(data, tm.doc) if tm is not None else ""
    tparams = _strings(data.strings[i] for i in tm.type_params) if tm is not None else _vec([])
    if info.kind == 0:
        fields = []
        for i, name in enumerate(info.fields):
            ref, fdoc = tm.body[i] if tm is not None else (None, None)
            fields.append(
                _vec([name, _ref_value(data, ref) if ref is not None else _unknown_ref(), _doc(data, fdoc)])
            )
        return _vec(["struct", t, doc, tparams, _vec(fields)])
    variants = []
    for i, (vname, vfields) in enumerate(info.variants):
        vdoc, refs = tm.body[i] if tm is not None else (None, None)
        fields = [
            _vec([fname, _ref_value(data, refs[j]) if refs is not None else _unknown_ref(), ""])
            for j, fname in enumerate(vfields)
        ]
        variants.append(_vec([vname, _doc(data, vdoc), _vec(fields)]))
    return _vec(["enum", t, doc, tparams, _vec(variants)])


def _methods(ctx, args):
    (t,) = args
    data = _reflect(ctx)
    t = _type_arg("reflect.methods", t)
    inherent = []
    traited = []
    for (type_name, method), entry in data.method_table.items():
        if type_name != t.name:
            continue
        if entry["inherent"] is not None and isinstance(entry["inherent"][0], Closure):
            fn, is_method = entry["inherent"]
            inherent.append((method, fn, is_method))
        for trait, (fn, is_method) in entry["traits"].items():
            if isinstance(fn, Closure):
                traited.append((trait, method, fn, is_method))
    inherent.sort(key=lambda m: m[0])
    traited.sort(key=lambda m: (m[0], m[1]))
    out = [_vec([m, fn, bool(is_method), NONE_VALUE]) for m, fn, is_method in inherent]
    out += [_vec([m, fn, bool(is_method), trait]) for trait, m, fn, is_method in traited]
    return _vec(out)


def _implements(ctx, args):
    t, trait = args
    data = _reflect(ctx)
    t = _type_arg("reflect.implements", t)
    if not isinstance(trait, str):
        raise MahRuntimeError(
            f"reflect.implements: the trait name must be a String, got {type_name_of(trait)}", kind="TypeMismatch"
        )
    for (type_name, _method), entry in data.method_table.items():
        if type_name == t.name and trait in entry["traits"]:
            return True
    return False


def _failure(message: str):
    return _vec([False, message])


def _field_map(fn: str, value) -> dict:
    if not isinstance(value, MapValue):
        raise MahRuntimeError(f"{fn}: the fields must be a Map, got {type_name_of(value)}", kind="TypeMismatch")
    out = {}
    for key, v in value.entries.values():
        if not isinstance(key, str):
            raise MahRuntimeError(
                f"{fn}: field names must be Strings, got a {type_name_of(key)} key", kind="TypeMismatch"
            )
        out[key] = v
    return out


def _build(label: str, names: list, given: dict):
    """The fields of a new value in declaration order, or a failure message."""
    for key in given:
        if key not in names:
            return None, f"{label} has no field '{key}'"
    for name in names:
        if name not in given:
            return None, f"missing field '{name}' for {label}"
    return {name: given[name] for name in names}, None


def _construct(ctx, args):
    t, fields = args
    data = _reflect(ctx)
    t = _type_arg("reflect.construct", t)
    given = _field_map("reflect.construct", fields)
    if t.kind == 1 or data.types[t.index].kind != 0:
        return _failure(f"can't construct {t.name}: it isn't a struct")
    info = data.types[t.index]
    built, message = _build(t.name, info.fields, given)
    if message is not None:
        return _failure(message)
    return _vec([True, StructInstance(info.name, built)])


def _construct_variant(ctx, args):
    t, variant, fields = args
    data = _reflect(ctx)
    t = _type_arg("reflect.construct_variant", t)
    if not isinstance(variant, str):
        raise MahRuntimeError(
            f"reflect.construct_variant: the variant name must be a String, got {type_name_of(variant)}",
            kind="TypeMismatch",
        )
    given = _field_map("reflect.construct_variant", fields)
    if t.kind == 1 or data.types[t.index].kind != 1:
        return _failure(f"can't construct a variant of {t.name}: it isn't an enum")
    info = data.types[t.index]
    if info.name == "Promise":
        return _failure("can't construct a Promise")
    declared = dict(info.variants)
    if variant not in declared:
        return _failure(f"{t.name} has no variant '{variant}'")
    built, message = _build(f"{t.name}.{variant}", declared[variant], given)
    if message is not None:
        return _failure(message)
    if info.name == "Option" and variant == "none":
        return _vec([True, NONE_VALUE])
    return _vec([True, EnumInstance(info.name, variant, built)])


NATIVES = {
    "reflect.type_of": (1, _type_of),
    "reflect.signature": (1, _signature),
    "reflect.schema": (1, _schema),
    "reflect.methods": (1, _methods),
    "reflect.implements": (2, _implements),
    "reflect.construct": (2, _construct),
    "reflect.construct_variant": (3, _construct_variant),
}
