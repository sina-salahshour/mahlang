"""M39 (docs/contracts/M39_http.md): TLS (`socket.start_tls`), `std:url`
and `std:http` -- what the .test.mh files can't cover: HTTPS against a
local TLS server with a throwaway certificate authority (SSL_CERT_FILE
names it, on both VMs), the native's argument errors, the bytecode minor
(1.19) and the checker's view of the modules. Plain-HTTP behavior is
covered by mah/std/http.test.mh, and URLs by mah/std/url.test.mh, on both
VMs. Runs on whichever VM `MAH_TEST_VM` selects.
"""

from __future__ import annotations

import http.server
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mah.bytecode.decode import decode  # noqa: E402
from mah.bytecode.format import MINOR, NATIVE_ARITIES, NATIVE_SINCE_MINOR  # noqa: E402
from tests.support import compile_bytes, run_source  # noqa: E402
from tests.test_typecheck import check  # noqa: E402

HTTP = 'import http from "std:http"\n'
SOCKET = 'import socket from "std:socket"\n'


def minor_of(src: str) -> int:
    return decode(compile_bytes(text=src)).minor


def _openssl(*args: str, cwd: str) -> None:
    subprocess.run(["openssl", *args], cwd=cwd, check=True, capture_output=True)


def _make_ca(directory: str, name: str) -> None:
    # The key usage keeps Python 3.13+'s default VERIFY_X509_STRICT happy
    # ("CA cert does not include key usage extension").
    _openssl(
        "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
        "-keyout", f"{name}.key", "-out", f"{name}.pem", "-subj", f"/CN=Mah {name}",
        "-addext", "basicConstraints=critical,CA:TRUE",
        "-addext", "keyUsage=critical,keyCertSign,cRLSign",
        cwd=directory,
    )


def _make_leaf(directory: str) -> None:
    """A server certificate for localhost and 127.0.0.1, signed by `ca`
    (not a self-signed CA certificate: rustls refuses those as a server's)."""
    with open(os.path.join(directory, "ext.cnf"), "w") as f:
        f.write("subjectAltName=DNS:localhost,IP:127.0.0.1\nbasicConstraints=CA:FALSE\nextendedKeyUsage=serverAuth\n")
    _openssl("req", "-newkey", "rsa:2048", "-nodes", "-keyout", "leaf.key", "-out", "leaf.csr",
             "-subj", "/CN=localhost", cwd=directory)
    _openssl("x509", "-req", "-in", "leaf.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial",
             "-out", "leaf.pem", "-days", "2", "-extfile", "ext.cnf", cwd=directory)


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _reply(self, status: int, body: bytes, content_type: str = "text/plain") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path == "/hello":
            self._reply(200, "héllo over TLS".encode())
        elif self.path == "/big":
            self._reply(200, b"x" * 300_000)
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/hello")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self._reply(404, b"no")

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self._reply(200, body, self.headers.get("Content-Type", "text/plain"))

    def log_message(self, *args):
        pass


@unittest.skipUnless(shutil.which("openssl"), "needs the openssl command to make certificates")
class HttpsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        _make_ca(cls.dir, "ca")
        _make_ca(cls.dir, "other")
        _make_leaf(cls.dir)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(os.path.join(cls.dir, "leaf.pem"), os.path.join(cls.dir, "leaf.key"))
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.server.socket = context.wrap_socket(cls.server.socket, server_side=True)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
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

    def url(self, path: str, host: str = "localhost") -> str:
        return f"https://{host}:{self.port}{path}"

    def test_get(self):
        src = HTTP + f'let r = http.get("{self.url("/hello")}")\nprint(r.status, r.text(), r.header("content-type"))\n'
        self.assertEqual(run_source(src), "200 héllo over TLS text/plain\n")

    def test_an_ip_address_and_a_big_body(self):
        src = HTTP + f'let r = http.get("{self.url("/big", "127.0.0.1")}")\nprint(r.body.len())\n'
        self.assertEqual(run_source(src), "300000\n")

    def test_post_and_redirect(self):
        src = HTTP + (
            f'let r = http.post("{self.url("/echo")}", json: ["a": [1, 2]])\n'
            'print(r.header("content-type"), r.json()["a"][1])\n'
            f'let d = http.get("{self.url("/redirect")}")\n'
            "print(d.text(), d.url)\n"
        )
        self.assertEqual(run_source(src), f"application/json 2\nhéllo over TLS {self.url('/hello')}\n")

    def test_an_untrusted_certificate(self):
        os.environ["SSL_CERT_FILE"] = os.path.join(self.dir, "other.pem")
        src = HTTP + (
            f'let r = try {{ http.get("{self.url("/hello")}"); "ok" }} catch {{ e: http.HttpError => {{ e.message() }} }}\n'
            "print(r)\n"
        )
        self.assertEqual(run_source(src), f"GET {self.url('/hello')}: the server's certificate isn't trusted\n")

    def test_a_name_the_certificate_does_not_have(self):
        src = SOCKET + (
            f'let c = socket.connect("127.0.0.1", {self.port})\n'
            'print(try { c.start_tls("other.example"); "ok" } catch { e: socket.SocketError => { e.kind + " " + e.op } })\n'
            "c.close()\n"
        )
        self.assertEqual(run_source(src), "tls_certificate start_tls\n")

    def test_socket_level_tls(self):
        src = SOCKET + (
            f'let c = socket.connect_tls("localhost", {self.port}, 5000)\n'
            'c.send_text("GET /hello HTTP/1.1\\r\\nHost: localhost\\r\\nConnection: close\\r\\n\\r\\n")\n'
            "print(c.read_line())\n"
            "c.close()\n"
            f'let d = socket.connect_tls("localhost", {self.port})\n'
            'print(try { d.start_tls("localhost"); "again" } catch { e: socket.SocketError => { e.kind } })\n'
            "d.close()\n"
        )
        self.assertEqual(run_source(src), "HTTP/1.1 200 OK\ntls\n")

    def test_a_server_that_does_not_speak_tls(self):
        src = SOCKET + (
            "let server = socket.listen(0)\n"
            "fn answer() {\n"
            "    let conn = server.accept()\n"
            '    conn.send_text("HTTP/1.1 400 Bad Request\\r\\n\\r\\n")\n'
            "    conn.recv(65536, 2000)\n"
            "    conn.close()\n"
            "}\n"
            "let done = detach answer()\n"
            'let c = socket.connect("127.0.0.1", server.port)\n'
            'print(try { c.start_tls("localhost", 5000); "ok" } catch { e: socket.SocketError => { e.kind } })\n'
            "c.close()\n"
            "done.await\n"
            "server.close()\n"
        )
        self.assertEqual(run_source(src), "tls\n")

    def test_the_server_closing_during_the_handshake(self):
        src = SOCKET + (
            "let server = socket.listen(0)\n"
            "let accepted = detach server.accept()\n"
            'let c = socket.connect("127.0.0.1", server.port)\n'
            "accepted.await.close()\n"
            'print(try { c.start_tls("localhost", 5000); "ok" } catch { e: socket.SocketError => { e.kind } })\n'
            "c.close()\n"
            "server.close()\n"
        )
        self.assertEqual(run_source(src), "connection_reset\n")


def error(expr: str) -> str:
    """`KIND: message` of the runtime error `expr` throws."""
    return run_source(
        SOCKET
        + 'let s: Unknown = "x"\nlet n: Unknown = 5\n'
        + f"print(try {{ {expr} }} catch {{\n"
        + '  RuntimeError.TypeMismatch { message } => { "TypeMismatch: " + message }\n'
        + '  RuntimeError.ArgumentError { message } => { "ArgumentError: " + message }\n'
        + '  e => { "other: " + e.message() }\n'
        + "})"
    ).rstrip("\n")


class StartTlsArgumentTests(unittest.TestCase):
    SOCK = 'socket.Socket { id: 999, peer_host: "h", peer_port: 1, local_port: 1, buffer: "".to_bytes() }'

    def test_argument_errors(self):
        cases = [
            (f"{self.SOCK}.start_tls(n)", "TypeMismatch: start_tls: server name must be a String, got Number"),
            (f'{self.SOCK}.start_tls("h", s)', "TypeMismatch: start_tls: timeout must be a Number or none, got String"),
            (f'{self.SOCK}.start_tls("h", 0 - 1)', "ArgumentError: start_tls: timeout must be a whole number of at least 0, got -1"),
            (f'{self.SOCK}.start_tls("h")', "other: start_tls: the socket is closed: h:1"),
        ]
        for expr, expected in cases:
            with self.subTest(expr=expr):
                self.assertEqual(error(expr), expected)

    def test_unread_bytes_are_an_argument_error(self):
        sock = self.SOCK.replace('buffer: "".to_bytes()', 'buffer: "x".to_bytes()')
        self.assertEqual(
            error(f'{sock}.start_tls("h")'),
            "ArgumentError: start_tls: the socket has unread bytes in its buffer",
        )


class BytecodeTests(unittest.TestCase):
    def test_minor(self):
        self.assertEqual(MINOR, 20)
        self.assertEqual(NATIVE_ARITIES["socket.start_tls"], 3)
        self.assertEqual(NATIVE_SINCE_MINOR["socket.start_tls"], 19)
        self.assertEqual(minor_of(HTTP + 'print(http.get)'), 20)
        self.assertEqual(minor_of(SOCKET + 'print(socket.listen(0))'), 20)

    def test_std_url_alone_needs_no_new_vm(self):
        self.assertLess(minor_of('import url from "std:url"\nprint(url.encode("a b"))'), 19)


class CheckerTests(unittest.TestCase):
    def test_types(self):
        diagnostics, types = check(
            HTTP
            + 'import url from "std:url"\n'
            + 'let r = http.get("http://h/")\n'
            + "let status = r.status\n"
            + "let body = r.body\n"
            + 'let u = url.parse("http://h/")\n'
            + 'let pairs = url.parse_query("a=1")\n'
        )
        self.assertEqual(types["r"][-1], "Response")
        self.assertEqual(types["status"][-1], "Number")
        self.assertEqual(types["body"][-1], "Bytes")
        self.assertEqual(types["u"][-1], "Url")
        unhandled = sorted(d[1] for d in diagnostics if d[0] == "unhandled")
        self.assertEqual(len(unhandled), 3)
        self.assertTrue(any("HttpError" in m for m in unhandled))
        self.assertEqual(sum("UrlError" in m for m in unhandled), 2)


if __name__ == "__main__":
    unittest.main()
