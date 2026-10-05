# M39 contract: TLS, `std:url` and the `std:http` client, bytecode 1.19

`docs/STDLIB.md` Phase 4's second step. Unlike M38 this milestone wasn't split across
agents; this file records the design it was built against, so the HTTP server (next) can
reuse it. The normative byte-level rules are in `docs/MAHC_FORMAT.md` §4.4 (`socket.start_tls`)
and the module APIs in `docs/STDLIB.md`.

## 1. Decisions

- **HTTP in Mah, TLS native.** `mah/std/http.mh` speaks HTTP/1.1 over `std:socket`; the only
  new native is `socket.start_tls`. Both VMs therefore run identical request code, and the
  server milestone can share the parsing helpers.
- **One native, one minor**: `socket.start_tls(id, server_name, timeout)`, arity 3, minor 19.
  std:socket declares it, so every program importing std:socket or std:http is 1.19;
  std:url alone is not.
- **Trust**: `SSL_CERT_FILE` (a PEM bundle) when set, read at each call; else the platform's
  roots (Python `ssl.create_default_context()`) or Mozilla's (`webpki-roots`, Rust).
  Never an option to skip verification.
- **TLS kinds**: `tls_certificate` ("the server's certificate isn't trusted") and `tls` ("the
  TLS handshake or connection failed"). A ragged EOF is a normal end. `shutdown` on a TLS
  socket shuts the TCP write side without a close notice (both VMs).

## 2. Surface

```mah
import socket from "std:socket"
import url from "std:url"
import http from "std:http"

let s = socket.connect_tls("example.com", 443)       # or connect + s.start_tls("example.com")
let u = url.parse("https://example.com/a?b=1")       # Url { scheme, username, password, host, port, path, query, fragment }
let r = http.get("https://example.com/", headers: ["Accept": "text/html"], timeout: 10000)
print(r.status, r.header("content-type"), r.text())
http.post("https://api.example.com/x", json: ["a": 1]).check_status()
```

- `Socket.start_tls(server_name: String, timeout: Unknown = none) throws SocketError`
  (`RuntimeError.ArgumentError` "start_tls: the socket has unread bytes in its buffer" when
  `buffer` isn't empty); `connect_tls(host, port, timeout = none) -> Socket`.
- `std:url`: `parse`, `is_valid`, `default_port`, `encode(text, safe = "")`, `decode`,
  `encode_query(params)`, `parse_query(query)`; `Url` methods `effective_port`, `authority`,
  `origin`, `request_target`, `query_pairs`, `resolve`, `with_query`; `UrlError { kind, text,
  description }` with kinds `invalid_url`, `invalid_encoding`.
- `std:http`: `request(method, url, body, json, form, headers, timeout = 30000,
  max_redirects = 10)`, `get`/`head`/`delete(url, headers, timeout, max_redirects)`,
  `post`/`put`/`patch(url, body, json, form, headers, timeout, max_redirects)`;
  `Response { status, reason, headers, body, url, method }`; `HttpError { kind, method, url,
  description }`.

## 3. Behavior the server milestone should keep

- Requests: `METHOD target HTTP/1.1`, headers in order (defaults Host, User-Agent `mah`,
  Accept `*/*`, Connection `close`, then Content-Length/Content-Type; a user header replaces
  a default of the same name, case-insensitively), CRLF line ends.
- Responses: status line `HTTP/1.x CODE [REASON]`; headers `name: value`, trimmed, kept as
  pairs; body by `Transfer-Encoding: chunked`, else `Content-Length`, else until close; none
  for HEAD/204/304; 1xx skipped.
- Redirects, proxies, timeouts: as `docs/STDLIB.md`'s `std:http` section.

## 4. Tests

`mah/std/url.test.mh`, `mah/std/http.test.mh` (both VMs via `tests/test_stdlib.py`),
`tests/test_http.py` (HTTPS with a throwaway CA, needs `openssl`; both VMs with
`MAH_TEST_VM=rust`), `runtime/tests/vm_diff.py`'s `std_tls_url_http`, and
`examples/http_client.mh` (golden test in `tests/test_examples.py`).
