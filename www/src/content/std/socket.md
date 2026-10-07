---
title: std:socket
order: 14
section: Networking
summary: TCP clients and servers, line and byte reads with timeouts, and TLS for clients (with certificate verification) and servers.
---

# `std:socket`

TCP networking: connect to a server, or listen and accept connections,
then send and receive Bytes or lines of text. Any connection can be
upgraded to **TLS**. Every call waits like an ordinary call while the I/O
happens in the background, so `detach` lets other tasks run alongside
(a server handling several clients, say). Timeouts are in milliseconds.

```mah
import socket from "std:socket"

let server = socket.listen(0)                  # a free port on 127.0.0.1
let incoming = detach server.accept()
let client = socket.connect("127.0.0.1", server.port)
let conn = incoming.await

client.send_text("hello\n")
print(conn.read_line())                        # hello
conn.send_text("hi yourself\n")
print(client.read_line())                      # hi yourself

client.close()
conn.close()
server.close()
```

For HTTP, use [`std:http`](/std/http), which is built on this module.

## Connecting

`connect(host, port, timeout = none)` opens a connection, trying each
address a name resolves to. `connect_tls(host, port, timeout = none)` does
the same and then starts TLS (below). Both give a `socket.Socket` with
`peer_host`, `peer_port` and `local_port`.

## Sending and receiving

- `send(data)` sends all of a Bytes; `send_text(text)` sends a String's
  UTF-8 bytes.
- `recv(max = 65536, timeout = none)` gives **up to** `max` bytes, whatever
  arrives next. TCP is a stream, so one `send` on the other side may arrive
  as several `recv`s, or several as one. **Empty Bytes means the peer has
  closed** its side.
- `recv_exactly(n, timeout = none)` waits for exactly `n` bytes, ideal for
  length-prefixed binary protocols. It throws kind `"closed_early"` if the
  peer closes first.
- `read_line(timeout = none)` gives the next line as text, without its
  `\n` or `\r\n`, or `none` once the peer has closed and nothing is left.
  Bytes after the line stay buffered for the next read.
- `shutdown()` stops sending (the peer's reads then see the end) while you
  can still receive. `close()` closes both ways.

A **length-prefixed** message exchange, using `recv_exactly`:

```mah
import bytes from "std:bytes"
import socket from "std:socket"

fn send_message(s: socket.Socket, text: String) throws socket.SocketError {
    let body = text.to_bytes()
    s.send(bytes.from_vector([body.len()]))    # 1-byte length (messages under 256 bytes)
    s.send(body)
}

fn read_message(s: socket.Socket) -> String throws socket.SocketError {
    let n = s.recv_exactly(1)[0]
    s.recv_exactly(n).to_text_lossy()
}

let server = socket.listen(0)
let accepted = detach server.accept()
let a = socket.connect("127.0.0.1", server.port)
let b = accepted.await
send_message(a, "first")
send_message(a, "second")
print(read_message(b), read_message(b))        # first second
a.close()
print(b.recv().len())                          # 0 (the peer closed)
b.close()
server.close()
```

## Servers

`listen(port, host = "127.0.0.1", backlog = 128)` starts listening; port 0
picks a free port (read it from the Listener's `port`). Listen on
`"0.0.0.0"` to accept connections from other machines. `accept(timeout =
none)` waits for the next connection.

To serve several clients at once, `detach` a handler per connection:

```mah
import socket from "std:socket"

fn handle(conn: socket.Socket) throws socket.SocketError {
    defer conn.close()
    let line = conn.read_line()
    if line != none { conn.send_text(line.to_upper() + "\n") }
}

fn serve(server: socket.Listener, count: Number) throws socket.SocketError {
    for let i in 0..count {
        let conn = server.accept()
        detach handle(conn)                    # don't wait: accept the next one
    }
}

let server = socket.listen(0)
let serving = detach serve(server, 2)
let c1 = socket.connect("127.0.0.1", server.port)
let c2 = socket.connect("127.0.0.1", server.port)
c1.send_text("one\n")
c2.send_text("two\n")
print(c2.read_line(), c1.read_line())          # TWO ONE
serving.await
c1.close()
c2.close()
server.close()
```

## TLS

`connect_tls(host, port)`, or `sock.start_tls(server_name)` on a connected
socket, makes the connection encrypted. The server's certificate must be
valid, name the server, and chain to a trusted root: the system's, or
those in the PEM file the `SSL_CERT_FILE` environment variable names.
Everything sent and received afterwards is encrypted, with the same
methods as before.

```mah
import socket from "std:socket"

fn fetch_head(host: String) -> String throws socket.SocketError {
    let s = socket.connect_tls(host, 443, timeout: 5000)
    defer s.close()
    s.send_text("HEAD / HTTP/1.1\r\nHost: " + host + "\r\nConnection: close\r\n\r\n")
    s.read_line(timeout: 5000)                 # e.g. "HTTP/1.1 200 OK"
}
```

## TLS servers

A TLS server loads its certificate chain and private key (PEM files, the
key unencrypted) **once** with `tls_server_config`, which checks that they
match, so a wrong path fails at startup rather than at the first
connection. Each accepted socket then runs `start_tls_server(config)`;
afterwards it sends and receives like any other socket. One config serves
any number of connections, and `std:http`'s `serve(..., tls: config)`
takes the same value.

```mah
import socket from "std:socket"

let identity = socket.tls_server_config("cert.pem", "key.pem")
let server = socket.listen(8443, host: "0.0.0.0")
while true {
    let conn = server.accept()
    try {
        conn.start_tls_server(identity, timeout: 5000)
        conn.send_text("hello over TLS\n")
    } catch {
        e: socket.SocketError => { print("handshake failed: " + e.kind) }
    }
    conn.close()
}
```

Loading failures are kind `"tls_config"`, and the description says what
is wrong: `can't read the certificate file`, `can't read the private key
file`, `no certificate in the certificate file`, `the private key is
encrypted`, `no private key in the key file`, `the private key doesn't
match the certificate` or `the certificate or private key isn't usable`.
A failed handshake is kind `"tls"` (a client that doesn't speak TLS or
doesn't trust the certificate), `"connection_reset"` or `"timed_out"`.
TLS 1.2 and 1.3 with one certificate; no client certificates.

## Errors

Failures throw `socket.SocketError { kind, op, address, description }`.
`kind` is one of `"connection_refused"`, `"connection_reset"`,
`"timed_out"`, `"address_in_use"`, `"address_not_available"`,
`"host_not_found"`, `"permission_denied"`, `"closed"` (a closed socket or
listener), `"tls_certificate"` (an untrusted or mismatched certificate),
`"tls"`, `"tls_config"` (`tls_server_config` couldn't use its files),
`"closed_early"` (`recv_exactly`), `"invalid_utf8"`
(`read_line`) or `"other"`.

```mah
import socket from "std:socket"

let server = socket.listen(0)
let port = server.port
server.close()                                  # nothing listens there now
try {
    socket.connect("127.0.0.1", port, timeout: 2000)
} catch {
    e: socket.SocketError => { print(e.kind) }  # connection_refused
}
```

## Reference

| Function | |
|---|---|
| `connect(host, port, timeout = none)` | a `Socket` |
| `connect_tls(host, port, timeout = none)` | `connect`, then `start_tls(host)` |
| `listen(port, host = "127.0.0.1", backlog = 128)` | a `Listener` (port 0: any free port) |
| `tls_server_config(cert_path, key_path)` | a `TlsServerConfig` for `start_tls_server` (`close()` frees it) |

| `Listener` | |
|---|---|
| `host`, `port` | where it listens |
| `accept(timeout = none)` | the next connection, as a `Socket` |
| `close()` | stop listening; a waiting `accept` fails with kind `"closed"` |

| `Socket` | |
|---|---|
| `peer_host`, `peer_port`, `local_port` | the two ends |
| `address()` | `"host:port"` of the peer |
| `send(data)`, `send_text(text)` | send all of it |
| `recv(max = 65536, timeout = none)` | up to `max` bytes; empty at the end |
| `recv_exactly(n, timeout = none)` | exactly `n` bytes |
| `read_line(timeout = none)` | the next line, or `none` at the end |
| `shutdown()` | stop sending |
| `start_tls(server_name, timeout = none)` | upgrade to TLS as a client |
| `start_tls_server(config, timeout = none)` | upgrade to TLS as the server |
| `close()` | close (twice does nothing) |

Programs that import `std:socket` need bytecode 1.20.
