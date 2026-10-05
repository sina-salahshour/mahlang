# M38 work contract: `std:socket` (TCP), bytecode 1.18

`docs/STDLIB.md` Phase 4's first module. The design below is normative; the subagents
implement it in parallel, **each editing only the files it owns (section 3)**. Agents read
anything, never run `git commit`/`push`/`checkout`/`stash`/`reset`, and never touch
`www/`. Agents R and P must not run `cargo build` into the shared target at the same time as
another agent's tests; only Agent R runs cargo.

## 1. Design

### Surface (`mah/std/socket.mh`, `import socket from "std:socket"`)

```mah
let server = socket.listen(0)                 # host "127.0.0.1"; port 0 picks a free one
print(server.port)                            # the real port
let incoming = detach server.accept()         # a Promise of a Socket
let client = socket.connect("127.0.0.1", server.port)
let conn = incoming.await
client.send_text("hello\n")
print(conn.read_line())                       # "hello"
conn.send("hi".to_bytes())
print(client.recv())                          # Bytes[68 69]
client.close(); conn.close(); server.close()
```

- `connect(host: String, port: Number, timeout: Unknown = none) -> Socket throws SocketError`
- `listen(port: Number, host: String = "127.0.0.1", backlog: Number = 128) -> Listener throws SocketError`
- `struct Listener { id: Number, host: String, port: Number }`, with methods
  `accept(timeout: Unknown = none) -> Socket throws SocketError` and `close() throws SocketError`.
- `struct Socket { id: Number, peer_host: String, peer_port: Number, local_port: Number, buffer: Bytes }`,
  with these methods:
  - `send(data: Bytes)` sends all of it. `send_text(text: String)` sends its UTF-8.
  - `recv(max: Number = 65536, timeout: Unknown = none) -> Bytes` returns up to `max` bytes, and
    **empty Bytes once the peer has closed its side**. Buffered bytes (see `read_line`) are
    returned first, without waiting.
  - `recv_exactly(n: Number, timeout: Unknown = none) -> Bytes` returns exactly `n` bytes, or
    throws SocketError kind `closed_early` ("the connection closed before N bytes arrived") if the
    peer closes first.
  - `read_line(timeout: Unknown = none) -> Unknown` returns the next line as a String, without
    `\n` or `\r\n`, or `none` at the end. Bytes after the line stay in `buffer`, and a final line
    without `\n` is still returned. Text that isn't valid UTF-8 throws SocketError kind
    `invalid_utf8`.
  - `shutdown()` stops sending: the peer's `recv` then sees the end.
  - `close()`. Closing twice does nothing.
  - `impl Printable`: `Socket(127.0.0.1:5000)` (the peer) and `Listener(127.0.0.1:5000)`.
- `export struct SocketError { kind: String, op: String, address: String, description: String }`
  with `impl Error` whose `message()` is `op + ": " + description + ": " + address`.
  - `op` is the function's name: `connect`, `listen`, `accept`, `send`, `recv`, `read_line`,
    `recv_exactly`, `shutdown` or `close`.
  - `address` is `host:port`: the peer's for Socket methods, the listen address for Listener
    methods and `listen`, the target for `connect`.
- **Timeouts are in milliseconds**, like std:async's. `none` means wait forever. A whole
  Number ≥ 0 is allowed, and 0 means "only what is already there". When the time runs out,
  SocketError kind `timed_out` is thrown.
- Every function waits like a call, but the work runs on a worker thread (like std:fs), so
  `detach` runs it in the background. A pending `accept`/`recv` **keeps the program running**
  (the scheduler's usual "pending I/O" rule). Closing the socket or listener ends it: the
  waiting call fails with kind `closed`.

### Natives (1.18; docs/MAHC_FORMAT.md §4.4)

Each native returns a pending Promise at once and does its work on a worker thread, which
settles it with a **result**: `[true, value]` or `[false, kind, description]`, exactly like the
`fs.*` natives. Sockets and listeners are ids (positive whole Numbers) in a per-VM **socket
table**, separate from the file table and numbered from 1. The VM's thread checks argument
types **before** anything else and raises them as RuntimeErrors at once:

| name | arity | value on success |
|---|---|---|
| `socket.connect` | 3 | `host, port, timeout` → `[id, peer_host, peer_port, local_port]` |
| `socket.listen` | 3 | `host, port, backlog` → `[id, port]` (the bound port) |
| `socket.accept` | 2 | `id, timeout` → `[id, peer_host, peer_port, local_port]` of the new socket |
| `socket.send` | 2 | `id, data` → `none` once every byte is written |
| `socket.recv` | 3 | `id, max, timeout` → a Bytes of 1..max bytes, or empty at the end of the stream |
| `socket.shutdown` | 1 | `id` → `none` (shuts the write side) |
| `socket.close` | 1 | `id` → `none`. An unknown or closed id is fine (`[true, none]`) |

- **Argument errors** (synchronous RuntimeErrors, same text on both VMs; NAME is the part after
  `socket.`):
  - host not a String: TypeMismatch `NAME: host must be a String, got TYPE`.
  - port not a Number: TypeMismatch `NAME: port must be a Number, got TYPE`. Not a whole
    Number from 0 to 65535: ArgumentError `NAME: port must be a whole number from 0 to 65535, got N`.
    `connect` refuses 0 the same way (`... from 1 to 65535 ...`).
  - timeout neither none nor a Number: TypeMismatch `NAME: timeout must be a Number or none, got TYPE`.
    Not whole ≥ 0: ArgumentError `NAME: timeout must be a whole number of at least 0, got N`.
  - backlog not a whole Number ≥ 1: ArgumentError `listen: backlog must be a whole number of at least 1, got N`
    (TypeMismatch `listen: backlog must be a Number, got TYPE` for a non-Number).
  - id not a Number: TypeMismatch `NAME: expected a socket id, got TYPE`.
  - data not Bytes: TypeMismatch `send: data must be Bytes, got TYPE`.
  - max not a whole Number ≥ 1: ArgumentError `recv: max must be a whole number of at least 1, got N`
    (TypeMismatch `recv: max must be a Number, got TYPE`).
  - Numbers in messages are formatted as `print` shows them.
- **An id that isn't open** (closed, never opened, or the wrong kind: a listener id given to
  `recv`, a socket id given to `accept`): the Promise settles at once with
  `[false, "closed", "the socket is closed"]`.
- **Failure kinds and fixed descriptions**:
  - `connection_refused` "connection refused"
  - `connection_reset` "connection reset by peer" (also broken pipe / aborted)
  - `timed_out` "timed out"
  - `address_in_use` "address already in use"
  - `address_not_available` "address not available"
  - `host_not_found` "host not found" (the name didn't resolve)
  - `permission_denied` "permission denied"
  - `closed` "the socket is closed"
  - `other`, whose description is the OS's text with no " (os error N)" suffix, like std:fs.
- **Waiting and closing**: a worker waiting in `accept`/`recv`/`connect` polls in slices of at
  most 50 ms. Between slices it checks the deadline (timeout) and whether the id was closed. So
  `close` (which removes the id from the table at once, on the VM's thread, then closes the OS
  socket on a worker) makes a waiting call fail with `closed` within ~50 ms. A call already
  waiting when its id closes fails with `closed`; a `send` in progress may fail with `closed` or
  `connection_reset`.
- `connect` resolves `host` (DNS, IPv4 or IPv6) and tries each address in order until one
  connects. The timeout covers the whole attempt. `peer_host` is the numeric address it
  connected to (`127.0.0.1`), `local_port` its local port.
- `listen` binds `host:port` with SO_REUSEADDR on Unix (both VMs, so a restarted server can
  rebind at once), then listens with `backlog`. Python: `socket.create_server`-equivalent
  without dual-stack. Rust: `std::net::TcpListener` (std sets SO_REUSEADDR on Unix).
- Sends and receives on one socket may run at the same time from different tasks: Rust shares
  the stream as `Arc<TcpStream>` (Read/Write for `&TcpStream`), never behind a lock held while
  waiting.
- When the VM finishes, every open socket and listener is closed.

### Versioning

MINOR becomes **18**. The seven `socket.*` natives are 1.18 (NATIVE_SINCE_MINOR /
`native_since_minor`). Only programs importing std:socket list them.

## 2. Conventions (all agents)

- Read before editing, match the surrounding style, tag new code `M38 (1.18)`. Follow how
  `std:fs`/`std:process` (M35/M36) did the same thing in each file. `mah/fs_natives.py` ↔
  `runtime/src/vm/fs.rs` is the closest model.
- The reference implementation is Python (`mah/socket_natives.py`). Rust must produce the same
  results, kinds, descriptions and messages.
- Report: files changed, exact command output (pass/fail counts), judgment calls, anything in
  the contract that turned out impossible or ambiguous.

## 3. Tasks and file ownership

### Agent P: Python VM
Owns:
- `mah/socket_natives.py` (new)
- `mah/natives.py` (only the registration lines at the end)
- `mah/code_interpreter.py` (only `_IoHub`: the socket table, and closing every socket in
  `close()`)
- `mah/bytecode/format.py` (MINOR 18, its docstring paragraph, NATIVE_ARITIES,
  NATIVE_SINCE_MINOR)

Implement section 1's natives exactly, with workers via `ctx.io.submit` like fs_natives.
Self-check by calling the natives directly from Python (`from mah.natives import NATIVES` with
a real `_IoHub`, or by running a scratch program once Agent M's `mah/std/socket.mh` exists).
Commands: `python3 -m unittest tests.test_bytecode tests.test_stdlib -q`. Some MINOR-pin tests will fail with 18 vs 17; those are Agent M's to update, so list them.

### Agent R: Rust VM
Owns:
- `runtime/src/vm/socket.rs` (new)
- `runtime/src/vm/mod.rs` (the `mod` line)
- `runtime/src/vm/link.rs` (NativeFn variants and names)
- `runtime/src/vm/natives.rs` (dispatch arms)
- `runtime/src/vm/exec.rs` (only `IoHub`/`Vm`: the socket table and an accessor like `files()`,
  and closing sockets at the end if the table needs it)
- `runtime/src/decode.rs` (MINOR 18, `native_since_minor`, and the MINOR pin in its own tests)
- `runtime/src/vm/fs.rs` (only if you must make a helper `pub(super)` to reuse it: `IoValue`,
  `ok`, `failure`, the `spawn` pattern)

std only, no new crates (std::net is enough). Port Agent P's rules exactly. Since P writes in
parallel, implement from **this contract**; it is complete. Commands: `cd runtime && cargo build
--release && cargo test --release`. Add Rust unit tests in socket.rs for the pure helpers
(argument validation messages, error-kind mapping).

### Agent M: std module, tests, example, parity
Owns:
- `mah/std/socket.mh` (new)
- `mah/std/socket.test.mh` (new)
- `tests/test_socket.py` (new)
- `examples/sockets.mh` (new)
- `runtime/tests/vm_diff.py` (add a `std_socket` case)
- the MINOR pin edits 17→18 in `tests/` (test_hooks, test_decorators, test_reflection,
  test_bytecode's unsupported-minor tests 18→19, and test_bytes if it pins MINOR), plus
  `tests/test_bytecode.py`'s example→minor entry for `sockets.mh` (18)

Write the module per section 1, in the style of `mah/std/fs.mh`/`process.mh` (`##` doc
comments on exports as process.mh does, a `result()` helper turning `[false, kind,
description]` into SocketError, Socket methods buffering through the `buffer` field).
- Tests: `socket.test.mh` (both VMs) covers echo, `read_line` with `\r\n`, a final unterminated
  line, `recv` returning buffered bytes first, `recv_exactly` and `closed_early`, shutdown
  making the peer's `recv` empty, `timed_out` on recv and accept (timeout 50), connection
  refused (listen, take the port, close, connect), closing a listener during a detached accept,
  closed ids, and Printable output. `test_socket.py` covers every argument-error message on both
  VMs, checker types (`socket.connect(...)` is a `Socket`), the bytecode minor (18), and that a
  program with a pending accept keeps running until the listener is closed from a timer.
- The example is deterministic: an echo server and a client in one program.
- Natives arrive in parallel from P and R. Write everything first, then run once they exist:
  `python3 -m mah test --file mah/std/socket.test.mh` (and `--vm rust` if the CLI supports it
  for test), `python3 -m unittest tests.test_socket -q`, `MAH_TEST_VM=rust python3 -m unittest tests.test_socket -q`,
  `python3 runtime/tests/vm_diff.py`. If the natives aren't there yet when you're done
  writing, wait by re-checking every couple of minutes (`grep -c socket.connect
  mah/natives.py mah/socket_natives.py runtime/src/vm/link.rs`) for up to 20 minutes before
  reporting.

### Agent D: documentation
Owns `docs/MAHC_FORMAT.md`, `docs/STDLIB.md`, `docs/V2_DESIGN.md`, `docs/NEXT_PHASES.md`,
`README.md`, `docs/RUST_VM.md`, `mah/project/templates/docs/mah-language.md`,
`mah/project/templates/AGENTS.md`.
- MAHC_FORMAT: title 1.18, §3 minor rule, §4.4 rows for the seven natives plus a paragraph
  (socket table, results, kinds and descriptions, polling/closing, argument errors), §6.4 if it
  describes what pending I/O keeps alive, and §7.
- STDLIB: status line M38, `std:socket` ✅ Landed (M38) with decisions (TCP only, ms timeouts,
  ids in a socket table wrapped by structs, buffered `read_line`, close ends waiting calls,
  UDP/TLS later).
- V2_DESIGN entry `43. **M38 — std:socket. ✅ Landed.**` in the M35/M36 style. NEXT_PHASES:
  next is the URL/HTTP client.
- README "Where this is going", the language reference (std:socket section, module lists) and
  AGENTS.md module list.
- Command: `python3 -m unittest tests.test_project -q` (the reference's ```mah blocks are
  compiled by DocsSanityTests; mark a block so it isn't run if it needs the network. Check how
  that test chooses blocks).
