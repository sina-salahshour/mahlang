//! Runtime values -- docs/MAHC_FORMAT.md #5, mirroring
//! `mah/runtime_values.py`. Struct/enum/Vector/Map/Promise instances are
//! `Rc<RefCell<_>>` ("mutable, by reference"); Numbers/Strings/Bools/`none`
//! are plain values (immutable, compared by content). `none` is not
//! wrapped in `Rc` at all: it's a single, unique, never-mutated value, so
//! `Value::None` already gives every observable property the Python
//! singleton `NONE_VALUE` has (including `none == none`).

use std::cell::RefCell;
use std::collections::HashMap;
use std::rc::Rc;

use crate::decimal::Decimal;
use crate::decode::Addr;

use super::error::RuntimeError;

// ---------------------------------------------------------------------------
// Frames
// ---------------------------------------------------------------------------

pub struct FrameData {
    pub slots: Vec<Value>,
    pub static_parent: Option<FrameRef>,
}

pub type FrameRef = Rc<RefCell<FrameData>>;

pub fn new_frame(slot_count: usize, static_parent: Option<FrameRef>) -> FrameRef {
    Rc::new(RefCell::new(FrameData { slots: vec![Value::None; slot_count], static_parent }))
}

/// `A` operand read: walk `static_parent` `depth` times from `frame`, then
/// read `slot`. A depth/slot bytecode never emits as out of range for a
/// well-formed program; a hand-corrupted CODE section can still make this
/// fail, so it's a runtime error (not a panic) either way, matching the
/// Python VM wrapping the equivalent `AttributeError`/`IndexError`.
pub fn read_addr(frame: &FrameRef, addr: Addr) -> Result<Value, RuntimeError> {
    let mut f = frame.clone();
    for _ in 0..addr.0 {
        let parent = f.borrow().static_parent.clone();
        f = parent.ok_or_else(|| RuntimeError::new("invalid frame address (depth exceeds the static chain)"))?;
    }
    let b = f.borrow();
    b.slots
        .get(addr.1 as usize)
        .cloned()
        .ok_or_else(|| RuntimeError::new("list index out of range"))
}

pub fn write_addr(frame: &FrameRef, addr: Addr, value: Value) -> Result<(), RuntimeError> {
    let mut f = frame.clone();
    for _ in 0..addr.0 {
        let parent = f.borrow().static_parent.clone();
        f = parent.ok_or_else(|| RuntimeError::new("invalid frame address (depth exceeds the static chain)"))?;
    }
    let mut b = f.borrow_mut();
    match b.slots.get_mut(addr.1 as usize) {
        Some(slot) => {
            *slot = value;
            Ok(())
        }
        None => Err(RuntimeError::new("list index out of range")),
    }
}

// ---------------------------------------------------------------------------
// Functions / closures
// ---------------------------------------------------------------------------

pub struct FunctionInfo {
    /// M41a: this function's FUNCTIONS index -- the key into META.
    pub index: usize,
    pub entry: usize,
    pub slot_count: usize,
    pub param_count: usize,
    pub name: Option<Rc<str>>,
    /// Parallel to param slots `0..param_count-1`: `(name, has_default)`.
    /// `None` for a 1.0 file (unnamed, all-required parameters).
    pub params: Option<Vec<(Rc<str>, bool)>>,
    /// M41c: rest-parameter flags -- bit 0 `...`, bit 1 `**` (PARAMS, 1.16).
    pub rest: u8,
}

pub struct ClosureData {
    pub func: Rc<FunctionInfo>,
    pub defining_frame: FrameRef,
    /// M41c: the FUNCTIONS index `reflect.signature`, the decorator lookups
    /// and item-type dispatch use -- `func.index`, unless `hooks.adopt` gave
    /// this closure the identity of the function it wraps.
    pub identity: std::cell::Cell<usize>,
}

// ---------------------------------------------------------------------------
// Struct / enum instances
// ---------------------------------------------------------------------------

pub struct StructData {
    pub type_name: Rc<str>,
    pub fields: Vec<(Rc<str>, Value)>,
    /// M25 (docs/MAHC_FORMAT.md #4.4): VM-internal, never visible to Mah
    /// code -- the pc a `throw` of this value first happened at (unset
    /// stays `None`), used only to locate an uncaught error; re-throwing
    /// (including automatically, when no catch arm matches) keeps the
    /// original.
    pub thrown_at: Option<usize>,
    /// M28: recorded with `thrown_at` -- that pc, then each enclosing call
    /// site in the throwing task, innermost first (`mah test` reports).
    pub backtrace: Option<Vec<usize>>,
}

impl StructData {
    pub fn get(&self, name: &str) -> Option<&Value> {
        self.fields.iter().find(|(n, _)| &**n == name).map(|(_, v)| v)
    }
    pub fn set(&mut self, name: &str, value: Value) -> bool {
        match self.fields.iter_mut().find(|(n, _)| &**n == name) {
            Some((_, v)) => {
                *v = value;
                true
            }
            None => false,
        }
    }
}

pub struct EnumData {
    pub type_name: Rc<str>,
    pub variant: Rc<str>,
    pub fields: Vec<(Rc<str>, Value)>,
    /// M25/M28: see `StructData`'s `thrown_at`/`backtrace` above.
    pub thrown_at: Option<usize>,
    pub backtrace: Option<Vec<usize>>,
}

impl EnumData {
    pub fn get(&self, name: &str) -> Option<&Value> {
        self.fields.iter().find(|(n, _)| &**n == name).map(|(_, v)| v)
    }
    pub fn set(&mut self, name: &str, value: Value) -> bool {
        match self.fields.iter_mut().find(|(n, _)| &**n == name) {
            Some((_, v)) => {
                *v = value;
                true
            }
            None => false,
        }
    }
}

// ---------------------------------------------------------------------------
// Promises (docs/MAHC_FORMAT.md #6.4) -- an enum instance of type
// "Promise" (`Pending`/`Settled { value }`) plus interpreter-internal
// scheduling state (never a Mah-visible field).
// ---------------------------------------------------------------------------

/// A registered `.await` continuation: write the resolved value to `dest`
/// in `task`'s current frame, then resume `task` at `resume_pc`, running it
/// via `drive` (stored as data, not a boxed closure, since resuming needs
/// `&mut Vm` -- see `super::exec`).
pub struct Continuation {
    pub task: TaskRef,
    pub dest: Addr,
    pub resume_pc: usize,
}

pub struct PromiseData {
    /// `None` = Pending, `Some(v)` = Settled { value: v }.
    pub settled: Option<Value>,
    /// M25 (docs/MAHC_FORMAT.md #4.6): `Some(e)` = Failed { error: e }.
    /// `settled`/`failed` are never both `Some` -- resolving/failing an
    /// already-settled-or-failed Promise does nothing (see `Vm::resolve_promise`/
    /// `Vm::fail_promise`).
    pub failed: Option<Value>,
    /// M25: set the first time any task `.await`s this Promise (settled,
    /// pending, or already failed) -- used at program end to find failed
    /// Promises nobody ever looked at.
    pub observed: bool,
    pub callbacks: Vec<Continuation>,
    /// M44 (docs/contracts/M44_threads.md #6.4): who settles this Promise,
    /// for deadlock detection.
    pub producer: Option<Producer>,
}

/// M44: the producer of a Promise -- a same-VM detached task, a job, or the
/// joins of a thread (an edge of the wait-for graph).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Producer {
    Task(u64),
    Job(u64),
    Join(u64),
}

impl PromiseData {
    pub fn new_pending() -> Rc<RefCell<PromiseData>> {
        Rc::new(RefCell::new(PromiseData {
            settled: None,
            failed: None,
            observed: false,
            callbacks: Vec::new(),
            producer: None,
        }))
    }

    pub fn is_pending(&self) -> bool {
        self.settled.is_none() && self.failed.is_none()
    }
}

// ---------------------------------------------------------------------------
// Tasks (docs/MAHC_FORMAT.md #6.1/#6.4)
// ---------------------------------------------------------------------------

pub struct Task {
    pub pc: usize,
    pub current_frame: FrameRef,
    pub return_stack: Vec<(usize, FrameRef)>,
    pub defer_stack: Vec<Vec<Value>>,
    pub watching_promise: Option<Rc<RefCell<PromiseData>>>,
    /// M44 (docs/contracts/M44_threads.md #6.1): a process-unique id, the
    /// shared variables this task holds (shared by reference with an
    /// implicit-call sub-task, #5.2), and whether it recorded an await edge.
    pub id: u64,
    pub held: Rc<RefCell<Vec<Held>>>,
    pub awaits_edge: bool,
}

/// M44: one shared variable a task holds: its working value, the lock's
/// re-entrancy depth, and `unwinding` (0, or the depth a throw is leaving).
pub struct Held {
    pub index: u64,
    pub value: Value,
    pub depth: u32,
    pub unwinding: u32,
}

pub type TaskRef = Rc<RefCell<Task>>;

/// M44: task ids are unique across every VM of the process.
static NEXT_TASK_ID: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(1);

pub fn new_task(pc: usize, frame: FrameRef, watching_promise: Option<Rc<RefCell<PromiseData>>>) -> TaskRef {
    Rc::new(RefCell::new(Task {
        pc,
        current_frame: frame,
        return_stack: Vec::new(),
        defer_stack: Vec::new(),
        watching_promise,
        id: NEXT_TASK_ID.fetch_add(1, std::sync::atomic::Ordering::Relaxed),
        held: Rc::new(RefCell::new(Vec::new())),
        awaits_edge: false,
    }))
}

// ---------------------------------------------------------------------------
// Vectors / Maps (docs/MAHC_FORMAT.md #6.9, 1.3+)
// ---------------------------------------------------------------------------

pub type VectorRef = Rc<RefCell<Vec<Value>>>;

/// A Map key: `(kind, value)` so `true`/`1` differ, while `1`/`1.0` are the
/// same Number key (`Decimal` is kept in canonical form, so its derived
/// `Eq`/`Hash` are already numeric equality) and Strings compare/hash by
/// content (an `Rc<str>`'s `Eq`/`Hash` impls delegate to the pointee).
#[derive(Clone, PartialEq, Eq, Hash)]
pub enum MapKey {
    Bool(bool),
    Number(Decimal),
    Str(Rc<str>),
}

pub struct MapData {
    /// Insertion order of keys (a new key is appended; an existing key
    /// keeps its position -- see `MahMap::index_assign`).
    pub order: Vec<MapKey>,
    /// key -> (original key Value, current value). The original key Value
    /// is kept (not derived from `MapKey`) so `to_string`/iteration show
    /// `1` after `m[1] = a; m[1.0] = b` -- docs/MAHC_FORMAT.md #6.9.
    pub entries: HashMap<MapKey, (Value, Value)>,
}

impl MapData {
    pub fn new() -> Rc<RefCell<MapData>> {
        Rc::new(RefCell::new(MapData { order: Vec::new(), entries: HashMap::new() }))
    }

    pub fn len(&self) -> usize {
        self.order.len()
    }

    pub fn get(&self, key: &MapKey) -> Option<&Value> {
        self.entries.get(key).map(|(_, v)| v)
    }

    pub fn index_assign(&mut self, key: MapKey, original_key: Value, value: Value) {
        match self.entries.get_mut(&key) {
            Some(entry) => entry.1 = value,
            None => {
                self.order.push(key.clone());
                self.entries.insert(key, (original_key, value));
            }
        }
    }

    pub fn remove(&mut self, key: &MapKey) -> Option<Value> {
        let removed = self.entries.remove(key);
        if removed.is_some() {
            self.order.retain(|k| k != key);
        }
        removed.map(|(_, v)| v)
    }

    /// Entries in insertion order: `(original key, value)`.
    pub fn iter_ordered(&self) -> impl Iterator<Item = (&Value, &Value)> {
        self.order.iter().map(move |k| {
            let (key, val) = self.entries.get(k).expect("order/entries out of sync");
            (key, val)
        })
    }
}

pub type MapRef = Rc<RefCell<MapData>>;

/// M37 (1.17): a Bytes value -- mutable, by reference, `==` by content.
pub type BytesRef = Rc<RefCell<Vec<u8>>>;

// ---------------------------------------------------------------------------
// Type values (M41a, docs/MAHC_FORMAT.md #5)
// ---------------------------------------------------------------------------

/// What a bare type name evaluates to. `kind` 0: `index` is a TYPES index
/// (the built-in enums 0-2, then user types); kind 1: a primitive code
/// (`PRIMITIVE_TYPE_NAMES`). Two Type values are `==` when kind and index
/// match; `name` is what `to_string` shows.
pub struct TypeData {
    pub kind: u8,
    pub index: usize,
    pub name: Rc<str>,
}

/// M41a: the primitive types a `Type` can be, by code (`loadtype 1, code`).
/// M37 (1.17) adds `Bytes`, code 8.
pub const PRIMITIVE_TYPE_NAMES: [&str; 9] =
    ["Number", "String", "Bool", "Function", "Vector", "Map", "None", "Type", "Bytes"];

// ---------------------------------------------------------------------------
// The value type
// ---------------------------------------------------------------------------

#[derive(Clone)]
pub enum Value {
    /// The single shared `Option.none` -- docs/MAHC_FORMAT.md #5.
    None,
    Bool(bool),
    Number(Decimal),
    /// Immutable Unicode text; `Rc<str>` clone is cheap (matches Mah's
    /// value semantics for Strings: content, not identity).
    Str(Rc<str>),
    Function(Rc<ClosureData>),
    Struct(Rc<RefCell<StructData>>),
    Enum(Rc<RefCell<EnumData>>),
    Promise(Rc<RefCell<PromiseData>>),
    Vector(VectorRef),
    Map(MapRef),
    /// M41a (1.14): a `Type` value -- a bare type name.
    Type(Rc<TypeData>),
    /// M37 (1.17): Bytes.
    Bytes(BytesRef),
    /// Interpreter-internal only: a defaulted-but-unbound parameter slot,
    /// until the callee's own `jmpset`-guarded default-computation code
    /// runs. Type name "Unknown"; never a real Mah value.
    Absent,
}

/// The built-in type names, interned once (so `type_name_of` never
/// allocates -- `Rc<str>::clone` on an already-built `Rc` is just a
/// refcount bump). A user struct/enum instance already owns its own
/// `Rc<str>` name (shared from `TypeInfo`, itself interned once at link
/// time), so the whole VM allocates each type name string exactly once.
#[derive(Clone)]
pub struct BuiltinTypeNames {
    pub number: Rc<str>,
    pub string: Rc<str>,
    pub bool_: Rc<str>,
    pub function: Rc<str>,
    pub option: Rc<str>,
    pub promise: Rc<str>,
    pub vector: Rc<str>,
    pub map_: Rc<str>,
    pub type_: Rc<str>,
    pub bytes: Rc<str>,
    pub unknown: Rc<str>,
}

impl BuiltinTypeNames {
    pub fn new() -> Self {
        BuiltinTypeNames {
            number: Rc::from("Number"),
            string: Rc::from("String"),
            bool_: Rc::from("Bool"),
            function: Rc::from("Function"),
            option: Rc::from("Option"),
            promise: Rc::from("Promise"),
            vector: Rc::from("Vector"),
            map_: Rc::from("Map"),
            type_: Rc::from("Type"),
            bytes: Rc::from("Bytes"),
            unknown: Rc::from("Unknown"),
        }
    }
}

impl Default for BuiltinTypeNames {
    fn default() -> Self {
        Self::new()
    }
}

/// The runtime type name `impl`/method dispatch sees `v` as -- docs/
/// MAHC_FORMAT.md #5/#6.7. A `Struct`/`Enum` instance reports its own
/// `type_name`; everything else reports its fixed built-in name.
/// M41s (docs/MAHC_FORMAT.md #4.3): a type or trait name of the form
/// `__mah_m<digits>_<rest>` (a module-scoped name, renamed by the
/// preprocessor) displays as `<rest>`. Dispatch keeps the full name.
pub fn display_name(name: &str) -> &str {
    match mangled_len(name.as_bytes()) {
        Some(n) => &name[n..],
        None => name,
    }
}

/// Length of a leading `__mah_m<digits>_` in `b`, if there is one (and
/// something follows it).
fn mangled_len(b: &[u8]) -> Option<usize> {
    const PREFIX: &[u8] = b"__mah_m";
    if !b.starts_with(PREFIX) {
        return None;
    }
    let mut i = PREFIX.len();
    let digits = i;
    while i < b.len() && b[i].is_ascii_digit() {
        i += 1;
    }
    if i == digits || i >= b.len() || b[i] != b'_' || i + 1 >= b.len() {
        return None;
    }
    Some(i + 1)
}

/// `display_name` applied to every mangled name inside a message.
pub fn demangle_text(text: String) -> String {
    if !text.contains("__mah_m") {
        return text;
    }
    let bytes = text.as_bytes();
    let mut out = String::with_capacity(text.len());
    let mut i = 0;
    let mut last = 0;
    while i < bytes.len() {
        if bytes[i] == b'_' {
            if let Some(n) = mangled_len(&bytes[i..]) {
                out.push_str(&text[last..i]);
                i += n;
                last = i;
                continue;
            }
        }
        i += 1;
    }
    out.push_str(&text[last..]);
    out
}

pub fn type_name_of(v: &Value, names: &BuiltinTypeNames) -> Rc<str> {
    match v {
        Value::None => names.option.clone(),
        Value::Bool(_) => names.bool_.clone(),
        Value::Number(_) => names.number.clone(),
        Value::Str(_) => names.string.clone(),
        Value::Function(_) => names.function.clone(),
        Value::Struct(s) => s.borrow().type_name.clone(),
        Value::Enum(e) => e.borrow().type_name.clone(),
        Value::Promise(_) => names.promise.clone(),
        Value::Vector(_) => names.vector.clone(),
        Value::Map(_) => names.map_.clone(),
        Value::Type(_) => names.type_.clone(),
        Value::Bytes(_) => names.bytes.clone(),
        Value::Absent => names.unknown.clone(),
    }
}

/// docs/MAHC_FORMAT.md #5: exactly four falsy values -- `false`, `none`,
/// the Number `0`, and the empty String.
pub fn truthy(v: &Value) -> bool {
    match v {
        Value::None => false,
        Value::Bool(b) => *b,
        Value::Number(n) => !n.is_zero(),
        Value::Str(s) => !s.is_empty(),
        _ => true,
    }
}

/// docs/MAHC_FORMAT.md #6.2: Numbers/Strings/Bools compare by value, `none`
/// equals only `none`; everything else (struct/enum -- including
/// `some(x)` -- Function, Promise, Vector, Map) is equal only to itself
/// (identity). M37: Bytes compare by content.
pub fn values_equal(a: &Value, b: &Value) -> bool {
    match (a, b) {
        (Value::None, Value::None) => true,
        (Value::Bool(x), Value::Bool(y)) => x == y,
        (Value::Number(x), Value::Number(y)) => x == y,
        (Value::Str(x), Value::Str(y)) => x == y,
        (Value::Function(x), Value::Function(y)) => Rc::ptr_eq(x, y),
        (Value::Struct(x), Value::Struct(y)) => Rc::ptr_eq(x, y),
        (Value::Enum(x), Value::Enum(y)) => Rc::ptr_eq(x, y),
        (Value::Promise(x), Value::Promise(y)) => Rc::ptr_eq(x, y),
        (Value::Vector(x), Value::Vector(y)) => Rc::ptr_eq(x, y),
        (Value::Map(x), Value::Map(y)) => Rc::ptr_eq(x, y),
        (Value::Type(x), Value::Type(y)) => x.kind == y.kind && x.index == y.index,
        (Value::Bytes(x), Value::Bytes(y)) => *x.borrow() == *y.borrow(),
        _ => false,
    }
}

/// The dict key a Mah Map stores `key` under, or `None` if `key` can't be
/// a Map key (only Strings, Numbers, Bools can).
pub fn map_key(key: &Value) -> Option<MapKey> {
    match key {
        Value::Bool(b) => Some(MapKey::Bool(*b)),
        Value::Number(n) => Some(MapKey::Number(n.clone())),
        Value::Str(s) => Some(MapKey::Str(s.clone())),
        _ => None,
    }
}

pub fn is_number(v: &Value) -> bool {
    matches!(v, Value::Number(_))
}
