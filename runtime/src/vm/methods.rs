//! Native (VM-builtin) method-table targets that aren't a Mah-code
//! `defmethod` -- docs/MAHC_FORMAT.md #6.6/#6.7/#6.9. A faithful port of
//! `code_interpreter.py`'s `_format_value`, `_string_char_at`,
//! `_seq_position`/`_vector_position`, `_slice_bound(s)`, `_vector_index(_assign)`,
//! `_string_index`, `_map_index(_assign)`, `_map_remove`, `_deep_copy`, and
//! the Vector/Map inherent methods (`push`, `pop`, `keys`, `has`, ...).

use std::cell::RefCell;
use std::collections::HashMap;
use std::rc::Rc;

use crate::decimal::Decimal;

use super::error::{RResult, RuntimeError};
use super::value::{
    map_key, type_name_of, BuiltinTypeNames, EnumData, MapData, MapKey, MapRef, StructData,
    Value, VectorRef,
};

// ---------------------------------------------------------------------------
// `to_string` structural formatting -- docs/MAHC_FORMAT.md #6.6 step 2.
// `recurse` is `Vm::to_str`, threaded through so nested values still get
// `Printable` dispatch.
// ---------------------------------------------------------------------------

pub fn format_value(val: &Value, recurse: &mut dyn FnMut(&Value) -> RResult<String>) -> RResult<String> {
    Ok(match val {
        Value::None => "none".to_string(),
        Value::Bool(b) => {
            if *b {
                "true".to_string()
            } else {
                "false".to_string()
            }
        }
        Value::Number(n) => n.format(),
        Value::Str(s) => s.to_string(),
        Value::Function(c) => match &c.func.name {
            Some(n) => format!("<fn {n}>"),
            None => "<fn>".to_string(),
        },
        Value::Struct(s) => {
            let b = s.borrow();
            let mut parts = Vec::with_capacity(b.fields.len());
            for (k, v) in &b.fields {
                parts.push(format!("{k}: {}", recurse(v)?));
            }
            format!("{} {{ {} }}", b.type_name, parts.join(", "))
        }
        Value::Enum(e) => {
            let b = e.borrow();
            if b.type_name.as_ref() == "Option" && b.variant.as_ref() == "some" {
                let v = b.get("value").expect("Option.some always has a 'value' field");
                format!("some({})", recurse(v)?)
            } else if !b.fields.is_empty() {
                let mut parts = Vec::with_capacity(b.fields.len());
                for (k, v) in &b.fields {
                    parts.push(format!("{k}: {}", recurse(v)?));
                }
                format!("{}.{} {{ {} }}", b.type_name, b.variant, parts.join(", "))
            } else {
                format!("{}.{}", b.type_name, b.variant)
            }
        }
        Value::Promise(p) => {
            let b = p.borrow();
            match &b.settled {
                Some(v) => format!("Promise.Settled {{ value: {} }}", recurse(v)?),
                None => "Promise.Pending".to_string(),
            }
        }
        Value::Vector(v) => {
            let items = v.borrow();
            let mut parts = Vec::with_capacity(items.len());
            for x in items.iter() {
                parts.push(recurse(x)?);
            }
            format!("[{}]", parts.join(", "))
        }
        Value::Map(m) => {
            let b = m.borrow();
            if b.len() == 0 {
                "[:]".to_string()
            } else {
                let mut parts = Vec::new();
                for (k, v) in b.iter_ordered() {
                    parts.push(format!("{}: {}", recurse(k)?, recurse(v)?));
                }
                format!("[{}]", parts.join(", "))
            }
        }
        Value::Absent => "<absent>".to_string(),
    })
}

// ---------------------------------------------------------------------------
// String.char_at / Vector & String indices & slices -- docs/MAHC_FORMAT.md #6.7/#6.9
// ---------------------------------------------------------------------------

pub fn string_char_at(s: &str, i: &Value, names: &BuiltinTypeNames) -> RResult<Value> {
    let n = match i {
        Value::Number(n) => n,
        other => return Err(RuntimeError::new(format!("char_at index must be a Number, got {}", type_name_of(other, names)))),
    };
    let len = s.chars().count();
    if !n.is_integer() || n.is_negative() || n.to_i64().map(|v| v as usize >= len).unwrap_or(true) {
        return Err(RuntimeError::new(format!(
            "char_at index {} is out of range for a String of length {len}",
            n.format()
        )));
    }
    let idx = n.to_i64().unwrap() as usize;
    Ok(Value::Str(Rc::from(s.chars().nth(idx).unwrap().to_string().as_str())))
}

/// The position a (possibly negative) index names in a sequence of length
/// `n`, or `None` when it names no item (fractional, or outside
/// `-n <= i < n`). A non-Number index is a runtime error.
fn seq_position(n: usize, i: &Value, type_label: &str, names: &BuiltinTypeNames) -> RResult<Option<usize>> {
    let num = match i {
        Value::Number(num) => num,
        other => {
            return Err(RuntimeError::new(format!(
                "{type_label} index must be a Number, got {}",
                type_name_of(other, names)
            )))
        }
    };
    if !num.is_integer() {
        return Ok(None);
    }
    let Some(mut pos) = num.to_i64() else { return Ok(None) };
    if pos < 0 {
        pos += n as i64;
    }
    if pos < 0 || pos >= n as i64 {
        return Ok(None);
    }
    Ok(Some(pos as usize))
}

fn is_range(v: &Value) -> Option<Rc<RefCell<StructData>>> {
    if let Value::Struct(s) = v {
        let is_range_type = matches!(s.borrow().type_name.as_ref(), "Range" | "FromRange" | "ToRange");
        if is_range_type {
            return Some(s.clone());
        }
    }
    None
}

fn slice_bound(v: &Value, type_label: &str, names: &BuiltinTypeNames) -> RResult<i64> {
    match v {
        Value::Number(n) if n.is_integer() => n.to_i64().ok_or_else(|| {
            RuntimeError::new(format!("{type_label} slice bounds must be integer Numbers, got {}", n.format()))
        }),
        Value::Number(n) => {
            Err(RuntimeError::new(format!("{type_label} slice bounds must be integer Numbers, got {}", n.format())))
        }
        other => Err(RuntimeError::new(format!(
            "{type_label} slice bounds must be integer Numbers, got {}",
            type_name_of(other, names)
        ))),
    }
}

/// `x[a..b]` and friends -- docs/MAHC_FORMAT.md #6.9: negative bounds count
/// from the end, then both are clamped into `0..n` (never an error).
fn slice_bounds(n: usize, r: &StructData, type_label: &str, names: &BuiltinTypeNames) -> RResult<(usize, usize)> {
    let n = n as i64;
    let mut start = match r.get("start") {
        Some(v) => slice_bound(v, type_label, names)?,
        None => 0,
    };
    if start < 0 {
        start += n;
    }
    let mut stop = match r.get("end") {
        Some(v) => {
            let mut e = slice_bound(v, type_label, names)?;
            if e < 0 {
                e += n;
            }
            if matches!(r.get("inclusive"), Some(Value::Bool(true))) {
                e += 1;
            }
            e
        }
        None => n,
    };
    start = start.clamp(0, n);
    stop = stop.clamp(0, n);
    Ok((start as usize, stop as usize))
}

pub fn vector_index(vec: &VectorRef, i: &Value, names: &BuiltinTypeNames) -> RResult<Value> {
    if let Some(r) = is_range(i) {
        let items = vec.borrow();
        let (start, stop) = slice_bounds(items.len(), &r.borrow(), "Vector", names)?;
        let slice = if start < stop { items[start..stop].to_vec() } else { Vec::new() };
        return Ok(Value::Vector(Rc::new(RefCell::new(slice))));
    }
    let items = vec.borrow();
    match seq_position(items.len(), i, "Vector", names)? {
        Some(pos) => Ok(items[pos].clone()),
        None => Ok(Value::None),
    }
}

pub fn vector_index_assign(vec: &VectorRef, i: &Value, value: Value, names: &BuiltinTypeNames) -> RResult<Value> {
    if is_range(i).is_some() {
        return Err(RuntimeError::new(
            "Can't assign to a Vector slice (v[a..b] = ...); assign items one at a time",
        ));
    }
    let len = vec.borrow().len();
    match seq_position(len, i, "Vector", names)? {
        Some(pos) => {
            vec.borrow_mut()[pos] = value;
            Ok(Value::None)
        }
        None => {
            let shown = match i {
                Value::Number(n) => n.format(),
                other => type_name_of(other, names).to_string(),
            };
            Err(RuntimeError::new(format!(
                "Vector index {shown} is out of range for a Vector of length {len} (use push to add items)"
            )))
        }
    }
}

pub fn string_index(s: &str, i: &Value, names: &BuiltinTypeNames) -> RResult<Value> {
    if let Some(r) = is_range(i) {
        let chars: Vec<char> = s.chars().collect();
        let (start, stop) = slice_bounds(chars.len(), &r.borrow(), "String", names)?;
        let substr: String = if start < stop { chars[start..stop].iter().collect() } else { String::new() };
        return Ok(Value::Str(Rc::from(substr.as_str())));
    }
    let chars: Vec<char> = s.chars().collect();
    match seq_position(chars.len(), i, "String", names)? {
        Some(pos) => Ok(Value::Str(Rc::from(chars[pos].to_string().as_str()))),
        None => Ok(Value::None),
    }
}

// ---------------------------------------------------------------------------
// Map natives -- docs/MAHC_FORMAT.md #6.9
// ---------------------------------------------------------------------------

pub fn map_key_of(key: &Value, names: &BuiltinTypeNames) -> RResult<MapKey> {
    map_key(key).ok_or_else(|| {
        RuntimeError::new(format!("Map keys must be a String, Number, or Bool, got {}", type_name_of(key, names)))
    })
}

pub fn map_index(m: &MapRef, key: &Value, names: &BuiltinTypeNames) -> RResult<Value> {
    let k = map_key_of(key, names)?;
    Ok(m.borrow().get(&k).cloned().unwrap_or(Value::None))
}

pub fn map_index_assign(m: &MapRef, key: Value, value: Value, names: &BuiltinTypeNames) -> RResult<Value> {
    let k = map_key_of(&key, names)?;
    m.borrow_mut().index_assign(k, key, value);
    Ok(Value::None)
}

pub fn map_remove(m: &MapRef, key: &Value, names: &BuiltinTypeNames) -> RResult<Value> {
    let k = map_key_of(key, names)?;
    Ok(m.borrow_mut().remove(&k).unwrap_or(Value::None))
}

// ---------------------------------------------------------------------------
// Deep copy (`copy(deep: true)`) -- docs/MAHC_FORMAT.md #6.9. Memoized by
// object identity (the `Rc`'s heap address) so shared sub-objects stay
// shared in the copy and cycles terminate.
// ---------------------------------------------------------------------------

pub fn deep_copy(value: &Value, memo: &mut HashMap<usize, Value>) -> Value {
    match value {
        Value::None | Value::Bool(_) | Value::Number(_) | Value::Str(_) | Value::Function(_) | Value::Promise(_) => {
            value.clone()
        }
        Value::Vector(v) => {
            let key = Rc::as_ptr(v) as usize;
            if let Some(existing) = memo.get(&key) {
                return existing.clone();
            }
            let out: VectorRef = Rc::new(RefCell::new(Vec::new()));
            memo.insert(key, Value::Vector(out.clone()));
            let copied: Vec<Value> = v.borrow().iter().map(|x| deep_copy(x, memo)).collect();
            *out.borrow_mut() = copied;
            Value::Vector(out)
        }
        Value::Map(m) => {
            let key = Rc::as_ptr(m) as usize;
            if let Some(existing) = memo.get(&key) {
                return existing.clone();
            }
            let out = MapData::new();
            memo.insert(key, Value::Map(out.clone()));
            let entries: Vec<(MapKey, Value, Value)> = {
                let b = m.borrow();
                b.order.iter().map(|k| {
                    let (orig, v) = b.entries.get(k).unwrap();
                    (k.clone(), orig.clone(), v.clone())
                }).collect()
            };
            let mut ob = out.borrow_mut();
            for (k, orig, v) in entries {
                let copied_v = deep_copy(&v, memo);
                ob.order.push(k.clone());
                ob.entries.insert(k, (orig, copied_v));
            }
            drop(ob);
            Value::Map(out)
        }
        Value::Struct(s) => {
            let key = Rc::as_ptr(s) as usize;
            if let Some(existing) = memo.get(&key) {
                return existing.clone();
            }
            let type_name = s.borrow().type_name.clone();
            let out = Rc::new(RefCell::new(StructData { type_name, fields: Vec::new() }));
            memo.insert(key, Value::Struct(out.clone()));
            let copied: Vec<(Rc<str>, Value)> =
                s.borrow().fields.iter().map(|(n, v)| (n.clone(), deep_copy(v, memo))).collect();
            out.borrow_mut().fields = copied;
            Value::Struct(out)
        }
        Value::Enum(e) => {
            let key = Rc::as_ptr(e) as usize;
            if let Some(existing) = memo.get(&key) {
                return existing.clone();
            }
            let type_name = e.borrow().type_name.clone();
            let variant = e.borrow().variant.clone();
            let out = Rc::new(RefCell::new(EnumData { type_name, variant, fields: Vec::new() }));
            memo.insert(key, Value::Enum(out.clone()));
            let copied: Vec<(Rc<str>, Value)> =
                e.borrow().fields.iter().map(|(n, v)| (n.clone(), deep_copy(v, memo))).collect();
            out.borrow_mut().fields = copied;
            Value::Enum(out)
        }
        Value::Absent => value.clone(),
    }
}

pub fn collection_copy(value: &Value, deep: &Value) -> Value {
    if super::value::truthy(deep) {
        let mut memo = HashMap::new();
        return deep_copy(value, &mut memo);
    }
    match value {
        Value::Vector(v) => Value::Vector(Rc::new(RefCell::new(v.borrow().clone()))),
        Value::Map(m) => {
            let b = m.borrow();
            let out = MapData::new();
            {
                let mut ob = out.borrow_mut();
                ob.order = b.order.clone();
                ob.entries = b.entries.clone();
            }
            Value::Map(out)
        }
        _ => unreachable!("collection_copy is only called on Vector/Map receivers"),
    }
}

// ---------------------------------------------------------------------------
// Vector inherent methods (docs/MAHC_FORMAT.md #6.7)
// ---------------------------------------------------------------------------

pub fn vector_push(vec: &VectorRef, value: Value) -> Value {
    vec.borrow_mut().push(value);
    Value::None
}
pub fn vector_push_start(vec: &VectorRef, value: Value) -> Value {
    vec.borrow_mut().insert(0, value);
    Value::None
}
pub fn vector_pop(vec: &VectorRef) -> Value {
    vec.borrow_mut().pop().unwrap_or(Value::None)
}
pub fn vector_pop_start(vec: &VectorRef) -> Value {
    let mut b = vec.borrow_mut();
    if b.is_empty() {
        Value::None
    } else {
        b.remove(0)
    }
}

pub fn number_from_usize(n: usize) -> Value {
    Value::Number(Decimal::from_i64(n as i64))
}

// ---------------------------------------------------------------------------
// Native method-table targets -- docs/MAHC_FORMAT.md #6.7. `arity` below
// excludes the receiver, matching `code_interpreter.py`'s `NativeMethod`.
// ---------------------------------------------------------------------------

#[derive(Clone, Copy)]
pub enum NativeMethodKind {
    ToString,
    StringLen,
    StringCharAt,
    FunctionArity,
    VectorLen,
    VectorPush,
    VectorPop,
    VectorPushStart,
    VectorPopStart,
    VectorCopy,
    MapLen,
    MapKeys,
    MapValues,
    MapHas,
    MapRemove,
    MapCopy,
    StringIndex,
    VectorIndex,
    MapIndex,
    VectorIndexAssign,
    MapIndexAssign,
}

impl NativeMethodKind {
    pub fn required_arity(self) -> usize {
        use NativeMethodKind::*;
        match self {
            ToString | StringLen | FunctionArity | VectorLen | VectorPop | VectorPopStart | VectorCopy | MapLen
            | MapKeys | MapValues | MapCopy => 0,
            StringCharAt | VectorPush | VectorPushStart | MapHas | MapRemove | StringIndex | VectorIndex | MapIndex => 1,
            VectorIndexAssign | MapIndexAssign => 2,
        }
    }

    pub fn optional_name(self) -> Option<&'static str> {
        match self {
            NativeMethodKind::VectorCopy | NativeMethodKind::MapCopy => Some("deep"),
            _ => None,
        }
    }
}

/// `bound[0]` is always the receiver; `bound[1..]` are the (already-bound,
/// defaults-substituted) arguments -- see `super::exec::bind_method_call`.
pub fn call_native_method(kind: NativeMethodKind, bound: &[Value], vm: &mut super::exec::Vm) -> RResult<Value> {
    // Cloned once (cheap: `BuiltinTypeNames` is a handful of `Rc<str>`
    // refcount bumps) so the `ToString` arm can still call `vm.to_str`
    // (needing `&mut vm`) without fighting the borrow checker over
    // `vm.names`, which never changes during a run anyway.
    let names = vm.names.clone();
    let names = &names;
    match kind {
        NativeMethodKind::ToString => {
            let s = vm.to_str(&bound[0])?;
            Ok(Value::Str(Rc::from(s.as_str())))
        }
        NativeMethodKind::StringLen => match &bound[0] {
            Value::Str(s) => Ok(number_from_usize(s.chars().count())),
            _ => unreachable!(),
        },
        NativeMethodKind::StringCharAt => match &bound[0] {
            Value::Str(s) => string_char_at(s, &bound[1], names),
            _ => unreachable!(),
        },
        NativeMethodKind::FunctionArity => match &bound[0] {
            Value::Function(c) => Ok(number_from_usize(c.func.param_count)),
            _ => unreachable!(),
        },
        NativeMethodKind::VectorLen => match &bound[0] {
            Value::Vector(v) => Ok(number_from_usize(v.borrow().len())),
            _ => unreachable!(),
        },
        NativeMethodKind::VectorPush => match &bound[0] {
            Value::Vector(v) => Ok(vector_push(v, bound[1].clone())),
            _ => unreachable!(),
        },
        NativeMethodKind::VectorPushStart => match &bound[0] {
            Value::Vector(v) => Ok(vector_push_start(v, bound[1].clone())),
            _ => unreachable!(),
        },
        NativeMethodKind::VectorPop => match &bound[0] {
            Value::Vector(v) => Ok(vector_pop(v)),
            _ => unreachable!(),
        },
        NativeMethodKind::VectorPopStart => match &bound[0] {
            Value::Vector(v) => Ok(vector_pop_start(v)),
            _ => unreachable!(),
        },
        NativeMethodKind::VectorCopy => Ok(collection_copy(&bound[0], &bound[1])),
        NativeMethodKind::MapLen => match &bound[0] {
            Value::Map(m) => Ok(number_from_usize(m.borrow().len())),
            _ => unreachable!(),
        },
        NativeMethodKind::MapKeys => match &bound[0] {
            Value::Map(m) => {
                let b = m.borrow();
                Ok(Value::Vector(Rc::new(RefCell::new(b.iter_ordered().map(|(k, _v)| k.clone()).collect()))))
            }
            _ => unreachable!(),
        },
        NativeMethodKind::MapValues => match &bound[0] {
            Value::Map(m) => {
                let b = m.borrow();
                Ok(Value::Vector(Rc::new(RefCell::new(b.iter_ordered().map(|(_k, v)| v.clone()).collect()))))
            }
            _ => unreachable!(),
        },
        NativeMethodKind::MapHas => match &bound[0] {
            Value::Map(m) => {
                let k = map_key_of(&bound[1], names)?;
                Ok(Value::Bool(m.borrow().get(&k).is_some()))
            }
            _ => unreachable!(),
        },
        NativeMethodKind::MapRemove => match &bound[0] {
            Value::Map(m) => map_remove(m, &bound[1], names),
            _ => unreachable!(),
        },
        NativeMethodKind::MapCopy => Ok(collection_copy(&bound[0], &bound[1])),
        NativeMethodKind::StringIndex => match &bound[0] {
            Value::Str(s) => string_index(s, &bound[1], names),
            _ => unreachable!(),
        },
        NativeMethodKind::VectorIndex => match &bound[0] {
            Value::Vector(v) => vector_index(v, &bound[1], names),
            _ => unreachable!(),
        },
        NativeMethodKind::MapIndex => match &bound[0] {
            Value::Map(m) => map_index(m, &bound[1], names),
            _ => unreachable!(),
        },
        NativeMethodKind::VectorIndexAssign => match &bound[0] {
            Value::Vector(v) => vector_index_assign(v, &bound[1], bound[2].clone(), names),
            _ => unreachable!(),
        },
        NativeMethodKind::MapIndexAssign => match &bound[0] {
            Value::Map(m) => map_index_assign(m, bound[1].clone(), bound[2].clone(), names),
            _ => unreachable!(),
        },
    }
}
