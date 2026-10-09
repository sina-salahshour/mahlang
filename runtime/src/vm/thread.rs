//! M44 (docs/contracts/M44_threads.md): the process-wide thread runtime of
//! the Rust VM -- jobs on other threads, shared variables, semaphores,
//! channels, the wait-for graph and the quiescence rule. A port of
//! `mah/thread_runtime.py` and `mah/thread_natives.py`, which define every
//! rule and message. M45 (docs/contracts/M45_atomic.md): the shared
//! variables' store with versions, `atomic { }` transactions (TL2-style
//! validation under the one runtime mutex), `retry` waits and the
//! exclusivity token.
//!
//! Every VM of a run (the main VM and one per running job) has its own heap
//! (`Rc`, never shared). Values cross threads only as copies (`copy_out` into
//! a `SendGraph`, `copy_in` back into a heap), and every cross-thread event
//! is a `Completion` posted to the receiving VM's done queue
//! (`RtState::post`), which that VM handles on its own thread -- Promises
//! never cross threads.

use std::cell::{Cell, RefCell};
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet, VecDeque};
use std::io::{BufWriter, Stdout};
use std::rc::Rc;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::mpsc::{Receiver, Sender};
use std::sync::{Arc, Condvar, Mutex, MutexGuard};
use std::time::{Duration, Instant};

use crate::decimal::{Decimal, SendDecimal};
use crate::decode::Program;

use super::error::{ErrorKind, RResult, RuntimeError, TxSignal};
use super::exec::{Completion, Vm, Wait};
use super::fs::FileTable;
use super::link::LinkedProgram;
use super::socket::SocketTable;
use super::value::{
    self, display_name, type_name_of, ClosureData, Continuation, EnumData, FrameRef, FunctionInfo, MapData, Producer,
    PromiseData, StructData, TaskRef, Tx, TxEntry, TypeData, Value,
};

pub const NOT_SENDABLE_MESSAGE: &str = "a Promise can't be sent to another thread";
pub const FOREIGN_PROMISE_MESSAGE: &str =
    "a Promise from another thread can't be awaited here (it was still pending when it was copied)";
pub const STUCK_MESSAGE: &str = "the wait can never finish: every thread is waiting";
pub const JOB_STUCK_MESSAGE: &str = "the job never finished: it waits on a Promise nothing will settle";
pub const AWAIT_DEADLOCK_MESSAGE: &str = "deadlock: this await would never end (it waits, through threads, for itself)";

// -- M45: transactions (docs/contracts/M45_atomic.md #5, #6) ------------------

/// A transaction that failed this many attempts runs its next one exclusive.
pub const ATOMIC_ATTEMPTS: u32 = 8;
pub const RETRY_NO_READS_MESSAGE: &str = "retry can never wake up: this transaction read no shared variable";
/// The natives a task in a transaction may not call (#5.2) -- exactly the
/// Python VM's `ATOMIC_REFUSED_NATIVES` (mah/thread_runtime.py).
pub const ATOMIC_REFUSED_NATIVES: &[&str] = &[
    "io.print",
    "io.write",
    "io.input",
    "io.read_line",
    "time.sleep_async",
    "time.cancel",
    "promise.resolve",
    "promise.fail",
    "fs.read_text",
    "fs.write_text",
    "fs.append_text",
    "fs.info",
    "fs.list_dir",
    "fs.mkdir",
    "fs.remove",
    "fs.rename",
    "fs.copy",
    "fs.temp_dir",
    "fs.open",
    "fs.read_line",
    "fs.read_all",
    "fs.write",
    "fs.close",
    "fs.read_bytes",
    "fs.write_bytes",
    "fs.append_bytes",
    "fs.file_read_bytes",
    "fs.file_write_bytes",
    "process.exit",
    "process.run",
    "process.env_set",
    "process.env_remove",
    "socket.connect",
    "socket.listen",
    "socket.accept",
    "socket.send",
    "socket.recv",
    "socket.shutdown",
    "socket.close",
    "socket.start_tls",
    "socket.tls_server_config",
    "socket.start_tls_server",
    "thread.spawn",
    "thread.submit",
    "thread.close",
    "thread.join",
    "thread.semaphore_acquire",
    "thread.semaphore_try_acquire",
    "thread.semaphore_release",
    "thread.channel_send",
    "thread.channel_recv",
    "thread.channel_try_recv",
    "thread.channel_close",
];
/// How long an exclusivity wait sleeps before looking again (#6.9).
const EXCL_WAIT: Duration = Duration::from_millis(50);
const CHANNEL_CLOSED_MESSAGE: &str = "the channel is closed";

// ---------------------------------------------------------------------------
// Values that cross threads (docs/contracts/M44_threads.md #4.2)
// ---------------------------------------------------------------------------

/// A value in a `SendGraph`: immutable data inline, heap objects by node.
#[derive(Clone, Debug)]
pub enum SendValue {
    None,
    Absent,
    Bool(bool),
    Number(SendDecimal),
    Str(String),
    Type { kind: u8, index: usize, name: String },
    Ref(usize),
}

#[derive(Clone, Debug)]
pub enum SendNode {
    Vector(Vec<SendValue>),
    Map(Vec<(SendValue, SendValue)>),
    Bytes(Vec<u8>),
    Struct {
        type_name: String,
        fields: Vec<(String, SendValue)>,
        thrown_at: Option<usize>,
        backtrace: Option<Vec<usize>>,
    },
    Enum {
        type_name: String,
        variant: String,
        fields: Vec<(String, SendValue)>,
        thrown_at: Option<usize>,
        backtrace: Option<Vec<usize>>,
    },
    /// Settled (`Ok`) or Failed (`Err`).
    Promise(Result<SendValue, SendValue>),
    Closure { func: usize, identity: usize, frame: usize },
    Frame { slots: Vec<SendValue>, parent: Option<usize> },
}

/// A copied object graph: identity and cycles are kept (one node per
/// source object).
#[derive(Clone, Debug, Default)]
pub struct SendGraph {
    pub nodes: Vec<SendNode>,
}

/// One copied value with its graph.
#[derive(Clone, Debug)]
pub struct Payload {
    pub graph: SendGraph,
    pub root: SendValue,
}

impl Payload {
    pub fn none() -> Payload {
        Payload { graph: SendGraph::default(), root: SendValue::None }
    }

    /// A `ThreadError { kind, message }` as a payload.
    pub fn thread_error(kind: &str, message: &str) -> Payload {
        let mut graph = SendGraph::default();
        let root = push_thread_error(&mut graph.nodes, kind, message);
        Payload { graph, root }
    }

    /// `RuntimeError.Internal { message }` as a payload.
    pub fn internal(message: &str) -> Payload {
        let graph = SendGraph {
            nodes: vec![SendNode::Enum {
                type_name: "RuntimeError".to_string(),
                variant: "Internal".to_string(),
                fields: vec![("message".to_string(), SendValue::Str(message.to_string()))],
                thrown_at: None,
                backtrace: None,
            }],
        };
        Payload { graph, root: SendValue::Ref(0) }
    }
}

fn push_thread_error(nodes: &mut Vec<SendNode>, kind: &str, message: &str) -> SendValue {
    nodes.push(SendNode::Struct {
        type_name: "ThreadError".to_string(),
        fields: vec![
            ("kind".to_string(), SendValue::Str(kind.to_string())),
            ("message".to_string(), SendValue::Str(message.to_string())),
        ],
        thrown_at: None,
        backtrace: None,
    });
    SendValue::Ref(nodes.len() - 1)
}

/// What a job needs from its submitter (docs/contracts/M44_threads.md #4.1),
/// copied when it is queued.
#[derive(Clone, Debug)]
pub struct Snapshot {
    pub graph: SendGraph,
    pub callee: SendValue,
    pub args: Vec<SendValue>,
    /// (type name, method name, trait or inherent, closure, is_method)
    pub methods: Vec<(String, String, Option<String>, SendValue, bool)>,
    pub decorators: Vec<((u64, usize, usize), SendValue)>,
    pub hook_types: Vec<(String, SendValue)>,
    pub hook_params: Vec<((usize, usize), SendValue)>,
    pub fn_items: bool,
    pub env: BTreeMap<String, String>,
}

pub struct Job {
    pub id: u64,
    pub snapshot: Snapshot,
    pub reply_vm: u64,
    pub reply_id: u64,
}

// Everything that crosses threads is plain data.
const _: fn() = || {
    fn f<T: Send + Sync>() {}
    f::<SendGraph>();
    f::<Payload>();
    f::<Job>();
    f::<ThreadRuntime>();
};

/// A strict copy met a Promise.
#[derive(Debug)]
pub struct NotSendable;

fn ptr_of(v: &Value) -> Option<usize> {
    Some(match v {
        Value::Vector(r) => Rc::as_ptr(r) as *const () as usize,
        Value::Map(r) => Rc::as_ptr(r) as *const () as usize,
        Value::Bytes(r) => Rc::as_ptr(r) as *const () as usize,
        Value::Struct(r) => Rc::as_ptr(r) as *const () as usize,
        Value::Enum(r) => Rc::as_ptr(r) as *const () as usize,
        Value::Promise(r) => Rc::as_ptr(r) as *const () as usize,
        Value::Function(r) => Rc::as_ptr(r) as *const () as usize,
        _ => return None,
    })
}

/// A heap object waiting to be filled: a value, or a frame.
#[derive(Clone)]
enum Src {
    V(Value),
    F(FrameRef),
}

struct Copier {
    nodes: Vec<SendNode>,
    memo: HashMap<usize, usize>,
    strict_work: VecDeque<(Src, usize)>,
    env_work: VecDeque<(Src, usize)>,
}

impl Copier {
    fn new() -> Copier {
        Copier { nodes: Vec::new(), memo: HashMap::new(), strict_work: VecDeque::new(), env_work: VecDeque::new() }
    }

    fn shell(&mut self, key: usize, node: SendNode) -> usize {
        self.nodes.push(node);
        let idx = self.nodes.len() - 1;
        self.memo.insert(key, idx);
        idx
    }

    fn conv(&mut self, v: &Value, strict: bool) -> Result<SendValue, NotSendable> {
        Ok(match v {
            Value::None => SendValue::None,
            Value::Absent => SendValue::Absent,
            Value::Bool(b) => SendValue::Bool(*b),
            Value::Number(n) => SendValue::Number(n.to_send()),
            Value::Str(s) => SendValue::Str(s.to_string()),
            Value::Type(t) => SendValue::Type { kind: t.kind, index: t.index, name: t.name.to_string() },
            Value::Promise(p) => {
                if strict {
                    return Err(NotSendable);
                }
                let key = Rc::as_ptr(p) as *const () as usize;
                if let Some(&idx) = self.memo.get(&key) {
                    return Ok(SendValue::Ref(idx));
                }
                let (pending, ok) = {
                    let b = p.borrow();
                    (b.is_pending(), b.settled.is_some())
                };
                let idx = self.shell(key, SendNode::Promise(Ok(SendValue::None)));
                if pending {
                    let err = push_thread_error(&mut self.nodes, "foreign_promise", FOREIGN_PROMISE_MESSAGE);
                    self.nodes[idx] = SendNode::Promise(Err(err));
                } else {
                    if !ok {
                        self.nodes[idx] = SendNode::Promise(Err(SendValue::None));
                    }
                    self.env_work.push_back((Src::V(v.clone()), idx));
                }
                SendValue::Ref(idx)
            }
            _ => {
                let key = ptr_of(v).expect("a heap value");
                if let Some(&idx) = self.memo.get(&key) {
                    return Ok(SendValue::Ref(idx));
                }
                let (node, strict) = match v {
                    Value::Vector(_) => (SendNode::Vector(Vec::new()), strict),
                    Value::Map(_) => (SendNode::Map(Vec::new()), strict),
                    Value::Bytes(b) => {
                        let idx = self.shell(key, SendNode::Bytes(b.borrow().clone()));
                        return Ok(SendValue::Ref(idx));
                    }
                    Value::Struct(_) => (
                        SendNode::Struct { type_name: String::new(), fields: Vec::new(), thrown_at: None, backtrace: None },
                        strict,
                    ),
                    Value::Enum(_) => (
                        SendNode::Enum {
                            type_name: String::new(),
                            variant: String::new(),
                            fields: Vec::new(),
                            thrown_at: None,
                            backtrace: None,
                        },
                        strict,
                    ),
                    Value::Function(_) => (SendNode::Closure { func: 0, identity: 0, frame: 0 }, false),
                    _ => unreachable!("handled above"),
                };
                let idx = self.shell(key, node);
                if strict {
                    self.strict_work.push_back((Src::V(v.clone()), idx));
                } else {
                    self.env_work.push_back((Src::V(v.clone()), idx));
                }
                SendValue::Ref(idx)
            }
        })
    }

    fn conv_frame(&mut self, f: &FrameRef) -> usize {
        let key = Rc::as_ptr(f) as *const () as usize;
        if let Some(&idx) = self.memo.get(&key) {
            return idx;
        }
        let idx = self.shell(key, SendNode::Frame { slots: Vec::new(), parent: None });
        self.env_work.push_back((Src::F(f.clone()), idx));
        idx
    }

    fn fill(&mut self, src: Src, idx: usize, strict: bool) -> Result<(), NotSendable> {
        let node = match src {
            Src::F(f) => {
                let (slots, parent) = {
                    let b = f.borrow();
                    (b.slots.clone(), b.static_parent.clone())
                };
                let mut out = Vec::with_capacity(slots.len());
                for s in &slots {
                    out.push(self.conv(s, false)?);
                }
                let parent = parent.map(|p| self.conv_frame(&p));
                SendNode::Frame { slots: out, parent }
            }
            Src::V(v) => match &v {
                Value::Promise(p) => {
                    let (settled, failed) = {
                        let b = p.borrow();
                        (b.settled.clone(), b.failed.clone())
                    };
                    match (settled, failed) {
                        (Some(s), _) => SendNode::Promise(Ok(self.conv(&s, false)?)),
                        (None, Some(e)) => SendNode::Promise(Err(self.conv(&e, false)?)),
                        (None, None) => return Ok(()),
                    }
                }
                Value::Vector(items) => {
                    let items = items.borrow().clone();
                    let mut out = Vec::with_capacity(items.len());
                    for x in &items {
                        out.push(self.conv(x, strict)?);
                    }
                    SendNode::Vector(out)
                }
                Value::Map(m) => {
                    let pairs: Vec<(Value, Value)> =
                        m.borrow().iter_ordered().map(|(k, v)| (k.clone(), v.clone())).collect();
                    let mut out = Vec::with_capacity(pairs.len());
                    for (k, x) in &pairs {
                        out.push((self.conv(k, strict)?, self.conv(x, strict)?));
                    }
                    SendNode::Map(out)
                }
                Value::Struct(s) => {
                    let (type_name, fields, thrown_at, backtrace) = {
                        let b = s.borrow();
                        (b.type_name.to_string(), b.fields.clone(), b.thrown_at, b.backtrace.clone())
                    };
                    let mut out = Vec::with_capacity(fields.len());
                    for (k, x) in &fields {
                        out.push((k.to_string(), self.conv(x, strict)?));
                    }
                    SendNode::Struct { type_name, fields: out, thrown_at, backtrace }
                }
                Value::Enum(e) => {
                    let (type_name, variant, fields, thrown_at, backtrace) = {
                        let b = e.borrow();
                        (b.type_name.to_string(), b.variant.to_string(), b.fields.clone(), b.thrown_at, b.backtrace.clone())
                    };
                    let mut out = Vec::with_capacity(fields.len());
                    for (k, x) in &fields {
                        out.push((k.to_string(), self.conv(x, strict)?));
                    }
                    SendNode::Enum { type_name, variant, fields: out, thrown_at, backtrace }
                }
                Value::Function(c) => {
                    let frame = self.conv_frame(&c.defining_frame);
                    SendNode::Closure { func: c.func.index, identity: c.identity.get(), frame }
                }
                _ => return Ok(()),
            },
        };
        self.nodes[idx] = node;
        Ok(())
    }

    fn drain_strict(&mut self) -> Result<(), NotSendable> {
        while let Some((src, idx)) = self.strict_work.pop_front() {
            self.fill(src, idx, true)?;
        }
        Ok(())
    }
}

/// docs/contracts/M44_threads.md #4.2: copy every `(value, strict)` root with
/// one identity memo. Strict roots are walked first; a Promise reached in
/// strict mode refuses the whole copy. Iterative (shells first, filled from a
/// work list).
pub fn copy_out(roots: &[(&Value, bool)]) -> Result<(SendGraph, Vec<SendValue>), NotSendable> {
    let mut c = Copier::new();
    let mut out: Vec<SendValue> = vec![SendValue::None; roots.len()];
    for (i, (v, strict)) in roots.iter().enumerate() {
        if *strict {
            out[i] = c.conv(v, true)?;
            c.drain_strict()?;
        }
    }
    for (i, (v, strict)) in roots.iter().enumerate() {
        if !*strict {
            out[i] = c.conv(v, false)?;
        }
    }
    loop {
        if let Some((src, idx)) = c.strict_work.pop_front() {
            c.fill(src, idx, true)?;
        } else if let Some((src, idx)) = c.env_work.pop_front() {
            c.fill(src, idx, false)?;
        } else {
            break;
        }
    }
    Ok((SendGraph { nodes: c.nodes }, out))
}

/// A strict copy of one value.
pub fn payload_of(v: &Value) -> Result<Payload, NotSendable> {
    let (graph, mut roots) = copy_out(&[(v, true)])?;
    Ok(Payload { graph, root: roots.pop().expect("one root") })
}

/// M45 #6.6: whether two strict copies are the same value graph (decides
/// whether an exposed, unassigned working value changed). Closures and
/// frames never count as the same.
pub fn same_copy(a: &Payload, b: &Payload) -> bool {
    let mut left: HashMap<usize, usize> = HashMap::new();
    let mut right: HashMap<usize, usize> = HashMap::new();
    let mut stack: Vec<(&SendValue, &SendValue)> = vec![(&a.root, &b.root)];
    while let Some((x, y)) = stack.pop() {
        let (i, j) = match (x, y) {
            (SendValue::Ref(i), SendValue::Ref(j)) => (*i, *j),
            (SendValue::None, SendValue::None) | (SendValue::Absent, SendValue::Absent) => continue,
            (SendValue::Bool(p), SendValue::Bool(q)) if p == q => continue,
            (SendValue::Number(p), SendValue::Number(q)) if p == q => continue,
            (SendValue::Str(p), SendValue::Str(q)) if p == q => continue,
            (SendValue::Type { kind: k1, index: i1, .. }, SendValue::Type { kind: k2, index: i2, .. })
                if k1 == k2 && i1 == i2 =>
            {
                continue
            }
            _ => return false,
        };
        match (left.get(&i), right.get(&j)) {
            (None, None) => {}
            (Some(&pj), Some(&pi)) if pj == j && pi == i => continue,
            _ => return false,
        }
        left.insert(i, j);
        right.insert(j, i);
        match (&a.graph.nodes[i], &b.graph.nodes[j]) {
            (SendNode::Vector(p), SendNode::Vector(q)) => {
                if p.len() != q.len() {
                    return false;
                }
                stack.extend(p.iter().zip(q.iter()));
            }
            (SendNode::Map(p), SendNode::Map(q)) => {
                if p.len() != q.len() {
                    return false;
                }
                for ((k1, v1), (k2, v2)) in p.iter().zip(q.iter()) {
                    stack.push((k1, k2));
                    stack.push((v1, v2));
                }
            }
            (SendNode::Bytes(p), SendNode::Bytes(q)) => {
                if p != q {
                    return false;
                }
            }
            (
                SendNode::Struct { type_name: t1, fields: f1, .. },
                SendNode::Struct { type_name: t2, fields: f2, .. },
            ) => {
                if t1 != t2 || !same_fields(f1, f2, &mut stack) {
                    return false;
                }
            }
            (
                SendNode::Enum { type_name: t1, variant: v1, fields: f1, .. },
                SendNode::Enum { type_name: t2, variant: v2, fields: f2, .. },
            ) => {
                if t1 != t2 || v1 != v2 || !same_fields(f1, f2, &mut stack) {
                    return false;
                }
            }
            // different kinds, or a Closure/Frame/Promise: conservatively changed
            _ => return false,
        }
    }
    true
}

fn same_fields<'a>(
    f1: &'a [(String, SendValue)],
    f2: &'a [(String, SendValue)],
    stack: &mut Vec<(&'a SendValue, &'a SendValue)>,
) -> bool {
    if f1.len() != f2.len() || f1.iter().zip(f2.iter()).any(|((n1, _), (n2, _))| n1 != n2) {
        return false;
    }
    stack.extend(f1.iter().zip(f2.iter()).map(|((_, a), (_, b))| (a, b)));
    true
}

enum Mat {
    V(Value),
    F(FrameRef),
}

/// The heap objects of a copied graph, built in this VM.
pub struct Materialized {
    nodes: Vec<Mat>,
}

impl Materialized {
    pub fn value(&self, v: &SendValue) -> Value {
        match v {
            SendValue::None => Value::None,
            SendValue::Absent => Value::Absent,
            SendValue::Bool(b) => Value::Bool(*b),
            SendValue::Number(n) => Value::Number(Decimal::from_send(n.clone())),
            SendValue::Str(s) => Value::Str(Rc::from(s.as_str())),
            SendValue::Type { kind, index, name } => {
                Value::Type(Rc::new(TypeData { kind: *kind, index: *index, name: Rc::from(name.as_str()) }))
            }
            SendValue::Ref(i) => match &self.nodes[*i] {
                Mat::V(v) => v.clone(),
                Mat::F(_) => Value::None,
            },
        }
    }

    fn frame(&self, i: usize) -> FrameRef {
        match &self.nodes[i] {
            Mat::F(f) => f.clone(),
            Mat::V(_) => value::new_frame(0, None),
        }
    }
}

/// Build a copied graph in this VM (pass 1: shells; pass 2: closures; pass
/// 3: fill). Every copied Promise is observed (never reported as an
/// unobserved failure) and has no waiters.
pub fn copy_in(functions: &[Rc<FunctionInfo>], graph: &SendGraph) -> Materialized {
    let mut names: HashMap<String, Rc<str>> = HashMap::new();
    let mut intern = |s: &str| -> Rc<str> {
        if let Some(r) = names.get(s) {
            return r.clone();
        }
        let r: Rc<str> = Rc::from(s);
        names.insert(s.to_string(), r.clone());
        r
    };
    let mut nodes: Vec<Mat> = Vec::with_capacity(graph.nodes.len());
    for node in &graph.nodes {
        nodes.push(match node {
            SendNode::Vector(_) => Mat::V(Value::Vector(Rc::new(RefCell::new(Vec::new())))),
            SendNode::Map(_) => Mat::V(Value::Map(MapData::new())),
            SendNode::Bytes(data) => Mat::V(super::bytes::bytes_value(data.clone())),
            SendNode::Struct { type_name, thrown_at, backtrace, .. } => {
                Mat::V(Value::Struct(Rc::new(RefCell::new(StructData {
                    type_name: intern(type_name),
                    fields: Vec::new(),
                    thrown_at: *thrown_at,
                    backtrace: backtrace.clone(),
                }))))
            }
            SendNode::Enum { type_name, variant, thrown_at, backtrace, .. } => {
                Mat::V(Value::Enum(Rc::new(RefCell::new(EnumData {
                    type_name: intern(type_name),
                    variant: intern(variant),
                    fields: Vec::new(),
                    thrown_at: *thrown_at,
                    backtrace: backtrace.clone(),
                }))))
            }
            SendNode::Promise(_) => {
                let p = PromiseData::new_pending();
                p.borrow_mut().observed = true;
                Mat::V(Value::Promise(p))
            }
            SendNode::Frame { .. } => Mat::F(value::new_frame(0, None)),
            SendNode::Closure { .. } => Mat::V(Value::None),
        });
    }
    let mut m = Materialized { nodes };
    for (i, node) in graph.nodes.iter().enumerate() {
        if let SendNode::Closure { func, identity, frame } = node {
            let info = functions.get(*func).cloned().expect("a copied closure names a function of this program");
            m.nodes[i] = Mat::V(Value::Function(Rc::new(ClosureData {
                func: info,
                defining_frame: m.frame(*frame),
                identity: Cell::new(*identity),
            })));
        }
    }
    for (i, node) in graph.nodes.iter().enumerate() {
        match node {
            SendNode::Vector(items) => {
                if let Mat::V(Value::Vector(v)) = &m.nodes[i] {
                    let built: Vec<Value> = items.iter().map(|x| m.value(x)).collect();
                    *v.borrow_mut() = built;
                }
            }
            SendNode::Map(pairs) => {
                if let Mat::V(Value::Map(map)) = &m.nodes[i] {
                    let mut b = map.borrow_mut();
                    for (k, x) in pairs {
                        let key = m.value(k);
                        if let Some(mk) = value::map_key(&key) {
                            b.index_assign(mk, key, m.value(x));
                        }
                    }
                }
            }
            SendNode::Struct { fields, .. } => {
                if let Mat::V(Value::Struct(s)) = &m.nodes[i] {
                    let built: Vec<(Rc<str>, Value)> = fields.iter().map(|(k, x)| (intern(k), m.value(x))).collect();
                    s.borrow_mut().fields = built;
                }
            }
            SendNode::Enum { fields, .. } => {
                if let Mat::V(Value::Enum(e)) = &m.nodes[i] {
                    let built: Vec<(Rc<str>, Value)> = fields.iter().map(|(k, x)| (intern(k), m.value(x))).collect();
                    e.borrow_mut().fields = built;
                }
            }
            SendNode::Promise(state) => {
                if let Mat::V(Value::Promise(p)) = &m.nodes[i] {
                    let mut b = p.borrow_mut();
                    match state {
                        Ok(v) => b.settled = Some(m.value(v)),
                        Err(e) => b.failed = Some(m.value(e)),
                    }
                }
            }
            SendNode::Frame { slots, parent } => {
                if let Mat::F(f) = &m.nodes[i] {
                    let built: Vec<Value> = slots.iter().map(|x| m.value(x)).collect();
                    let parent = parent.map(|p| m.frame(p));
                    let mut b = f.borrow_mut();
                    b.slots = built;
                    b.static_parent = parent;
                }
            }
            SendNode::Bytes(_) | SendNode::Closure { .. } => {}
        }
    }
    m
}

/// A payload as a value of this VM.
pub fn materialize(functions: &[Rc<FunctionInfo>], p: &Payload) -> Value {
    copy_in(functions, &p.graph).value(&p.root)
}

/// docs/contracts/M44_threads.md #6.4 `sharedget`'s *local* copy: like the
/// strict walk, but every Promise is kept as the same object (the copy stays
/// in this VM).
pub fn copy_local(v: &Value) -> Value {
    let mut memo: HashMap<usize, Mat> = HashMap::new();
    let mut work: VecDeque<(Src, usize)> = VecDeque::new();
    fn conv(v: &Value, memo: &mut HashMap<usize, Mat>, work: &mut VecDeque<(Src, usize)>) -> Value {
        match v {
            Value::Vector(_) | Value::Map(_) | Value::Bytes(_) | Value::Struct(_) | Value::Enum(_) | Value::Function(_) => {}
            other => return other.clone(),
        }
        let key = ptr_of(v).expect("a heap value");
        if let Some(Mat::V(hit)) = memo.get(&key) {
            return hit.clone();
        }
        let shell = match v {
            Value::Vector(_) => Value::Vector(Rc::new(RefCell::new(Vec::new()))),
            Value::Map(_) => Value::Map(MapData::new()),
            Value::Bytes(b) => {
                let copy = super::bytes::bytes_value(b.borrow().clone());
                memo.insert(key, Mat::V(copy.clone()));
                return copy;
            }
            Value::Struct(s) => {
                let b = s.borrow();
                Value::Struct(Rc::new(RefCell::new(StructData {
                    type_name: b.type_name.clone(),
                    fields: Vec::new(),
                    thrown_at: b.thrown_at,
                    backtrace: b.backtrace.clone(),
                })))
            }
            Value::Enum(e) => {
                let b = e.borrow();
                Value::Enum(Rc::new(RefCell::new(EnumData {
                    type_name: b.type_name.clone(),
                    variant: b.variant.clone(),
                    fields: Vec::new(),
                    thrown_at: b.thrown_at,
                    backtrace: b.backtrace.clone(),
                })))
            }
            Value::Function(c) => {
                let frame = conv_frame(&c.defining_frame, memo, work);
                let copy = Value::Function(Rc::new(ClosureData {
                    func: c.func.clone(),
                    defining_frame: frame,
                    identity: Cell::new(c.identity.get()),
                }));
                memo.insert(key, Mat::V(copy.clone()));
                return copy;
            }
            _ => unreachable!(),
        };
        memo.insert(key, Mat::V(shell.clone()));
        work.push_back((Src::V(v.clone()), key));
        shell
    }
    fn conv_frame(f: &FrameRef, memo: &mut HashMap<usize, Mat>, work: &mut VecDeque<(Src, usize)>) -> FrameRef {
        let key = Rc::as_ptr(f) as *const () as usize;
        if let Some(Mat::F(hit)) = memo.get(&key) {
            return hit.clone();
        }
        let shell = value::new_frame(0, None);
        memo.insert(key, Mat::F(shell.clone()));
        work.push_back((Src::F(f.clone()), key));
        shell
    }
    let root = conv(v, &mut memo, &mut work);
    while let Some((src, key)) = work.pop_front() {
        match src {
            Src::F(f) => {
                let (slots, parent) = {
                    let b = f.borrow();
                    (b.slots.clone(), b.static_parent.clone())
                };
                let built: Vec<Value> = slots.iter().map(|x| conv(x, &mut memo, &mut work)).collect();
                let parent = parent.map(|p| conv_frame(&p, &mut memo, &mut work));
                if let Some(Mat::F(dst)) = memo.get(&key) {
                    let mut b = dst.borrow_mut();
                    b.slots = built;
                    b.static_parent = parent;
                }
            }
            Src::V(v) => {
                let dst = match memo.get(&key) {
                    Some(Mat::V(d)) => d.clone(),
                    _ => continue,
                };
                match (&v, &dst) {
                    (Value::Vector(s), Value::Vector(d)) => {
                        let items = s.borrow().clone();
                        let built: Vec<Value> = items.iter().map(|x| conv(x, &mut memo, &mut work)).collect();
                        *d.borrow_mut() = built;
                    }
                    (Value::Map(s), Value::Map(d)) => {
                        let pairs: Vec<(Value, Value)> =
                            s.borrow().iter_ordered().map(|(k, v)| (k.clone(), v.clone())).collect();
                        for (k, x) in pairs {
                            let x = conv(&x, &mut memo, &mut work);
                            if let Some(mk) = value::map_key(&k) {
                                d.borrow_mut().index_assign(mk, k, x);
                            }
                        }
                    }
                    (Value::Struct(s), Value::Struct(d)) => {
                        let fields = s.borrow().fields.clone();
                        let built: Vec<(Rc<str>, Value)> =
                            fields.iter().map(|(k, x)| (k.clone(), conv(x, &mut memo, &mut work))).collect();
                        d.borrow_mut().fields = built;
                    }
                    (Value::Enum(s), Value::Enum(d)) => {
                        let fields = s.borrow().fields.clone();
                        let built: Vec<(Rc<str>, Value)> =
                            fields.iter().map(|(k, x)| (k.clone(), conv(x, &mut memo, &mut work))).collect();
                        d.borrow_mut().fields = built;
                    }
                    _ => {}
                }
            }
        }
    }
    root
}

/// A `ThreadError { kind, message }` value.
pub fn thread_error(kind: &str, message: &str) -> Value {
    Value::Struct(Rc::new(RefCell::new(StructData {
        type_name: Rc::from("ThreadError"),
        fields: vec![
            (Rc::from("kind"), Value::Str(Rc::from(kind))),
            (Rc::from("message"), Value::Str(Rc::from(message))),
        ],
        thrown_at: None,
        backtrace: None,
    })))
}

/// Throw a `ThreadError` as a Mah value (so `try`/`catch` sees it).
pub fn throw_thread_error<T>(kind: &str, message: &str) -> RResult<T> {
    Err(RuntimeError::thrown_value(thread_error(kind, message)))
}

fn settled(v: Value) -> Value {
    let p = PromiseData::new_pending();
    p.borrow_mut().settled = Some(v);
    Value::Promise(p)
}

fn failed(e: Value) -> Value {
    let p = PromiseData::new_pending();
    p.borrow_mut().failed = Some(e);
    Value::Promise(p)
}

// ---------------------------------------------------------------------------
// The runtime (docs/contracts/M44_threads.md #6.1)
// ---------------------------------------------------------------------------

struct Semaphore {
    permits: u64,
    available: u64,
    /// (vm id, pending id)
    waiters: VecDeque<(u64, u64)>,
}

struct Channel {
    queue: VecDeque<Payload>,
    capacity: Option<u64>,
    closed: bool,
    receivers: VecDeque<(u64, u64)>,
    senders: VecDeque<(u64, u64, Payload)>,
}

struct Pool {
    name: String,
    workers: u64,
    capacity: Option<u64>,
    jobs: VecDeque<Job>,
    running: u64,
    running_set: BTreeSet<u64>,
    closed: bool,
    alive: u64,
    joiners: Vec<(u64, u64)>,
    cv: Arc<Condvar>,
}

struct VmRecord {
    tx: Sender<(u64, Completion)>,
    blocked: bool,
}

/// The id a completion without a pending entry (`Stuck`) is posted with.
pub const NO_ENTRY: u64 = u64::MAX;

#[derive(Default)]
pub struct RtState {
    /// M45 (#6.1): index -> (stored strict copy, version); the version
    /// clock; retry waits by (vm id, pending id) with the indices they
    /// watch; the exclusivity token.
    shared: HashMap<u64, (Arc<Payload>, u64)>,
    clock: u64,
    watchers: HashMap<u64, BTreeSet<(u64, u64)>>,
    retry_waits: HashMap<(u64, u64), Vec<u64>>,
    /// (tx serial, vm id)
    excl_owner: Option<(u64, u64)>,
    excl_queue: VecDeque<u64>,
    commits_waiting: usize,
    /// task id -> (producer, vm id)
    awaiting: HashMap<u64, (Producer, u64)>,
    job_roots: HashMap<u64, u64>,
    threads: BTreeMap<u64, Pool>,
    next_thread: u64,
    next_job: u64,
    semaphores: BTreeMap<u64, Semaphore>,
    next_semaphore: u64,
    channels: BTreeMap<u64, Channel>,
    next_channel: u64,
    vms: HashMap<u64, VmRecord>,
    blocked_count: usize,
    /// Jobs whose VM is torn down but whose reply isn't posted yet: they
    /// still settle a Promise, so the run isn't stuck (#6.10).
    finishing: HashSet<u64>,
}

pub struct ThreadRuntime {
    state: Mutex<RtState>,
    /// M45: signalled whenever the exclusivity token or `commits_waiting`
    /// changes (#6.9).
    excl_cv: Condvar,
    /// false until the first `thread.spawn` of the run
    pub tracking: AtomicBool,
    /// The one stdout every VM hands whole lines to.
    pub stdout: Mutex<BufWriter<Stdout>>,
    pub files: FileTable,
    pub sockets: SocketTable,
    stdin: Mutex<Option<Sender<(u64, Sender<(u64, Completion)>)>>>,
    pub started: Instant,
    pub program: Arc<Program>,
    pub args: Vec<String>,
    pub test_mode: bool,
    next_vm: AtomicU64,
}

impl ThreadRuntime {
    pub fn new(program: Arc<Program>, args: &[String], test_mode: bool) -> ThreadRuntime {
        let state = RtState { next_thread: 1, next_job: 1, next_semaphore: 1, next_channel: 1, ..RtState::default() };
        ThreadRuntime {
            state: Mutex::new(state),
            excl_cv: Condvar::new(),
            tracking: AtomicBool::new(false),
            stdout: Mutex::new(BufWriter::new(std::io::stdout())),
            files: FileTable::default(),
            sockets: SocketTable::default(),
            stdin: Mutex::new(None),
            started: Instant::now(),
            program,
            args: args.to_vec(),
            test_mode,
            next_vm: AtomicU64::new(1),
        }
    }

    pub fn lock(&self) -> MutexGuard<'_, RtState> {
        self.state.lock().unwrap_or_else(|e| e.into_inner())
    }

    pub fn stdout(&self) -> MutexGuard<'_, BufWriter<Stdout>> {
        self.stdout.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// Register a VM's done queue (the main VM is 0).
    pub fn register_vm(&self, vm_id: u64, tx: Sender<(u64, Completion)>) {
        self.lock().vms.insert(vm_id, VmRecord { tx, blocked: false });
    }

    /// One stdin reader per run: lines are handed out in request order.
    pub fn request_line(&self, id: u64, done: Sender<(u64, Completion)>) {
        let mut guard = self.stdin.lock().unwrap_or_else(|e| e.into_inner());
        let requests = guard.get_or_insert_with(|| {
            let (tx, rx) = std::sync::mpsc::channel::<(u64, Sender<(u64, Completion)>)>();
            std::thread::spawn(move || stdin_worker(rx));
            tx
        });
        let _ = requests.send((id, done));
    }

    /// Mark `vm` as blocked if it is (#6.10): every pending entry internal,
    /// and nothing in its done queue. Returns an item found in the queue.
    pub fn about_to_block(
        &self,
        vm_id: u64,
        all_internal: bool,
        rx: &Receiver<(u64, Completion)>,
    ) -> Option<(u64, Completion)> {
        let mut st = self.lock();
        if !all_internal {
            return None;
        }
        if let Ok(item) = rx.try_recv() {
            return Some(item);
        }
        let mut changed = false;
        if let Some(rec) = st.vms.get_mut(&vm_id) {
            if !rec.blocked {
                rec.blocked = true;
                changed = true;
            }
        }
        if changed {
            st.blocked_count += 1;
            st.check_quiescence();
        }
        None
    }

    // -- the wait-for graph (#6.4) ------------------------------------------

    /// Called (only when tracking) before task `task_id` suspends on a
    /// Promise with `producer`: `Err` = deadlock; else the edge is recorded.
    pub fn await_check(&self, vm_id: u64, task_id: u64, producer: Producer) -> bool {
        let mut st = self.lock();
        let strong = !matches!(producer, Producer::Task(_));
        let edges = st.producer_edges(producer);
        if st.cycle_from(edges, task_id, strong) {
            return false;
        }
        st.awaiting.insert(task_id, (producer, vm_id));
        true
    }

    pub fn clear_await(&self, task_id: u64) {
        self.lock().awaiting.remove(&task_id);
    }

    pub fn set_job_root(&self, job_id: u64, task_id: u64) {
        self.lock().job_roots.insert(job_id, task_id);
    }

    // -- shared variables and transactions (docs/contracts/M45_atomic.md #6) --

    /// The store's `(value, version)` of shared variable `k` (absent:
    /// `(none, 0)`).
    pub fn read_shared(&self, k: u64) -> (Arc<Payload>, u64) {
        let st = self.lock();
        match st.shared.get(&k) {
            Some((p, v)) => (p.clone(), *v),
            None => (Arc::new(Payload::none()), 0),
        }
    }

    /// A plain assignment outside a transaction (`stored` is a strict copy):
    /// a new version, waking `retry` waiters of `k`.
    pub fn write_shared(&self, vm_id: u64, k: u64, stored: Payload) {
        let st = self.lock();
        let mut st = self.wait_no_excl(st, vm_id, None);
        st.clock += 1;
        let clock = st.clock;
        st.shared.insert(k, (Arc::new(stored), clock));
        st.wake_watchers(&[k]);
    }

    /// Start an attempt: take the token first when it is exclusive. Returns
    /// the attempt's snapshot time `rv`.
    pub fn tx_start(&self, vm_id: u64, serial: u64, irrevocable: bool) -> u64 {
        let mut st = self.lock();
        if irrevocable {
            st = self.acquire_excl(st, vm_id, serial);
        }
        st.clock
    }

    /// Validate `reads` and publish `publish` atomically (a read-only
    /// transaction neither waits nor validates); releases the token.
    pub fn tx_commit(&self, vm_id: u64, serial: u64, reads: &[(u64, u64)], publish: Vec<(u64, Payload)>) -> bool {
        let mut st = self.lock();
        if !publish.is_empty() {
            st = self.wait_no_excl(st, vm_id, Some(serial));
            if !st.reads_valid(reads) {
                return false;
            }
            st.clock += 1;
            let clock = st.clock;
            let keys: Vec<u64> = publish.iter().map(|(k, _)| *k).collect();
            for (k, p) in publish {
                st.shared.insert(k, (Arc::new(p), clock));
            }
            st.wake_watchers(&keys);
        }
        self.release_excl(&mut st, serial);
        true
    }

    /// The attempt of transaction `serial` ends without a commit.
    pub fn end_attempt(&self, serial: u64) {
        let mut st = self.lock();
        self.release_excl(&mut st, serial);
    }

    /// Register pending entry `pid` of VM `vm_id` as a retry wait on
    /// `reads`; false = one of them changed already (run again now).
    pub fn register_retry(&self, vm_id: u64, pid: u64, reads: &[(u64, u64)]) -> bool {
        let mut st = self.lock();
        if !st.reads_valid(reads) {
            return false;
        }
        let key = (vm_id, pid);
        let keys: Vec<u64> = reads.iter().map(|(k, _)| *k).collect();
        for &k in &keys {
            st.watchers.entry(k).or_default().insert(key);
        }
        st.retry_waits.insert(key, keys);
        true
    }

    fn excl_wait<'a>(&'a self, st: MutexGuard<'a, RtState>) -> MutexGuard<'a, RtState> {
        match self.excl_cv.wait_timeout(st, EXCL_WAIT) {
            Ok((g, _)) => g,
            Err(e) => e.into_inner().0,
        }
    }

    /// Become the one exclusive transaction (#6.9): FIFO among the waiting
    /// ones, after the commits already waiting have gone through.
    fn acquire_excl<'a>(&'a self, mut st: MutexGuard<'a, RtState>, vm_id: u64, serial: u64) -> MutexGuard<'a, RtState> {
        st.excl_queue.push_back(serial);
        while !(st.excl_owner.is_none() && st.excl_queue.front() == Some(&serial) && st.commits_waiting == 0) {
            st = self.excl_wait(st);
        }
        st.excl_queue.pop_front();
        st.excl_owner = Some((serial, vm_id));
        st
    }

    /// Wait while another transaction is exclusive (a commit of `serial`, or
    /// a plain write when `serial` is `None`).
    fn wait_no_excl<'a>(
        &'a self,
        mut st: MutexGuard<'a, RtState>,
        _vm_id: u64,
        serial: Option<u64>,
    ) -> MutexGuard<'a, RtState> {
        let blocked = |st: &RtState| matches!(st.excl_owner, Some((owner, _)) if Some(owner) != serial);
        if !blocked(&st) {
            return st;
        }
        st.commits_waiting += 1;
        while blocked(&st) {
            st = self.excl_wait(st);
        }
        st.commits_waiting -= 1;
        if st.commits_waiting == 0 {
            self.excl_cv.notify_all();
        }
        st
    }

    fn release_excl(&self, st: &mut RtState, serial: u64) {
        if matches!(st.excl_owner, Some((owner, _)) if owner == serial) {
            st.excl_owner = None;
            self.excl_cv.notify_all();
        }
    }

    /// Program end / teardown of a job VM (#6.6).
    pub fn forget_vm(&self, vm_id: u64, job_id: Option<u64>, rx: Option<&Receiver<(u64, Completion)>>) {
        {
            let mut st = self.lock();
            // M45 (#6.8): this VM's retry waits and its exclusivity token
            let mine: Vec<(u64, u64)> = st.retry_waits.keys().filter(|(vm, _)| *vm == vm_id).copied().collect();
            for key in mine {
                st.drop_retry_wait(key);
            }
            if let Some((serial, vm)) = st.excl_owner {
                if vm == vm_id {
                    self.release_excl(&mut st, serial);
                }
            }
            let RtState { semaphores, channels, threads, awaiting, .. } = &mut *st;
            for s in semaphores.values_mut() {
                s.waiters.retain(|&(vm, _)| vm != vm_id);
            }
            for c in channels.values_mut() {
                c.receivers.retain(|&(vm, _)| vm != vm_id);
                c.senders.retain(|(vm, _, _)| *vm != vm_id);
            }
            for p in threads.values_mut() {
                p.joiners.retain(|&(vm, _)| vm != vm_id);
            }
            awaiting.retain(|_, (_, vm)| *vm != vm_id);
            if let Some(j) = job_id {
                st.job_roots.remove(&j);
                st.finishing.insert(j);
            }
            if let Some(rec) = st.vms.remove(&vm_id) {
                if rec.blocked {
                    st.blocked_count -= 1;
                }
            }
        }
        // Completions that arrived for this VM: give permits and messages back.
        let Some(rx) = rx else { return };
        while let Ok((_, completion)) = rx.try_recv() {
            match completion {
                Completion::Sem(sid) => {
                    let mut st = self.lock();
                    let _ = st.semaphore_release(sid, false);
                }
                Completion::Recv(cid, message) => {
                    let mut st = self.lock();
                    let target = match st.channels.get_mut(&cid) {
                        Some(ch) => match ch.receivers.pop_front() {
                            Some(r) => Some((r, message)),
                            None => {
                                ch.queue.push_front(message);
                                None
                            }
                        },
                        None => None,
                    };
                    if let Some(((vm, pid), message)) = target {
                        st.post(vm, pid, Completion::Recv(cid, message));
                    }
                }
                _ => {}
            }
        }
    }

    /// The main VM's `stuck` completion: remove every internal wait of the
    /// VM (runtime waiter and pending entry, by pending id).
    pub fn take_stuck(&self, vm_id: u64, ids: &[u64]) {
        let mut st = self.lock();
        for &pid in ids {
            st.remove_waiter(vm_id, pid);
        }
    }
}

fn stdin_worker(requests: Receiver<(u64, Sender<(u64, Completion)>)>) {
    use std::io::BufRead;
    while let Ok((id, done)) = requests.recv() {
        let mut line = String::new();
        let got = match std::io::stdin().lock().read_line(&mut line) {
            Ok(0) | Err(_) => None,
            Ok(_) => {
                if line.ends_with('\n') {
                    line.pop();
                    if line.ends_with('\r') {
                        line.pop();
                    }
                }
                Some(line)
            }
        };
        let _ = done.send((id, Completion::Line(got)));
    }
}

impl RtState {
    /// Wake `vm` with `completion` (the state lock held): clears its blocked
    /// flag first. A VM that's gone drops it.
    fn post(&mut self, vm: u64, pid: u64, completion: Completion) {
        if let Some(rec) = self.vms.get_mut(&vm) {
            if rec.blocked {
                rec.blocked = false;
                self.blocked_count -= 1;
            }
            let _ = rec.tx.send((pid, completion));
        }
    }

    /// The (task id, strong) nodes a producer leads to.
    fn producer_edges(&self, producer: Producer) -> Vec<(u64, bool)> {
        match producer {
            Producer::Task(t) => vec![(t, false)],
            Producer::Job(j) => self.job_roots.get(&j).map(|&r| vec![(r, true)]).unwrap_or_default(),
            Producer::Join(id) => match self.threads.get(&id) {
                None => Vec::new(),
                Some(p) => p.running_set.iter().filter_map(|j| self.job_roots.get(j)).map(|&r| (r, true)).collect(),
            },
        }
    }

    fn out_edges(&self, node: u64) -> Vec<(u64, bool)> {
        let mut edges = Vec::new();
        if let Some((producer, _)) = self.awaiting.get(&node) {
            edges.extend(self.producer_edges(*producer));
        }
        edges
    }

    /// Whether following `edges` reaches task `target` along a path that,
    /// with the new edge (strong or not), contains a strong edge.
    fn cycle_from(&self, edges: Vec<(u64, bool)>, target: u64, strong: bool) -> bool {
        let mut stack: Vec<(u64, bool)> = edges.into_iter().map(|(n, s)| (n, strong || s)).collect();
        let mut seen: HashSet<(u64, bool)> = HashSet::new();
        while let Some((node, has_strong)) = stack.pop() {
            if node == target {
                if has_strong {
                    return true;
                }
                continue;
            }
            if !seen.insert((node, has_strong)) {
                continue;
            }
            for (next, s) in self.out_edges(node) {
                stack.push((next, has_strong || s));
            }
        }
        false
    }

    fn reads_valid(&self, reads: &[(u64, u64)]) -> bool {
        reads.iter().all(|(k, version)| self.shared.get(k).map_or(0, |(_, v)| *v) == *version)
    }

    /// M45 #6.5: wake every retry wait watching one of `keys`.
    fn wake_watchers(&mut self, keys: &[u64]) {
        for k in keys {
            let Some(waiting) = self.watchers.remove(k) else { continue };
            for key in waiting {
                let Some(indices) = self.retry_waits.remove(&key) else { continue };
                for other in indices {
                    if other != *k {
                        if let Some(w) = self.watchers.get_mut(&other) {
                            w.remove(&key);
                        }
                    }
                }
                self.post(key.0, key.1, Completion::Settle(Ok(Payload::none())));
            }
        }
    }

    fn drop_retry_wait(&mut self, key: (u64, u64)) {
        let Some(indices) = self.retry_waits.remove(&key) else { return };
        for k in indices {
            if let Some(w) = self.watchers.get_mut(&k) {
                w.remove(&key);
                if w.is_empty() {
                    self.watchers.remove(&k);
                }
            }
        }
    }

    fn check_quiescence(&mut self) {
        let live = self.vms.len();
        // A worker that has a job to start, or (closed pool) is about to exit
        // and settle its joiners, can still make progress -- and so can one
        // whose job VM is gone but whose reply isn't posted yet.
        let starting = !self.finishing.is_empty()
            || self.threads.values().any(|p| (!p.jobs.is_empty() || p.closed) && p.running < p.alive);
        if live > 0 && self.blocked_count == live && !starting && self.vms.contains_key(&0) {
            self.post(0, NO_ENTRY, Completion::Stuck);
        }
    }

    fn remove_waiter(&mut self, vm_id: u64, pid: u64) {
        self.drop_retry_wait((vm_id, pid));
        let RtState { semaphores, channels, threads, .. } = self;
        for s in semaphores.values_mut() {
            s.waiters.retain(|&(vm, p)| !(vm == vm_id && p == pid));
        }
        for c in channels.values_mut() {
            c.receivers.retain(|&(vm, p)| !(vm == vm_id && p == pid));
            c.senders.retain(|(vm, p, _)| !(*vm == vm_id && *p == pid));
        }
        for t in threads.values_mut() {
            t.joiners.retain(|&(vm, p)| !(vm == vm_id && p == pid));
        }
    }

    fn semaphore_release(&mut self, sid: u64, check: bool) -> Result<(), u64> {
        let Some(sem) = self.semaphores.get_mut(&sid) else { return Ok(()) };
        if let Some((vm, pid)) = sem.waiters.pop_front() {
            self.post(vm, pid, Completion::Sem(sid));
        } else if sem.available == sem.permits {
            if check {
                return Err(sem.permits);
            }
        } else {
            sem.available += 1;
        }
        Ok(())
    }

    /// recv's non-waiting branches.
    fn take_message(&mut self, cid: u64) -> Option<Payload> {
        let ch = self.channels.get_mut(&cid)?;
        if let Some(message) = ch.queue.pop_front() {
            if let Some((vm, pid, m)) = ch.senders.pop_front() {
                ch.queue.push_back(m);
                self.post(vm, pid, Completion::Settle(Ok(Payload::none())));
            }
            return Some(message);
        }
        if let Some((vm, pid, m)) = ch.senders.pop_front() {
            self.post(vm, pid, Completion::Settle(Ok(Payload::none())));
            return Some(m);
        }
        None
    }

    fn close_pool(&mut self, id: u64, cancel: bool) {
        let Some(pool) = self.threads.get_mut(&id) else { return };
        pool.closed = true;
        let mut cancelled = Vec::new();
        if cancel {
            let message = format!("thread '{}' was closed before this job started", pool.name);
            while let Some(job) = pool.jobs.pop_front() {
                cancelled.push((job.reply_vm, job.reply_id, message.clone()));
            }
        }
        pool.cv.notify_all();
        for (vm, pid, message) in cancelled {
            self.post(vm, pid, Completion::Job(Err(Payload::thread_error("cancelled", &message))));
        }
        self.check_quiescence();
    }
}

// ---------------------------------------------------------------------------
// Workers (#6.5)
// ---------------------------------------------------------------------------

fn panic_text(panic: &(dyn std::any::Any + Send)) -> String {
    if let Some(s) = panic.downcast_ref::<&str>() {
        s.to_string()
    } else if let Some(s) = panic.downcast_ref::<String>() {
        s.clone()
    } else {
        "a job panicked".to_string()
    }
}

fn worker(rt: Arc<ThreadRuntime>, pool_id: u64, cv: Arc<Condvar>) {
    let linked: Option<LinkedProgram> = super::link::link(&rt.program).ok();
    loop {
        let mut st = rt.lock();
        loop {
            let p = st.threads.get(&pool_id).expect("a worker's pool exists");
            if !p.jobs.is_empty() || p.closed {
                break;
            }
            st = cv.wait(st).unwrap_or_else(|e| e.into_inner());
        }
        let pool = st.threads.get_mut(&pool_id).expect("a worker's pool exists");
        let Some(job) = pool.jobs.pop_front() else {
            // closed and drained
            pool.alive -= 1;
            if pool.alive == 0 {
                let joiners = std::mem::take(&mut pool.joiners);
                for (vm, pid) in joiners {
                    st.post(vm, pid, Completion::Settle(Ok(Payload::none())));
                }
            }
            st.check_quiescence();
            return;
        };
        pool.running += 1;
        pool.running_set.insert(job.id);
        let name = pool.name.clone();
        let vm_id = rt.next_vm.fetch_add(1, Ordering::SeqCst);
        let (tx, rx) = std::sync::mpsc::channel();
        st.vms.insert(vm_id, VmRecord { tx: tx.clone(), blocked: false });
        drop(st);
        let Job { id, snapshot, reply_vm, reply_id } = job;
        let outcome = match &linked {
            Some(l) => {
                let run = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    super::exec::run_job(&rt, l, (pool_id, &name), id, snapshot, vm_id, (tx, rx))
                }));
                match run {
                    Ok((outcome, rx)) => {
                        rt.forget_vm(vm_id, Some(id), Some(&rx));
                        outcome
                    }
                    Err(panic) => {
                        rt.forget_vm(vm_id, Some(id), None);
                        Err(Payload::internal(&panic_text(&*panic)))
                    }
                }
            }
            None => {
                rt.forget_vm(vm_id, Some(id), Some(&rx));
                Err(Payload::internal("the program could not be linked on a worker thread"))
            }
        };
        let mut st = rt.lock();
        if let Some(pool) = st.threads.get_mut(&pool_id) {
            pool.running -= 1;
            pool.running_set.remove(&id);
        }
        st.finishing.remove(&id);
        st.post(reply_vm, reply_id, Completion::Job(outcome));
        st.check_quiescence();
    }
}

// ---------------------------------------------------------------------------
// The natives (docs/contracts/M44_threads.md #7.2)
// ---------------------------------------------------------------------------

/// A handle id as a key, or `None` when it can't be one.
fn ident(v: &Value) -> Option<u64> {
    match v {
        Value::Number(n) if n.is_integer() => n.to_i64().and_then(|i| u64::try_from(i).ok()),
        _ => None,
    }
}

fn shown_number(vm: &mut Vm, v: &Value) -> String {
    match v {
        Value::Number(n) => n.format(),
        other => vm.to_str(other).unwrap_or_default(),
    }
}

fn no_such(vm: &mut Vm, what: &str, v: &Value) -> RuntimeError {
    RuntimeError::with_kind(format!("thread: no such {what} {}", shown_number(vm, v)), ErrorKind::ArgumentError)
}

fn whole(v: &Value) -> u64 {
    match v {
        Value::Number(n) => n.to_i64().and_then(|i| u64::try_from(i).ok()).unwrap_or(0),
        _ => 0,
    }
}

fn shown_type(vm: &Vm, v: &Value) -> String {
    match v {
        Value::None => "None".to_string(),
        other => display_name(&type_name_of(other, &vm.names)).to_string(),
    }
}

fn pool_id(vm: &mut Vm, v: &Value) -> RResult<u64> {
    let rt = vm.rt.clone();
    let found = ident(v).filter(|id| rt.lock().threads.contains_key(id));
    match found {
        Some(id) => Ok(id),
        None => Err(no_such(vm, "thread", v)),
    }
}

fn semaphore_id(vm: &mut Vm, v: &Value) -> RResult<u64> {
    let rt = vm.rt.clone();
    let found = ident(v).filter(|id| rt.lock().semaphores.contains_key(id));
    match found {
        Some(id) => Ok(id),
        None => Err(no_such(vm, "semaphore", v)),
    }
}

fn channel_id(vm: &mut Vm, v: &Value) -> RResult<u64> {
    let rt = vm.rt.clone();
    let found = ident(v).filter(|id| rt.lock().channels.contains_key(id));
    match found {
        Some(id) => Ok(id),
        None => Err(no_such(vm, "channel", v)),
    }
}

fn num(n: u64) -> Value {
    Value::Number(Decimal::from_u64(n))
}

pub fn spawn(vm: &mut Vm, args: &[Value]) -> RResult<Value> {
    let rt = vm.rt.clone();
    let workers = whole(&args[1]).max(1);
    let capacity = match &args[2] {
        Value::None => None,
        other => Some(whole(other)),
    };
    let cv = Arc::new(Condvar::new());
    let (id, name) = {
        let mut st = rt.lock();
        rt.tracking.store(true, Ordering::SeqCst);
        let id = st.next_thread;
        st.next_thread += 1;
        let name = match &args[0] {
            Value::Str(s) => s.to_string(),
            _ => format!("thread-{id}"),
        };
        st.threads.insert(
            id,
            Pool {
                name: name.clone(),
                workers,
                capacity,
                jobs: VecDeque::new(),
                running: 0,
                running_set: BTreeSet::new(),
                closed: false,
                alive: workers,
                joiners: Vec::new(),
                cv: cv.clone(),
            },
        );
        (id, name)
    };
    for i in 0..workers {
        let rt = rt.clone();
        let cv = cv.clone();
        let started = std::thread::Builder::new()
            .name(format!("mah-{name}-{i}"))
            .stack_size(super::VM_STACK_SIZE)
            .spawn(move || worker(rt, id, cv));
        if let Err(e) = started {
            // The workers that never started are not alive: close the pool
            // so the ones that did exit (and a join can't wait forever).
            let mut st = vm.rt.lock();
            if let Some(pool) = st.threads.get_mut(&id) {
                pool.alive -= workers - i;
                pool.closed = true;
                pool.cv.notify_all();
            }
            st.check_quiescence();
            return Err(RuntimeError::new(format!("thread.spawn: could not start a thread: {e}")));
        }
    }
    Ok(Value::Vector(Rc::new(RefCell::new(vec![num(id), Value::Str(Rc::from(name.as_str()))]))))
}

pub fn submit(vm: &mut Vm, args: &[Value]) -> RResult<Value> {
    let thread_id = match &args[0] {
        Value::Struct(s) => {
            let b = s.borrow();
            if display_name(&b.type_name) == "Thread" {
                match b.get("id") {
                    Some(v @ Value::Number(_)) => Some(v.clone()),
                    _ => None,
                }
            } else {
                None
            }
        }
        _ => None,
    };
    let Some(thread_id) = thread_id else {
        return Err(RuntimeError::with_kind(
            format!("detach(...) needs a thread.Thread to run on, got {}", shown_type(vm, &args[0])),
            ErrorKind::TypeMismatch,
        ));
    };
    let Value::Function(closure) = &args[1] else {
        return Err(RuntimeError::with_kind(
            format!("thread.run: f must be a function, got {}", shown_type(vm, &args[1])),
            ErrorKind::TypeMismatch,
        ));
    };
    let pid = pool_id(vm, &thread_id)?;
    let values: Vec<Value> = match &args[2] {
        Value::Vector(v) => v.borrow().clone(),
        _ => Vec::new(),
    };
    let snapshot = match vm.make_job(closure, values)? {
        Ok(s) => s,
        Err(NotSendable) => return throw_thread_error("not_sendable", NOT_SENDABLE_MESSAGE),
    };
    let rt = vm.rt.clone();
    let entry = vm.alloc_id();
    let job_id = {
        let mut st = rt.lock();
        let next_job = st.next_job;
        let pool = st.threads.get_mut(&pid).expect("checked above");
        if pool.closed {
            let message = format!("thread '{}' is closed", pool.name);
            drop(st);
            return throw_thread_error("closed", &message);
        }
        if let Some(cap) = pool.capacity {
            if pool.jobs.len() as u64 + pool.running >= pool.workers + cap {
                let message = format!("thread '{}' is full ({} jobs queued or running)", pool.name, pool.workers + cap);
                drop(st);
                return throw_thread_error("full", &message);
            }
        }
        pool.jobs.push_back(Job { id: next_job, snapshot, reply_vm: vm.vm_id, reply_id: entry });
        pool.cv.notify_one();
        st.next_job += 1;
        next_job
    };
    let promise = PromiseData::new_pending();
    promise.borrow_mut().producer = Some(Producer::Job(job_id));
    vm.add_wait(entry, promise.clone(), Wait::Job);
    Ok(Value::Promise(promise))
}

pub fn close(vm: &mut Vm, args: &[Value]) -> RResult<Value> {
    let id = pool_id(vm, &args[0])?;
    let cancel = matches!(args[1], Value::Bool(true));
    vm.rt.lock().close_pool(id, cancel);
    Ok(Value::None)
}

pub fn join(vm: &mut Vm, args: &[Value]) -> RResult<Value> {
    let id = pool_id(vm, &args[0])?;
    if vm.thread_info.0 == id {
        return throw_thread_error("deadlock", "deadlock: a thread can't join itself");
    }
    let rt = vm.rt.clone();
    let entry = vm.alloc_id();
    {
        let mut st = rt.lock();
        st.close_pool(id, false);
        let pool = st.threads.get_mut(&id).expect("checked above");
        if pool.alive == 0 {
            return Ok(settled(Value::None));
        }
        pool.joiners.push((vm.vm_id, entry));
    }
    let promise = PromiseData::new_pending();
    promise.borrow_mut().producer = Some(Producer::Join(id));
    vm.add_wait(entry, promise.clone(), Wait::Join);
    Ok(Value::Promise(promise))
}

pub fn pending(vm: &mut Vm, args: &[Value]) -> RResult<Value> {
    let id = pool_id(vm, &args[0])?;
    let st = vm.rt.lock();
    let pool = st.threads.get(&id).expect("checked above");
    Ok(num(pool.jobs.len() as u64 + pool.running))
}

pub fn current(vm: &mut Vm, _args: &[Value]) -> RResult<Value> {
    let (id, name) = vm.thread_info.clone();
    Ok(Value::Vector(Rc::new(RefCell::new(vec![num(id), Value::Str(name)]))))
}

pub fn cores(_vm: &mut Vm, _args: &[Value]) -> RResult<Value> {
    let n = std::thread::available_parallelism().map(|n| n.get() as u64).unwrap_or(1);
    Ok(num(n))
}

pub fn semaphore_new(vm: &mut Vm, args: &[Value]) -> RResult<Value> {
    let permits = whole(&args[0]);
    let mut st = vm.rt.lock();
    let id = st.next_semaphore;
    st.next_semaphore += 1;
    st.semaphores.insert(id, Semaphore { permits, available: permits, waiters: VecDeque::new() });
    Ok(num(id))
}

pub fn semaphore_acquire(vm: &mut Vm, args: &[Value]) -> RResult<Value> {
    let id = semaphore_id(vm, &args[0])?;
    let rt = vm.rt.clone();
    let entry = vm.alloc_id();
    {
        let mut st = rt.lock();
        let sem = st.semaphores.get_mut(&id).expect("checked above");
        if sem.available > 0 && sem.waiters.is_empty() {
            sem.available -= 1;
            return Ok(settled(Value::None));
        }
        sem.waiters.push_back((vm.vm_id, entry));
    }
    let promise = PromiseData::new_pending();
    vm.add_wait(entry, promise.clone(), Wait::Sem);
    Ok(Value::Promise(promise))
}

pub fn semaphore_try_acquire(vm: &mut Vm, args: &[Value]) -> RResult<Value> {
    let id = semaphore_id(vm, &args[0])?;
    let mut st = vm.rt.lock();
    let sem = st.semaphores.get_mut(&id).expect("checked above");
    if sem.available > 0 && sem.waiters.is_empty() {
        sem.available -= 1;
        return Ok(Value::Bool(true));
    }
    Ok(Value::Bool(false))
}

pub fn semaphore_release(vm: &mut Vm, args: &[Value]) -> RResult<Value> {
    let id = semaphore_id(vm, &args[0])?;
    let result = vm.rt.lock().semaphore_release(id, true);
    match result {
        Ok(()) => Ok(Value::None),
        Err(permits) => throw_thread_error(
            "over_release",
            &format!("release without a matching acquire (all {permits} permits are free)"),
        ),
    }
}

pub fn semaphore_available(vm: &mut Vm, args: &[Value]) -> RResult<Value> {
    let id = semaphore_id(vm, &args[0])?;
    let st = vm.rt.lock();
    Ok(num(st.semaphores.get(&id).expect("checked above").available))
}

pub fn channel_new(vm: &mut Vm, args: &[Value]) -> RResult<Value> {
    let capacity = match &args[0] {
        Value::None => None,
        other => Some(whole(other)),
    };
    let mut st = vm.rt.lock();
    let id = st.next_channel;
    st.next_channel += 1;
    st.channels.insert(
        id,
        Channel {
            queue: VecDeque::new(),
            capacity,
            closed: false,
            receivers: VecDeque::new(),
            senders: VecDeque::new(),
        },
    );
    Ok(num(id))
}

pub fn channel_send(vm: &mut Vm, args: &[Value]) -> RResult<Value> {
    let id = channel_id(vm, &args[0])?;
    let Ok(message) = payload_of(&args[1]) else {
        return throw_thread_error("not_sendable", NOT_SENDABLE_MESSAGE);
    };
    let rt = vm.rt.clone();
    let entry = vm.alloc_id();
    {
        let mut st = rt.lock();
        let ch = st.channels.get_mut(&id).expect("checked above");
        if ch.closed {
            drop(st);
            return throw_thread_error("closed", CHANNEL_CLOSED_MESSAGE);
        }
        if let Some((rvm, rpid)) = ch.receivers.pop_front() {
            st.post(rvm, rpid, Completion::Recv(id, message));
            return Ok(settled(Value::None));
        }
        if ch.capacity.is_none_or(|cap| (ch.queue.len() as u64) < cap) {
            ch.queue.push_back(message);
            return Ok(settled(Value::None));
        }
        ch.senders.push_back((vm.vm_id, entry, message));
    }
    let promise = PromiseData::new_pending();
    vm.add_wait(entry, promise.clone(), Wait::Send);
    Ok(Value::Promise(promise))
}

pub fn channel_recv(vm: &mut Vm, args: &[Value]) -> RResult<Value> {
    let id = channel_id(vm, &args[0])?;
    let rt = vm.rt.clone();
    let entry = vm.alloc_id();
    let got = {
        let mut st = rt.lock();
        match st.take_message(id) {
            Some(m) => Some(m),
            None => {
                let ch = st.channels.get_mut(&id).expect("checked above");
                if ch.closed {
                    drop(st);
                    return Ok(failed(thread_error("closed", CHANNEL_CLOSED_MESSAGE)));
                }
                ch.receivers.push_back((vm.vm_id, entry));
                None
            }
        }
    };
    if let Some(m) = got {
        return Ok(settled(vm.materialize(&m)));
    }
    let promise = PromiseData::new_pending();
    vm.add_wait(entry, promise.clone(), Wait::Recv);
    Ok(Value::Promise(promise))
}

pub fn channel_try_recv(vm: &mut Vm, args: &[Value]) -> RResult<Value> {
    let id = channel_id(vm, &args[0])?;
    let got = vm.rt.lock().take_message(id);
    Ok(match got {
        Some(m) => {
            let v = vm.materialize(&m);
            Value::Enum(Rc::new(RefCell::new(EnumData {
                type_name: Rc::from("Option"),
                variant: Rc::from("some"),
                fields: vec![(Rc::from("value"), v)],
                thrown_at: None,
                backtrace: None,
            })))
        }
        None => Value::None,
    })
}

pub fn channel_close(vm: &mut Vm, args: &[Value]) -> RResult<Value> {
    let id = channel_id(vm, &args[0])?;
    let mut st = vm.rt.lock();
    let ch = st.channels.get_mut(&id).expect("checked above");
    if ch.closed {
        return Ok(Value::None);
    }
    ch.closed = true;
    let receivers: Vec<(u64, u64)> = ch.receivers.drain(..).collect();
    let senders: Vec<(u64, u64)> = ch.senders.drain(..).map(|(v, p, _)| (v, p)).collect();
    for (v, p) in receivers.into_iter().chain(senders) {
        st.post(v, p, Completion::Settle(Err(Payload::thread_error("closed", CHANNEL_CLOSED_MESSAGE))));
    }
    Ok(Value::None)
}

pub fn channel_len(vm: &mut Vm, args: &[Value]) -> RResult<Value> {
    let id = channel_id(vm, &args[0])?;
    let st = vm.rt.lock();
    Ok(num(st.channels.get(&id).expect("checked above").queue.len() as u64))
}

pub fn channel_closed(vm: &mut Vm, args: &[Value]) -> RResult<Value> {
    let id = channel_id(vm, &args[0])?;
    let st = vm.rt.lock();
    Ok(Value::Bool(st.channels.get(&id).expect("checked above").closed))
}

// ---------------------------------------------------------------------------
// Transactions (docs/contracts/M45_atomic.md #6.3) -- the VM side
// ---------------------------------------------------------------------------

/// `ThreadError in_atomic` for `name` (#3.8), thrown as a Mah value.
pub fn in_atomic<T>(name: &str) -> RResult<T> {
    throw_thread_error("in_atomic", &format!("'{name}' can't run inside 'atomic {{ }}': its body may run more than once"))
}

fn task_key(task: &TaskRef) -> usize {
    Rc::as_ptr(task) as *const () as usize
}

/// Whether `task` began (owns) the transaction it is in.
pub fn owns_tx(task: &TaskRef) -> bool {
    let t = task.borrow();
    matches!(&t.tx, Some(tx) if tx.borrow().owner == task_key(task))
}

/// `sharedget k, name, mode` (`working` = mode 1).
pub fn get(vm: &mut Vm, task: &TaskRef, k: u64, name: &Rc<str>, working: bool) -> RResult<Value> {
    let tx = task.borrow().tx.clone();
    let Some(tx) = tx else {
        if working {
            return Err(RuntimeError::new("sharedget in working mode outside a transaction"));
        }
        let (p, _) = vm.rt.read_shared(k);
        return Ok(vm.materialize(&p));
    };
    let pos = tx.borrow().entries.iter().position(|e| e.index == k);
    let pos = match pos {
        Some(pos) => pos,
        None => {
            let (p, version) = vm.rt.read_shared(k);
            if version > tx.borrow().rv {
                return Err(RuntimeError::restart(TxSignal::Conflict));
            }
            let working_value = vm.materialize(&p);
            let mut t = tx.borrow_mut();
            t.reads.push((k, version));
            t.entries.push(TxEntry {
                index: k,
                name: name.clone(),
                base: Some(p),
                working: working_value,
                assigned: false,
                exposed: false,
            });
            t.entries.len() - 1
        }
    };
    let mut t = tx.borrow_mut();
    let entry = &mut t.entries[pos];
    if working {
        entry.exposed = true;
        return Ok(entry.working.clone());
    }
    Ok(copy_local(&entry.working))
}

/// `sharedset k, name, src`.
pub fn set(vm: &mut Vm, task: &TaskRef, k: u64, name: &Rc<str>, v: Value) -> RResult<()> {
    let tx = task.borrow().tx.clone();
    if let Some(tx) = tx {
        let w = copy_local(&v);
        let mut t = tx.borrow_mut();
        match t.entries.iter_mut().find(|e| e.index == k) {
            Some(entry) => {
                entry.working = w;
                entry.assigned = true;
            }
            None => t.entries.push(TxEntry {
                index: k,
                name: name.clone(),
                base: None,
                working: w,
                assigned: true,
                exposed: false,
            }),
        }
        return Ok(());
    }
    let Ok(stored) = payload_of(&v) else {
        return throw_thread_error("not_sendable", &format!("shared variable '{name}' can't hold a Promise"));
    };
    vm.rt.write_shared(vm.vm_id, k, stored);
    Ok(())
}

/// `tx_start`, guarded: any error but a restart ends the transaction.
/// (Run through `guarded`: in Rust nothing in it can fail today, but the
/// guard keeps the #6.3 rule in one place.)
fn start(rt: &ThreadRuntime, vm_id: u64, tx: &Rc<RefCell<Tx>>) -> RResult<()> {
    let (serial, irrevocable) = {
        let t = tx.borrow();
        (t.serial, t.irrevocable)
    };
    // (never hold a RefCell borrow across the exclusivity wait)
    let rv = rt.tx_start(vm_id, serial, irrevocable);
    tx.borrow_mut().rv = rv;
    Ok(())
}

/// `atomicbegin` at pc `pc`.
pub fn begin(vm: &mut Vm, task: &TaskRef, pc: usize) -> RResult<()> {
    let rt = vm.rt.clone();
    begin_in(&rt, vm.vm_id, task, pc)
}

fn begin_in(rt: &ThreadRuntime, vm_id: u64, task: &TaskRef, pc: usize) -> RResult<()> {
    let tx = task.borrow().tx.clone();
    match tx {
        None => {
            let tx = {
                let t = task.borrow();
                Tx::new(
                    task_key(task),
                    t.implicit.clone(),
                    (pc, t.current_frame.clone(), t.return_stack.len(), t.defer_stack.len()),
                )
            };
            let tx = Rc::new(RefCell::new(tx));
            task.borrow_mut().tx = Some(tx.clone());
            guarded(rt, task, &tx, |rt| start(rt, vm_id, &tx))
        }
        Some(tx) => {
            {
                let mut t = tx.borrow_mut();
                if t.depth > 0 {
                    t.depth += 1;
                    return Ok(());
                }
                t.depth = 1;
                if t.attempts >= ATOMIC_ATTEMPTS {
                    t.irrevocable = true;
                }
            }
            guarded(rt, task, &tx, |rt| start(rt, vm_id, &tx))
        }
    }
}

/// #6.3 "Guards": an error (not a restart) out of `f` ends the transaction.
fn guarded(
    rt: &ThreadRuntime,
    task: &TaskRef,
    tx: &Rc<RefCell<Tx>>,
    f: impl FnOnce(&ThreadRuntime) -> RResult<()>,
) -> RResult<()> {
    match f(rt) {
        Ok(()) => Ok(()),
        Err(e) if e.restart.is_some() => Err(e),
        Err(e) => {
            rt.end_attempt(tx.borrow().serial);
            let mut t = task.borrow_mut();
            if matches!(&t.tx, Some(cur) if Rc::ptr_eq(cur, tx)) {
                t.tx = None;
            }
            Err(e)
        }
    }
}

fn the_tx(task: &TaskRef, op: &str) -> RResult<Rc<RefCell<Tx>>> {
    task.borrow().tx.clone().ok_or_else(|| RuntimeError::new(format!("{op} outside a transaction")))
}

/// `atomicend`: the body's call returned.
pub fn end(vm: &mut Vm, task: &TaskRef) -> RResult<()> {
    let rt = vm.rt.clone();
    end_in(&rt, vm.vm_id, task)
}

fn end_in(rt: &ThreadRuntime, vm_id: u64, task: &TaskRef) -> RResult<()> {
    let tx = the_tx(task, "atomicend")?;
    {
        let mut t = tx.borrow_mut();
        if t.depth > 1 {
            t.depth -= 1;
            return Ok(());
        }
    }
    guarded(rt, task, &tx, |rt| {
        let mut publish: Vec<(u64, Payload)> = Vec::new();
        let mut refused: Option<Rc<str>> = None;
        let (serial, reads) = {
            let t = tx.borrow();
            for entry in &t.entries {
                if !(entry.assigned || entry.exposed) {
                    continue;
                }
                let Ok(stored) = payload_of(&entry.working) else {
                    refused = Some(entry.name.clone());
                    break;
                };
                if !entry.assigned {
                    if let Some(base) = &entry.base {
                        if same_copy(&stored, base) {
                            continue;
                        }
                    }
                }
                publish.push((entry.index, stored));
            }
            (t.serial, t.reads.clone())
        };
        if let Some(name) = refused {
            rt.end_attempt(serial);
            task.borrow_mut().tx = None;
            return throw_thread_error("not_sendable", &format!("shared variable '{name}' can't hold a Promise"));
        }
        if !rt.tx_commit(vm_id, serial, &reads, publish) {
            return Err(RuntimeError::restart(TxSignal::Conflict));
        }
        task.borrow_mut().tx = None;
        Ok(())
    })
}

/// `atomicabort`: a throw is leaving the block.
pub fn abort(vm: &mut Vm, task: &TaskRef) -> RResult<()> {
    let rt = vm.rt.clone();
    abort_in(&rt, task)
}

fn abort_in(rt: &ThreadRuntime, task: &TaskRef) -> RResult<()> {
    let tx = the_tx(task, "atomicabort")?;
    let serial = {
        let mut t = tx.borrow_mut();
        if t.depth > 1 {
            t.depth -= 1;
            return Ok(());
        }
        t.serial
    };
    rt.end_attempt(serial);
    task.borrow_mut().tx = None;
    Ok(())
}

/// `retry`.
pub fn retry(task: &TaskRef) -> RResult<()> {
    let tx = the_tx(task, "retry")?;
    let t = tx.borrow();
    if let Some(label) = &t.implicit {
        return Err(RuntimeError::new(format!(
            "'{label}' cannot suspend (it used 'retry') when called implicitly by the runtime"
        )));
    }
    if t.reads.is_empty() {
        return throw_thread_error("stuck", RETRY_NO_READS_MESSAGE);
    }
    Err(RuntimeError::restart(TxSignal::Retry))
}

/// #6.4: abandon the attempt of `task`'s transaction -- back to its
/// outermost `atomicbegin`; a `retry` then waits for a change of what the
/// attempt read. `Ok(None)`: keep stepping; `Ok(Some(()))`: suspended.
pub fn restart_tx(vm: &mut Vm, task: &TaskRef, sig: TxSignal) -> RResult<Option<()>> {
    let tx = task.borrow().tx.clone().expect("restart_tx: the task is in a transaction");
    let (pc, frame, rs, ds, serial) = {
        let t = tx.borrow();
        let (pc, frame, rs, ds) = t.restart.clone();
        (pc, frame, rs, ds, t.serial)
    };
    {
        let mut t = task.borrow_mut();
        t.pc = pc;
        t.current_frame = frame;
        t.return_stack.truncate(rs);
        t.defer_stack.truncate(ds);
    }
    vm.rt.end_attempt(serial);
    if sig == TxSignal::Conflict {
        let mut t = tx.borrow_mut();
        t.attempts += 1;
        t.depth = 0;
        t.reads.clear();
        t.entries.clear();
        t.irrevocable = false;
        return Ok(None);
    }
    let reads = std::mem::take(&mut tx.borrow_mut().reads);
    task.borrow_mut().tx = None;
    let pid = vm.alloc_id();
    let promise = PromiseData::new_pending();
    // registered before the runtime can post its wake-up
    vm.add_wait(pid, promise.clone(), Wait::Retry);
    if !vm.rt.register_retry(vm.vm_id, pid, &reads) {
        vm.take_wait(pid);
        return Ok(None);
    }
    promise.borrow_mut().callbacks.push(Continuation { task: task.clone(), dest: (0, 0), resume_pc: pc + 1, restart: true });
    Ok(Some(()))
}

/// #6.3 safety net: a task never ends inside a transaction it began.
pub fn end_task_tx(rt: &ThreadRuntime, task: &TaskRef) {
    if owns_tx(task) {
        let tx = task.borrow_mut().tx.take().expect("owns_tx");
        rt.end_attempt(tx.borrow().serial);
    }
}

// ---------------------------------------------------------------------------
// Tests: the wait-for graph (mirrors tests/test_threads.py::WaitForGraphTests)
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn state() -> RtState {
        RtState { next_thread: 1, next_job: 1, next_semaphore: 1, next_channel: 1, ..RtState::default() }
    }

    fn pool(st: &mut RtState, id: u64, running: &[u64]) {
        st.threads.insert(
            id,
            Pool {
                name: "p".to_string(),
                workers: 1,
                capacity: None,
                jobs: VecDeque::new(),
                running: running.len() as u64,
                running_set: running.iter().copied().collect(),
                closed: false,
                alive: 1,
                joiners: Vec::new(),
                cv: Arc::new(Condvar::new()),
            },
        );
    }

    #[test]
    fn join_cycle() {
        let mut st = state();
        pool(&mut st, 1, &[7]);
        st.job_roots.insert(7, 40);
        pool(&mut st, 2, &[8]);
        st.job_roots.insert(8, 50);
        st.awaiting.insert(40, (Producer::Join(2), 0));
        let edges = st.producer_edges(Producer::Join(1));
        assert!(st.cycle_from(edges, 50, true));
    }

    #[test]
    fn a_job_not_started_yet_is_no_edge_until_it_is() {
        let mut st = state();
        st.awaiting.insert(30, (Producer::Job(6), 0));
        st.job_roots.insert(6, 10);
        let edges = st.producer_edges(Producer::Job(5));
        assert!(!st.cycle_from(edges, 10, true));
        st.job_roots.insert(5, 30);
        let edges = st.producer_edges(Producer::Job(5));
        assert!(st.cycle_from(edges, 10, true));
    }

    #[test]
    fn a_plain_await_cycle_is_not_reported() {
        let mut st = state();
        st.awaiting.insert(20, (Producer::Task(10), 0));
        let edges = st.producer_edges(Producer::Task(20));
        assert!(!st.cycle_from(edges, 10, false));
    }

    #[test]
    fn copies_keep_identity_and_refuse_promises() {
        let inner = Value::Vector(Rc::new(RefCell::new(vec![Value::Number(Decimal::from_i64(1))])));
        let outer = Value::Vector(Rc::new(RefCell::new(vec![inner.clone(), inner])));
        let (graph, roots) = copy_out(&[(&outer, true)]).expect("no Promise");
        let m = copy_in(&[], &graph);
        let Value::Vector(v) = m.value(&roots[0]) else { panic!("a Vector") };
        let v = v.borrow();
        assert!(value::values_equal(&v[0], &v[1]));
        let p = Value::Promise(PromiseData::new_pending());
        let bad = Value::Vector(Rc::new(RefCell::new(vec![p])));
        assert!(copy_out(&[(&bad, true)]).is_err());
    }

    // -- M45: the runtime's transaction helpers, driven by hand (mirrors
    // tests/test_threads.py::AtomicRuntimeTests 1-3 and 6) ------------------

    fn runtime() -> (ThreadRuntime, Receiver<(u64, Completion)>) {
        let program = Program {
            strings: Vec::new(),
            constants: Vec::new(),
            types: Vec::new(),
            natives: Vec::new(),
            functions: Vec::new(),
            code: Vec::new(),
            debug: None,
            minor: 21,
            handlers: Vec::new(),
            tests: Vec::new(),
            meta: None,
        };
        let rt = ThreadRuntime::new(Arc::new(program), &[], false);
        let (tx, rx) = std::sync::mpsc::channel();
        rt.register_vm(0, tx);
        (rt, rx)
    }

    fn number(n: i64) -> Payload {
        payload_of(&Value::Number(Decimal::from_i64(n))).expect("a Number")
    }

    fn number_of(p: &Payload) -> Decimal {
        match &p.root {
            SendValue::Number(n) => Decimal::from_send(n.clone()),
            _ => panic!("a Number"),
        }
    }

    #[test]
    fn atomic_1_validation() {
        let (rt, _rx) = runtime();
        rt.write_shared(0, 0, number(1));
        let rv = rt.tx_start(0, 1, false);
        assert_eq!(rv, 1);
        rt.write_shared(0, 0, number(2));
        assert!(!rt.tx_commit(0, 1, &[(0, 1)], vec![(0, number(5))]));
        let (p, v) = rt.read_shared(0);
        assert_eq!((number_of(&p), v), (Decimal::from_i64(2), 2));
        let _ = rt.tx_start(0, 2, false);
        assert!(rt.tx_commit(0, 2, &[(0, 2)], vec![(0, number(5))]));
        let (p, v) = rt.read_shared(0);
        assert_eq!((number_of(&p), v), (Decimal::from_i64(5), 3));
    }

    #[test]
    fn atomic_2_read_only() {
        let (rt, _rx) = runtime();
        rt.write_shared(0, 0, number(1));
        let _ = rt.tx_start(0, 1, false);
        rt.write_shared(0, 0, number(2));
        let clock = rt.lock().clock;
        assert!(rt.tx_commit(0, 1, &[(0, 1)], Vec::new()));
        assert_eq!(rt.lock().clock, clock);
    }

    #[test]
    fn atomic_3_retry_wake_ups() {
        let (rt, rx) = runtime();
        rt.write_shared(0, 0, number(1));
        rt.write_shared(0, 1, number(1));
        let version = rt.read_shared(0).1;
        assert!(rt.register_retry(0, 5, &[(0, version)]));
        rt.write_shared(0, 1, number(2));
        assert!(rx.try_recv().is_err());
        rt.write_shared(0, 0, number(2));
        match rx.try_recv() {
            Ok((5, Completion::Settle(Ok(p)))) => assert!(matches!(p.root, SendValue::None)),
            _ => panic!("a settle ok none for pending id 5"),
        }
        assert!(!rt.register_retry(0, 6, &[(0, 1)]));
        assert!(rt.lock().retry_waits.is_empty());
    }

    fn task_in(attempts: u32, implicit: Option<&str>) -> (TaskRef, Rc<RefCell<Tx>>) {
        let task = value::new_task(0, value::new_frame(0, None), None);
        let mut tx = Tx::new(task_key(&task), implicit.map(Rc::from), (0, value::new_frame(0, None), 0, 0));
        tx.depth = 0;
        tx.attempts = attempts;
        let tx = Rc::new(RefCell::new(tx));
        task.borrow_mut().tx = Some(tx.clone());
        (task, tx)
    }

    #[test]
    fn atomic_6_the_ninth_attempt_is_exclusive() {
        let (rt, _rx) = runtime();
        let (task, tx) = task_in(ATOMIC_ATTEMPTS, None);
        begin_in(&rt, 0, &task, 0).expect("begins");
        assert!(tx.borrow().irrevocable);
        assert_eq!(tx.borrow().depth, 1);
        assert_eq!(rt.lock().excl_owner, Some((tx.borrow().serial, 0)));
        end_in(&rt, 0, &task).expect("commits");
        assert_eq!(rt.lock().excl_owner, None);
        assert!(task.borrow().tx.is_none());

        let (task, tx) = task_in(ATOMIC_ATTEMPTS - 1, None);
        begin_in(&rt, 0, &task, 0).expect("begins");
        assert!(!tx.borrow().irrevocable);
        assert_eq!(rt.lock().excl_owner, None);
        end_in(&rt, 0, &task).expect("commits");

        let (task, tx) = task_in(ATOMIC_ATTEMPTS, Some("to_string"));
        begin_in(&rt, 0, &task, 0).expect("begins");
        assert!(tx.borrow().irrevocable);
        assert_eq!(rt.lock().excl_owner, Some((tx.borrow().serial, 0)));
        end_in(&rt, 0, &task).expect("commits");
        assert_eq!(rt.lock().excl_owner, None);
    }

    #[test]
    fn atomic_exclusivity_blocks_writers() {
        let (rt, _rx) = runtime();
        let rt = Arc::new(rt);
        let _ = rt.tx_start(0, 1, true);
        let writer = {
            let rt = rt.clone();
            std::thread::spawn(move || rt.write_shared(1, 0, number(9)))
        };
        std::thread::sleep(Duration::from_millis(200));
        assert_eq!(rt.read_shared(0).1, 0);
        assert!(!writer.is_finished());
        rt.end_attempt(1);
        writer.join().expect("the writer finishes");
        assert_eq!(number_of(&rt.read_shared(0).0), Decimal::from_i64(9));
    }

    #[test]
    fn atomic_teardown_drops_retry_waits_and_the_token() {
        // #6.8: a job VM torn down while it holds the token and has retry
        // waits releases the one and forgets the others (the main VM's
        // retry wait on the same variable stays and is woken)
        let (rt, rx) = runtime();
        let (job_tx, _job_rx) = std::sync::mpsc::channel();
        rt.register_vm(7, job_tx);
        rt.write_shared(0, 0, number(1));
        let version = rt.read_shared(0).1;
        assert!(rt.register_retry(7, 3, &[(0, version)]));
        assert!(rt.register_retry(0, 4, &[(0, version)]));
        let _ = rt.tx_start(7, 42, true);
        assert_eq!(rt.lock().excl_owner, Some((42, 7)));
        rt.forget_vm(7, None, None);
        {
            let st = rt.lock();
            assert_eq!(st.excl_owner, None);
            assert_eq!(st.retry_waits.keys().copied().collect::<Vec<_>>(), vec![(0, 4)]);
            assert_eq!(st.watchers.get(&0).map(|w| w.iter().copied().collect::<Vec<_>>()), Some(vec![(0, 4)]));
        }
        // the token is free: a plain write doesn't wait, and wakes vm 0 only
        rt.write_shared(0, 0, number(2));
        assert!(matches!(rx.try_recv(), Ok((4, Completion::Settle(Ok(_))))));
        assert!(rt.lock().retry_waits.is_empty());
    }

    #[test]
    fn same_copy_compares_graphs() {
        let v = |items: Vec<Value>| Value::Vector(Rc::new(RefCell::new(items)));
        let n = |i: i64| Value::Number(Decimal::from_i64(i));
        let a = payload_of(&v(vec![n(1), Value::Str(Rc::from("x"))])).unwrap();
        let b = payload_of(&v(vec![n(1), Value::Str(Rc::from("x"))])).unwrap();
        let c = payload_of(&v(vec![n(2), Value::Str(Rc::from("x"))])).unwrap();
        assert!(same_copy(&a, &b));
        assert!(!same_copy(&a, &c));
        // shared vs. separate inner objects differ
        let inner = v(vec![n(1)]);
        let shared = payload_of(&v(vec![inner.clone(), inner])).unwrap();
        let separate = payload_of(&v(vec![v(vec![n(1)]), v(vec![n(1)])])).unwrap();
        assert!(!same_copy(&shared, &separate));
        assert!(same_copy(&Payload::none(), &Payload::none()));
    }
}
