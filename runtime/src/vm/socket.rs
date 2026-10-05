//! M38 (1.18, docs/MAHC_FORMAT.md #4.4): std:socket's natives (TCP) -- a
//! port of `mah/socket_natives.py`; the contract is docs/contracts/M38_socket.md.
//!
//! Like std:fs, each native returns a Promise at once and works on a worker
//! thread (`Vm::submit`), settling it with `[true, value]` or `[false, kind,
//! description]`. Sockets and listeners are ids in a per-VM table, separate
//! from the file table. Waiting calls poll in slices of at most 50 ms,
//! checking their deadline and whether their id was closed in between.

use std::collections::HashMap;
use std::io::{self, Read, Write};
use std::net::{Shutdown, TcpListener, TcpStream, ToSocketAddrs};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use crate::decimal::Decimal;

use super::error::{ErrorKind, RuntimeError};
use super::exec::Vm;
use super::fs::{failure, ok, IoValue};
use super::value::{type_name_of, PromiseData, Value};

const SLICE: Duration = Duration::from_millis(50);

#[derive(Clone)]
enum Entry {
    Stream(Arc<TcpStream>),
    Listener(Arc<TcpListener>),
}

/// The VM's socket table (ids start at 1), shared with the workers.
#[derive(Default, Clone)]
pub struct SocketTable {
    entries: Arc<Mutex<HashMap<u64, Entry>>>,
    next: Arc<AtomicU64>,
}

impl SocketTable {
    fn add(&self, entry: Entry) -> u64 {
        let id = self.next.fetch_add(1, Ordering::SeqCst) + 1;
        self.entries.lock().expect("socket table").insert(id, entry);
        id
    }
    fn get(&self, id: u64) -> Option<Entry> {
        self.entries.lock().expect("socket table").get(&id).cloned()
    }
    fn is_open(&self, id: u64) -> bool {
        self.entries.lock().expect("socket table").contains_key(&id)
    }
    fn remove(&self, id: u64) -> Option<Entry> {
        self.entries.lock().expect("socket table").remove(&id)
    }
    /// Closes every open socket and listener (the VM is finishing).
    pub fn close_all(&self) {
        let all: Vec<Entry> = self.entries.lock().expect("socket table").drain().map(|(_, e)| e).collect();
        for e in all {
            close_entry(e);
        }
    }
}

/// Closing is dropping the table's handle (no shutdown first); waiting
/// workers notice the id is gone within a slice and drop theirs.
fn close_entry(entry: Entry) {
    drop(entry);
}

// -- pure helpers --------------------------------------------------------------

fn fixed(kind: &str) -> IoValue {
    failure(kind, description_of(kind))
}

fn description_of(kind: &str) -> &'static str {
    match kind {
        "connection_refused" => "connection refused",
        "connection_reset" => "connection reset by peer",
        "timed_out" => "timed out",
        "address_in_use" => "address already in use",
        "address_not_available" => "address not available",
        "host_not_found" => "host not found",
        "permission_denied" => "permission denied",
        "closed" => "the socket is closed",
        _ => "",
    }
}

/// The failure kind of an I/O error (`other` when it has no kind of its own).
fn kind_of(e: &io::Error) -> &'static str {
    match e.kind() {
        io::ErrorKind::ConnectionRefused => "connection_refused",
        io::ErrorKind::ConnectionReset | io::ErrorKind::BrokenPipe | io::ErrorKind::ConnectionAborted => {
            "connection_reset"
        }
        io::ErrorKind::TimedOut | io::ErrorKind::WouldBlock => "timed_out",
        io::ErrorKind::AddrInUse => "address_in_use",
        io::ErrorKind::AddrNotAvailable => "address_not_available",
        io::ErrorKind::PermissionDenied => "permission_denied",
        _ => "other",
    }
}

/// The OS's text without Rust's " (os error N)" suffix.
fn os_text(e: &io::Error) -> String {
    let text = e.to_string();
    match text.rfind(" (os error ") {
        Some(i) => text[..i].to_string(),
        None => text,
    }
}

fn classify(e: &io::Error) -> IoValue {
    match kind_of(e) {
        "other" => failure("other", &os_text(e)),
        kind => fixed(kind),
    }
}

/// A port: a whole Number from `min` to 65535.
fn check_port(name: &str, n: &Decimal, min: u16) -> Result<u16, String> {
    match n.to_i64() {
        Some(i) if n.is_integer() && i >= min as i64 && i <= 65535 => Ok(i as u16),
        _ => Err(format!("{name}: port must be a whole number from {min} to 65535, got {}", n.format())),
    }
}

/// A timeout in ms: a whole Number >= 0.
fn check_timeout(name: &str, n: &Decimal) -> Result<u64, String> {
    match n.to_i64() {
        Some(i) if n.is_integer() && i >= 0 => Ok(i as u64),
        _ => Err(format!("{name}: timeout must be a whole number of at least 0, got {}", n.format())),
    }
}

fn check_backlog(n: &Decimal) -> Result<u64, String> {
    match n.to_i64() {
        Some(i) if n.is_integer() && i >= 1 => Ok(i as u64),
        _ => Err(format!("listen: backlog must be a whole number of at least 1, got {}", n.format())),
    }
}

fn check_max(n: &Decimal) -> Result<usize, String> {
    match n.to_i64() {
        Some(i) if n.is_integer() && i >= 1 => Ok(i as usize),
        _ => Err(format!("recv: max must be a whole number of at least 1, got {}", n.format())),
    }
}

// -- argument checks (on the VM's thread) --------------------------------------------

fn type_err(vm: &Vm, name: &str, what: &str, expected: &str, v: &Value) -> RuntimeError {
    RuntimeError::with_kind(
        format!("{name}: {what} must be {expected}, got {}", type_name_of(v, &vm.names)),
        ErrorKind::TypeMismatch,
    )
}

fn arg_err(message: String) -> RuntimeError {
    RuntimeError::with_kind(message, ErrorKind::ArgumentError)
}

fn host_arg(vm: &Vm, name: &str, v: &Value) -> Result<String, RuntimeError> {
    match v {
        Value::Str(s) => Ok(s.to_string()),
        other => Err(type_err(vm, name, "host", "a String", other)),
    }
}

fn port_arg(vm: &Vm, name: &str, v: &Value, min: u16) -> Result<u16, RuntimeError> {
    match v {
        Value::Number(n) => check_port(name, n, min).map_err(arg_err),
        other => Err(type_err(vm, name, "port", "a Number", other)),
    }
}

fn timeout_arg(vm: &Vm, name: &str, v: &Value) -> Result<Option<Duration>, RuntimeError> {
    match v {
        Value::None => Ok(None),
        Value::Number(n) => check_timeout(name, n).map(|ms| Some(Duration::from_millis(ms))).map_err(arg_err),
        other => Err(type_err(vm, name, "timeout", "a Number or none", other)),
    }
}

/// `None` when the id can't be open (negative, fractional, huge).
fn id_arg(vm: &Vm, name: &str, v: &Value) -> Result<Option<u64>, RuntimeError> {
    match v {
        Value::Number(n) => Ok(match n.to_sign_u64() {
            Some((false, id)) => Some(id),
            _ => None,
        }),
        other => Err(RuntimeError::with_kind(
            format!("{name}: expected a socket id, got {}", type_name_of(other, &vm.names)),
            ErrorKind::TypeMismatch,
        )),
    }
}

fn spawn(vm: &mut Vm, job: impl FnOnce() -> IoValue + Send + 'static) -> Value {
    let promise = PromiseData::new_pending();
    vm.submit(promise.clone(), Box::new(job));
    Value::Promise(promise)
}

fn closed() -> Value {
    let promise = PromiseData::new_pending();
    promise.borrow_mut().settled = Some(fixed("closed").into_value());
    Value::Promise(promise)
}

fn stream_of(vm: &Vm, id: Option<u64>) -> Option<(u64, Arc<TcpStream>)> {
    let id = id?;
    match vm.sockets().get(id) {
        Some(Entry::Stream(s)) => Some((id, s)),
        _ => None,
    }
}

fn address_list(stream: &TcpStream) -> io::Result<IoValue> {
    let peer = stream.peer_addr()?;
    let local = stream.local_addr()?;
    Ok(IoValue::List(vec![
        IoValue::Num(0),
        IoValue::Str(peer.ip().to_string()),
        IoValue::Num(peer.port() as u64),
        IoValue::Num(local.port() as u64),
    ]))
}

/// `[id, peer_host, peer_port, local_port]` for a new stream.
fn register(table: &SocketTable, stream: TcpStream) -> IoValue {
    let mut info = match address_list(&stream) {
        Ok(v) => v,
        Err(e) => return classify(&e),
    };
    let id = table.add(Entry::Stream(Arc::new(stream)));
    if let IoValue::List(items) = &mut info {
        items[0] = IoValue::Num(id);
    }
    ok(info)
}

/// The slice to wait now, or `None` once the deadline has passed.
fn slice_of(deadline: Option<Instant>) -> Option<Duration> {
    match deadline {
        None => Some(SLICE),
        Some(d) => {
            let left = d.saturating_duration_since(Instant::now());
            if left.is_zero() {
                None
            } else {
                Some(left.min(SLICE))
            }
        }
    }
}

// -- natives -----------------------------------------------------------------------

pub fn connect(vm: &mut Vm, args: &[Value]) -> Result<Value, RuntimeError> {
    let host = host_arg(vm, "connect", &args[0])?;
    let port = port_arg(vm, "connect", &args[1], 1)?;
    let timeout = timeout_arg(vm, "connect", &args[2])?;
    let table = vm.sockets().clone();
    Ok(spawn(vm, move || {
        let addrs: Vec<_> = match (host.as_str(), port).to_socket_addrs() {
            Ok(a) => a.collect(),
            Err(_) => return fixed("host_not_found"),
        };
        if addrs.is_empty() {
            return fixed("host_not_found");
        }
        // DNS isn't counted against the timeout.
        let deadline = timeout.map(|t| Instant::now() + t);
        let mut last: Option<IoValue> = None;
        for addr in addrs {
            let result = match deadline {
                None => TcpStream::connect(addr),
                Some(d) => {
                    let left = d.saturating_duration_since(Instant::now());
                    TcpStream::connect_timeout(&addr, left.max(Duration::from_millis(1)))
                }
            };
            match result {
                Ok(stream) => {
                    let _ = stream.set_nodelay(true);
                    return register(&table, stream);
                }
                Err(e) => last = Some(classify(&e)),
            }
        }
        last.unwrap_or_else(|| fixed("host_not_found"))
    }))
}

pub fn listen(vm: &mut Vm, args: &[Value]) -> Result<Value, RuntimeError> {
    let host = host_arg(vm, "listen", &args[0])?;
    let port = port_arg(vm, "listen", &args[1], 0)?;
    match &args[2] {
        // The backlog is checked but std offers no way to set it; std uses
        // its own (128 by default, like the contract's default).
        Value::Number(n) => {
            check_backlog(n).map_err(arg_err)?;
        }
        other => return Err(type_err(vm, "listen", "backlog", "a Number", other)),
    }
    let table = vm.sockets().clone();
    Ok(spawn(vm, move || {
        let addrs: Vec<_> = match (host.as_str(), port).to_socket_addrs() {
            Ok(a) => a.collect(),
            Err(_) => return fixed("host_not_found"),
        };
        // The first resolved address only.
        let mut last: Option<IoValue> = None;
        for addr in addrs.into_iter().take(1) {
            match TcpListener::bind(addr) {
                Ok(listener) => {
                    let bound = match listener.local_addr() {
                        Ok(a) => a.port(),
                        Err(e) => return classify(&e),
                    };
                    if let Err(e) = listener.set_nonblocking(true) {
                        return classify(&e);
                    }
                    let id = table.add(Entry::Listener(Arc::new(listener)));
                    return ok(IoValue::List(vec![IoValue::Num(id), IoValue::Num(bound as u64)]));
                }
                Err(e) => last = Some(classify(&e)),
            }
        }
        last.unwrap_or_else(|| fixed("host_not_found"))
    }))
}

pub fn accept(vm: &mut Vm, args: &[Value]) -> Result<Value, RuntimeError> {
    let id = id_arg(vm, "accept", &args[0])?;
    let timeout = timeout_arg(vm, "accept", &args[1])?;
    let listener = match id.and_then(|i| vm.sockets().get(i).map(|e| (i, e))) {
        Some((i, Entry::Listener(l))) => (i, l),
        _ => return Ok(closed()),
    };
    let table = vm.sockets().clone();
    Ok(spawn(vm, move || {
        let (id, listener) = listener;
        let deadline = timeout.map(|t| Instant::now() + t);
        loop {
            if !table.is_open(id) {
                return fixed("closed");
            }
            match listener.accept() {
                Ok((stream, _)) => {
                    if !table.is_open(id) {
                        return fixed("closed");
                    }
                    let _ = stream.set_nonblocking(false);
                    let _ = stream.set_nodelay(true);
                    return register(&table, stream);
                }
                Err(e) if e.kind() == io::ErrorKind::WouldBlock || e.kind() == io::ErrorKind::Interrupted => {
                    match slice_of(deadline) {
                        None => return fixed("timed_out"),
                        Some(s) => std::thread::sleep(s.min(Duration::from_millis(5))),
                    }
                }
                Err(e) => return classify(&e),
            }
        }
    }))
}

pub fn send(vm: &mut Vm, args: &[Value]) -> Result<Value, RuntimeError> {
    let id = id_arg(vm, "send", &args[0])?;
    let data = match &args[1] {
        Value::Bytes(b) => b.borrow().clone(),
        other => return Err(type_err(vm, "send", "data", "Bytes", other)),
    };
    let Some((id, stream)) = stream_of(vm, id) else { return Ok(closed()) };
    let table = vm.sockets().clone();
    Ok(spawn(vm, move || {
        let _ = stream.set_write_timeout(Some(SLICE));
        let mut rest: &[u8] = &data;
        while !rest.is_empty() {
            if !table.is_open(id) {
                return fixed("closed");
            }
            match (&*stream).write(rest) {
                Ok(0) => return fixed("connection_reset"),
                Ok(n) => rest = &rest[n..],
                Err(e) if matches!(e.kind(), io::ErrorKind::WouldBlock | io::ErrorKind::TimedOut) => {}
                Err(e) if e.kind() == io::ErrorKind::Interrupted => {}
                Err(e) => return classify(&e),
            }
        }
        ok(IoValue::None)
    }))
}

pub fn recv(vm: &mut Vm, args: &[Value]) -> Result<Value, RuntimeError> {
    let id = id_arg(vm, "recv", &args[0])?;
    let max = match &args[1] {
        Value::Number(n) => check_max(n).map_err(arg_err)?,
        other => return Err(type_err(vm, "recv", "max", "a Number", other)),
    };
    let timeout = timeout_arg(vm, "recv", &args[2])?;
    let Some((id, stream)) = stream_of(vm, id) else { return Ok(closed()) };
    let table = vm.sockets().clone();
    Ok(spawn(vm, move || {
        let deadline = timeout.map(|t| Instant::now() + t);
        let mut buf = vec![0u8; max.min(1 << 20)];
        loop {
            if !table.is_open(id) {
                return fixed("closed");
            }
            // At least 1 ms: a zero read timeout is an error. With timeout 0
            // this is one quick look at what is already there.
            let slice = slice_of(deadline).unwrap_or(Duration::from_millis(1)).max(Duration::from_millis(1));
            let _ = stream.set_read_timeout(Some(slice));
            match (&*stream).read(&mut buf) {
                Ok(n) => {
                    if n == 0 && !table.is_open(id) {
                        return fixed("closed");
                    }
                    buf.truncate(n);
                    return ok(IoValue::Bytes(buf));
                }
                Err(e) if matches!(e.kind(), io::ErrorKind::WouldBlock | io::ErrorKind::TimedOut) => {
                    if slice_of(deadline).is_none() {
                        return fixed("timed_out");
                    }
                }
                Err(e) if e.kind() == io::ErrorKind::Interrupted => {}
                Err(e) => return classify(&e),
            }
        }
    }))
}

pub fn shutdown(vm: &mut Vm, args: &[Value]) -> Result<Value, RuntimeError> {
    let id = id_arg(vm, "shutdown", &args[0])?;
    let Some((_, stream)) = stream_of(vm, id) else { return Ok(closed()) };
    Ok(spawn(vm, move || match stream.shutdown(Shutdown::Write) {
        Ok(()) => ok(IoValue::None),
        Err(e) => classify(&e),
    }))
}

pub fn close(vm: &mut Vm, args: &[Value]) -> Result<Value, RuntimeError> {
    let removed = id_arg(vm, "close", &args[0])?.and_then(|id| vm.sockets().remove(id));
    Ok(spawn(vm, move || {
        if let Some(entry) = removed {
            close_entry(entry);
        }
        ok(IoValue::None)
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn num(text: &str) -> Decimal {
        Decimal::parse(text).expect("a number")
    }

    #[test]
    fn port_messages() {
        assert_eq!(check_port("listen", &num("0"), 0), Ok(0));
        assert_eq!(check_port("connect", &num("65535"), 1), Ok(65535));
        assert_eq!(
            check_port("connect", &num("0"), 1),
            Err("connect: port must be a whole number from 1 to 65535, got 0".to_string())
        );
        assert_eq!(
            check_port("listen", &num("65536"), 0),
            Err("listen: port must be a whole number from 0 to 65535, got 65536".to_string())
        );
        assert_eq!(
            check_port("listen", &num("1.5"), 0),
            Err("listen: port must be a whole number from 0 to 65535, got 1.5".to_string())
        );
        assert!(check_port("listen", &num("-1"), 0).is_err());
    }

    #[test]
    fn timeout_backlog_max_messages() {
        assert_eq!(check_timeout("recv", &num("0")), Ok(0));
        assert_eq!(
            check_timeout("recv", &num("-5")),
            Err("recv: timeout must be a whole number of at least 0, got -5".to_string())
        );
        assert_eq!(
            check_backlog(&num("0")),
            Err("listen: backlog must be a whole number of at least 1, got 0".to_string())
        );
        assert_eq!(check_backlog(&num("128")), Ok(128));
        assert_eq!(
            check_max(&num("0")),
            Err("recv: max must be a whole number of at least 1, got 0".to_string())
        );
        assert_eq!(check_max(&num("2.5")), Err("recv: max must be a whole number of at least 1, got 2.5".to_string()));
    }

    #[test]
    fn error_kinds() {
        let kind = |k| kind_of(&io::Error::from(k));
        assert_eq!(kind(io::ErrorKind::ConnectionRefused), "connection_refused");
        assert_eq!(kind(io::ErrorKind::ConnectionReset), "connection_reset");
        assert_eq!(kind(io::ErrorKind::BrokenPipe), "connection_reset");
        assert_eq!(kind(io::ErrorKind::ConnectionAborted), "connection_reset");
        assert_eq!(kind(io::ErrorKind::TimedOut), "timed_out");
        assert_eq!(kind(io::ErrorKind::AddrInUse), "address_in_use");
        assert_eq!(kind(io::ErrorKind::AddrNotAvailable), "address_not_available");
        assert_eq!(kind(io::ErrorKind::PermissionDenied), "permission_denied");
        assert_eq!(kind(io::ErrorKind::NotFound), "other");
        assert_eq!(description_of("connection_reset"), "connection reset by peer");
        assert_eq!(description_of("closed"), "the socket is closed");
        assert_eq!(description_of("host_not_found"), "host not found");
        assert_eq!(os_text(&io::Error::from_raw_os_error(2)), "No such file or directory");
    }

    #[test]
    fn loopback_round_trip() {
        let table = SocketTable::default();
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let port = listener.local_addr().unwrap().port();
        let stream = TcpStream::connect(("127.0.0.1", port)).unwrap();
        let id = match register(&table, stream) {
            IoValue::List(v) => match &v[1] {
                IoValue::List(info) => matches!(info[0], IoValue::Num(1)),
                _ => false,
            },
            _ => false,
        };
        assert!(id);
        assert!(table.is_open(1));
        table.close_all();
        assert!(!table.is_open(1));
    }
}
