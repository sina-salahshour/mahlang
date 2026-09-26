//! `Program` (raw indices, `crate::decode`) -> `LinkedProgram` (resolved
//! strings/constants/types/functions/natives) -- a Rust port of
//! `code_interpreter.py`'s `_link`. Every string used anywhere in the
//! program is interned exactly once into an `Rc<str>`, so every later
//! lookup (a `getfield`'s field name, a method name, a type name) is a
//! cheap refcount bump, never a fresh allocation -- see the spec's
//! performance section.

use std::rc::Rc;

use crate::decimal::Decimal;
use crate::decode::{self, Addr, BinOp, Const, FunctionDecl, NativeRef, Program, RawInstr, TypeBody, TypeDecl};

use super::value::{FunctionInfo, Value};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum NativeFn {
    IoPrint,
    IoWrite,
    IoInput,
    MathSin,
    MathCos,
    TimeSleepAsync,
}

fn native_by_name(name: &str) -> Option<(u64, NativeFn)> {
    match name {
        "io.print" => Some((1, NativeFn::IoPrint)),
        "io.write" => Some((1, NativeFn::IoWrite)),
        "io.input" => Some((0, NativeFn::IoInput)),
        "math.sin" => Some((1, NativeFn::MathSin)),
        "math.cos" => Some((1, NativeFn::MathCos)),
        "time.sleep_async" => Some((1, NativeFn::TimeSleepAsync)),
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
    Native { native: NativeFn, args: Vec<Addr>, dest: Option<Addr> },
}

pub struct LinkedProgram {
    pub types: Vec<TypeInfo>,
    pub functions: Vec<Rc<FunctionInfo>>,
    pub code: Vec<LinkedInstr>,
    pub debug: Option<DebugIndex>,
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

fn build_types(type_decls: &[TypeDecl], interned: &[Rc<str>]) -> Vec<TypeInfo> {
    let mut infos = vec![
        TypeInfo {
            name: Rc::from("Option"),
            kind: TypeKind::Enum(vec![(Rc::from("none"), vec![]), (Rc::from("some"), vec![Rc::from("value")])]),
        },
        TypeInfo {
            name: Rc::from("Promise"),
            kind: TypeKind::Enum(vec![(Rc::from("Pending"), vec![]), (Rc::from("Settled"), vec![Rc::from("value")])]),
        },
    ];
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
        .map(|fd| {
            let name = fd.name.map(|i| interned[i].clone());
            let params = fd
                .params
                .as_ref()
                .map(|ps| ps.iter().map(|&(i, has_default)| (interned[i].clone(), has_default)).collect());
            Rc::new(FunctionInfo {
                entry: fd.entry as usize,
                slot_count: fd.slot_count as usize,
                param_count: fd.param_count as usize,
                name,
                params,
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
        RawInstr::Native { native, args, dest } => {
            LinkedInstr::Native { native: natives[*native], args: args.clone(), dest: *dest }
        }
    }
}

pub fn link(program: &Program) -> decode::FResult<LinkedProgram> {
    let interned: Vec<Rc<str>> = program.strings.iter().map(|s| Rc::from(s.as_str())).collect();
    let constants: Vec<Value> =
        program.constants.iter().map(|c| convert_const(c, &program.strings)).collect::<decode::FResult<_>>()?;
    let types = build_types(&program.types, &interned);
    let natives = validate_natives(&program.natives, &program.strings)?;
    let functions = build_functions(&program.functions, &interned);
    let code: Vec<LinkedInstr> =
        program.code.iter().map(|i| link_instr(i, &interned, &constants, &functions, &natives)).collect();
    let debug = program.debug.as_ref().map(|d| DebugIndex {
        pcs: d.runs.iter().map(|r| r.0).collect(),
        runs: d.runs.iter().map(|r| (r.1, r.2, r.3)).collect(),
        file_paths: d.files.iter().map(|&i| interned[i].clone()).collect(),
    });
    Ok(LinkedProgram { types, functions, code, debug })
}
