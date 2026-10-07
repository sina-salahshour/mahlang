"""M42 part A (docs/contracts/M42_http_server.md #3): `std:http`'s server,
talked to by Python clients (`http.client`, `urllib`, raw sockets) -- what
mah/std/http_server.test.mh (Mah clients, both VMs) can't show: that real
third-party clients agree with it. Also the checker's view of the server API
and the bytecode minor. Runs on whichever VM `MAH_TEST_VM` selects: the Mah
server runs in a background thread (in-process on the Python VM, `mah-vm` on
the Rust one).
"""

from __future__ import annotations

import http.client
import os
import socket
import sys
import threading
import time
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mah.bytecode.decode import decode  # noqa: E402
from mah.bytecode.format import MINOR  # noqa: E402
from tests.support import compile_bytes, run_source  # noqa: E402
from tests.test_typecheck import check  # noqa: E402

HTTP = 'import http from "std:http"\n'

PROGRAM = """import http from "std:http"
let running = []
{app}
fn handle(req: http.Request) -> http.Reply {{
    if req.path == "/__quit" {{
        running[0].shutdown()
        return http.Reply.text("bye")
    }}
    app(req)
}}
let server = http.serve({port}, handle{options})
running.push(server)
server.wait()
print("stopped")
"""

# Echoes what it got; /slow sleeps first.
ECHO_APP = """
fn app(req: http.Request) -> http.Reply {
    if req.path == "/slow" { sleep_async(500) }
    if req.path == "/nap" { sleep_async(200) }
    if req.path == "/length" { return http.Reply.text("" + req.body.len()) }
    if req.path == "/parts" {
        let out = []
        for let part in req.multipart() {
            out.push([part.name, part.filename, part.content_type, part.body.len()])
        }
        return http.Reply.json(out)
    }
    if req.method == "POST" { return http.Reply.bytes(req.body) }
    http.Reply.text(req.method + " " + req.target + " " + req.version)
}
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class MahServer:
    """`with MahServer(app) as s:` runs PROGRAM on a free port in a thread,
    waits until it answers, and on exit asks it to quit (GET /__quit) and
    checks it stopped."""

    def __init__(self, app: str = ECHO_APP, options: str = ""):
        self.port = free_port()
        self.source = PROGRAM.format(app=app, port=self.port, options=options)
        self.out: str | None = None
        self.exc: BaseException | None = None

    def _run(self) -> None:
        try:
            self.out = run_source(self.source)
        except BaseException as exc:  # noqa: BLE001
            self.exc = exc

    def __enter__(self) -> "MahServer":
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        end = time.monotonic() + 10
        while True:
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=1).close()
                return self
            except OSError:
                if self.exc is not None:
                    raise self.exc
                if time.monotonic() > end:
                    raise AssertionError("the Mah server didn't start listening")
                time.sleep(0.05)

    def __exit__(self, exc_type, exc, tb) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("GET", "/__quit")
            conn.getresponse().read()
        finally:
            conn.close()
        self.thread.join(30)
        if exc_type is not None:
            return
        if self.thread.is_alive():
            raise AssertionError("the Mah server didn't stop")
        if self.exc is not None:
            raise self.exc
        assert self.out is not None and self.out.endswith("stopped\n"), self.out

    def raw(self) -> socket.socket:
        return socket.create_connection(("127.0.0.1", self.port), timeout=10)

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"


def read_all(sock: socket.socket) -> bytes:
    data = b""
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            return data
        data += chunk


class ServerWithPythonClientsTests(unittest.TestCase):
    def test_keep_alive_with_http_client(self):
        with MahServer() as s:
            conn = http.client.HTTPConnection("127.0.0.1", s.port, timeout=10)
            conn.connect()
            first = conn.sock
            for i in range(3):
                conn.request("GET", f"/n/{i}?x=1")
                r = conn.getresponse()
                self.assertEqual(r.status, 200)
                self.assertEqual(r.read(), f"GET /n/{i}?x=1 HTTP/1.1".encode())
                self.assertEqual(r.getheader("Content-Type"), "text/plain; charset=utf-8")
                self.assertIsNone(r.getheader("Connection"))
            self.assertIs(conn.sock, first)
            conn.close()

    def test_chunked_upload(self):
        with MahServer() as s:
            conn = http.client.HTTPConnection("127.0.0.1", s.port, timeout=10)
            conn.request("POST", "/echo", body=iter([b"one ", b"two ", b"three"]), encode_chunked=True)
            r = conn.getresponse()
            self.assertEqual(r.status, 200)
            self.assertEqual(r.read(), b"one two three")
            conn.close()

    def test_a_large_upload(self):
        body = bytes(range(256)) * (5 * 1024 * 1024 // 256)
        with MahServer() as s:
            conn = http.client.HTTPConnection("127.0.0.1", s.port, timeout=60)
            conn.request("POST", "/length", body=body)
            r = conn.getresponse()
            self.assertEqual(r.status, 200)
            self.assertEqual(r.read(), str(len(body)).encode())
            conn.close()

    def test_multipart_upload_with_urllib(self):
        boundary = "----mahBoundary7"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="title"\r\n\r\n'
            "hello\r\n"
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="upload"; filename="data.bin"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode() + bytes([0, 1, 2, 255, 13, 10]) + f"\r\n--{boundary}--\r\n".encode()
        with MahServer() as s:
            req = urllib.request.Request(
                s.url("/parts"),
                data=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                self.assertEqual(
                    r.read().decode(),
                    '[["title",null,null,5],["upload","data.bin","application/octet-stream",6]]',
                )

    def test_raw_100_continue(self):
        with MahServer() as s:
            sock = s.raw()
            sock.sendall(b"POST /echo HTTP/1.1\r\nHost: x\r\nContent-Length: 3\r\nExpect: 100-continue\r\n\r\n")
            self.assertEqual(sock.recv(65536), b"HTTP/1.1 100 Continue\r\n\r\n")
            sock.sendall(b"abc")
            sock.settimeout(10)
            reply = b""
            while not reply.endswith(b"abc"):
                reply += sock.recv(65536)
            self.assertTrue(reply.startswith(b"HTTP/1.1 200 OK\r\n"), reply)
            sock.close()

    def test_a_slow_head_times_out(self):
        with MahServer(options=", read_timeout: 500") as s:
            sock = s.raw()
            start = time.monotonic()
            got = b""
            for byte in b"GET / HTTP/1.1\r\nHost: x\r\nX-Slow: " + b"y" * 200:
                try:
                    sock.sendall(bytes([byte]))
                except OSError:
                    break
                sock.settimeout(0.1)
                try:
                    got = sock.recv(65536)
                except socket.timeout:
                    continue
                except OSError:
                    break
                break
            sock.settimeout(5)
            got += read_all(sock)
            self.assertTrue(got.startswith(b"HTTP/1.1 408 Request Timeout\r\n"), got)
            self.assertTrue(got.endswith(b"the request took too long to arrive"), got)
            self.assertLess(time.monotonic() - start, 2)
            sock.close()

    def test_parallel_requests(self):
        with MahServer() as s:
            results: list[bytes] = []

            def fetch(i: int) -> None:
                with urllib.request.urlopen(s.url(f"/nap?i={i}"), timeout=10) as r:
                    results.append(r.read())

            start = time.monotonic()
            threads = [threading.Thread(target=fetch, args=(i,)) for i in range(20)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(len(results), 20)
            self.assertLess(time.monotonic() - start, 3)

    def test_http_1_0(self):
        with MahServer() as s:
            sock = s.raw()
            sock.sendall(b"GET /old HTTP/1.0\r\n\r\n")
            reply = read_all(sock)
            self.assertTrue(reply.startswith(b"HTTP/1.1 200 OK\r\n"), reply)
            self.assertIn(b"\r\nConnection: close\r\n", reply)
            self.assertTrue(reply.endswith(b"\r\n\r\nGET /old HTTP/1.0"), reply)
            sock.close()

    def test_shutdown_lets_a_request_in_flight_finish(self):
        server = MahServer()
        with server as s:
            box: list = []

            def slow() -> None:
                conn = http.client.HTTPConnection("127.0.0.1", s.port, timeout=10)
                conn.request("GET", "/slow")
                r = conn.getresponse()
                box.append((r.status, r.getheader("Connection"), r.read()))
                conn.close()

            t = threading.Thread(target=slow)
            t.start()
            time.sleep(0.2)
        # (the `with` asked the server to quit while /slow was sleeping)
        t.join(10)
        self.assertEqual(box, [(200, "close", b"GET /slow HTTP/1.1")])
        self.assertEqual(server.out, "stopped\n")


class CheckerTests(unittest.TestCase):
    def test_std_http_is_clean(self):
        diagnostics, _ = check(HTTP)
        self.assertEqual([d for d in diagnostics if d[0] != "implicit"], [])

    def test_serve_gives_a_server(self):
        _, types = check(HTTP + 'let s = http.serve(0, fn(r: http.Request) -> http.Reply { http.Reply.text("x") })\n')
        self.assertEqual(types["s"][-1], "Server")


class BytecodeTests(unittest.TestCase):
    def test_minor(self):
        self.assertEqual(decode(compile_bytes(text=HTTP + "print(http.serve)")).minor, MINOR)


if __name__ == "__main__":
    unittest.main()
