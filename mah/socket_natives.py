"""M38 (1.18, docs/MAHC_FORMAT.md #4.4): std:socket's natives (TCP), as the
Python VM implements them. `runtime/src/vm/socket.rs` mirrors every rule here.

Like `fs_natives`, every native returns a Promise at once and does its
blocking work on a worker thread (`_IoHub.submit`), which settles it with a
**result** Vector: `[true, value]`, or `[false, kind, description]`.
std:socket turns the failure into a `SocketError`. `kind` is one of `KINDS`;
`description` is fixed per kind, except for "other" (the OS's own text).

Sockets and listeners are ids (positive whole Numbers, from 1) in the hub's
socket table (`_IoHub.sockets`), separate from the file table. Argument
types are checked first, on the VM's thread, and raised as RuntimeErrors. An
id that isn't open (closed, never opened, or the wrong kind) settles at once
with `closed`. A worker waiting in `connect`/`accept`/`recv` polls in slices
of at most 50 ms, checking its deadline and whether its id is still in the
table, so `close` (which removes the id at once) ends it with `closed`.

M42 (1.20): TLS servers. `tls_server_config` loads a PEM certificate chain
and private key once into the socket table as an entry of kind "tls_config"
(an id no other native accepts: it is "not open" for them), and
`start_tls_server` runs a server handshake on an accepted socket with it.
Failures to load are kind "tls_config", whose description -- unlike every
other kind's but "other" -- is one of a fixed set (`_pem_problem` and
`_TLS_CONFIG_PROBLEMS`), the same text on both VMs.

Like the VM, this module never imports the compiler.
"""

from __future__ import annotations

import errno
import select
import socket
import ssl
import time
from decimal import Decimal

from .runtime_values import NONE_VALUE, BytesValue, MahRuntimeError, PromiseInstance, VectorValue, type_name_of

DESCRIPTIONS = {
    "connection_refused": "connection refused",
    "connection_reset": "connection reset by peer",
    "timed_out": "timed out",
    "address_in_use": "address already in use",
    "address_not_available": "address not available",
    "host_not_found": "host not found",
    "permission_denied": "permission denied",
    "closed": "the socket is closed",
    # M39: start_tls
    "tls_certificate": "the server's certificate isn't trusted",
    "tls": "the TLS handshake or connection failed",
    # M42: tls_server_config; this generic text is the last of its fixed
    # descriptions (see _TLS_CONFIG_PROBLEMS)
    "tls_config": "the certificate or private key isn't usable",
}

# M42: tls_server_config's descriptions, in the order they are checked.
CANT_READ_CERT = "can't read the certificate file"
CANT_READ_KEY = "can't read the private key file"
NO_CERT = "no certificate in the certificate file"
KEY_ENCRYPTED = "the private key is encrypted"
NO_KEY = "no private key in the key file"
KEY_MISMATCH = "the private key doesn't match the certificate"
UNUSABLE = DESCRIPTIONS["tls_config"]

# M39: the TLS layer asks to wait for the socket before retrying.
_WANT = (BlockingIOError, InterruptedError, ssl.SSLWantReadError, ssl.SSLWantWriteError)
KINDS = frozenset(DESCRIPTIONS) | {"other"}

SLICE = 0.05  # seconds: the longest a waiting worker sleeps between checks


def _ok(value) -> VectorValue:
    return VectorValue([True, value])


def _failure(kind: str, description: str | None = None) -> VectorValue:
    return VectorValue([False, kind, description if description is not None else DESCRIPTIONS[kind]])


class _Closed(Exception):
    """The id was closed while a worker waited on it."""


class _TimedOut(Exception):
    pass


class _TlsConfigProblem(Exception):
    """M42: tls_server_config can't use its files; args[0] is the description."""


class _TlsServerConfig:
    """M42: a socket-table entry of kind "tls_config". `close` does nothing
    (the table's `close` paths call `entry.sock.close()`)."""

    __slots__ = ("context",)

    def __init__(self, context):
        self.context = context

    def close(self):
        pass


class _Entry:
    """A table slot: a connected socket (`kind` "socket") or a listener."""

    __slots__ = ("kind", "sock")

    def __init__(self, kind: str, sock):
        self.kind = kind
        self.sock = sock


def _classify(exc: BaseException) -> VectorValue:
    if isinstance(exc, _TlsConfigProblem):
        return _failure("tls_config", exc.args[0])
    if isinstance(exc, _Closed):
        return _failure("closed")
    if isinstance(exc, (_TimedOut, TimeoutError, socket.timeout)):
        return _failure("timed_out")
    if isinstance(exc, ssl.SSLCertVerificationError):
        return _failure("tls_certificate")
    if isinstance(exc, ssl.SSLEOFError):
        return _failure("connection_reset")
    if isinstance(exc, ssl.SSLError):
        return _failure("tls")
    if isinstance(exc, socket.gaierror):
        return _failure("host_not_found")
    code = getattr(exc, "errno", None)
    if code == errno.ECONNREFUSED:
        return _failure("connection_refused")
    if code in (errno.ECONNRESET, errno.EPIPE, errno.ECONNABORTED):
        return _failure("connection_reset")
    if code == errno.ETIMEDOUT:
        return _failure("timed_out")
    if code == errno.EADDRINUSE:
        return _failure("address_in_use")
    if code == errno.EADDRNOTAVAIL:
        return _failure("address_not_available")
    if code in (errno.EACCES, errno.EPERM):
        return _failure("permission_denied")
    if isinstance(exc, OSError) and exc.strerror:
        return _failure("other", exc.strerror)
    return _failure("other", str(exc))


def _guarded(job):
    def run():
        try:
            return _ok(job())
        except (OSError, _Closed, _TimedOut, _TlsConfigProblem) as exc:
            return _classify(exc)

    return run


def _async(ctx, job):
    promise = PromiseInstance()
    ctx.io.submit(promise, _guarded(job))
    return promise


def _closed_now():
    promise = PromiseInstance()
    promise.resolve(_failure("closed"))
    return promise


# -- argument checks (on the VM's thread, before anything else) -----------------


def _number(value) -> bool:
    return isinstance(value, Decimal) and not isinstance(value, bool)


def _fmt(value) -> str:
    from .bytes_methods import _format_decimal

    return _format_decimal(value)


def _host(name: str, value) -> str:
    if not isinstance(value, str):
        raise MahRuntimeError(f"{name}: host must be a String, got {type_name_of(value)}", kind="TypeMismatch")
    return value


def _port(name: str, value, low: int = 0) -> int:
    if not _number(value):
        raise MahRuntimeError(f"{name}: port must be a Number, got {type_name_of(value)}", kind="TypeMismatch")
    if value != value.to_integral_value() or value < low or value > 65535:
        raise MahRuntimeError(
            f"{name}: port must be a whole number from {low} to 65535, got {_fmt(value)}", kind="ArgumentError"
        )
    return int(value)


def _timeout(name: str, value):
    """Seconds as a float, or None to wait forever."""
    if value is NONE_VALUE:
        return None
    if not _number(value):
        raise MahRuntimeError(
            f"{name}: timeout must be a Number or none, got {type_name_of(value)}", kind="TypeMismatch"
        )
    if value != value.to_integral_value() or value < 0:
        raise MahRuntimeError(
            f"{name}: timeout must be a whole number of at least 0, got {_fmt(value)}", kind="ArgumentError"
        )
    return float(value) / 1000.0


def _id(name: str, value) -> int | None:
    """The id as an int, or None when it can't name an open socket."""
    if not _number(value):
        raise MahRuntimeError(f"{name}: expected a socket id, got {type_name_of(value)}", kind="TypeMismatch")
    return int(value) if value == value.to_integral_value() else None


def _lookup(ctx, sock_id, kind: str):
    if sock_id is None:
        return None
    entry = ctx.io.sockets.get(sock_id)
    if entry is None or entry.kind != kind:
        return None
    return entry


# -- waiting --------------------------------------------------------------------


def _wait(ctx, sock_id, entry, deadline, want_write=False) -> None:
    """Block until `entry.sock` is readable (or writable), in slices: raises
    _Closed once the id is no longer this entry, _TimedOut at the deadline."""
    while True:
        if ctx.io.sockets.get(sock_id) is not entry:
            raise _Closed()
        # M39: bytes TLS already decrypted are ready, though select can't see them
        if not want_write and isinstance(entry.sock, ssl.SSLSocket) and entry.sock.pending():
            return
        wait = SLICE
        if deadline is not None:
            wait = min(wait, max(0.0, deadline - time.monotonic()))
        try:
            r, w, _ = select.select([] if want_write else [entry.sock], [entry.sock] if want_write else [], [], wait)
        except ValueError:
            # select() on a socket closed under it (fileno -1): only `close`
            # does that, and it removes the id first
            raise _Closed() from None
        except OSError:
            if ctx.io.sockets.get(sock_id) is not entry:
                raise _Closed()
            raise
        if r or w:
            return
        if deadline is not None and time.monotonic() >= deadline:
            raise _TimedOut()


def _deadline(timeout):
    return None if timeout is None else time.monotonic() + timeout


def _register(ctx, kind: str, sock) -> int:
    return ctx.io.add_socket(_Entry(kind, sock))


def _connected(ctx, sock) -> VectorValue:
    peer = sock.getpeername()
    local = sock.getsockname()
    return VectorValue([Decimal(_register(ctx, "socket", sock)), peer[0], Decimal(peer[1]), Decimal(local[1])])


# -- natives --------------------------------------------------------------------


def _connect(ctx, args):
    host = _host("connect", args[0])
    port = _port("connect", args[1], 1)
    timeout = _timeout("connect", args[2])

    def job():
        deadline = _deadline(timeout)
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        last: BaseException | None = None
        for family, stype, proto, _, addr in infos:
            sock = socket.socket(family, stype, proto)
            try:
                sock.setblocking(False)
                code = sock.connect_ex(addr)
                if code not in (0, errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EINTR):
                    raise OSError(code, "")
                while code != 0:
                    wait = SLICE
                    if deadline is not None:
                        if time.monotonic() >= deadline:
                            raise _TimedOut()
                        wait = min(wait, deadline - time.monotonic())
                    _, w, _ = select.select([], [sock], [], wait)
                    if w:
                        code = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                        if code != 0:
                            raise OSError(code, "")
                        break
                return _connected(ctx, sock)
            except _TimedOut:
                sock.close()
                raise
            except OSError as exc:
                sock.close()
                if exc.strerror == "" or exc.strerror is None:
                    import os

                    exc = OSError(exc.errno, os.strerror(exc.errno))
                last = exc
        if last is None:
            raise OSError(errno.EADDRNOTAVAIL, "address not available")
        raise last

    return _async(ctx, job)


def _listen(ctx, args):
    host = _host("listen", args[0])
    port = _port("listen", args[1])
    backlog = args[2]
    if not _number(backlog):
        raise MahRuntimeError(f"listen: backlog must be a Number, got {type_name_of(backlog)}", kind="TypeMismatch")
    if backlog != backlog.to_integral_value() or backlog < 1:
        raise MahRuntimeError(
            f"listen: backlog must be a whole number of at least 1, got {_fmt(backlog)}", kind="ArgumentError"
        )
    backlog = int(backlog)

    def job():
        family, stype, proto, _, addr = socket.getaddrinfo(
            host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE
        )[0]
        sock = socket.socket(family, stype, proto)
        try:
            if hasattr(socket, "SO_REUSEADDR") and not _is_windows():
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(addr)
            sock.listen(backlog)
            sock.setblocking(False)
        except BaseException:
            sock.close()
            raise
        return VectorValue([Decimal(_register(ctx, "listener", sock)), Decimal(sock.getsockname()[1])])

    return _async(ctx, job)


def _is_windows() -> bool:
    import os

    return os.name == "nt"


def _accept(ctx, args):
    sock_id = _id("accept", args[0])
    timeout = _timeout("accept", args[1])
    entry = _lookup(ctx, sock_id, "listener")
    if entry is None:
        return _closed_now()

    def job():
        deadline = _deadline(timeout)
        while True:
            _wait(ctx, sock_id, entry, deadline)
            try:
                conn, _ = entry.sock.accept()
            except _WANT:
                continue
            except OSError:
                if ctx.io.sockets.get(sock_id) is not entry:
                    raise _Closed()
                raise
            if ctx.io.sockets.get(sock_id) is not entry:
                conn.close()
                raise _Closed()
            conn.setblocking(False)
            return _connected(ctx, conn)

    return _async(ctx, job)


def _send(ctx, args):
    sock_id = _id("send", args[0])
    if not isinstance(args[1], BytesValue):
        raise MahRuntimeError(f"send: data must be Bytes, got {type_name_of(args[1])}", kind="TypeMismatch")
    data = bytes(args[1].data)  # copied now, on the VM's thread
    entry = _lookup(ctx, sock_id, "socket")
    if entry is None:
        return _closed_now()

    def job():
        view = memoryview(data)
        while len(view):
            _wait(ctx, sock_id, entry, None, want_write=True)
            try:
                sent = entry.sock.send(view)
            except _WANT:
                continue
            except OSError:
                if ctx.io.sockets.get(sock_id) is not entry:
                    raise _Closed()
                raise
            view = view[sent:]
        return NONE_VALUE

    return _async(ctx, job)


def _recv(ctx, args):
    sock_id = _id("recv", args[0])
    limit = args[1]
    if not _number(limit):
        raise MahRuntimeError(f"recv: max must be a Number, got {type_name_of(limit)}", kind="TypeMismatch")
    if limit != limit.to_integral_value() or limit < 1:
        raise MahRuntimeError(
            f"recv: max must be a whole number of at least 1, got {_fmt(limit)}", kind="ArgumentError"
        )
    limit = int(limit)
    timeout = _timeout("recv", args[2])
    entry = _lookup(ctx, sock_id, "socket")
    if entry is None:
        return _closed_now()

    def job():
        deadline = _deadline(timeout)
        while True:
            _wait(ctx, sock_id, entry, deadline)
            try:
                chunk = entry.sock.recv(min(limit, 1 << 20))
            except _WANT:
                continue
            except OSError:
                if ctx.io.sockets.get(sock_id) is not entry:
                    raise _Closed()
                raise
            return BytesValue(chunk)

    return _async(ctx, job)


def _start_tls(ctx, args):
    """M39: TLS on an open socket, as a client of `server_name` (which its
    certificate must name), verified against the system's trusted roots --
    or the PEM file in the SSL_CERT_FILE environment variable. The socket
    keeps its id; everything sent and received from then on is encrypted.
    A peer closing without TLS's close notice is a normal end, like a
    plain close."""
    sock_id = _id("start_tls", args[0])
    if not isinstance(args[1], str):
        raise MahRuntimeError(
            f"start_tls: server name must be a String, got {type_name_of(args[1])}", kind="TypeMismatch"
        )
    server_name = args[1]
    timeout = _timeout("start_tls", args[2])
    entry = _lookup(ctx, sock_id, "socket")
    if entry is None:
        return _closed_now()

    def job():
        if isinstance(entry.sock, ssl.SSLSocket):
            raise ssl.SSLError("the socket already uses TLS")
        deadline = _deadline(timeout)
        context = ssl.create_default_context()
        tls = context.wrap_socket(entry.sock, server_hostname=server_name, do_handshake_on_connect=False)
        entry.sock = tls
        return _handshake(ctx, sock_id, entry, tls, deadline)

    return _async(ctx, job)


def _handshake(ctx, sock_id, entry, tls, deadline):
    """Drives `tls`'s handshake (client or server) to the end, waiting for the
    socket in slices like every other worker."""
    while True:
        try:
            tls.do_handshake()
            return NONE_VALUE
        except ssl.SSLWantReadError:
            _wait(ctx, sock_id, entry, deadline)
        except ssl.SSLWantWriteError:
            _wait(ctx, sock_id, entry, deadline, want_write=True)


# -- M42: TLS servers -------------------------------------------------------------

_CERT_BEGIN = b"-----BEGIN CERTIFICATE-----"
_ENCRYPTED_MARKS = (b"-----BEGIN ENCRYPTED PRIVATE KEY-----", b"Proc-Type: 4,ENCRYPTED")
_KEY_BEGINS = (b"-----BEGIN PRIVATE KEY-----", b"-----BEGIN RSA PRIVATE KEY-----", b"-----BEGIN EC PRIVATE KEY-----")


def _pem_problem(cert: bytes, key: bytes) -> str | None:
    """The common mistakes, found by a plain byte search before any TLS
    library sees the data (`pem_problem` in socket.rs does the same)."""
    if _CERT_BEGIN not in cert:
        return NO_CERT
    if any(mark in key for mark in _ENCRYPTED_MARKS):
        return KEY_ENCRYPTED
    if not any(begin in key for begin in _KEY_BEGINS):
        return NO_KEY
    return None


def _tls_server_config(ctx, args):
    if not isinstance(args[0], str):
        raise MahRuntimeError(
            f"tls_server_config: certificate path must be a String, got {type_name_of(args[0])}",
            kind="TypeMismatch",
        )
    if not isinstance(args[1], str):
        raise MahRuntimeError(
            f"tls_server_config: key path must be a String, got {type_name_of(args[1])}", kind="TypeMismatch"
        )
    cert_path, key_path = args[0], args[1]

    def job():
        try:
            with open(cert_path, "rb") as f:
                cert = f.read()
        except OSError:
            raise _TlsConfigProblem(CANT_READ_CERT) from None
        try:
            with open(key_path, "rb") as f:
                key = f.read()
        except OSError:
            raise _TlsConfigProblem(CANT_READ_KEY) from None
        problem = _pem_problem(cert, key)
        if problem is not None:
            raise _TlsConfigProblem(problem)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        try:
            # the password callback keeps OpenSSL from ever prompting
            context.load_cert_chain(cert_path, key_path, password=lambda: b"")
        except ssl.SSLError as exc:
            if getattr(exc, "reason", None) == "KEY_VALUES_MISMATCH":
                raise _TlsConfigProblem(KEY_MISMATCH) from None
            raise _TlsConfigProblem(UNUSABLE) from None
        except (OSError, ValueError):
            raise _TlsConfigProblem(UNUSABLE) from None
        return Decimal(_register(ctx, "tls_config", _TlsServerConfig(context)))

    return _async(ctx, job)


def _start_tls_server(ctx, args):
    """M42: a TLS server handshake on the open socket `id`, with the loaded
    config `config`; afterwards send/recv/shutdown/close work as after
    `start_tls`."""
    sock_id = _id("start_tls_server", args[0])
    if not _number(args[1]):
        raise MahRuntimeError(
            f"start_tls_server: expected a TLS config id, got {type_name_of(args[1])}", kind="TypeMismatch"
        )
    config_id = int(args[1]) if args[1] == args[1].to_integral_value() else None
    timeout = _timeout("start_tls_server", args[2])
    entry = _lookup(ctx, sock_id, "socket")
    config = _lookup(ctx, config_id, "tls_config")
    if entry is None or config is None:
        return _closed_now()

    def job():
        if isinstance(entry.sock, ssl.SSLSocket):
            raise ssl.SSLError("the socket already uses TLS")
        deadline = _deadline(timeout)
        tls = config.sock.context.wrap_socket(entry.sock, server_side=True, do_handshake_on_connect=False)
        entry.sock = tls
        return _handshake(ctx, sock_id, entry, tls, deadline)

    return _async(ctx, job)


def _shutdown(ctx, args):
    sock_id = _id("shutdown", args[0])
    entry = _lookup(ctx, sock_id, "socket")
    if entry is None:
        return _closed_now()

    def job():
        # M39: on a TLS socket this ends the sending side without TLS's
        # close notice (SSLSocket.shutdown would drop TLS for reading too).
        socket.socket.shutdown(entry.sock, socket.SHUT_WR)
        return NONE_VALUE

    return _async(ctx, job)


def _close(ctx, args):
    sock_id = _id("close", args[0])
    entry = ctx.io.sockets.pop(sock_id, None) if sock_id is not None else None

    def job():
        if entry is not None:
            try:
                entry.sock.close()
            except OSError:
                pass
        return NONE_VALUE

    return _async(ctx, job)


NATIVES = {
    "socket.connect": (3, _connect),
    "socket.listen": (3, _listen),
    "socket.accept": (2, _accept),
    "socket.send": (2, _send),
    "socket.recv": (3, _recv),
    "socket.shutdown": (1, _shutdown),
    "socket.close": (1, _close),
    "socket.start_tls": (3, _start_tls),
    "socket.tls_server_config": (2, _tls_server_config),
    "socket.start_tls_server": (3, _start_tls_server),
}
