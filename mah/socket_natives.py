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

Like the VM, this module never imports the compiler.
"""

from __future__ import annotations

import errno
import select
import socket
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
}
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


class _Entry:
    """A table slot: a connected socket (`kind` "socket") or a listener."""

    __slots__ = ("kind", "sock")

    def __init__(self, kind: str, sock):
        self.kind = kind
        self.sock = sock


def _classify(exc: BaseException) -> VectorValue:
    if isinstance(exc, _Closed):
        return _failure("closed")
    if isinstance(exc, (_TimedOut, TimeoutError, socket.timeout)):
        return _failure("timed_out")
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
        except (OSError, _Closed, _TimedOut) as exc:
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
            except (BlockingIOError, InterruptedError):
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
            except (BlockingIOError, InterruptedError):
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
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                if ctx.io.sockets.get(sock_id) is not entry:
                    raise _Closed()
                raise
            return BytesValue(chunk)

    return _async(ctx, job)


def _shutdown(ctx, args):
    sock_id = _id("shutdown", args[0])
    entry = _lookup(ctx, sock_id, "socket")
    if entry is None:
        return _closed_now()

    def job():
        entry.sock.shutdown(socket.SHUT_WR)
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
}
