//! The native function registry -- docs/MAHC_FORMAT.md #4.4, mirroring
//! `mah/natives.py`. `io.input`'s digit scanning matches Python's
//! `str.isdigit()` restricted to Unicode category Nd (see the spec: Python
//! also accepts ~128 non-Nd characters there, which then fail `Decimal()`
//! anyway -- an accepted, documented corner case).

use crate::decimal::Decimal;

use super::error::RuntimeError;
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

fn io_input(vm: &mut Vm, _args: &[Value]) -> Result<Value, RuntimeError> {
    vm.flush_stdout();
    let mut raw = String::new();
    let mut started = false;
    loop {
        match vm.read_stdin_char() {
            None => {
                if !started {
                    return Err(RuntimeError::new("input: end of input"));
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
        None => Err(RuntimeError::new(format!("input: could not parse '{raw}' as a Number"))),
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
    }
}
