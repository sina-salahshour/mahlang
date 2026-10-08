//! M44 (docs/contracts/M44_threads.md): the process-wide thread runtime of
//! the Rust VM -- jobs on other threads, shared variables and their locks,
//! semaphores, channels, the wait-for graph and the quiescence rule. A port
//! of `mah/thread_runtime.py` and `mah/thread_natives.py`, which define every
//! rule and message.
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
use std::time::Instant;

use crate::decimal::{Decimal, SendDecimal};
use crate::decode::Program;

use super::error::{ErrorKind, RResult, RuntimeError};
use super::exec::{Completion, Vm, Wait};
use super::fs::FileTable;
use super::link::LinkedProgram;
use super::socket::SocketTable;
use super::value::{
    self, display_name, type_name_of, ClosureData, EnumData, FrameRef, FunctionInfo, MapData, Producer, PromiseData,
    StructData, TaskRef, TypeData, Value,
};

pub const NOT_SENDABLE_MESSAGE: &str = "a Promise can't be sent to another thread";
pub const FOREIGN_PROMISE_MESSAGE: &str =
    "a Promise from another thread can't be awaited here (it was still pending when it was copied)";
pub const STUCK_MESSAGE: &str = "the wait can never finish: every thread is waiting";
pub const JOB_STUCK_MESSAGE: &str = "the job never finished: it waits on a Promise nothing will settle";
pub const AWAIT_DEADLOCK_MESSAGE: &str =
    "deadlock: this await would never end (it waits, through locks or threads, for itself)";
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

struct LockState {
    /// (task id, vm id)
    owner: Option<(u64, u64)>,
    /// (task id, vm id, pending id)
    waiters: VecDeque<(u64, u64, u64)>,
}

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
    shared: HashMap<u64, Arc<Payload>>,
    locks: BTreeMap<u64, LockState>,
    waiting_on: HashMap<u64, u64>,
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
}

impl Default for LockState {
    fn default() -> Self {
        LockState { owner: None, waiters: VecDeque::new() }
    }
}

pub struct ThreadRuntime {
    state: Mutex<RtState>,
    /// false until the first `sharedlock` or `thread.spawn` of the run
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

    // -- shared variables (#6.4) ------------------------------------------------

    /// The store's value of shared variable `k` (absent: `none`).
    pub fn shared_value(&self, k: u64) -> Option<Arc<Payload>> {
        self.lock().shared.get(&k).cloned()
    }

    /// Write back (`value` = `None`: refused) and pass the lock on.
    pub fn release(&self, k: u64, value: Option<Payload>) {
        let mut st = self.lock();
        if let Some(p) = value {
            st.shared.insert(k, Arc::new(p));
        }
        st.grant_next(k);
    }

    /// Program end / teardown of a job VM (#6.6).
    pub fn forget_vm(&self, vm_id: u64, job_id: Option<u64>, rx: Option<&Receiver<(u64, Completion)>>) {
        {
            let mut st = self.lock();
            let owned: Vec<u64> = st
                .locks
                .iter()
                .filter(|(_, l)| matches!(l.owner, Some((_, v)) if v == vm_id))
                .map(|(k, _)| *k)
                .collect();
            for k in owned {
                st.grant_next(k);
            }
            let RtState { locks, waiting_on, semaphores, channels, threads, awaiting, .. } = &mut *st;
            for l in locks.values_mut() {
                l.waiters.retain(|&(task, vm, _)| {
                    if vm == vm_id {
                        waiting_on.remove(&task);
                        false
                    } else {
                        true
                    }
                });
            }
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
        if let Some(k) = self.waiting_on.get(&node) {
            if let Some(LockState { owner: Some((owner, _)), .. }) = self.locks.get(k) {
                edges.push((*owner, true));
            }
        }
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

    fn cycle(&self, start: u64, target: u64) -> bool {
        self.cycle_from(vec![(start, false)], target, true)
    }

    fn grant_next(&mut self, k: u64) {
        let Some(state) = self.locks.get_mut(&k) else { return };
        state.owner = None;
        if let Some((task, vm, pid)) = state.waiters.pop_front() {
            state.owner = Some((task, vm));
            self.waiting_on.remove(&task);
            let value = self.shared.get(&k).cloned().unwrap_or_else(|| Arc::new(Payload::none()));
            self.post(vm, pid, Completion::Lock(value));
        }
    }

    fn check_quiescence(&mut self) {
        let live = self.vms.len();
        // A worker that has a job to start, or (closed pool) is about to exit
        // and settle its joiners, can still make progress.
        let starting =
            self.threads.values().any(|p| (!p.jobs.is_empty() || p.closed) && p.running < p.alive);
        if live > 0 && self.blocked_count == live && !starting && self.vms.contains_key(&0) {
            self.post(0, NO_ENTRY, Completion::Stuck);
        }
    }

    fn remove_waiter(&mut self, vm_id: u64, pid: u64) {
        let RtState { locks, waiting_on, semaphores, channels, threads, .. } = self;
        for l in locks.values_mut() {
            l.waiters.retain(|&(task, vm, p)| {
                if vm == vm_id && p == pid {
                    waiting_on.remove(&task);
                    false
                } else {
                    true
                }
            });
        }
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
        std::thread::Builder::new()
            .name(format!("mah-{name}-{i}"))
            .stack_size(super::VM_STACK_SIZE)
            .spawn(move || worker(rt, id, cv))
            .map_err(|e| RuntimeError::new(format!("thread.spawn: could not start a thread: {e}")))?;
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
// Locks (#6.4) -- the VM side
// ---------------------------------------------------------------------------

/// `sharedlock`: acquire shared variable `k` for `task`; a Promise.
pub fn acquire(vm: &mut Vm, task: &TaskRef, k: u64, name: &str) -> RResult<Value> {
    let held = task.borrow().held.clone();
    if let Some(h) = held.borrow_mut().iter_mut().find(|h| h.index == k) {
        h.depth += 1;
        return Ok(settled(Value::None));
    }
    let task_id = task.borrow().id;
    let rt = vm.rt.clone();
    let entry = vm.alloc_id();
    let granted = {
        let mut st = rt.lock();
        rt.tracking.store(true, Ordering::SeqCst);
        let owner = st.locks.entry(k).or_default().owner;
        match owner {
            None => {
                st.locks.get_mut(&k).expect("just made").owner = Some((task_id, vm.vm_id));
                Some(st.shared.get(&k).cloned())
            }
            Some((u, _)) => {
                if st.cycle(u, task_id) {
                    drop(st);
                    return Ok(failed(thread_error("deadlock", &format!("deadlock: waiting for '{name}' would never end"))));
                }
                st.locks.get_mut(&k).expect("just made").waiters.push_back((task_id, vm.vm_id, entry));
                st.waiting_on.insert(task_id, k);
                None
            }
        }
    };
    match granted {
        Some(stored) => {
            let value = match stored {
                Some(p) => vm.materialize(&p),
                None => Value::None,
            };
            held.borrow_mut().push(value::Held { index: k, value, depth: 1, unwinding: 0 });
            Ok(settled(Value::None))
        }
        None => {
            let promise = PromiseData::new_pending();
            vm.add_wait(entry, promise.clone(), Wait::Lock { task: task.clone(), index: k });
            Ok(Value::Promise(promise))
        }
    }
}

/// `sharedunlock` mode 0: release (and write back) at the outermost level.
pub fn release(vm: &mut Vm, task: &TaskRef, k: u64, name: &str) -> RResult<()> {
    let held = task.borrow().held.clone();
    let working = {
        let mut h = held.borrow_mut();
        let Some(pos) = h.iter().position(|e| e.index == k) else {
            return Err(RuntimeError::new("sharedunlock without holding the lock"));
        };
        let entry = &mut h[pos];
        let leaving_by_throw = entry.unwinding == entry.depth;
        if leaving_by_throw {
            entry.unwinding = 0;
        }
        entry.depth -= 1;
        if entry.depth > 0 {
            return Ok(());
        }
        let entry = h.remove(pos);
        (entry.value, leaving_by_throw)
    };
    let (value, leaving_by_throw) = working;
    let copy = payload_of(&value).ok();
    let refused = copy.is_none();
    vm.rt.release(k, copy);
    if refused && !leaving_by_throw {
        return throw_thread_error("not_sendable", &format!("shared variable '{name}' can't hold a Promise"));
    }
    Ok(())
}

/// `sharedunlock` mode 1: a `lock` body is being left by a throw.
pub fn mark(task: &TaskRef, k: u64) {
    let held = task.borrow().held.clone();
    if let Some(h) = held.borrow_mut().iter_mut().find(|h| h.index == k) {
        h.unwinding = h.depth;
    }
}

/// `sharedget`.
pub fn get(vm: &mut Vm, task: &TaskRef, k: u64, locked: bool) -> RResult<Value> {
    let held = task.borrow().held.clone();
    let working = held.borrow().iter().find(|h| h.index == k).map(|h| h.value.clone());
    if locked {
        return working.ok_or_else(|| RuntimeError::new("sharedget without holding the lock"));
    }
    if let Some(w) = working {
        return Ok(copy_local(&w));
    }
    Ok(match vm.rt.shared_value(k) {
        Some(p) => vm.materialize(&p),
        None => Value::None,
    })
}

/// `sharedset`.
pub fn set(task: &TaskRef, k: u64, v: Value) -> RResult<()> {
    let held = task.borrow().held.clone();
    let mut h = held.borrow_mut();
    match h.iter_mut().find(|h| h.index == k) {
        Some(entry) => {
            entry.value = v;
            Ok(())
        }
        None => Err(RuntimeError::new("sharedset without holding the lock")),
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

    fn own(st: &mut RtState, k: u64, task: u64) {
        st.locks.entry(k).or_default().owner = Some((task, 0));
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
    fn lock_to_lock() {
        let mut st = state();
        own(&mut st, 1, 10);
        own(&mut st, 2, 20);
        st.waiting_on.insert(10, 2);
        // 20 asks for lock 1 (owned by 10, which waits for 20's lock 2)
        assert!(st.cycle(10, 20));
    }

    #[test]
    fn await_task_then_lock_back() {
        let mut st = state();
        own(&mut st, 1, 10);
        st.waiting_on.insert(20, 1);
        // 10 awaits task 20, which waits for 10's lock
        let edges = st.producer_edges(Producer::Task(20));
        assert!(st.cycle_from(edges, 10, false));
    }

    #[test]
    fn await_job_whose_root_waits_on_my_lock() {
        let mut st = state();
        own(&mut st, 1, 10);
        st.job_roots.insert(5, 30);
        st.waiting_on.insert(30, 1);
        let edges = st.producer_edges(Producer::Job(5));
        assert!(st.cycle_from(edges, 10, true));
    }

    #[test]
    fn a_job_not_started_yet_is_no_edge_until_it_is() {
        let mut st = state();
        own(&mut st, 1, 10);
        st.waiting_on.insert(30, 1);
        let edges = st.producer_edges(Producer::Job(5));
        assert!(!st.cycle_from(edges, 10, true));
        st.job_roots.insert(5, 30);
        let edges = st.producer_edges(Producer::Job(5));
        assert!(st.cycle_from(edges, 10, true));
    }

    #[test]
    fn join_reaches_a_running_jobs_root() {
        let mut st = state();
        own(&mut st, 1, 10);
        pool(&mut st, 3, &[7]);
        st.job_roots.insert(7, 40);
        st.waiting_on.insert(40, 1);
        let edges = st.producer_edges(Producer::Join(3));
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
}
