//! The step loop, method dispatch, scheduler, and arithmetic -- a Rust port
//! of `code_interpreter.py`'s `_execute`/`_exec`/`step_task`/`drive`.

use std::cell::RefCell;
use std::collections::{BTreeMap, BinaryHeap, HashMap};
use std::io::{self, Read, Stdin, Stdout, Write};
use std::rc::Rc;
use std::time::{Duration, Instant};

use crate::decode::{Addr, BinOp};

use super::error::{RResult, RuntimeError};
use super::link::{LinkedInstr, LinkedProgram, TypeInfo};
use super::methods::{self, call_native_method, NativeMethodKind};
use super::natives;
use super::value::{
    self, is_number, map_key, truthy, type_name_of, values_equal, BuiltinTypeNames, ClosureData, Continuation,
    EnumData, FrameRef, MapData, PromiseData, StructData, TaskRef, Value,
};

// ---------------------------------------------------------------------------
// Method table
// ---------------------------------------------------------------------------

#[derive(Clone)]
pub enum Callable {
    Closure(Rc<ClosureData>),
    Native(NativeMethodKind),
}

pub struct MethodEntry {
    pub inherent: Option<(Callable, bool)>,
    pub traits: BTreeMap<Rc<str>, (Callable, bool)>,
}

impl MethodEntry {
    fn empty() -> Self {
        MethodEntry { inherent: None, traits: BTreeMap::new() }
    }
}

fn build_initial_method_table(names: &BuiltinTypeNames) -> HashMap<(Rc<str>, Rc<str>), MethodEntry> {
    let mut table: HashMap<(Rc<str>, Rc<str>), MethodEntry> = HashMap::new();
    let to_string: Rc<str> = Rc::from("to_string");
    let printable: Rc<str> = Rc::from("Printable");
    for tn in [
        &names.number,
        &names.string,
        &names.bool_,
        &names.function,
        &names.option,
        &names.promise,
        &names.vector,
        &names.map_,
    ] {
        let entry = table.entry((tn.clone(), to_string.clone())).or_insert_with(MethodEntry::empty);
        entry.traits.insert(printable.clone(), (Callable::Native(NativeMethodKind::ToString), true));
    }
    let mut set_inherent = |tn: &Rc<str>, mname: &str, kind: NativeMethodKind| {
        table.entry((tn.clone(), Rc::from(mname))).or_insert_with(MethodEntry::empty).inherent =
            Some((Callable::Native(kind), true));
    };
    set_inherent(&names.string, "len", NativeMethodKind::StringLen);
    set_inherent(&names.string, "char_at", NativeMethodKind::StringCharAt);
    set_inherent(&names.function, "arity", NativeMethodKind::FunctionArity);
    set_inherent(&names.vector, "len", NativeMethodKind::VectorLen);
    set_inherent(&names.vector, "push", NativeMethodKind::VectorPush);
    set_inherent(&names.vector, "pop", NativeMethodKind::VectorPop);
    set_inherent(&names.vector, "push_start", NativeMethodKind::VectorPushStart);
    set_inherent(&names.vector, "pop_start", NativeMethodKind::VectorPopStart);
    set_inherent(&names.vector, "copy", NativeMethodKind::VectorCopy);
    set_inherent(&names.map_, "len", NativeMethodKind::MapLen);
    set_inherent(&names.map_, "keys", NativeMethodKind::MapKeys);
    set_inherent(&names.map_, "values", NativeMethodKind::MapValues);
    set_inherent(&names.map_, "has", NativeMethodKind::MapHas);
    set_inherent(&names.map_, "remove", NativeMethodKind::MapRemove);
    set_inherent(&names.map_, "copy", NativeMethodKind::MapCopy);
    drop(set_inherent);

    let index_trait: Rc<str> = Rc::from("Index");
    let index_assign_trait: Rc<str> = Rc::from("IndexAssign");
    for (tn, kind) in [
        (&names.string, NativeMethodKind::StringIndex),
        (&names.vector, NativeMethodKind::VectorIndex),
        (&names.map_, NativeMethodKind::MapIndex),
    ] {
        table
            .entry((tn.clone(), Rc::from("index")))
            .or_insert_with(MethodEntry::empty)
            .traits
            .insert(index_trait.clone(), (Callable::Native(kind), true));
    }
    for (tn, kind) in
        [(&names.vector, NativeMethodKind::VectorIndexAssign), (&names.map_, NativeMethodKind::MapIndexAssign)]
    {
        table
            .entry((tn.clone(), Rc::from("index_assign")))
            .or_insert_with(MethodEntry::empty)
            .traits
            .insert(index_assign_trait.clone(), (Callable::Native(kind), true));
    }
    table
}

// ---------------------------------------------------------------------------
// Argument binding -- docs/MAHC_FORMAT.md #6.1, mirroring `_bind_params`/
// `_bind_method_call` exactly (including every message).
// ---------------------------------------------------------------------------

pub fn bind_params(
    param_count: usize,
    params: Option<&[(Rc<str>, bool)]>,
    values: Vec<Value>,
    kwargs: Vec<(Rc<str>, Value)>,
    label: &str,
) -> RResult<Vec<Value>> {
    let m = values.len();
    let n = param_count;
    let has_any_default = params.is_some_and(|ps| ps.iter().any(|(_, d)| *d));
    if kwargs.is_empty() && !has_any_default {
        if m != n {
            return Err(RuntimeError::new(format!(
                "Argument Count is invalid. {label} accepts {n} arguments but {m} was given"
            )));
        }
        return Ok(values);
    }
    if m > n {
        return Err(RuntimeError::new(format!("{label} takes at most {n} positional arguments but {m} were given")));
    }
    let mut bound: Vec<Value> = values;
    let mut bound_flags: Vec<bool> = vec![true; bound.len()];
    bound.resize(n, Value::Absent);
    bound_flags.resize(n, false);
    let name_to_index: HashMap<&str, usize> = match params {
        Some(ps) => ps.iter().enumerate().map(|(i, (name, _))| (name.as_ref(), i)).collect(),
        None => HashMap::new(),
    };
    for (k, w) in kwargs {
        match name_to_index.get(k.as_ref()) {
            None => return Err(RuntimeError::new(format!("{label} got an unexpected keyword argument '{k}'"))),
            Some(&idx) => {
                if bound_flags[idx] {
                    return Err(RuntimeError::new(format!("{label} got multiple values for argument '{k}'")));
                }
                bound[idx] = w;
                bound_flags[idx] = true;
            }
        }
    }
    for i in 0..n {
        if bound_flags[i] {
            continue;
        }
        let has_default = params.is_some_and(|ps| ps[i].1);
        if !has_default {
            let pname = params.map(|ps| ps[i].0.to_string()).unwrap_or_else(|| format!("#{i}"));
            return Err(RuntimeError::new(format!("{label} is missing required argument '{pname}'")));
        }
    }
    Ok(bound)
}

pub fn bind_method_call(
    recv: Value,
    target: &Callable,
    include_self: bool,
    name: &str,
    values: Vec<Value>,
    kwargs: Vec<(Rc<str>, Value)>,
) -> RResult<Vec<Value>> {
    let label = if include_self { format!("method '{name}'") } else { format!("'{name}'") };
    match target {
        Callable::Closure(c) => {
            if include_self {
                let rest_params: Option<Vec<(Rc<str>, bool)>> = c.func.params.as_ref().map(|p| p[1..].to_vec());
                let bound_rest = bind_params(c.func.param_count - 1, rest_params.as_deref(), values, kwargs, &label)?;
                let mut out = Vec::with_capacity(bound_rest.len() + 1);
                out.push(recv);
                out.extend(bound_rest);
                Ok(out)
            } else {
                bind_params(c.func.param_count, c.func.params.as_deref(), values, kwargs, &label)
            }
        }
        Callable::Native(kind) => {
            if let Some(opt_name) = kind.optional_name() {
                let arity = kind.required_arity();
                let mut params: Vec<(Rc<str>, bool)> =
                    (0..arity).map(|i| (Rc::from(format!("#{i}").as_str()), false)).collect();
                params.push((Rc::from(opt_name), true));
                let mut bound = bind_params(params.len(), Some(&params), values, kwargs, &label)?;
                if matches!(bound[arity], Value::Absent) {
                    bound[arity] = Value::Bool(false); // every optional native param defaults to `false`
                }
                let mut out = Vec::with_capacity(bound.len() + 1);
                out.push(recv);
                out.extend(bound);
                Ok(out)
            } else {
                if !kwargs.is_empty() {
                    return Err(RuntimeError::new(format!(
                        "{label} got an unexpected keyword argument '{}'",
                        kwargs[0].0
                    )));
                }
                let arity = kind.required_arity();
                if values.len() != arity {
                    return Err(RuntimeError::new(format!(
                        "Argument Count is invalid. {label} accepts {arity} arguments but {} was given",
                        values.len()
                    )));
                }
                let mut out = Vec::with_capacity(values.len() + 1);
                out.push(recv);
                out.extend(values);
                Ok(out)
            }
        }
    }
}

fn struct_or_enum_field(recv: &Value, name: &str) -> Option<Value> {
    match recv {
        Value::Struct(s) => s.borrow().get(name).cloned(),
        Value::Enum(e) => e.borrow().get(name).cloned(),
        Value::Promise(p) => {
            if name == "value" {
                p.borrow().settled.clone()
            } else {
                None
            }
        }
        _ => None,
    }
}

fn binop_symbol(op: &BinOp) -> &'static str {
    match op {
        BinOp::Add => "+",
        BinOp::Sub => "-",
        BinOp::Mul => "*",
        BinOp::Div => "/",
        BinOp::Idiv => "//",
        BinOp::Mod => "%",
        BinOp::Pow => "**",
        BinOp::Lt => "<",
        BinOp::Gt => ">",
        BinOp::Le => "<=",
        BinOp::Ge => ">=",
        BinOp::Eq | BinOp::Neq | BinOp::And | BinOp::Or => unreachable!("no symbol needed"),
    }
}

fn value_cmp_lt(a: &Value, b: &Value) -> bool {
    match (a, b) {
        (Value::Number(x), Value::Number(y)) => x < y,
        (Value::Str(x), Value::Str(y)) => x < y,
        _ => false,
    }
}
fn value_cmp_le(a: &Value, b: &Value) -> bool {
    match (a, b) {
        (Value::Number(x), Value::Number(y)) => x <= y,
        (Value::Str(x), Value::Str(y)) => x <= y,
        _ => false,
    }
}

/// docs/MAHC_FORMAT.md #6.3 `matchrange`, never raises.
fn matchrange(val: &Value, lo: Option<&Value>, hi: Option<&Value>, inclusive: bool) -> bool {
    let kind_ok: fn(&Value) -> bool = if is_number(val) {
        is_number
    } else if matches!(val, Value::Str(_)) {
        |v: &Value| matches!(v, Value::Str(_))
    } else {
        return false;
    };
    if let Some(lo) = lo {
        if !kind_ok(lo) {
            return false;
        }
    }
    if let Some(hi) = hi {
        if !kind_ok(hi) {
            return false;
        }
    }
    if let Some(lo) = lo {
        if !value_cmp_le(lo, val) {
            return false;
        }
    }
    if let Some(hi) = hi {
        return if inclusive { value_cmp_le(val, hi) } else { value_cmp_lt(val, hi) };
    }
    true
}

// ---------------------------------------------------------------------------
// Timers (docs/MAHC_FORMAT.md #6.4) -- min-heap on (wake instant, seq).
// ---------------------------------------------------------------------------

struct TimerEntry {
    wake: Instant,
    seq: u64,
    promise: Rc<RefCell<PromiseData>>,
}
impl PartialEq for TimerEntry {
    fn eq(&self, other: &Self) -> bool {
        self.wake == other.wake && self.seq == other.seq
    }
}
impl Eq for TimerEntry {}
impl PartialOrd for TimerEntry {
    fn partial_cmp(&self, other: &Self) -> Option<std::cmp::Ordering> {
        Some(self.cmp(other))
    }
}
impl Ord for TimerEntry {
    fn cmp(&self, other: &Self) -> std::cmp::Ordering {
        // Reversed so `BinaryHeap` (a max-heap) behaves as a min-heap on
        // (wake, seq) -- earliest wake time (ties: earliest registration).
        (other.wake, other.seq).cmp(&(self.wake, self.seq))
    }
}

// ---------------------------------------------------------------------------
// StdinReader -- one Unicode char at a time, UTF-8, for `io.input`.
// ---------------------------------------------------------------------------

fn utf8_seq_len(first_byte: u8) -> usize {
    if first_byte < 0x80 {
        1
    } else if first_byte & 0xE0 == 0xC0 {
        2
    } else if first_byte & 0xF0 == 0xE0 {
        3
    } else if first_byte & 0xF8 == 0xF0 {
        4
    } else {
        1
    }
}

fn read_stdin_char(stdin: &Stdin) -> Option<char> {
    let mut handle = stdin.lock();
    let mut buf = [0u8; 4];
    if handle.read(&mut buf[..1]).ok()? == 0 {
        return None;
    }
    let len = utf8_seq_len(buf[0]);
    for slot in buf.iter_mut().take(len).skip(1) {
        let mut b = [0u8; 1];
        if handle.read(&mut b).ok()? == 0 {
            return None;
        }
        *slot = b[0];
    }
    std::str::from_utf8(&buf[..len]).ok()?.chars().next()
}

// ---------------------------------------------------------------------------
// The VM
// ---------------------------------------------------------------------------

pub enum StepControl {
    Done(Value),
    Suspended,
}

pub struct Vm<'p> {
    code: &'p [LinkedInstr],
    types: &'p [TypeInfo],
    debug: Option<&'p super::link::DebugIndex>,
    pub names: BuiltinTypeNames,
    method_table: HashMap<(Rc<str>, Rc<str>), MethodEntry>,
    return_register: Value,
    timers: BinaryHeap<TimerEntry>,
    timer_seq: u64,
    stdout: io::BufWriter<Stdout>,
    stdin: Stdin,
    to_string_name: Rc<str>,
}

impl<'p> Vm<'p> {
    pub fn write_stdout(&mut self, s: &str) {
        let _ = self.stdout.write_all(s.as_bytes());
    }
    pub fn flush_stdout(&mut self) {
        let _ = self.stdout.flush();
    }
    pub fn read_stdin_char(&self) -> Option<char> {
        read_stdin_char(&self.stdin)
    }
    pub fn schedule_timer(&mut self, delay_seconds: f64, promise: Rc<RefCell<PromiseData>>) {
        let secs = if delay_seconds.is_finite() { delay_seconds.max(0.0) } else { 0.0 };
        let wake = Instant::now() + Duration::from_secs_f64(secs);
        let seq = self.timer_seq;
        self.timer_seq += 1;
        self.timers.push(TimerEntry { wake, seq, promise });
    }

    fn locate(&self, pc: usize, message: &str) -> String {
        let Some(debug) = self.debug else { return message.to_string() };
        let idx = debug.pcs.partition_point(|&x| x <= pc);
        if idx == 0 {
            return message.to_string();
        }
        let (file_idx, line, col) = debug.runs[idx - 1];
        if line == 0 {
            return message.to_string();
        }
        if file_idx == 0 {
            format!("{message} at position #{line}:{col}")
        } else {
            format!("{message} at position {}#{line}:{col}", debug.file_paths[file_idx])
        }
    }

    fn op_add(&mut self, a: &Value, b: &Value) -> RResult<Value> {
        if matches!(a, Value::Str(_)) || matches!(b, Value::Str(_)) {
            let sa = self.to_str(a)?;
            let sb = self.to_str(b)?;
            return Ok(Value::Str(Rc::from(format!("{sa}{sb}").as_str())));
        }
        if let (Value::Number(x), Value::Number(y)) = (a, b) {
            return x.add(y).map(Value::Number).map_err(|e| RuntimeError::new(e.message()));
        }
        Err(RuntimeError::new(format!(
            "Cannot apply '+' to {} and {}",
            type_name_of(a, &self.names),
            type_name_of(b, &self.names)
        )))
    }

    fn op_mul(&self, a: &Value, b: &Value) -> RResult<Value> {
        match (a, b) {
            (Value::Number(x), Value::Number(y)) => x.mul(y).map(Value::Number).map_err(|e| RuntimeError::new(e.message())),
            (Value::Str(s), Value::Number(n)) if n.is_integer() => Ok(repeat_str(s, n)),
            (Value::Number(n), Value::Str(s)) if n.is_integer() => Ok(repeat_str(s, n)),
            _ => Err(RuntimeError::new(format!(
                "Cannot apply '*' to {} and {}",
                type_name_of(a, &self.names),
                type_name_of(b, &self.names)
            ))),
        }
    }

    fn numeric_binop(&self, op: &BinOp, a: &Value, b: &Value) -> RResult<Value> {
        let (Value::Number(x), Value::Number(y)) = (a, b) else {
            return Err(RuntimeError::new(format!(
                "Cannot apply '{}' to {} and {}",
                binop_symbol(op),
                type_name_of(a, &self.names),
                type_name_of(b, &self.names)
            )));
        };
        match op {
            BinOp::Sub => x.sub(y).map(Value::Number).map_err(|e| RuntimeError::new(e.message())),
            BinOp::Div => {
                if y.is_zero() {
                    return Err(RuntimeError::new("Division by zero"));
                }
                x.div(y).map(Value::Number).map_err(|e| RuntimeError::new(e.message()))
            }
            BinOp::Idiv => {
                if y.is_zero() {
                    return Err(RuntimeError::new("Division by zero"));
                }
                x.idiv(y).map(Value::Number).map_err(|e| RuntimeError::new(e.message()))
            }
            BinOp::Mod => {
                if y.is_zero() {
                    return Err(RuntimeError::new("Division by zero"));
                }
                x.rem(y).map(Value::Number).map_err(|e| RuntimeError::new(e.message()))
            }
            BinOp::Pow => {
                // Deliberate deviation matching the Python VM: 0 ** (negative) is Division by zero.
                if x.is_zero() && y.is_negative() {
                    return Err(RuntimeError::new("Division by zero"));
                }
                x.pow(y).map(Value::Number).map_err(|e| RuntimeError::new(e.message()))
            }
            _ => unreachable!(),
        }
    }

    fn compare(&self, op: &BinOp, a: &Value, b: &Value) -> RResult<Value> {
        match (a, b) {
            (Value::Number(_), Value::Number(_)) | (Value::Str(_), Value::Str(_)) => Ok(Value::Bool(match op {
                BinOp::Lt => value_cmp_lt(a, b),
                BinOp::Gt => value_cmp_lt(b, a),
                BinOp::Le => value_cmp_le(a, b),
                BinOp::Ge => value_cmp_le(b, a),
                _ => unreachable!(),
            })),
            _ => Err(RuntimeError::new(format!(
                "Cannot compare {} and {} with '{}'",
                type_name_of(a, &self.names),
                type_name_of(b, &self.names),
                binop_symbol(op)
            ))),
        }
    }

    fn getfield(&self, obj: &Value, field: &str) -> RResult<Value> {
        match obj {
            Value::None => Err(RuntimeError::new(format!("'Option' has no field '{field}'"))),
            Value::Struct(s) => {
                let b = s.borrow();
                b.get(field).cloned().ok_or_else(|| RuntimeError::new(format!("'{}' has no field '{field}'", b.type_name)))
            }
            Value::Enum(e) => {
                let b = e.borrow();
                b.get(field).cloned().ok_or_else(|| RuntimeError::new(format!("'{}' has no field '{field}'", b.type_name)))
            }
            Value::Promise(p) => {
                let b = p.borrow();
                if field == "value" {
                    b.settled.clone().ok_or_else(|| RuntimeError::new("'Promise' has no field 'value'"))
                } else {
                    Err(RuntimeError::new(format!("'Promise' has no field '{field}'")))
                }
            }
            other => Err(RuntimeError::new(format!(
                "Tried to access field '{field}' on a non-struct value ({})",
                type_name_of(other, &self.names)
            ))),
        }
    }

    fn setfield(&self, obj: &Value, field: &str, value: Value) -> RResult<()> {
        match obj {
            Value::None => Err(RuntimeError::new(format!("'Option' has no field '{field}'"))),
            Value::Struct(s) => {
                let mut b = s.borrow_mut();
                if b.set(field, value) {
                    Ok(())
                } else {
                    Err(RuntimeError::new(format!("'{}' has no field '{field}'", b.type_name)))
                }
            }
            Value::Enum(e) => {
                let mut b = e.borrow_mut();
                if b.set(field, value) {
                    Ok(())
                } else {
                    Err(RuntimeError::new(format!("'{}' has no field '{field}'", b.type_name)))
                }
            }
            Value::Promise(p) => {
                let mut b = p.borrow_mut();
                if field == "value" && b.settled.is_some() {
                    b.settled = Some(value);
                    Ok(())
                } else {
                    Err(RuntimeError::new(format!("'Promise' has no field '{field}'")))
                }
            }
            other => Err(RuntimeError::new(format!(
                "Tried to access field '{field}' on a non-struct value ({})",
                type_name_of(other, &self.names)
            ))),
        }
    }

    fn matchenum(&self, val: &Value, ty: &TypeInfo, variant_idx: usize) -> bool {
        let variants = ty.variants().expect("decode validated matchenum targets an enum type");
        let (vname, _) = &variants[variant_idx];
        if ty.name.as_ref() == "Option" {
            match val {
                Value::None => vname.as_ref() == "none",
                Value::Enum(e) => {
                    let b = e.borrow();
                    b.type_name.as_ref() == "Option" && b.variant.as_ref() == vname.as_ref()
                }
                _ => false,
            }
        } else if ty.name.as_ref() == "Promise" {
            match val {
                Value::Promise(p) => (vname.as_ref() == "Settled") == p.borrow().settled.is_some(),
                Value::Enum(e) => {
                    let b = e.borrow();
                    b.type_name.as_ref() == "Promise" && b.variant.as_ref() == vname.as_ref()
                }
                _ => false,
            }
        } else {
            match val {
                Value::Enum(e) => {
                    let b = e.borrow();
                    b.type_name.as_ref() == ty.name.as_ref() && b.variant.as_ref() == vname.as_ref()
                }
                _ => false,
            }
        }
    }

    pub fn find_method(&self, recv: &Value, name: &Rc<str>, trait_: Option<&Rc<str>>) -> RResult<(Callable, bool)> {
        let tname = type_name_of(recv, &self.names);
        let entry = self.method_table.get(&(tname.clone(), name.clone()));
        let mut target: Option<&(Callable, bool)> = None;
        if let Some(e) = entry {
            if let Some(t) = trait_ {
                target = e.traits.get(t.as_ref());
            } else if let Some(inh) = &e.inherent {
                target = Some(inh);
            } else if e.traits.len() == 1 {
                target = e.traits.values().next();
            } else if e.traits.len() > 1 {
                let names_list: Vec<String> = e.traits.keys().map(|k| format!("'{k}'")).collect();
                return Err(RuntimeError::new(format!(
                    "Method '{name}' on '{tname}' is ambiguous: provided by traits [{}]; call it as 'Trait.{name}(value, ...)'",
                    names_list.join(", ")
                )));
            }
        }
        if trait_.is_none() && target.as_ref().map(|(_, is_method)| !is_method).unwrap_or(true) {
            if let Some(field_val) = struct_or_enum_field(recv, name) {
                return match field_val {
                    Value::Function(c) => Ok((Callable::Closure(c), false)),
                    other => Err(RuntimeError::new(format!(
                        "Field '{name}' of '{tname}' is not a function (it holds a {})",
                        type_name_of(&other, &self.names)
                    ))),
                };
            }
        }
        match target {
            None => {
                if let Some(t) = trait_ {
                    Err(RuntimeError::new(format!("'{tname}' does not implement trait '{t}' (no method '{name}')")))
                } else {
                    Err(RuntimeError::new(format!("'{tname}' has no method '{name}'")))
                }
            }
            Some((callable, is_method)) => {
                if !is_method {
                    return Err(RuntimeError::new(format!(
                        "'{name}' is a static function of '{tname}', not a method; call it as '{tname}.{name}(...)'"
                    )));
                }
                Ok((callable.clone(), true))
            }
        }
    }

    pub fn to_str(&mut self, val: &Value) -> RResult<String> {
        let tname = type_name_of(val, &self.names);
        let target: Option<Callable> = self
            .method_table
            .get(&(tname.clone(), self.to_string_name.clone()))
            .and_then(|e| e.traits.get("Printable"))
            .map(|(c, _)| c.clone());
        match target {
            Some(Callable::Closure(c)) => {
                let result = self.invoke_sync(&c, vec![val.clone()], "to_string")?;
                match result {
                    Value::Str(s) => Ok(s.to_string()),
                    other => Err(RuntimeError::new(format!(
                        "Printable.to_string for '{tname}' must return a String, got {}",
                        type_name_of(&other, &self.names)
                    ))),
                }
            }
            _ => methods::format_value(val, &mut |v| self.to_str(v)),
        }
    }

    pub fn invoke_sync(&mut self, closure: &Rc<ClosureData>, arg_values: Vec<Value>, label: &str) -> RResult<Value> {
        let call_label = match &closure.func.name {
            Some(n) => format!("'{n}'"),
            None => "function".to_string(),
        };
        let bound = bind_params(closure.func.param_count, closure.func.params.as_deref(), arg_values, Vec::new(), &call_label)?;
        let frame = value::new_frame(closure.func.slot_count, Some(closure.defining_frame.clone()));
        {
            let mut fb = frame.borrow_mut();
            for (i, v) in bound.into_iter().enumerate() {
                fb.slots[i] = v;
            }
        }
        let sub_task = value::new_task(closure.func.entry, frame, None);
        match self.step_task(&sub_task)? {
            StepControl::Done(v) => Ok(v),
            StepControl::Suspended => Err(RuntimeError::new(format!(
                "'{label}' cannot suspend (it awaited a pending Promise) when called implicitly by the runtime"
            ))),
        }
    }

    fn enter_closure(&mut self, task: &TaskRef, closure: &Rc<ClosureData>, args: Vec<Value>) {
        let new_frame = value::new_frame(closure.func.slot_count, Some(closure.defining_frame.clone()));
        {
            let mut fb = new_frame.borrow_mut();
            for (i, v) in args.into_iter().enumerate() {
                fb.slots[i] = v;
            }
        }
        let mut t = task.borrow_mut();
        let old_pc = t.pc;
        let old_frame = t.current_frame.clone();
        t.return_stack.push((old_pc, old_frame));
        t.current_frame = new_frame;
        t.pc = closure.func.entry;
    }

    fn spawn_detached(&mut self, closure: &Rc<ClosureData>, args: Vec<Value>) -> RResult<Rc<RefCell<PromiseData>>> {
        let new_frame = value::new_frame(closure.func.slot_count, Some(closure.defining_frame.clone()));
        {
            let mut fb = new_frame.borrow_mut();
            for (i, v) in args.into_iter().enumerate() {
                fb.slots[i] = v;
            }
        }
        let promise = PromiseData::new_pending();
        let new_task = value::new_task(closure.func.entry, new_frame, Some(promise.clone()));
        self.drive(new_task)?;
        Ok(promise)
    }

    fn resolve_promise(&mut self, p: &Rc<RefCell<PromiseData>>, value: Value) -> RResult<()> {
        if p.borrow().settled.is_some() {
            return Ok(());
        }
        p.borrow_mut().settled = Some(value.clone());
        let callbacks = std::mem::take(&mut p.borrow_mut().callbacks);
        for cb in callbacks {
            let frame = cb.task.borrow().current_frame.clone();
            value::write_addr(&frame, cb.dest, value.clone())?;
            cb.task.borrow_mut().pc = cb.resume_pc;
            self.drive(cb.task.clone())?;
        }
        Ok(())
    }

    fn drive(&mut self, task: TaskRef) -> RResult<()> {
        match self.step_task(&task)? {
            StepControl::Done(v) => {
                let watching = task.borrow().watching_promise.clone();
                if let Some(p) = watching {
                    self.resolve_promise(&p, v)?;
                }
            }
            StepControl::Suspended => {}
        }
        Ok(())
    }

    fn step_task(&mut self, task: &TaskRef) -> RResult<StepControl> {
        loop {
            let current_pc = task.borrow().pc;
            task.borrow_mut().pc = current_pc + 1;
            match self.exec_one(task, current_pc) {
                Ok(Some(outcome)) => return Ok(outcome),
                Ok(None) => continue,
                Err(mut e) => {
                    if !e.located {
                        e.message = self.locate(current_pc, &e.message);
                        e.located = true;
                    }
                    return Err(e);
                }
            }
        }
    }

    fn drain_next_timer(&mut self) -> RResult<bool> {
        let Some(TimerEntry { wake, seq: _, promise }) = self.timers.pop() else { return Ok(false) };
        let now = Instant::now();
        if wake > now {
            self.flush_stdout();
            std::thread::sleep(wake - now);
        }
        self.resolve_promise(&promise, Value::None)?;
        Ok(true)
    }

    #[allow(clippy::too_many_lines)]
    fn exec_one(&mut self, task: &TaskRef, pc: usize) -> RResult<Option<StepControl>> {
        let instr =
            self.code.get(pc).ok_or_else(|| RuntimeError::new("program counter out of range"))?;
        let frame: FrameRef = task.borrow().current_frame.clone();
        let rd = |a: Addr| value::read_addr(&frame, a);
        macro_rules! wr {
            ($addr:expr, $val:expr) => {
                value::write_addr(&frame, $addr, $val)?
            };
        }
        match instr {
            LinkedInstr::Halt => return Ok(Some(StepControl::Done(Value::None))),
            LinkedInstr::Move { src, dest } => wr!(*dest, rd(*src)?),
            LinkedInstr::Loadk { value, dest } => wr!(*dest, value.clone()),
            LinkedInstr::Jmp { target } => task.borrow_mut().pc = *target,
            LinkedInstr::Jmpf { cond, target } => {
                if !truthy(&rd(*cond)?) {
                    task.borrow_mut().pc = *target;
                }
            }
            LinkedInstr::Jmpset { param, target } => {
                if !matches!(rd(*param)?, Value::Absent) {
                    task.borrow_mut().pc = *target;
                }
            }
            LinkedInstr::BinOp { op, a, b, dest } => {
                let av = rd(*a)?;
                let bv = rd(*b)?;
                let result = match op {
                    BinOp::Add => self.op_add(&av, &bv)?,
                    BinOp::Mul => self.op_mul(&av, &bv)?,
                    BinOp::Sub | BinOp::Div | BinOp::Idiv | BinOp::Mod | BinOp::Pow => {
                        self.numeric_binop(op, &av, &bv)?
                    }
                    BinOp::Eq => Value::Bool(values_equal(&av, &bv)),
                    BinOp::Neq => Value::Bool(!values_equal(&av, &bv)),
                    BinOp::Lt | BinOp::Gt | BinOp::Le | BinOp::Ge => self.compare(op, &av, &bv)?,
                    BinOp::And => Value::Bool(truthy(&av) && truthy(&bv)),
                    BinOp::Or => Value::Bool(truthy(&av) || truthy(&bv)),
                };
                wr!(*dest, result);
            }
            LinkedInstr::Neg { a, dest } => {
                let v = rd(*a)?;
                let Value::Number(n) = &v else {
                    return Err(RuntimeError::new(format!("Cannot negate {}", type_name_of(&v, &self.names))));
                };
                let r = n.neg().map_err(|e| RuntimeError::new(e.message()))?;
                wr!(*dest, Value::Number(r));
            }
            LinkedInstr::Not { a, dest } => {
                let v = rd(*a)?;
                wr!(*dest, Value::Bool(!truthy(&v)));
            }
            LinkedInstr::Closure { func, dest } => {
                wr!(*dest, Value::Function(Rc::new(ClosureData { func: func.clone(), defining_frame: frame.clone() })));
            }
            LinkedInstr::Call { callee, args } => {
                let closure_val = rd(*callee)?;
                let Value::Function(c) = &closure_val else {
                    return Err(RuntimeError::new(format!(
                        "Tried to call a non-function value ({})",
                        type_name_of(&closure_val, &self.names)
                    )));
                };
                let label = match &c.func.name {
                    Some(n) => format!("'{n}'"),
                    None => "function".to_string(),
                };
                let values: Vec<Value> = args.iter().map(|a| rd(*a)).collect::<RResult<_>>()?;
                let bound = bind_params(c.func.param_count, c.func.params.as_deref(), values, Vec::new(), &label)?;
                let c = c.clone();
                self.enter_closure(task, &c, bound);
            }
            LinkedInstr::CallKw { callee, args, kwnames } => {
                let closure_val = rd(*callee)?;
                let Value::Function(c) = &closure_val else {
                    return Err(RuntimeError::new(format!(
                        "Tried to call a non-function value ({})",
                        type_name_of(&closure_val, &self.names)
                    )));
                };
                let label = match &c.func.name {
                    Some(n) => format!("'{n}'"),
                    None => "function".to_string(),
                };
                let npos = args.len() - kwnames.len();
                let values: Vec<Value> = args[..npos].iter().map(|a| rd(*a)).collect::<RResult<_>>()?;
                let mut kwargs = Vec::with_capacity(kwnames.len());
                for (j, kw) in kwnames.iter().enumerate() {
                    kwargs.push((kw.clone(), rd(args[npos + j])?));
                }
                let bound = bind_params(c.func.param_count, c.func.params.as_deref(), values, kwargs, &label)?;
                let c = c.clone();
                self.enter_closure(task, &c, bound);
            }
            LinkedInstr::Ret { value } => {
                let v = rd(*value)?;
                self.return_register = v.clone();
                let popped = task.borrow_mut().return_stack.pop();
                match popped {
                    None => return Ok(Some(StepControl::Done(v))),
                    Some((pc, fr)) => {
                        let mut t = task.borrow_mut();
                        t.pc = pc;
                        t.current_frame = fr;
                    }
                }
            }
            LinkedInstr::Retval { dest } => wr!(*dest, self.return_register.clone()),
            LinkedInstr::CallMethod { recv, name, args, trait_ } => {
                let recv_v = rd(*recv)?;
                let (target, include_self) = self.find_method(&recv_v, name, trait_.as_ref())?;
                let values: Vec<Value> = args.iter().map(|a| rd(*a)).collect::<RResult<_>>()?;
                let bound = bind_method_call(recv_v, &target, include_self, name, values, Vec::new())?;
                match &target {
                    Callable::Closure(c) => {
                        let c = c.clone();
                        self.enter_closure(task, &c, bound);
                    }
                    Callable::Native(kind) => {
                        let r = call_native_method(*kind, &bound, self)?;
                        self.return_register = r;
                    }
                }
            }
            LinkedInstr::CallMethodKw { recv, name, args, kwnames, trait_ } => {
                let recv_v = rd(*recv)?;
                let (target, include_self) = self.find_method(&recv_v, name, trait_.as_ref())?;
                let npos = args.len() - kwnames.len();
                let values: Vec<Value> = args[..npos].iter().map(|a| rd(*a)).collect::<RResult<_>>()?;
                let mut kwargs = Vec::with_capacity(kwnames.len());
                for (j, kw) in kwnames.iter().enumerate() {
                    kwargs.push((kw.clone(), rd(args[npos + j])?));
                }
                let bound = bind_method_call(recv_v, &target, include_self, name, values, kwargs)?;
                match &target {
                    Callable::Closure(c) => {
                        let c = c.clone();
                        self.enter_closure(task, &c, bound);
                    }
                    Callable::Native(kind) => {
                        let r = call_native_method(*kind, &bound, self)?;
                        self.return_register = r;
                    }
                }
            }
            LinkedInstr::Defmethod { closure, type_name, trait_, name, is_method } => {
                let closure_v = rd(*closure)?;
                let Value::Function(c) = &closure_v else {
                    return Err(RuntimeError::new("'defmethod' given a non-function value"));
                };
                let entry = self.method_table.entry((type_name.clone(), name.clone())).or_insert_with(MethodEntry::empty);
                match trait_ {
                    None => entry.inherent = Some((Callable::Closure(c.clone()), *is_method)),
                    Some(t) => {
                        entry.traits.insert(t.clone(), (Callable::Closure(c.clone()), *is_method));
                    }
                }
            }
            LinkedInstr::Detach { callee, args, dest } => {
                let closure_val = rd(*callee)?;
                let Value::Function(c) = &closure_val else {
                    return Err(RuntimeError::new(format!(
                        "Tried to detach a non-function value ({})",
                        type_name_of(&closure_val, &self.names)
                    )));
                };
                let label = match &c.func.name {
                    Some(n) => format!("'{n}'"),
                    None => "function".to_string(),
                };
                let values: Vec<Value> = args.iter().map(|a| rd(*a)).collect::<RResult<_>>()?;
                let bound = bind_params(c.func.param_count, c.func.params.as_deref(), values, Vec::new(), &label)?;
                let c = c.clone();
                let promise = self.spawn_detached(&c, bound)?;
                wr!(*dest, Value::Promise(promise));
            }
            LinkedInstr::DetachKw { callee, args, kwnames, dest } => {
                let closure_val = rd(*callee)?;
                let Value::Function(c) = &closure_val else {
                    return Err(RuntimeError::new(format!(
                        "Tried to detach a non-function value ({})",
                        type_name_of(&closure_val, &self.names)
                    )));
                };
                let label = match &c.func.name {
                    Some(n) => format!("'{n}'"),
                    None => "function".to_string(),
                };
                let npos = args.len() - kwnames.len();
                let values: Vec<Value> = args[..npos].iter().map(|a| rd(*a)).collect::<RResult<_>>()?;
                let mut kwargs = Vec::with_capacity(kwnames.len());
                for (j, kw) in kwnames.iter().enumerate() {
                    kwargs.push((kw.clone(), rd(args[npos + j])?));
                }
                let bound = bind_params(c.func.param_count, c.func.params.as_deref(), values, kwargs, &label)?;
                let c = c.clone();
                let promise = self.spawn_detached(&c, bound)?;
                wr!(*dest, Value::Promise(promise));
            }
            LinkedInstr::DetachMethod { recv, name, args, trait_, dest } => {
                let recv_v = rd(*recv)?;
                let (target, include_self) = self.find_method(&recv_v, name, trait_.as_ref())?;
                let values: Vec<Value> = args.iter().map(|a| rd(*a)).collect::<RResult<_>>()?;
                let bound = bind_method_call(recv_v, &target, include_self, name, values, Vec::new())?;
                let promise = match &target {
                    Callable::Closure(c) => self.spawn_detached(c, bound)?,
                    Callable::Native(kind) => {
                        let r = call_native_method(*kind, &bound, self)?;
                        let p = PromiseData::new_pending();
                        self.resolve_promise(&p, r)?;
                        p
                    }
                };
                wr!(*dest, Value::Promise(promise));
            }
            LinkedInstr::DetachMethodKw { recv, name, args, kwnames, trait_, dest } => {
                let recv_v = rd(*recv)?;
                let (target, include_self) = self.find_method(&recv_v, name, trait_.as_ref())?;
                let npos = args.len() - kwnames.len();
                let values: Vec<Value> = args[..npos].iter().map(|a| rd(*a)).collect::<RResult<_>>()?;
                let mut kwargs = Vec::with_capacity(kwnames.len());
                for (j, kw) in kwnames.iter().enumerate() {
                    kwargs.push((kw.clone(), rd(args[npos + j])?));
                }
                let bound = bind_method_call(recv_v, &target, include_self, name, values, kwargs)?;
                let promise = match &target {
                    Callable::Closure(c) => self.spawn_detached(c, bound)?,
                    Callable::Native(kind) => {
                        let r = call_native_method(*kind, &bound, self)?;
                        let p = PromiseData::new_pending();
                        self.resolve_promise(&p, r)?;
                        p
                    }
                };
                wr!(*dest, Value::Promise(promise));
            }
            LinkedInstr::Await { promise, dest } => {
                let pv = rd(*promise)?;
                let Value::Promise(p) = &pv else {
                    return Err(RuntimeError::new(format!(
                        "'.await' used on a non-Promise value ({})",
                        type_name_of(&pv, &self.names)
                    )));
                };
                let settled = p.borrow().settled.clone();
                match settled {
                    Some(v) => wr!(*dest, v),
                    None => {
                        let resume_pc = task.borrow().pc;
                        p.borrow_mut().callbacks.push(Continuation { task: task.clone(), dest: *dest, resume_pc });
                        return Ok(Some(StepControl::Suspended));
                    }
                }
            }
            LinkedInstr::Struct { type_idx, values, dest } => {
                let ty = &self.types[*type_idx];
                let field_names = ty.field_names().expect("decode validated struct type_idx targets a struct");
                let mut fields = Vec::with_capacity(values.len());
                for (fname, addr) in field_names.iter().zip(values.iter()) {
                    fields.push((fname.clone(), rd(*addr)?));
                }
                let type_name = ty.name.clone();
                wr!(*dest, Value::Struct(Rc::new(RefCell::new(StructData { type_name, fields }))));
            }
            LinkedInstr::Enum { type_idx, variant, values, dest } => {
                let ty = &self.types[*type_idx];
                let variants = ty.variants().expect("decode validated enum type_idx targets an enum");
                let (vname, vfields) = &variants[*variant];
                // `Promise.Settled { ... }` written in Mah code is a plain enum
                // instance, like in the Python VM: only `detach` makes real
                // (awaitable) Promises.
                if ty.name.as_ref() == "Option" && vname.as_ref() == "none" {
                    wr!(*dest, Value::None);
                } else {
                    let mut fields = Vec::with_capacity(values.len());
                    for (fname, addr) in vfields.iter().zip(values.iter()) {
                        fields.push((fname.clone(), rd(*addr)?));
                    }
                    let type_name = ty.name.clone();
                    let variant_name = vname.clone();
                    wr!(*dest, Value::Enum(Rc::new(RefCell::new(EnumData { type_name, variant: variant_name, fields }))));
                }
            }
            LinkedInstr::GetField { obj, field, dest } => {
                let ov = rd(*obj)?;
                let v = self.getfield(&ov, field)?;
                wr!(*dest, v);
            }
            LinkedInstr::SetField { obj, field, src } => {
                let ov = rd(*obj)?;
                let sv = rd(*src)?;
                self.setfield(&ov, field, sv)?;
            }
            LinkedInstr::MatchStruct { value, type_idx, dest } => {
                let v = rd(*value)?;
                let ty = &self.types[*type_idx];
                let m = matches!(&v, Value::Struct(s) if s.borrow().type_name.as_ref() == ty.name.as_ref());
                wr!(*dest, Value::Bool(m));
            }
            LinkedInstr::MatchEnum { value, type_idx, variant, dest } => {
                let v = rd(*value)?;
                let m = self.matchenum(&v, &self.types[*type_idx], *variant);
                wr!(*dest, Value::Bool(m));
            }
            LinkedInstr::MatchFail => return Err(RuntimeError::new("No pattern in 'match' matched the value")),
            LinkedInstr::MatchRange { value, lo, hi, inclusive, dest } => {
                let v = rd(*value)?;
                let lov = match lo {
                    Some(a) => Some(rd(*a)?),
                    None => None,
                };
                let hiv = match hi {
                    Some(a) => Some(rd(*a)?),
                    None => None,
                };
                let m = matchrange(&v, lov.as_ref(), hiv.as_ref(), *inclusive);
                wr!(*dest, Value::Bool(m));
            }
            LinkedInstr::Vector { items, dest } => {
                let vals: Vec<Value> = items.iter().map(|a| rd(*a)).collect::<RResult<_>>()?;
                wr!(*dest, Value::Vector(Rc::new(RefCell::new(vals))));
            }
            LinkedInstr::Map { pairs, dest } => {
                let m = MapData::new();
                for chunk in pairs.chunks(2) {
                    let k = rd(chunk[0])?;
                    let v = rd(chunk[1])?;
                    let mk = map_key(&k).ok_or_else(|| {
                        RuntimeError::new(format!(
                            "Map keys must be a String, Number, or Bool, got {}",
                            type_name_of(&k, &self.names)
                        ))
                    })?;
                    m.borrow_mut().index_assign(mk, k, v);
                }
                wr!(*dest, Value::Map(m));
            }
            LinkedInstr::DeferPush => task.borrow_mut().defer_stack.push(Vec::new()),
            LinkedInstr::DeferAdd { closure } => {
                let v = rd(*closure)?;
                task.borrow_mut().defer_stack.last_mut().expect("deferadd without deferpush").push(v);
            }
            LinkedInstr::DeferPeek { dest } => {
                let nonempty = !task.borrow().defer_stack.last().expect("deferpeek without deferpush").is_empty();
                wr!(*dest, Value::Bool(nonempty));
            }
            LinkedInstr::DeferPop { dest } => {
                let v = task
                    .borrow_mut()
                    .defer_stack
                    .last_mut()
                    .expect("deferpop without deferpush")
                    .pop()
                    .expect("deferpop on an empty scope");
                wr!(*dest, v);
            }
            LinkedInstr::DeferScopePop => {
                task.borrow_mut().defer_stack.pop();
            }
            LinkedInstr::Native { native, args, dest } => {
                let vals: Vec<Value> = args.iter().map(|a| rd(*a)).collect::<RResult<_>>()?;
                let r = natives::call_native(self, *native, &vals)?;
                match dest {
                    Some(d) => wr!(*d, r),
                    None => self.return_register = r,
                }
            }
        }
        Ok(None)
    }
}

fn repeat_str(s: &Rc<str>, n: &crate::decimal::Decimal) -> Value {
    let count = n.to_i64().unwrap_or(0);
    if count > 0 {
        Value::Str(Rc::from(s.repeat(count as usize).as_str()))
    } else {
        Value::Str(Rc::from(""))
    }
}

/// Run a fully linked program to completion -- docs/MAHC_FORMAT.md #6.1's
/// startup plus #6.4's scheduler loop.
pub fn execute(linked: &LinkedProgram) -> RResult<()> {
    let names = BuiltinTypeNames::new();
    let method_table = build_initial_method_table(&names);
    let mut vm = Vm {
        code: &linked.code,
        types: &linked.types,
        debug: linked.debug.as_ref(),
        names,
        method_table,
        return_register: Value::None,
        timers: BinaryHeap::new(),
        timer_seq: 0,
        stdout: io::BufWriter::new(io::stdout()),
        stdin: io::stdin(),
        to_string_name: Rc::from("to_string"),
    };
    if linked.functions.is_empty() {
        return Err(RuntimeError::new("FUNCTIONS section must declare at least one function"));
    }
    let main_fn = &linked.functions[0];
    let main_frame = value::new_frame(main_fn.slot_count, None);
    let main_promise = PromiseData::new_pending();
    let main_task = value::new_task(main_fn.entry, main_frame, Some(main_promise.clone()));
    vm.drive(main_task)?;

    loop {
        let settled = main_promise.borrow().settled.is_some();
        if settled && vm.timers.is_empty() {
            break;
        }
        if !vm.drain_next_timer()? {
            break;
        }
    }
    vm.flush_stdout();
    Ok(())
}
