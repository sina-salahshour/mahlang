//! `.mahc` bytes -> `Program`, with full load-time structural validation --
//! docs/MAHC_FORMAT.md #3/#4 ("fail fast"). A faithful port of
//! `mah/bytecode/decode.py` (+ `format.py`, `leb128.py`, `program.py`):
//! every validation and its exact message text is reproduced so `error:
//! invalid .mahc file: <message>` matches the Python VM byte for byte.
//!
//! Native *names* are deliberately NOT checked here (see decode.py's own
//! docstring) -- that's `crate::vm`'s job at link time.

use std::fmt;

// ---------------------------------------------------------------------------
// Format constants (mirrors mah/bytecode/format.py)
// ---------------------------------------------------------------------------

pub const MAGIC: &[u8; 4] = b"MAHC";
pub const MAJOR: u16 = 1;
pub const MINOR: u16 = 3;

const SEC_STRINGS: u8 = 0x01;
const SEC_CONSTANTS: u8 = 0x02;
const SEC_TYPES: u8 = 0x03;
const SEC_NATIVES: u8 = 0x04;
const SEC_FUNCTIONS: u8 = 0x05;
const SEC_CODE: u8 = 0x06;
const SEC_PARAMS: u8 = 0x07;
const SEC_DEBUG: u8 = 0x80;

const REQUIRED_SECTIONS: &[u8] = &[
    SEC_STRINGS,
    SEC_CONSTANTS,
    SEC_TYPES,
    SEC_NATIVES,
    SEC_FUNCTIONS,
    SEC_CODE,
];
const REQUIRED_SECTIONS_V1: &[u8] = &[
    SEC_STRINGS,
    SEC_CONSTANTS,
    SEC_TYPES,
    SEC_NATIVES,
    SEC_FUNCTIONS,
    SEC_CODE,
    SEC_PARAMS,
];

const TAG_NONE: u8 = 0;
const TAG_FALSE: u8 = 1;
const TAG_TRUE: u8 = 2;
const TAG_INT: u8 = 3;
const TAG_DEC: u8 = 4;
const TAG_STR: u8 = 5;

// ---------------------------------------------------------------------------
// Error type
// ---------------------------------------------------------------------------

/// A malformed `.mahc` file (docs/MAHC_FORMAT.md #3/#4), or (raised by
/// `crate::vm`'s linker, reusing this same type) a declared native this VM
/// doesn't implement -- both are reported by the CLI as
/// `error: invalid .mahc file: <message>`, exit code 2.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FormatError(pub String);

impl fmt::Display for FormatError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.0)
    }
}

impl std::error::Error for FormatError {}

pub type FResult<T> = Result<T, FormatError>;

fn err<T>(msg: impl Into<String>) -> FResult<T> {
    Err(FormatError(msg.into()))
}

/// Python's `repr(bytes)` -- used in "bad magic: expected b'MAHC', got
/// b'...'". Printable ASCII as-is except backslash and the quote char,
/// `\t\n\r` get their short escapes, everything else is `\xNN`. The quote
/// char is `'` unless the bytes contain `'` and not `"`.
pub fn python_bytes_repr(bytes: &[u8]) -> String {
    let has_single = bytes.contains(&b'\'');
    let has_double = bytes.contains(&b'"');
    let quote = if has_single && !has_double { b'"' } else { b'\'' };
    let mut out = String::with_capacity(bytes.len() + 2);
    out.push('b');
    out.push(quote as char);
    for &b in bytes {
        match b {
            b'\\' => out.push_str("\\\\"),
            b'\t' => out.push_str("\\t"),
            b'\n' => out.push_str("\\n"),
            b'\r' => out.push_str("\\r"),
            c if c == quote => {
                out.push('\\');
                out.push(c as char);
            }
            0x20..=0x7e => out.push(b as char),
            _ => out.push_str(&format!("\\x{:02x}", b)),
        }
    }
    out.push(quote as char);
    out
}

// ---------------------------------------------------------------------------
// Program data model (mirrors mah/bytecode/program.py) -- raw indices, no
// resolution to strings/runtime values. `crate::vm::link` does that once.
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq)]
pub enum Const {
    None,
    False,
    True,
    /// Raw 7-bit LEB128 groups of the zigzag-encoded integer, low group
    /// first -- arbitrary size, fed to `Decimal::from_zigzag_groups`.
    Int(Vec<u8>),
    /// String-table index of the decimal text (`-?[0-9]+\.[0-9]+`).
    Dec(usize),
    /// String-table index.
    Str(usize),
}

#[derive(Debug, Clone, PartialEq)]
pub enum TypeBody {
    /// Field name string indices, declaration order.
    Struct(Vec<usize>),
    /// (variant name string index, field name string indices), declaration order.
    Enum(Vec<(usize, Vec<usize>)>),
}

#[derive(Debug, Clone, PartialEq)]
pub struct TypeDecl {
    pub name: usize,
    pub body: TypeBody,
}

#[derive(Debug, Clone, PartialEq)]
pub struct NativeRef {
    pub name: usize,
    pub arity: u64,
}

#[derive(Debug, Clone, PartialEq)]
pub struct FunctionDecl {
    pub entry: u64,
    pub slot_count: u64,
    pub param_count: u64,
    pub name: Option<usize>,
    /// M16 (1.1+): parallel to param slots 0..param_count-1 -- (name string
    /// index, has_default). `None` for a 1.0 file (no PARAMS section).
    pub params: Option<Vec<(usize, bool)>>,
}

/// A frame-slot address: `(depth, slot)` -- walk `static_parent` `depth`
/// times from the current frame, then index `slot`.
pub type Addr = (u64, u64);

#[derive(Debug, Clone, PartialEq)]
pub enum BinOp {
    Add,
    Sub,
    Mul,
    Div,
    Idiv,
    Mod,
    Pow,
    Eq,
    Neq,
    Lt,
    Gt,
    Le,
    Ge,
    And,
    Or,
}

#[derive(Debug, Clone, PartialEq)]
pub enum RawInstr {
    Halt,
    Move { src: Addr, dest: Addr },
    Loadk { k: usize, dest: Addr },
    Jmp { target: usize },
    Jmpf { cond: Addr, target: usize },
    Jmpset { param: Addr, target: usize },
    BinOp { op: BinOp, a: Addr, b: Addr, dest: Addr },
    Neg { a: Addr, dest: Addr },
    Not { a: Addr, dest: Addr },
    Closure { func: usize, dest: Addr },
    Call { callee: Addr, args: Vec<Addr> },
    CallKw { callee: Addr, args: Vec<Addr>, kwnames: Vec<usize> },
    Ret { value: Addr },
    Retval { dest: Addr },
    CallMethod { recv: Addr, name: usize, args: Vec<Addr>, trait_: Option<usize> },
    CallMethodKw { recv: Addr, name: usize, args: Vec<Addr>, kwnames: Vec<usize>, trait_: Option<usize> },
    Defmethod { closure: Addr, type_name: usize, trait_: Option<usize>, name: usize, is_method: bool },
    Detach { callee: Addr, args: Vec<Addr>, dest: Addr },
    DetachKw { callee: Addr, args: Vec<Addr>, kwnames: Vec<usize>, dest: Addr },
    DetachMethod { recv: Addr, name: usize, args: Vec<Addr>, trait_: Option<usize>, dest: Addr },
    DetachMethodKw {
        recv: Addr,
        name: usize,
        args: Vec<Addr>,
        kwnames: Vec<usize>,
        trait_: Option<usize>,
        dest: Addr,
    },
    Await { promise: Addr, dest: Addr },
    Struct { type_idx: usize, values: Vec<Addr>, dest: Addr },
    Enum { type_idx: usize, variant: usize, values: Vec<Addr>, dest: Addr },
    GetField { obj: Addr, field: usize, dest: Addr },
    SetField { obj: Addr, field: usize, src: Addr },
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
    Native { native: usize, args: Vec<Addr>, dest: Option<Addr> },
}

#[derive(Debug, Clone, PartialEq)]
pub struct DebugInfo {
    /// String-table indices; file 0 = entry file.
    pub files: Vec<usize>,
    /// Absolute `(pc, file_idx, line, col)`, ascending by `pc`.
    pub runs: Vec<(usize, usize, u64, u64)>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Program {
    pub strings: Vec<String>,
    pub constants: Vec<Const>,
    pub types: Vec<TypeDecl>,
    pub natives: Vec<NativeRef>,
    pub functions: Vec<FunctionDecl>,
    pub code: Vec<RawInstr>,
    pub debug: Option<DebugInfo>,
    pub minor: u16,
}

// ---------------------------------------------------------------------------
// Byte reader
// ---------------------------------------------------------------------------

struct Reader<'a> {
    data: &'a [u8],
    pos: usize,
}

impl<'a> Reader<'a> {
    fn new(data: &'a [u8]) -> Self {
        Reader { data, pos: 0 }
    }

    fn remaining(&self) -> usize {
        self.data.len() - self.pos
    }

    fn bytes(&mut self, n: usize) -> FResult<&'a [u8]> {
        if self.pos + n > self.data.len() {
            return err("truncated file: not enough bytes remaining");
        }
        let out = &self.data[self.pos..self.pos + n];
        self.pos += n;
        Ok(out)
    }

    fn u8(&mut self) -> FResult<u8> {
        Ok(self.bytes(1)?[0])
    }

    fn u16(&mut self) -> FResult<u16> {
        let b = self.bytes(2)?;
        Ok(u16::from_le_bytes([b[0], b[1]]))
    }

    /// Reads a `varuint`, saturating at `u64::MAX` if the encoded value
    /// (arbitrary size in the format) doesn't fit. Any legitimate
    /// index/count is far smaller than that, so a saturated value simply
    /// fails the very next range check -- see decode.rs's module docs.
    fn varuint(&mut self) -> FResult<u64> {
        let mut result: u128 = 0;
        let mut shift: u32 = 0;
        loop {
            if self.pos >= self.data.len() {
                return err("truncated file: expected a varuint, ran out of bytes");
            }
            let byte = self.data[self.pos];
            self.pos += 1;
            if shift < 128 {
                result |= (byte as u128 & 0x7F) << shift;
            }
            if byte & 0x80 == 0 {
                return Ok(if result > u64::MAX as u128 { u64::MAX } else { result as u64 });
            }
            shift = shift.saturating_add(7);
        }
    }

    /// Raw 7-bit LEB128 groups of a varuint (low group first), without
    /// interpreting the value -- used only for TAG_INT constants, whose
    /// magnitude may exceed 64 bits (see `Decimal::from_zigzag_groups`).
    fn raw_varuint_groups(&mut self) -> FResult<Vec<u8>> {
        let mut groups = Vec::new();
        loop {
            if self.pos >= self.data.len() {
                return err("truncated file: expected a varuint, ran out of bytes");
            }
            let byte = self.data[self.pos];
            self.pos += 1;
            groups.push(byte & 0x7F);
            if byte & 0x80 == 0 {
                return Ok(groups);
            }
        }
    }
}

fn check_consumed(r: &Reader, section_name: &str) -> FResult<()> {
    if r.remaining() != 0 {
        return err(format!("{section_name} section payload not fully consumed (trailing garbage)"));
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// Sections
// ---------------------------------------------------------------------------

struct Sections {
    payloads: std::collections::HashMap<u8, Vec<u8>>,
    debug_payload: Option<Vec<u8>>,
}

fn read_sections(r: &mut Reader, minor: u16) -> FResult<Sections> {
    let required: &[u8] = if minor >= 1 { REQUIRED_SECTIONS_V1 } else { REQUIRED_SECTIONS };
    let mut payloads = std::collections::HashMap::new();
    let mut debug_payload: Option<Vec<u8>> = None;
    let mut expect_idx = 0usize;
    while r.remaining() > 0 {
        let sec_id = r.u8()?;
        let length = r.varuint()?;
        if length > r.remaining() as u64 {
            return err("truncated file: not enough bytes remaining");
        }
        let payload = r.bytes(length as usize)?.to_vec();
        if expect_idx < required.len() {
            let expected = required[expect_idx];
            if sec_id != expected {
                if required.contains(&sec_id) {
                    return err(format!(
                        "required sections out of order or duplicated: expected section 0x{expected:02x}, got 0x{sec_id:02x}"
                    ));
                }
                if sec_id < 0x80 {
                    return err(format!("unknown required section 0x{sec_id:02x}"));
                }
                return err(format!(
                    "missing required section 0x{expected:02x} (found optional section 0x{sec_id:02x} first)"
                ));
            }
            payloads.insert(sec_id, payload);
            expect_idx += 1;
        } else {
            if required.contains(&sec_id) {
                return err(format!("duplicate required section 0x{sec_id:02x}"));
            }
            if sec_id < 0x80 {
                return err(format!("unknown required section 0x{sec_id:02x}"));
            }
            if sec_id == SEC_DEBUG {
                if debug_payload.is_some() {
                    return err("duplicate DEBUG section");
                }
                debug_payload = Some(payload);
            }
            // else: an unknown optional section -- already consumed, skip it.
        }
    }
    if expect_idx < required.len() {
        return err(format!("missing required section 0x{:02x}", required[expect_idx]));
    }
    Ok(Sections { payloads, debug_payload })
}

fn parse_strings(payload: &[u8]) -> FResult<Vec<String>> {
    let mut pr = Reader::new(payload);
    let count = pr.varuint()?;
    let mut strings = Vec::new();
    for _ in 0..count {
        let length = pr.varuint()?;
        let raw = pr.bytes(length as usize)?;
        match std::str::from_utf8(raw) {
            Ok(s) => strings.push(s.to_string()),
            Err(e) => return err(format!("invalid UTF-8 in STRINGS section: {e}")),
        }
    }
    check_consumed(&pr, "STRINGS")?;
    Ok(strings)
}

fn parse_constants(payload: &[u8], nstrings: usize) -> FResult<Vec<Const>> {
    let mut pr = Reader::new(payload);
    let count = pr.varuint()?;
    let mut constants = Vec::new();
    for _ in 0..count {
        let tag = pr.u8()?;
        let c = match tag {
            TAG_NONE => Const::None,
            TAG_FALSE => Const::False,
            TAG_TRUE => Const::True,
            TAG_INT => Const::Int(pr.raw_varuint_groups()?),
            TAG_DEC => {
                let idx = pr.varuint()? as usize;
                if idx >= nstrings {
                    return err(format!("CONSTANTS: decimal-text string index {idx} out of range"));
                }
                Const::Dec(idx)
            }
            TAG_STR => {
                let idx = pr.varuint()? as usize;
                if idx >= nstrings {
                    return err(format!("CONSTANTS: string constant index {idx} out of range"));
                }
                Const::Str(idx)
            }
            other => return err(format!("unknown constant tag {other}")),
        };
        constants.push(c);
    }
    check_consumed(&pr, "CONSTANTS")?;
    Ok(constants)
}

fn str_idx(pr: &mut Reader, nstrings: usize, section: &str) -> FResult<usize> {
    let idx = pr.varuint()? as usize;
    if idx >= nstrings {
        return err(format!("{section}: string index {idx} out of range"));
    }
    Ok(idx)
}

fn parse_types(payload: &[u8], nstrings: usize) -> FResult<Vec<TypeDecl>> {
    let mut pr = Reader::new(payload);
    let count = pr.varuint()?;
    let mut types = Vec::new();
    for _ in 0..count {
        let kind = pr.u8()?;
        let name = str_idx(&mut pr, nstrings, "TYPES")?;
        let body = match kind {
            0 => {
                let nfields = pr.varuint()?;
                let mut fields = Vec::new();
                for _ in 0..nfields {
                    fields.push(str_idx(&mut pr, nstrings, "TYPES")?);
                }
                TypeBody::Struct(fields)
            }
            1 => {
                let nvariants = pr.varuint()?;
                let mut variants = Vec::new();
                for _ in 0..nvariants {
                    let vname = str_idx(&mut pr, nstrings, "TYPES")?;
                    let nfields = pr.varuint()?;
                    let mut vfields = Vec::new();
                    for _ in 0..nfields {
                        vfields.push(str_idx(&mut pr, nstrings, "TYPES")?);
                    }
                    variants.push((vname, vfields));
                }
                TypeBody::Enum(variants)
            }
            other => return err(format!("unknown TYPES kind {other}")),
        };
        types.push(TypeDecl { name, body });
    }
    check_consumed(&pr, "TYPES")?;
    Ok(types)
}

fn parse_natives(payload: &[u8], nstrings: usize) -> FResult<Vec<NativeRef>> {
    let mut pr = Reader::new(payload);
    let count = pr.varuint()?;
    let mut natives = Vec::new();
    for _ in 0..count {
        let name = pr.varuint()? as usize;
        if name >= nstrings {
            return err(format!("NATIVES: name string index {name} out of range"));
        }
        let arity = pr.varuint()?;
        natives.push(NativeRef { name, arity });
    }
    check_consumed(&pr, "NATIVES")?;
    Ok(natives)
}

fn parse_functions(payload: &[u8], nstrings: usize) -> FResult<Vec<FunctionDecl>> {
    let mut pr = Reader::new(payload);
    let count = pr.varuint()?;
    if count < 1 {
        return err("FUNCTIONS section must declare at least one function (function 0, the main program)");
    }
    let mut functions = Vec::new();
    for _ in 0..count {
        let entry = pr.varuint()?;
        let slot_count = pr.varuint()?;
        let param_count = pr.varuint()?;
        let name_flag = pr.varuint()?;
        let name = if name_flag == 0 { None } else { Some((name_flag - 1) as usize) };
        if let Some(n) = name {
            if n >= nstrings {
                return err(format!("FUNCTIONS: name string index {n} out of range"));
            }
        }
        functions.push(FunctionDecl { entry, slot_count, param_count, name, params: None });
    }
    if functions[0].entry != 0 || functions[0].param_count != 0 {
        return err("function 0 (the main program) must have entry=0 and param_count=0");
    }
    check_consumed(&pr, "FUNCTIONS")?;
    Ok(functions)
}

fn parse_params(payload: &[u8], functions: Vec<FunctionDecl>, nstrings: usize) -> FResult<Vec<FunctionDecl>> {
    let mut pr = Reader::new(payload);
    let mut out = Vec::with_capacity(functions.len());
    for fn_decl in functions {
        let nparams = pr.varuint()?;
        if nparams != fn_decl.param_count {
            return err(format!(
                "PARAMS: function declares {} parameter(s) but PARAMS lists {nparams}",
                fn_decl.param_count
            ));
        }
        let mut params = Vec::new();
        for _ in 0..nparams {
            let name = pr.varuint()? as usize;
            if name >= nstrings {
                return err(format!("PARAMS: name string index {name} out of range"));
            }
            let flags = pr.u8()?;
            if flags & !1 != 0 {
                return err(format!("PARAMS: invalid flags byte {flags} (only bit 0 is defined)"));
            }
            params.push((name, flags & 1 != 0));
        }
        out.push(FunctionDecl { params: Some(params), ..fn_decl });
    }
    check_consumed(&pr, "PARAMS")?;
    Ok(out)
}

// ---------------------------------------------------------------------------
// CODE section
// ---------------------------------------------------------------------------

struct CodeCtx<'a> {
    nstrings: usize,
    strings: &'a [String],
    nconsts: usize,
    ntypes: usize,
    types: &'a [TypeDecl],
    nfunctions: usize,
    nnatives: usize,
    natives: &'a [NativeRef],
    minor: u16,
}

/// The `(variant_name_idx, field_indices)` list for enum type index
/// `t_index` (0/1 built in, or >=2 user), or `None` if it isn't an enum.
fn type_variants<'a>(t_index: usize, ctx: &'a CodeCtx) -> Option<Vec<(usize, usize)>> {
    // returns (variant "index" placeholder unused, nfields) pairs -- callers
    // only need nfields per variant plus the variant count.
    if t_index == 0 {
        return Some(vec![(0, 0), (0, 1)]); // Option: none(0), some(1)
    }
    if t_index == 1 {
        return Some(vec![(0, 0), (0, 1)]); // Promise: Pending(0), Settled(1)
    }
    let decl = ctx.types.get(t_index - 2)?;
    match &decl.body {
        TypeBody::Enum(variants) => Some(variants.iter().map(|(_n, f)| (0, f.len())).collect()),
        TypeBody::Struct(_) => None,
    }
}

fn type_field_count(t_index: usize, ctx: &CodeCtx) -> Option<usize> {
    if t_index == 0 || t_index == 1 {
        return None; // Option/Promise are enums, never a struct target
    }
    let decl = ctx.types.get(t_index - 2)?;
    match &decl.body {
        TypeBody::Struct(fields) => Some(fields.len()),
        TypeBody::Enum(_) => None,
    }
}

fn decode_addr(pr: &mut Reader) -> FResult<Addr> {
    Ok((pr.varuint()?, pr.varuint()?))
}

fn decode_addr_opt(pr: &mut Reader) -> FResult<Option<Addr>> {
    let d = pr.varuint()?;
    if d == 0 {
        return Ok(None);
    }
    Ok(Some((d - 1, pr.varuint()?)))
}

fn decode_addr_list(pr: &mut Reader) -> FResult<Vec<Addr>> {
    let n = pr.varuint()?;
    let mut out = Vec::new();
    for _ in 0..n {
        out.push(decode_addr(pr)?);
    }
    Ok(out)
}

fn decode_k(pr: &mut Reader, ctx: &CodeCtx) -> FResult<usize> {
    let k = pr.varuint()? as usize;
    if k >= ctx.nconsts {
        return err(format!("constant index {k} out of range"));
    }
    Ok(k)
}

fn decode_s(pr: &mut Reader, ctx: &CodeCtx) -> FResult<usize> {
    let s = pr.varuint()? as usize;
    if s >= ctx.nstrings {
        return err(format!("string index {s} out of range"));
    }
    Ok(s)
}

fn decode_s_opt(pr: &mut Reader, ctx: &CodeCtx) -> FResult<Option<usize>> {
    let s = pr.varuint()?;
    if s == 0 {
        return Ok(None);
    }
    let idx = (s - 1) as usize;
    if idx >= ctx.nstrings {
        return err(format!("string index {idx} out of range"));
    }
    Ok(Some(idx))
}

fn decode_s_list(pr: &mut Reader, ctx: &CodeCtx) -> FResult<Vec<usize>> {
    let n = pr.varuint()?;
    let mut out = Vec::new();
    for _ in 0..n {
        out.push(decode_s(pr, ctx)?);
    }
    Ok(out)
}

fn decode_f(pr: &mut Reader, ctx: &CodeCtx) -> FResult<usize> {
    let f = pr.varuint()? as usize;
    if f >= ctx.nfunctions {
        return err(format!("function index {f} out of range"));
    }
    Ok(f)
}

fn decode_t(pr: &mut Reader, ctx: &CodeCtx) -> FResult<usize> {
    let t = pr.varuint()? as usize;
    if t >= 2 + ctx.ntypes {
        return err(format!("type index {t} out of range"));
    }
    Ok(t)
}

fn decode_x(pr: &mut Reader, ctx: &CodeCtx) -> FResult<usize> {
    let x = pr.varuint()? as usize;
    if x >= ctx.nnatives {
        return err(format!("native index {x} out of range"));
    }
    Ok(x)
}

fn decode_b(pr: &mut Reader) -> FResult<bool> {
    let b = pr.u8()?;
    match b {
        0 => Ok(false),
        1 => Ok(true),
        other => err(format!("invalid boolean flag {other}")),
    }
}

fn opcode_info(op: u8) -> Option<(&'static str, Option<u16>)> {
    // (name, since_minor)
    Some(match op {
        0x00 => ("halt", None),
        0x01 => ("move", None),
        0x02 => ("loadk", None),
        0x03 => ("jmp", None),
        0x04 => ("jmpf", None),
        0x05 => ("jmpset", Some(1)),
        0x10 => ("add", None),
        0x11 => ("sub", None),
        0x12 => ("mul", None),
        0x13 => ("div", None),
        0x14 => ("idiv", None),
        0x15 => ("mod", None),
        0x16 => ("pow", None),
        0x17 => ("eq", None),
        0x18 => ("neq", None),
        0x19 => ("lt", None),
        0x1A => ("gt", None),
        0x1B => ("and", None),
        0x1C => ("or", None),
        0x1D => ("neg", None),
        0x1E => ("le", Some(2)),
        0x1F => ("ge", Some(2)),
        0x0F => ("not", Some(2)),
        0x20 => ("closure", None),
        0x21 => ("call", None),
        0x22 => ("ret", None),
        0x23 => ("retval", None),
        0x26 => ("callkw", Some(1)),
        0x24 => ("callmethod", None),
        0x25 => ("defmethod", None),
        0x27 => ("callmethodkw", Some(1)),
        0x28 => ("detach", None),
        0x29 => ("detachmethod", None),
        0x2A => ("await", None),
        0x2B => ("detachkw", Some(1)),
        0x2C => ("detachmethodkw", Some(1)),
        0x30 => ("struct", None),
        0x31 => ("enum", None),
        0x32 => ("getfield", None),
        0x33 => ("setfield", None),
        0x34 => ("matchstruct", None),
        0x35 => ("matchenum", None),
        0x36 => ("matchfail", None),
        0x37 => ("matchrange", Some(2)),
        0x38 => ("vector", Some(3)),
        0x39 => ("map", Some(3)),
        0x40 => ("deferpush", None),
        0x41 => ("deferadd", None),
        0x42 => ("deferpeek", None),
        0x43 => ("deferpop", None),
        0x44 => ("deferscopepop", None),
        0x50 => ("native", None),
        _ => return None,
    })
}

fn native_since_minor(name: &str) -> Option<u16> {
    match name {
        "io.write" => Some(1),
        _ => None,
    }
}

fn parse_code(payload: &[u8], ctx: &CodeCtx) -> FResult<Vec<RawInstr>> {
    let mut pr = Reader::new(payload);
    let count = pr.varuint()?;
    let mut instrs: Vec<RawInstr> = Vec::new();
    for i in 0..count {
        let opcode = pr.u8()?;
        let (name, since) = match opcode_info(opcode) {
            Some(v) => v,
            None => return err(format!("unknown opcode 0x{opcode:02x} at instruction {i}")),
        };
        if let Some(since) = since {
            if ctx.minor < since {
                return err(format!(
                    "opcode '{name}' at instruction {i} requires minor version >= {since}, but this file's minor version is {}",
                    ctx.minor
                ));
            }
        }
        let instr = decode_one_instr(name, &mut pr, ctx, i)?;
        instrs.push(instr);
    }
    check_consumed(&pr, "CODE")?;

    let ncode = instrs.len() as u64;
    for (i, instr) in instrs.iter().enumerate() {
        validate_instr(instr, i, ncode, ctx)?;
    }
    Ok(instrs)
}

fn decode_one_instr(name: &str, pr: &mut Reader, ctx: &CodeCtx, _i: u64) -> FResult<RawInstr> {
    Ok(match name {
        "halt" => RawInstr::Halt,
        "move" => RawInstr::Move { src: decode_addr(pr)?, dest: decode_addr(pr)? },
        "loadk" => RawInstr::Loadk { k: decode_k(pr, ctx)?, dest: decode_addr(pr)? },
        "jmp" => RawInstr::Jmp { target: pr.varuint()? as usize },
        "jmpf" => RawInstr::Jmpf { cond: decode_addr(pr)?, target: pr.varuint()? as usize },
        "jmpset" => RawInstr::Jmpset { param: decode_addr(pr)?, target: pr.varuint()? as usize },
        "add" | "sub" | "mul" | "div" | "idiv" | "mod" | "pow" | "eq" | "neq" | "lt" | "gt" | "le" | "ge" | "and"
        | "or" => {
            let a = decode_addr(pr)?;
            let b = decode_addr(pr)?;
            let dest = decode_addr(pr)?;
            let op = match name {
                "add" => BinOp::Add,
                "sub" => BinOp::Sub,
                "mul" => BinOp::Mul,
                "div" => BinOp::Div,
                "idiv" => BinOp::Idiv,
                "mod" => BinOp::Mod,
                "pow" => BinOp::Pow,
                "eq" => BinOp::Eq,
                "neq" => BinOp::Neq,
                "lt" => BinOp::Lt,
                "gt" => BinOp::Gt,
                "le" => BinOp::Le,
                "ge" => BinOp::Ge,
                "and" => BinOp::And,
                _ => BinOp::Or,
            };
            RawInstr::BinOp { op, a, b, dest }
        }
        "neg" => RawInstr::Neg { a: decode_addr(pr)?, dest: decode_addr(pr)? },
        "not" => RawInstr::Not { a: decode_addr(pr)?, dest: decode_addr(pr)? },
        "closure" => RawInstr::Closure { func: decode_f(pr, ctx)?, dest: decode_addr(pr)? },
        "call" => RawInstr::Call { callee: decode_addr(pr)?, args: decode_addr_list(pr)? },
        "callkw" => {
            let callee = decode_addr(pr)?;
            let args = decode_addr_list(pr)?;
            let kwnames = decode_s_list(pr, ctx)?;
            RawInstr::CallKw { callee, args, kwnames }
        }
        "ret" => RawInstr::Ret { value: decode_addr(pr)? },
        "retval" => RawInstr::Retval { dest: decode_addr(pr)? },
        "callmethod" => {
            let recv = decode_addr(pr)?;
            let name_idx = decode_s(pr, ctx)?;
            let args = decode_addr_list(pr)?;
            let trait_ = decode_s_opt(pr, ctx)?;
            RawInstr::CallMethod { recv, name: name_idx, args, trait_ }
        }
        "defmethod" => {
            let closure = decode_addr(pr)?;
            let type_name = decode_s(pr, ctx)?;
            let trait_ = decode_s_opt(pr, ctx)?;
            let mname = decode_s(pr, ctx)?;
            let is_method = decode_b(pr)?;
            RawInstr::Defmethod { closure, type_name, trait_, name: mname, is_method }
        }
        "callmethodkw" => {
            let recv = decode_addr(pr)?;
            let name_idx = decode_s(pr, ctx)?;
            let args = decode_addr_list(pr)?;
            let kwnames = decode_s_list(pr, ctx)?;
            let trait_ = decode_s_opt(pr, ctx)?;
            RawInstr::CallMethodKw { recv, name: name_idx, args, kwnames, trait_ }
        }
        "detach" => {
            let callee = decode_addr(pr)?;
            let args = decode_addr_list(pr)?;
            let dest = decode_addr(pr)?;
            RawInstr::Detach { callee, args, dest }
        }
        "detachmethod" => {
            let recv = decode_addr(pr)?;
            let name_idx = decode_s(pr, ctx)?;
            let args = decode_addr_list(pr)?;
            let trait_ = decode_s_opt(pr, ctx)?;
            let dest = decode_addr(pr)?;
            RawInstr::DetachMethod { recv, name: name_idx, args, trait_, dest }
        }
        "await" => RawInstr::Await { promise: decode_addr(pr)?, dest: decode_addr(pr)? },
        "detachkw" => {
            let callee = decode_addr(pr)?;
            let args = decode_addr_list(pr)?;
            let kwnames = decode_s_list(pr, ctx)?;
            let dest = decode_addr(pr)?;
            RawInstr::DetachKw { callee, args, kwnames, dest }
        }
        "detachmethodkw" => {
            let recv = decode_addr(pr)?;
            let name_idx = decode_s(pr, ctx)?;
            let args = decode_addr_list(pr)?;
            let kwnames = decode_s_list(pr, ctx)?;
            let trait_ = decode_s_opt(pr, ctx)?;
            let dest = decode_addr(pr)?;
            RawInstr::DetachMethodKw { recv, name: name_idx, args, kwnames, trait_, dest }
        }
        "struct" => {
            let type_idx = decode_t(pr, ctx)?;
            let values = decode_addr_list(pr)?;
            let dest = decode_addr(pr)?;
            RawInstr::Struct { type_idx, values, dest }
        }
        "enum" => {
            let type_idx = decode_t(pr, ctx)?;
            let variant = pr.varuint()? as usize;
            let values = decode_addr_list(pr)?;
            let dest = decode_addr(pr)?;
            RawInstr::Enum { type_idx, variant, values, dest }
        }
        "getfield" => {
            let obj = decode_addr(pr)?;
            let field = decode_s(pr, ctx)?;
            let dest = decode_addr(pr)?;
            RawInstr::GetField { obj, field, dest }
        }
        "setfield" => {
            let obj = decode_addr(pr)?;
            let field = decode_s(pr, ctx)?;
            let src = decode_addr(pr)?;
            RawInstr::SetField { obj, field, src }
        }
        "matchstruct" => {
            let value = decode_addr(pr)?;
            let type_idx = decode_t(pr, ctx)?;
            let dest = decode_addr(pr)?;
            RawInstr::MatchStruct { value, type_idx, dest }
        }
        "matchenum" => {
            let value = decode_addr(pr)?;
            let type_idx = decode_t(pr, ctx)?;
            let variant = pr.varuint()? as usize;
            let dest = decode_addr(pr)?;
            RawInstr::MatchEnum { value, type_idx, variant, dest }
        }
        "matchfail" => RawInstr::MatchFail,
        "matchrange" => {
            let value = decode_addr(pr)?;
            let lo = decode_addr_opt(pr)?;
            let hi = decode_addr_opt(pr)?;
            let inclusive = decode_b(pr)?;
            let dest = decode_addr(pr)?;
            RawInstr::MatchRange { value, lo, hi, inclusive, dest }
        }
        "vector" => RawInstr::Vector { items: decode_addr_list(pr)?, dest: decode_addr(pr)? },
        "map" => RawInstr::Map { pairs: decode_addr_list(pr)?, dest: decode_addr(pr)? },
        "deferpush" => RawInstr::DeferPush,
        "deferadd" => RawInstr::DeferAdd { closure: decode_addr(pr)? },
        "deferpeek" => RawInstr::DeferPeek { dest: decode_addr(pr)? },
        "deferpop" => RawInstr::DeferPop { dest: decode_addr(pr)? },
        "deferscopepop" => RawInstr::DeferScopePop,
        "native" => {
            let native = decode_x(pr, ctx)?;
            let args = decode_addr_list(pr)?;
            let dest = decode_addr_opt(pr)?;
            RawInstr::Native { native, args, dest }
        }
        other => unreachable!("unhandled opcode name {other:?}"),
    })
}

fn validate_instr(instr: &RawInstr, i: usize, ncode: u64, ctx: &CodeCtx) -> FResult<()> {
    match instr {
        RawInstr::Jmp { target } | RawInstr::Jmpf { target, .. } | RawInstr::Jmpset { target, .. } => {
            if *target as u64 >= ncode {
                return err(format!("jump target {target} out of range at instruction {i}"));
            }
        }
        RawInstr::CallKw { args, kwnames, .. } => validate_kwnames("callkw", args.len(), kwnames, ctx, i)?,
        RawInstr::CallMethodKw { args, kwnames, .. } => {
            validate_kwnames("callmethodkw", args.len(), kwnames, ctx, i)?
        }
        RawInstr::DetachKw { args, kwnames, .. } => validate_kwnames("detachkw", args.len(), kwnames, ctx, i)?,
        RawInstr::DetachMethodKw { args, kwnames, .. } => {
            validate_kwnames("detachmethodkw", args.len(), kwnames, ctx, i)?
        }
        RawInstr::Struct { type_idx, values, .. } => {
            let nfields = match type_field_count(*type_idx, ctx) {
                Some(n) => n,
                None => return err(format!("'struct' at instruction {i} used with a non-struct type index {type_idx}")),
            };
            if values.len() != nfields {
                return err(format!(
                    "'struct' at instruction {i}: {} value(s) given, type declares {nfields} field(s)",
                    values.len()
                ));
            }
        }
        RawInstr::Enum { type_idx, variant, values, .. } => {
            let variants = match type_variants(*type_idx, ctx) {
                Some(v) => v,
                None => return err(format!("'enum' at instruction {i} used with a non-enum type index {type_idx}")),
            };
            if *variant >= variants.len() {
                return err(format!(
                    "'enum' at instruction {i}: variant index {variant} out of range for type {type_idx}"
                ));
            }
            let nfields = variants[*variant].1;
            if values.len() != nfields {
                return err(format!(
                    "'enum' at instruction {i}: {} value(s) given, variant declares {nfields} field(s)",
                    values.len()
                ));
            }
        }
        RawInstr::MatchStruct { type_idx, .. } => {
            if type_field_count(*type_idx, ctx).is_none() {
                return err(format!(
                    "'matchstruct' at instruction {i} used with a non-struct type index {type_idx}"
                ));
            }
        }
        RawInstr::MatchEnum { type_idx, variant, .. } => {
            let variants = match type_variants(*type_idx, ctx) {
                Some(v) => v,
                None => {
                    return err(format!(
                        "'matchenum' at instruction {i} used with a non-enum type index {type_idx}"
                    ))
                }
            };
            if *variant >= variants.len() {
                return err(format!(
                    "'matchenum' at instruction {i}: variant index {variant} out of range for type {type_idx}"
                ));
            }
        }
        RawInstr::Map { pairs, .. } => {
            if pairs.len() % 2 != 0 {
                return err(format!(
                    "'map' at instruction {i}: needs an even number of addresses, got {}",
                    pairs.len()
                ));
            }
        }
        RawInstr::MatchRange { lo, hi, .. } => {
            if lo.is_none() && hi.is_none() {
                return err(format!("'matchrange' at instruction {i}: at least one of lo/hi must be present"));
            }
        }
        RawInstr::Native { native, args, .. } => {
            let n = &ctx.natives[*native];
            if args.len() as u64 != n.arity {
                return err(format!(
                    "'native' at instruction {i}: {} arg(s) given, native declares arity {}",
                    args.len(),
                    n.arity
                ));
            }
            let native_name = &ctx.strings[n.name];
            if let Some(since) = native_since_minor(native_name) {
                if ctx.minor < since {
                    return err(format!(
                        "native '{native_name}' at instruction {i} requires minor version >= {since}, but this file's minor version is {}",
                        ctx.minor
                    ));
                }
            }
        }
        _ => {}
    }
    Ok(())
}

fn validate_kwnames(op: &str, nargs: usize, kwnames: &[usize], ctx: &CodeCtx, i: usize) -> FResult<()> {
    if kwnames.len() > nargs {
        return err(format!(
            "'{op}' at instruction {i}: {} keyword name(s) but only {nargs} arg(s)",
            kwnames.len()
        ));
    }
    let mut seen = std::collections::HashSet::new();
    for &idx in kwnames {
        let text = &ctx.strings[idx];
        if !seen.insert(text) {
            return err(format!("'{op}' at instruction {i}: keyword argument name '{text}' given more than once"));
        }
    }
    Ok(())
}

fn parse_debug(payload: &[u8], nstrings: usize) -> FResult<DebugInfo> {
    let mut pr = Reader::new(payload);
    let nfiles = pr.varuint()?;
    let mut files = Vec::new();
    for _ in 0..nfiles {
        let idx = pr.varuint()? as usize;
        if idx >= nstrings {
            return err(format!("DEBUG: file string index {idx} out of range"));
        }
        files.push(idx);
    }
    let nruns = pr.varuint()?;
    let mut runs = Vec::new();
    let mut prev_pc: u64 = 0;
    for _ in 0..nruns {
        let delta = pr.varuint()?;
        let pc = prev_pc + delta;
        prev_pc = pc;
        let file_idx = pr.varuint()? as usize;
        if file_idx >= files.len() {
            return err(format!("DEBUG: run file index {file_idx} out of range"));
        }
        let line = pr.varuint()?;
        let col = pr.varuint()?;
        runs.push((pc as usize, file_idx, line, col));
    }
    check_consumed(&pr, "DEBUG")?;
    Ok(DebugInfo { files, runs })
}

/// Decode a `.mahc` file, including a possible leading shebang line (bytes
/// `#!` up to and including the first `\n`) -- docs/MAHC_FORMAT.md #3.
pub fn decode(data: &[u8]) -> FResult<Program> {
    let data: &[u8] = if data.starts_with(b"#!") {
        match data.iter().position(|&b| b == b'\n') {
            Some(nl) => &data[nl + 1..],
            None => return err("truncated file: shebang line without a newline"),
        }
    } else {
        data
    };
    let mut r = Reader::new(data);
    let magic = r.bytes(4)?;
    if magic != MAGIC {
        return err(format!(
            "bad magic: expected {}, got {}",
            python_bytes_repr(MAGIC),
            python_bytes_repr(magic)
        ));
    }
    let major = r.u16()?;
    if major != MAJOR {
        return err(format!("unsupported major version {major} (this VM implements major version {MAJOR})"));
    }
    let minor = r.u16()?;
    if minor > MINOR {
        return err(format!("unsupported minor version {minor} (this VM supports up to minor version {MINOR})"));
    }

    let sections = read_sections(&mut r, minor)?;
    let get = |id: u8| sections.payloads.get(&id).expect("required section missing after read_sections validated it");

    let strings = parse_strings(get(SEC_STRINGS))?;
    let constants = parse_constants(get(SEC_CONSTANTS), strings.len())?;
    let types = parse_types(get(SEC_TYPES), strings.len())?;
    let natives = parse_natives(get(SEC_NATIVES), strings.len())?;
    let mut functions = parse_functions(get(SEC_FUNCTIONS), strings.len())?;
    if minor >= 1 {
        functions = parse_params(get(SEC_PARAMS), functions, strings.len())?;
    }

    let ctx = CodeCtx {
        nstrings: strings.len(),
        strings: &strings,
        nconsts: constants.len(),
        ntypes: types.len(),
        types: &types,
        nfunctions: functions.len(),
        nnatives: natives.len(),
        natives: &natives,
        minor,
    };
    let code = parse_code(get(SEC_CODE), &ctx)?;

    let debug = match &sections.debug_payload {
        Some(p) => Some(parse_debug(p, strings.len())?),
        None => None,
    };

    Ok(Program { strings, constants, types, natives, functions, code, debug, minor })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn minimal_file(minor: u16) -> Vec<u8> {
        // MAGIC, major=1, minor, then STRINGS(empty), CONSTANTS(empty),
        // TYPES(empty), NATIVES(empty), FUNCTIONS(one: main), [PARAMS], CODE(halt).
        let mut out = Vec::new();
        out.extend_from_slice(MAGIC);
        out.extend_from_slice(&1u16.to_le_bytes());
        out.extend_from_slice(&minor.to_le_bytes());
        // STRINGS: count 0
        push_section(&mut out, 0x01, &[0]);
        // CONSTANTS: count 0
        push_section(&mut out, 0x02, &[0]);
        // TYPES: count 0
        push_section(&mut out, 0x03, &[0]);
        // NATIVES: count 0
        push_section(&mut out, 0x04, &[0]);
        // FUNCTIONS: count 1, entry=0 slot_count=0 param_count=0 name_flag=0
        push_section(&mut out, 0x05, &[1, 0, 0, 0, 0]);
        // CODE: count 1, halt
        push_section(&mut out, 0x06, &[1, 0x00]);
        if minor >= 1 {
            // PARAMS: one function, 0 params (required section order is by
            // increasing id, i.e. PARAMS 0x07 comes right after CODE 0x06).
            push_section(&mut out, 0x07, &[0]);
        }
        out
    }

    fn push_section(out: &mut Vec<u8>, id: u8, payload: &[u8]) {
        out.push(id);
        out.extend(write_varuint(payload.len() as u64));
        out.extend_from_slice(payload);
    }

    fn write_varuint(mut n: u64) -> Vec<u8> {
        let mut out = Vec::new();
        loop {
            let byte = (n & 0x7F) as u8;
            n >>= 7;
            if n != 0 {
                out.push(byte | 0x80);
            } else {
                out.push(byte);
                return out;
            }
        }
    }

    #[test]
    fn decodes_minimal_file() {
        let data = minimal_file(3);
        let program = decode(&data).expect("should decode");
        assert_eq!(program.functions.len(), 1);
        assert_eq!(program.code.len(), 1);
        assert_eq!(program.code[0], RawInstr::Halt);
    }

    #[test]
    fn minor_0_without_params_ok() {
        let data = minimal_file(0);
        assert!(decode(&data).is_ok());
    }

    #[test]
    fn skips_shebang() {
        let mut data = b"#!/usr/bin/env -S mah runc\n".to_vec();
        data.extend(minimal_file(3));
        assert!(decode(&data).is_ok());
    }

    #[test]
    fn shebang_without_newline_is_truncated() {
        let data = b"#!oops".to_vec();
        let e = decode(&data).unwrap_err();
        assert_eq!(e.0, "truncated file: shebang line without a newline");
    }

    #[test]
    fn bad_magic_message_matches_python_repr() {
        let data = b"XXXX".to_vec();
        let e = decode(&data).unwrap_err();
        assert_eq!(e.0, "bad magic: expected b'MAHC', got b'XXXX'");
    }

    #[test]
    fn truncated_file_reports_truncation() {
        let data = b"MA".to_vec();
        let e = decode(&data).unwrap_err();
        assert!(e.0.starts_with("truncated file"));
    }

    #[test]
    fn unsupported_major_version() {
        let mut data = MAGIC.to_vec();
        data.extend_from_slice(&2u16.to_le_bytes());
        data.extend_from_slice(&0u16.to_le_bytes());
        let e = decode(&data).unwrap_err();
        assert_eq!(e.0, "unsupported major version 2 (this VM implements major version 1)");
    }

    #[test]
    fn unsupported_minor_version() {
        let mut data = MAGIC.to_vec();
        data.extend_from_slice(&1u16.to_le_bytes());
        data.extend_from_slice(&99u16.to_le_bytes());
        let e = decode(&data).unwrap_err();
        assert_eq!(e.0, "unsupported minor version 99 (this VM supports up to minor version 3)");
    }

    #[test]
    fn unknown_opcode_rejected() {
        let mut data = minimal_file(3);
        // Locate the CODE section's payload (id=0x06, len=2, [count=1, opcode=0x00])
        // and replace the opcode byte with an unknown one.
        let needle = [0x06u8, 0x02, 0x01, 0x00];
        let pos = data
            .windows(needle.len())
            .position(|w| w == needle)
            .expect("CODE section bytes not found");
        data[pos + 3] = 0xEE;
        let e = decode(&data).unwrap_err();
        assert_eq!(e.0, "unknown opcode 0xee at instruction 0");
    }

    #[test]
    fn python_bytes_repr_matches_expected_forms() {
        assert_eq!(python_bytes_repr(b"MAHC"), "b'MAHC'");
        assert_eq!(python_bytes_repr(b"\t\n\r"), "b'\\t\\n\\r'");
        assert_eq!(python_bytes_repr(&[0x00, 0xff]), "b'\\x00\\xff'");
        assert_eq!(python_bytes_repr(b"it's"), "b\"it's\"");
        assert_eq!(python_bytes_repr(b"a'b\"c"), "b'a\\'b\"c'");
    }
}
