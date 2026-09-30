//! M36 (1.13, docs/MAHC_FORMAT.md #4.4): std:process's natives -- a port of
//! `mah/process_natives.py`, which defines every rule and message.
//!
//! The VM owns an environment table (a snapshot of the process's
//! environment when it started); `env_*` touch only that, never the real
//! environment, and `run` starts its child with exactly that table plus the
//! call's overrides. `run` is asynchronous like std:fs's natives.

use std::collections::BTreeMap;
use std::io::{self, Write};
use std::process::{Command, Stdio};
use std::rc::Rc;

use crate::decimal::Decimal;

use super::error::{ErrorKind, RuntimeError};
use super::exec::Vm;
use super::fs::{failure, fixed, ok, IoValue};
use super::value::{type_name_of, MapData, MapKey, PromiseData, Value};

/// The process's environment as `name -> value`, without the entries whose
/// name or value isn't valid Unicode.
pub fn snapshot_environment() -> BTreeMap<String, String> {
    let mut env = BTreeMap::new();
    for (name, value) in std::env::vars_os() {
        if let (Ok(name), Ok(value)) = (name.into_string(), value.into_string()) {
            env.insert(name, value);
        }
    }
    env
}

fn string(vm: &Vm, func: &str, what: &str, v: &Value) -> Result<String, RuntimeError> {
    match v {
        Value::Str(s) => Ok(s.to_string()),
        other => Err(RuntimeError::with_kind(
            format!("{func}: {what} must be a String, got {}", type_name_of(other, &vm.names)),
            ErrorKind::TypeMismatch,
        )),
    }
}

/// A String without NUL.
fn text(vm: &Vm, func: &str, what: &str, v: &Value) -> Result<String, RuntimeError> {
    let s = string(vm, func, what, v)?;
    if s.contains('\0') {
        return Err(RuntimeError::with_kind(
            format!("{func}: {what} must not contain a NUL character"),
            ErrorKind::ArgumentError,
        ));
    }
    Ok(s)
}

/// An environment variable name: non-empty, no `=`, no NUL.
fn name(vm: &Vm, func: &str, what: &str, v: &Value) -> Result<String, RuntimeError> {
    let s = text(vm, func, what, v)?;
    if s.is_empty() {
        return Err(RuntimeError::with_kind(format!("{func}: {what} must not be empty"), ErrorKind::ArgumentError));
    }
    if s.contains('=') {
        return Err(RuntimeError::with_kind(
            format!("{func}: {what} must not contain \"=\""),
            ErrorKind::ArgumentError,
        ));
    }
    Ok(s)
}

fn str_value(s: &str) -> Value {
    Value::Str(Rc::from(s))
}

pub fn args(vm: &mut Vm) -> Result<Value, RuntimeError> {
    let items: Vec<Value> = vm.args.iter().map(|a| str_value(a)).collect();
    Ok(Value::Vector(Rc::new(std::cell::RefCell::new(items))))
}

pub fn exit(vm: &mut Vm, args: &[Value]) -> Result<Value, RuntimeError> {
    let code = match &args[0] {
        Value::Number(n) if n.is_integer() => n.to_i64().filter(|c| (0..=255).contains(c)),
        _ => None,
    };
    let Some(code) = code else {
        return Err(RuntimeError::with_kind(
            "exit code must be a whole number from 0 to 255",
            ErrorKind::ArgumentError,
        ));
    };
    vm.exit_program(code as i32)
}

pub fn env_get(vm: &mut Vm, args: &[Value]) -> Result<Value, RuntimeError> {
    let n = name(vm, "env_get", "name", &args[0])?;
    Ok(vm.env.get(&n).map_or(Value::None, |v| str_value(v)))
}

pub fn env_set(vm: &mut Vm, args: &[Value]) -> Result<Value, RuntimeError> {
    let n = name(vm, "env_set", "name", &args[0])?;
    let v = text(vm, "env_set", "value", &args[1])?;
    vm.env.insert(n, v);
    Ok(Value::None)
}

pub fn env_remove(vm: &mut Vm, args: &[Value]) -> Result<Value, RuntimeError> {
    let n = name(vm, "env_remove", "name", &args[0])?;
    vm.env.remove(&n);
    Ok(Value::None)
}

/// The table as a Map, keys in sorted (code point) order.
pub fn env_all(vm: &mut Vm) -> Result<Value, RuntimeError> {
    let map = MapData::new();
    for (k, v) in vm.env.iter() {
        map.borrow_mut().index_assign(MapKey::Str(Rc::from(k.as_str())), str_value(k), str_value(v));
    }
    Ok(Value::Map(map))
}

pub fn cwd() -> Result<Value, RuntimeError> {
    match std::env::current_dir() {
        Ok(p) => Ok(str_value(&p.to_string_lossy())),
        Err(e) => Err(RuntimeError::new(format!("cwd: {}", os_text(&e)))),
    }
}

pub fn pid() -> Result<Value, RuntimeError> {
    Ok(Value::Number(Decimal::from_u64(std::process::id() as u64)))
}

pub fn platform() -> Result<Value, RuntimeError> {
    Ok(str_value(std::env::consts::OS))
}

/// The OS's own text for an error (strerror, without Rust's " (os error N)").
fn os_text(e: &io::Error) -> String {
    let text = e.to_string();
    match text.rfind(" (os error ") {
        Some(i) => text[..i].to_string(),
        None => text,
    }
}

fn start_failure(e: &io::Error) -> IoValue {
    match e.kind() {
        io::ErrorKind::NotFound => fixed("not_found"),
        io::ErrorKind::PermissionDenied => fixed("permission_denied"),
        _ => failure("other", &os_text(e)),
    }
}

pub fn run(vm: &mut Vm, args: &[Value]) -> Result<Value, RuntimeError> {
    let program = text(vm, "run", "program", &args[0])?;
    if program.is_empty() {
        return Err(RuntimeError::with_kind("run: program must not be empty", ErrorKind::ArgumentError));
    }
    let Value::Vector(argv) = &args[1] else {
        return Err(RuntimeError::with_kind(
            format!("run: args must be a Vector of Strings, got {}", type_name_of(&args[1], &vm.names)),
            ErrorKind::TypeMismatch,
        ));
    };
    let mut arguments = Vec::new();
    for a in argv.borrow().iter() {
        arguments.push(text(vm, "run", "argument", a)?);
    }
    let cwd = match &args[2] {
        Value::None => None,
        v => Some(text(vm, "run", "cwd", v)?),
    };
    let mut env = vm.env.clone();
    match &args[3] {
        Value::None => {}
        Value::Map(m) => {
            for (k, v) in m.borrow().iter_ordered() {
                let k = name(vm, "run", "env name", k)?;
                let v = text(vm, "run", "env value", v)?;
                env.insert(k, v);
            }
        }
        other => {
            return Err(RuntimeError::with_kind(
                format!("run: env must be a Map of Strings or none, got {}", type_name_of(other, &vm.names)),
                ErrorKind::TypeMismatch,
            ));
        }
    }
    let stdin = text(vm, "run", "stdin", &args[4])?;

    let promise = PromiseData::new_pending();
    vm.submit(promise.clone(), Box::new(move || run_job(program, arguments, cwd, env, stdin)));
    Ok(Value::Promise(promise))
}

fn run_job(
    program: String,
    arguments: Vec<String>,
    cwd: Option<String>,
    env: BTreeMap<String, String>,
    stdin: String,
) -> IoValue {
    let mut cmd = Command::new(&program);
    cmd.args(&arguments).env_clear().envs(&env);
    if let Some(dir) = &cwd {
        cmd.current_dir(dir);
    }
    cmd.stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped());
    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(e) => return start_failure(&e),
    };
    // Written from its own thread (so a child that fills its output pipes
    // before reading its input can't deadlock us); dropping the pipe closes it.
    let writer = child.stdin.take().map(|mut pipe| {
        std::thread::spawn(move || {
            let _ = pipe.write_all(stdin.as_bytes());
        })
    });
    let output = child.wait_with_output();
    if let Some(w) = writer {
        let _ = w.join();
    }
    let output = match output {
        Ok(o) => o,
        Err(e) => return start_failure(&e),
    };
    let code = exit_code(&output.status);
    ok(IoValue::List(vec![
        IoValue::Num(code),
        IoValue::Str(String::from_utf8_lossy(&output.stdout).into_owned()),
        IoValue::Str(String::from_utf8_lossy(&output.stderr).into_owned()),
    ]))
}

/// The exit code; 128 + N for a child killed by signal N.
fn exit_code(status: &std::process::ExitStatus) -> u64 {
    if let Some(code) = status.code() {
        return code as u32 as u64;
    }
    #[cfg(unix)]
    {
        use std::os::unix::process::ExitStatusExt;
        if let Some(sig) = status.signal() {
            return 128 + sig as u64;
        }
    }
    1
}
