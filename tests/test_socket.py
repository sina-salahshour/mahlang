"""M38 (docs/contracts/M38_socket.md): `std:socket` (TCP) -- the natives'
argument errors, the checker's view of the module, the bytecode minor (1.18)
and the "pending I/O keeps the program running" rule, on whichever VM
`MAH_TEST_VM` selects. The behavior of every function is covered by
mah/std/socket.test.mh (both VMs); the natives are in mah/socket_natives.py
and runtime/src/vm/socket.rs.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mah.bytecode.decode import decode  # noqa: E402
from mah.bytecode.format import MINOR, NATIVE_ARITIES, NATIVE_SINCE_MINOR  # noqa: E402
from tests.support import compile_bytes, run_source  # noqa: E402
from tests.test_typecheck import check  # noqa: E402

SOCKET = 'import socket from "std:socket"\n'
SOCK = 'socket.Socket { id: 1, peer_host: "h", peer_port: 1, local_port: 1, buffer: "".to_bytes() }'
UNKNOWNS = 'let s: Unknown = "x"\nlet f: Unknown = 1.5\nlet neg: Unknown = -1\nlet nan: Unknown = [1]\n'


def error(expr: str) -> str:
    """`KIND: message` of the runtime error `expr` throws."""
    return run_source(
        SOCKET
        + UNKNOWNS
        + f"print(try {{ {expr} }} catch {{\n"
        + '  RuntimeError.TypeMismatch { message } => { "TypeMismatch: " + message }\n'
        + '  RuntimeError.ArgumentError { message } => { "ArgumentError: " + message }\n'
        + '  e => { "other: " + e.message() }\n'
        + "})"
    ).rstrip("\n")


def minor_of(src: str) -> int:
    return decode(compile_bytes(text=src)).minor


class ArgumentErrorTests(unittest.TestCase):
    def test_connect(self):
        cases = [
            ("socket.connect(nan, 80)", "TypeMismatch: connect: host must be a String, got Vector"),
            ('socket.connect("127.0.0.1", s)', "TypeMismatch: connect: port must be a Number, got String"),
            ('socket.connect("127.0.0.1", 0)', "ArgumentError: connect: port must be a whole number from 1 to 65535, got 0"),
            ('socket.connect("127.0.0.1", 65536)', "ArgumentError: connect: port must be a whole number from 1 to 65535, got 65536"),
            ('socket.connect("127.0.0.1", f)', "ArgumentError: connect: port must be a whole number from 1 to 65535, got 1.5"),
            ('socket.connect("127.0.0.1", 80, s)', "TypeMismatch: connect: timeout must be a Number or none, got String"),
            ('socket.connect("127.0.0.1", 80, f)', "ArgumentError: connect: timeout must be a whole number of at least 0, got 1.5"),
            ('socket.connect("127.0.0.1", 80, neg)', "ArgumentError: connect: timeout must be a whole number of at least 0, got -1"),
        ]
        for expr, expected in cases:
            with self.subTest(expr=expr):
                self.assertEqual(error(expr), expected)

    def test_listen(self):
        cases = [
            ("socket.listen(s)", "TypeMismatch: listen: port must be a Number, got String"),
            ("socket.listen(neg)", "ArgumentError: listen: port must be a whole number from 0 to 65535, got -1"),
            ("socket.listen(65536)", "ArgumentError: listen: port must be a whole number from 0 to 65535, got 65536"),
            ("socket.listen(f)", "ArgumentError: listen: port must be a whole number from 0 to 65535, got 1.5"),
            ("socket.listen(0, nan)", "TypeMismatch: listen: host must be a String, got Vector"),
            ('socket.listen(0, "127.0.0.1", 0)', "ArgumentError: listen: backlog must be a whole number of at least 1, got 0"),
            ('socket.listen(0, "127.0.0.1", f)', "ArgumentError: listen: backlog must be a whole number of at least 1, got 1.5"),
            ('socket.listen(0, "127.0.0.1", s)', "TypeMismatch: listen: backlog must be a Number, got String"),
        ]
        for expr, expected in cases:
            with self.subTest(expr=expr):
                self.assertEqual(error(expr), expected)

    def test_listener_and_socket_methods(self):
        listener = 'socket.Listener { id: %s, host: "h", port: 1 }'
        cases = [
            (listener % "s" + ".accept()", "TypeMismatch: accept: expected a socket id, got String"),
            (listener % "1" + ".accept(s)", "TypeMismatch: accept: timeout must be a Number or none, got String"),
            (listener % "1" + ".accept(neg)", "ArgumentError: accept: timeout must be a whole number of at least 0, got -1"),
            (SOCK + ".recv(0)", "ArgumentError: recv: max must be a whole number of at least 1, got 0"),
            (SOCK + ".recv(f)", "ArgumentError: recv: max must be a whole number of at least 1, got 1.5"),
            (SOCK + ".recv(s)", "TypeMismatch: recv: max must be a Number, got String"),
            (SOCK + ".recv(10, s)", "TypeMismatch: recv: timeout must be a Number or none, got String"),
            (SOCK + ".recv(10, f)", "ArgumentError: recv: timeout must be a whole number of at least 0, got 1.5"),
            (SOCK + ".send(s)", "TypeMismatch: send: data must be Bytes, got String"),
            (SOCK.replace("id: 1", "id: s") + ".send(\"x\".to_bytes())", "TypeMismatch: send: expected a socket id, got String"),
            (SOCK.replace("id: 1", "id: s") + ".shutdown()", "TypeMismatch: shutdown: expected a socket id, got String"),
            (SOCK.replace("id: 1", "id: s") + ".close()", "TypeMismatch: close: expected a socket id, got String"),
        ]
        for expr, expected in cases:
            with self.subTest(expr=expr):
                self.assertEqual(error(expr), expected)

    def test_argument_errors_come_before_anything_else(self):
        # a bad argument is raised at once, even for an id that isn't open
        self.assertEqual(
            error(SOCK + ".recv(0, s)"),
            "ArgumentError: recv: max must be a whole number of at least 1, got 0",
        )


class RuntimeBehaviorTests(unittest.TestCase):
    def test_bytecode_minor(self):
        self.assertEqual(MINOR, 18)
        self.assertEqual(minor_of(SOCKET + 'print(socket.listen(0))'), 18)
        self.assertEqual(minor_of('print("a")'), 4)
        for name in (
            "connect", "listen", "accept", "send", "recv", "shutdown", "close",
        ):
            self.assertEqual(NATIVE_SINCE_MINOR["socket." + name], 18)
        self.assertEqual(
            {k: v for k, v in NATIVE_ARITIES.items() if k.startswith("socket.")},
            {
                "socket.connect": 3, "socket.listen": 3, "socket.accept": 2, "socket.send": 2,
                "socket.recv": 3, "socket.shutdown": 1, "socket.close": 1,
            },
        )

    def test_a_pending_accept_keeps_the_program_running_until_closed(self):
        src = SOCKET + (
            'import async from "std:async"\n'
            "let server = socket.listen(0)\n"
            "let waiting = detach server.accept()\n"
            'async.set_timeout(fn() { server.close() }, 150)\n'
            'print("waiting")\n'
            "print(try { waiting.await } catch { e: socket.SocketError => { e.kind } })\n"
        )
        self.assertEqual(run_source(src), "waiting\nclosed\n")

    def test_a_detached_accept_with_no_one_waiting_still_finishes_with_the_listener(self):
        # nothing awaits the promise: the program ends once the timer closes the listener
        src = SOCKET + (
            'import async from "std:async"\n'
            "let server = socket.listen(0)\n"
            "let ignored = detach try server.accept() else none\n"
            'async.set_timeout(fn() { print("closing"); server.close() }, 100)\n'
            'print("started")\n'
        )
        self.assertEqual(run_source(src), "started\nclosing\n")

    def test_the_vm_finishing_closes_open_sockets(self):
        # a program that leaves a listener open still ends
        self.assertEqual(run_source(SOCKET + "let s = socket.listen(0)\nprint(s.port > 0)\n"), "true\n")


class CheckerTests(unittest.TestCase):
    def test_types(self):
        diagnostics, types = check(
            SOCKET
            + 'let c = try socket.connect("127.0.0.1", 1) else none\n'
            + "let l = try socket.listen(0) else none\n"
            + "let port = try socket.listen(0).port else 0\n"
        )
        self.assertEqual(types["port"][-1], "Number")
        self.assertIn("Socket", types["c"][-1])
        self.assertIn("Listener", types["l"][-1])

    def test_connect_is_a_socket(self):
        diagnostics, types = check(
            SOCKET + 'let c = socket.connect("127.0.0.1", 1)\nlet data = c.recv()\nlet line = c.read_line()\n'
        )
        self.assertEqual(types["c"][-1], "Socket")
        self.assertEqual(types["data"][-1], "Bytes")
        self.assertEqual([d[1] for d in diagnostics if d[0] == "unhandled"], ["Unhandled error: SocketError"] * 3)

    def test_wrong_argument_types_are_reported(self):
        diagnostics, _ = check(SOCKET + 'let c = try socket.connect(80, "h") else none\n')
        self.assertEqual(len([d for d in diagnostics if d[0] == "mismatch"]), 2)


if __name__ == "__main__":
    unittest.main()
