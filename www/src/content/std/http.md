---
title: std:http
order: 15
section: Networking
summary: An HTTP/1.1 client (http and https, JSON and form bodies, redirects, proxies) and server (handler functions, keep-alive, limits, graceful shutdown, TLS).
---

# `std:http`

An HTTP/1.1 client for `http://` and `https://` URLs, and an HTTP/1.1
server (see [Serving HTTP](#serving-http)). Both are written in Mah over
[`std:socket`](/std/socket) (only TLS is native), so both runtimes run
exactly the same code.

```mah
import http from "std:http"

fn show() throws http.HttpError {
    let r = http.get("https://example.com/")
    print(r.status, r.header("content-type"))    # e.g. 200 text/html
    print(r.text().len() > 0)                    # true
}
```

## Making requests

`get`, `post`, `put`, `patch`, `delete` and `head` each send one request
and return a `http.Response`; `request(method, url, ...)` sends any method.
They all wait like ordinary calls; `detach` several to run them
concurrently.

Every function takes:

- `headers`: a Map (or a Vector of `[name, value]` pairs). Yours override
  the defaults (`Host`, `User-Agent: mah`, `Accept`, `Content-Length`,
  `Content-Type`).
- `timeout`: milliseconds for **each** wait (connecting, the TLS
  handshake, each read), 30000 by default; `none` for no limit.
- `max_redirects`: 10 by default; 0 returns the redirect response itself.

`post`, `put`, `patch` and `request` also take **one** body:

- `body`: a String or Bytes, sent as is.
- `json`: any value [`std:json`](/std/json) can write, sent with
  `Content-Type: application/json`.
- `form`: a Map or pairs (see [`std:url`](/std/url)'s `encode_query`),
  sent as `application/x-www-form-urlencoded`.

```mah
import http from "std:http"
import url from "std:url"

fn examples() throws http.HttpError {
    let q = url.encode_query(["q": "mah lang", "page": 2])
    let found = http.get("https://api.example.com/search?" + q, headers: ["Accept": "application/json"], timeout: 5000)

    let created = http.post("https://api.example.com/items", json: ["name": "mah", "tags": ["lang"]])
    let login = http.post("https://example.com/login", form: ["user": "ann", "pass": "s3cret"])
    let raw = http.put("https://example.com/upload", body: "plain text", headers: ["Content-Type": "text/plain"])
    let gone = http.delete("https://api.example.com/items/7")
    let info = http.head("https://example.com/big.iso")
    print(info.header("content-length"))
}
```

## Responses

A `Response` has `status` (a Number), `reason`, `headers` (the `[name,
value]` pairs as received), `body` (raw Bytes), and `url` (the address
that finally answered, after redirects).

- `header(name)` gives the first value of a header, any case, or `none`;
  `header_all(name)` every value (for `Set-Cookie`, say).
- `text()` decodes the body as UTF-8 (invalid sequences become U+FFFD).
- `json()` parses the body with `std:json`, throwing `json.JsonError`.
- `is_success()` is whether the status is 2xx.

Here's a complete, runnable round trip against a tiny local server (so it
works offline):

```mah
import http from "std:http"
import socket from "std:socket"

# A tiny one-request server, so this example runs without the internet.
fn serve_once(server: socket.Listener) throws socket.SocketError {
    let conn = server.accept()
    defer conn.close()
    let request_line = conn.read_line()          # "GET /hello?x=1 HTTP/1.1"
    let line = conn.read_line()
    while line != none & line != "" { line = conn.read_line() }   # skip the headers
    let body = "{\"you_asked\": \"" + request_line.split(" ")[1] + "\"}"
    conn.send_text("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: " + body.len().to_string() + "\r\nConnection: close\r\n\r\n" + body)
}

let server = socket.listen(0)
let done = detach serve_once(server)
try {
    let r = http.get("http://127.0.0.1:" + server.port.to_string() + "/hello?x=1")
    print(r.status, r.is_success(), r.header("content-type"))   # 200 true application/json
    print(r.json()["you_asked"])                                 # /hello?x=1
} catch {
    e: http.HttpError => { print(e.kind, e.message()) }
}
done.await
server.close()
```

## Status codes vs errors

A **404 or 500 is still a Response**, not an error: the request worked,
the server just said no. Check `r.status` or `r.is_success()`, or call
`r.check_status()`, which returns the Response for a 2xx/3xx status and
throws `HttpError` kind `"status"` for 4xx/5xx. That lets one `catch`
handle both network and HTTP failures:

```mah
import http from "std:http"
import json from "std:json"

fn load_user(id: Number) -> String throws http.HttpError | json.JsonError {
    let r = http.get("https://api.example.com/users/" + id.to_string()).check_status()
    r.json()["name"]
}
```

`HttpError` is for requests that **couldn't complete**. Its `kind` is a
[`SocketError`](/std/socket#errors) kind for network failures
(`"connection_refused"`, `"host_not_found"`, `"timed_out"`,
`"tls_certificate"`, ...), or `"invalid_url"`, `"unsupported_scheme"`
(not http/https), `"invalid_response"` (the server didn't speak HTTP/1.x),
`"too_many_redirects"`, `"proxy"`, or `"status"` (from `check_status`).
It also carries the `method` and `url`.

## Redirects, proxies and security

- Redirects (301, 302, 303, 307, 308) are followed up to `max_redirects`
  times. A 303, or a 301/302 after a POST, continues as a GET without a
  body, like browsers do.
- `Authorization` and `Cookie` headers are **dropped** when a redirect
  leaves the original origin, so credentials don't leak to another host.
- The `HTTP_PROXY`, `HTTPS_PROXY` and `NO_PROXY` environment variables are
  honored; HTTPS goes through the proxy with `CONNECT`.
- HTTPS verifies certificates against the system's roots (or
  `SSL_CERT_FILE`), as in `std:socket`.
- The client uses a new connection for each request (no keep-alive pooling yet).

## Serving HTTP

`http.serve(port, handler)` starts a server and returns an `http.Server`
**at once**; it serves in the background until you stop it. A handler is a
plain function from an `http.Request` to an `http.Reply`, called once per
request:

```mah
import http from "std:http"

fn handle(req: http.Request) -> http.Reply {
    if req.path == "/" { return http.Reply.text("hello") }
    if req.path == "/greet" {
        let name = req.query_param("name")
        return http.Reply.json(["greeting": "hi " + (if name == none { "you" } else { name })])
    }
    if req.path == "/items" & req.method == "POST" {
        let item = req.json()                 # bad JSON: answered with 400
        return http.Reply.json(["created": item], status: 201)
    }
    http.Reply.text("no route for " + req.path, status: 404)
}

let server = http.serve(0, handle)            # port 0: a free one (see server.port)
print(http.get(server.url("/greet?name=mah")).text())   # {"greeting":"hi mah"}
server.close()                                # or server.wait() to serve until shut down
```

To serve for real, listen on a fixed port and wait: `http.serve(8080,
handle, host: "0.0.0.0").wait()` (the default host, `127.0.0.1`, only
accepts local connections).

### Requests

An `http.Request` has `method`, `target` (as sent, `"/a/b?x=1"`), `path`
(`"/a/b"`, still percent-encoded), `query` (`"x=1"`, or `none`), `version`,
`headers` (`[name, value]` pairs as received), `body` (Bytes, read whole
before the handler runs), `peer_host`, `peer_port` and `tls`, and:

- `header(name)`, `header_all(name)`: header values, any case.
- `content_type()`: the media type, lowercased (`"application/json"`), `""`
  without one.
- `text()`, `json()`: the body decoded.
- `query_pairs()`, `query_param(name)`: the decoded query.
- `form()`: an urlencoded or multipart form's fields as `[name, value]`
  pairs; `multipart()`: every part (`http.Part { name, filename,
  content_type, headers, body }`), files included.

When the body or query can't be read (bad JSON, a malformed form), these
throw `http.HttpError` kind `"bad_request"`, and if the handler lets it
escape the client gets a **400** with the reason.

Because a handler is just a function, you can test it without a server:

```mah
import http from "std:http"

fn hello(req: http.Request) -> http.Reply { http.Reply.text("hi " + req.path) }

let req = http.Request { method: "GET", target: "/x", path: "/x", query: none, version: "HTTP/1.1", headers: [], body: "".to_bytes(), peer_host: "test", peer_port: 0, tls: false }
print(hello(req).body)                        # hi /x
```

### Replies

| | |
|---|---|
| `Reply.text(s)`, `Reply.html(s)` | text with `text/plain` / `text/html; charset=utf-8` |
| `Reply.json(value)` | `application/json` |
| `Reply.bytes(data, content_type = "application/octet-stream")` | binary |
| `Reply.redirect(location, status = 302)` | a redirect |
| `Reply.empty(status = 204)` | no body |
| `Reply.new(status = 200, body = none, headers = [:])` | anything |
| `Reply.stream(fn(w) { ... })` | a body written piece by piece |

Each takes `status:` and `headers:` (a Map or pairs; a `Content-Type` you
give wins). `reply.set_header(name, value)` replaces a header and
`reply.add_header(name, value)` adds one (for `Set-Cookie`); both return
the Reply. The server adds `Content-Length` (or chunked coding for a
stream), `Date`, and `Connection` when needed. A streamed body's producer
gets an `http.BodyWriter` whose `write(text_or_bytes)` sends the next piece:

```mah
import http from "std:http"

fn countdown(req: http.Request) -> http.Reply {
    http.Reply.stream(fn(w: http.BodyWriter) {
        for let i in 0..3 {
            w.write("" + (3 - i) + "\n")
            sleep_async(100)
        }
    })
}
```

### Errors

Anything a handler throws, or a result that isn't a Reply, becomes a bare
**500** (an uncaught `"bad_request"` HttpError is a **400**). Pass
`on_error: fn(e, req) { ... }` to see every such error (to log it, say) and
optionally return the Reply to send instead.

### Limits, keep-alive and concurrency

The server speaks HTTP/1.1 and 1.0, keeps connections alive, answers
pipelined requests in order, reads chunked request bodies and answers
`Expect: 100-continue`. Malformed requests get a 400 (or 501/505) and are
closed. Options of `serve`:

| option | default | |
|---|---|---|
| `host` | `"127.0.0.1"` | `"0.0.0.0"` for every interface |
| `max_head` | 16384 | bytes of request line and headers (414, 431) |
| `max_body` | 10485760 | bytes of body (413) |
| `read_timeout` | 30000 | ms to receive a whole request (408) |
| `idle_timeout` | 5000 | ms a kept-alive connection may wait for its next request |
| `max_connections` | 256 | open connections (503 beyond) |
| `tls` | `none` | a `socket.tls_server_config(cert, key)` to serve HTTPS |
| `on_error` | `none` | see Errors |

Each connection is its own task, so requests are handled concurrently
whenever a handler waits (for I/O, `sleep_async` or `.await`).

### Shutting down

`server.shutdown(grace = 10000)` stops accepting, closes idle connections,
lets requests in flight finish and closes whatever is left after `grace`
ms; it returns at once. `server.wait()` waits until everything has
stopped, and `server.close()` is both. `server.connections()` counts open
connections, and `server.url(path)` gives a URL on the server.

### HTTPS

```mah
import http from "std:http"
import socket from "std:socket"

fn serve_https(handle: fn(http.Request) -> http.Reply) throws socket.SocketError {
    let identity = socket.tls_server_config("cert.pem", "key.pem")   # checked once, here
    http.serve(8443, handle, host: "0.0.0.0", tls: identity).wait()
}
```

## Reference

| Function | |
|---|---|
| `get(url, headers = [:], timeout = 30000, max_redirects = 10)` | a GET |
| `head(url, ...)` | status and headers, no body |
| `delete(url, ...)` | a DELETE |
| `post(url, body = none, json = none, form = none, headers = [:], timeout = 30000, max_redirects = 10)` | a POST |
| `put(url, ...)`, `patch(url, ...)` | like `post` |
| `request(method, url, body = none, json = none, form = none, headers = [:], timeout = 30000, max_redirects = 10)` | any method |

| `Response` | |
|---|---|
| `status`, `reason` | e.g. `404`, `"Not Found"` |
| `headers` | `[name, value]` pairs, as received |
| `body` | the raw Bytes |
| `url`, `method` | of the request that answered (after redirects) |
| `header(name)`, `header_all(name)` | header values, any case |
| `text()`, `json()` | the body decoded |
| `is_success()` | 2xx |
| `check_status()` | the Response, or throws for 4xx/5xx |

| Server side | |
|---|---|
| `serve(port, handler, host, tls, max_head, max_body, read_timeout, idle_timeout, max_connections, backlog, on_error)` | starts a `Server` |
| `Server` | `host`, `port`, `tls`; `url(path)`, `shutdown(grace)`, `wait()`, `close(grace)`, `connections()` |
| `Request` | see [Requests](#requests) |
| `Reply` | `status`, `headers`, `body`; see [Replies](#replies) |
| `reason_phrase(status)` | `"Not Found"` for 404 |
| `http_date(timestamp)` | `"Wed, 07 Oct 2026 12:00:00 GMT"` |

`http.HttpError { kind, method, url, description }` is the error type
(`kind` `"bad_request"` for a request a server handler couldn't read).
Programs that import `std:http` need bytecode 1.20.
