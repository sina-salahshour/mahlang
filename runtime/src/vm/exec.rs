//! The step loop, method dispatch, scheduler, and arithmetic -- a Rust port
//! of `code_interpreter.py`'s `_execute`/`_exec`/`step_task`/`drive`.

use std::cell::RefCell;
use std::collections::{BTreeMap, BinaryHeap, HashMap};
use std::io::{self, Read, Stdin, Write};
use std::rc::Rc;
use std::sync::mpsc::{Receiver, Sender};
use std::sync::Arc;
use std::time::{Duration, Instant};

use crate::decimal::Decimal;
use crate::decode::{Addr, BinOp, HandlerEntry, Program};

use super::error::{ErrorKind, RResult, RuntimeError};
use super::link::{LinkedInstr, LinkedProgram, TypeInfo, TypeKind};
use super::methods::{self, call_native_method, NativeMethodKind};
use super::natives;
use super::thread::{self as threads, NotSendable, Payload, Snapshot, ThreadRuntime};
use super::value::{
    self, display_name, is_number, map_key, truthy, type_name_of, values_equal, BuiltinTypeNames, ClosureData, Continuation,
    EnumData, FrameRef, MapData, Producer, PromiseData, StructData, TaskRef, Value,
};

/// M25 (docs/MAHC_FORMAT.md #6.3): `matchtype` -- any variant matches, for
/// an enum, so this is `matchstruct`/`matchenum` minus the variant check.
/// `Value::None`/`Value::Promise` need their own arms since they aren't
/// `Value::Enum` in this VM's representation (unlike the Python VM, where
/// every enum-shaped value -- including `none` and every Promise -- IS an
/// `EnumInstance`).
fn matches_type(val: &Value, ty: &TypeInfo) -> bool {
    match &ty.kind {
        TypeKind::Struct(_) => matches!(val, Value::Struct(s) if s.borrow().type_name.as_ref() == ty.name.as_ref()),
        TypeKind::Enum(_) => match val {
            Value::Enum(e) => e.borrow().type_name.as_ref() == ty.name.as_ref(),
            Value::None => ty.name.as_ref() == "Option",
            Value::Promise(_) => ty.name.as_ref() == "Promise",
            _ => false,
        },
    }
}

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
        &names.type_,
        &names.bytes,
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
    // M29 (1.6): the String methods and Vector.join -- docs/STDLIB.md,
    // mirroring mah/string_methods.py.
    for (mname, kind) in [
        ("split", NativeMethodKind::StringSplit),
        ("trim", NativeMethodKind::StringTrim),
        ("trim_start", NativeMethodKind::StringTrimStart),
        ("trim_end", NativeMethodKind::StringTrimEnd),
        ("pad_start", NativeMethodKind::StringPadStart),
        ("pad_end", NativeMethodKind::StringPadEnd),
        ("replace", NativeMethodKind::StringReplace),
        ("replace_all", NativeMethodKind::StringReplaceAll),
        ("starts_with", NativeMethodKind::StringStartsWith),
        ("ends_with", NativeMethodKind::StringEndsWith),
        ("contains", NativeMethodKind::StringContains),
        ("index_of", NativeMethodKind::StringIndexOf),
        ("repeat", NativeMethodKind::StringRepeat),
        ("to_upper", NativeMethodKind::StringToUpper),
        ("to_lower", NativeMethodKind::StringToLower),
        ("lines", NativeMethodKind::StringLines),
        ("parse_number", NativeMethodKind::StringParseNumber),
    ] {
        set_inherent(&names.string, mname, kind);
    }
    set_inherent(&names.vector, "join", NativeMethodKind::VectorJoin);
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
    // M37 (1.17): Bytes and String.to_bytes (runtime/src/vm/bytes.rs).
    set_inherent(&names.string, "to_bytes", NativeMethodKind::StringToBytes);
    {
        use super::bytes::BytesMethod as B;
        for (mname, m) in [
            ("len", B::Len),
            ("push", B::Push),
            ("pop", B::Pop),
            ("extend", B::Extend),
            ("copy", B::Copy),
            ("to_vector", B::ToVector),
            ("to_text", B::ToText),
            ("to_text_lossy", B::ToTextLossy),
            ("to_hex", B::ToHex),
            ("to_base64", B::ToBase64),
            ("index_of", B::IndexOf),
        ] {
            set_inherent(&names.bytes, mname, NativeMethodKind::Bytes(m));
        }
    }
    drop(set_inherent);

    let index_trait: Rc<str> = Rc::from("Index");
    let index_assign_trait: Rc<str> = Rc::from("IndexAssign");
    for (tn, kind) in [
        (&names.string, NativeMethodKind::StringIndex),
        (&names.vector, NativeMethodKind::VectorIndex),
        (&names.map_, NativeMethodKind::MapIndex),
        (&names.bytes, NativeMethodKind::BytesIndex),
    ] {
        table
            .entry((tn.clone(), Rc::from("index")))
            .or_insert_with(MethodEntry::empty)
            .traits
            .insert(index_trait.clone(), (Callable::Native(kind), true));
    }
    for (tn, kind) in [
        (&names.vector, NativeMethodKind::VectorIndexAssign),
        (&names.map_, NativeMethodKind::MapIndexAssign),
        (&names.bytes, NativeMethodKind::BytesIndexAssign),
    ] {
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
    rest: u8,
) -> RResult<Vec<Value>> {
    let m = values.len();
    let n = param_count;
    // M41c: the last parameter slot(s) may be rest parameters (`...` collects
    // extra positional arguments into a Vector, `**` the keyword arguments
    // that match no ordinary parameter into a Map).
    let n_ord = n - (rest & 1) as usize - ((rest >> 1) & 1) as usize;
    let has_any_default = params.is_some_and(|ps| ps.iter().any(|(_, d)| *d));
    if kwargs.is_empty() && !has_any_default && rest == 0 {
        if m != n {
            return Err(RuntimeError::with_kind(
                format!("Argument Count is invalid. {label} accepts {n} arguments but {m} was given"),
                ErrorKind::ArgumentError,
            ));
        }
        return Ok(values);
    }
    if m > n_ord && rest & 1 == 0 {
        return Err(RuntimeError::with_kind(
            format!("{label} takes at most {n_ord} positional arguments but {m} were given"),
            ErrorKind::ArgumentError,
        ));
    }
    let mut values = values;
    let extra: Vec<Value> = if m > n_ord { values.split_off(n_ord) } else { Vec::new() };
    let mut bound: Vec<Value> = values;
    let mut bound_flags: Vec<bool> = vec![true; bound.len()];
    bound.resize(n_ord, Value::Absent);
    bound_flags.resize(n_ord, false);
    let name_to_index: HashMap<&str, usize> = match params {
        Some(ps) => ps.iter().take(n_ord).enumerate().map(|(i, (name, _))| (name.as_ref(), i)).collect(),
        None => HashMap::new(),
    };
    let extra_map = MapData::new();
    for (k, w) in kwargs {
        match name_to_index.get(k.as_ref()) {
            None => {
                if rest & 2 != 0 {
                    let mk = map_key(&Value::Str(k.clone())).expect("a String is a Map key");
                    if extra_map.borrow().entries.contains_key(&mk) {
                        return Err(RuntimeError::with_kind(
                            format!("{label} got multiple values for argument '{k}'"),
                            ErrorKind::ArgumentError,
                        ));
                    }
                    extra_map.borrow_mut().index_assign(mk, Value::Str(k.clone()), w);
                    continue;
                }
                return Err(RuntimeError::with_kind(
                    format!("{label} got an unexpected keyword argument '{k}'"),
                    ErrorKind::ArgumentError,
                ));
            }
            Some(&idx) => {
                if bound_flags[idx] {
                    return Err(RuntimeError::with_kind(
                        format!("{label} got multiple values for argument '{k}'"),
                        ErrorKind::ArgumentError,
                    ));
                }
                bound[idx] = w;
                bound_flags[idx] = true;
            }
        }
    }
    for i in 0..n_ord {
        if bound_flags[i] {
            continue;
        }
        let has_default = params.is_some_and(|ps| ps[i].1);
        if !has_default {
            let pname = params.map(|ps| ps[i].0.to_string()).unwrap_or_else(|| format!("#{i}"));
            return Err(RuntimeError::with_kind(
                format!("{label} is missing required argument '{pname}'"),
                ErrorKind::ArgumentError,
            ));
        }
    }
    if rest & 1 != 0 {
        bound.push(Value::Vector(Rc::new(RefCell::new(extra))));
    }
    if rest & 2 != 0 {
        bound.push(Value::Map(extra_map));
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
            if include_self && c.func.rest & 1 != 0 && c.func.param_count == c.func.rest.count_ones() as usize {
                // M41c: the receiver is the method's first positional argument; when
                // the function's first parameter is a `...` rest parameter (a wrapper
                // like `fn(...args, **kw)`), it becomes that Vector's first item.
                let mut all = Vec::with_capacity(values.len() + 1);
                all.push(recv);
                all.extend(values);
                bind_params(c.func.param_count, c.func.params.as_deref(), all, kwargs, &label, c.func.rest)
            } else if include_self {
                let rest_params: Option<Vec<(Rc<str>, bool)>> = c.func.params.as_ref().map(|p| p[1..].to_vec());
                let bound_rest =
                    bind_params(c.func.param_count - 1, rest_params.as_deref(), values, kwargs, &label, c.func.rest)?;
                let mut out = Vec::with_capacity(bound_rest.len() + 1);
                out.push(recv);
                out.extend(bound_rest);
                Ok(out)
            } else {
                bind_params(c.func.param_count, c.func.params.as_deref(), values, kwargs, &label, c.func.rest)
            }
        }
        Callable::Native(kind) => {
            let optional = kind.optional_params();
            if !optional.is_empty() {
                let arity = kind.required_arity();
                let mut params: Vec<(Rc<str>, bool)> =
                    (0..arity).map(|i| (Rc::from(format!("#{i}").as_str()), false)).collect();
                params.extend(optional.iter().map(|(name, _)| (Rc::from(*name), true)));
                let mut bound = bind_params(params.len(), Some(&params), values, kwargs, &label, 0)?;
                for (i, (_name, default)) in optional.iter().enumerate() {
                    if matches!(bound[arity + i], Value::Absent) {
                        bound[arity + i] = default.value();
                    }
                }
                let mut out = Vec::with_capacity(bound.len() + 1);
                out.push(recv);
                out.extend(bound);
                Ok(out)
            } else {
                if !kwargs.is_empty() {
                    return Err(RuntimeError::with_kind(
                        format!("{label} got an unexpected keyword argument '{}'", kwargs[0].0),
                        ErrorKind::ArgumentError,
                    ));
                }
                let arity = kind.required_arity();
                if values.len() != arity {
                    return Err(RuntimeError::with_kind(
                        format!(
                            "Argument Count is invalid. {label} accepts {arity} arguments but {} was given",
                            values.len()
                        ),
                        ErrorKind::ArgumentError,
                    ));
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

fn no_such_field(type_name: &str, field: &str) -> RuntimeError {
    RuntimeError::with_kind(format!("'{type_name}' has no field '{field}'"), ErrorKind::NoSuchField)
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
    /// M25 (docs/MAHC_FORMAT.md #4.6): an error uncaught anywhere in this
    /// task -- its return stack ran out with no handler found. `drive`
    /// decides what this means for the task (main: fatal; detached: fail
    /// its Promise).
    Failed(Value),
}

pub struct Vm<'p> {
    /// M41a: the whole linked program, for `std:reflect` (types, functions,
    /// constants, META).
    pub(super) linked: &'p LinkedProgram,
    code: &'p [LinkedInstr],
    types: &'p [TypeInfo],
    /// M25 (docs/MAHC_FORMAT.md #4.8): `(start, end, handler, slot)`, in
    /// section order (innermost-first) -- see `find_handler`/`unwind`.
    handlers: &'p [HandlerEntry],
    debug: Option<&'p super::link::DebugIndex>,
    pub names: BuiltinTypeNames,
    pub(super) method_table: HashMap<(Rc<str>, Rc<str>), MethodEntry>,
    /// M41b: the decorators `decorate` stored this run, by target
    /// `(kind, a, b)` (`b` is 0 for kinds 0 and 2), each a Vector.
    pub(super) decorators: HashMap<(u64, usize, usize), Value>,
    /// M41c: `hooks.set_type`'s data by struct type name, `hooks.set_param`'s
    /// by (function identity, parameter index).
    pub(super) hook_types: HashMap<Rc<str>, Value>,
    pub(super) hook_params: HashMap<(usize, usize), Value>,
    /// M41c: whether any function-item impl (a `fn#<index>` method-table
    /// type) was registered; until then function dispatch skips the identity
    /// lookup.
    fn_items: bool,
    return_register: Value,
    timers: BinaryHeap<TimerEntry>,
    timer_seq: u64,
    /// M44 (docs/contracts/M44_threads.md #6.3): this VM's line buffer --
    /// only whole lines reach the run's shared stdout, except at flush points.
    line: Vec<u8>,
    stdin: Stdin,
    to_string_name: Rc<str>,
    /// M25: identifies the main task for `drive`'s "is this task the main
    /// one" check (`Rc::ptr_eq`) -- the main task's own failure is always
    /// fatal, never settles a Promise.
    main_task: TaskRef,
    /// M25 (docs/MAHC_FORMAT.md #4.6): every detached task's Promise that
    /// failed, in fail order -- checked at program end for ones nobody
    /// ever `.await`ed.
    failed_promises: Vec<Rc<RefCell<PromiseData>>>,
    /// M32: std:regex's compiled patterns, by canonical source.
    pub regex_cache: HashMap<Rc<str>, regex::Regex>,
    /// M33 (docs/MAHC_FORMAT.md #6.4): blocking operations in flight.
    io: IoHub,
    /// M34: when the run started, for `time.monotonic_ms` (M44: shared by
    /// every VM of the run).
    started: Instant,
    /// M44 (docs/contracts/M44_threads.md #6.1): the run's thread runtime,
    /// this VM's id (the main VM is 0), and `[id, name]` of the thread it runs
    /// on (`(0, "main")` in the main VM).
    pub(super) rt: Arc<ThreadRuntime>,
    pub(super) vm_id: u64,
    pub(super) thread_info: (u64, Rc<str>),
    /// M44/M45: this VM's runtime waits (`retry`, semaphore, channel, join,
    /// job reply) by pending id, in creation order -- the *internal* pending
    /// entries (#6.1).
    waits: BTreeMap<u64, Wait>,
    /// M44: the tasks this VM is stepping, innermost last (an implicit-call
    /// sub-task takes its identity from the top, #5.2).
    stepping: Vec<TaskRef>,
    /// M36 (std:process): the program's arguments, its environment table
    /// (a snapshot taken at start; sorted by name), and whether this is a
    /// `mah test` run (where `exit` fails the test instead).
    pub args: Vec<String>,
    pub env: std::collections::BTreeMap<String, String>,
    test_mode: bool,
}

/// M33: blocking operations run on worker threads, which only report back
/// through `done`; the scheduler settles their Promises on the VM's own
/// thread (Promises aren't `Send`, so they wait in `pending`, by id). The
/// program keeps running while any is pending. Standard input has one
/// reader thread, so lines are handed out in the order `input` asked.
/// Mirrors `code_interpreter.py`'s `_IoHub`.
struct IoHub {
    done_tx: Sender<(u64, Completion)>,
    done_rx: Receiver<(u64, Completion)>,
    pending: HashMap<u64, Rc<RefCell<PromiseData>>>,
    next_id: u64,
    /// M35 (std:fs): open files by id -- the run's handle table (M44: shared
    /// by every VM of the run). Shared with the workers, since an `fs.open`
    /// job adds its file itself.
    pub files: super::fs::FileTable,
    /// M38 (std:socket): open sockets and listeners by id (M44: the run's).
    pub sockets: super::socket::SocketTable,
    /// M44: only the main VM's hub closes the shared tables.
    owns_tables: bool,
}

/// M38: every open socket is closed when the program finishes.
impl Drop for IoHub {
    fn drop(&mut self) {
        if self.owns_tables {
            self.sockets.close_all();
        }
    }
}

/// What a worker reports: a line of standard input (`None` at its end), or
/// a job's result as plain data (M35), turned into Mah values on the VM's
/// own thread. M44 (docs/contracts/M44_threads.md #6.2): the thread
/// runtime's completions.
pub enum Completion {
    Line(Option<String>),
    Value(super::fs::IoValue),
    /// A job's reply (including `cancelled`).
    Job(Result<Payload, Payload>),
    /// Joins, send acknowledgements, channel-closed failures.
    Settle(Result<Payload, Payload>),
    /// A semaphore permit, by semaphore id.
    Sem(u64),
    /// A channel message, by channel id.
    Recv(u64, Payload),
    /// #6.10: every VM of the run is blocked.
    Stuck,
}

/// M44: one runtime wait of this VM (an *internal* pending entry, #6.1).
pub enum Wait {
    /// M45: a `retry` waiting for a change of what its attempt read.
    Retry,
    Sem,
    Recv,
    Send,
    Join,
    Job,
}

type Done = (Sender<(u64, Completion)>, Receiver<(u64, Completion)>);

impl IoHub {
    fn new(rt: &ThreadRuntime, owns_tables: bool, done: Done) -> IoHub {
        let (done_tx, done_rx) = done;
        IoHub {
            done_tx,
            done_rx,
            pending: HashMap::new(),
            next_id: 0,
            files: rt.files.clone(),
            sockets: rt.sockets.clone(),
            owns_tables,
        }
    }

    /// M35: run `job` on a worker thread; its result settles `promise`.
    fn submit(&mut self, promise: Rc<RefCell<PromiseData>>, job: Box<dyn FnOnce() -> super::fs::IoValue + Send>) {
        let id = self.next_id;
        self.next_id += 1;
        self.pending.insert(id, promise);
        let done = self.done_tx.clone();
        std::thread::spawn(move || {
            let _ = done.send((id, Completion::Value(job())));
        });
    }

    fn read_line(&mut self, rt: &ThreadRuntime, promise: Rc<RefCell<PromiseData>>) {
        let id = self.next_id;
        self.next_id += 1;
        self.pending.insert(id, promise);
        rt.request_line(id, self.done_tx.clone());
    }
}

impl<'p> Vm<'p> {
    /// M44 (docs/contracts/M44_threads.md #6.3): append to this VM's line
    /// buffer; whole lines go to the shared stdout in one write.
    pub fn write_stdout(&mut self, s: &str) {
        self.line.extend_from_slice(s.as_bytes());
        if s.as_bytes().contains(&b'\n') {
            let pos = self.line.iter().rposition(|&b| b == b'\n').expect("just appended one");
            let mut out = self.rt.stdout();
            let _ = out.write_all(&self.line[..=pos]);
            drop(out);
            self.line.drain(..=pos);
        }
    }
    /// A flush point: hand over everything left, then flush the shared stdout.
    pub fn flush_stdout(&mut self) {
        let mut out = self.rt.stdout();
        if !self.line.is_empty() {
            let _ = out.write_all(&self.line);
            self.line.clear();
        }
        let _ = out.flush();
    }
    pub fn read_stdin_char(&self) -> Option<char> {
        read_stdin_char(&self.stdin)
    }
    /// M35: run `job` off the VM's thread; its result settles `promise`.
    pub fn submit(&mut self, promise: Rc<RefCell<PromiseData>>, job: Box<dyn FnOnce() -> super::fs::IoValue + Send>) {
        self.io.submit(promise, job);
    }

    /// M36: `process.exit` -- flush stdout and end the program at once
    /// (pending defers, tasks, timers and I/O are abandoned). In a test run
    /// the test fails instead, reported as `main.rs`'s `load_and_test` does.
    ///
    /// M44: the first exit of the run wins (`super::claim_exit`); a later
    /// one, from any thread, never returns.
    pub fn exit_program(&mut self, code: i32) -> ! {
        if !super::claim_exit() {
            loop {
                std::thread::park();
            }
        }
        self.flush_stdout();
        if self.test_mode {
            let outcome = TestOutcome::new("failed", format!("the test called exit({code})"));
            eprint!("{}", outcome.format());
            std::process::exit(0);
        }
        std::process::exit(code);
    }

    /// M35: the open-file table (std:fs).
    pub fn files(&self) -> &super::fs::FileTable {
        &self.io.files
    }

    /// M38: the socket table (std:socket).
    pub fn sockets(&self) -> &super::socket::SocketTable {
        &self.io.sockets
    }

    /// M34: drop `promise`'s pending timer, if it has one.
    pub fn cancel_timer(&mut self, promise: &Rc<RefCell<PromiseData>>) -> bool {
        let before = self.timers.len();
        self.timers.retain(|t| !Rc::ptr_eq(&t.promise, promise));
        self.timers.len() != before
    }

    /// M34: whole milliseconds since this VM started.
    pub fn monotonic_ms(&self) -> u64 {
        self.started.elapsed().as_millis() as u64
    }

    /// M33: settle `promise` with the next line of standard input (or fail
    /// it with EndOfInput), from the scheduler, later.
    pub fn read_line(&mut self, promise: Rc<RefCell<PromiseData>>) {
        self.io.read_line(&self.rt, promise);
    }

    // -- M44: runtime waits, copies, jobs (docs/contracts/M44_threads.md) ----

    /// A fresh pending id (registered later with `add_wait`).
    pub(super) fn alloc_id(&mut self) -> u64 {
        let id = self.io.next_id;
        self.io.next_id += 1;
        id
    }

    /// Register an internal pending entry: it keeps the VM alive until its
    /// completion arrives (or it fails with `stuck`).
    pub(super) fn add_wait(&mut self, id: u64, promise: Rc<RefCell<PromiseData>>, wait: Wait) {
        self.io.pending.insert(id, promise);
        self.waits.insert(id, wait);
    }

    /// Drop an internal pending entry that will never be settled.
    pub(super) fn take_wait(&mut self, id: u64) {
        self.io.pending.remove(&id);
        self.waits.remove(&id);
    }

    /// A copied value, built in this VM's heap.
    pub(super) fn materialize(&self, p: &Payload) -> Value {
        threads::materialize(&self.linked.functions, p)
    }

    /// docs/contracts/M44_threads.md #2.3 steps 2-3: bind the arguments
    /// here, then copy what the job needs (#4.1).
    pub(super) fn make_job(
        &mut self,
        closure: &Rc<ClosureData>,
        values: Vec<Value>,
    ) -> RResult<Result<Snapshot, NotSendable>> {
        let label = match &closure.func.name {
            Some(n) => format!("'{n}'"),
            None => "function".to_string(),
        };
        let bound = bind_params(
            closure.func.param_count,
            closure.func.params.as_deref(),
            values,
            Vec::new(),
            &label,
            closure.func.rest,
        )?;
        let mut methods: Vec<(String, String, Option<String>, Value, bool)> = Vec::new();
        for ((tn, mn), entry) in &self.method_table {
            if let Some((Callable::Closure(c), is_method)) = &entry.inherent {
                methods.push((tn.to_string(), mn.to_string(), None, Value::Function(c.clone()), *is_method));
            }
            for (t, (target, is_method)) in &entry.traits {
                if let Callable::Closure(c) = target {
                    methods.push((
                        tn.to_string(),
                        mn.to_string(),
                        Some(t.to_string()),
                        Value::Function(c.clone()),
                        *is_method,
                    ));
                }
            }
        }
        let decorators: Vec<((u64, usize, usize), Value)> =
            self.decorators.iter().map(|(k, v)| (*k, v.clone())).collect();
        let hook_types: Vec<(String, Value)> = self.hook_types.iter().map(|(k, v)| (k.to_string(), v.clone())).collect();
        let hook_params: Vec<((usize, usize), Value)> = self.hook_params.iter().map(|(k, v)| (*k, v.clone())).collect();
        let callee = Value::Function(closure.clone());
        let mut roots: Vec<(&Value, bool)> = bound.iter().map(|v| (v, true)).collect();
        roots.push((&callee, false));
        roots.extend(methods.iter().map(|m| (&m.3, false)));
        roots.extend(decorators.iter().map(|(_, v)| (v, false)));
        roots.extend(hook_types.iter().map(|(_, v)| (v, false)));
        roots.extend(hook_params.iter().map(|(_, v)| (v, false)));
        let (graph, copies) = match threads::copy_out(&roots) {
            Ok(r) => r,
            Err(e) => return Ok(Err(e)),
        };
        let n = bound.len();
        let mut it = copies.into_iter();
        let args: Vec<_> = it.by_ref().take(n).collect();
        let callee = it.next().expect("the callee was copied");
        let methods = methods.into_iter().map(|(a, b, c, _, e)| (a, b, c, it.next().expect("copied"), e)).collect();
        let decorators = decorators.into_iter().map(|(k, _)| (k, it.next().expect("copied"))).collect();
        let hook_types = hook_types.into_iter().map(|(k, _)| (k, it.next().expect("copied"))).collect();
        let hook_params = hook_params.into_iter().map(|(k, _)| (k, it.next().expect("copied"))).collect();
        Ok(Ok(Snapshot {
            graph,
            callee,
            args,
            methods,
            decorators,
            hook_types,
            hook_params,
            fn_items: self.fn_items,
            env: self.env.clone(),
        }))
    }

    /// Remove `task`'s await edge from the wait-for graph (it resumed).
    fn clear_await(&mut self, task: &TaskRef) {
        let (edge, id) = {
            let t = task.borrow();
            (t.awaits_edge, t.id)
        };
        if edge {
            self.rt.clear_await(id);
            task.borrow_mut().awaits_edge = false;
        }
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

    // -- M25: throw/catch (docs/MAHC_FORMAT.md #4.4/#4.6) -----------------

    /// The first handler entry (in section order -- innermost first) whose
    /// `[start, end)` range contains `pc`, or `None`. A linear scan is fine
    /// (no performance work on handler lookup is in scope for M25).
    fn find_handler(&self, pc: usize) -> Option<(usize, u64)> {
        self.handlers.iter().find(|h| h.start <= pc && pc < h.end).map(|h| (h.handler, h.slot))
    }

    /// docs/MAHC_FORMAT.md #4.4's throw/unwind algorithm. `pc` is the
    /// instruction that's throwing `value` (in `task`'s CURRENT frame --
    /// the caller is responsible for `task`'s current frame already being
    /// the right one when this is first called). Returns `true` (a handler
    /// was found; `task`'s pc/frame are already set to resume there) or
    /// `false` (uncaught in `task`).
    fn unwind(&mut self, task: &TaskRef, value: Value, mut pc: usize) -> RResult<bool> {
        // M28: where it was thrown, then each enclosing call site.
        let backtrace = || {
            let mut bt = vec![pc];
            bt.extend(task.borrow().return_stack.iter().rev().map(|(ret_pc, _)| ret_pc - 1));
            bt
        };
        match &value {
            Value::Struct(s) => {
                let mut b = s.borrow_mut();
                if b.thrown_at.is_none() {
                    b.thrown_at = Some(pc);
                    b.backtrace = Some(backtrace());
                }
            }
            Value::Enum(e) => {
                let mut b = e.borrow_mut();
                if b.thrown_at.is_none() {
                    b.thrown_at = Some(pc);
                    b.backtrace = Some(backtrace());
                }
            }
            _ => {}
        }
        loop {
            if let Some((handler, slot)) = self.find_handler(pc) {
                let frame = task.borrow().current_frame.clone();
                value::write_addr(&frame, (0, slot), value)?;
                task.borrow_mut().pc = handler;
                return Ok(true);
            }
            let popped = task.borrow_mut().return_stack.pop();
            match popped {
                Some((ret_pc, ret_frame)) => {
                    task.borrow_mut().current_frame = ret_frame;
                    pc = ret_pc - 1; // the call instruction that's still unwinding
                }
                None => return Ok(false),
            }
        }
    }

    /// `throw` (docs/MAHC_FORMAT.md #4.4/#6): whether `v`'s type has an
    /// `Error`-trait target for method `message` in the method table.
    fn implements_error(&self, v: &Value) -> bool {
        let tname = type_name_of(v, &self.names);
        // A RuntimeError always implements Error (see the Python VM's
        // `_implements_error`): its impl is in the prelude, which may not
        // be included, yet an implicit defer handler re-throws it.
        if tname.as_ref() == "RuntimeError" {
            return true;
        }
        self.method_table.get(&(tname, Rc::from("message"))).is_some_and(|e| e.traits.contains_key("Error"))
    }

    /// Build a `RuntimeError` enum value (docs/MAHC_FORMAT.md #4.1) from a
    /// classified VM error -- `field message = exactly today's message
    /// text (no location)`.
    fn make_runtime_error_value(&self, kind: ErrorKind, message: &str) -> Value {
        Value::Enum(Rc::new(RefCell::new(EnumData {
            type_name: Rc::from("RuntimeError"),
            variant: Rc::from(kind.variant_name()),
            fields: vec![(Rc::from("message"), Value::Str(Rc::from(message)))],
            thrown_at: None,
            backtrace: None,
        })))
    }

    /// docs/MAHC_FORMAT.md #4.6: the `<m>` in `Uncaught T: <m>` -- call
    /// `value`'s `Error.message` synchronously; fall back to
    /// `Vm::to_str(value)` if that throws, suspends, or returns a non-
    /// String; `None` (caller falls back to the bare `Uncaught T`) if that
    /// fails too.
    fn uncaught_message(&mut self, value: &Value) -> Option<String> {
        let tname = type_name_of(value, &self.names);
        let target = self
            .method_table
            .get(&(tname, Rc::from("message")))
            .and_then(|e| e.traits.get("Error"))
            .map(|(c, _)| c.clone());
        if let Some(Callable::Closure(c)) = target {
            if let Ok(Value::Str(s)) = self.invoke_sync(&c, vec![value.clone()], "message") {
                return Some(s.to_string());
            }
        }
        self.to_str(value).ok()
    }

    /// docs/MAHC_FORMAT.md #4.6's uncaught-error report text: a
    /// `RuntimeError` value's own `message` field, or `Uncaught T: <m>` for
    /// any other error type -- located at `thrown_at`, exactly like an
    /// ordinary runtime error's `at position ...` suffix (none in a
    /// release build, via `locate`).
    fn uncaught_report(&mut self, value: &Value) -> String {
        let (base, thrown_at) = match value {
            Value::Enum(e) if e.borrow().type_name.as_ref() == "RuntimeError" => {
                let b = e.borrow();
                let msg = match b.get("message") {
                    Some(Value::Str(s)) => s.to_string(),
                    _ => String::new(),
                };
                (msg, b.thrown_at)
            }
            _ => {
                let tname = type_name_of(value, &self.names);
                let m = self.uncaught_message(value);
                let base = match m {
                    Some(m) => format!("Uncaught {}: {m}", display_name(&tname)),
                    None => format!("Uncaught {}", display_name(&tname)),
                };
                let thrown_at = match value {
                    Value::Struct(s) => s.borrow().thrown_at,
                    Value::Enum(e) => e.borrow().thrown_at,
                    _ => None,
                };
                (base, thrown_at)
            }
        };
        match thrown_at {
            Some(pc) => {
                let pc = self.report_pc(value, pc);
                self.locate(pc, &base)
            }
            None => base,
        }
    }

    /// M29: where to locate an uncaught error -- its throw site, unless that's
    /// in the prelude or (M30) a standard library module, in which case the
    /// innermost enclosing call outside them (from the backtrace). Mirrors `code_interpreter.py`'s `report_pc`.
    fn report_pc(&self, value: &Value, pc: usize) -> usize {
        let Some(debug) = self.debug else { return pc };
        let backtrace = match value {
            Value::Struct(s) => s.borrow().backtrace.clone(),
            Value::Enum(e) => e.borrow().backtrace.clone(),
            _ => None,
        }
        .unwrap_or_else(|| vec![pc]);
        for candidate in backtrace {
            let idx = debug.pcs.partition_point(|&x| x <= candidate);
            let path: &str = if idx == 0 { "" } else { debug.file_paths[debug.runs[idx - 1].0].as_ref() };
            if idx == 0 || !(path == "<prelude>" || path.starts_with("std:")) {
                return candidate;
            }
        }
        pc
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
        if let (Value::Bytes(x), Value::Bytes(y)) = (a, b) {
            return Ok(super::bytes::concat(x, y)); // M37
        }
        Err(RuntimeError::with_kind(
            format!("Cannot apply '+' to {} and {}", type_name_of(a, &self.names), type_name_of(b, &self.names)),
            ErrorKind::TypeMismatch,
        ))
    }

    fn op_mul(&self, a: &Value, b: &Value) -> RResult<Value> {
        match (a, b) {
            (Value::Number(x), Value::Number(y)) => x.mul(y).map(Value::Number).map_err(|e| RuntimeError::new(e.message())),
            (Value::Str(s), Value::Number(n)) if n.is_integer() => Ok(repeat_str(s, n)),
            (Value::Number(n), Value::Str(s)) if n.is_integer() => Ok(repeat_str(s, n)),
            _ => Err(RuntimeError::with_kind(
                format!("Cannot apply '*' to {} and {}", type_name_of(a, &self.names), type_name_of(b, &self.names)),
                ErrorKind::TypeMismatch,
            )),
        }
    }

    fn numeric_binop(&self, op: &BinOp, a: &Value, b: &Value) -> RResult<Value> {
        let (Value::Number(x), Value::Number(y)) = (a, b) else {
            return Err(RuntimeError::with_kind(
                format!(
                    "Cannot apply '{}' to {} and {}",
                    binop_symbol(op),
                    type_name_of(a, &self.names),
                    type_name_of(b, &self.names)
                ),
                ErrorKind::TypeMismatch,
            ));
        };
        match op {
            BinOp::Sub => x.sub(y).map(Value::Number).map_err(|e| RuntimeError::new(e.message())),
            BinOp::Div => {
                if y.is_zero() {
                    return Err(RuntimeError::with_kind("Division by zero", ErrorKind::DivisionByZero));
                }
                x.div(y).map(Value::Number).map_err(|e| RuntimeError::new(e.message()))
            }
            BinOp::Idiv => {
                if y.is_zero() {
                    return Err(RuntimeError::with_kind("Division by zero", ErrorKind::DivisionByZero));
                }
                x.idiv(y).map(Value::Number).map_err(|e| RuntimeError::new(e.message()))
            }
            BinOp::Mod => {
                if y.is_zero() {
                    return Err(RuntimeError::with_kind("Division by zero", ErrorKind::DivisionByZero));
                }
                x.rem(y).map(Value::Number).map_err(|e| RuntimeError::new(e.message()))
            }
            BinOp::Pow => {
                // Deliberate deviation matching the Python VM: 0 ** (negative) is Division by zero.
                if x.is_zero() && y.is_negative() {
                    return Err(RuntimeError::with_kind("Division by zero", ErrorKind::DivisionByZero));
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
            _ => Err(RuntimeError::with_kind(
                format!(
                    "Cannot compare {} and {} with '{}'",
                    type_name_of(a, &self.names),
                    type_name_of(b, &self.names),
                    binop_symbol(op)
                ),
                ErrorKind::TypeMismatch,
            )),
        }
    }

    fn getfield(&self, obj: &Value, field: &str) -> RResult<Value> {
        match obj {
            Value::None => Err(no_such_field("Option", field)),
            Value::Struct(s) => {
                let b = s.borrow();
                b.get(field).cloned().ok_or_else(|| no_such_field(&b.type_name, field))
            }
            Value::Enum(e) => {
                let b = e.borrow();
                b.get(field).cloned().ok_or_else(|| no_such_field(&b.type_name, field))
            }
            Value::Promise(p) => {
                let b = p.borrow();
                match field {
                    "value" => b.settled.clone().ok_or_else(|| no_such_field("Promise", field)),
                    "error" => b.failed.clone().ok_or_else(|| no_such_field("Promise", field)),
                    _ => Err(no_such_field("Promise", field)),
                }
            }
            other => Err(RuntimeError::with_kind(
                format!("Tried to access field '{field}' on a non-struct value ({})", type_name_of(other, &self.names)),
                ErrorKind::TypeMismatch,
            )),
        }
    }

    fn setfield(&self, obj: &Value, field: &str, value: Value) -> RResult<()> {
        match obj {
            Value::None => Err(no_such_field("Option", field)),
            Value::Struct(s) => {
                let mut b = s.borrow_mut();
                if b.set(field, value) {
                    Ok(())
                } else {
                    Err(no_such_field(&b.type_name, field))
                }
            }
            Value::Enum(e) => {
                let mut b = e.borrow_mut();
                if b.set(field, value) {
                    Ok(())
                } else {
                    Err(no_such_field(&b.type_name, field))
                }
            }
            Value::Promise(p) => {
                let mut b = p.borrow_mut();
                if field == "value" && b.settled.is_some() {
                    b.settled = Some(value);
                    Ok(())
                } else if field == "error" && b.failed.is_some() {
                    b.failed = Some(value);
                    Ok(())
                } else {
                    Err(no_such_field("Promise", field))
                }
            }
            other => Err(RuntimeError::with_kind(
                format!("Tried to access field '{field}' on a non-struct value ({})", type_name_of(other, &self.names)),
                ErrorKind::TypeMismatch,
            )),
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
                // M25: Promise gains `Failed { error }` -- three variants now.
                Value::Promise(p) => {
                    let b = p.borrow();
                    match vname.as_ref() {
                        "Pending" => b.settled.is_none() && b.failed.is_none(),
                        "Settled" => b.settled.is_some(),
                        "Failed" => b.failed.is_some(),
                        _ => false,
                    }
                }
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

    fn pick_target<'a>(
        entry: Option<&'a MethodEntry>,
        name: &Rc<str>,
        trait_: Option<&Rc<str>>,
        tname: &Rc<str>,
    ) -> RResult<Option<&'a (Callable, bool)>> {
        let Some(e) = entry else { return Ok(None) };
        if let Some(t) = trait_ {
            return Ok(e.traits.get(t.as_ref()));
        }
        if let Some(inh) = &e.inherent {
            return Ok(Some(inh));
        }
        if e.traits.len() == 1 {
            return Ok(e.traits.values().next());
        }
        if e.traits.len() > 1 {
            let names_list: Vec<String> = e.traits.keys().map(|k| format!("'{k}'")).collect();
            return Err(RuntimeError::with_kind(
                format!(
                    "Method '{name}' on '{tname}' is ambiguous: provided by traits [{}]; call it as 'Trait.{name}(value, ...)'",
                    names_list.join(", ")
                ),
                ErrorKind::NoSuchMethod,
            ));
        }
        Ok(None)
    }

    pub fn find_method(&self, recv: &Value, name: &Rc<str>, trait_: Option<&Rc<str>>) -> RResult<(Callable, bool)> {
        let tname = type_name_of(recv, &self.names);
        let mut target: Option<&(Callable, bool)> = None;
        if self.fn_items {
            if let Value::Function(c) = recv {
                // M41c: a function's item type (its identity's `fn#<index>`) first
                let key: Rc<str> = Rc::from(format!("fn#{}", c.identity.get()).as_str());
                target = Self::pick_target(self.method_table.get(&(key, name.clone())), name, trait_, &tname)?;
            }
        }
        if target.is_none() {
            target = Self::pick_target(self.method_table.get(&(tname.clone(), name.clone())), name, trait_, &tname)?;
        }
        if trait_.is_none() && target.as_ref().map(|(_, is_method)| !is_method).unwrap_or(true) {
            if let Some(field_val) = struct_or_enum_field(recv, name) {
                return match field_val {
                    Value::Function(c) => Ok((Callable::Closure(c), false)),
                    other => Err(RuntimeError::with_kind(
                        format!("Field '{name}' of '{tname}' is not a function (it holds a {})", type_name_of(&other, &self.names)),
                        ErrorKind::NoSuchMethod,
                    )),
                };
            }
        }
        match target {
            None => {
                if let Some(t) = trait_ {
                    Err(RuntimeError::with_kind(
                        format!("'{tname}' does not implement trait '{t}' (no method '{name}')"),
                        ErrorKind::NoSuchMethod,
                    ))
                } else {
                    Err(RuntimeError::with_kind(format!("'{tname}' has no method '{name}'"), ErrorKind::NoSuchMethod))
                }
            }
            Some((callable, is_method)) => {
                if !is_method {
                    return Err(RuntimeError::with_kind(
                        format!("'{name}' is a static function of '{tname}', not a method; call it as '{tname}.{name}(...)'"),
                        ErrorKind::NoSuchMethod,
                    ));
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
                    other => Err(RuntimeError::with_kind(
                        format!(
                            "Printable.to_string for '{tname}' must return a String, got {}",
                            type_name_of(&other, &self.names)
                        ),
                        ErrorKind::TypeMismatch,
                    )),
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
        let bound = bind_params(closure.func.param_count, closure.func.params.as_deref(), arg_values, Vec::new(), &call_label, closure.func.rest)?;
        let frame = value::new_frame(closure.func.slot_count, Some(closure.defining_frame.clone()));
        {
            let mut fb = frame.borrow_mut();
            for (i, v) in bound.into_iter().enumerate() {
                fb.slots[i] = v;
            }
        }
        let sub_task = value::new_task(closure.func.entry, frame, None);
        if let Some(top) = self.stepping.last() {
            // M44/M45: an implicit runtime call belongs to its caller's task
            // (the same identity and transaction,
            // docs/contracts/M45_atomic.md #6.2).
            let (id, tx) = {
                let t = top.borrow();
                (t.id, t.tx.clone())
            };
            let mut st = sub_task.borrow_mut();
            st.id = id;
            st.tx = tx;
        }
        sub_task.borrow_mut().implicit = Some(Rc::from(label));
        match self.step_task(&sub_task, None)? {
            StepControl::Done(v) => Ok(v),
            StepControl::Suspended => Err(RuntimeError::new(format!(
                "'{label}' cannot suspend (it awaited a pending Promise) when called implicitly by the runtime"
            ))),
            // M25 (docs/MAHC_FORMAT.md #4.6): the sub-task's error is thrown
            // in the CALLING task, at the instruction that invoked it, so it
            // can be caught there -- the enclosing `step_task`'s own error
            // handling (via `RuntimeError::thrown_value`) does exactly that.
            StepControl::Failed(value) => Err(RuntimeError::thrown_value(value)),
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
        // M44: the wait-for graph's await edge
        promise.borrow_mut().producer = Some(Producer::Task(new_task.borrow().id));
        self.drive(new_task, None)?;
        Ok(promise)
    }

    pub(super) fn resolve_promise(&mut self, p: &Rc<RefCell<PromiseData>>, value: Value) -> RResult<()> {
        if p.borrow().settled.is_some() || p.borrow().failed.is_some() {
            return Ok(());
        }
        p.borrow_mut().settled = Some(value.clone());
        let callbacks = std::mem::take(&mut p.borrow_mut().callbacks);
        for cb in callbacks {
            self.clear_await(&cb.task);
            if cb.restart {
                // M45 #6.4: a woken `retry` -- its `atomicbegin` runs again
                cb.task.borrow_mut().pc = cb.resume_pc - 1;
            } else {
                let frame = cb.task.borrow().current_frame.clone();
                value::write_addr(&frame, cb.dest, value.clone())?;
                cb.task.borrow_mut().pc = cb.resume_pc;
            }
            self.drive(cb.task.clone(), None)?;
        }
        Ok(())
    }

    /// M25 (docs/MAHC_FORMAT.md #4.6): settle `p` with a failure -- does
    /// nothing if it's already settled or failed. Each waiting task
    /// resumes by throwing `error` at its own `.await` instruction
    /// (`resume_pc - 1`).
    pub(super) fn fail_promise(&mut self, p: &Rc<RefCell<PromiseData>>, error: Value) -> RResult<()> {
        if p.borrow().settled.is_some() || p.borrow().failed.is_some() {
            return Ok(());
        }
        p.borrow_mut().failed = Some(error.clone());
        let callbacks = std::mem::take(&mut p.borrow_mut().callbacks);
        for cb in callbacks {
            self.clear_await(&cb.task);
            let resume_pc = cb.resume_pc;
            self.drive(cb.task.clone(), Some((error.clone(), resume_pc - 1)))?;
        }
        Ok(())
    }

    /// M25: `pending` resumes a task that suspended awaiting a Promise
    /// which has since failed -- unwind with it before the normal step
    /// loop, exactly like `step_task`'s own error handling.
    fn drive(&mut self, task: TaskRef, pending: Option<(Value, usize)>) -> RResult<()> {
        match self.step_task(&task, pending)? {
            StepControl::Done(v) => {
                let watching = task.borrow().watching_promise.clone();
                if let Some(p) = watching {
                    self.resolve_promise(&p, v)?;
                }
            }
            StepControl::Suspended => {}
            StepControl::Failed(value) => {
                if Rc::ptr_eq(&task, &self.main_task) {
                    // Fatal: stops the whole program immediately, exactly
                    // like an uncaught runtime error always has (pending
                    // timers are abandoned) -- docs/MAHC_FORMAT.md #4.6.
                    let report = self.uncaught_report(&value);
                    let mut err = RuntimeError::new(report);
                    err.located = true;
                    return Err(err);
                }
                let watching = task.borrow().watching_promise.clone();
                if let Some(p) = watching {
                    self.fail_promise(&p, value)?;
                    self.failed_promises.push(p);
                }
            }
        }
        Ok(())
    }

    /// Advance `task` until it finishes (`StepControl::Done`), genuinely
    /// suspends (`StepControl::Suspended`), or fails with an error
    /// uncaught anywhere in `task` (`StepControl::Failed` -- docs/
    /// MAHC_FORMAT.md #4.6). Every runtime error (a classified
    /// `RuntimeError`, an explicit `throw` via `RuntimeError::thrown_value`,
    /// or any other failure) is turned into a `RuntimeError` value (or, for
    /// a thrown one, the Mah value itself) and unwound within `task`
    /// exactly like docs/MAHC_FORMAT.md #4.4 describes; only when `task`'s
    /// own return stack runs out with no handler found does this return
    /// `StepControl::Failed` rather than looping again.
    fn step_task(&mut self, task: &TaskRef, pending: Option<(Value, usize)>) -> RResult<StepControl> {
        self.stepping.push(task.clone());
        let result = self.step_task_inner(task, pending);
        self.stepping.pop();
        if matches!(result, Ok(StepControl::Done(_)) | Ok(StepControl::Failed(_))) {
            // M45 safety net (docs/contracts/M45_atomic.md #6.3): a task
            // never ends inside its transaction
            threads::end_task_tx(&self.rt, task);
        }
        result
    }

    fn step_task_inner(&mut self, task: &TaskRef, pending: Option<(Value, usize)>) -> RResult<StepControl> {
        if let Some((value, pc)) = pending {
            if !self.unwind(task, value.clone(), pc)? {
                return Ok(StepControl::Failed(value));
            }
        }
        loop {
            let current_pc = task.borrow().pc;
            task.borrow_mut().pc = current_pc + 1;
            match self.exec_one(task, current_pc) {
                Ok(Some(outcome)) => return Ok(outcome),
                Ok(None) => continue,
                Err(e) => {
                    if let Some(sig) = e.restart {
                        // M45 (#6.4): a conflict or `retry` -- only the
                        // transaction's owner restarts it; an implicit
                        // call's sub-task passes it up.
                        if !threads::owns_tx(task) {
                            return Err(e);
                        }
                        match threads::restart_tx(self, task, sig)? {
                            Some(()) => return Ok(StepControl::Suspended),
                            None => continue,
                        }
                    }
                    if e.located {
                        // A fatal, already-reported error bubbling out (see
                        // `drive`) -- never caught/unwound, just re-raised
                        // untouched past this step loop too.
                        return Err(e);
                    }
                    let value = match e.thrown {
                        Some(v) => v,
                        None => self.make_runtime_error_value(e.kind, &e.message),
                    };
                    if self.unwind(task, value.clone(), current_pc)? {
                        continue;
                    }
                    return Ok(StepControl::Failed(value));
                }
            }
        }
    }

    fn settle_io(&mut self, (id, completion): (u64, Completion)) -> RResult<()> {
        if let Completion::Stuck = completion {
            // docs/contracts/M44_threads.md #6.10: fail every internal wait
            let ids: Vec<u64> = self.waits.keys().copied().collect();
            self.rt.take_stuck(self.vm_id, &ids);
            let mut promises = Vec::with_capacity(ids.len());
            for id in ids {
                self.waits.remove(&id);
                if let Some(p) = self.io.pending.remove(&id) {
                    promises.push(p);
                }
            }
            for p in promises {
                self.fail_promise(&p, threads::thread_error("stuck", threads::STUCK_MESSAGE))?;
            }
            return Ok(());
        }
        let Some(promise) = self.io.pending.remove(&id) else { return Ok(()) };
        self.waits.remove(&id);
        match completion {
            Completion::Stuck => Ok(()),
            Completion::Job(outcome) => {
                if !promise.borrow().is_pending() {
                    return Ok(()); // settled early by hand
                }
                match outcome {
                    Ok(v) => {
                        let v = self.materialize(&v);
                        self.resolve_promise(&promise, v)
                    }
                    Err(e) => {
                        let e = self.materialize(&e);
                        self.fail_promise(&promise, e)?;
                        self.failed_promises.push(promise);
                        Ok(())
                    }
                }
            }
            Completion::Settle(outcome) => match outcome {
                Ok(v) => {
                    let v = self.materialize(&v);
                    self.resolve_promise(&promise, v)
                }
                Err(e) => {
                    let e = self.materialize(&e);
                    self.fail_promise(&promise, e)
                }
            },
            Completion::Sem(_) => self.resolve_promise(&promise, Value::None),
            Completion::Recv(_, message) => {
                let v = self.materialize(&message);
                self.resolve_promise(&promise, v)
            }
            Completion::Value(v) => self.resolve_promise(&promise, v.into_value()),
            Completion::Line(Some(text)) => self.resolve_promise(&promise, Value::Str(Rc::from(text.as_str()))),
            Completion::Line(None) => {
                let error = Value::Struct(Rc::new(RefCell::new(StructData {
                    type_name: Rc::from("EndOfInput"),
                    fields: Vec::new(),
                    thrown_at: None,
                    backtrace: None,
                })));
                self.fail_promise(&promise, error)
            }
        }
    }

    /// M33: handle the next event -- a finished I/O operation, or else the
    /// next timer, whichever comes first -- waiting for it if need be.
    /// `false` when there's nothing left that could happen.
    fn next_event(&mut self) -> RResult<bool> {
        if let Ok(item) = self.io.done_rx.try_recv() {
            self.settle_io(item)?;
            return Ok(true);
        }
        if self.io.pending.is_empty() {
            return self.drain_next_timer();
        }
        self.flush_stdout();
        let got = match self.timers.peek() {
            Some(t) => {
                let wait = t.wake.saturating_duration_since(Instant::now());
                self.io.done_rx.recv_timeout(wait).ok()
            }
            None => {
                // M44 (docs/contracts/M44_threads.md #6.10): about to block
                // with only runtime waits pending -- maybe every VM is.
                if !self.waits.is_empty() {
                    let all_internal = self.io.pending.len() == self.waits.len();
                    if let Some(item) = self.rt.about_to_block(self.vm_id, all_internal, &self.io.done_rx) {
                        self.settle_io(item)?;
                        return Ok(true);
                    }
                }
                self.io.done_rx.recv().ok()
            }
        };
        match got {
            Some(item) => {
                self.settle_io(item)?;
                Ok(true)
            }
            None => self.drain_next_timer(),
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
                    return Err(RuntimeError::with_kind(
                        format!("Cannot negate {}", type_name_of(&v, &self.names)),
                        ErrorKind::TypeMismatch,
                    ));
                };
                let r = n.neg().map_err(|e| RuntimeError::new(e.message()))?;
                wr!(*dest, Value::Number(r));
            }
            LinkedInstr::Not { a, dest } => {
                let v = rd(*a)?;
                wr!(*dest, Value::Bool(!truthy(&v)));
            }
            LinkedInstr::Closure { func, dest } => {
                wr!(*dest, Value::Function(Rc::new(ClosureData {
                    func: func.clone(),
                    defining_frame: frame.clone(),
                    identity: std::cell::Cell::new(func.index),
                })));
            }
            LinkedInstr::Call { callee, args } => {
                let closure_val = rd(*callee)?;
                let Value::Function(c) = &closure_val else {
                    return Err(RuntimeError::with_kind(
                        format!("Tried to call a non-function value ({})", type_name_of(&closure_val, &self.names)),
                        ErrorKind::TypeMismatch,
                    ));
                };
                let label = match &c.func.name {
                    Some(n) => format!("'{n}'"),
                    None => "function".to_string(),
                };
                let values: Vec<Value> = args.iter().map(|a| rd(*a)).collect::<RResult<_>>()?;
                let bound = bind_params(c.func.param_count, c.func.params.as_deref(), values, Vec::new(), &label, c.func.rest)?;
                let c = c.clone();
                self.enter_closure(task, &c, bound);
            }
            LinkedInstr::CallKw { callee, args, kwnames } => {
                let closure_val = rd(*callee)?;
                let Value::Function(c) = &closure_val else {
                    return Err(RuntimeError::with_kind(
                        format!("Tried to call a non-function value ({})", type_name_of(&closure_val, &self.names)),
                        ErrorKind::TypeMismatch,
                    ));
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
                let bound = bind_params(c.func.param_count, c.func.params.as_deref(), values, kwargs, &label, c.func.rest)?;
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
            LinkedInstr::CallSpread { callee, args, kwargs } => {
                let closure_val = rd(*callee)?;
                let Value::Function(c) = &closure_val else {
                    return Err(RuntimeError::with_kind(
                        format!("Tried to call a non-function value ({})", type_name_of(&closure_val, &self.names)),
                        ErrorKind::TypeMismatch,
                    ));
                };
                let label = match &c.func.name {
                    Some(n) => format!("'{n}'"),
                    None => "function".to_string(),
                };
                let (values, kwargs) = spread_arguments(&rd(*args)?, &rd(*kwargs)?);
                let bound = bind_params(c.func.param_count, c.func.params.as_deref(), values, kwargs, &label, c.func.rest)?;
                let c = c.clone();
                self.enter_closure(task, &c, bound);
            }
            LinkedInstr::CallMethodSpread { recv, name, args, kwargs, trait_ } => {
                let recv_v = rd(*recv)?;
                let (target, include_self) = self.find_method(&recv_v, name, trait_.as_ref())?;
                let (values, kwargs) = spread_arguments(&rd(*args)?, &rd(*kwargs)?);
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
            LinkedInstr::Spread { target, source, keyword } => {
                spread(&rd(*target)?, &rd(*source)?, *keyword, &self.names)?;
            }
            LinkedInstr::Decorate { kind, a, b, values } => {
                // M41b: store the decorators of one target (docs/MAHC_FORMAT.md #6.10)
                let key = (*kind, *a, if matches!(*kind, 1 | 3 | 4) { *b } else { 0 });
                if self.decorators.contains_key(&key) {
                    return Err(RuntimeError::new(format!(
                        "decorate: the target (kind {kind}, {a}, {b}) is decorated twice"
                    )));
                }
                let mut items = Vec::with_capacity(values.len());
                for addr in values {
                    items.push(rd(*addr)?);
                }
                self.decorators.insert(key, Value::Vector(Rc::new(RefCell::new(items))));
            }
            LinkedInstr::ParamHooks { func, param, dest } => {
                // M41c: the WrapParam hooks `hooks.set_param` stored for this parameter
                let data = self.hook_params.get(&(*func, *param)).cloned().unwrap_or(Value::None);
                wr!(*dest, data);
            }
            LinkedInstr::LoadType { value, dest } => wr!(*dest, value.clone()),
            LinkedInstr::Defmethod { closure, type_name, trait_, name, is_method } => {
                let closure_v = rd(*closure)?;
                let Value::Function(c) = &closure_v else {
                    return Err(RuntimeError::new("'defmethod' given a non-function value"));
                };
                if type_name.starts_with("fn#") {
                    self.fn_items = true;
                }
                let entry = self.method_table.entry((type_name.clone(), name.clone())).or_insert_with(MethodEntry::empty);
                match trait_ {
                    None => entry.inherent = Some((Callable::Closure(c.clone()), *is_method)),
                    Some(t) => {
                        entry.traits.insert(t.clone(), (Callable::Closure(c.clone()), *is_method));
                    }
                }
            }
            LinkedInstr::Detach { callee, args, dest } => {
                if task.borrow().tx.is_some() {
                    return threads::in_atomic("detach");
                }
                let closure_val = rd(*callee)?;
                let Value::Function(c) = &closure_val else {
                    return Err(RuntimeError::with_kind(
                        format!("Tried to detach a non-function value ({})", type_name_of(&closure_val, &self.names)),
                        ErrorKind::TypeMismatch,
                    ));
                };
                let label = match &c.func.name {
                    Some(n) => format!("'{n}'"),
                    None => "function".to_string(),
                };
                let values: Vec<Value> = args.iter().map(|a| rd(*a)).collect::<RResult<_>>()?;
                let bound = bind_params(c.func.param_count, c.func.params.as_deref(), values, Vec::new(), &label, c.func.rest)?;
                let c = c.clone();
                let promise = self.spawn_detached(&c, bound)?;
                wr!(*dest, Value::Promise(promise));
            }
            LinkedInstr::DetachKw { callee, args, kwnames, dest } => {
                if task.borrow().tx.is_some() {
                    return threads::in_atomic("detach");
                }
                let closure_val = rd(*callee)?;
                let Value::Function(c) = &closure_val else {
                    return Err(RuntimeError::with_kind(
                        format!("Tried to detach a non-function value ({})", type_name_of(&closure_val, &self.names)),
                        ErrorKind::TypeMismatch,
                    ));
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
                let bound = bind_params(c.func.param_count, c.func.params.as_deref(), values, kwargs, &label, c.func.rest)?;
                let c = c.clone();
                let promise = self.spawn_detached(&c, bound)?;
                wr!(*dest, Value::Promise(promise));
            }
            LinkedInstr::DetachMethod { recv, name, args, trait_, dest } => {
                if task.borrow().tx.is_some() {
                    return threads::in_atomic("detach");
                }
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
                if task.borrow().tx.is_some() {
                    return threads::in_atomic("detach");
                }
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
                if task.borrow().tx.is_some() {
                    return threads::in_atomic(".await");
                }
                let pv = rd(*promise)?;
                let Value::Promise(p) = &pv else {
                    return Err(RuntimeError::with_kind(
                        format!("'.await' used on a non-Promise value ({})", type_name_of(&pv, &self.names)),
                        ErrorKind::TypeMismatch,
                    ));
                };
                // M25 (docs/MAHC_FORMAT.md #4.6): observed regardless of
                // state -- settled, still pending, or already failed.
                p.borrow_mut().observed = true;
                let settled = p.borrow().settled.clone();
                let failed = p.borrow().failed.clone();
                match (settled, failed) {
                    (Some(v), _) => wr!(*dest, v),
                    // Throw at the await instruction itself -- `current_pc`
                    // in the enclosing `step_task` is exactly that.
                    (None, Some(e)) => return Err(RuntimeError::thrown_value(e)),
                    (None, None) => {
                        let producer = p.borrow().producer;
                        if let Some(producer) = producer {
                            if self.rt.tracking.load(std::sync::atomic::Ordering::SeqCst) {
                                // M44 (docs/contracts/M44_threads.md #6.4): a
                                // deadlock throws here; else the edge is recorded.
                                let task_id = task.borrow().id;
                                if !self.rt.await_check(self.vm_id, task_id, producer) {
                                    if let Producer::Join(_) = producer {
                                        // M45: std:thread's `join` awaits its
                                        // Promise at once and drops it, so a
                                        // join that fails here can never be
                                        // awaited again: drop its waiter and
                                        // pending entry, or it would keep this
                                        // VM (and its job) alive forever.
                                        let pid = self.io.pending.iter().find(|(_, q)| Rc::ptr_eq(q, p)).map(|(id, _)| *id);
                                        if let Some(pid) = pid {
                                            self.rt.take_stuck(self.vm_id, &[pid]);
                                            self.take_wait(pid);
                                        }
                                    }
                                    return threads::throw_thread_error("deadlock", threads::AWAIT_DEADLOCK_MESSAGE);
                                }
                                task.borrow_mut().awaits_edge = true;
                            }
                        }
                        let resume_pc = task.borrow().pc;
                        p.borrow_mut().callbacks.push(Continuation { task: task.clone(), dest: *dest, resume_pc, restart: false });
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
                wr!(*dest, Value::Struct(Rc::new(RefCell::new(StructData { type_name, fields, thrown_at: None, backtrace: None }))));
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
                    wr!(
                        *dest,
                        Value::Enum(Rc::new(RefCell::new(EnumData {
                            type_name,
                            variant: variant_name,
                            fields,
                            thrown_at: None,
                            backtrace: None,
                        })))
                    );
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
            LinkedInstr::MatchFail => {
                return Err(RuntimeError::with_kind("No pattern in 'match' matched the value", ErrorKind::MatchFailed))
            }
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
                        RuntimeError::with_kind(
                            format!("Map keys must be a String, Number, or Bool, got {}", type_name_of(&k, &self.names)),
                            ErrorKind::TypeMismatch,
                        )
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
            LinkedInstr::MatchType { value, type_idx, dest } => {
                let v = rd(*value)?;
                let ty = &self.types[*type_idx];
                wr!(*dest, Value::Bool(matches_type(&v, ty)));
            }
            LinkedInstr::DeferDepth { dest } => {
                let depth = task.borrow().defer_stack.len() as i64;
                wr!(*dest, Value::Number(Decimal::from_i64(depth)));
            }
            LinkedInstr::DeferAbove { depth, dest } => {
                let d = rd(*depth)?;
                let cur = task.borrow().defer_stack.len() as i64;
                let above = match &d {
                    Value::Number(n) => &Decimal::from_i64(cur) > n,
                    _ => unreachable!("deferabove's depth operand is always a Number (codegen-emitted)"),
                };
                wr!(*dest, Value::Bool(above));
            }
            LinkedInstr::Throw { value } => {
                let v = rd(*value)?;
                let v = if self.implements_error(&v) {
                    v
                } else {
                    let tname = type_name_of(&v, &self.names);
                    self.make_runtime_error_value(
                        ErrorKind::TypeMismatch,
                        &format!("Cannot throw a value of type '{tname}': it does not implement Error"),
                    )
                };
                return Err(RuntimeError::thrown_value(v));
            }
            LinkedInstr::Native { native, args, dest, atomic } => {
                if let Some(name) = atomic {
                    if task.borrow().tx.is_some() {
                        return threads::in_atomic(name);
                    }
                }
                let vals: Vec<Value> = args.iter().map(|a| rd(*a)).collect::<RResult<_>>()?;
                let r = natives::call_native(self, *native, &vals)?;
                match dest {
                    Some(d) => wr!(*d, r),
                    None => self.return_register = r,
                }
            }
            // M44/M45 (1.21, docs/contracts/M45_atomic.md #6.3): shared
            // variables and transactions.
            LinkedInstr::SharedGet { index, name, working, dest } => {
                let v = threads::get(self, task, *index, name, *working)?;
                wr!(*dest, v);
            }
            LinkedInstr::SharedSet { index, name, src } => {
                let v = rd(*src)?;
                threads::set(self, task, *index, name, v)?;
            }
            LinkedInstr::AtomicBegin => threads::begin(self, task, pc)?,
            LinkedInstr::AtomicEnd => threads::end(self, task)?,
            LinkedInstr::AtomicAbort => threads::abort(self, task)?,
            LinkedInstr::Retry => threads::retry(task)?,
        }
        Ok(None)
    }
}

/// M41a: the argument lists a `callspread`/`callmethodspread` binds -- the
/// items of the positional Vector and the entries of the keyword Map (whose
/// keys `spread` already checked are Strings), in order. The two values are
/// always the VM-built Vector and Map codegen made.
fn spread_arguments(args: &Value, kwargs: &Value) -> (Vec<Value>, Vec<(Rc<str>, Value)>) {
    let values = match args {
        Value::Vector(v) => v.borrow().clone(),
        _ => Vec::new(),
    };
    let mut pairs = Vec::new();
    if let Value::Map(m) = kwargs {
        for (k, v) in m.borrow().iter_ordered() {
            if let Value::Str(name) = k {
                pairs.push((name.clone(), v.clone()));
            }
        }
    }
    (values, pairs)
}

/// M41a `spread` (docs/MAHC_FORMAT.md #6.1): append the items of the Vector
/// `source` to the Vector `target` (the positional arguments of a spread
/// call), or merge the entries of the Map `source` into the Map `target` (its
/// keyword arguments). Anything else is an ArgumentError, as is a non-String
/// key or a keyword that's already in `target`. Mirrors `_spread` in
/// `code_interpreter.py`, messages included.
fn spread(target: &Value, source: &Value, keyword: bool, names: &BuiltinTypeNames) -> RResult<()> {
    if !keyword {
        let Value::Vector(items) = source else {
            return Err(RuntimeError::with_kind(
                format!("'...' needs a Vector, got {}", type_name_of(source, names)),
                ErrorKind::ArgumentError,
            ));
        };
        if let Value::Vector(t) = target {
            let extra = items.borrow().clone();
            t.borrow_mut().extend(extra);
        }
        return Ok(());
    }
    let Value::Map(entries) = source else {
        return Err(RuntimeError::with_kind(
            format!("'**' needs a Map, got {}", type_name_of(source, names)),
            ErrorKind::ArgumentError,
        ));
    };
    let Value::Map(t) = target else { return Ok(()) };
    let pairs: Vec<(Value, Value)> = entries.borrow().iter_ordered().map(|(k, v)| (k.clone(), v.clone())).collect();
    for (key, value) in pairs {
        let Value::Str(text) = &key else {
            return Err(RuntimeError::with_kind(
                format!("'**' needs String keys, got a {} key", type_name_of(&key, names)),
                ErrorKind::ArgumentError,
            ));
        };
        let mk = map_key(&key).expect("a String is a Map key");
        if t.borrow().entries.contains_key(&mk) {
            return Err(RuntimeError::with_kind(
                format!("keyword argument '{text}' given more than once"),
                ErrorKind::ArgumentError,
            ));
        }
        t.borrow_mut().index_assign(mk, key.clone(), value);
    }
    Ok(())
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
/// M28 (docs/MAHC_FORMAT.md #6.10): the result of one test of a `mah test`
/// build -- mirrors `mah/test_outcome.py`'s `TestOutcome`, and `format` is
/// its text form exactly.
pub struct TestOutcome {
    /// "ok", "skipped", or "failed" ("timeout" is the runner's own).
    pub status: &'static str,
    pub message: String,
    /// `(file, line)`, innermost first; file `None` for the test file.
    pub frames: Vec<(Option<String>, u64)>,
    pub leftover: bool,
}

impl TestOutcome {
    fn new(status: &'static str, message: impl Into<String>) -> Self {
        TestOutcome { status, message: message.into(), frames: Vec::new(), leftover: false }
    }

    pub fn format(&self) -> String {
        let mut out = format!("status {}\nleftover {}\n", self.status, if self.leftover { 1 } else { 0 });
        for (file, line) in &self.frames {
            out.push_str(&format!("frame {line} {}\n", file.as_deref().unwrap_or("-")));
        }
        out.push_str("message\n");
        out.push_str(&self.message);
        out
    }
}

impl<'a> Vm<'a> {
    /// A thrown value's backtrace as `(file, line)` pairs (file `None` for
    /// the entry file), empty without DEBUG info.
    fn frames_of(&self, value: &Value) -> Vec<(Option<String>, u64)> {
        let backtrace = match value {
            Value::Struct(s) => s.borrow().backtrace.clone(),
            Value::Enum(e) => e.borrow().backtrace.clone(),
            _ => None,
        };
        let (Some(debug), Some(backtrace)) = (self.debug, backtrace) else { return Vec::new() };
        let mut out = Vec::new();
        for pc in backtrace {
            let idx = debug.pcs.partition_point(|&x| x <= pc);
            if idx == 0 {
                continue;
            }
            let (file_idx, line, _col) = debug.runs[idx - 1];
            if line != 0 {
                let file = if file_idx == 0 { None } else { Some(debug.file_paths[file_idx].to_string()) };
                out.push((file, line));
            }
        }
        out
    }

    /// Run the test whose closure the top-level code stored in main-frame
    /// slot `slot`, to completion (driving timers), and describe how it
    /// ended. Mirrors `code_interpreter.py`'s `run_test`.
    fn run_test(&mut self, main_frame: &FrameRef, slot: usize) -> RResult<TestOutcome> {
        let closure = match value::read_addr(main_frame, (0, slot as u64))? {
            Value::Function(c) => c,
            _ => return Err(RuntimeError::new("TESTS entry doesn't hold a test")),
        };
        let promise = self.spawn_detached(&closure, Vec::new())?;
        loop {
            let pending = {
                let p = promise.borrow();
                p.settled.is_none() && p.failed.is_none()
            };
            if !pending || !self.next_event()? {
                break;
            }
        }
        let leftover = !self.timers.is_empty() || !self.io.pending.is_empty();
        let (settled, failed) = {
            let p = promise.borrow();
            (p.settled.is_some(), p.failed.clone())
        };
        let mut outcome = if settled {
            TestOutcome::new("ok", "")
        } else if let Some(error) = failed {
            promise.borrow_mut().observed = true;
            self.failed_outcome(&error)?
        } else {
            TestOutcome::new("failed", "the test never finished: it waits on a Promise nothing will settle")
        };
        outcome.leftover = leftover;
        Ok(outcome)
    }

    fn failed_outcome(&mut self, error: &Value) -> RResult<TestOutcome> {
        let tname = type_name_of(error, &self.names);
        if let Value::Struct(s) = error {
            if display_name(&tname) == "SkipTest" {
                let reason = s.borrow().get("reason").cloned().unwrap_or(Value::Str(Rc::from("")));
                let text = match reason {
                    Value::Str(r) => r.to_string(),
                    other => self.to_str(&other)?,
                };
                return Ok(TestOutcome::new("skipped", text));
            }
        }
        let message = match error {
            Value::Struct(_) if display_name(&tname) == "AssertionError" => {
                self.uncaught_message(error).unwrap_or_else(|| "assertion failed".to_string())
            }
            Value::Enum(e) if tname.as_ref() == "RuntimeError" => match e.borrow().get("message") {
                Some(Value::Str(m)) => m.to_string(),
                _ => String::new(),
            },
            _ => match self.uncaught_message(error) {
                Some(m) => format!("Uncaught {}: {m}", display_name(&tname)),
                None => format!("Uncaught {}", display_name(&tname)),
            },
        };
        let mut outcome = TestOutcome::new("failed", message);
        outcome.frames = self.frames_of(error);
        Ok(outcome)
    }
}

pub fn execute(program: Arc<Program>, linked: &LinkedProgram, args: &[String]) -> RResult<()> {
    run(program, linked, None, args).map(|_| ())
}

/// M28: run the test in main-frame slot `slot` after the file's own
/// top-level code (docs/MAHC_FORMAT.md #6.10).
pub fn execute_test(program: Arc<Program>, linked: &LinkedProgram, slot: usize) -> RResult<TestOutcome> {
    match run(program, linked, Some(slot), &[]) {
        Ok(outcome) => Ok(outcome.expect("a test run always has an outcome")),
        // The file's own top-level code failed before the test ran.
        Err(e) => Ok(TestOutcome::new("failed", e.message)),
    }
}

/// M44: the one constructor of a VM -- the main VM (`run`) and every job VM
/// (`run_job`).
fn new_vm<'p>(
    linked: &'p LinkedProgram,
    rt: Arc<ThreadRuntime>,
    vm_id: u64,
    thread_info: (u64, Rc<str>),
    io: IoHub,
    env: BTreeMap<String, String>,
    main_task: TaskRef,
) -> Vm<'p> {
    let names = BuiltinTypeNames::new();
    let method_table = build_initial_method_table(&names);
    Vm {
        linked,
        code: &linked.code,
        types: &linked.types,
        handlers: &linked.handlers,
        debug: linked.debug.as_ref(),
        names,
        method_table,
        decorators: HashMap::new(),
        hook_types: HashMap::new(),
        hook_params: HashMap::new(),
        fn_items: false,
        return_register: Value::None,
        timers: BinaryHeap::new(),
        timer_seq: 0,
        line: Vec::new(),
        stdin: io::stdin(),
        to_string_name: Rc::from("to_string"),
        main_task,
        failed_promises: Vec::new(),
        regex_cache: HashMap::new(),
        io,
        started: rt.started,
        args: rt.args.clone(),
        env,
        test_mode: rt.test_mode,
        rt,
        vm_id,
        thread_info,
        waits: BTreeMap::new(),
        stepping: Vec::new(),
    }
}

fn run(
    program: Arc<Program>,
    linked: &LinkedProgram,
    test_slot: Option<usize>,
    args: &[String],
) -> RResult<Option<TestOutcome>> {
    if linked.functions.is_empty() {
        return Err(RuntimeError::new("FUNCTIONS section must declare at least one function"));
    }
    let rt = Arc::new(ThreadRuntime::new(program, args, test_slot.is_some()));
    let done = std::sync::mpsc::channel();
    // The main VM is live for the whole run (docs/contracts/M44_threads.md #6.10).
    rt.register_vm(0, done.0.clone());
    let io = IoHub::new(&rt, true, done);
    let main_fn = &linked.functions[0];
    let main_frame = value::new_frame(main_fn.slot_count, None);
    let main_promise = PromiseData::new_pending();
    let main_task = value::new_task(main_fn.entry, main_frame.clone(), Some(main_promise.clone()));
    let env = super::process::snapshot_environment();
    let mut vm = new_vm(linked, rt, 0, (0, Rc::from("main")), io, env, main_task.clone());
    let result = vm.run_main(main_task, &main_promise, &main_frame, test_slot);
    // M44: the shared stdout is no longer dropped with the VM -- flush it on
    // every way out, before an error reaches stderr.
    vm.flush_stdout();
    result
}

impl<'p> Vm<'p> {
    fn run_main(
        &mut self,
        main_task: TaskRef,
        main_promise: &Rc<RefCell<PromiseData>>,
        main_frame: &FrameRef,
        test_slot: Option<usize>,
    ) -> RResult<Option<TestOutcome>> {
        self.drive(main_task, None)?;

        loop {
            // M33: the program ends once the main task has finished and no
            // timer or I/O operation is pending (docs/MAHC_FORMAT.md #6.4).
            // M44: nor any runtime wait or job reply.
            let settled = main_promise.borrow().settled.is_some();
            if settled && self.timers.is_empty() && self.io.pending.is_empty() {
                break;
            }
            if !self.next_event()? {
                break;
            }
        }

        // M25 (docs/MAHC_FORMAT.md #4.6): once the program would otherwise end
        // normally, report the FIRST never-observed failed detached-task
        // Promise (in fail order) as an uncaught error, if there is one.
        let failed = self.failed_promises.clone();
        for p in &failed {
            let observed = p.borrow().observed;
            if !observed {
                let error = p.borrow().failed.clone().expect("recorded in failed_promises, so it must be Failed");
                let report = self.uncaught_report(&error);
                let mut err = RuntimeError::new(report);
                err.located = true;
                return Err(err);
            }
        }

        match test_slot {
            Some(slot) => Ok(Some(self.run_test(main_frame, slot)?)),
            None => Ok(None),
        }
    }

    /// docs/contracts/M44_threads.md #6.5: drive the job's root task, then
    /// its event loop. `Some(outcome)` when the root failed (the job ends at
    /// once); `None` when nothing more can happen.
    fn job_loop(&mut self, root: TaskRef, root_promise: &Rc<RefCell<PromiseData>>) -> RResult<Option<Value>> {
        self.drive(root, None)?;
        loop {
            let (failed, settled) = {
                let p = root_promise.borrow();
                (p.failed.clone(), p.settled.is_some())
            };
            if let Some(error) = failed {
                root_promise.borrow_mut().observed = true;
                return Ok(Some(error));
            }
            if settled && self.timers.is_empty() && self.io.pending.is_empty() {
                return Ok(None);
            }
            if !self.next_event()? {
                return Ok(None);
            }
        }
    }

    /// The job's outcome once its loop ended (#6.5), before the copy back.
    fn job_outcome(&mut self, root_promise: &Rc<RefCell<PromiseData>>) -> Result<Value, Value> {
        let (pending, settled) = {
            let p = root_promise.borrow();
            (p.is_pending(), p.settled.clone())
        };
        if pending {
            return Err(threads::thread_error("stuck", threads::JOB_STUCK_MESSAGE));
        }
        for p in &self.failed_promises {
            let observed = p.borrow().observed;
            if !observed {
                p.borrow_mut().observed = true;
                return Err(p.borrow().failed.clone().expect("a failed Promise"));
            }
        }
        Ok(settled.unwrap_or(Value::None))
    }
}

/// docs/contracts/M44_threads.md #6.5: run one job in a fresh job VM on the
/// calling (worker) thread. Returns its outcome as a copy, and the VM's done
/// queue (for the teardown, #6.6).
#[allow(clippy::too_many_arguments)]
pub(super) fn run_job(
    rt: &Arc<ThreadRuntime>,
    linked: &LinkedProgram,
    thread: (u64, &str),
    job_id: u64,
    snapshot: Snapshot,
    vm_id: u64,
    done: Done,
) -> (Result<Payload, Payload>, Receiver<(u64, Completion)>) {
    let io = IoHub::new(rt, false, done);
    // The VM's "main task" is never run: a root failure fails the root
    // Promise instead of being fatal.
    let dummy = value::new_task(0, value::new_frame(0, None), None);
    let mut vm = new_vm(linked, rt.clone(), vm_id, (thread.0, Rc::from(thread.1)), io, snapshot.env.clone(), dummy);
    let outcome = vm.run_job_body(job_id, snapshot);
    vm.flush_stdout(); // a job's output precedes the settling of its Promise
    let (_tx, empty) = std::sync::mpsc::channel();
    let rx = std::mem::replace(&mut vm.io.done_rx, empty);
    (outcome, rx)
}

impl<'p> Vm<'p> {
    fn run_job_body(&mut self, job_id: u64, snapshot: Snapshot) -> Result<Payload, Payload> {
        let m = threads::copy_in(&self.linked.functions, &snapshot.graph);
        for (tn, mn, trait_, closure, is_method) in &snapshot.methods {
            let Value::Function(c) = m.value(closure) else { continue };
            let entry =
                self.method_table.entry((Rc::from(tn.as_str()), Rc::from(mn.as_str()))).or_insert_with(MethodEntry::empty);
            match trait_ {
                None => entry.inherent = Some((Callable::Closure(c), *is_method)),
                Some(t) => {
                    entry.traits.insert(Rc::from(t.as_str()), (Callable::Closure(c), *is_method));
                }
            }
        }
        self.fn_items = snapshot.fn_items;
        self.decorators = snapshot.decorators.iter().map(|(k, v)| (*k, m.value(v))).collect();
        self.hook_types = snapshot.hook_types.iter().map(|(k, v)| (Rc::from(k.as_str()), m.value(v))).collect();
        self.hook_params = snapshot.hook_params.iter().map(|(k, v)| (*k, m.value(v))).collect();
        let Value::Function(callee) = m.value(&snapshot.callee) else {
            return Err(Payload::internal("a job's callee is not a function"));
        };
        let frame = value::new_frame(callee.func.slot_count, Some(callee.defining_frame.clone()));
        {
            let mut fb = frame.borrow_mut();
            for (i, v) in snapshot.args.iter().enumerate() {
                if i < fb.slots.len() {
                    fb.slots[i] = m.value(v);
                }
            }
        }
        drop(m);
        let root_promise = PromiseData::new_pending();
        let root = value::new_task(callee.func.entry, frame, Some(root_promise.clone()));
        self.rt.set_job_root(job_id, root.borrow().id);
        let outcome = match self.job_loop(root, &root_promise) {
            Err(e) => return Err(Payload::internal(&e.message)),
            Ok(Some(error)) => Err(error),
            Ok(None) => self.job_outcome(&root_promise),
        };
        let not_sendable = || Payload::thread_error("not_sendable", threads::NOT_SENDABLE_MESSAGE);
        match outcome {
            Ok(v) => threads::payload_of(&v).map_err(|_| not_sendable()),
            Err(e) => Err(threads::payload_of(&e).unwrap_or_else(|_| not_sendable())),
        }
    }
}
