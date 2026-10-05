//! M37 (1.17, docs/MAHC_FORMAT.md #4.4/#6.7/#6.9): the Bytes value's native
//! methods, its Index/IndexAssign, `String.to_bytes`, and the `bytes.*`
//! natives behind std:bytes -- a port of `mah/bytes_methods.py`, which
//! defines every rule and message.

use std::cell::RefCell;
use std::rc::Rc;

use super::error::{ErrorKind, RResult, RuntimeError};
use super::exec::Vm;
use super::methods::{is_range, number_from_usize, seq_position, slice_bounds};
use super::value::{type_name_of, BuiltinTypeNames, BytesRef, EnumData, Value};

const B64: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

pub fn bytes_value(data: Vec<u8>) -> Value {
    Value::Bytes(Rc::new(RefCell::new(data)))
}

fn some(value: Value) -> Value {
    Value::Enum(Rc::new(RefCell::new(EnumData {
        type_name: Rc::from("Option"),
        variant: Rc::from("some"),
        fields: vec![(Rc::from("value"), value)],
        thrown_at: None,
        backtrace: None,
    })))
}

fn str_value(s: &str) -> Value {
    Value::Str(Rc::from(s))
}

/// `value` as a byte; `what` starts the error message ("push: the value",
/// "Bytes item", ...).
pub fn byte_arg(what: &str, value: &Value, names: &BuiltinTypeNames) -> RResult<u8> {
    let n = match value {
        Value::Number(n) => n,
        other => {
            return Err(RuntimeError::with_kind(
                format!("{what} must be a Number, got {}", type_name_of(other, names)),
                ErrorKind::TypeMismatch,
            ))
        }
    };
    match n.to_i64() {
        Some(i) if n.is_integer() && (0..=255).contains(&i) => Ok(i as u8),
        _ => Err(RuntimeError::with_kind(
            format!("{what} must be a whole number from 0 to 255, got {}", n.format()),
            ErrorKind::ArgumentError,
        )),
    }
}

fn bytes_arg<'a>(what: &str, value: &'a Value, names: &BuiltinTypeNames) -> RResult<&'a BytesRef> {
    match value {
        Value::Bytes(b) => Ok(b),
        other => Err(RuntimeError::with_kind(
            format!("{what} must be Bytes, got {}", type_name_of(other, names)),
            ErrorKind::TypeMismatch,
        )),
    }
}

fn string_arg<'a>(what: &str, value: &'a Value, names: &BuiltinTypeNames) -> RResult<&'a Rc<str>> {
    match value {
        Value::Str(s) => Ok(s),
        other => Err(RuntimeError::with_kind(
            format!("{what} must be a String, got {}", type_name_of(other, names)),
            ErrorKind::TypeMismatch,
        )),
    }
}

// -- formatting -------------------------------------------------------------------

/// `Bytes[68 69]`: each byte as two lowercase hex digits.
pub fn to_string(data: &[u8]) -> String {
    let parts: Vec<String> = data.iter().map(|b| format!("{b:02x}")).collect();
    format!("Bytes[{}]", parts.join(" "))
}

/// `a + b`: a new Bytes.
pub fn concat(a: &BytesRef, b: &BytesRef) -> Value {
    let mut out = a.borrow().clone();
    out.extend_from_slice(&b.borrow());
    bytes_value(out)
}

// -- Index / IndexAssign ------------------------------------------------------------

pub fn index(b: &BytesRef, i: &Value, names: &BuiltinTypeNames) -> RResult<Value> {
    let data = b.borrow();
    if let Some(r) = is_range(i) {
        let (start, stop) = slice_bounds(data.len(), &r.borrow(), "Bytes", names)?;
        return Ok(bytes_value(if start < stop { data[start..stop].to_vec() } else { Vec::new() }));
    }
    Ok(match seq_position(data.len(), i, "Bytes", names)? {
        Some(pos) => number_from_usize(data[pos] as usize),
        None => Value::None,
    })
}

pub fn index_assign(b: &BytesRef, i: &Value, value: &Value, names: &BuiltinTypeNames) -> RResult<Value> {
    if is_range(i).is_some() {
        return Err(RuntimeError::with_kind(
            "Can't assign to a Bytes slice (b[a..b] = ...); assign items one at a time",
            ErrorKind::TypeMismatch,
        ));
    }
    let len = b.borrow().len();
    match seq_position(len, i, "Bytes", names)? {
        Some(pos) => {
            let byte = byte_arg("Bytes item", value, names)?;
            b.borrow_mut()[pos] = byte;
            Ok(Value::None)
        }
        None => {
            let shown = match i {
                Value::Number(n) => n.format(),
                other => type_name_of(other, names).to_string(),
            };
            Err(RuntimeError::with_kind(
                format!("Bytes index {shown} is out of range for Bytes of length {len} (use push to add items)"),
                ErrorKind::IndexOutOfRange,
            ))
        }
    }
}

// -- methods ----------------------------------------------------------------------------

#[derive(Clone, Copy)]
pub enum BytesMethod {
    Len,
    Push,
    Pop,
    Extend,
    Copy,
    ToVector,
    ToText,
    ToTextLossy,
    ToHex,
    ToBase64,
    IndexOf,
}

impl BytesMethod {
    pub fn required_arity(self) -> usize {
        match self {
            BytesMethod::Push | BytesMethod::Extend | BytesMethod::IndexOf => 1,
            _ => 0,
        }
    }
}

pub fn call_method(method: BytesMethod, b: &BytesRef, args: &[Value], names: &BuiltinTypeNames) -> RResult<Value> {
    Ok(match method {
        BytesMethod::Len => number_from_usize(b.borrow().len()),
        BytesMethod::Push => {
            let byte = byte_arg("push: the value", &args[0], names)?;
            b.borrow_mut().push(byte);
            Value::None
        }
        BytesMethod::Pop => match b.borrow_mut().pop() {
            Some(x) => number_from_usize(x as usize),
            None => Value::None,
        },
        BytesMethod::Extend => {
            // copied first: `b.extend(b)` borrows the same buffer twice
            let other = bytes_arg("extend: other", &args[0], names)?.borrow().clone();
            b.borrow_mut().extend_from_slice(&other);
            Value::None
        }
        BytesMethod::Copy => bytes_value(b.borrow().clone()),
        BytesMethod::ToVector => {
            let items: Vec<Value> = b.borrow().iter().map(|x| number_from_usize(*x as usize)).collect();
            Value::Vector(Rc::new(RefCell::new(items)))
        }
        BytesMethod::ToText => match std::str::from_utf8(&b.borrow()) {
            Ok(s) => some(str_value(s)),
            Err(_) => Value::None,
        },
        BytesMethod::ToTextLossy => str_value(&String::from_utf8_lossy(&b.borrow())),
        BytesMethod::ToHex => {
            let data = b.borrow();
            let mut out = String::with_capacity(data.len() * 2);
            for x in data.iter() {
                out.push_str(&format!("{x:02x}"));
            }
            str_value(&out)
        }
        BytesMethod::ToBase64 => str_value(&encode_base64(&b.borrow())),
        BytesMethod::IndexOf => {
            let needle = bytes_arg("index_of: needle", &args[0], names)?.borrow().clone();
            let data = b.borrow();
            let found = if needle.is_empty() {
                Some(0)
            } else if needle.len() > data.len() {
                None
            } else {
                data.windows(needle.len()).position(|w| w == needle.as_slice())
            };
            match found {
                Some(pos) => some(number_from_usize(pos)),
                None => Value::None,
            }
        }
    })
}

/// `String.to_bytes()`: the text's UTF-8 encoding.
pub fn string_to_bytes(s: &str) -> Value {
    bytes_value(s.as_bytes().to_vec())
}

// -- encoding and decoding ----------------------------------------------------------------

fn encode_base64(data: &[u8]) -> String {
    let mut out = String::with_capacity(data.len().div_ceil(3) * 4);
    for chunk in data.chunks(3) {
        let mut n: u32 = 0;
        for (i, x) in chunk.iter().enumerate() {
            n |= (*x as u32) << (16 - 8 * i);
        }
        for i in 0..4 {
            if i <= chunk.len() {
                out.push(B64[((n >> (18 - 6 * i)) & 63) as usize] as char);
            } else {
                out.push('=');
            }
        }
    }
    out
}

fn hex_digit(c: u8) -> Option<u8> {
    match c {
        b'0'..=b'9' => Some(c - b'0'),
        b'a'..=b'f' => Some(c - b'a' + 10),
        b'A'..=b'F' => Some(c - b'A' + 10),
        _ => None,
    }
}

fn decode_hex(text: &str) -> Option<Vec<u8>> {
    let b = text.as_bytes();
    if b.len() % 2 != 0 {
        return None;
    }
    b.chunks(2).map(|p| Some(hex_digit(p[0])? << 4 | hex_digit(p[1])?)).collect()
}

fn b64_digit(c: u8) -> Option<u32> {
    B64.iter().position(|x| *x == c).map(|i| i as u32)
}

/// The standard alphabet with `=` padding, required; the unused bits of the
/// last group are ignored.
fn decode_base64(text: &str) -> Option<Vec<u8>> {
    let b = text.as_bytes();
    if b.len() % 4 != 0 {
        return None;
    }
    let mut out = Vec::with_capacity(b.len() / 4 * 3);
    let groups = b.len() / 4;
    for (g, quad) in b.chunks(4).enumerate() {
        let pad = if g + 1 == groups { quad.iter().rev().take_while(|c| **c == b'=').count() } else { 0 };
        if pad > 2 {
            return None;
        }
        let mut n: u32 = 0;
        for c in &quad[..4 - pad] {
            n = n << 6 | b64_digit(*c)?;
        }
        n <<= 6 * pad as u32;
        out.extend_from_slice(&[(n >> 16) as u8, (n >> 8) as u8, n as u8][..3 - pad]);
    }
    Some(out)
}

// -- the bytes.* natives (1.17) ---------------------------------------------------------

pub fn native_new(vm: &mut Vm, args: &[Value]) -> RResult<Value> {
    let names = &vm.names;
    let size = match &args[0] {
        Value::Number(n) => n,
        other => {
            return Err(RuntimeError::with_kind(
                format!("new: size must be a Number, got {}", type_name_of(other, names)),
                ErrorKind::TypeMismatch,
            ))
        }
    };
    let size = match size.to_i64() {
        Some(i) if size.is_integer() && i >= 0 => i as usize,
        _ => {
            return Err(RuntimeError::with_kind(
                format!("new: size must be a whole number of at least 0, got {}", size.format()),
                ErrorKind::ArgumentError,
            ))
        }
    };
    let fill = byte_arg("new: fill", &args[1], names)?;
    Ok(bytes_value(vec![fill; size]))
}

pub fn native_from_vector(vm: &mut Vm, args: &[Value]) -> RResult<Value> {
    let names = &vm.names;
    let Value::Vector(items) = &args[0] else {
        return Err(RuntimeError::with_kind(
            format!("from_vector: items must be a Vector, got {}", type_name_of(&args[0], names)),
            ErrorKind::TypeMismatch,
        ));
    };
    let items = items.borrow();
    let mut out = Vec::with_capacity(items.len());
    for (i, v) in items.iter().enumerate() {
        out.push(byte_arg(&format!("from_vector: item {i}"), v, names)?);
    }
    Ok(bytes_value(out))
}

pub fn native_from_hex(vm: &mut Vm, args: &[Value]) -> RResult<Value> {
    let text = string_arg("from_hex: text", &args[0], &vm.names)?;
    Ok(decode_hex(text).map(|d| some(bytes_value(d))).unwrap_or(Value::None))
}

pub fn native_from_base64(vm: &mut Vm, args: &[Value]) -> RResult<Value> {
    let text = string_arg("from_base64: text", &args[0], &vm.names)?;
    Ok(decode_base64(text).map(|d| some(bytes_value(d))).unwrap_or(Value::None))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn base64_round_trips() {
        for (raw, text) in [("", ""), ("f", "Zg=="), ("fo", "Zm8="), ("foo", "Zm9v"), ("foob", "Zm9vYg==")] {
            assert_eq!(encode_base64(raw.as_bytes()), text);
            assert_eq!(decode_base64(text).unwrap(), raw.as_bytes());
        }
        assert_eq!(decode_base64("Zg="), None);
        assert_eq!(decode_base64("Z==="), None);
        assert_eq!(decode_base64("Zg==Zg=="), None);
        assert_eq!(decode_base64("Zh=="), Some(b"f".to_vec())); // unused bits ignored
    }

    #[test]
    fn hex_decodes_either_case() {
        assert_eq!(decode_hex("00fFaB"), Some(vec![0, 255, 171]));
        assert_eq!(decode_hex("abc"), None);
        assert_eq!(decode_hex("zz"), None);
    }
}
