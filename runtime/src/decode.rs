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
pub const MINOR: u16 = 20;

const SEC_STRINGS: u8 = 0x01;
const SEC_CONSTANTS: u8 = 0x02;
const SEC_TYPES: u8 = 0x03;
const SEC_NATIVES: u8 = 0x04;
const SEC_FUNCTIONS: u8 = 0x05;
const SEC_CODE: u8 = 0x06;
const SEC_PARAMS: u8 = 0x07;
/// M25 (1.4, docs/MAHC_FORMAT.md #4.8): required iff minor >= 4.
const SEC_HANDLERS: u8 = 0x08;
const SEC_DEBUG: u8 = 0x80;
const SEC_TESTS: u8 = 0x81; // M28: optional, `mah test` builds only
const SEC_META: u8 = 0x82; // M41a: optional, annotations/docs/defaults (docs/MAHC_FORMAT.md #4.10)

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
const REQUIRED_SECTIONS_V4: &[u8] = &[
    SEC_STRINGS,
    SEC_CONSTANTS,
    SEC_TYPES,
    SEC_NATIVES,
    SEC_FUNCTIONS,
    SEC_CODE,
    SEC_PARAMS,
    SEC_HANDLERS,
];

/// M25 (docs/MAHC_FORMAT.md #4.1): how many built-in TYPES-section-index-0..
/// entries a file of this minor version has (and where user types start
/// numbering from) -- 2 (Option, Promise) below minor 4, 3 (+RuntimeError)
/// from minor 4.
fn builtin_type_count(minor: u16) -> usize {
    if minor >= 4 {
        3
    } else {
        2
    }
}

/// M25: the fixed field-name list for each `RuntimeError` variant, in
/// declaration order -- mirrors `mah/bytecode/format.py`'s
/// `BUILTIN_TYPES_V4` and `compiler/resolve.py`'s pre-seeded `enum_decls`.
pub const RUNTIME_ERROR_VARIANTS: &[&str] = &[
    "DivisionByZero",
    "TypeMismatch",
    "NoSuchField",
    "NoSuchMethod",
    "ArgumentError",
    "IndexOutOfRange",
    "MatchFailed",
    "InputError",
    "Internal",
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
    /// M41c (1.16, PARAMS flag bits 1/2): bit 0 = the last ordinary-after
    /// parameter is a `...` positional rest, bit 1 = the last is a `**`
    /// keyword rest.
    pub rest: u8,
}

/// M41a (docs/MAHC_FORMAT.md #4.10): a written type annotation, resolved to
/// the declarations it names. Mirrors `mah/bytecode/program.py`'s `TypeRef`.
#[derive(Debug, Clone, PartialEq)]
pub enum TypeRef {
    Unknown,
    /// `kind` 0: a TYPES index (built-in enums 0-2, then user types); 1: a
    /// primitive code (Number, String, Bool, Function, Vector, Map, None, Type).
    Named { kind: u8, index: usize, args: Vec<TypeRef> },
    Fn { params: Vec<TypeRef>, ret: Box<TypeRef>, throws: Option<Vec<TypeRef>> },
    Param(usize),
    SelfType,
    Never,
    Trait { name: usize, args: Vec<TypeRef> },
}

#[derive(Debug, Clone, PartialEq)]
pub struct ParamMeta {
    pub ty: TypeRef,
    pub doc: Option<usize>,
    /// 0 = no default, 1 = a default that isn't constant, 2 = constant.
    pub default: u8,
    pub const_index: Option<usize>,
}

/// One function's META entry; `has_meta` false = nothing was recorded.
#[derive(Debug, Clone, PartialEq)]
pub struct FnMeta {
    pub has_meta: bool,
    pub doc: Option<usize>,
    pub type_params: Vec<usize>,
    pub params: Vec<ParamMeta>,
    pub returns: TypeRef,
    pub throws: Option<Vec<TypeRef>>,
}

#[derive(Debug, Clone, PartialEq)]
pub enum TypeMetaBody {
    /// Per field: its type and doc.
    Struct(Vec<(TypeRef, Option<usize>)>),
    /// Per variant: its doc and its fields' types.
    Enum(Vec<(Option<usize>, Vec<TypeRef>)>),
}

#[derive(Debug, Clone, PartialEq)]
pub struct TypeMeta {
    pub doc: Option<usize>,
    pub type_params: Vec<usize>,
    pub body: TypeMetaBody,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Meta {
    pub functions: Vec<FnMeta>,
    pub types: Vec<TypeMeta>,
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
    /// M41a (1.14): callee, the Vector of positional arguments, the Map of keyword arguments
    CallSpread { callee: Addr, args: Addr, kwargs: Addr },
    /// M41a (1.14)
    CallMethodSpread { recv: Addr, name: usize, args: Addr, kwargs: Addr, trait_: Option<usize> },
    /// M41a (1.14): append a Vector's items to a Vector / merge a Map into a Map
    Spread { target: Addr, source: Addr, keyword: bool },
    /// M41a (1.14): a Type value -- `kind` 0 a TYPES index, 1 a primitive code
    LoadType { kind: u8, index: usize, dest: Addr },
    /// M41b (1.15): store the decorators (`values`, in source order) of one
    /// target -- `kind` 0 function `a`, 1 parameter `b` of function `a`, 2 type `a`,
    /// 3 field `b` of struct `a`, 4 variant `b` of enum `a`
    Decorate { kind: u64, a: usize, b: usize, values: Vec<Addr> },
    /// M41c (1.16): `dest` <- the WrapParam hooks stored for parameter `param`
    /// of function `func` (or `none`)
    ParamHooks { func: usize, param: usize, dest: Addr },
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
    /// M25 (1.4)
    MatchType { value: Addr, type_idx: usize, dest: Addr },
    /// M25 (1.4)
    DeferDepth { dest: Addr },
    /// M25 (1.4)
    DeferAbove { depth: Addr, dest: Addr },
    /// M25 (1.4)
    Throw { value: Addr },
    Native { native: usize, args: Vec<Addr>, dest: Option<Addr> },
}

/// M25 (docs/MAHC_FORMAT.md #4.8): one HANDLERS entry -- `[start, end)`
/// covers instructions whose throw unwinds to `handler` in the same frame,
/// writing the thrown value into frame-0 slot `slot`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct HandlerEntry {
    pub start: usize,
    pub end: usize,
    pub handler: usize,
    pub slot: u64,
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
    /// M25 (1.4, docs/MAHC_FORMAT.md #4.8): always empty for minor < 4.
    pub handlers: Vec<HandlerEntry>,
    /// M28 (docs/MAHC_FORMAT.md #4.9): the optional TESTS section -- only
    /// in a `mah test` build.
    pub tests: Vec<TestEntry>,
    /// M41a (docs/MAHC_FORMAT.md #4.10): the optional META section.
    pub meta: Option<Meta>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct TestEntry {
    pub name: usize,
    pub slot: usize,
    pub line: u64,
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
    let required: &[u8] = if minor >= 4 {
        REQUIRED_SECTIONS_V4
    } else if minor >= 1 {
        REQUIRED_SECTIONS_V1
    } else {
        REQUIRED_SECTIONS
    };
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
            } else if sec_id == SEC_TESTS {
                // M28: kept alongside the required payloads.
                if payloads.contains_key(&SEC_TESTS) {
                    return err("duplicate TESTS section");
                }
                payloads.insert(SEC_TESTS, payload);
            } else if sec_id == SEC_META {
                // M41a: likewise.
                if payloads.contains_key(&SEC_META) {
                    return err("duplicate META section");
                }
                payloads.insert(SEC_META, payload);
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
        functions.push(FunctionDecl { entry, slot_count, param_count, name, params: None, rest: 0 });
    }
    if functions[0].entry != 0 || functions[0].param_count != 0 {
        return err("function 0 (the main program) must have entry=0 and param_count=0");
    }
    check_consumed(&pr, "FUNCTIONS")?;
    Ok(functions)
}

fn parse_params(
    payload: &[u8],
    functions: Vec<FunctionDecl>,
    nstrings: usize,
    minor: u64,
) -> FResult<Vec<FunctionDecl>> {
    let mut pr = Reader::new(payload);
    let mut out = Vec::with_capacity(functions.len());
    for (fn_index, fn_decl) in functions.into_iter().enumerate() {
        let nparams = pr.varuint()?;
        if nparams != fn_decl.param_count {
            return err(format!(
                "PARAMS: function declares {} parameter(s) but PARAMS lists {nparams}",
                fn_decl.param_count
            ));
        }
        let mut params = Vec::new();
        let mut flag_bytes = Vec::new();
        for _ in 0..nparams {
            let name = pr.varuint()? as usize;
            if name >= nstrings {
                return err(format!("PARAMS: name string index {name} out of range"));
            }
            let flags = pr.u8()?;
            if minor < 16 {
                if flags & !1 != 0 {
                    return err(format!("PARAMS: invalid flags byte {flags} (only bit 0 is defined)"));
                }
            } else if flags & !7 != 0 {
                return err(format!("PARAMS: invalid flags byte {flags} (only bits 0-2 are defined)"));
            }
            params.push((name, flags & 1 != 0));
            flag_bytes.push(flags);
        }
        let rest = rest_flags(&flag_bytes, fn_index)?;
        out.push(FunctionDecl { params: Some(params), rest, ..fn_decl });
    }
    check_consumed(&pr, "PARAMS")?;
    Ok(out)
}

/// M41c: the rest-parameter bits of one function's PARAMS flags (mirrors
/// `mah/bytecode/decode.py`'s `_rest_flags`, message for message).
fn rest_flags(flag_bytes: &[u8], fn_index: usize) -> FResult<u8> {
    let n = flag_bytes.len();
    let mut rest = 0u8;
    for (i, &flags) in flag_bytes.iter().enumerate() {
        let pos = flags & 2 != 0;
        let kw = flags & 4 != 0;
        if !(pos || kw) {
            continue;
        }
        if pos && kw {
            return err(format!(
                "PARAMS: function {fn_index} parameter {i} is flagged as both a '...' and a '**' rest parameter"
            ));
        }
        if flags & 1 != 0 {
            return err(format!(
                "PARAMS: function {fn_index} parameter {i} is a rest parameter and can't have a default"
            ));
        }
        if kw {
            if i != n - 1 {
                return err(format!("PARAMS: function {fn_index}: the '**' rest parameter must be the last parameter"));
            }
            rest |= 2;
        } else {
            let last_ok = i == n - 1 || (i + 2 == n && flag_bytes[n - 1] & 4 != 0);
            if !last_ok {
                return err(format!(
                    "PARAMS: function {fn_index}: the '...' rest parameter must be the last parameter or the one before the '**' rest parameter"
                ));
            }
            rest |= 1;
        }
    }
    Ok(rest)
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
    /// M41b: each function's parameter count, for `decorate`'s validation.
    param_counts: Vec<u64>,
    nnatives: usize,
    natives: &'a [NativeRef],
    minor: u16,
    /// M25: `builtin_type_count(minor)` -- how many built-in TYPES-index-0..
    /// entries precede the user types in `types`.
    builtin_types: usize,
}

/// The `(variant_name_idx, field_indices)` list for enum type index
/// `t_index` (built in, or user -- `>= ctx.builtin_types`), or `None` if it
/// isn't an enum. M25: `RuntimeError` (index 2, minor >= 4) is a built-in
/// enum too, every variant with exactly one field (`message`).
fn type_variants<'a>(t_index: usize, ctx: &'a CodeCtx) -> Option<Vec<(usize, usize)>> {
    // returns (variant "index" placeholder unused, nfields) pairs -- callers
    // only need nfields per variant plus the variant count.
    if t_index == 0 {
        return Some(vec![(0, 0), (0, 1)]); // Option: none(0), some(1)
    }
    if t_index == 1 {
        if ctx.minor >= 4 {
            return Some(vec![(0, 0), (0, 1), (0, 1)]); // Promise: Pending, Settled, Failed
        }
        return Some(vec![(0, 0), (0, 1)]); // Promise: Pending(0), Settled(1)
    }
    if t_index == 2 && ctx.minor >= 4 {
        // RuntimeError: every variant has exactly one field (`message`).
        return Some(vec![(0, 1); crate::decode::RUNTIME_ERROR_VARIANTS.len()]);
    }
    let decl = ctx.types.get(t_index - ctx.builtin_types)?;
    match &decl.body {
        TypeBody::Enum(variants) => Some(variants.iter().map(|(_n, f)| (0, f.len())).collect()),
        TypeBody::Struct(_) => None,
    }
}

fn type_field_count(t_index: usize, ctx: &CodeCtx) -> Option<usize> {
    if t_index < ctx.builtin_types {
        return None; // every built-in type (Option/Promise/RuntimeError) is an enum
    }
    let decl = ctx.types.get(t_index - ctx.builtin_types)?;
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
    if t >= ctx.builtin_types + ctx.ntypes {
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
        0x2D => ("callspread", Some(14)),
        0x2E => ("callmethodspread", Some(14)),
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
        0x3A => ("matchtype", Some(4)),
        0x3B => ("loadtype", Some(14)),
        0x3C => ("spread", Some(14)),
        0x3D => ("decorate", Some(15)),
        0x3E => ("paramhooks", Some(16)),
        0x40 => ("deferpush", None),
        0x41 => ("deferadd", None),
        0x42 => ("deferpeek", None),
        0x43 => ("deferpop", None),
        0x44 => ("deferscopepop", None),
        0x45 => ("deferdepth", Some(4)),
        0x46 => ("deferabove", Some(4)),
        0x50 => ("native", None),
        0x60 => ("throw", Some(4)),
        _ => return None,
    })
}

fn native_since_minor(name: &str) -> Option<u16> {
    match name {
        "io.write" => Some(1),
        "math.tan" | "math.asin" | "math.acos" | "math.atan" | "math.atan2" | "math.exp" | "math.log"
        | "math.log10" => Some(5),
        "value.type_name" | "value.fields" | "value.variant" | "string.chars" | "string.code_point"
        | "string.from_code_point" => Some(7),
        "random.seed" | "random.fresh" | "random.next" | "random.below" => Some(8),
        "regex.find" | "regex.find_all" => Some(9),
        "io.read_line" => Some(10),
        "time.now_ms" | "time.monotonic_ms" | "time.cancel" | "promise.new" | "promise.resolve"
        | "promise.fail" => Some(11),
        "fs.read_text" | "fs.write_text" | "fs.append_text" | "fs.info" | "fs.list_dir" | "fs.mkdir"
        | "fs.remove" | "fs.rename" | "fs.copy" | "fs.temp_dir" | "fs.open" | "fs.read_line" | "fs.read_all"
        | "fs.write" | "fs.close" => Some(12),
        "process.args" | "process.exit" | "process.env_get" | "process.env_set" | "process.env_remove"
        | "process.env_all" | "process.cwd" | "process.pid" | "process.platform" | "process.run" => Some(13),
        "reflect.type_of" | "reflect.signature" | "reflect.schema" | "reflect.methods" | "reflect.implements"
        | "reflect.construct" | "reflect.construct_variant" => Some(14),
        "reflect.decorators" => Some(15),
        "hooks.has" | "hooks.adopt" | "hooks.same_fn" | "hooks.set_type" | "hooks.set_param" | "hooks.of"
        | "hooks.get_field" | "hooks.set_field" => Some(16),
        "bytes.new" | "bytes.from_vector" | "bytes.from_hex" | "bytes.from_base64" | "fs.read_bytes"
        | "fs.write_bytes" | "fs.append_bytes" | "fs.file_read_bytes" | "fs.file_write_bytes" => Some(17),
        "socket.connect" | "socket.listen" | "socket.accept" | "socket.send" | "socket.recv"
        | "socket.shutdown" | "socket.close" => Some(18),
        "socket.start_tls" => Some(19),
        "socket.tls_server_config" | "socket.start_tls_server" => Some(20),
        _ => None,
    }
}

/// How many primitive type codes (`loadtype 1, code`, META) a file of this
/// minor may use: M37 (1.17) adds code 8, Bytes.
fn primitive_type_count(minor: u16) -> usize {
    if minor >= 17 {
        9
    } else {
        8
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
        "callspread" => {
            let callee = decode_addr(pr)?;
            let args = decode_addr(pr)?;
            let kwargs = decode_addr(pr)?;
            RawInstr::CallSpread { callee, args, kwargs }
        }
        "callmethodspread" => {
            let recv = decode_addr(pr)?;
            let name_idx = decode_s(pr, ctx)?;
            let args = decode_addr(pr)?;
            let kwargs = decode_addr(pr)?;
            let trait_ = decode_s_opt(pr, ctx)?;
            RawInstr::CallMethodSpread { recv, name: name_idx, args, kwargs, trait_ }
        }
        "spread" => {
            let target = decode_addr(pr)?;
            let source = decode_addr(pr)?;
            let keyword = decode_b(pr)?;
            RawInstr::Spread { target, source, keyword }
        }
        "loadtype" => {
            let kind = pr.varuint()?;
            let index = pr.varuint()? as usize;
            let dest = decode_addr(pr)?;
            RawInstr::LoadType { kind: kind.min(255) as u8, index, dest }
        }
        "decorate" => {
            let kind = pr.varuint()?;
            let a = pr.varuint()? as usize;
            let b = pr.varuint()? as usize;
            let values = decode_addr_list(pr)?;
            RawInstr::Decorate { kind, a, b, values }
        }
        "paramhooks" => {
            let func = pr.varuint()? as usize;
            let param = pr.varuint()? as usize;
            let dest = decode_addr(pr)?;
            RawInstr::ParamHooks { func, param, dest }
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
        "matchtype" => {
            let value = decode_addr(pr)?;
            let type_idx = decode_t(pr, ctx)?;
            let dest = decode_addr(pr)?;
            RawInstr::MatchType { value, type_idx, dest }
        }
        "deferdepth" => RawInstr::DeferDepth { dest: decode_addr(pr)? },
        "deferabove" => RawInstr::DeferAbove { depth: decode_addr(pr)?, dest: decode_addr(pr)? },
        "throw" => RawInstr::Throw { value: decode_addr(pr)? },
        "native" => {
            let native = decode_x(pr, ctx)?;
            let args = decode_addr_list(pr)?;
            let dest = decode_addr_opt(pr)?;
            RawInstr::Native { native, args, dest }
        }
        other => unreachable!("unhandled opcode name {other:?}"),
    })
}

/// M41b: `decorate`'s operands -- mirrors `_validate_decorate` in
/// `mah/bytecode/decode.py` (same messages).
fn validate_decorate(kind: u64, a: usize, b: usize, i: usize, ctx: &CodeCtx) -> FResult<()> {
    if kind > 4 {
        return err(format!("'decorate' at instruction {i}: unknown kind {kind}"));
    }
    if kind <= 1 {
        if a >= ctx.nfunctions {
            return err(format!("'decorate' at instruction {i}: function index {a} out of range"));
        }
        if kind == 1 && b as u64 >= ctx.param_counts[a] {
            return err(format!(
                "'decorate' at instruction {i}: parameter index {b} out of range for function {a}"
            ));
        }
        return Ok(());
    }
    if a < ctx.builtin_types || a >= ctx.builtin_types + ctx.ntypes {
        return err(format!("'decorate' at instruction {i}: type index {a} is not a user type"));
    }
    let decl = &ctx.types[a - ctx.builtin_types];
    match (kind, &decl.body) {
        (3, TypeBody::Struct(fields)) => {
            if b >= fields.len() {
                return err(format!("'decorate' at instruction {i}: field index {b} out of range for type {a}"));
            }
        }
        (3, TypeBody::Enum(_)) => return err(format!("'decorate' at instruction {i}: type index {a} is not a struct")),
        (4, TypeBody::Enum(variants)) => {
            if b >= variants.len() {
                return err(format!("'decorate' at instruction {i}: variant index {b} out of range for type {a}"));
            }
        }
        (4, TypeBody::Struct(_)) => return err(format!("'decorate' at instruction {i}: type index {a} is not an enum")),
        _ => {}
    }
    Ok(())
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
        RawInstr::LoadType { kind, index, .. } => match kind {
            0 => {
                if *index >= ctx.builtin_types + ctx.ntypes {
                    return err(format!("'loadtype' at instruction {i}: type index {index} out of range"));
                }
            }
            1 => {
                if *index >= primitive_type_count(ctx.minor) {
                    return err(format!("'loadtype' at instruction {i}: primitive type code {index} out of range"));
                }
            }
            other => return err(format!("'loadtype' at instruction {i}: unknown kind {other}")),
        },
        RawInstr::Decorate { kind, a, b, .. } => validate_decorate(*kind, *a, *b, i, ctx)?,
        RawInstr::ParamHooks { func, param, .. } => {
            if *func >= ctx.nfunctions {
                return err(format!("'paramhooks' at instruction {i}: function index {func} out of range"));
            }
            if *param as u64 >= ctx.param_counts[*func] {
                return err(format!(
                    "'paramhooks' at instruction {i}: parameter index {param} out of range for function {func}"
                ));
            }
        }
        RawInstr::Defmethod { type_name, .. } if ctx.minor >= 16 => {
            // M41c: a function-item impl's type is spelled `fn#<function index>`
            let name = &ctx.strings[*type_name];
            if let Some(digits) = name.strip_prefix("fn#") {
                let canonical = !digits.is_empty()
                    && digits.bytes().all(|c| c.is_ascii_digit())
                    && (digits == "0" || !digits.starts_with('0'));
                let in_range = canonical && digits.parse::<usize>().map(|n| n < ctx.nfunctions).unwrap_or(false);
                if !in_range {
                    return err(format!(
                        "'defmethod' at instruction {i}: '{name}' is not a valid function-item key"
                    ));
                }
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

/// M25 (docs/MAHC_FORMAT.md #4.8): HANDLERS section, required iff minor >=
/// 4. `ncode` (already known: CODE, id 0x06, decodes before HANDLERS, id
/// 0x08, in section-id order) lets every range/target be validated
/// immediately, like jump targets in `parse_code`.
fn parse_handlers(payload: &[u8], ncode: usize) -> FResult<Vec<HandlerEntry>> {
    let mut pr = Reader::new(payload);
    let count = pr.varuint()?;
    let mut handlers = Vec::new();
    for _ in 0..count {
        let start = pr.varuint()? as usize;
        let end = pr.varuint()? as usize;
        let handler = pr.varuint()? as usize;
        let slot = pr.varuint()?;
        if !(start < end && end <= ncode) {
            return err(format!("HANDLERS: invalid range [{start}, {end}) for {ncode} instruction(s)"));
        }
        if handler >= ncode {
            return err(format!("HANDLERS: handler {handler} out of range for {ncode} instruction(s)"));
        }
        handlers.push(HandlerEntry { start, end, handler, slot });
    }
    check_consumed(&pr, "HANDLERS")?;
    Ok(handlers)
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
/// M27 (docs/MAHC_FORMAT.md #3/#4.4): a file newer than this VM is still
/// rejected, but when it lists natives this VM doesn't have, the message
/// names them. STRINGS and NATIVES keep their 1.0 layout in every 1.x file;
/// anything unreadable just gives the plain message. Mirrors
/// `mah/bytecode/decode.py`'s `_newer_minor_message`.
fn newer_minor_message(r: &mut Reader, minor: u16) -> String {
    let message = format!("unsupported minor version {minor} (this VM supports up to minor version {MINOR})");
    let mut payloads: std::collections::HashMap<u8, &[u8]> = std::collections::HashMap::new();
    while r.remaining() > 0 {
        let Ok(id) = r.u8() else { return message };
        let Ok(len) = r.varuint() else { return message };
        let Ok(payload) = r.bytes(len as usize) else { return message };
        payloads.insert(id, payload);
    }
    let (Some(strings), Some(natives)) = (payloads.get(&SEC_STRINGS), payloads.get(&SEC_NATIVES)) else {
        return message;
    };
    let Ok(strings) = parse_strings(strings) else { return message };
    let Ok(natives) = parse_natives(natives, strings.len()) else { return message };
    let missing: Vec<String> = natives
        .iter()
        .map(|n| &strings[n.name])
        .filter(|name| !crate::vm::is_known_native(name))
        .map(|name| format!("'{name}'"))
        .collect();
    if missing.is_empty() {
        return message;
    }
    format!("{message}: it uses natives this VM doesn't have ({}); upgrade mah to run it", missing.join(", "))
}

/// M41a (docs/MAHC_FORMAT.md #4.10): the META section -- one entry per
/// function (FUNCTIONS order) and per user type (TYPES order, built-ins
/// excluded), each validated against what it describes. Mirrors
/// `mah/bytecode/decode.py`'s `_parse_meta`, messages included.
fn parse_meta(payload: &[u8], functions: &[FunctionDecl], types: &[TypeDecl], ctx: &CodeCtx) -> FResult<Meta> {
    struct M<'a, 'c> {
        pr: Reader<'a>,
        ctx: &'c CodeCtx<'c>,
    }
    impl M<'_, '_> {
        fn str_idx(&mut self) -> FResult<usize> {
            let idx = self.pr.varuint()? as usize;
            if idx >= self.ctx.nstrings {
                return err(format!("META: string index {idx} out of range"));
            }
            Ok(idx)
        }
        fn opt_str(&mut self) -> FResult<Option<usize>> {
            let v = self.pr.varuint()?;
            if v == 0 {
                return Ok(None);
            }
            let idx = (v - 1) as usize;
            if idx >= self.ctx.nstrings {
                return err(format!("META: string index {idx} out of range"));
            }
            Ok(Some(idx))
        }
        fn count(&mut self) -> FResult<usize> {
            let n = self.pr.varuint()?;
            if n > self.pr.remaining() as u64 {
                return err("META: count larger than the remaining payload");
            }
            Ok(n as usize)
        }
        fn throws_clause(&mut self) -> FResult<Option<Vec<TypeRef>>> {
            let flag = self.pr.u8()?;
            match flag {
                0 => Ok(None),
                1 => {
                    let n = self.count()?;
                    let mut out = Vec::with_capacity(n);
                    for _ in 0..n {
                        out.push(self.typeref()?);
                    }
                    Ok(Some(out))
                }
                other => err(format!("META: invalid throws flag {other}")),
            }
        }
        fn refs(&mut self) -> FResult<Vec<TypeRef>> {
            let n = self.count()?;
            let mut out = Vec::with_capacity(n);
            for _ in 0..n {
                out.push(self.typeref()?);
            }
            Ok(out)
        }
        fn typeref(&mut self) -> FResult<TypeRef> {
            let tag = self.pr.u8()?;
            match tag {
                0 => Ok(TypeRef::Unknown),
                4 => Ok(TypeRef::SelfType),
                5 => Ok(TypeRef::Never),
                1 => {
                    let kind = self.pr.u8()?;
                    let index = self.pr.varuint()? as usize;
                    match kind {
                        0 => {
                            if index >= self.ctx.builtin_types + self.ctx.ntypes {
                                return err(format!("META: type index {index} out of range"));
                            }
                        }
                        1 => {
                            if index >= primitive_type_count(self.ctx.minor) {
                                return err(format!("META: primitive type code {index} out of range"));
                            }
                        }
                        other => return err(format!("META: unknown named-type kind {other}")),
                    }
                    Ok(TypeRef::Named { kind, index, args: self.refs()? })
                }
                2 => {
                    let params = self.refs()?;
                    let ret = Box::new(self.typeref()?);
                    Ok(TypeRef::Fn { params, ret, throws: self.throws_clause()? })
                }
                3 => Ok(TypeRef::Param(self.str_idx()?)),
                6 => {
                    let name = self.str_idx()?;
                    Ok(TypeRef::Trait { name, args: self.refs()? })
                }
                other => err(format!("META: unknown type tag {other}")),
            }
        }
    }
    let mut m = M { pr: Reader::new(payload), ctx };
    let nfunctions = m.pr.varuint()?;
    if nfunctions != functions.len() as u64 {
        return err(format!("META: describes {nfunctions} function(s) but FUNCTIONS declares {}", functions.len()));
    }
    let mut fn_metas = Vec::with_capacity(functions.len());
    for f in functions {
        let flags = m.pr.u8()?;
        if flags & !1 != 0 {
            return err(format!("META: invalid flags byte {flags} (only bit 0 is defined)"));
        }
        if flags & 1 == 0 {
            fn_metas.push(FnMeta {
                has_meta: false,
                doc: None,
                type_params: Vec::new(),
                params: Vec::new(),
                returns: TypeRef::Unknown,
                throws: None,
            });
            continue;
        }
        let doc = m.opt_str()?;
        let ntp = m.count()?;
        let mut type_params = Vec::with_capacity(ntp);
        for _ in 0..ntp {
            type_params.push(m.str_idx()?);
        }
        let nparams = m.pr.varuint()?;
        if nparams != f.param_count {
            return err(format!("META: function declares {} parameter(s) but META lists {nparams}", f.param_count));
        }
        let mut params = Vec::new();
        for _ in 0..nparams {
            let ty = m.typeref()?;
            let pdoc = m.opt_str()?;
            let default = m.pr.u8()?;
            let mut const_index = None;
            if default == 2 {
                let c = m.pr.varuint()? as usize;
                if c >= ctx.nconsts {
                    return err(format!("META: constant index {c} out of range"));
                }
                const_index = Some(c);
            } else if default > 2 {
                return err(format!("META: invalid default flag {default}"));
            }
            params.push(ParamMeta { ty, doc: pdoc, default, const_index });
        }
        let returns = m.typeref()?;
        let throws = m.throws_clause()?;
        fn_metas.push(FnMeta { has_meta: true, doc, type_params, params, returns, throws });
    }
    let ntypes = m.pr.varuint()?;
    if ntypes != types.len() as u64 {
        return err(format!("META: describes {ntypes} type(s) but TYPES declares {}", types.len()));
    }
    let mut type_metas = Vec::with_capacity(types.len());
    for decl in types {
        let doc = m.opt_str()?;
        let ntp = m.count()?;
        let mut type_params = Vec::with_capacity(ntp);
        for _ in 0..ntp {
            type_params.push(m.str_idx()?);
        }
        let body = match &decl.body {
            TypeBody::Struct(fields) => {
                let mut out = Vec::with_capacity(fields.len());
                for _ in fields {
                    let ty = m.typeref()?;
                    out.push((ty, m.opt_str()?));
                }
                TypeMetaBody::Struct(out)
            }
            TypeBody::Enum(variants) => {
                let mut out = Vec::with_capacity(variants.len());
                for (_vname, vfields) in variants {
                    let vdoc = m.opt_str()?;
                    let mut refs = Vec::with_capacity(vfields.len());
                    for _ in vfields {
                        refs.push(m.typeref()?);
                    }
                    out.push((vdoc, refs));
                }
                TypeMetaBody::Enum(out)
            }
        };
        type_metas.push(TypeMeta { doc, type_params, body });
    }
    check_consumed(&m.pr, "META")?;
    Ok(Meta { functions: fn_metas, types: type_metas })
}

/// M28 (docs/MAHC_FORMAT.md #4.9): `count x (name str, slot varuint, line
/// varuint)`; `slot` must be a slot of the main function's frame.
fn parse_tests(payload: &[u8], nstrings: usize, nslots: u64) -> FResult<Vec<TestEntry>> {
    let mut pr = Reader::new(payload);
    let count = pr.varuint()?;
    let mut tests = Vec::new();
    for _ in 0..count {
        let name = pr.varuint()?;
        if name as usize >= nstrings {
            return err(format!("TESTS: name string index {name} out of range"));
        }
        let slot = pr.varuint()?;
        if slot >= nslots {
            return err(format!("TESTS: slot {slot} out of range"));
        }
        let line = pr.varuint()?;
        tests.push(TestEntry { name: name as usize, slot: slot as usize, line });
    }
    if pr.remaining() != 0 {
        return err("TESTS section payload not fully consumed (trailing garbage)");
    }
    Ok(tests)
}

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
        return err(newer_minor_message(&mut r, minor));
    }

    let sections = read_sections(&mut r, minor)?;
    let get = |id: u8| sections.payloads.get(&id).expect("required section missing after read_sections validated it");

    let strings = parse_strings(get(SEC_STRINGS))?;
    let constants = parse_constants(get(SEC_CONSTANTS), strings.len())?;
    let types = parse_types(get(SEC_TYPES), strings.len())?;
    let natives = parse_natives(get(SEC_NATIVES), strings.len())?;
    let mut functions = parse_functions(get(SEC_FUNCTIONS), strings.len())?;
    if minor >= 1 {
        functions = parse_params(get(SEC_PARAMS), functions, strings.len(), minor as u64)?;
    }

    let ctx = CodeCtx {
        nstrings: strings.len(),
        strings: &strings,
        nconsts: constants.len(),
        ntypes: types.len(),
        types: &types,
        nfunctions: functions.len(),
        param_counts: functions.iter().map(|f| f.param_count).collect(),
        nnatives: natives.len(),
        natives: &natives,
        minor,
        builtin_types: builtin_type_count(minor),
    };
    let code = parse_code(get(SEC_CODE), &ctx)?;

    let handlers = if minor >= 4 { parse_handlers(get(SEC_HANDLERS), code.len())? } else { Vec::new() };

    let debug = match &sections.debug_payload {
        Some(p) => Some(parse_debug(p, strings.len())?),
        None => None,
    };

    let tests = match sections.payloads.get(&SEC_TESTS) {
        Some(p) => match functions.first() {
            Some(main) => parse_tests(p, strings.len(), main.slot_count)?,
            None => return err("TESTS section in a file with no functions"),
        },
        None => Vec::new(),
    };

    let meta = match sections.payloads.get(&SEC_META) {
        Some(p) => Some(parse_meta(p, &functions, &types, &ctx)?),
        None => None,
    };

    Ok(Program { strings, constants, types, natives, functions, code, debug, minor, handlers, tests, meta })
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
        if minor >= 4 {
            // HANDLERS: count 0 (M25, 0x08, right after PARAMS).
            push_section(&mut out, 0x08, &[0]);
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
        let data = minimal_file(4);
        let program = decode(&data).expect("should decode");
        assert_eq!(program.functions.len(), 1);
        assert_eq!(program.code.len(), 1);
        assert_eq!(program.code[0], RawInstr::Halt);
        assert!(program.handlers.is_empty());
    }

    /// `minimal_file`, but with `code` (a whole CODE section payload) and a
    /// STRINGS/NATIVES-free layout -- for opcode tests.
    fn file_with_code(minor: u16, code: &[u8]) -> Vec<u8> {
        let mut out = Vec::new();
        out.extend_from_slice(MAGIC);
        out.extend_from_slice(&1u16.to_le_bytes());
        out.extend_from_slice(&minor.to_le_bytes());
        push_section(&mut out, 0x01, &[0]);
        push_section(&mut out, 0x02, &[0]);
        push_section(&mut out, 0x03, &[0]);
        push_section(&mut out, 0x04, &[0]);
        push_section(&mut out, 0x05, &[1, 0, 0, 0, 0]);
        push_section(&mut out, 0x06, code);
        push_section(&mut out, 0x07, &[0]);
        push_section(&mut out, 0x08, &[0]);
        out
    }

    #[test]
    fn decodes_loadtype_and_the_spread_opcodes() {
        // loadtype (primitive 7 = Type) into (0,0); spread (0,0) <- (0,0), keyword;
        // callspread callee (0,0) args (0,0) kwargs (0,0); halt
        let code = [4, 0x3B, 1, 7, 0, 0, 0x3C, 0, 0, 0, 0, 1, 0x2D, 0, 0, 0, 0, 0, 0, 0x00];
        let program = decode(&file_with_code(14, &code)).expect("should decode");
        assert_eq!(program.code[0], RawInstr::LoadType { kind: 1, index: 7, dest: (0, 0) });
        assert_eq!(program.code[1], RawInstr::Spread { target: (0, 0), source: (0, 0), keyword: true });
        assert_eq!(program.code[2], RawInstr::CallSpread { callee: (0, 0), args: (0, 0), kwargs: (0, 0) });
    }

    #[test]
    fn the_new_opcodes_need_minor_14() {
        let code = [2, 0x3B, 1, 7, 0, 0, 0x00];
        let e = decode(&file_with_code(13, &code)).unwrap_err();
        assert_eq!(e.0, "opcode 'loadtype' at instruction 0 requires minor version >= 14, but this file's minor version is 13");
    }

    #[test]
    fn loadtype_operands_are_validated() {
        for (kind, index, message) in [
            (0u8, 9u8, "'loadtype' at instruction 0: type index 9 out of range"),
            (1, 8, "'loadtype' at instruction 0: primitive type code 8 out of range"),
            (2, 0, "'loadtype' at instruction 0: unknown kind 2"),
        ] {
            let code = [2, 0x3B, kind, index, 0, 0, 0x00];
            assert_eq!(decode(&file_with_code(14, &code)).unwrap_err().0, message);
        }
    }

    #[test]
    fn decodes_decorate_and_validates_it() {
        // decorate kind 0, function 0, b 0, no values; halt
        let ok = [2, 0x3D, 0, 0, 0, 0, 0x00];
        let program = decode(&file_with_code(15, &ok)).expect("should decode");
        assert_eq!(program.code[0], RawInstr::Decorate { kind: 0, a: 0, b: 0, values: vec![] });
        for (code, message) in [
            ([2u8, 0x3D, 5, 0, 0, 0, 0x00], "'decorate' at instruction 0: unknown kind 5"),
            ([2, 0x3D, 0, 9, 0, 0, 0x00], "'decorate' at instruction 0: function index 9 out of range"),
            ([2, 0x3D, 1, 0, 3, 0, 0x00], "'decorate' at instruction 0: parameter index 3 out of range for function 0"),
            ([2, 0x3D, 2, 1, 0, 0, 0x00], "'decorate' at instruction 0: type index 1 is not a user type"),
            ([2, 0x3D, 3, 3, 0, 0, 0x00], "'decorate' at instruction 0: type index 3 is not a user type"),
        ] {
            assert_eq!(decode(&file_with_code(15, &code)).unwrap_err().0, message);
        }
        let e = decode(&file_with_code(14, &ok)).unwrap_err();
        assert_eq!(e.0, "opcode 'decorate' at instruction 0 requires minor version >= 15, but this file's minor version is 14");
    }


    /// A file whose FUNCTIONS declares function 0 plus one function with `nparams`
    /// parameters (named by string 0) whose PARAMS flags are `flags`.
    fn file_with_params(minor: u16, flags: &[u8], code: &[u8]) -> Vec<u8> {
        let mut out = Vec::new();
        out.extend_from_slice(MAGIC);
        out.extend_from_slice(&1u16.to_le_bytes());
        out.extend_from_slice(&minor.to_le_bytes());
        push_section(&mut out, 0x01, &[1, 1, b'a']);
        push_section(&mut out, 0x02, &[0]);
        push_section(&mut out, 0x03, &[0]);
        push_section(&mut out, 0x04, &[0]);
        push_section(&mut out, 0x05, &[2, 0, 0, 0, 0, 0, 0, flags.len() as u8, 0]);
        push_section(&mut out, 0x06, code);
        let mut params = vec![0u8, flags.len() as u8];
        for flag in flags {
            params.extend_from_slice(&[0, *flag]);
        }
        push_section(&mut out, 0x07, &params);
        push_section(&mut out, 0x08, &[0]);
        out
    }

    #[test]
    fn rest_flags_are_decoded_and_validated() {
        let halt = [1, 0x00];
        let program = decode(&file_with_params(16, &[0, 2, 4], &halt)).expect("should decode");
        assert_eq!(program.functions[1].rest, 3);
        assert_eq!(decode(&file_with_params(16, &[0, 2], &halt)).unwrap().functions[1].rest, 1);
        assert_eq!(decode(&file_with_params(16, &[4], &halt)).unwrap().functions[1].rest, 2);
        for (flags, message) in [
            (vec![0u8, 1, 5], "PARAMS: function 1 parameter 2 is a rest parameter and can't have a default"),
            (vec![0, 0, 6], "PARAMS: function 1 parameter 2 is flagged as both a '...' and a '**' rest parameter"),
            (vec![0, 0, 8], "PARAMS: invalid flags byte 8 (only bits 0-2 are defined)"),
            (vec![2, 0, 0], "PARAMS: function 1: the '...' rest parameter must be the last parameter or the one before the '**' rest parameter"),
            (vec![0, 4, 4], "PARAMS: function 1: the '**' rest parameter must be the last parameter"),
        ] {
            assert_eq!(decode(&file_with_params(16, &flags, &halt)).unwrap_err().0, message);
        }
        assert_eq!(
            decode(&file_with_params(15, &[0, 2, 4], &halt)).unwrap_err().0,
            "PARAMS: invalid flags byte 2 (only bit 0 is defined)"
        );
    }

    #[test]
    fn decodes_paramhooks_and_validates_it() {
        // paramhooks function 1, parameter 0 -> (0,0); halt
        let ok = [2, 0x3E, 1, 0, 0, 0, 0x00];
        let program = decode(&file_with_params(16, &[0], &ok)).expect("should decode");
        assert_eq!(program.code[0], RawInstr::ParamHooks { func: 1, param: 0, dest: (0, 0) });
        assert_eq!(
            decode(&file_with_params(16, &[0], &[2, 0x3E, 9, 0, 0, 0, 0x00])).unwrap_err().0,
            "'paramhooks' at instruction 0: function index 9 out of range"
        );
        assert_eq!(
            decode(&file_with_params(16, &[0], &[2, 0x3E, 1, 5, 0, 0, 0x00])).unwrap_err().0,
            "'paramhooks' at instruction 0: parameter index 5 out of range for function 1"
        );
        assert_eq!(
            decode(&file_with_params(15, &[0], &ok)).unwrap_err().0,
            "opcode 'paramhooks' at instruction 0 requires minor version >= 16, but this file's minor version is 15"
        );
    }

    #[test]
    fn decodes_a_meta_section() {
        // one function without metadata, no user types
        let mut data = minimal_file(14);
        push_section(&mut data, 0x82, &[1, 0, 0]);
        let program = decode(&data).expect("should decode");
        let meta = program.meta.expect("META was there");
        assert_eq!(meta.functions.len(), 1);
        assert!(!meta.functions[0].has_meta);
        assert!(meta.types.is_empty());
        // ...and without one, there is none (older encoders, or META stripped)
        assert!(decode(&minimal_file(14)).unwrap().meta.is_none());
    }

    #[test]
    fn meta_must_describe_the_files_functions() {
        let mut data = minimal_file(14);
        push_section(&mut data, 0x82, &[5]);
        assert_eq!(decode(&data).unwrap_err().0, "META: describes 5 function(s) but FUNCTIONS declares 1");
        let mut data = minimal_file(14);
        push_section(&mut data, 0x82, &[1, 3]);
        assert_eq!(decode(&data).unwrap_err().0, "META: invalid flags byte 3 (only bit 0 is defined)");
        let mut data = minimal_file(14);
        push_section(&mut data, 0x82, &[1, 0, 0]);
        push_section(&mut data, 0x82, &[1, 0, 0]);
        assert_eq!(decode(&data).unwrap_err().0, "duplicate META section");
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
        // M27: the current maximum is 5; the file has no sections at all,
        // so there are no natives to name.
        assert_eq!(e.0, format!("unsupported minor version 99 (this VM supports up to minor version {MINOR})"));
        assert_eq!(MINOR, 20);
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
