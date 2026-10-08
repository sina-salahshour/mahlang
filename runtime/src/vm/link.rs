//! `Program` (raw indices, `crate::decode`) -> `LinkedProgram` (resolved
//! strings/constants/types/functions/natives) -- a Rust port of
//! `code_interpreter.py`'s `_link`. Every string used anywhere in the
//! program is interned exactly once into an `Rc<str>`, so every later
//! lookup (a `getfield`'s field name, a method name, a type name) is a
//! cheap refcount bump, never a fresh allocation -- see the spec's
//! performance section.

use std::rc::Rc;

use crate::decimal::Decimal;
use crate::decode::{
    self, Addr, BinOp, Const, FunctionDecl, HandlerEntry, Meta, NativeRef, Program, RawInstr, TypeBody, TypeDecl,
    RUNTIME_ERROR_VARIANTS,
};

use super::value::{FunctionInfo, TypeData, Value, PRIMITIVE_TYPE_NAMES};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum NativeFn {
    IoPrint,
    IoWrite,
    IoInput,
    MathSin,
    MathCos,
    TimeSleepAsync,
    /// M27 (1.5): `std:math`'s transcendental functions -- all computed in
    /// f64, like `math.sin`/`math.cos` (docs/MAHC_FORMAT.md #4.4).
    MathTan,
    MathAsin,
    MathAcos,
    MathAtan,
    MathAtan2,
    MathExp,
    MathLog,
    MathLog10,
    /// M30 (1.7): reflection and characters, for std:json/std:csv.
    ValueTypeName,
    ValueFields,
    ValueVariant,
    StringChars,
    StringCodePoint,
    StringFromCodePoint,
    /// M31 (1.8): the shared xoshiro256** generator, for std:random.
    RandomSeed,
    RandomFresh,
    RandomNext,
    RandomBelow,
    /// M32 (1.9): std:regex's matcher, over canonical patterns.
    RegexFind,
    RegexFindAll,
    /// M33 (1.10): the async `input`.
    IoReadLine,
    /// M34 (1.11): std:time and std:async.
    TimeNowMs,
    TimeMonotonicMs,
    TimeCancel,
    PromiseNew,
    PromiseResolve,
    PromiseFail,
    /// M35 (1.12): std:fs (runtime/src/vm/fs.rs).
    FsReadText,
    FsWriteText,
    FsAppendText,
    FsInfo,
    FsListDir,
    FsMkdir,
    FsRemove,
    FsRename,
    FsCopy,
    FsTempDir,
    FsOpen,
    FsReadLine,
    FsReadAll,
    FsWrite,
    FsClose,
    /// M36 (1.13): std:process (runtime/src/vm/process.rs).
    ProcessArgs,
    ProcessExit,
    ProcessEnvGet,
    ProcessEnvSet,
    ProcessEnvRemove,
    ProcessEnvAll,
    ProcessCwd,
    ProcessPid,
    ProcessPlatform,
    ProcessRun,
    /// M41a (1.14): std:reflect (runtime/src/vm/reflect.rs).
    ReflectTypeOf,
    ReflectSignature,
    ReflectSchema,
    ReflectMethods,
    ReflectImplements,
    ReflectConstruct,
    ReflectConstructVariant,
    /// M41b (1.15)
    ReflectDecorators,
    /// M41c (1.16): the hook machinery (runtime/src/vm/reflect.rs).
    HooksHas,
    HooksAdopt,
    HooksSameFn,
    HooksSetType,
    HooksSetParam,
    HooksOf,
    HooksGetField,
    HooksSetField,
    /// M37 (1.17): std:bytes (runtime/src/vm/bytes.rs) and binary std:fs.
    BytesNew,
    BytesFromVector,
    BytesFromHex,
    BytesFromBase64,
    FsReadBytes,
    FsWriteBytes,
    FsAppendBytes,
    FsFileReadBytes,
    FsFileWriteBytes,
    /// M38 (1.18): std:socket (runtime/src/vm/socket.rs).
    SocketConnect,
    SocketListen,
    SocketAccept,
    SocketSend,
    SocketRecv,
    SocketShutdown,
    SocketClose,
    /// M39 (1.19): TLS on an open socket (std:http).
    SocketStartTls,
    /// M42 (1.20): TLS servers.
    SocketTlsServerConfig,
    SocketStartTlsServer,
    /// M44 (1.21): std:thread (runtime/src/vm/thread.rs).
    ThreadSpawn,
    ThreadSubmit,
    ThreadClose,
    ThreadJoin,
    ThreadPending,
    ThreadCurrent,
    ThreadCores,
    SemaphoreNew,
    SemaphoreAcquire,
    SemaphoreTryAcquire,
    SemaphoreRelease,
    SemaphoreAvailable,
    ChannelNew,
    ChannelSend,
    ChannelRecv,
    ChannelTryRecv,
    ChannelClose,
    ChannelLen,
    ChannelClosed,
}

/// Whether this VM implements a native of that name (any arity) -- for
/// `decode.rs`'s message about a file newer than this VM.
pub fn is_known_native(name: &str) -> bool {
    native_by_name(name).is_some()
}

fn native_by_name(name: &str) -> Option<(u64, NativeFn)> {
    match name {
        "io.print" => Some((1, NativeFn::IoPrint)),
        "io.write" => Some((1, NativeFn::IoWrite)),
        "io.input" => Some((0, NativeFn::IoInput)),
        "math.sin" => Some((1, NativeFn::MathSin)),
        "math.cos" => Some((1, NativeFn::MathCos)),
        "time.sleep_async" => Some((1, NativeFn::TimeSleepAsync)),
        "math.tan" => Some((1, NativeFn::MathTan)),
        "math.asin" => Some((1, NativeFn::MathAsin)),
        "math.acos" => Some((1, NativeFn::MathAcos)),
        "math.atan" => Some((1, NativeFn::MathAtan)),
        "math.atan2" => Some((2, NativeFn::MathAtan2)),
        "math.exp" => Some((1, NativeFn::MathExp)),
        "math.log" => Some((1, NativeFn::MathLog)),
        "math.log10" => Some((1, NativeFn::MathLog10)),
        "value.type_name" => Some((1, NativeFn::ValueTypeName)),
        "value.fields" => Some((1, NativeFn::ValueFields)),
        "value.variant" => Some((1, NativeFn::ValueVariant)),
        "string.chars" => Some((1, NativeFn::StringChars)),
        "string.code_point" => Some((1, NativeFn::StringCodePoint)),
        "string.from_code_point" => Some((1, NativeFn::StringFromCodePoint)),
        "random.seed" => Some((1, NativeFn::RandomSeed)),
        "random.fresh" => Some((0, NativeFn::RandomFresh)),
        "random.next" => Some((1, NativeFn::RandomNext)),
        "random.below" => Some((2, NativeFn::RandomBelow)),
        "regex.find" => Some((3, NativeFn::RegexFind)),
        "regex.find_all" => Some((2, NativeFn::RegexFindAll)),
        "io.read_line" => Some((1, NativeFn::IoReadLine)),
        "time.now_ms" => Some((0, NativeFn::TimeNowMs)),
        "time.monotonic_ms" => Some((0, NativeFn::TimeMonotonicMs)),
        "time.cancel" => Some((1, NativeFn::TimeCancel)),
        "promise.new" => Some((0, NativeFn::PromiseNew)),
        "promise.resolve" => Some((2, NativeFn::PromiseResolve)),
        "promise.fail" => Some((2, NativeFn::PromiseFail)),
        "fs.read_text" => Some((1, NativeFn::FsReadText)),
        "fs.write_text" => Some((2, NativeFn::FsWriteText)),
        "fs.append_text" => Some((2, NativeFn::FsAppendText)),
        "fs.info" => Some((1, NativeFn::FsInfo)),
        "fs.list_dir" => Some((1, NativeFn::FsListDir)),
        "fs.mkdir" => Some((2, NativeFn::FsMkdir)),
        "fs.remove" => Some((2, NativeFn::FsRemove)),
        "fs.rename" => Some((2, NativeFn::FsRename)),
        "fs.copy" => Some((2, NativeFn::FsCopy)),
        "fs.temp_dir" => Some((0, NativeFn::FsTempDir)),
        "fs.open" => Some((2, NativeFn::FsOpen)),
        "fs.read_line" => Some((1, NativeFn::FsReadLine)),
        "fs.read_all" => Some((1, NativeFn::FsReadAll)),
        "fs.write" => Some((2, NativeFn::FsWrite)),
        "fs.close" => Some((1, NativeFn::FsClose)),
        "process.args" => Some((0, NativeFn::ProcessArgs)),
        "process.exit" => Some((1, NativeFn::ProcessExit)),
        "process.env_get" => Some((1, NativeFn::ProcessEnvGet)),
        "process.env_set" => Some((2, NativeFn::ProcessEnvSet)),
        "process.env_remove" => Some((1, NativeFn::ProcessEnvRemove)),
        "process.env_all" => Some((0, NativeFn::ProcessEnvAll)),
        "process.cwd" => Some((0, NativeFn::ProcessCwd)),
        "process.pid" => Some((0, NativeFn::ProcessPid)),
        "process.platform" => Some((0, NativeFn::ProcessPlatform)),
        "process.run" => Some((5, NativeFn::ProcessRun)),
        "reflect.type_of" => Some((1, NativeFn::ReflectTypeOf)),
        "reflect.signature" => Some((1, NativeFn::ReflectSignature)),
        "reflect.schema" => Some((1, NativeFn::ReflectSchema)),
        "reflect.methods" => Some((1, NativeFn::ReflectMethods)),
        "reflect.implements" => Some((2, NativeFn::ReflectImplements)),
        "reflect.construct" => Some((2, NativeFn::ReflectConstruct)),
        "reflect.construct_variant" => Some((3, NativeFn::ReflectConstructVariant)),
        "reflect.decorators" => Some((3, NativeFn::ReflectDecorators)),
        "hooks.has" => Some((2, NativeFn::HooksHas)),
        "hooks.adopt" => Some((2, NativeFn::HooksAdopt)),
        "hooks.same_fn" => Some((2, NativeFn::HooksSameFn)),
        "hooks.set_type" => Some((2, NativeFn::HooksSetType)),
        "hooks.set_param" => Some((3, NativeFn::HooksSetParam)),
        "hooks.of" => Some((1, NativeFn::HooksOf)),
        "hooks.get_field" => Some((2, NativeFn::HooksGetField)),
        "hooks.set_field" => Some((3, NativeFn::HooksSetField)),
        "bytes.new" => Some((2, NativeFn::BytesNew)),
        "bytes.from_vector" => Some((1, NativeFn::BytesFromVector)),
        "bytes.from_hex" => Some((1, NativeFn::BytesFromHex)),
        "bytes.from_base64" => Some((1, NativeFn::BytesFromBase64)),
        "fs.read_bytes" => Some((1, NativeFn::FsReadBytes)),
        "fs.write_bytes" => Some((2, NativeFn::FsWriteBytes)),
        "fs.append_bytes" => Some((2, NativeFn::FsAppendBytes)),
        "fs.file_read_bytes" => Some((2, NativeFn::FsFileReadBytes)),
        "fs.file_write_bytes" => Some((2, NativeFn::FsFileWriteBytes)),
        "socket.connect" => Some((3, NativeFn::SocketConnect)),
        "socket.listen" => Some((3, NativeFn::SocketListen)),
        "socket.accept" => Some((2, NativeFn::SocketAccept)),
        "socket.send" => Some((2, NativeFn::SocketSend)),
        "socket.recv" => Some((3, NativeFn::SocketRecv)),
        "socket.shutdown" => Some((1, NativeFn::SocketShutdown)),
        "socket.close" => Some((1, NativeFn::SocketClose)),
        "socket.start_tls" => Some((3, NativeFn::SocketStartTls)),
        "socket.tls_server_config" => Some((2, NativeFn::SocketTlsServerConfig)),
        "socket.start_tls_server" => Some((3, NativeFn::SocketStartTlsServer)),
        "thread.spawn" => Some((3, NativeFn::ThreadSpawn)),
        "thread.submit" => Some((3, NativeFn::ThreadSubmit)),
        "thread.close" => Some((2, NativeFn::ThreadClose)),
        "thread.join" => Some((1, NativeFn::ThreadJoin)),
        "thread.pending" => Some((1, NativeFn::ThreadPending)),
        "thread.current" => Some((0, NativeFn::ThreadCurrent)),
        "thread.cores" => Some((0, NativeFn::ThreadCores)),
        "thread.semaphore_new" => Some((1, NativeFn::SemaphoreNew)),
        "thread.semaphore_acquire" => Some((1, NativeFn::SemaphoreAcquire)),
        "thread.semaphore_try_acquire" => Some((1, NativeFn::SemaphoreTryAcquire)),
        "thread.semaphore_release" => Some((1, NativeFn::SemaphoreRelease)),
        "thread.semaphore_available" => Some((1, NativeFn::SemaphoreAvailable)),
        "thread.channel_new" => Some((1, NativeFn::ChannelNew)),
        "thread.channel_send" => Some((2, NativeFn::ChannelSend)),
        "thread.channel_recv" => Some((1, NativeFn::ChannelRecv)),
        "thread.channel_try_recv" => Some((1, NativeFn::ChannelTryRecv)),
        "thread.channel_close" => Some((1, NativeFn::ChannelClose)),
        "thread.channel_len" => Some((1, NativeFn::ChannelLen)),
        "thread.channel_closed" => Some((1, NativeFn::ChannelClosed)),
        _ => None,
    }
}

pub enum TypeKind {
    Struct(Vec<Rc<str>>),
    /// `(variant name, field names)`, declaration order.
    Enum(Vec<(Rc<str>, Vec<Rc<str>>)>),
}

pub struct TypeInfo {
    pub name: Rc<str>,
    pub kind: TypeKind,
}

impl TypeInfo {
    pub fn field_names(&self) -> Option<&[Rc<str>]> {
        match &self.kind {
            TypeKind::Struct(f) => Some(f),
            TypeKind::Enum(_) => None,
        }
    }
    pub fn variants(&self) -> Option<&[(Rc<str>, Vec<Rc<str>>)]> {
        match &self.kind {
            TypeKind::Enum(v) => Some(v),
            TypeKind::Struct(_) => None,
        }
    }
}

pub struct DebugIndex {
    /// Ascending pcs, parallel to `runs`.
    pub pcs: Vec<usize>,
    pub runs: Vec<(usize, u64, u64)>, // (file_idx, line, col)
    pub file_paths: Vec<Rc<str>>,
}

pub enum LinkedInstr {
    Halt,
    Move { src: Addr, dest: Addr },
    Loadk { value: Value, dest: Addr },
    Jmp { target: usize },
    Jmpf { cond: Addr, target: usize },
    Jmpset { param: Addr, target: usize },
    BinOp { op: BinOp, a: Addr, b: Addr, dest: Addr },
    Neg { a: Addr, dest: Addr },
    Not { a: Addr, dest: Addr },
    Closure { func: Rc<FunctionInfo>, dest: Addr },
    Call { callee: Addr, args: Vec<Addr> },
    CallKw { callee: Addr, args: Vec<Addr>, kwnames: Vec<Rc<str>> },
    Ret { value: Addr },
    Retval { dest: Addr },
    CallMethod { recv: Addr, name: Rc<str>, args: Vec<Addr>, trait_: Option<Rc<str>> },
    CallMethodKw { recv: Addr, name: Rc<str>, args: Vec<Addr>, kwnames: Vec<Rc<str>>, trait_: Option<Rc<str>> },
    CallSpread { callee: Addr, args: Addr, kwargs: Addr },
    CallMethodSpread { recv: Addr, name: Rc<str>, args: Addr, kwargs: Addr, trait_: Option<Rc<str>> },
    Spread { target: Addr, source: Addr, keyword: bool },
    LoadType { value: Value, dest: Addr },
    /// M41b (1.15): `kind` 0 function `a`, 1 parameter `b` of function `a`,
    /// 2 type `a`, 3 field `b` of struct `a`, 4 variant `b` of enum `a`
    Decorate { kind: u64, a: usize, b: usize, values: Vec<Addr> },
    /// M41c (1.16): `dest` <- the WrapParam hooks of parameter `param` of function `func`, or `none`
    ParamHooks { func: usize, param: usize, dest: Addr },
    Defmethod { closure: Addr, type_name: Rc<str>, trait_: Option<Rc<str>>, name: Rc<str>, is_method: bool },
    Detach { callee: Addr, args: Vec<Addr>, dest: Addr },
    DetachKw { callee: Addr, args: Vec<Addr>, kwnames: Vec<Rc<str>>, dest: Addr },
    DetachMethod { recv: Addr, name: Rc<str>, args: Vec<Addr>, trait_: Option<Rc<str>>, dest: Addr },
    DetachMethodKw {
        recv: Addr,
        name: Rc<str>,
        args: Vec<Addr>,
        kwnames: Vec<Rc<str>>,
        trait_: Option<Rc<str>>,
        dest: Addr,
    },
    Await { promise: Addr, dest: Addr },
    Struct { type_idx: usize, values: Vec<Addr>, dest: Addr },
    Enum { type_idx: usize, variant: usize, values: Vec<Addr>, dest: Addr },
    GetField { obj: Addr, field: Rc<str>, dest: Addr },
    SetField { obj: Addr, field: Rc<str>, src: Addr },
    MatchStruct { value: Addr, type_idx: usize, dest: Addr },
    MatchEnum { value: Addr, type_idx: usize, variant: usize, dest: Addr },
    MatchFail,
    MatchRange { value: Addr, lo: Option<Addr>, hi: Option<Addr>, inclusive: bool, dest: Addr },
    Vector { items: Vec<Addr>, dest: Addr },
    Map { pairs: Vec<Addr>, dest: Addr },
    DeferPush,
    DeferAdd { closure: Addr },
    DeferPeek { dest: Addr },
    DeferPop { dest: Addr },
    DeferScopePop,
    /// M25 (1.4)
    MatchType { value: Addr, type_idx: usize, dest: Addr },
    /// M25 (1.4)
    DeferDepth { dest: Addr },
    /// M25 (1.4)
    DeferAbove { depth: Addr, dest: Addr },
    /// M25 (1.4)
    Throw { value: Addr },
    Native { native: NativeFn, args: Vec<Addr>, dest: Option<Addr> },
    /// M44 (1.21, docs/contracts/M44_threads.md #6.4): shared variables
    /// (`name`, the source name, is for messages; reads and sets have none).
    #[allow(dead_code)]
    SharedGet { index: u64, name: Rc<str>, locked: bool, dest: Addr },
    #[allow(dead_code)]
    SharedSet { index: u64, name: Rc<str>, src: Addr },
    SharedLock { index: u64, name: Rc<str>, dest: Addr },
    SharedUnlock { index: u64, name: Rc<str>, mark: bool },
}

pub struct LinkedProgram {
    /// M41a: what `std:reflect` reads -- the constants, strings, META and the
    /// number of built-in types (user types follow them in `types`).
    pub constants: Vec<Value>,
    pub strings: Vec<Rc<str>>,
    pub meta: Option<Meta>,
    pub builtin_type_count: usize,
    pub types: Vec<TypeInfo>,
    pub functions: Vec<Rc<FunctionInfo>>,
    pub code: Vec<LinkedInstr>,
    pub debug: Option<DebugIndex>,
    /// M25 (docs/MAHC_FORMAT.md #4.8): copied straight through -- addresses
    /// are already final instruction indices, no relinking needed.
    pub handlers: Vec<HandlerEntry>,
}

fn convert_const(c: &Const, strings: &[String]) -> decode::FResult<Value> {
    Ok(match c {
        Const::None => Value::None,
        Const::False => Value::Bool(false),
        Const::True => Value::Bool(true),
        Const::Int(groups) => Value::Number(Decimal::from_zigzag_groups(groups)),
        Const::Dec(idx) => {
            let text = &strings[*idx];
            match Decimal::parse(text) {
                Some(d) => Value::Number(d),
                None => return Err(decode::FormatError(format!("invalid decimal constant '{text}'"))),
            }
        }
        Const::Str(idx) => Value::Str(Rc::from(strings[*idx].as_str())),
    })
}

fn build_types(type_decls: &[TypeDecl], interned: &[Rc<str>], minor: u16) -> Vec<TypeInfo> {
    let promise_variants = if minor >= 4 {
        vec![
            (Rc::from("Pending"), vec![]),
            (Rc::from("Settled"), vec![Rc::from("value")]),
            (Rc::from("Failed"), vec![Rc::from("error")]),
        ]
    } else {
        vec![(Rc::from("Pending"), vec![]), (Rc::from("Settled"), vec![Rc::from("value")])]
    };
    let mut infos = vec![
        TypeInfo {
            name: Rc::from("Option"),
            kind: TypeKind::Enum(vec![(Rc::from("none"), vec![]), (Rc::from("some"), vec![Rc::from("value")])]),
        },
        TypeInfo { name: Rc::from("Promise"), kind: TypeKind::Enum(promise_variants) },
    ];
    if minor >= 4 {
        // M25 (docs/MAHC_FORMAT.md #4.1): the built-in `RuntimeError` enum,
        // index 2 -- every variant has one field, `message`.
        infos.push(TypeInfo {
            name: Rc::from("RuntimeError"),
            kind: TypeKind::Enum(
                RUNTIME_ERROR_VARIANTS.iter().map(|&v| (Rc::from(v), vec![Rc::from("message")])).collect(),
            ),
        });
    }
    for t in type_decls {
        let name = interned[t.name].clone();
        let kind = match &t.body {
            TypeBody::Struct(fields) => TypeKind::Struct(fields.iter().map(|&i| interned[i].clone()).collect()),
            TypeBody::Enum(variants) => TypeKind::Enum(
                variants
                    .iter()
                    .map(|(vn, vf)| (interned[*vn].clone(), vf.iter().map(|&i| interned[i].clone()).collect()))
                    .collect(),
            ),
        };
        infos.push(TypeInfo { name, kind });
    }
    infos
}

fn validate_natives(native_refs: &[NativeRef], strings: &[String]) -> decode::FResult<Vec<NativeFn>> {
    let mut out = Vec::with_capacity(native_refs.len());
    for r in native_refs {
        let name = &strings[r.name];
        match native_by_name(name) {
            Some((arity, nf)) if arity == r.arity => out.push(nf),
            _ => {
                return Err(decode::FormatError(format!(
                    "this VM does not support native '{name}' (arity {})",
                    r.arity
                )))
            }
        }
    }
    Ok(out)
}

fn build_functions(decls: &[FunctionDecl], interned: &[Rc<str>]) -> Vec<Rc<FunctionInfo>> {
    decls
        .iter()
        .enumerate()
        .map(|(index, fd)| {
            let name = fd.name.map(|i| interned[i].clone());
            let params = fd
                .params
                .as_ref()
                .map(|ps| ps.iter().map(|&(i, has_default)| (interned[i].clone(), has_default)).collect());
            Rc::new(FunctionInfo {
                index,
                entry: fd.entry as usize,
                slot_count: fd.slot_count as usize,
                param_count: fd.param_count as usize,
                name,
                params,
                rest: fd.rest,
            })
        })
        .collect()
}

#[allow(clippy::too_many_arguments)]
fn link_instr(
    instr: &RawInstr,
    interned: &[Rc<str>],
    constants: &[Value],
    functions: &[Rc<FunctionInfo>],
    natives: &[NativeFn],
    types: &[TypeInfo],
) -> LinkedInstr {
    let s = |i: usize| interned[i].clone();
    let s_opt = |i: Option<usize>| i.map(s);
    let s_list = |v: &[usize]| v.iter().map(|&i| s(i)).collect::<Vec<_>>();
    match instr {
        RawInstr::Halt => LinkedInstr::Halt,
        RawInstr::Move { src, dest } => LinkedInstr::Move { src: *src, dest: *dest },
        RawInstr::Loadk { k, dest } => LinkedInstr::Loadk { value: constants[*k].clone(), dest: *dest },
        RawInstr::Jmp { target } => LinkedInstr::Jmp { target: *target },
        RawInstr::Jmpf { cond, target } => LinkedInstr::Jmpf { cond: *cond, target: *target },
        RawInstr::Jmpset { param, target } => LinkedInstr::Jmpset { param: *param, target: *target },
        RawInstr::BinOp { op, a, b, dest } => LinkedInstr::BinOp { op: op.clone(), a: *a, b: *b, dest: *dest },
        RawInstr::Neg { a, dest } => LinkedInstr::Neg { a: *a, dest: *dest },
        RawInstr::Not { a, dest } => LinkedInstr::Not { a: *a, dest: *dest },
        RawInstr::Closure { func, dest } => LinkedInstr::Closure { func: functions[*func].clone(), dest: *dest },
        RawInstr::Call { callee, args } => LinkedInstr::Call { callee: *callee, args: args.clone() },
        RawInstr::CallKw { callee, args, kwnames } => {
            LinkedInstr::CallKw { callee: *callee, args: args.clone(), kwnames: s_list(kwnames) }
        }
        RawInstr::Ret { value } => LinkedInstr::Ret { value: *value },
        RawInstr::Retval { dest } => LinkedInstr::Retval { dest: *dest },
        RawInstr::CallMethod { recv, name, args, trait_ } => {
            LinkedInstr::CallMethod { recv: *recv, name: s(*name), args: args.clone(), trait_: s_opt(*trait_) }
        }
        RawInstr::CallMethodKw { recv, name, args, kwnames, trait_ } => LinkedInstr::CallMethodKw {
            recv: *recv,
            name: s(*name),
            args: args.clone(),
            kwnames: s_list(kwnames),
            trait_: s_opt(*trait_),
        },
        RawInstr::CallSpread { callee, args, kwargs } => {
            LinkedInstr::CallSpread { callee: *callee, args: *args, kwargs: *kwargs }
        }
        RawInstr::CallMethodSpread { recv, name, args, kwargs, trait_ } => LinkedInstr::CallMethodSpread {
            recv: *recv,
            name: s(*name),
            args: *args,
            kwargs: *kwargs,
            trait_: s_opt(*trait_),
        },
        RawInstr::Spread { target, source, keyword } => {
            LinkedInstr::Spread { target: *target, source: *source, keyword: *keyword }
        }
        RawInstr::LoadType { kind, index, dest } => {
            let name: Rc<str> = if *kind == 0 { types[*index].name.clone() } else { Rc::from(PRIMITIVE_TYPE_NAMES[*index]) };
            LinkedInstr::LoadType {
                value: Value::Type(Rc::new(TypeData { kind: *kind, index: *index, name })),
                dest: *dest,
            }
        }
        RawInstr::ParamHooks { func, param, dest } => {
            LinkedInstr::ParamHooks { func: *func, param: *param, dest: *dest }
        }
        RawInstr::Decorate { kind, a, b, values } => {
            LinkedInstr::Decorate { kind: *kind, a: *a, b: *b, values: values.clone() }
        }
        RawInstr::Defmethod { closure, type_name, trait_, name, is_method } => LinkedInstr::Defmethod {
            closure: *closure,
            type_name: s(*type_name),
            trait_: s_opt(*trait_),
            name: s(*name),
            is_method: *is_method,
        },
        RawInstr::Detach { callee, args, dest } => {
            LinkedInstr::Detach { callee: *callee, args: args.clone(), dest: *dest }
        }
        RawInstr::DetachKw { callee, args, kwnames, dest } => {
            LinkedInstr::DetachKw { callee: *callee, args: args.clone(), kwnames: s_list(kwnames), dest: *dest }
        }
        RawInstr::DetachMethod { recv, name, args, trait_, dest } => LinkedInstr::DetachMethod {
            recv: *recv,
            name: s(*name),
            args: args.clone(),
            trait_: s_opt(*trait_),
            dest: *dest,
        },
        RawInstr::DetachMethodKw { recv, name, args, kwnames, trait_, dest } => LinkedInstr::DetachMethodKw {
            recv: *recv,
            name: s(*name),
            args: args.clone(),
            kwnames: s_list(kwnames),
            trait_: s_opt(*trait_),
            dest: *dest,
        },
        RawInstr::Await { promise, dest } => LinkedInstr::Await { promise: *promise, dest: *dest },
        RawInstr::Struct { type_idx, values, dest } => {
            LinkedInstr::Struct { type_idx: *type_idx, values: values.clone(), dest: *dest }
        }
        RawInstr::Enum { type_idx, variant, values, dest } => {
            LinkedInstr::Enum { type_idx: *type_idx, variant: *variant, values: values.clone(), dest: *dest }
        }
        RawInstr::GetField { obj, field, dest } => LinkedInstr::GetField { obj: *obj, field: s(*field), dest: *dest },
        RawInstr::SetField { obj, field, src } => LinkedInstr::SetField { obj: *obj, field: s(*field), src: *src },
        RawInstr::MatchStruct { value, type_idx, dest } => {
            LinkedInstr::MatchStruct { value: *value, type_idx: *type_idx, dest: *dest }
        }
        RawInstr::MatchEnum { value, type_idx, variant, dest } => {
            LinkedInstr::MatchEnum { value: *value, type_idx: *type_idx, variant: *variant, dest: *dest }
        }
        RawInstr::MatchFail => LinkedInstr::MatchFail,
        RawInstr::MatchRange { value, lo, hi, inclusive, dest } => {
            LinkedInstr::MatchRange { value: *value, lo: *lo, hi: *hi, inclusive: *inclusive, dest: *dest }
        }
        RawInstr::Vector { items, dest } => LinkedInstr::Vector { items: items.clone(), dest: *dest },
        RawInstr::Map { pairs, dest } => LinkedInstr::Map { pairs: pairs.clone(), dest: *dest },
        RawInstr::DeferPush => LinkedInstr::DeferPush,
        RawInstr::DeferAdd { closure } => LinkedInstr::DeferAdd { closure: *closure },
        RawInstr::DeferPeek { dest } => LinkedInstr::DeferPeek { dest: *dest },
        RawInstr::DeferPop { dest } => LinkedInstr::DeferPop { dest: *dest },
        RawInstr::DeferScopePop => LinkedInstr::DeferScopePop,
        RawInstr::MatchType { value, type_idx, dest } => {
            LinkedInstr::MatchType { value: *value, type_idx: *type_idx, dest: *dest }
        }
        RawInstr::DeferDepth { dest } => LinkedInstr::DeferDepth { dest: *dest },
        RawInstr::DeferAbove { depth, dest } => LinkedInstr::DeferAbove { depth: *depth, dest: *dest },
        RawInstr::Throw { value } => LinkedInstr::Throw { value: *value },
        RawInstr::Native { native, args, dest } => {
            LinkedInstr::Native { native: natives[*native], args: args.clone(), dest: *dest }
        }
        RawInstr::SharedGet { index, name, mode, dest } => {
            LinkedInstr::SharedGet { index: *index, name: s(*name), locked: *mode == 1, dest: *dest }
        }
        RawInstr::SharedSet { index, name, src } => LinkedInstr::SharedSet { index: *index, name: s(*name), src: *src },
        RawInstr::SharedLock { index, name, dest } => {
            LinkedInstr::SharedLock { index: *index, name: s(*name), dest: *dest }
        }
        RawInstr::SharedUnlock { index, name, mode } => {
            LinkedInstr::SharedUnlock { index: *index, name: s(*name), mark: *mode == 1 }
        }
    }
}

/// M44 (docs/contracts/M44_threads.md #7.1): linking refuses a
/// `sharedget`/`sharedunlock` mode other than 0 or 1 -- a runtime error
/// (`RuntimeError.Internal`), like the Python VM's `_link`.
pub fn check_modes(program: &Program) -> Result<(), super::error::RuntimeError> {
    for instr in &program.code {
        let (mode, op) = match instr {
            RawInstr::SharedGet { mode, .. } => (*mode, "sharedget"),
            RawInstr::SharedUnlock { mode, .. } => (*mode, "sharedunlock"),
            _ => continue,
        };
        if mode > 1 {
            return Err(super::error::RuntimeError::new(format!("bad mode {mode} for '{op}'")));
        }
    }
    Ok(())
}

pub fn link(program: &Program) -> decode::FResult<LinkedProgram> {
    let interned: Vec<Rc<str>> = program.strings.iter().map(|s| Rc::from(s.as_str())).collect();
    let constants: Vec<Value> =
        program.constants.iter().map(|c| convert_const(c, &program.strings)).collect::<decode::FResult<_>>()?;
    let types = build_types(&program.types, &interned, program.minor);
    let natives = validate_natives(&program.natives, &program.strings)?;
    let functions = build_functions(&program.functions, &interned);
    let code: Vec<LinkedInstr> =
        program.code.iter().map(|i| link_instr(i, &interned, &constants, &functions, &natives, &types)).collect();
    let debug = program.debug.as_ref().map(|d| DebugIndex {
        pcs: d.runs.iter().map(|r| r.0).collect(),
        runs: d.runs.iter().map(|r| (r.1, r.2, r.3)).collect(),
        file_paths: d.files.iter().map(|&i| interned[i].clone()).collect(),
    });
    let builtin_type_count = types.len() - program.types.len();
    Ok(LinkedProgram {
        constants,
        strings: interned,
        meta: program.meta.clone(),
        builtin_type_count,
        types,
        functions,
        code,
        debug,
        handlers: program.handlers.clone(),
    })
}
