# M42 contract: the HTTP server (`std:http`'s server side) and server TLS, bytecode 1.20

`docs/NEXT_PHASES.md`'s "The HTTP server", the next step after M39 (`docs/contracts/M39_http.md`).
Written by the thinker (`docs/DEVELOPMENT_WORKFLOW.md`) for **two coders working in parallel**:

- **Part A, "server core"**: pure Mah in `mah/std/http.mh` (no new natives): the server API,
  request parsing, response writing, keep-alive, `Expect: 100-continue`, form and multipart
  bodies, limits, error mapping, graceful shutdown, concurrency, tests, docs.
- **Part B, "server TLS"**: two new `socket.*` natives on **both** VMs (Python and Rust, parity
  mandatory), their `std:socket` wrappers, the bytecode MINOR bump to **20**, tests, docs.

§2 is the only interface between the parts. Each part's section is complete on its own: a coder
reads §1, §2, their own part, §5 (shared files), §7 (deferred) and §8 (Mah gotchas). Nothing here
is open; where a choice was made by judgment it says so (§9 lists them for the user).

Milestones around it: **M43** is packages from GitHub, **M44** the multithreading design (neither
is touched here). The NestJS/Hono-style **backend framework is out of scope**: it will live in a
separate repository and build on this API (§7 says what this milestone does for it).

---

## 1. Decisions

- **The server is Mah, like the client.** `http.serve` is written in `mah/std/http.mh` over
  `std:socket`'s `listen`/`accept`, so both VMs run identical server code and parity holds by
  construction. The only natives are Part B's two TLS ones.
- **One module, shared wire code.** Server and client live in `mah/std/http.mh`; the header-line
  reader, header-pair helpers and the chunked decoder become shared private helpers (§3.3)
  instead of being duplicated. The client's observable behavior does **not** change (every
  existing `http.test.mh` / `test_http.py` / vm_diff expectation stays as is).
- **Handler shape: `fn(Request) -> Reply`.** A handler is a plain function from a request value to
  a reply value (Hono/Fetch style, not Node's `(req, res)`). Middleware and routing (the
  framework's job) are then just function composition, and a handler is unit-testable without a
  socket by building an `http.Request` literal. Streaming is a reply whose body is a producer
  function `fn(BodyWriter)` (§3.6).
- **New names: `Request`, `Reply`, `BodyWriter`, `Part`, `Server`.** The client's `Response`
  (status, reason, headers, body, url, method) stays the client's; the server's answer is a
  `Reply` so neither struct carries fields meaningless on the other side (judgment call, §9).
- **Request bodies are read eagerly** (up to `max_body`) before the handler runs; streaming
  request bodies are deferred (§7).
- **Concurrency**: `serve` returns at once; an accept loop runs as a detached task and each
  connection is its own detached task, so connections are handled concurrently whenever a
  handler waits (I/O, `sleep_async`, `.await`). The scheduler stays single-threaded (M44).
- **Graceful shutdown**: `server.shutdown(grace)` (returns at once) stops accepting, closes idle
  connections, lets in-flight requests finish (their replies say `Connection: close`) and
  force-closes what is left after `grace` ms; `server.wait()` returns once everything has
  stopped; `server.close(grace)` is both.
- **Limits** (keyword options of `serve`, defaults in §3.2): head size (431/414), body size
  (413), a whole-request read deadline (408), an idle keep-alive timeout (silent close), a
  connection cap (503), 100 header fields (431).
- **Server TLS is a loaded config plus a per-connection handshake.** `socket.tls_server_config
  (cert_path, key_path)` loads and checks a PEM certificate chain and private key **once** (a
  typo'd path fails at startup, not at the first connection) into an id in the socket table;
  `Socket.start_tls_server(config, timeout)` runs a server handshake on an accepted socket.
  `http.serve(..., tls: config)` takes the loaded config (§2).
- **Bytecode 1.20**: the two TLS natives are 1.20. `std:socket` declares them, so every program
  importing `std:socket` or `std:http` is 1.20 (the same trade M37 and M39 made).

---

## 2. The interface between Part A and Part B

Part B owns, in `mah/std/socket.mh`, exactly this API (Part A never edits `socket.mh`):

```mah
## A TLS server's certificate chain and private key, loaded and checked once by
## `tls_server_config`; `Socket.start_tls_server` uses it for each connection.
export struct TlsServerConfig { id: Number, cert_path: String, key_path: String }

export fn tls_server_config(cert_path: String, key_path: String) -> TlsServerConfig throws SocketError

impl TlsServerConfig {
    fn close(self) throws SocketError
}

impl Socket {
    fn start_tls_server(self, config: TlsServerConfig, timeout: Unknown = none) throws SocketError
}
```

Part A's side, the **only** places `http.mh` touches TLS:

1. `serve(..., tls: Unknown = none, ...)`: `tls` is `none` or a value whose
   `type_name(tls) == "TlsServerConfig"` (`type_name` is the `value.type_name` extern `http.mh`
   already declares). Anything else is `RuntimeError.ArgumentError` with message
   `http: tls must be a socket.TlsServerConfig (from socket.tls_server_config), got TYPE`.
2. Per connection, when `tls != none`: `conn.start_tls_server(tls, read_timeout)` (a **method
   call**, resolved at run time). On any `socket.SocketError` the connection is closed silently.
3. `Request.tls` and `Server.tls` are `true` and `Server.url` uses `https`.

`http.mh` never names `socket.TlsServerConfig` or `socket.tls_server_config` (naming a type or
function `std:socket` doesn't export is a **compile error**, which would break Part A before Part B
lands). So Part A compiles and passes its tests with or without Part B; its TLS branch is simply
unreachable until Part B exists. The user writes:

```mah
import socket from "std:socket"
import http from "std:http"
let identity = socket.tls_server_config("cert.pem", "key.pem")    # throws SocketError kind "tls_config"
let server = http.serve(8443, handle, host: "0.0.0.0", tls: identity)
```

The server never closes the config (the caller owns it; one config may serve several servers).

The end-to-end HTTPS test (`http.serve(tls:)` + `http.get("https://...")`) needs both parts; Part
B writes it (§4.7, `HttpsServerTests`) guarded by a skip until Part A has landed.

---

## 3. Part A: server core

### 3.1 Files (Part A owns these)

| file | change |
|---|---|
| `mah/std/http.mh` | refactor shared wire helpers (§3.3); add the server (§3.2–§3.10) |
| `mah/std/http_server.test.mh` | **new**: Mah tests, both VMs (§3.11) |
| `tests/test_http_server.py` | **new**: Python clients against a Mah server, both VMs (§3.11) |
| `runtime/tests/vm_diff.py` | add case `std_http_server` immediately **after** `std_tls_url_http` |
| `examples/http_server.mh` | **new** (§3.11) |
| `tests/test_examples.py` | golden test `test_http_server` after `test_http_client` |
| `tests/test_bytecode.py` | one line in the per-example minor map: `"http_server.mh": 20` (see §5) |
| `docs/STDLIB.md` | the `### std:http` section only (§3.12) |
| `docs/V2_DESIGN.md` | the new M42 entry and the Status paragraph (§3.12; includes Part B's text) |
| `docs/NEXT_PHASES.md` | "The HTTP server" marked landed |
| `mah/project/templates/docs/mah-language.md` | the `std:http` paragraph and the "Not available" bullet |
| `www/src/content/std/http.md`, `www/src/content/docs/standard-library.md` (http part) | server section |

Do **not** touch: `mah/std/socket.mh`, any native (`mah/*_natives.py`, `mah/natives.py`,
`runtime/src/**`), `mah/bytecode/format.py`, `docs/MAHC_FORMAT.md`, `tests/test_http.py`,
`tests/test_socket.py`, `mah/std/http.test.mh` (the client tests must pass **unchanged**).

### 3.2 Public API (exact)

New imports at the top of `http.mh` (keep the existing ones): `import time from "std:time"`,
`import async from "std:async"`. New externs (1.11 natives, so they raise no minor):

```mah
extern fn new_promise() -> Promise<Unknown> = "promise.new"
extern fn settle(promise: Promise<Unknown>, value: Unknown) -> Bool = "promise.resolve"
```

Exported items (all `export`; doc them with `##` as the rest of the module is):

```mah
## An incoming request, as the server read it. `target` is the request target as sent
## ("/a/b?x=1"); `path` is its part before "?" (still percent-encoded) and `query` the part
## after it, or `none` (like std:url's Url). `headers` are the [name, value] pairs as received
## (names as the client wrote them; see `header`); `body` is the whole body (empty Bytes when
## there is none). `version` is "HTTP/1.1" or "HTTP/1.0" as sent; `tls` is true over TLS.
export struct Request {
    method: String,
    target: String,
    path: String,
    query: Unknown,
    version: String,
    headers: Vector<Vector<String>>,
    body: Bytes,
    peer_host: String,
    peer_port: Number,
    tls: Bool
}

## What a handler answers. `body` is `none`, a String, Bytes, or a producer
## `fn(BodyWriter)` that streams the body (see `Reply.stream`).
export struct Reply { status: Number, headers: Vector<Vector<String>>, body: Unknown }

## Where a streamed reply's producer writes the body.
export struct BodyWriter { conn: socket.Socket, mode: String, remaining: Unknown, written: Number }

## One part of a multipart/form-data body.
export struct Part {
    name: String,
    filename: Unknown,
    content_type: Unknown,
    headers: Vector<Vector<String>>,
    body: Bytes
}

## A running server (see `serve`). `state` is private.
export struct Server { host: String, port: Number, tls: Bool, state: Unknown }

export fn serve(
    port: Number,
    handler: fn(Request) -> Reply,
    host: String = "127.0.0.1",
    tls: Unknown = none,
    max_head: Number = 16384,
    max_body: Number = 10485760,
    read_timeout: Unknown = 30000,
    idle_timeout: Unknown = 5000,
    max_connections: Number = 256,
    backlog: Number = 128,
    on_error: Unknown = none
) -> Server throws socket.SocketError

export fn reason_phrase(status: Number) -> String
export fn http_date(timestamp: Number) -> String
```

`serve`'s keywords:

| option | meaning |
|---|---|
| `port`, `host`, `backlog` | as `socket.listen` (port 0 picks a free one; `server.port` is the real one). Bind failures throw `socket.SocketError` (`address_in_use`, ...) from `serve` itself |
| `handler` | `fn(Request) -> Reply`, called once per request |
| `tls` | `none` or a `socket.TlsServerConfig` (§2) |
| `max_head` | bytes: request line + header lines + their line ends (414 / 431) |
| `max_body` | bytes of request body (Content-Length or decoded chunked) (413) |
| `read_timeout` | ms (or `none`): the whole time allowed to receive one request's head and body, from its first byte; also the TLS handshake's timeout (408) |
| `idle_timeout` | ms (or `none`): how long a connection (new or kept alive) may wait for the first byte of a request; then it is closed silently |
| `max_connections` | open connections at once; further ones get 503 and are closed |
| `on_error` | `none` or `fn(Unknown, Request) -> Unknown`: called with every error a handler or a body producer throws; if it returns a `Reply` (and nothing was sent yet) that is sent instead of the default (§3.9) |

Argument checks in `serve`, **before** listening, each a `RuntimeError.ArgumentError` (`TYPE` is
`type_name(value)`, `N` the value printed with `"" + value`):

- handler not a Function: `http: the handler must be a function, got TYPE`
- `max_head`, `max_body`, `max_connections` not a whole Number ≥ 1:
  `http: max_head must be a whole number of at least 1, got N` (same text with the option's name;
  a non-Number shows its TYPE in place of N)
- `read_timeout`, `idle_timeout` not `none` and not a whole Number ≥ 0:
  `http: read_timeout must be none or a whole number of at least 0, got N`
- `tls`: §2's message. `on_error` not `none` or a Function:
  `http: on_error must be a function, got TYPE`

Checks use `type_name(v) == "Number"` first, then `v % 1 == 0` (so no `<` is evaluated on a
non-Number; remember `&` doesn't short-circuit, §8).

`Request` methods:

| method | result |
|---|---|
| `header(name: String) -> Unknown` | first value of header `name` (any case) or `none` |
| `header_all(name: String) -> Vector<String>` | every value, in order |
| `content_type() -> String` | the Content-Type's media type, lowercased, without parameters, trimmed; `""` when absent |
| `text() -> String` | the body, lossy UTF-8 (`to_text_lossy`) |
| `json() -> Unknown throws HttpError` | `json_lib.parse` of `text()`; a JsonError becomes `bad_request` "the body isn't valid JSON: " + `e.message()` |
| `query_pairs() -> Vector<Vector<String>> throws HttpError` | `url.parse_query(query)` (`[]` when `query` is none); a UrlError becomes `bad_request` "a bad query string: " + `e.description` |
| `query_param(name: String) -> Unknown throws HttpError` | first value for `name` in `query_pairs()`, or `none` |
| `form() -> Vector<Vector<String>> throws HttpError` | §3.8 |
| `multipart() -> Vector<Part> throws HttpError` | §3.8 |

A `bad_request` error is `HttpError { kind: "bad_request", method: req.method, url: req.target,
description }`. Add `"bad_request"` to `HttpError`'s doc comment ("a request a server handler
couldn't read: a bad query, JSON, form or multipart body").

`Printable for Request`: `"Request(" + method + " " + target + ")"`.

`Reply` static constructors and methods (`headers` arguments are a Map or `[name, value]` pairs,
validated by the existing `header_pairs`, whose ArgumentError messages they reuse):

| | |
|---|---|
| `Reply.new(status: Number = 200, body: Unknown = none, headers: Unknown = [:]) -> Reply` | as given (status and body checked, below) |
| `Reply.text(text: String, status: Number = 200, headers: Unknown = [:])` | Content-Type `text/plain; charset=utf-8` |
| `Reply.html(html: String, status = 200, headers = [:])` | `text/html; charset=utf-8` |
| `Reply.json(value: Unknown, status = 200, headers = [:])` | `json_lib.stringify(value)`, `application/json`; an unwritable value is ArgumentError `http: json ` + the JsonError's message (as `request` does) |
| `Reply.bytes(data: Bytes, content_type: String = "application/octet-stream", status = 200, headers = [:])` | |
| `Reply.redirect(location: String, status: Number = 302)` | `Location` header, no body |
| `Reply.empty(status: Number = 204)` | no body |
| `Reply.stream(producer: fn(BodyWriter) -> Unknown, status = 200, headers = [:])` | body streamed (§3.6) |
| `header(self, name) -> Unknown` | first value (any case) or `none` |
| `set_header(self, name: String, value: String) -> Reply` | removes every header named `name` (any case), appends `[name, value]`, returns `self` (validated like `header_pairs`) |
| `add_header(self, name: String, value: String) -> Reply` | appends (for `Set-Cookie`), returns `self` |

The constructors put their Content-Type first and then the given headers, except that a given
Content-Type (any case) replaces theirs. Construction-time checks (ArgumentError):
`http: a Reply status must be a whole number from 200 to 999, got N`;
`http: a Reply body must be none, a String, Bytes or a function, got TYPE`. (1xx replies are
deferred, §7.)

`BodyWriter.write(self, data: Unknown) throws socket.SocketError`: `data` a String (its UTF-8) or
Bytes, else ArgumentError `http: write takes a String or Bytes, got TYPE`; empty data writes
nothing (never a zero-size chunk). Modes (§3.6): `"chunked"` sends `HEX\r\n` + data + `\r\n` as
**one** `conn.send`; `"length"` sends data and lowers `remaining`, and writing past it is
ArgumentError `http: the body is longer than its Content-Length (N)` (nothing of that write is
sent); `"close"` sends data as is. `written` counts body bytes.

`Part`: `text() -> String` (lossy) and `header(name) -> Unknown`.

`Server` methods:

| | |
|---|---|
| `url(self, path: String = "/") -> String` | `scheme + "://" + h + ":" + port + path`; scheme `https` when `tls`; `h` is `127.0.0.1` when host is `0.0.0.0`, `[::1]` when host is `::`, `"[" + host + "]"` for any other host containing `:`, else host |
| `shutdown(self, grace: Unknown = 10000)` | begins a graceful shutdown and returns at once (§3.10); calling it again does nothing |
| `wait(self)` | returns once the server has shut down and every connection has ended; returns at once if that already happened |
| `close(self, grace: Unknown = 10000)` | `shutdown(grace)` then `wait()` |
| `connections(self) -> Number` | open connections now |

`Printable for Server`: `"Server(" + url without the trailing path + ")"`, i.e.
`Server(http://127.0.0.1:8080)`.

`reason_phrase(status)`: 100 Continue, 101 Switching Protocols, 200 OK, 201 Created, 202 Accepted,
203 Non-Authoritative Information, 204 No Content, 205 Reset Content, 206 Partial Content, 300
Multiple Choices, 301 Moved Permanently, 302 Found, 303 See Other, 304 Not Modified, 307 Temporary
Redirect, 308 Permanent Redirect, 400 Bad Request, 401 Unauthorized, 402 Payment Required, 403
Forbidden, 404 Not Found, 405 Method Not Allowed, 406 Not Acceptable, 408 Request Timeout, 409
Conflict, 410 Gone, 411 Length Required, 412 Precondition Failed, 413 Content Too Large, 414 URI Too
Long, 415 Unsupported Media Type, 416 Range Not Satisfiable, 417 Expectation Failed, 421 Misdirected
Request, 422 Unprocessable Content, 426 Upgrade Required, 428 Precondition Required, 429 Too Many
Requests, 431 Request Header Fields Too Large, 500 Internal Server Error, 501 Not Implemented, 502
Bad Gateway, 503 Service Unavailable, 504 Gateway Timeout, 505 HTTP Version Not Supported; any
other status `""` (the status line is then `HTTP/1.1 599 ` with its space).

`http_date(timestamp)`: `time.format(time.utc(t), "%a, %d %b %Y %H:%M:%S GMT")` with
`t = timestamp - timestamp % 1` (whole seconds), e.g. `Wed, 07 Oct 2026 12:00:00 GMT`.

### 3.3 Shared wire helpers (refactor; all private)

Place them in a `# -- wire format` section **above** both the client's `exchange` and the
server code (a top-level `fn` may only call functions declared above it, §8). Exact shapes:

```mah
# A malformed message. `status` is the server's answer (400, 413, 414, 431, 501, ...);
# `closed` is true when the peer closed mid-message.
struct WireError { status: Number, closed: Bool, description: String }
impl Error for WireError { fn message(self) -> String { self.description } }

# How long each wait may take: `each` (ms or none) per wait, or, when `deadline` (a
# time.monotonic() * 1000 instant) is set, whatever is left of it (0 at the least).
struct Wait { each: Unknown, deadline: Unknown }

# What is left of a size limit across several lines (a message head): `left` bytes of
# `limit`, or both none for no limit.
struct Budget { left: Unknown, limit: Unknown }
```

| helper | behavior |
|---|---|
| `now_ms() -> Number` | `time.monotonic() * 1000` |
| `wait_ms(w: Wait) -> Unknown` | `w.each` when `w.deadline == none`; else `max(0, w.deadline - now_ms())` |
| `fill(conn, w: Wait) -> Bool throws socket.SocketError` | appends the next bytes to arrive onto `conn.buffer`; `false` once the peer has closed. Algorithm: `let held = conn.buffer`; `conn.buffer = "".to_bytes()` (so `recv` reads the network, not the buffer); `chunk = conn.recv(65536, wait_ms(w))`, on a SocketError restore `conn.buffer = held` and rethrow; `held.extend(chunk)`; `conn.buffer = held`; result `chunk.len() > 0` |
| `read_line_within(conn, w: Wait, budget: Budget) -> Unknown throws WireError \| socket.SocketError` | when `budget.limit == none`: exactly `conn.read_line(wait_ms(w))` (the client's path, unchanged). Otherwise: loop { `at = conn.buffer.index_of("\n" bytes)`; if found at `n`: if `n + 1 > budget.left` throw `too_long(budget)`; take `conn.buffer[..n]`, keep `[n + 1..]`, `budget.left = budget.left - (n + 1)`, drop one trailing `\r` (byte 13), `to_text()` or throw `WireError { status: 400, closed: false, description: "the request head isn't valid UTF-8 text" }`, return it; else if `conn.buffer.len() >= budget.left` throw `too_long(budget)`; else if `!fill(conn, w)` return `none` } |
| `too_long(b: Budget) -> WireError` | `WireError { status: 431, closed: false, description: "the request head is larger than " + b.limit + " bytes" }` |
| `read_exactly(conn, n, w: Wait) -> Bytes throws socket.SocketError` | `while conn.buffer.len() < n { if !fill(conn, w) { throw socket.SocketError { kind: "closed_early", op: "recv_exactly", address: conn.address(), description: "the connection closed before " + n + " bytes arrived" } } }`, then take `conn.buffer[..n]` (exactly `Socket.recv_exactly`'s error) |
| `read_fields(conn, w, budget, strict: Bool) -> Vector<Vector<String>> throws WireError \| socket.SocketError` | header lines up to the empty line, as `[name, value]` pairs: name = text before the first `:` (trimmed when not strict), value = text after it, trimmed. A line `none` throws `WireError { status: 400, closed: true, description: "the connection closed mid-message" }`; no `:` throws 400 `a header line without ':': LINE` (the client's existing text). **Strict** (server) only, checked in this order per line: a line starting with space or tab: 400 `obsolete line folding isn't supported`; no `:`; a name that is empty or has a character outside RFC 9110 `tchar` (letters, digits, ``!#$%&'*+-.^_`\|~``): 400 `a bad header name: NAME`; a `\r` left anywhere in the line: 400 `a bad header line`; more than 100 fields: 431 `more than 100 header fields` |
| `parse_hex(text)` | unchanged |
| `read_chunked(conn, w, max_body: Unknown, budget: Budget) -> Bytes throws WireError \| socket.SocketError` | the existing algorithm on the helpers above: size line via `read_line_within(conn, w, line_budget)` where `line_budget` is `Budget { left: 4096, limit: 4096 }` when `max_body != none` else unlimited, a `too_long` there becomes 400 `a chunk size line is too long`; `none` → the `closed` WireError above; size text = before `;`, `parse_hex` → `none` (or, when `max_body != none`, more than 15 hex digits) → 400 `a bad chunk size: LINE`; running total `> max_body` → 413 `the request body is larger than MAX bytes`; data via `read_exactly`; the line after data must be `""` else 400 `a chunk isn't followed by a line end`; trailers read with `read_fields(conn, w, budget, max_body != none)` and dropped |
| `find_header(pairs, name) -> Unknown`, `find_headers(pairs, name) -> Vector<String>` | first / every value, any case. `Response.header`/`header_all` become one-line calls to these |
| `header_pairs`, `has_header`, `without_header` | unchanged, shared by `Reply` |

Client changes (behavior-preserving): `exchange` builds `let w = Wait { each: timeout, deadline:
none }` and `let open = Budget { left: none, limit: none }`; its header loop becomes
`read_fields(conn, w, open, false)`; `read_chunked(conn, w, none, open)`; `line_of` stays. Every
call that can now throw `WireError` is wrapped so a `WireError` becomes
`invalid_response(method, shown, d)` where `d` is `"the connection closed mid-response"` when
`closed`, else its description. Content-Length and close-delimited bodies keep using
`conn.recv_exactly` / `read_to_end`. `mah/std/http.test.mh` must pass with **no edits**.

### 3.4 State and tasks

```mah
struct ConnEntry { conn: socket.Socket, busy: Bool }
struct ServerState {
    listener: socket.Listener,
    handler: Unknown,
    on_error: Unknown,
    tls: Unknown,
    max_head: Number,
    max_body: Number,
    read_timeout: Unknown,
    idle_timeout: Unknown,
    max_connections: Number,
    conns: Map<Number, ConnEntry>,     # by socket id
    closing: Bool,
    accepting: Bool,
    stopped: Bool,
    done: Promise<Unknown>,            # new_promise(), settled with none once stopped
    timer: Unknown                     # async.TimerId of the grace period, or none
}
```

`serve`: check arguments (§3.2), `let listener = socket.listen(port, host, backlog)`, build the
state (`accepting: true`), `detach accept_loop(state)`, return `Server { host, port:
listener.port, tls: tls != none, state }`.

`accept_loop(state)`: until it stops: `conn = state.listener.accept()`; a `SocketError` ends the
loop when `state.closing` or its kind is `"closed"`, otherwise `sleep_async(50)` and go on (never
a hot loop). With a conn: if `state.closing`, close it; elif `state.conns.len() >=
max_connections`, `detach refuse(state, conn)` (not counted); else `state.conns[conn.id] =
ConnEntry { conn, busy: false }` and `detach connection(state, conn)`. After the loop:
`state.accepting = false`, `maybe_stopped(state)`.

`refuse(state, conn)`: plain TCP: send the §3.5 error reply 503 `too many connections`, then
`linger(conn)`; TLS: just close. Every error swallowed.

`connection(state, conn)`: `try { run_connection(state, conn) } catch { _ => { } }` (it must
**never** fail: a failed, unobserved detached task stops the whole program at exit, MAHC_FORMAT
§6.4), then `try conn.close() else none`, `state.conns.remove(conn.id)`, `maybe_stopped(state)`.

`maybe_stopped(state)`: if `closing & !accepting & conns.len() == 0 & !stopped` (nest the `if`s):
`stopped = true`; `async.clear_timeout(timer)` when set; `settle(done, none)`.

### 3.5 Reading a request (`run_connection`)

```
if state.tls != none: conn.start_tls_server(state.tls, state.read_timeout)   (SocketError → return)
loop:
  entry.busy = false
  if state.closing: return
  if conn.buffer.len() == 0:
      chunk = conn.recv(65536, state.idle_timeout)      (any SocketError → return: idle timeout, closed, reset)
      if chunk.len() == 0: return                        (client closed)
      conn.buffer = chunk
  entry.busy = true
  w = Wait { each: none, deadline: read_timeout == none ? none : now_ms() + read_timeout }
  read the request (below); on WireError/SocketError apply the failure table and return
  call the handler (§3.9); write the reply (§3.6); if not keep-alive (§3.7): return
```

Reading, in order (`head = Budget { left: max_head, limit: max_head }`):

1. Request line: `read_line_within(conn, w, head)`, skipping lines that are `""` (they still use
   the budget). A `too_long` here is re-thrown as 414 `the request line is longer than MAX bytes`.
   `none` → closed.
2. Split on `" "`: exactly 3 non-empty parts, else 400 `a request line needs a method, a target
   and a version`. Method must be a `tchar` token: else 400 `a bad method: M` (case kept as
   sent). Version must be `HTTP/` + digit + `.` + digit, else 400 `a bad HTTP version: V`; major
   ≠ 1 → 505 `only HTTP/1.x is supported`. Behavior is HTTP/1.0 only for exactly `HTTP/1.0`,
   HTTP/1.1 for every other `HTTP/1.x`. Method `CONNECT` → 501 `CONNECT isn't supported`.
3. Target: starts with `/` → origin form: `path` = before the first `?`, `query` = after it or
   `none`. `*` with method `OPTIONS` → `path "*"`, `query none`. Starts with `http://` or
   `https://` (any case) → `url.parse`; `path` = its path (`/` when `""`), `query` = its query; a
   UrlError → 400. Anything else → 400 `a bad request target: T`.
4. Headers: `read_fields(conn, w, head, true)` (the line budget continues from the request line).
5. Host: an HTTP/1.1 request with no `Host` → 400 `a request without a Host header`; two or more
   → 400 `more than one Host header`.
6. Framing. `te` = every Transfer-Encoding value joined with `,`, split on `,`, trimmed,
   lowercased, empty items dropped; `cl` = every Content-Length value likewise (not lowercased).
   - `te` and `cl` both present → 400 `both Transfer-Encoding and Content-Length`.
   - `te` present: HTTP/1.0 → 400 `Transfer-Encoding in an HTTP/1.0 request`; last item ≠
     `chunked` → 400 `chunked must be the last transfer coding`; more than one item → 501
     `only the chunked transfer coding is supported`; else chunked.
   - `cl` present: every item all ASCII digits and all items equal, else 400 `a bad Content-Length:
     V` (`V` = the values joined with `, `); more than 15 digits, or the value `> max_body` → 413
     `the request body is larger than MAX bytes`.
   - neither → no body.
7. Expect (only when the request has a body and is HTTP/1.1; ignored otherwise): the value,
   trimmed and lowercased, `100-continue` → after the 413 check above, `conn.send_text("HTTP/1.1 100
   Continue\r\n\r\n")` before reading the body; any other value → 417 `only 100-continue is
   supported in Expect`.
8. Body: `read_exactly(conn, n, w)` or `read_chunked(conn, w, max_body, Budget { left: max_head,
   limit: max_head })`.

Failure table (the reply is §3.6's **error reply**; every one closes the connection after a
`linger`):

| failure | reply |
|---|---|
| `WireError` with `closed`, or SocketError `closed_early`/`closed`/`connection_reset`/`other`, before or during the request | none: close silently |
| any other `WireError` | its `status`, its description as the body |
| SocketError `timed_out` after the first byte | 408 `the request took too long to arrive` |
| SocketError `invalid_utf8` (only reachable through the client path) | 400 `the request head isn't valid UTF-8 text` |

**Error reply** (`error_reply(status, text)`): `Reply` with that status, body `text`, header
`Content-Type: text/plain; charset=utf-8`, written by §3.6 with keep-alive off (so it carries
`Connection: close`; for a HEAD request, no body). **`linger(conn)`**: `conn.shutdown()`, then
`conn.recv(65536, 1000)` repeatedly, discarding, until empty Bytes, an error, or 262144 bytes in
all; every error swallowed (so the client reads the reply instead of a reset).

### 3.6 Writing a reply

`write_reply(state, conn, req, reply, keep: Bool) -> Bool` (the final keep-alive decision).
Validate first; a failure here is a handler error (§3.9) — status not a whole Number 200–999,
`header_pairs(reply.headers)` throwing, or a body of another type (the §3.2 messages).

1. `headers` = the reply's pairs without `content-length`, `transfer-encoding` and `connection`
   (any case) — except a stream body keeps a valid `content-length` (all digits) and uses it. A
   reply header `Connection` whose tokens include `close` sets `keep = false`.
2. Body plan: `none` → empty; String → its UTF-8; Bytes → as is; a function → stream.
   No body is ever sent for status 204 or 304 (no Content-Length, no Transfer-Encoding, the
   producer isn't called) or for a HEAD request (headers as for GET, the producer isn't called).
3. Stream framing: with a reply Content-Length → mode `"length"`; else HTTP/1.1 → `"chunked"`
   with `Transfer-Encoding: chunked`; else (HTTP/1.0) → `"close"` and `keep = false`.
4. Head: `"HTTP/1.1 " + status + " " + reason_phrase(status) + "\r\n"`, then the reply's headers
   in order, then the server's in this order: `Content-Type` when the reply has none (a String
   body: `text/plain; charset=utf-8`; Bytes: `application/octet-stream`; otherwise none);
   `Content-Length: N` (fixed bodies, except 204/304) or `Content-Length` kept from step 1 or
   `Transfer-Encoding: chunked` (streams); `Date: http_date(time.now())` unless the reply has one;
   `Connection: close` when `!keep` or `state.closing`, `Connection: keep-alive` when keep-alive
   is on for an HTTP/1.0 request; nothing for HTTP/1.1 keep-alive. Then `\r\n`.
5. Fixed body: head and body in **one** `conn.send`. Stream: send the head, call
   `producer(writer)`; afterwards `"chunked"` sends `0\r\n\r\n`; `"length"` with `remaining > 0`
   counts as a producer error (ArgumentError `http: the body is shorter than its Content-Length
   (N)`); `"close"` sets `keep = false`.
6. A producer that throws after the head was sent: `on_error` is called (its result ignored)
   and the connection is closed without a final chunk (`keep = false`). Return `keep &
   !state.closing`.

### 3.7 Keep-alive

Connection tokens = every `Connection` value joined, split on `,`, trimmed, lowercased. A
request keeps the connection when: HTTP/1.1 and no `close` token, or HTTP/1.0 and a
`keep-alive` token. It is closed after the reply when the request doesn't keep it, the reply
says `Connection: close`, the stream is close-delimited, a producer failed, the server is
closing, or any §3.5 failure happened. Pipelined requests are served in order (bytes already in
`conn.buffer` skip the idle wait). No `Keep-Alive` header is sent or read; no per-connection
request cap.

### 3.8 Bodies: form and multipart

`form()`: `content_type()` `application/x-www-form-urlencoded` → body `to_text()` (`none` → bad_request
`the form isn't valid UTF-8 text`) → `url.parse_query` (UrlError → bad_request `a bad form: ` +
description). `multipart/form-data` → `[part.name, part.text()]` for every part whose `filename` is
`none`, in order. Anything else → bad_request `the body isn't a form (Content-Type is X)`, `X` the
header value or `none`.

`multipart()`: needs `content_type() == "multipart/form-data"` else bad_request `the body isn't
multipart/form-data (Content-Type is X)`. Parameters of a header value (`header_params(value) ->
Vector<Vector<String>>`, private, shared with Content-Disposition): the text after the first `;`,
split on `;` outside double quotes; each `name=value` with the name trimmed and lowercased and the
value trimmed; a value in double quotes is unquoted, `\x` giving `x`; items without `=` dropped.
`boundary`: missing, empty or longer than 70 characters → bad_request `a multipart body without a
boundary`. On the body Bytes, with `delim = ("--" + boundary).to_bytes()`:

1. First `delim` anywhere (preamble ignored); none → bad_request `the multipart body doesn't
   contain its boundary`. `pos` = just after it.
2. Loop: the 2 bytes at `pos` are `--` → done (epilogue ignored). Otherwise skip spaces and
   tabs, then require `\r\n` else bad_request `a malformed multipart boundary line`.
3. Part head: if the bytes at `pos` are `\r\n`, no headers; else find `\r\n\r\n` in
   `body[pos..]` (none → bad_request `a multipart part without the end of its headers`); the
   head as text (`none` → bad_request `a multipart part's headers aren't UTF-8 text`), split on
   `\r\n`, each line `name: value` (no `:` → bad_request `a malformed multipart header: LINE`).
4. Content: up to the next `"\r\n" + delim` in the rest (none → bad_request `the multipart body
   doesn't end with its closing boundary`); `pos` = after that delimiter.
5. `Content-Disposition` required, first item (before `;`, trimmed, lowercased) `form-data`, else
   bad_request `a multipart part isn't form-data`; its `name` parameter required (bad_request `a
   multipart part without a name`); `filename` → the String or `none` (`filename*` ignored);
   `content_type` = the part's Content-Type or `none`.

### 3.9 Calling the handler

`call(state, req) -> Reply`: `r = (state.handler)(req)` inside `try`. A result that isn't a
`Reply` (`type_name(r) != "Reply"`) becomes the error `RuntimeError.ArgumentError { message:
"http: the handler must return an http.Reply, got TYPE" }`. On an error `e`:

1. `on_error` set: `x = try (state.on_error)(e, req) else none`; if `x` is a `Reply` use it.
2. Else `e` is an `HttpError` with kind `bad_request` → `Reply.text(e.description, status: 400)`.
3. Else `Reply.text("Internal Server Error", status: 500)`.

These replies keep the connection alive (the request was fully read). If writing the chosen reply
itself fails validation (§3.6), the 500 is sent instead.

### 3.10 Shutdown

`shutdown(grace)` (once; later calls do nothing): `closing = true`; `try listener.close()`; close
every entry with `busy == false` (its pending `recv` fails `closed`, the task ends); if `grace !=
none`, `timer = async.set_timeout(fn() { force_close(state) }, grace)` where `force_close` closes
every remaining conn with every error caught (the callback type is `fn() throws never`);
`maybe_stopped(state)`. Busy connections finish their current reply (with `Connection: close`) and
end. `wait()`: `self.state.done.await`. Calling `shutdown` from inside a handler is fine (it never
waits); calling `close`/`wait` there waits for that handler's own connection until `grace`.

A server that is never shut down keeps the program running (its pending `accept`); so a script
can end with `http.serve(...).wait()`. Tests must always shut their servers down (`defer
server.close()`), since a pending accept would keep a test running.

### 3.11 Tests (Part A)

All on both VMs. Wire checks use raw `socket.connect` and compare text; `Date` values are never
compared (check `^[A-Z][a-z]{2}, \d\d [A-Z][a-z]{2} \d{4} \d\d:\d\d:\d\d GMT$` with std:regex
where needed). Unless said otherwise, a raw request's reply is read with a helper that reads the
head lines and then Content-Length / chunked body.

`mah/std/http_server.test.mh` (`import http from "./http"`, `import socket from "./socket"`, as
`http.test.mh` does), at least these tests:

1. **a GET**: handler records the Request; `http.get(server.url("/a/b?x=1&y=2"))` → 200, text,
   `content-type` `text/plain; charset=utf-8`, `content-length` the byte count, Date matches; the
   Request had method `GET`, target `/a/b?x=1&y=2`, path `/a/b`, query `x=1&y=2`, version
   `HTTP/1.1`, header `host` `127.0.0.1:PORT`, `peer_host` `127.0.0.1`, `tls` false, body
   `Bytes[]`; `req.query_param("y")` `2`; `req.to_string()` `Request(GET /a/b?x=1&y=2)`.
2. **request bodies**: `http.post(... body: "héllo")` → `req.text()`; `json:` → `req.json()`;
   `form: ["q": "a b", "n": 1]` → `req.form()` `[[q, a b], [n, 1]]`; invalid JSON → client sees
   400 with body `the body isn't valid JSON: ...` (starts with); `?q=%zz` + `query_pairs()` → 400
   `a bad query string: ...`.
3. **keep-alive**: one raw connection, two `GET` requests in turn → two 200s, no `Connection`
   header; a third with `Connection: close` → `Connection: close` and then `recv()` is empty.
4. **pipelining**: two requests in one `send_text` → two replies in order.
5. **HTTP/1.0**: no Host needed, reply has `Connection: close` and the socket closes;
   `Connection: keep-alive` → reply `Connection: keep-alive`, socket stays open for a second.
6. **chunked request**: `Transfer-Encoding: chunked`, `5\r\nhello\r\n6;x=y\r\n world\r\n0\r\nT: 1\r\n\r\n`
   → body `hello world`.
7. **100-continue**: send the head with `Expect: 100-continue` and `Content-Length: 4`, read
   `HTTP/1.1 100 Continue` and an empty line, send the body, read 200; `Expect: 100-continue`
   with Content-Length over `max_body` → 413 without sending the body; `Expect: x` → 417.
8. **protocol errors**: each raw request → exact status line and body from §3.5's texts:
   `BAD\r\n\r\n` 400; `GET / HTTP/1.1\r\n\r\n` 400 (Host); `GET / HTTP/2.0` 505; `G@T / HTTP/1.1` 400;
   `GET x HTTP/1.1` 400 target; header `bad` 400; folded header 400; `Content-Length: abc` 400;
   two different Content-Lengths 400; TE + CL 400; `Transfer-Encoding: gzip, chunked` 501;
   `Transfer-Encoding: chunked, gzip` 400; `CONNECT h:1 HTTP/1.1` 501; bad chunk size 400. Every
   one: `Connection: close` then the socket closes.
9. **limits**: `max_head: 64` → long request line 414, many headers 431; 101 headers 431;
   `max_body: 10` → Content-Length 11 → 413, chunked totaling 11 → 413.
10. **timeouts**: `read_timeout: 200` + half a request line then silence → 408; `idle_timeout:
    200` + connect and send nothing → `recv(65536, 2000)` empty (closed, no reply).
11. **handler errors**: throw → 500 `Internal Server Error`; return a String → 500; `on_error`
    returning `Reply.text("oops", status: 503)` → 503 `oops`, and it saw the thrown value; the
    connection stays usable after a 500.
12. **replies**: `Reply.html`, `bytes` (default type), `redirect` (`http.get(..., max_redirects:
    0)` → 302 + Location), `empty` (204, no Content-Length), 304, `add_header("Set-Cookie", ...)`
    twice → `header_all("set-cookie")` both, `set_header` replaces any case, a reply
    Content-Length is replaced by the real one, HEAD → Content-Length but no body, constructor
    argument errors (exact §3.2 messages, CRLF in a header value → `http: invalid header X`).
13. **streams**: `Reply.stream` writing `a`, `bc`, `""` → raw HTTP/1.1 wire `Transfer-Encoding:
    chunked` and exactly `1\r\na\r\n2\r\nbc\r\n0\r\n\r\n`; with header `Content-Length: 3` → no
    chunking, body `abc`; over-long write → (connection closed, client errors); HTTP/1.0 →
    close-delimited; producer throwing after a write → client `http.get` fails with
    `invalid_response: the connection closed mid-response`.
14. **multipart**: a raw body with a text field, a field with a quoted `name="a;b"` and a file
    (`filename="x.txt"`, `Content-Type: text/plain`, 3 binary bytes 0, 255, 10) → `multipart()`
    names, filenames, content types, bodies; `form()` → the two text fields; missing boundary,
    no closing boundary, no name, not form-data → 400 with those texts.
15. **shutdown**: a handler sleeping 300 ms is in flight; `server.shutdown()`; a new
    `socket.connect` fails `connection_refused`; the in-flight request gets 200 with
    `Connection: close`; `wait()` returns; `connections()` 0. An idle keep-alive connection is
    closed by `shutdown` at once (`recv` empty within 500 ms). `shutdown(grace: 100)` with a
    handler sleeping 5000 → the client fails, `wait()` returns in under 2 s.
16. **concurrency**: two requests to a handler sleeping 300 ms, detached together → both 200 in
    under 550 ms total (`time.monotonic()`).
17. **max_connections: 1**: hold one connection open idle; a second gets `503` `too many
    connections` and is closed.
18. **serve errors**: each §3.2 ArgumentError message; a second `serve` on the same port →
    `socket.SocketError` kind `address_in_use`; `tls: "x"` → §2's message.
19. **printing and urls**: `Server(http://127.0.0.1:PORT)`, `url("/x")`, host `0.0.0.0` →
    `http://127.0.0.1:PORT/`; `reason_phrase(404)` `Not Found`, `(599)` `""`; `http_date(0)`
    `Thu, 01 Jan 1970 00:00:00 GMT`.

`tests/test_http_server.py` (Python clients; runs on whichever VM `MAH_TEST_VM` selects): a
`MahServer` helper picks a free port in Python, runs this program with `run_source` in a
background thread, polls `connect` until the port answers (10 s), and on exit sends `GET /__quit`
and joins the thread, asserting its output ends with `stopped\n`:

```mah
import http from "std:http"
let running = []
APP_SOURCE                       # defines fn app(req: http.Request) -> http.Reply
fn handle(req: http.Request) -> http.Reply {
    if req.path == "/__quit" {
        running[0].shutdown()
        return http.Reply.text("bye")
    }
    app(req)
}
let server = http.serve(PORT, handle OPTIONS)
running.push(server)
server.wait()
print("stopped")
```

Cases: `http.client.HTTPConnection` making three requests on **one** connection (same
`conn.sock` object afterwards); `encode_chunked=True` upload of 3 chunks echoed back; a 5 MB
Content-Length upload echoed as its length; a hand-built multipart upload via `urllib.request`;
raw 100-continue; a slowloris head (1 byte every 100 ms, `read_timeout: 500`) → 408 within 2 s;
20 parallel `urllib` GETs to a handler sleeping 200 ms finish in under 3 s; HTTP/1.0 via raw
socket; graceful shutdown while a request is in flight. Plus `CheckerTests`: `check(HTTP)` has
no diagnostics other than kind `implicit`; `check(HTTP + 'let s = http.serve(0, fn(r: http.Request)
-> http.Reply { http.Reply.text("x") })')` types `s` as `Server`; and `BytecodeTests`:
`minor_of(HTTP + "print(http.serve)") == MINOR` (the constant, so it holds before and after Part B).

`runtime/tests/vm_diff.py` case `std_http_server` (after `std_tls_url_http`): a program that
serves on port 0 and prints, with ports and dates kept out of the output: a GET's status, body and
`content-type`; a raw keep-alive exchange (two status lines); a POST form echoed; a 400 and a 413
status line and body; a chunked stream's raw bytes; a 500; `reason_phrase` of a few codes; then
`close()` and `connections()`.

`examples/http_server.mh` (golden test `test_http_server`; bytecode map `"http_server.mh": 20`):

```mah
# std:http's server (M42): a handler function answers requests, and std:http's
# client talks to it -- in one program, so it runs offline.

import http from "std:http"

fn handle(req: http.Request) -> http.Reply {
    if req.path == "/" { return http.Reply.text("hello from mah") }
    if req.path == "/greet" {
        let name = req.query_param("name")
        let who = if name == none { "stranger" } else { name }
        return http.Reply.json(["greeting": "hi " + who])
    }
    if req.path == "/echo" & req.method == "POST" { return http.Reply.json(["you_sent": req.form()]) }
    if req.path == "/count" {
        return http.Reply.stream(fn(w: http.BodyWriter) {
            for let i in 1..4 { w.write("" + i + "\n") }
        })
    }
    http.Reply.text("no route for " + req.path, status: 404)
}

let server = http.serve(0, handle)
let r = http.get(server.url("/"))
print(r.status, r.header("content-type"), r.text())
print(http.get(server.url("/greet?name=mah")).text())
print(http.post(server.url("/echo"), form: ["name": "mah", "n": 1]).text())
let counted = http.get(server.url("/count"))
print(counted.header("transfer-encoding"), counted.text().lines())
let missing = http.get(server.url("/nope"))
print(missing.status, missing.text())
server.close()
print("stopped with", server.connections(), "connections")
```

Expected output (verify by running; it must be identical on both VMs):

```
200 text/plain; charset=utf-8 hello from mah
{"greeting":"hi mah"}
{"you_sent":[["name","mah"],["n","1"]]}
chunked [1, 2, 3]
404 no route for /nope
stopped with 0 connections
```

`mah format` the new `.mh` files; `make test` and `make test-rust` green.

### 3.12 Docs (Part A)

- `docs/STDLIB.md` `### std:http`: retitle the intro to "an HTTP/1.1 client and server"; keep the
  client bullets; add **Server** bullets mirroring §1 and §3.2 (surface, handler shape, Request /
  Reply / BodyWriter / Part, limits and their statuses, keep-alive, 100-continue, form/multipart,
  error mapping and `on_error`, shutdown, concurrency, TLS via `socket.tls_server_config`). Change
  "One connection per request ... no keep-alive" to say it is the **client** that uses one
  connection per request.
- `docs/V2_DESIGN.md`: a new item after M39's (`45. **M42 — the HTTP server and server TLS. ✅
  Landed.**`) in the style of M38/M39: design (Mah over std:socket, shared wire helpers), the
  surface, limits/statuses, shutdown, tests; **and these two bullets verbatim for Part B**:
  - "**Server TLS** (`docs/contracts/M42_http_server.md` Part B), both VMs: `socket.
    tls_server_config(cert_path, key_path)` loads a PEM chain and key once into the socket table
    (Python: an `ssl.SSLContext(PROTOCOL_TLS_SERVER)`, TLS 1.2+; Rust: an
    `Arc<rustls::ServerConfig>`, ring provider) and checks them, failing with kind `tls_config`;
    `socket.start_tls_server(id, config, timeout)` runs the server handshake, after which
    `send`/`recv` go through TLS exactly as for a client (Rust's `Conn` now holds a
    `rustls::Connection`). No client certificates, no ALPN. std:socket wraps them as
    `tls_server_config` (a `TlsServerConfig`) and `Socket.start_tls_server`."
  - "**Versioning**: MINOR 20; the two natives are 1.20. std:socket declares them, so every
    std:socket and std:http program is 1.20. Test changes: MINOR pins 19 → 20, unsupported-minor
    tests use 21, `sockets.mh`/`http_client.mh` expected minors 20."
  Status paragraph: add M42, "currently 1.20", and "the HTTP server" leaves the "what comes next"
  list (GitHub packages and multithreaded `detach` remain; the framework is a separate repository).
- `docs/NEXT_PHASES.md` "The HTTP server": `Landed as M42 -- see docs/V2_DESIGN.md's M42 entry
  and docs/contracts/M42_http_server.md.` plus one line on what was deferred (§7). In "A
  NestJS/Hono-style web framework" add one sentence: it will live in a separate repository, built
  on `http.serve`'s `fn(Request) -> Reply` handlers.
- `mah/project/templates/docs/mah-language.md`: a server paragraph and example under `std:http`;
  in "Not available" drop "TLS, an HTTP client or server" (keep UDP; add "HTTP/2, WebSockets").
- `www/src/content/std/http.md`: a "Server" section; its version line says 1.20.
  `www/src/content/docs/standard-library.md`: the std:http paragraph mentions the server.

---

## 4. Part B: server TLS

### 4.1 Files (Part B owns these)

`mah/socket_natives.py`, `mah/bytecode/format.py`, `runtime/src/vm/socket.rs`,
`runtime/src/vm/link.rs`, `runtime/src/vm/natives.rs`, `runtime/src/decode.rs`,
`mah/std/socket.mh`, `mah/std/socket.test.mh`, `tests/test_tls_server.py` (new), the pins in
§4.6, `runtime/tests/vm_diff.py` (case `std_tls_server` immediately **after** `std_socket`),
`docs/MAHC_FORMAT.md`, `docs/STDLIB.md` (`### std:socket` section and Phase 4 decisions only),
`docs/RUST_VM.md` (the rustls line), the template's `std:socket` paragraph,
`www/src/content/std/socket.md`, the std:socket paragraph of `www/src/content/docs/standard-library.md`.

Do **not** touch `mah/std/http.mh`, `examples/`, `docs/V2_DESIGN.md`, `docs/NEXT_PHASES.md`.

### 4.2 The natives (normative; MAHC_FORMAT §4.4 gets these rows)

| name | arity | |
|---|---|---|
| `socket.tls_server_config` | 2 | *(1.20)* `cert_path, key_path` → `id` (a socket-table id of kind TLS config) once the PEM certificate chain and private key have been read, parsed and checked to match |
| `socket.start_tls_server` | 3 | *(1.20)* `id, config, timeout` → `none` once a TLS **server** handshake on the open socket `id`, with config `config`, has finished; from then on `send`/`recv`/`shutdown`/`close` behave as after `start_tls` |

Both return a pending Promise settled by a worker with a result, like every socket native.
Argument checks, on the VM's thread, before anything else (same text on both VMs):

- `tls_server_config`: `cert_path` not a String → TypeMismatch `tls_server_config: certificate
  path must be a String, got TYPE`; `key_path` → `tls_server_config: key path must be a String,
  got TYPE`.
- `start_tls_server`: `id` not a Number → TypeMismatch `start_tls_server: expected a socket id,
  got TYPE`; `config` not a Number → TypeMismatch `start_tls_server: expected a TLS config id,
  got TYPE`; `timeout` → the usual `start_tls_server: timeout must be ...` messages.
- `start_tls_server` settles at once with `closed` when `id` isn't an open socket **or** `config`
  isn't an open TLS config (fractional/negative ids count as not open). A TLS config id given to
  any other socket native is "not open" for it (`closed`), as a listener id is for `recv`.
  `socket.close(config)` removes a config (returns `none`; twice is fine).

`tls_server_config` failures: kind **`tls_config`**, with one of these descriptions (the first
that applies, in this order; unlike the other kinds the description isn't fixed per kind but is
one of this fixed set):

1. `can't read the certificate file` — opening/reading `cert_path` fails (any OS error).
2. `can't read the private key file` — likewise `key_path`.
3. `no certificate in the certificate file` — the cert bytes lack `-----BEGIN CERTIFICATE-----`.
4. `the private key is encrypted` — the key bytes contain `-----BEGIN ENCRYPTED PRIVATE KEY-----`
   or `Proc-Type: 4,ENCRYPTED`.
5. `no private key in the key file` — the key bytes contain none of `-----BEGIN PRIVATE KEY-----`,
   `-----BEGIN RSA PRIVATE KEY-----`, `-----BEGIN EC PRIVATE KEY-----`.
6. `the private key doesn't match the certificate`.
7. `the certificate or private key isn't usable` — any other parse/load failure.

Steps 3–5 are a plain byte search done identically by both VMs (`pem_problem(cert, key)`) before
any TLS library sees the data, so the common mistakes classify the same everywhere. Supported keys
(tested): RSA 2048+ and ECDSA P-256. Others (Ed25519, P-384) are expected to work on both but are
untested; exotic ones may load on one VM only (documented, not tested).

`start_tls_server` failures: as `start_tls`'s: `tls` (the client doesn't speak TLS, a handshake
alert, calling it on a socket that already uses TLS), `connection_reset` (the client closed during
the handshake), `timed_out`, `closed`. TLS 1.2 and 1.3; no client certificates; no ALPN; no SNI
handling (one certificate). After the handshake a ragged EOF from the client is a normal end, and
`shutdown` sends no close notice, exactly as for client sockets.

### 4.3 Python (`mah/socket_natives.py`)

- `class _TlsServerConfig` with `__slots__ = ("context",)` and a no-op `close(self)`; stored as
  `_Entry("tls_config", _TlsServerConfig(ctx))` via `_register`. (The no-op `close` keeps
  `_close` and `_IoHub.close`, which call `entry.sock.close()`, working unchanged.)
- `class _TlsConfigProblem(Exception)` carrying the description; add it to `_guarded`'s `except`
  tuple and to `_classify` (→ `_failure("tls_config", exc.args[0])`). Add `"tls_config"` to `KINDS`
  (via `DESCRIPTIONS` with the generic text 7, the module docstring noting its descriptions vary).
- `_tls_server_config(ctx, args)`: checks, then a job: read both files in binary (`OSError` →
  problem 1 / 2), `_pem_problem(cert, key)` (3–5), then `ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)`,
  `ctx.minimum_version = ssl.TLSVersion.TLSv1_2`, `ctx.load_cert_chain(cert_path, key_path,
  password=lambda: b"")` (the callback keeps OpenSSL from ever prompting on a terminal); an
  `ssl.SSLError` whose `reason == "KEY_VALUES_MISMATCH"` → 6, any other `ssl.SSLError`/`OSError`/
  `ValueError` → 7. Returns `Decimal(id)`.
- `_start_tls_server(ctx, args)`: checks; `entry = _lookup(ctx, sock_id, "socket")`,
  `config = _lookup(ctx, config_id, "tls_config")`; either `None` → `_closed_now()`. Job: an
  `ssl.SSLSocket` already → `ssl.SSLError("the socket already uses TLS")`; `tls =
  config.sock.context.wrap_socket(entry.sock, server_side=True, do_handshake_on_connect=False)`,
  `entry.sock = tls`, then the same handshake loop as `_start_tls` — factor it into
  `_handshake(ctx, sock_id, entry, tls, deadline)` and use it from both.
- `NATIVES`: `"socket.tls_server_config": (2, _tls_server_config)`, `"socket.start_tls_server":
  (3, _start_tls_server)`. Module docstring: an M42 paragraph.

### 4.4 Rust (`runtime/src/vm/socket.rs` and the tables)

- `Entry::TlsConfig(Arc<rustls::ServerConfig>)`; `close_entry` unchanged (dropping).
- `Conn.tls: Mutex<Option<rustls::Connection>>` (the enum of client and server); `start_tls`
  stores `Connection::Client(..)`; `tls_send`/`tls_recv` work on `rustls::Connection` unchanged
  in logic. Factor the handshake loop into `fn handshake(table, id, conn: &Conn, tls:
  rustls::Connection, deadline) -> IoValue` used by `start_tls` and `start_tls_server`.
- `pub fn tls_server_config(vm, args)`: checks; a job: `std::fs::read` each file (→ 1 / 2),
  `pem_problem(&cert, &key)` (a pure function, 3–5), `CertificateDer::pem_slice_iter(&cert)`
  collected (error or empty → 7), `PrivateKeyDer::from_pem_slice(&key)` (error → 7),
  `ServerConfig::builder_with_provider(Arc::new(rustls::crypto::ring::default_provider()))
  .with_safe_default_protocol_versions()?.with_no_client_auth().with_single_cert(certs, key)`;
  `Err(rustls::Error::InconsistentKeys(_))` → 6, other `Err` → 7. **Verify** against the
  `rustls` version in `Cargo.lock` that a mismatched key is rejected; if `with_single_cert` doesn't
  check it, build the `CertifiedKey` with `CertifiedKey::from_der` and call `keys_match()`
  yourself — the `key_mismatch` test (§4.7) pins the behavior on both VMs. Register
  `Entry::TlsConfig(Arc::new(config))`, result the id.
- `pub fn start_tls_server(vm, args)`: checks (a `config_arg` with the "TLS config id" message);
  look up both (`closed` when either isn't the right kind); job: lock `conn.tls`, `Some` → `tls`;
  `ServerConnection::new(config)` (error → `tls`); `handshake(..., Connection::Server(c), ...)`.
- `link.rs`: `NativeFn::SocketTlsServerConfig`, `NativeFn::SocketStartTlsServer`, entries
  `"socket.tls_server_config" => Some((2, ...))`, `"socket.start_tls_server" => Some((3, ...))`;
  `natives.rs` dispatch; `decode.rs`: `native_since_minor` → `Some(20)` for both, `MINOR = 20`.
- `description_of("tls_config")` → text 7; `tls_config` failures pass their description
  explicitly (`failure("tls_config", text)`).
- Unit tests in `socket.rs`: `pem_problem` for each case 3–5 and the all-good case; argument
  message helpers.
- No new crates (`rustls-pki-types` comes with `rustls`; use `rustls::pki_types::...`). Update the
  module doc comment and `runtime/Cargo.toml`'s rustls reason line ("std:socket's TLS clients
  (M39) and servers (M42)").

### 4.5 `mah/std/socket.mh`

Add the two externs and §2's API exactly:

```mah
extern fn native_tls_server_config(cert_path: String, key_path: String) -> Promise<
    Vector<Unknown>
> = "socket.tls_server_config"
extern fn native_start_tls_server(id: Number, config: Number, timeout: Unknown) -> Promise<
    Vector<Unknown>
> = "socket.start_tls_server"
```

- `tls_server_config(cert_path, key_path)`: `result(native_tls_server_config(...).await,
  "tls_server_config", cert_path)` → `TlsServerConfig { id, cert_path, key_path }` (so
  `e.message()` reads `tls_server_config: no certificate in the certificate file: cert.pem`).
- `TlsServerConfig.close()`: `result(native_close(self.id).await, "close", self.cert_path)`.
- `Printable for TlsServerConfig`: `"TlsServerConfig(" + self.cert_path + ")"`.
- `Socket.start_tls_server(config, timeout = none)`: `buffer` not empty → ArgumentError
  `start_tls_server: the socket has unread bytes in its buffer`; then `result(native_start_tls_server
  (self.id, config.id, timeout).await, "start_tls_server", self.address())`.
- Doc comments: SocketError's kinds gain `"tls_config"` (`tls_server_config` couldn't use the
  files); the header comment mentions TLS servers; `start_tls`'s doc stays.

### 4.6 Versioning: MINOR 19 → 20, and every pin

- `mah/bytecode/format.py`: `MINOR = 20` with an "M42 bumps MINOR to 20" docstring paragraph;
  `NATIVE_ARITIES` and `NATIVE_SINCE_MINOR` entries (20) under an `# M42 (1.20)` comment.
- `runtime/src/decode.rs`: `MINOR = 20`; `native_since_minor`; its test `assert_eq!(MINOR, 20)`.
- `MINOR, 19` → `20`: `tests/test_decorators.py:817`, `tests/test_hooks.py:1076`,
  `tests/test_reflection.py:718`, `tests/test_socket.py:105-107`, `tests/test_http.py:243-250`
  (also add `NATIVE_SINCE_MINOR["socket.tls_server_config"] == 20` etc. in test_socket; keep
  `assertLess(..., 19)` for std:url, it is still true).
- Unsupported-minor tests now use **21**: `tests/test_bytecode.py:179-199` (`data[6] = 21`,
  `"unsupported minor version 21"`, comment "M42"), `tests/test_bytes.py`
  `test_unsupported_minor_is_20` → `test_unsupported_minor_is_21` with 21.
- `tests/test_bytecode.py` example minors: `"sockets.mh": 20`, `"http_client.mh": 20` (and
  `"http_server.mh": 20`, see §5).
- Any other `19` the full test suite turns up (grep for `19` in `tests/` and `runtime/` after the
  bump) — fix and mention it in the report.

### 4.7 Tests (Part B)

`tests/test_tls_server.py` (both VMs; skip the whole module when `openssl` isn't on `PATH`).
`setUpClass` makes, in a temp dir, with `tests.test_http`'s `_make_ca`/`_make_leaf` (import them):
`ca`, `leaf` (RSA, `localhost` + `127.0.0.1`), `other` CA; plus an ECDSA P-256 leaf signed by
`ca` (`openssl ecparam -name prime256v1 -genkey -noout -out ec.key`, CSR, sign with the same
`ext.cnf`), an encrypted key (`openssl genpkey -algorithm RSA -aes256 -pass pass:x -out enc.key`),
`empty.pem` (no PEM blocks). `SSL_CERT_FILE` = `ca.pem` while Mah clients run.

1. **config errors** (exact `kind: message`): missing cert → `tls_config: tls_server_config:
   can't read the certificate file: PATH`; missing key → `... can't read the private key file:
   CERT`; `empty.pem` as cert → `no certificate in the certificate file`; encrypted key → `the
   private key is encrypted`; cert file as key → `no private key in the key file`; `leaf.pem` with
   `other.key` → `the private key doesn't match the certificate`; a cert file with a corrupted
   base64 body → `the certificate or private key isn't usable`. Printing:
   `TlsServerConfig(PATH)`.
2. **Mah server, Mah client** (one program): listen, detached accept + `start_tls_server(cfg,
   5000)` + `read_line` + `send_text` + close; client `connect_tls("localhost", port, 5000)` →
   sends a line, prints the answer. Once with the RSA leaf, once with the EC leaf.
3. **Mah server, Python client**: the Mah program (thread, port chosen by Python) accepts,
   handshakes, echoes a line; Python `ssl.create_default_context(cafile=ca.pem)` connects with
   `server_hostname="localhost"`, checks the echo and `version()` in `TLSv1.2`/`TLSv1.3`; a Python
   client restricted to TLS 1.2 (`maximum_version`) also works.
4. **handshake failures**: a plain-text client (`GET / HTTP/1.1\r\n\r\n`) → `tls`; a client that
   connects and closes → `connection_reset`; a silent client with timeout 300 → `timed_out`;
   `start_tls_server` twice → `tls`; Python client that doesn't trust the CA → server side `tls`.
5. **ids**: a config id given to `recv` → `closed`; a socket id as config → `closed`; after
   `cfg.close()` → `closed`; closing twice fine.
6. **argument errors**: every §4.2 message and the buffer ArgumentError (like
   `StartTlsArgumentTests`).
7. **bytecode**: `MINOR == 20`, arities 2/3, since-minors 20, `minor_of(SOCKET + ...) == 20`,
   `minor_of(HTTP + ...) == 20`.
8. **`HttpsServerTests`** (integration, needs Part A): decorate with
   `@unittest.skipUnless("export fn serve" in open(<repo>/mah/std/http.mh).read(), "needs M42 part A")`.
   `http.serve(0, handle, tls: socket.tls_server_config(leaf, key))` + `http.get(server.url("/hi"))`
   → `200 hello`, `req.tls` true, `server.url` starts with `https://127.0.0.1:`; Python
   `http.client.HTTPSConnection` doing two requests on one connection (keep-alive over TLS); a
   plain-HTTP request to the TLS port gets no HTTP reply (connection closed) and the server keeps
   serving.

`mah/std/socket.test.mh` additions (both VMs, files written with std:fs into `fs.temp_dir()`):
`tls_server_config` errors 1, 3, 4, 5 (`kind`, `description`, `op`, `address`) and
`start_tls_server` on a closed socket → `closed`.

`runtime/tests/vm_diff.py` `std_tls_server`: the argument-error probes (each message), missing
file / `no certificate` / `encrypted` / `no private key` errors via std:fs temp files, and
`start_tls_server` on a never-opened id. (No handshake: vm_diff has no certificates.)

### 4.8 Docs (Part B)

- `docs/MAHC_FORMAT.md`: title `(version 1.20)`; §3's list "… 19 for `socket.start_tls`, 20 for
  `socket.tls_server_config`/`socket.start_tls_server`" and a `*(1.20)*` paragraph like 1.19's;
  §4.4 the two rows and a prose paragraph after `start_tls`'s (TLS config ids in the socket table,
  the `tls_config` descriptions and their order, the server handshake, failure kinds); §7 a
  `**1.20**` bullet ("added `socket.tls_server_config` and `socket.start_tls_server` (§4.4, M42),
  TLS servers, behind std:socket's `tls_server_config`/`start_tls_server` and `std:http`'s
  `serve(tls:)`; and nothing else. The encoder writes 20 for a file that lists them, which every
  program importing std:socket does.").
- `docs/STDLIB.md` `### std:socket`: a **TLS servers (M42)** bullet (API, kinds, PEM rules,
  versions); "UDP and TLS servers come later" → "UDP comes later"; the 1.18/1.19 sentence gains
  "1.20 since M42".
- `docs/RUST_VM.md`: rustls is for `start_tls` and `start_tls_server`.
- Template `std:socket` paragraph: TLS servers in one sentence; "needs a 1.20 VM".
  `www/src/content/std/socket.md`: a "TLS servers" section; "need bytecode 1.20".

---

## 5. Shared files and conflict rules

| file | Part A | Part B | rule |
|---|---|---|---|
| `docs/STDLIB.md` | `### std:http` | `### std:socket` | disjoint sections |
| `docs/V2_DESIGN.md` | whole M42 entry + Status | — | A writes B's two bullets verbatim (§3.12) |
| `docs/MAHC_FORMAT.md` | — | all | |
| `runtime/tests/vm_diff.py` | case after `std_tls_url_http` | case after `std_socket` | disjoint hunks |
| `tests/test_bytecode.py` | adds `"http_server.mh": 20` | `sockets.mh`/`http_client.mh` → 20, unsupported → 21 | whoever lands second keeps all three at 20; A writes 20 even if B isn't in yet (that one assertion fails only until B lands — note it in A's report) |
| template `mah-language.md` | std:http paragraph, "Not available" | std:socket paragraph | disjoint paragraphs |
| `www/.../standard-library.md` | http paragraph | socket paragraph | disjoint |

Docs describe the finished milestone (1.20) whichever part lands first. Neither part edits a file
the other owns outright (§3.1, §4.1).

## 6. Integration and verification (after both land)

1. `make test` and `make test-rust` green; `python runtime/tests/vm_diff.py` (both new cases).
2. Remove the `skipUnless` from `HttpsServerTests` and run it on both VMs.
3. `check('import http from "std:http"')` and `check('import socket from "std:socket"')`: no
   diagnostics but `implicit`; test_http's `CheckerTests` unchanged.
4. `examples/http_server.mh` output identical on both VMs; `mah format --check` clean.
5. Thinker's extra probes (not in the coders' lists): a request split across many 1-byte sends;
   a reply streamed for longer than `idle_timeout`; shutdown during a TLS handshake; 200
   keep-alive requests on one connection.

## 7. Deferred, and what the framework repository gets

Deferred (recorded in NEXT_PHASES by Part A): HTTP/2, `Upgrade`/WebSockets/1xx replies, request
**body streaming** (bodies are read whole), response compression, Range requests, static files,
cookies helpers, routing/middleware (framework), per-connection request caps, write timeouts (a
`send` waits until the client reads; shutdown's force-close ends it), client certificates, SNI /
several certificates, ALPN, certificate reloading (load a new config and restart the server).

For the framework: handlers are plain `fn(Request) -> Reply` values (compose middleware as
functions); `http.Request { ... }` literals make handlers testable without sockets; `on_error`
is the error-handling hook; `Reply` helpers + `set_header`/`add_header`; `reason_phrase` and
`http_date` are exported; `Request.json/form/multipart/query_param` throw `HttpError` kind
`bad_request`, which the server maps to 400 — a framework's validation layer can throw the same.

## 8. Mah gotchas for both coders

- `&` and `|` **evaluate both sides**: never write `x != none & x.len() > 0`; nest `if`s.
- A top-level `fn` can only call top-level functions declared **above** it (impl methods may call
  anything): order helpers before their callers.
- No `+=`, no ternary, no string interpolation; `"" + n` turns a Number into text.
- A function stored in a field is called as `(state.handler)(req)`.
- `try { ... } catch { e: T => { ... } e => { ... } }`; `try EXPR else FALLBACK` catches all. An
  arm naming a type the body never throws is a checker warning (keep `check(HTTP)` clean).
- A detached task that fails and is never awaited stops the program at exit: every detached
  server task catches everything.
- Bytes slices (`b[a..b]`) are new Bytes; `b.index_of(needle)` searches from the start, so search
  a slice `b[pos..]` and add `pos`.
- Naming a type or function that a module doesn't export is a compile error (`socket.X`), but a
  method call (`conn.start_tls_server(...)`) is resolved at run time.
- Run `mah format` on every changed `.mh`; std modules must keep their `##` docs.

## 9. Judgment calls (for the user to revisit)

1. Handler is `fn(Request) -> Reply` (Fetch/Hono style) rather than Node/Go's `(req, res)`
   writer; streaming via a producer function.
2. The server's answer is a new `Reply` type instead of reusing the client's `Response`.
3. Request bodies are read whole (default cap 10 MiB) — no streaming uploads yet.
4. Server TLS takes a pre-loaded `socket.TlsServerConfig` (`tls: socket.tls_server_config(cert,
   key)`) rather than `tls: [cert, key]` paths, for fail-fast loading and so Part A has no
   compile-time dependency on Part B; costs one extra native and a config id kind.
5. One failure kind `tls_config` with seven fixed descriptions (instead of a kind per case).
6. Defaults: `max_head` 16 KiB, `max_body` 10 MiB, `read_timeout` 30 s (whole request),
   `idle_timeout` 5 s, `max_connections` 256, shutdown `grace` 10 s, 100 header fields.
7. The default host stays `127.0.0.1` (like `socket.listen`); containers pass `host: "0.0.0.0"`.
8. Uncaught `HttpError` kind `bad_request` → 400 with its description; everything else → a bare
   500 with no details; no default logging (use `on_error`).
9. Every response carries `Date`; no `Server` header.
10. Server and client stay in one module (`std:http`), at the cost of a larger file every client
    program compiles.
