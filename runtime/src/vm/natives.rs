//! The native function registry -- docs/MAHC_FORMAT.md #4.4, mirroring
//! `mah/natives.py`. `io.input`'s digit scanning matches Python's
//! `str.isdigit()` restricted to Unicode category Nd (see the spec: Python
//! also accepts ~128 non-Nd characters there, which then fail `Decimal()`
//! anyway -- an accepted, documented corner case).

use std::rc::Rc;

use crate::decimal::Decimal;

use super::error::{ErrorKind, RuntimeError};
use super::exec::Vm;
use super::link::NativeFn;
use super::value::Value;

/// The first code point of every contiguous "0..9" Unicode Nd digit block
/// (spec-provided; matches Python's `unicodedata.digit`/`str.isdigit` for
/// category Nd characters).
const DIGIT_BLOCK_ZEROES: &[u32] = &[
    0x30, 0x660, 0x6f0, 0x7c0, 0x966, 0x9e6, 0xa66, 0xae6, 0xb66, 0xbe6, 0xc66, 0xce6, 0xd66, 0xde6, 0xe50, 0xed0,
    0xf20, 0x1040, 0x1090, 0x17e0, 0x1810, 0x1946, 0x19d0, 0x1a80, 0x1a90, 0x1b50, 0x1bb0, 0x1c40, 0x1c50, 0xa620,
    0xa8d0, 0xa900, 0xa9d0, 0xa9f0, 0xaa50, 0xabf0, 0xff10, 0x104a0, 0x10d30, 0x10d40, 0x11066, 0x110f0, 0x11136,
    0x111d0, 0x112f0, 0x11450, 0x114d0, 0x11650, 0x116c0, 0x116d0, 0x116da, 0x11730, 0x118e0, 0x11950, 0x11bf0,
    0x11c50, 0x11d50, 0x11da0, 0x11f50, 0x16130, 0x16a60, 0x16ac0, 0x16b50, 0x16d70, 0x1ccf0, 0x1d7ce, 0x1d7d8,
    0x1d7e2, 0x1d7ec, 0x1d7f6, 0x1e140, 0x1e2f0, 0x1e4f0, 0x1e5f1, 0x1e950, 0x1fbf0,
];

/// The ASCII digit `c` represents under Unicode category Nd, or `None` if
/// `c` isn't such a digit.
fn nd_digit_value(c: char) -> Option<u8> {
    let cp = c as u32;
    for &zero in DIGIT_BLOCK_ZEROES {
        if cp >= zero && cp < zero + 10 {
            return Some((cp - zero) as u8);
        }
    }
    None
}

fn io_print(vm: &mut Vm, args: &[Value]) -> Result<Value, RuntimeError> {
    let text = vm.to_str(&args[0])?;
    vm.write_stdout(&text);
    vm.write_stdout("\n");
    Ok(Value::None)
}

fn io_write(vm: &mut Vm, args: &[Value]) -> Result<Value, RuntimeError> {
    let text = vm.to_str(&args[0])?;
    vm.write_stdout(&text);
    Ok(Value::None)
}

type PromiseRef = Rc<std::cell::RefCell<super::value::PromiseData>>;

fn promise_arg(vm: &Vm, name: &str, v: &Value) -> Result<PromiseRef, RuntimeError> {
    match v {
        Value::Promise(p) => Ok(p.clone()),
        other => Err(RuntimeError::with_kind(
            format!("{name}: expected a Promise, got {}", super::value::type_name_of(other, &vm.names)),
            ErrorKind::TypeMismatch,
        )),
    }
}

fn is_pending(p: &PromiseRef) -> bool {
    let p = p.borrow();
    p.settled.is_none() && p.failed.is_none()
}

/// M33 (1.10): `input(prompt)`'s native -- writes the prompt (flushed, no
/// newline added), then returns a Promise of the next line of standard
/// input, which fails with an `EndOfInput` struct when there are no more.
fn io_read_line(vm: &mut Vm, args: &[Value]) -> Result<Value, RuntimeError> {
    let prompt = match &args[0] {
        Value::Str(s) => s.clone(),
        other => {
            return Err(RuntimeError::with_kind(
                format!("input: the prompt must be a String, got {}", super::value::type_name_of(other, &vm.names)),
                ErrorKind::TypeMismatch,
            ))
        }
    };
    vm.write_stdout(&prompt);
    vm.flush_stdout();
    let promise = super::value::PromiseData::new_pending();
    vm.read_line(promise.clone());
    Ok(Value::Promise(promise))
}

/// Pre-1.10 files' `input()`: the digits of the first run of them.
fn io_input(vm: &mut Vm, _args: &[Value]) -> Result<Value, RuntimeError> {
    vm.flush_stdout();
    let mut raw = String::new();
    let mut started = false;
    loop {
        match vm.read_stdin_char() {
            None => {
                if !started {
                    return Err(RuntimeError::with_kind("input: end of input", ErrorKind::InputError));
                }
                break;
            }
            Some(ch) => match nd_digit_value(ch) {
                Some(d) => {
                    started = true;
                    raw.push((b'0' + d) as char);
                }
                None => {
                    if started {
                        break;
                    }
                    // else: skip characters until the first digit.
                }
            },
        }
    }
    match Decimal::parse(&raw) {
        Some(d) => Ok(Value::Number(d)),
        None => Err(RuntimeError::with_kind(format!("input: could not parse '{raw}' as a Number"), ErrorKind::InputError)),
    }
}

fn math_sin(_vm: &mut Vm, args: &[Value]) -> Result<Value, RuntimeError> {
    let n = expect_number(&args[0])?;
    let f = n.to_f64().sin();
    Decimal::from_f64(f).map(Value::Number).map_err(|e| RuntimeError::new(e.message()))
}

fn math_cos(_vm: &mut Vm, args: &[Value]) -> Result<Value, RuntimeError> {
    let n = expect_number(&args[0])?;
    let f = n.to_f64().cos();
    Decimal::from_f64(f).map(Value::Number).map_err(|e| RuntimeError::new(e.message()))
}

/// M27 (1.5): a `math.*` native computed in f64, the same way as
/// `math.sin`/`math.cos`; a non-finite result is an `ArgumentError`. Mirrors
/// `mah/natives.py`'s `_float_math`, messages included.
fn float_math(short: &str, args: &[Value], f: impl Fn(&[f64]) -> f64) -> Result<Value, RuntimeError> {
    let mut floats = Vec::with_capacity(args.len());
    for a in args {
        match a {
            Value::Number(n) => floats.push(n.to_f64()),
            other => {
                return Err(RuntimeError::with_kind(
                    format!(
                        "{short}: expected a Number, got {}",
                        super::value::type_name_of(other, &super::value::BuiltinTypeNames::new())
                    ),
                    ErrorKind::TypeMismatch,
                ))
            }
        }
    }
    let result = f(&floats);
    if !result.is_finite() {
        return Err(RuntimeError::with_kind(format!("{short}: argument out of range"), ErrorKind::ArgumentError));
    }
    Decimal::from_f64(result).map(Value::Number).map_err(|e| RuntimeError::new(e.message()))
}

// -- M30 (1.7): reflection and characters -- mirrors mah/natives.py --------

fn value_type_name(vm: &mut Vm, args: &[Value]) -> Result<Value, RuntimeError> {
    let name = match &args[0] {
        Value::None => Rc::from("None"),
        other => super::value::type_name_of(other, &vm.names),
    };
    Ok(Value::Str(Rc::from(super::value::display_name(&name))))
}

/// A struct, or an enum value other than none/Promise, as `(fields, variant)`.
fn record(v: &Value) -> Option<(Vec<(Rc<str>, Value)>, Option<Rc<str>>)> {
    match v {
        Value::Struct(s) => Some((s.borrow().fields.clone(), None)),
        Value::Enum(e) => {
            let b = e.borrow();
            Some((b.fields.clone(), Some(b.variant.clone())))
        }
        _ => None,
    }
}

fn value_fields(_vm: &mut Vm, args: &[Value]) -> Result<Value, RuntimeError> {
    let Some((fields, _)) = record(&args[0]) else { return Ok(Value::None) };
    let map = super::value::MapData::new();
    {
        let mut m = map.borrow_mut();
        for (name, value) in fields {
            let key = Value::Str(name);
            let mk = super::value::map_key(&key).expect("a String is always a Map key");
            m.index_assign(mk, key, value);
        }
    }
    Ok(Value::Map(map))
}

fn value_variant(_vm: &mut Vm, args: &[Value]) -> Result<Value, RuntimeError> {
    match record(&args[0]) {
        Some((_, Some(variant))) => Ok(Value::Str(variant)),
        _ => Ok(Value::None),
    }
}

fn string_arg<'a>(vm: &Vm, name: &str, v: &'a Value) -> Result<&'a Rc<str>, RuntimeError> {
    match v {
        Value::Str(s) => Ok(s),
        other => Err(RuntimeError::with_kind(
            format!("{name}: expected a String, got {}", super::value::type_name_of(other, &vm.names)),
            ErrorKind::TypeMismatch,
        )),
    }
}

fn string_chars(vm: &mut Vm, args: &[Value]) -> Result<Value, RuntimeError> {
    let s = string_arg(vm, "chars", &args[0])?;
    let items: Vec<Value> = s.chars().map(|c| Value::Str(Rc::from(c.to_string().as_str()))).collect();
    Ok(Value::Vector(Rc::new(std::cell::RefCell::new(items))))
}

fn string_code_point(vm: &mut Vm, args: &[Value]) -> Result<Value, RuntimeError> {
    let s = string_arg(vm, "code_point", &args[0])?;
    let mut chars = s.chars();
    match (chars.next(), chars.next()) {
        (Some(c), None) => Ok(Value::Number(Decimal::from_i64(c as i64))),
        _ => Err(RuntimeError::with_kind(
            format!("code_point: expected one character, got {}", s.chars().count()),
            ErrorKind::ArgumentError,
        )),
    }
}

fn string_from_code_point(vm: &mut Vm, args: &[Value]) -> Result<Value, RuntimeError> {
    let n = match &args[0] {
        Value::Number(n) => n,
        other => {
            return Err(RuntimeError::with_kind(
                format!("from_code_point: expected a Number, got {}", super::value::type_name_of(other, &vm.names)),
                ErrorKind::TypeMismatch,
            ))
        }
    };
    let c = if n.is_integer() { n.to_i64().and_then(|i| u32::try_from(i).ok()).and_then(char::from_u32) } else { None };
    match c {
        Some(c) => Ok(Value::Str(Rc::from(c.to_string().as_str()))),
        None => Err(RuntimeError::with_kind("from_code_point: not a Unicode scalar value", ErrorKind::ArgumentError)),
    }
}

// -- M31 (1.8): the shared generator behind std:random ------------------------
//
// xoshiro256** seeded through splitmix64, over a state of four 64-bit words
// kept in a Mah Vector of four whole Numbers -- exactly `mah/natives.py`'s.

fn splitmix64(x: &mut u64) -> u64 {
    *x = x.wrapping_add(0x9E37_79B9_7F4A_7C15);
    let mut z = *x;
    z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
    z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
    z ^ (z >> 31)
}

fn state_from_seed(seed: u64) -> Value {
    let mut x = seed;
    let words: Vec<Value> = (0..4).map(|_| Value::Number(Decimal::from_u64(splitmix64(&mut x)))).collect();
    Value::Vector(Rc::new(std::cell::RefCell::new(words)))
}

/// 64 bits from the operating system's randomness: std's `RandomState` is
/// seeded from it, so hashing nothing with a fresh one gives such bits.
fn fresh_seed() -> u64 {
    use std::hash::{BuildHasher, Hasher};
    std::collections::hash_map::RandomState::new().build_hasher().finish()
}

fn number_arg<'a>(vm: &Vm, name: &str, v: &'a Value) -> Result<&'a Decimal, RuntimeError> {
    match v {
        Value::Number(n) => Ok(n),
        other => Err(RuntimeError::with_kind(
            format!("{name}: expected a Number, got {}", super::value::type_name_of(other, &vm.names)),
            ErrorKind::TypeMismatch,
        )),
    }
}

fn random_seed(vm: &mut Vm, args: &[Value]) -> Result<Value, RuntimeError> {
    let n = number_arg(vm, "seed", &args[0])?;
    match n.to_sign_u64() {
        Some((false, m)) => Ok(state_from_seed(m)),
        Some((true, m)) => Ok(state_from_seed(m.wrapping_neg())),
        None => Err(RuntimeError::with_kind(
            "seed: expected a whole number smaller than 2^64 in size",
            ErrorKind::ArgumentError,
        )),
    }
}

type State = Rc<std::cell::RefCell<Vec<Value>>>;

fn read_state(name: &str, v: &Value) -> Result<(State, [u64; 4]), RuntimeError> {
    let bad = || RuntimeError::with_kind(format!("{name}: not a generator state"), ErrorKind::ArgumentError);
    let Value::Vector(items) = v else { return Err(bad()) };
    let mut s = [0u64; 4];
    {
        let items = items.borrow();
        if items.len() != 4 {
            return Err(bad());
        }
        for (i, w) in items.iter().enumerate() {
            match w {
                Value::Number(n) => match n.to_sign_u64() {
                    Some((false, m)) => s[i] = m,
                    _ => return Err(bad()),
                },
                _ => return Err(bad()),
            }
        }
    }
    if s == [0; 4] {
        return Err(bad());
    }
    Ok((items.clone(), s))
}

/// xoshiro256**'s next output; advances `s` and writes it back to `state`.
fn next_word(state: &State, s: &mut [u64; 4]) -> u64 {
    let result = s[1].wrapping_mul(5).rotate_left(7).wrapping_mul(9);
    let t = s[1] << 17;
    s[2] ^= s[0];
    s[3] ^= s[1];
    s[1] ^= s[2];
    s[0] ^= s[3];
    s[2] ^= t;
    s[3] = s[3].rotate_left(45);
    *state.borrow_mut() = s.iter().map(|&w| Value::Number(Decimal::from_u64(w))).collect();
    result
}

fn random_next(args: &[Value]) -> Result<Value, RuntimeError> {
    let (state, mut s) = read_state("next", &args[0])?;
    Ok(Value::Number(Decimal::from_u64(next_word(&state, &mut s))))
}

/// A uniform whole Number in [0, n) for a whole `n` in [1, 2^64]: outputs
/// at or above the largest multiple of `n` are rejected.
fn random_below(vm: &mut Vm, args: &[Value]) -> Result<Value, RuntimeError> {
    let (state, mut s) = read_state("below", &args[0])?;
    let n = number_arg(vm, "below", &args[1])?;
    let two_64 = Decimal::parse("18446744073709551616").expect("a valid literal");
    let bound: u128 = if *n == two_64 {
        1u128 << 64
    } else {
        match n.to_sign_u64() {
            Some((false, m)) if m >= 1 => m as u128,
            _ => {
                return Err(RuntimeError::with_kind(
                    "below: expected a whole number from 1 to 2^64",
                    ErrorKind::ArgumentError,
                ))
            }
        }
    };
    let limit = (1u128 << 64) - (1u128 << 64) % bound;
    loop {
        let x = next_word(&state, &mut s) as u128;
        if x < limit {
            return Ok(Value::Number(Decimal::from_u64((x % bound) as u64)));
        }
    }
}

// -- M32 (1.9): std:regex's matcher ---------------------------------------------
//
// Patterns arrive in std:regex's canonical form, whose syntax is this
// crate's (docs/MAHC_FORMAT.md #4.4); spans go back as code-point positions,
// exactly as `mah/natives.py` computes them.

fn compiled(vm: &mut Vm, name: &str, v: &Value) -> Result<regex::Regex, RuntimeError> {
    let source = string_arg(vm, name, v)?.clone();
    if let Some(r) = vm.regex_cache.get(&source) {
        return Ok(r.clone());
    }
    let r = regex::RegexBuilder::new(&source)
        .size_limit(1 << 28)
        .nest_limit(1000)
        .build()
        .map_err(|_| RuntimeError::with_kind(format!("{name}: not a canonical pattern"), ErrorKind::ArgumentError))?;
    if vm.regex_cache.len() >= 256 {
        vm.regex_cache.clear();
    }
    vm.regex_cache.insert(source, r.clone());
    Ok(r)
}

/// The byte offset of every code point of `text`, then `text.len()`.
fn char_offsets(text: &str) -> Vec<usize> {
    let mut out: Vec<usize> = text.char_indices().map(|(i, _)| i).collect();
    out.push(text.len());
    out
}

fn to_char(offsets: &[usize], byte: usize) -> Value {
    let i = offsets.binary_search(&byte).expect("a match ends on a char boundary");
    Value::Number(Decimal::from_u64(i as u64))
}

fn spans(offsets: &[usize], caps: &regex::Captures) -> Value {
    let mut out = Vec::with_capacity(caps.len() * 2);
    for g in 0..caps.len() {
        match caps.get(g) {
            Some(m) => {
                out.push(to_char(offsets, m.start()));
                out.push(to_char(offsets, m.end()));
            }
            None => {
                out.push(Value::None);
                out.push(Value::None);
            }
        }
    }
    Value::Vector(Rc::new(std::cell::RefCell::new(out)))
}

fn regex_find(vm: &mut Vm, args: &[Value]) -> Result<Value, RuntimeError> {
    let r = compiled(vm, "find", &args[0])?;
    let text = string_arg(vm, "find", &args[1])?.clone();
    let offsets = char_offsets(&text);
    let start = match &args[2] {
        Value::Number(n) => match n.to_sign_u64() {
            Some((false, i)) if (i as usize) < offsets.len() => i as usize,
            _ => usize::MAX,
        },
        _ => usize::MAX,
    };
    if start == usize::MAX {
        return Err(RuntimeError::with_kind("find: start must be a position in the text", ErrorKind::ArgumentError));
    }
    Ok(match r.captures_at(&text, offsets[start]) {
        Some(caps) => spans(&offsets, &caps),
        None => Value::None,
    })
}

/// Every match, left to right: after a match ending at `e`, the next search
/// starts at `e` -- or one code point later after an empty one.
fn regex_find_all(vm: &mut Vm, args: &[Value]) -> Result<Value, RuntimeError> {
    let r = compiled(vm, "find_all", &args[0])?;
    let text = string_arg(vm, "find_all", &args[1])?.clone();
    let offsets = char_offsets(&text);
    let mut out = Vec::new();
    let mut pos = 0usize; // a byte offset, always on a char boundary
    while pos <= text.len() {
        let Some(caps) = r.captures_at(&text, pos) else { break };
        let m = caps.get(0).expect("group 0 always takes part");
        out.push(spans(&offsets, &caps));
        pos = if m.start() == m.end() {
            match text[m.end()..].chars().next() {
                Some(c) => m.end() + c.len_utf8(),
                None => break,
            }
        } else {
            m.end()
        };
    }
    Ok(Value::Vector(Rc::new(std::cell::RefCell::new(out))))
}

fn time_sleep_async(vm: &mut Vm, args: &[Value]) -> Result<Value, RuntimeError> {
    let ms = expect_number(&args[0])?;
    let promise = super::value::PromiseData::new_pending();
    vm.schedule_timer(ms.to_f64() / 1000.0, promise.clone());
    Ok(Value::Promise(promise))
}

fn expect_number(v: &Value) -> Result<&Decimal, RuntimeError> {
    match v {
        Value::Number(n) => Ok(n),
        other => Err(RuntimeError::new(format!(
            "expected a Number, got {}",
            super::value::type_name_of(other, &super::value::BuiltinTypeNames::new())
        ))),
    }
}

pub fn call_native(vm: &mut Vm, native: NativeFn, args: &[Value]) -> Result<Value, RuntimeError> {
    match native {
        NativeFn::IoPrint => io_print(vm, args),
        NativeFn::IoWrite => io_write(vm, args),
        NativeFn::IoInput => io_input(vm, args),
        NativeFn::MathSin => math_sin(vm, args),
        NativeFn::MathCos => math_cos(vm, args),
        NativeFn::TimeSleepAsync => time_sleep_async(vm, args),
        NativeFn::MathTan => float_math("tan", args, |x| x[0].tan()),
        NativeFn::MathAsin => float_math("asin", args, |x| x[0].asin()),
        NativeFn::MathAcos => float_math("acos", args, |x| x[0].acos()),
        NativeFn::MathAtan => float_math("atan", args, |x| x[0].atan()),
        NativeFn::MathAtan2 => float_math("atan2", args, |x| x[0].atan2(x[1])),
        NativeFn::MathExp => float_math("exp", args, |x| x[0].exp()),
        NativeFn::MathLog => float_math("log", args, |x| x[0].ln()),
        NativeFn::MathLog10 => float_math("log10", args, |x| x[0].log10()),
        NativeFn::ValueTypeName => value_type_name(vm, args),
        NativeFn::ValueFields => value_fields(vm, args),
        NativeFn::ValueVariant => value_variant(vm, args),
        NativeFn::StringChars => string_chars(vm, args),
        NativeFn::StringCodePoint => string_code_point(vm, args),
        NativeFn::StringFromCodePoint => string_from_code_point(vm, args),
        NativeFn::RandomSeed => random_seed(vm, args),
        NativeFn::RandomFresh => Ok(state_from_seed(fresh_seed())),
        NativeFn::RandomNext => random_next(args),
        NativeFn::RandomBelow => random_below(vm, args),
        NativeFn::RegexFind => regex_find(vm, args),
        NativeFn::RegexFindAll => regex_find_all(vm, args),
        NativeFn::IoReadLine => io_read_line(vm, args),
        NativeFn::TimeNowMs => {
            let ms = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_millis() as u64)
                .unwrap_or(0);
            Ok(Value::Number(Decimal::from_u64(ms)))
        }
        NativeFn::TimeMonotonicMs => Ok(Value::Number(Decimal::from_u64(vm.monotonic_ms()))),
        NativeFn::TimeCancel => {
            let p = promise_arg(vm, "cancel", &args[0])?;
            Ok(Value::Bool(vm.cancel_timer(&p)))
        }
        NativeFn::PromiseNew => Ok(Value::Promise(super::value::PromiseData::new_pending())),
        NativeFn::PromiseResolve => {
            let p = promise_arg(vm, "resolve", &args[0])?;
            if !is_pending(&p) {
                return Ok(Value::Bool(false));
            }
            vm.resolve_promise(&p, args[1].clone())?;
            Ok(Value::Bool(true))
        }
        NativeFn::PromiseFail => {
            let p = promise_arg(vm, "fail", &args[0])?;
            if !is_pending(&p) {
                return Ok(Value::Bool(false));
            }
            vm.fail_promise(&p, args[1].clone())?;
            Ok(Value::Bool(true))
        }
        NativeFn::FsReadText => super::fs::read_text(vm, args),
        NativeFn::FsWriteText => super::fs::write_text(vm, args, false),
        NativeFn::FsAppendText => super::fs::write_text(vm, args, true),
        NativeFn::FsInfo => super::fs::info(vm, args),
        NativeFn::FsListDir => super::fs::list_dir(vm, args),
        NativeFn::FsMkdir => super::fs::mkdir(vm, args),
        NativeFn::FsRemove => super::fs::remove(vm, args),
        NativeFn::FsRename => super::fs::rename(vm, args),
        NativeFn::FsCopy => super::fs::copy(vm, args),
        NativeFn::FsTempDir => super::fs::temp_dir(vm),
        NativeFn::FsOpen => super::fs::open(vm, args),
        NativeFn::FsReadLine => super::fs::read_line(vm, args),
        NativeFn::FsReadAll => super::fs::read_all(vm, args),
        NativeFn::FsWrite => super::fs::write(vm, args),
        NativeFn::FsClose => super::fs::close(vm, args),
        NativeFn::ProcessArgs => super::process::args(vm),
        NativeFn::ProcessExit => super::process::exit(vm, args),
        NativeFn::ProcessEnvGet => super::process::env_get(vm, args),
        NativeFn::ProcessEnvSet => super::process::env_set(vm, args),
        NativeFn::ProcessEnvRemove => super::process::env_remove(vm, args),
        NativeFn::ProcessEnvAll => super::process::env_all(vm),
        NativeFn::ProcessCwd => super::process::cwd(),
        NativeFn::ProcessPid => super::process::pid(),
        NativeFn::ProcessPlatform => super::process::platform(),
        NativeFn::ProcessRun => super::process::run(vm, args),
        NativeFn::ReflectTypeOf => super::reflect::type_of(vm, args),
        NativeFn::ReflectSignature => super::reflect::signature(vm, args),
        NativeFn::ReflectSchema => super::reflect::schema(vm, args),
        NativeFn::ReflectMethods => super::reflect::methods(vm, args),
        NativeFn::ReflectImplements => super::reflect::implements(vm, args),
        NativeFn::ReflectConstruct => super::reflect::construct(vm, args),
        NativeFn::ReflectConstructVariant => super::reflect::construct_variant(vm, args),
        NativeFn::ReflectDecorators => super::reflect::decorators(vm, args),
        NativeFn::HooksHas => super::reflect::hooks_has(vm, args),
        NativeFn::HooksAdopt => super::reflect::hooks_adopt(vm, args),
        NativeFn::HooksSameFn => super::reflect::hooks_same_fn(vm, args),
        NativeFn::HooksSetType => super::reflect::hooks_set_type(vm, args),
        NativeFn::HooksSetParam => super::reflect::hooks_set_param(vm, args),
        NativeFn::HooksOf => super::reflect::hooks_of(vm, args),
        NativeFn::HooksGetField => super::reflect::hooks_get_field(vm, args),
        NativeFn::HooksSetField => super::reflect::hooks_set_field(vm, args),
        NativeFn::BytesNew => super::bytes::native_new(vm, args),
        NativeFn::BytesFromVector => super::bytes::native_from_vector(vm, args),
        NativeFn::BytesFromHex => super::bytes::native_from_hex(vm, args),
        NativeFn::BytesFromBase64 => super::bytes::native_from_base64(vm, args),
        NativeFn::FsReadBytes => super::fs::read_bytes(vm, args),
        NativeFn::FsWriteBytes => super::fs::write_bytes(vm, args, false),
        NativeFn::FsAppendBytes => super::fs::write_bytes(vm, args, true),
        NativeFn::FsFileReadBytes => super::fs::file_read_bytes(vm, args),
        NativeFn::FsFileWriteBytes => super::fs::file_write_bytes(vm, args),
        NativeFn::SocketConnect => super::socket::connect(vm, args),
        NativeFn::SocketListen => super::socket::listen(vm, args),
        NativeFn::SocketAccept => super::socket::accept(vm, args),
        NativeFn::SocketSend => super::socket::send(vm, args),
        NativeFn::SocketRecv => super::socket::recv(vm, args),
        NativeFn::SocketShutdown => super::socket::shutdown(vm, args),
        NativeFn::SocketClose => super::socket::close(vm, args),
        NativeFn::SocketStartTls => super::socket::start_tls(vm, args),
    }
}
