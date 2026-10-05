//! M35 (1.12, docs/MAHC_FORMAT.md #4.4): std:fs's natives -- a port of
//! `mah/fs_natives.py`, which defines every rule and message.
//!
//! Each native returns a Promise at once and does its blocking work on a
//! worker thread (`Vm::submit`), settling it with a result Vector: `[true,
//! value]`, or `[false, kind, description]`. Workers only produce plain
//! data (`IoValue`); the VM turns it into Mah values on its own thread.
//! Open files live in a table of ids shared with the workers.

use std::collections::HashMap;
use std::fs::{self, File, OpenOptions};
use std::io::{self, BufRead, BufReader, Read, Write};
use std::path::Path;
use std::rc::Rc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

use crate::decimal::Decimal;

use super::error::{ErrorKind, RuntimeError};
use super::exec::Vm;
use super::value::{type_name_of, PromiseData, Value};

/// A job's result, as data that can cross threads.
pub enum IoValue {
    None,
    Bool(bool),
    Str(String),
    Num(u64),
    List(Vec<IoValue>),
    /// M37: binary data, a Bytes value.
    Bytes(Vec<u8>),
}

impl IoValue {
    pub fn into_value(self) -> Value {
        match self {
            IoValue::None => Value::None,
            IoValue::Bool(b) => Value::Bool(b),
            IoValue::Str(s) => Value::Str(Rc::from(s.as_str())),
            IoValue::Num(n) => Value::Number(Decimal::from_u64(n)),
            IoValue::List(items) => {
                let items: Vec<Value> = items.into_iter().map(IoValue::into_value).collect();
                Value::Vector(Rc::new(std::cell::RefCell::new(items)))
            }
            IoValue::Bytes(data) => super::bytes::bytes_value(data),
        }
    }
}

enum FileState {
    Reader(BufReader<File>),
    /// Unbuffered: each write reaches the file at once, as in Python.
    Writer(File),
}

type OpenFile = Arc<Mutex<FileState>>;

/// The VM's handle table: open files by id (ids start at 1).
#[derive(Default, Clone)]
pub struct FileTable {
    files: Arc<Mutex<HashMap<u64, OpenFile>>>,
    next: Arc<AtomicU64>,
}

impl FileTable {
    fn add(&self, state: FileState) -> u64 {
        let id = self.next.fetch_add(1, Ordering::SeqCst) + 1;
        self.files.lock().expect("file table").insert(id, Arc::new(Mutex::new(state)));
        id
    }
    fn get(&self, id: u64) -> Option<OpenFile> {
        self.files.lock().expect("file table").get(&id).cloned()
    }
    fn remove(&self, id: u64) -> Option<OpenFile> {
        self.files.lock().expect("file table").remove(&id)
    }
}

pub(super) fn ok(value: IoValue) -> IoValue {
    IoValue::List(vec![IoValue::Bool(true), value])
}

pub(super) fn failure(kind: &str, description: &str) -> IoValue {
    IoValue::List(vec![IoValue::Bool(false), IoValue::Str(kind.to_string()), IoValue::Str(description.to_string())])
}

pub(super) fn fixed(kind: &str) -> IoValue {
    let description = match kind {
        "not_found" => "no such file or directory",
        "permission_denied" => "permission denied",
        "already_exists" => "already exists",
        "is_a_directory" => "is a directory",
        "not_a_directory" => "not a directory",
        "directory_not_empty" => "directory not empty",
        "invalid_utf8" => "not valid UTF-8 text",
        "closed" => "the file is closed",
        _ => "",
    };
    failure(kind, description)
}

/// A failure's kind and description, like `fs_natives._classify`: "other"
/// carries the OS's own text (strerror, without Rust's " (os error N)").
fn classify(e: &io::Error) -> IoValue {
    match e.kind() {
        io::ErrorKind::NotFound => fixed("not_found"),
        io::ErrorKind::PermissionDenied => fixed("permission_denied"),
        io::ErrorKind::AlreadyExists => fixed("already_exists"),
        io::ErrorKind::IsADirectory => fixed("is_a_directory"),
        io::ErrorKind::NotADirectory => fixed("not_a_directory"),
        io::ErrorKind::DirectoryNotEmpty => fixed("directory_not_empty"),
        io::ErrorKind::InvalidData => fixed("invalid_utf8"),
        _ => {
            let text = e.to_string();
            let text = match text.rfind(" (os error ") {
                Some(i) => text[..i].to_string(),
                None => text,
            };
            failure("other", &text)
        }
    }
}

fn guarded(job: impl FnOnce() -> io::Result<IoValue>) -> IoValue {
    match job() {
        Ok(v) => ok(v),
        Err(e) => classify(&e),
    }
}

fn string_arg(vm: &Vm, name: &str, what: &str, v: &Value) -> Result<String, RuntimeError> {
    match v {
        Value::Str(s) => Ok(s.to_string()),
        other => Err(RuntimeError::with_kind(
            format!("{name}: {what} must be a String, got {}", type_name_of(other, &vm.names)),
            ErrorKind::TypeMismatch,
        )),
    }
}

fn spawn(vm: &mut Vm, job: impl FnOnce() -> io::Result<IoValue> + Send + 'static) -> Value {
    let promise = PromiseData::new_pending();
    vm.submit(promise.clone(), Box::new(move || guarded(job)));
    Value::Promise(promise)
}

fn utf8(bytes: Vec<u8>) -> io::Result<String> {
    String::from_utf8(bytes).map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "invalid UTF-8"))
}

fn is_a_directory() -> io::Error {
    io::Error::from(io::ErrorKind::IsADirectory)
}

fn other(text: &str) -> io::Error {
    io::Error::other(text.to_string())
}

// -- whole files and paths ------------------------------------------------------

pub fn read_text(vm: &mut Vm, args: &[Value]) -> Result<Value, RuntimeError> {
    let path = string_arg(vm, "read_text", "path", &args[0])?;
    Ok(spawn(vm, move || Ok(IoValue::Str(utf8(fs::read(&path)?)?))))
}

pub fn write_text(vm: &mut Vm, args: &[Value], append: bool) -> Result<Value, RuntimeError> {
    let name = if append { "append_text" } else { "write_text" };
    let path = string_arg(vm, name, "path", &args[0])?;
    let text = string_arg(vm, name, "text", &args[1])?;
    Ok(spawn(vm, move || {
        let mut f = if append {
            OpenOptions::new().append(true).create(true).open(&path)?
        } else {
            File::create(&path)?
        };
        f.write_all(text.as_bytes())?;
        Ok(IoValue::None)
    }))
}

/// M37: a Bytes argument, copied now (on the VM's thread) so the worker
/// never sees later changes to it.
fn bytes_arg(vm: &Vm, name: &str, v: &Value) -> Result<Vec<u8>, RuntimeError> {
    match v {
        Value::Bytes(b) => Ok(b.borrow().clone()),
        other => Err(RuntimeError::with_kind(
            format!("{name}: data must be Bytes, got {}", type_name_of(other, &vm.names)),
            ErrorKind::TypeMismatch,
        )),
    }
}

/// M37 (1.17): the whole file as Bytes.
pub fn read_bytes(vm: &mut Vm, args: &[Value]) -> Result<Value, RuntimeError> {
    let path = string_arg(vm, "read_bytes", "path", &args[0])?;
    Ok(spawn(vm, move || Ok(IoValue::Bytes(fs::read(&path)?))))
}

pub fn write_bytes(vm: &mut Vm, args: &[Value], append: bool) -> Result<Value, RuntimeError> {
    let name = if append { "append_bytes" } else { "write_bytes" };
    let path = string_arg(vm, name, "path", &args[0])?;
    let data = bytes_arg(vm, name, &args[1])?;
    Ok(spawn(vm, move || {
        let mut f = if append {
            OpenOptions::new().append(true).create(true).open(&path)?
        } else {
            File::create(&path)?
        };
        f.write_all(&data)?;
        Ok(IoValue::None)
    }))
}

/// `[kind ("file", "dir" or "other"), size in bytes, modified (ms since
/// 1970, 0 before it)]`, following symlinks.
pub fn info(vm: &mut Vm, args: &[Value]) -> Result<Value, RuntimeError> {
    let path = string_arg(vm, "info", "path", &args[0])?;
    Ok(spawn(vm, move || {
        let m = fs::metadata(&path)?;
        let kind = if m.is_dir() {
            "dir"
        } else if m.is_file() {
            "file"
        } else {
            "other"
        };
        let modified = m
            .modified()
            .ok()
            .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
            .map(|d| d.as_millis() as u64)
            .unwrap_or(0);
        Ok(IoValue::List(vec![IoValue::Str(kind.to_string()), IoValue::Num(m.len()), IoValue::Num(modified)]))
    }))
}

pub fn list_dir(vm: &mut Vm, args: &[Value]) -> Result<Value, RuntimeError> {
    let path = string_arg(vm, "list_dir", "path", &args[0])?;
    Ok(spawn(vm, move || {
        let mut names = Vec::new();
        for entry in fs::read_dir(&path)? {
            names.push(entry?.file_name().to_string_lossy().into_owned());
        }
        names.sort();
        Ok(IoValue::List(names.into_iter().map(IoValue::Str).collect()))
    }))
}

pub fn mkdir(vm: &mut Vm, args: &[Value]) -> Result<Value, RuntimeError> {
    let path = string_arg(vm, "mkdir", "path", &args[0])?;
    let parents = matches!(args[1], Value::Bool(true));
    Ok(spawn(vm, move || {
        if parents {
            fs::create_dir_all(&path)?;
        } else {
            fs::create_dir(&path)?;
        }
        Ok(IoValue::None)
    }))
}

/// A file, or a directory: an empty one, or with `recursive` a whole tree.
/// Symlinks themselves are removed, never followed.
pub fn remove(vm: &mut Vm, args: &[Value]) -> Result<Value, RuntimeError> {
    let path = string_arg(vm, "remove", "path", &args[0])?;
    let recursive = matches!(args[1], Value::Bool(true));
    Ok(spawn(vm, move || {
        let is_dir = fs::symlink_metadata(&path).map(|m| m.is_dir()).unwrap_or(false);
        if is_dir {
            if recursive {
                fs::remove_dir_all(&path)?;
            } else {
                fs::remove_dir(&path)?;
            }
        } else {
            fs::remove_file(&path)?;
        }
        Ok(IoValue::None)
    }))
}

pub fn rename(vm: &mut Vm, args: &[Value]) -> Result<Value, RuntimeError> {
    let from = string_arg(vm, "rename", "from", &args[0])?;
    let to = string_arg(vm, "rename", "to", &args[1])?;
    Ok(spawn(vm, move || {
        fs::rename(&from, &to)?;
        Ok(IoValue::None)
    }))
}

/// A file's contents to `to` (replacing it); a directory is refused.
pub fn copy(vm: &mut Vm, args: &[Value]) -> Result<Value, RuntimeError> {
    let from = string_arg(vm, "copy", "from", &args[0])?;
    let to = string_arg(vm, "copy", "to", &args[1])?;
    Ok(spawn(vm, move || {
        if Path::new(&from).is_dir() {
            return Err(is_a_directory());
        }
        let data = fs::read(&from)?;
        fs::write(&to, data)?;
        Ok(IoValue::None)
    }))
}

/// A new, empty directory in the system's temporary directory.
pub fn temp_dir(vm: &mut Vm) -> Result<Value, RuntimeError> {
    Ok(spawn(vm, move || {
        use std::hash::{BuildHasher, Hasher};
        loop {
            let n = std::collections::hash_map::RandomState::new().build_hasher().finish();
            let path = std::env::temp_dir().join(format!("mah-{:08x}", n & 0xffff_ffff));
            match fs::create_dir(&path) {
                Ok(()) => return Ok(IoValue::Str(path.to_string_lossy().into_owned())),
                Err(e) if e.kind() == io::ErrorKind::AlreadyExists => continue,
                Err(e) => return Err(e),
            }
        }
    }))
}

// -- open files ------------------------------------------------------------------

pub fn open(vm: &mut Vm, args: &[Value]) -> Result<Value, RuntimeError> {
    let path = string_arg(vm, "open", "path", &args[0])?;
    let mode = string_arg(vm, "open", "mode", &args[1])?;
    if !matches!(mode.as_str(), "r" | "w" | "a") {
        return Err(RuntimeError::with_kind(
            format!("open: mode must be \"r\", \"w\" or \"a\", got \"{mode}\""),
            ErrorKind::ArgumentError,
        ));
    }
    let table = vm.files().clone();
    Ok(spawn(vm, move || {
        let state = match mode.as_str() {
            "r" => {
                if Path::new(&path).is_dir() {
                    return Err(is_a_directory());
                }
                FileState::Reader(BufReader::new(File::open(&path)?))
            }
            "w" => FileState::Writer(File::create(&path)?),
            _ => FileState::Writer(OpenOptions::new().append(true).create(true).open(&path)?),
        };
        Ok(IoValue::Num(table.add(state)))
    }))
}

fn file_id(vm: &Vm, name: &str, v: &Value) -> Result<Option<u64>, RuntimeError> {
    match v {
        Value::Number(n) => Ok(match n.to_sign_u64() {
            Some((false, id)) => Some(id),
            _ => None,
        }),
        other => Err(RuntimeError::with_kind(
            format!("{name}: expected a file id, got {}", type_name_of(other, &vm.names)),
            ErrorKind::TypeMismatch,
        )),
    }
}

fn closed() -> Value {
    let promise = PromiseData::new_pending();
    promise.borrow_mut().settled = Some(fixed("closed").into_value());
    Value::Promise(promise)
}

/// The open file behind `args[0]`, or `None` when it's closed or unknown.
fn open_file(vm: &Vm, name: &str, args: &[Value]) -> Result<Option<OpenFile>, RuntimeError> {
    Ok(file_id(vm, name, &args[0])?.and_then(|id| vm.files().get(id)))
}

/// The next line without its `\n` (or `\r\n`), or `none` at the end.
pub fn read_line(vm: &mut Vm, args: &[Value]) -> Result<Value, RuntimeError> {
    let Some(file) = open_file(vm, "read_line", args)? else { return Ok(closed()) };
    Ok(spawn(vm, move || {
        let mut state = file.lock().expect("file");
        let FileState::Reader(reader) = &mut *state else { return Err(other("the file isn't open for reading")) };
        let mut line = Vec::new();
        if reader.read_until(b'\n', &mut line)? == 0 {
            return Ok(IoValue::None);
        }
        if line.last() == Some(&b'\n') {
            line.pop();
            if line.last() == Some(&b'\r') {
                line.pop();
            }
        }
        Ok(IoValue::Str(utf8(line)?))
    }))
}

pub fn read_all(vm: &mut Vm, args: &[Value]) -> Result<Value, RuntimeError> {
    let Some(file) = open_file(vm, "read_all", args)? else { return Ok(closed()) };
    Ok(spawn(vm, move || {
        let mut state = file.lock().expect("file");
        let FileState::Reader(reader) = &mut *state else { return Err(other("the file isn't open for reading")) };
        let mut data = Vec::new();
        reader.read_to_end(&mut data)?;
        Ok(IoValue::Str(utf8(data)?))
    }))
}

pub fn write(vm: &mut Vm, args: &[Value]) -> Result<Value, RuntimeError> {
    let text = string_arg(vm, "write", "text", &args[1])?;
    let Some(file) = open_file(vm, "write", args)? else { return Ok(closed()) };
    Ok(spawn(vm, move || {
        let mut state = file.lock().expect("file");
        let FileState::Writer(f) = &mut *state else { return Err(other("the file isn't open for writing")) };
        f.write_all(text.as_bytes())?;
        Ok(IoValue::None)
    }))
}

/// `file_read_bytes`'s `max`: `none` (to the end) or a whole Number >= 0.
fn max_bytes(vm: &Vm, v: &Value) -> Result<Option<usize>, RuntimeError> {
    match v {
        Value::None => Ok(None),
        Value::Number(n) => match n.to_i64() {
            Some(i) if n.is_integer() && i >= 0 => Ok(Some(i as usize)),
            _ => Err(RuntimeError::with_kind(
                format!("file_read_bytes: max must be a whole number of at least 0, got {}", n.format()),
                ErrorKind::ArgumentError,
            )),
        },
        other => Err(RuntimeError::with_kind(
            format!("file_read_bytes: max must be a Number or none, got {}", type_name_of(other, &vm.names)),
            ErrorKind::TypeMismatch,
        )),
    }
}

/// M37: up to `max` bytes (all the rest when it's none); empty Bytes at the
/// end.
pub fn file_read_bytes(vm: &mut Vm, args: &[Value]) -> Result<Value, RuntimeError> {
    let limit = max_bytes(vm, &args[1])?;
    let Some(file) = open_file(vm, "file_read_bytes", args)? else { return Ok(closed()) };
    Ok(spawn(vm, move || {
        let mut state = file.lock().expect("file");
        let FileState::Reader(reader) = &mut *state else { return Err(other("the file isn't open for reading")) };
        let mut data = Vec::new();
        match limit {
            None => {
                reader.read_to_end(&mut data)?;
            }
            Some(n) => {
                reader.by_ref().take(n as u64).read_to_end(&mut data)?;
            }
        }
        Ok(IoValue::Bytes(data))
    }))
}

pub fn file_write_bytes(vm: &mut Vm, args: &[Value]) -> Result<Value, RuntimeError> {
    let data = bytes_arg(vm, "file_write_bytes", &args[1])?;
    let Some(file) = open_file(vm, "file_write_bytes", args)? else { return Ok(closed()) };
    Ok(spawn(vm, move || {
        let mut state = file.lock().expect("file");
        let FileState::Writer(f) = &mut *state else { return Err(other("the file isn't open for writing")) };
        f.write_all(&data)?;
        Ok(IoValue::None)
    }))
}

pub fn close(vm: &mut Vm, args: &[Value]) -> Result<Value, RuntimeError> {
    let removed = file_id(vm, "close", &args[0])?.and_then(|id| vm.files().remove(id));
    Ok(spawn(vm, move || {
        drop(removed);
        Ok(IoValue::None)
    }))
}
