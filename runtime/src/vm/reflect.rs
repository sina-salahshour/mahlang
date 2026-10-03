//! M41a (1.14, docs/MAHC_FORMAT.md #4.4/#4.10, docs/REFLECTION.md):
//! std:reflect's natives -- a port of `mah/reflect_natives.py`, which defines
//! every rule and the shapes of what they return (type descriptors as
//! `[tag, ...]` Vectors, `[true, value]` / `[false, message]` results).

use std::cell::RefCell;
use std::rc::Rc;

use crate::decimal::Decimal;
use crate::decode::{FnMeta, TypeMeta, TypeMetaBody, TypeRef};

use super::error::{ErrorKind, RuntimeError};
use super::exec::{Callable, Vm};
use super::link::{TypeInfo, TypeKind};
use super::value::{display_name, type_name_of, EnumData, StructData, TypeData, Value, PRIMITIVE_TYPE_NAMES};

type R = Result<Value, RuntimeError>;

fn vec_value(items: Vec<Value>) -> Value {
    Value::Vector(Rc::new(RefCell::new(items)))
}

fn str_value(s: &str) -> Value {
    Value::Str(Rc::from(s))
}

fn number(n: u64) -> Value {
    Value::Number(Decimal::from_u64(n))
}

fn type_value(vm: &Vm, kind: u8, index: usize) -> Value {
    let name: Rc<str> = if kind == 0 {
        vm.linked.types[index].name.clone()
    } else {
        Rc::from(PRIMITIVE_TYPE_NAMES[index])
    };
    Value::Type(Rc::new(TypeData { kind, index, name }))
}

fn prim(vm: &Vm, name: &str) -> Value {
    let code = PRIMITIVE_TYPE_NAMES.iter().position(|n| *n == name).expect("a primitive type name");
    type_value(vm, 1, code)
}

fn s(vm: &Vm, idx: usize) -> Value {
    Value::Str(vm.linked.strings[idx].clone())
}

fn doc(vm: &Vm, doc: Option<usize>) -> Value {
    match doc {
        Some(i) => s(vm, i),
        None => str_value(""),
    }
}

fn refs_value(vm: &Vm, refs: &[TypeRef]) -> Value {
    vec_value(refs.iter().map(|r| ref_value(vm, r)).collect())
}

fn throws_value(vm: &Vm, throws: &Option<Vec<TypeRef>>) -> Value {
    match throws {
        None => Value::None,
        Some(list) => refs_value(vm, list),
    }
}

fn ref_value(vm: &Vm, r: &TypeRef) -> Value {
    match r {
        TypeRef::Unknown => vec_value(vec![number(0)]),
        TypeRef::SelfType => vec_value(vec![number(4)]),
        TypeRef::Never => vec_value(vec![number(5)]),
        TypeRef::Named { kind, index, args } => {
            vec_value(vec![number(1), type_value(vm, *kind, *index), refs_value(vm, args)])
        }
        TypeRef::Fn { params, ret, throws } => vec_value(vec![
            number(2),
            refs_value(vm, params),
            ref_value(vm, ret),
            throws_value(vm, throws),
        ]),
        TypeRef::Param(name) => vec_value(vec![number(3), s(vm, *name)]),
        TypeRef::Trait { name, args } => vec_value(vec![
            number(6),
            str_value(display_name(&vm.linked.strings[*name])),
            refs_value(vm, args),
        ]),
    }
}

/// M41b: a copy of the decorators stored for a target, or an empty Vector.
fn decorators_of(vm: &Vm, kind: u64, a: usize, b: usize) -> Value {
    match vm.decorators.get(&(kind, a, if matches!(kind, 1 | 3 | 4) { b } else { 0 })) {
        Some(Value::Vector(items)) => vec_value(items.borrow().clone()),
        _ => vec_value(Vec::new()),
    }
}

/// `reflect.decorators(kind, a, b)` (1.15).
pub fn decorators(vm: &mut Vm, args: &[Value]) -> R {
    let mut parts = [0i64; 3];
    for (slot, (label, value)) in ["kind", "a", "b"].iter().zip(args.iter()).enumerate() {
        parts[slot] = match value {
            Value::Number(n) => match n.to_i64().filter(|_| n.is_integer()) {
                Some(v) => v,
                None => {
                    return Err(RuntimeError::with_kind(
                        format!("reflect.decorators: {label} must be a whole Number, got Number"),
                        ErrorKind::TypeMismatch,
                    ))
                }
            },
            other => {
                return Err(RuntimeError::with_kind(
                    format!(
                        "reflect.decorators: {label} must be a whole Number, got {}",
                        type_name_of(other, &vm.names)
                    ),
                    ErrorKind::TypeMismatch,
                ))
            }
        };
    }
    if parts.iter().any(|&p| p < 0) {
        return Ok(vec_value(Vec::new()));
    }
    Ok(decorators_of(vm, parts[0] as u64, parts[1] as usize, parts[2] as usize))
}

fn unknown_ref() -> Value {
    vec_value(vec![number(0)])
}

fn type_arg<'a>(vm: &Vm, func: &str, v: &'a Value) -> Result<&'a Rc<TypeData>, RuntimeError> {
    match v {
        Value::Type(t) => Ok(t),
        other => Err(RuntimeError::with_kind(
            format!("{func}: expected a Type, got {}", type_name_of(other, &vm.names)),
            ErrorKind::TypeMismatch,
        )),
    }
}

pub fn type_of(vm: &mut Vm, args: &[Value]) -> R {
    let value = &args[0];
    let find = |want_struct: bool, name: &str| -> Option<usize> {
        vm.linked.types.iter().position(|t| {
            t.name.as_ref() == name && matches!(t.kind, TypeKind::Struct(_)) == want_struct
        })
    };
    Ok(match value {
        Value::None => prim(vm, "None"),
        Value::Bool(_) => prim(vm, "Bool"),
        Value::Number(_) => prim(vm, "Number"),
        Value::Str(_) => prim(vm, "String"),
        Value::Function(_) => prim(vm, "Function"),
        Value::Vector(_) => prim(vm, "Vector"),
        Value::Map(_) => prim(vm, "Map"),
        Value::Type(_) => prim(vm, "Type"),
        Value::Struct(st) => match find(true, &st.borrow().type_name) {
            Some(i) => type_value(vm, 0, i),
            None => return Err(cant_tell(vm, value)),
        },
        Value::Enum(e) => match find(false, &e.borrow().type_name) {
            Some(i) => type_value(vm, 0, i),
            None => return Err(cant_tell(vm, value)),
        },
        Value::Promise(_) => match find(false, "Promise") {
            Some(i) => type_value(vm, 0, i),
            None => return Err(cant_tell(vm, value)),
        },
        Value::Absent => return Err(cant_tell(vm, value)),
    })
}

fn cant_tell(vm: &Vm, value: &Value) -> RuntimeError {
    RuntimeError::with_kind(
        format!("reflect.type_of: can't tell the type of a {}", type_name_of(value, &vm.names)),
        ErrorKind::TypeMismatch,
    )
}

pub fn signature(vm: &mut Vm, args: &[Value]) -> R {
    let Value::Function(f) = &args[0] else {
        return Err(RuntimeError::with_kind(
            format!("reflect.signature: expected a Function, got {}", type_name_of(&args[0], &vm.names)),
            ErrorKind::TypeMismatch,
        ));
    };
    let meta: Option<&FnMeta> =
        vm.linked.meta.as_ref().and_then(|m| m.functions.get(f.func.index)).filter(|m| m.has_meta);
    let names: Vec<(Rc<str>, bool)> = match &f.func.params {
        Some(p) => p.clone(),
        None => (0..f.func.param_count).map(|i| (Rc::from(format!("#{i}").as_str()), false)).collect(),
    };
    let mut params = Vec::with_capacity(names.len());
    for (i, (pname, has_default)) in names.iter().enumerate() {
        let pm = meta.map(|m| &m.params[i]);
        let constant = pm.is_some_and(|p| p.default == 2);
        params.push(vec_value(vec![
            Value::Str(pname.clone()),
            pm.map(|p| ref_value(vm, &p.ty)).unwrap_or_else(unknown_ref),
            match pm {
                Some(p) => doc(vm, p.doc),
                None => str_value(""),
            },
            Value::Bool(*has_default),
            Value::Bool(constant),
            match pm.and_then(|p| p.const_index) {
                Some(c) if constant => vm.linked.constants[c].clone(),
                _ => Value::None,
            },
            decorators_of(vm, 1, f.func.index, i),
        ]));
    }
    Ok(vec_value(vec![
        match &f.func.name {
            Some(n) => Value::Str(n.clone()),
            None => Value::None,
        },
        match meta {
            Some(m) => doc(vm, m.doc),
            None => str_value(""),
        },
        vec_value(match meta {
            Some(m) => m.type_params.iter().map(|&t| s(vm, t)).collect(),
            None => Vec::new(),
        }),
        vec_value(params),
        match meta {
            Some(m) => ref_value(vm, &m.returns),
            None => unknown_ref(),
        },
        match meta {
            Some(m) => throws_value(vm, &m.throws),
            None => Value::None,
        },
        decorators_of(vm, 0, f.func.index, 0),
    ]))
}

pub fn schema(vm: &mut Vm, args: &[Value]) -> R {
    let t = type_arg(vm, "reflect.schema", &args[0])?.clone();
    if t.kind == 1 {
        return Ok(Value::None);
    }
    let info: &TypeInfo = &vm.linked.types[t.index];
    let tm: Option<&TypeMeta> = if t.index >= vm.linked.builtin_type_count {
        vm.linked.meta.as_ref().and_then(|m| m.types.get(t.index - vm.linked.builtin_type_count))
    } else {
        None
    };
    let type_doc = match tm {
        Some(m) => doc(vm, m.doc),
        None => str_value(""),
    };
    let tparams = vec_value(match tm {
        Some(m) => m.type_params.iter().map(|&i| s(vm, i)).collect(),
        None => Vec::new(),
    });
    let self_type = args[0].clone();
    match &info.kind {
        TypeKind::Struct(fields) => {
            let mut out = Vec::new();
            for (i, name) in fields.iter().enumerate() {
                let (ty, fdoc) = match tm.map(|m| &m.body) {
                    Some(TypeMetaBody::Struct(list)) => (ref_value(vm, &list[i].0), doc(vm, list[i].1)),
                    _ => (unknown_ref(), str_value("")),
                };
                out.push(vec_value(vec![Value::Str(name.clone()), ty, fdoc, decorators_of(vm, 3, t.index, i)]));
            }
            Ok(vec_value(vec![
                str_value("struct"),
                self_type,
                type_doc,
                tparams,
                vec_value(out),
                decorators_of(vm, 2, t.index, 0),
            ]))
        }
        TypeKind::Enum(variants) => {
            let mut out = Vec::new();
            for (i, (vname, vfields)) in variants.iter().enumerate() {
                let (vdoc, refs): (Value, Option<&Vec<TypeRef>>) = match tm.map(|m| &m.body) {
                    Some(TypeMetaBody::Enum(list)) => (doc(vm, list[i].0), Some(&list[i].1)),
                    _ => (str_value(""), None),
                };
                let mut fields = Vec::new();
                for (j, fname) in vfields.iter().enumerate() {
                    let ty = match refs {
                        Some(r) => ref_value(vm, &r[j]),
                        None => unknown_ref(),
                    };
                    fields.push(vec_value(vec![Value::Str(fname.clone()), ty, str_value(""), vec_value(Vec::new())]));
                }
                out.push(vec_value(vec![
                    Value::Str(vname.clone()),
                    vdoc,
                    vec_value(fields),
                    decorators_of(vm, 4, t.index, i),
                ]));
            }
            Ok(vec_value(vec![
                str_value("enum"),
                self_type,
                type_doc,
                tparams,
                vec_value(out),
                decorators_of(vm, 2, t.index, 0),
            ]))
        }
    }
}

pub fn methods(vm: &mut Vm, args: &[Value]) -> R {
    let t = type_arg(vm, "reflect.methods", &args[0])?.clone();
    let mut inherent: Vec<(Rc<str>, Value, bool)> = Vec::new();
    let mut traited: Vec<(Rc<str>, Rc<str>, Value, bool)> = Vec::new();
    for ((type_name, method), entry) in vm.method_table.iter() {
        if type_name.as_ref() != t.name.as_ref() {
            continue;
        }
        if let Some((Callable::Closure(c), is_method)) = &entry.inherent {
            inherent.push((method.clone(), Value::Function(c.clone()), *is_method));
        }
        for (trait_name, (callable, is_method)) in &entry.traits {
            if let Callable::Closure(c) = callable {
                traited.push((trait_name.clone(), method.clone(), Value::Function(c.clone()), *is_method));
            }
        }
    }
    inherent.sort_by(|a, b| a.0.cmp(&b.0));
    traited.sort_by(|a, b| (&a.0, &a.1).cmp(&(&b.0, &b.1)));
    let mut out = Vec::new();
    for (name, f, is_method) in inherent {
        out.push(vec_value(vec![Value::Str(name), f, Value::Bool(is_method), Value::None]));
    }
    for (trait_name, name, f, is_method) in traited {
        out.push(vec_value(vec![Value::Str(name), f, Value::Bool(is_method), str_value(display_name(&trait_name))]));
    }
    Ok(vec_value(out))
}

pub fn implements(vm: &mut Vm, args: &[Value]) -> R {
    let t = type_arg(vm, "reflect.implements", &args[0])?.clone();
    let Value::Str(trait_name) = &args[1] else {
        return Err(RuntimeError::with_kind(
            format!(
                "reflect.implements: the trait name must be a String, got {}",
                type_name_of(&args[1], &vm.names)
            ),
            ErrorKind::TypeMismatch,
        ));
    };
    for ((type_name, _method), entry) in vm.method_table.iter() {
        if type_name.as_ref() == t.name.as_ref() && entry.traits.keys().any(|k| display_name(k) == trait_name.as_ref()) {
            return Ok(Value::Bool(true));
        }
    }
    Ok(Value::Bool(false))
}

fn failure(message: String) -> Value {
    vec_value(vec![Value::Bool(false), Value::Str(Rc::from(message.as_str()))])
}

/// The `(name, value)` entries of a Map of fields, in order.
fn field_entries(vm: &Vm, func: &str, v: &Value) -> Result<Vec<(Rc<str>, Value)>, RuntimeError> {
    let Value::Map(m) = v else {
        return Err(RuntimeError::with_kind(
            format!("{func}: the fields must be a Map, got {}", type_name_of(v, &vm.names)),
            ErrorKind::TypeMismatch,
        ));
    };
    let mut out = Vec::new();
    for (k, val) in m.borrow().iter_ordered() {
        let Value::Str(name) = k else {
            return Err(RuntimeError::with_kind(
                format!("{func}: field names must be Strings, got a {} key", type_name_of(k, &vm.names)),
                ErrorKind::TypeMismatch,
            ));
        };
        out.push((name.clone(), val.clone()));
    }
    Ok(out)
}

/// The fields of a new value in declaration order, or a failure message.
fn build(label: &str, names: &[Rc<str>], given: &[(Rc<str>, Value)]) -> Result<Vec<(Rc<str>, Value)>, String> {
    for (key, _) in given {
        if !names.iter().any(|n| n == key) {
            return Err(format!("{label} has no field '{key}'"));
        }
    }
    let mut out = Vec::with_capacity(names.len());
    for name in names {
        match given.iter().find(|(k, _)| k == name) {
            Some((_, v)) => out.push((name.clone(), v.clone())),
            None => return Err(format!("missing field '{name}' for {label}")),
        }
    }
    Ok(out)
}

pub fn construct(vm: &mut Vm, args: &[Value]) -> R {
    let t = type_arg(vm, "reflect.construct", &args[0])?.clone();
    let given = field_entries(vm, "reflect.construct", &args[1])?;
    let TypeKind::Struct(names) = (if t.kind == 0 { &vm.linked.types[t.index].kind } else { &TypeKind::Enum(Vec::new()) })
    else {
        return Ok(vec_value(vec![
            Value::Bool(false),
            Value::Str(Rc::from(format!("can't construct {}: it isn't a struct", display_name(&t.name)).as_str())),
        ]));
    };
    match build(display_name(&t.name), names, &given) {
        Err(message) => Ok(failure(message)),
        Ok(fields) => {
            let value = Value::Struct(Rc::new(RefCell::new(StructData {
                type_name: vm.linked.types[t.index].name.clone(),
                fields,
                thrown_at: None,
                backtrace: None,
            })));
            Ok(vec_value(vec![Value::Bool(true), value]))
        }
    }
}

pub fn construct_variant(vm: &mut Vm, args: &[Value]) -> R {
    let t = type_arg(vm, "reflect.construct_variant", &args[0])?.clone();
    let Value::Str(variant) = &args[1] else {
        return Err(RuntimeError::with_kind(
            format!(
                "reflect.construct_variant: the variant name must be a String, got {}",
                type_name_of(&args[1], &vm.names)
            ),
            ErrorKind::TypeMismatch,
        ));
    };
    let given = field_entries(vm, "reflect.construct_variant", &args[2])?;
    let info = if t.kind == 0 { Some(&vm.linked.types[t.index]) } else { None };
    let Some(TypeInfo { name, kind: TypeKind::Enum(variants) }) = info else {
        return Ok(failure(format!("can't construct a variant of {}: it isn't an enum", display_name(&t.name))));
    };
    if name.as_ref() == "Promise" {
        return Ok(failure("can't construct a Promise".to_string()));
    }
    let Some((_, declared)) = variants.iter().find(|(v, _)| v.as_ref() == variant.as_ref()) else {
        return Ok(failure(format!("{} has no variant '{variant}'", display_name(&t.name))));
    };
    match build(&format!("{}.{variant}", display_name(&t.name)), declared, &given) {
        Err(message) => Ok(failure(message)),
        Ok(fields) => {
            let value = if name.as_ref() == "Option" && variant.as_ref() == "none" {
                Value::None
            } else {
                Value::Enum(Rc::new(RefCell::new(EnumData {
                    type_name: name.clone(),
                    variant: variant.clone(),
                    fields,
                    thrown_at: None,
                    backtrace: None,
                })))
            };
            Ok(vec_value(vec![Value::Bool(true), value]))
        }
    }
}
