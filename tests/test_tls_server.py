"""M42 part B (docs/contracts/M42_http_server.md #4): TLS servers --
`socket.tls_server_config` and `Socket.start_tls_server`, with a throwaway
certificate authority made by the `openssl` command (SSL_CERT_FILE names it
while Mah clients run, on both VMs). Covers loading errors, Mah-to-Mah and
Python-to-Mah handshakes (RSA and ECDSA certificates, TLS 1.2 and 1.3),
handshake failures, ids, argument errors and the bytecode minor (1.20).
Runs on whichever VM `MAH_TEST_VM` selects.
"""

from __future__ import annotations

import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mah.bytecode.decode import decode  # noqa: E402
from mah.bytecode.format import MINOR, NATIVE_ARITIES, NATIVE_SINCE_MINOR  # noqa: E402
from tests.support import compile_bytes, run_source  # noqa: E402
from tests.test_http import _make_ca, _make_leaf, _openssl  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOCKET = 'import socket from "std:socket"\n'
HTTP = 'import http from "std:http"\n'


def minor_of(src: str) -> int:
    return decode(compile_bytes(text=src)).minor


def mah_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def error(src: str, expr: str) -> str:
    """`KIND: message` of the error `expr` throws (after `src`)."""
    return run_source(
        SOCKET
        + 'let s: Unknown = "x"\nlet n: Unknown = 5\nlet neg: Unknown = -1\nlet f: Unknown = 1.5\n'
        + src
        + f"print(try {{ {expr} }} catch {{\n"
        + '  RuntimeError.TypeMismatch { message } => { "TypeMismatch: " + message }\n'
        + '  RuntimeError.ArgumentError { message } => { "ArgumentError: " + message }\n'
        + '  e: socket.SocketError => { e.kind + ": " + e.message() }\n'
        + '  e => { "other: " + e.message() }\n'
        + "})"
    ).rstrip("\n")


class _MahInThread:
    """Runs a Mah program in a thread (it listens on a port Python chose)."""

    def __init__(self, src: str):
        self.out: str | None = None
        self.exc: BaseException | None = None
        self.thread = threading.Thread(target=self._run, args=(src,), daemon=True)
        self.thread.start()

    def _run(self, src: str) -> None:
        try:
            self.out = run_source(src)
        except BaseException as exc:  # noqa: BLE001
            self.exc = exc

    def result(self, timeout: float = 30) -> str:
        self.thread.join(timeout)
        if self.thread.is_alive():
            raise AssertionError("the Mah program didn't finish")
        if self.exc is not None:
            raise self.exc
        return self.out or ""


def connect_retrying(port: int, deadline: float = 20) -> socket.socket:
    """A TCP connection to 127.0.0.1:port once the Mah program listens."""
    end = time.monotonic() + deadline
    while True:
        try:
            return socket.create_connection(("127.0.0.1", port), timeout=10)
        except OSError:
            if time.monotonic() > end:
                raise
            time.sleep(0.05)


def read_line(sock) -> bytes:
    data = b""
    while not data.endswith(b"\n"):
        chunk = sock.recv(1024)
        if not chunk:
            break
        data += chunk
    return data


@unittest.skipUnless(shutil.which("openssl"), "needs the openssl command to make certificates")
class TlsServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        d = cls.dir
        _make_ca(d, "ca")
        _make_ca(d, "other")
        _make_leaf(d)  # leaf.pem / leaf.key (RSA), signed by ca, with ext.cnf
        _openssl("ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", "ec.key", cwd=d)
        _openssl("req", "-new", "-key", "ec.key", "-out", "ec.csr", "-subj", "/CN=localhost", cwd=d)
        _openssl("x509", "-req", "-in", "ec.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial",
                 "-out", "ec.pem", "-days", "2", "-extfile", "ext.cnf", cwd=d)
        _openssl("genpkey", "-algorithm", "RSA", "-aes256", "-pass", "pass:x", "-out", "enc.key", cwd=d)
        # review: `ecparam -genkey` without -noout writes an EC PARAMETERS
        # block before the key; and one file holding both the chain and the
        # key (a common layout), given as both paths
        _openssl("ecparam", "-name", "prime256v1", "-genkey", "-out", "ecparams.key", cwd=d)
        _openssl("req", "-new", "-key", "ecparams.key", "-out", "ecparams.csr", "-subj", "/CN=localhost", cwd=d)
        _openssl("x509", "-req", "-in", "ecparams.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial",
                 "-out", "ecparams.pem", "-days", "2", "-extfile", "ext.cnf", cwd=d)
        with open(os.path.join(d, "both.pem"), "w") as f:
            for name in ("leaf.pem", "ca.pem", "leaf.key"):
                with open(os.path.join(d, name)) as part:
                    f.write(part.read())
        with open(os.path.join(d, "empty.pem"), "w") as f:
            f.write("no PEM blocks here\n")
        with open(os.path.join(d, "leaf.pem")) as f:
            lines = f.read().splitlines()
        # a base64 line in the middle of the body, made invalid
        lines[3] = "!!!!" + lines[3][4:]
        with open(os.path.join(d, "corrupt.pem"), "w") as f:
            f.write("\n".join(lines) + "\n")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in ("SSL_CERT_FILE", "HTTPS_PROXY", "https_proxy")}
        os.environ["SSL_CERT_FILE"] = self.path("ca.pem")
        os.environ.pop("HTTPS_PROXY", None)
        os.environ.pop("https_proxy", None)

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def path(self, name: str) -> str:
        return os.path.join(self.dir, name)

    def config(self, cert: str, key: str) -> str:
        return f"socket.tls_server_config({mah_string(self.path(cert))}, {mah_string(self.path(key))})"

    # 1. config errors -------------------------------------------------------------

    def test_config_errors(self):
        cases = [
            ("missing.pem", "leaf.key", "can't read the certificate file", "missing.pem"),
            ("leaf.pem", "missing.key", "can't read the private key file", "leaf.pem"),
            ("empty.pem", "leaf.key", "no certificate in the certificate file", "empty.pem"),
            ("leaf.pem", "enc.key", "the private key is encrypted", "leaf.pem"),
            ("leaf.pem", "leaf.pem", "no private key in the key file", "leaf.pem"),
            ("leaf.pem", "other.key", "the private key doesn't match the certificate", "leaf.pem"),
            ("corrupt.pem", "leaf.key", "the certificate or private key isn't usable", "corrupt.pem"),
        ]
        for cert, key, description, address in cases:
            with self.subTest(cert=cert, key=key):
                self.assertEqual(
                    error("", self.config(cert, key)),
                    f"tls_config: tls_server_config: {description}: {self.path(address)}",
                )

    def test_a_nul_in_a_path_is_unreadable(self):
        # review: Python's open() raises ValueError (not OSError) for this,
        # which used to kill the worker thread and hang the program.
        nul = 'bytes.from_hex("00").to_text().unwrap()'
        src = 'import bytes from "std:bytes"\n'
        self.assertEqual(
            error(src, f'socket.tls_server_config("cert" + {nul}, "key")'),
            "tls_config: tls_server_config: can't read the certificate file: cert\x00",
        )
        self.assertEqual(
            error(src, f'socket.tls_server_config({mah_string(self.path("leaf.pem"))}, "key" + {nul})'),
            f"tls_config: tls_server_config: can't read the private key file: {self.path('leaf.pem')}",
        )

    def test_a_config_prints_its_certificate_path(self):
        src = SOCKET + f"let cfg = {self.config('leaf.pem', 'leaf.key')}\nprint(cfg)\ncfg.close()\ncfg.close()\n"
        self.assertEqual(run_source(src), f"TlsServerConfig({self.path('leaf.pem')})\n")

    # 2. Mah server, Mah client ------------------------------------------------------

    def mah_echo(self, cert: str, key: str) -> str:
        return SOCKET + (
            f"let cfg = {self.config(cert, key)}\n"
            "let server = socket.listen(0)\n"
            "fn serve_one() {\n"
            "    let conn = server.accept(10000)\n"
            "    conn.start_tls_server(cfg, 5000)\n"
            "    let line = conn.read_line(5000)\n"
            '    conn.send_text("echo: " + line + "\\n")\n'
            '    let again = try { conn.start_tls_server(cfg, 5000); "again" } catch { e: socket.SocketError => { "twice: " + e.kind } }\n'
            "    conn.close()\n"
            "    again\n"
            "}\n"
            "let done = detach serve_one()\n"
            'let c = socket.connect_tls("localhost", server.port, 5000)\n'
            'c.send_text("hi there\\n")\n'
            "print(c.read_line(5000))\n"
            "print(done.await)\n"
            "c.close()\n"
            "server.close()\n"
            "cfg.close()\n"
        )

    def test_mah_server_mah_client_rsa(self):
        self.assertEqual(run_source(self.mah_echo("leaf.pem", "leaf.key")), "echo: hi there\ntwice: tls\n")

    def test_mah_server_mah_client_ecdsa(self):
        self.assertEqual(run_source(self.mah_echo("ec.pem", "ec.key")), "echo: hi there\ntwice: tls\n")

    def test_one_file_with_chain_and_key(self):
        self.assertEqual(run_source(self.mah_echo("both.pem", "both.pem")), "echo: hi there\ntwice: tls\n")

    def test_an_ec_key_after_its_parameters(self):
        self.assertEqual(run_source(self.mah_echo("ecparams.pem", "ecparams.key")), "echo: hi there\ntwice: tls\n")

    def test_closing_the_socket_during_the_handshake(self):
        # review: another task closes the connection while start_tls_server
        # waits for a silent client
        port = free_port()
        mah = _MahInThread(SOCKET + (
            f"let cfg = {self.config('leaf.pem', 'leaf.key')}\n"
            f"let server = socket.listen({port})\n"
            "let conn = server.accept(20000)\n"
            "fn handshake() -> String {\n"
            '    try { conn.start_tls_server(cfg, 10000); "done" } catch { e: socket.SocketError => { e.kind } }\n'
            "}\n"
            "let pending = detach handshake()\n"
            "sleep_async(200)\n"
            "conn.close()\n"
            "print(pending.await)\n"
            "server.close()\n"
        ))
        with connect_retrying(port):
            out = mah.result()
        self.assertEqual(out, "closed\n")

    # 3. Mah server, Python client ---------------------------------------------------

    def mah_server(self, port: int, timeout: int = 5000, echo: bool = True, cert: str = "leaf.pem",
                   key: str = "leaf.key") -> _MahInThread:
        body = (
            '    let line = conn.read_line(5000)\n'
            '    conn.send_text("echo: " + line + "\\n")\n'
            '    "ok"\n'
        ) if echo else '    "ok"\n'
        src = SOCKET + (
            f"let cfg = {self.config(cert, key)}\n"
            f"let server = socket.listen({port})\n"
            "let conn = server.accept(20000)\n"
            "let r = try {\n"
            f"    conn.start_tls_server(cfg, {timeout})\n"
            + body
            + "} catch { e: socket.SocketError => { e.kind + \" \" + e.op } }\n"
            "print(r)\n"
            "conn.close()\n"
            "server.close()\n"
        )
        return _MahInThread(src)

    def python_client(self, port: int, **context_options) -> tuple[bytes, str]:
        context = ssl.create_default_context(cafile=self.path("ca.pem"))
        for name, value in context_options.items():
            setattr(context, name, value)
        with connect_retrying(port) as raw:
            with context.wrap_socket(raw, server_hostname="localhost") as tls:
                tls.sendall(b"from python\n")
                return read_line(tls), tls.version()

    def test_python_client(self):
        for cert, key in (("leaf.pem", "leaf.key"), ("ec.pem", "ec.key")):
            with self.subTest(cert=cert):
                port = free_port()
                mah = self.mah_server(port, cert=cert, key=key)
                line, version = self.python_client(port)
                self.assertEqual(line, b"echo: from python\n")
                self.assertIn(version, ("TLSv1.2", "TLSv1.3"))
                self.assertEqual(mah.result(), "ok\n")

    def test_python_client_tls_1_2(self):
        port = free_port()
        mah = self.mah_server(port)
        line, version = self.python_client(port, maximum_version=ssl.TLSVersion.TLSv1_2)
        self.assertEqual(line, b"echo: from python\n")
        self.assertEqual(version, "TLSv1.2")
        self.assertEqual(mah.result(), "ok\n")

    # 4. handshake failures ----------------------------------------------------------

    def test_a_client_that_does_not_speak_tls(self):
        port = free_port()
        mah = self.mah_server(port, echo=False)
        with connect_retrying(port) as raw:
            raw.sendall(b"GET / HTTP/1.1\r\n\r\n")
            try:
                while raw.recv(1024):
                    pass
            except OSError:
                pass
        self.assertEqual(mah.result(), "tls start_tls_server\n")

    def test_a_client_that_closes_at_once(self):
        port = free_port()
        mah = self.mah_server(port, echo=False)
        connect_retrying(port).close()
        self.assertEqual(mah.result(), "connection_reset start_tls_server\n")

    def test_a_silent_client_times_out(self):
        port = free_port()
        mah = self.mah_server(port, timeout=300, echo=False)
        with connect_retrying(port):
            out = mah.result()
        self.assertEqual(out, "timed_out start_tls_server\n")

    def test_a_client_that_does_not_trust_the_certificate(self):
        port = free_port()
        mah = self.mah_server(port, echo=False)
        context = ssl.create_default_context(cafile=self.path("other.pem"))
        with connect_retrying(port) as raw:
            with self.assertRaises(ssl.SSLError):
                context.wrap_socket(raw, server_hostname="localhost")
        self.assertEqual(mah.result(), "tls start_tls_server\n")

    # 5. ids -------------------------------------------------------------------------

    def test_ids(self):
        src = SOCKET + (
            f"let cfg = {self.config('leaf.pem', 'leaf.key')}\n"
            "let server = socket.listen(0)\n"
            "let incoming = detach server.accept()\n"
            'let c = socket.connect("127.0.0.1", server.port)\n'
            "let conn = incoming.await\n"
            "fn kind(f: fn() -> Unknown) -> String {\n"
            '    try { f(); "ok" } catch { e: socket.SocketError => { e.kind } }\n'
            "}\n"
            'print(kind(fn() { socket.Socket { id: cfg.id, peer_host: "h", peer_port: 1, local_port: 1, buffer: "".to_bytes() }.recv(10, 0) }))\n'
            'print(kind(fn() { conn.start_tls_server(socket.TlsServerConfig { id: c.id, cert_path: "c", key_path: "k" }, 1000) }))\n'
            'print(kind(fn() { conn.start_tls_server(socket.TlsServerConfig { id: 0 - 3, cert_path: "c", key_path: "k" }, 1000) }))\n'
            "cfg.close()\n"
            "cfg.close()\n"
            "print(kind(fn() { conn.start_tls_server(cfg, 1000) }))\n"
            "c.close()\n"
            "conn.close()\n"
            "server.close()\n"
        )
        self.assertEqual(run_source(src), "closed\nclosed\nclosed\nclosed\n")


# 6. argument errors ---------------------------------------------------------------


class ArgumentTests(unittest.TestCase):
    SOCK = 'socket.Socket { id: 999, peer_host: "h", peer_port: 1, local_port: 1, buffer: "".to_bytes() }'
    CFG = 'socket.TlsServerConfig { id: 999, cert_path: "c", key_path: "k" }'

    def test_tls_server_config(self):
        cases = [
            ('socket.tls_server_config(n, "k")',
             "TypeMismatch: tls_server_config: certificate path must be a String, got Number"),
            ('socket.tls_server_config("c", n)',
             "TypeMismatch: tls_server_config: key path must be a String, got Number"),
        ]
        for expr, expected in cases:
            with self.subTest(expr=expr):
                self.assertEqual(error("", expr), expected)

    def test_start_tls_server(self):
        bad_sock = self.SOCK.replace("id: 999", "id: s")
        bad_cfg = self.CFG.replace("id: 999", "id: s")
        cases = [
            (f"{bad_sock}.start_tls_server({self.CFG})",
             "TypeMismatch: start_tls_server: expected a socket id, got String"),
            (f"{self.SOCK}.start_tls_server({bad_cfg})",
             "TypeMismatch: start_tls_server: expected a TLS config id, got String"),
            (f"{self.SOCK}.start_tls_server({self.CFG}, s)",
             "TypeMismatch: start_tls_server: timeout must be a Number or none, got String"),
            (f"{self.SOCK}.start_tls_server({self.CFG}, neg)",
             "ArgumentError: start_tls_server: timeout must be a whole number of at least 0, got -1"),
            (f"{self.SOCK}.start_tls_server({self.CFG}, f)",
             "ArgumentError: start_tls_server: timeout must be a whole number of at least 0, got 1.5"),
            (f"{self.SOCK}.start_tls_server({self.CFG})",
             "closed: start_tls_server: the socket is closed: h:1"),
            (f"{self.SOCK.replace('id: 999', 'id: f')}.start_tls_server({self.CFG.replace('id: 999', 'id: f')})",
             "closed: start_tls_server: the socket is closed: h:1"),
        ]
        for expr, expected in cases:
            with self.subTest(expr=expr):
                self.assertEqual(error("", expr), expected)

    def test_unread_bytes_are_an_argument_error(self):
        sock = self.SOCK.replace('buffer: "".to_bytes()', 'buffer: "x".to_bytes()')
        self.assertEqual(
            error("", f"{sock}.start_tls_server({self.CFG})"),
            "ArgumentError: start_tls_server: the socket has unread bytes in its buffer",
        )


# 7. bytecode ----------------------------------------------------------------------


class BytecodeTests(unittest.TestCase):
    def test_minor(self):
        self.assertEqual(MINOR, 20)
        self.assertEqual(NATIVE_ARITIES["socket.tls_server_config"], 2)
        self.assertEqual(NATIVE_ARITIES["socket.start_tls_server"], 3)
        self.assertEqual(NATIVE_SINCE_MINOR["socket.tls_server_config"], 20)
        self.assertEqual(NATIVE_SINCE_MINOR["socket.start_tls_server"], 20)
        self.assertEqual(minor_of(SOCKET + "print(socket.listen)"), 20)
        self.assertEqual(minor_of(HTTP + "print(http.get)"), 20)


# 8. HTTPS server (needs M42 part A) -----------------------------------------------


def _has_serve() -> bool:
    with open(os.path.join(REPO, "mah", "std", "http.mh")) as f:
        return "export fn serve" in f.read()


@unittest.skipUnless(shutil.which("openssl"), "needs the openssl command to make certificates")
@unittest.skipUnless(_has_serve(), "needs M42 part A")
class HttpsServerTests(unittest.TestCase):
    """`http.serve(tls:)` end to end. The handler answers with the request's
    path and whether it came over TLS."""

    HANDLER = (
        "fn handle(req: http.Request) -> http.Reply {\n"
        '    let how = if req.tls { "tls" } else { "plain" }\n'
        '    http.Reply.text(req.path + " " + how)\n'
        "}\n"
    )

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        _make_ca(cls.dir, "ca")
        _make_leaf(cls.dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in ("SSL_CERT_FILE", "HTTPS_PROXY", "https_proxy")}
        os.environ["SSL_CERT_FILE"] = os.path.join(self.dir, "ca.pem")
        os.environ.pop("HTTPS_PROXY", None)
        os.environ.pop("https_proxy", None)

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def config(self) -> str:
        cert = mah_string(os.path.join(self.dir, "leaf.pem"))
        key = mah_string(os.path.join(self.dir, "leaf.key"))
        return f"socket.tls_server_config({cert}, {key})"

    def test_mah_client(self):
        src = SOCKET + HTTP + self.HANDLER + (
            f"let server = http.serve(0, handle, tls: {self.config()})\n"
            'print(server.tls, server.url("/hi").starts_with("https://127.0.0.1:"))\n'
            'let r = http.get(server.url("/hi"))\n'
            "print(r.status, r.text())\n"
            "server.close(1000)\n"
        )
        self.assertEqual(run_source(src), "true true\n200 /hi tls\n")

    def test_python_client_keep_alive_and_plain_http(self):
        import http.client

        port = free_port()
        control = free_port()
        # serves until Python connects to the control port
        mah = _MahInThread(SOCKET + HTTP + self.HANDLER + (
            f"let server = http.serve({port}, handle, tls: {self.config()})\n"
            f"let ctl = socket.listen({control})\n"
            "ctl.accept(60000).close()\n"
            "ctl.close()\n"
            "server.close(1000)\n"
            'print("stopped")\n'
        ))
        try:
            connect_retrying(port).close()
            context = ssl.create_default_context(cafile=os.path.join(self.dir, "ca.pem"))
            conn = http.client.HTTPSConnection("localhost", port, context=context, timeout=10)
            conn.request("GET", "/a")
            first = conn.getresponse().read()
            sock = conn.sock
            conn.request("GET", "/b")
            second = conn.getresponse().read()
            self.assertIs(conn.sock, sock)  # one connection for both
            conn.close()
            self.assertEqual((first, second), (b"/a tls", b"/b tls"))
            # plain HTTP to the TLS port: no HTTP reply, the connection closes
            with connect_retrying(port) as raw:
                raw.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
                got = b""
                try:
                    while True:
                        chunk = raw.recv(1024)
                        if not chunk:
                            break
                        got += chunk
                except OSError:
                    pass
            self.assertFalse(got.startswith(b"HTTP/"))
            # and the server keeps serving
            conn = http.client.HTTPSConnection("localhost", port, context=context, timeout=10)
            conn.request("GET", "/c")
            self.assertEqual(conn.getresponse().read(), b"/c tls")
            conn.close()
        finally:
            connect_retrying(control).close()
        self.assertEqual(mah.result(), "stopped\n")


if __name__ == "__main__":
    unittest.main()
