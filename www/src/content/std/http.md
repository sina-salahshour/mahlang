---
title: std:http
order: 15
section: Networking
summary: An HTTP/1.1 client for http and https with JSON and form bodies, redirects, proxies and timeouts.
---

# `std:http`

An HTTP/1.1 client for `http://` and `https://` URLs. It's written in Mah
over [`std:socket`](/std/socket) (only TLS is native), so both runtimes
run exactly the same code.

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
- Each request uses a new connection (no keep-alive pooling yet).

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

`http.HttpError { kind, method, url, description }` is the error type.
Programs that import `std:http` need bytecode 1.19.
