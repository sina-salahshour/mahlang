"""M33 (docs/STDLIB.md "input", docs/MAHC_FORMAT.md #4.4/#6.4): `input`
is an ordinary (non-keyword) built-in that prints a prompt and reads one
line as a String, throwing `EndOfInput` at the end of the input. It's
asynchronous underneath: a bare call waits for the line, while `detach
input()` hands back a Promise so timers and other tasks keep running.

Everything here runs on whichever VM `MAH_TEST_VM` selects, except the
tests that start both VMs themselves.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mah import rust_vm  # noqa: E402
from mah.bytecode.decode import decode  # noqa: E402
from mah.lsp import analysis  # noqa: E402
from mah.runtime_values import MahRuntimeError  # noqa: E402
from tests.support import compile_bytes, run_source, run_source_and_error  # noqa: E402
from tests.test_typecheck import check  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class InputTests(unittest.TestCase):
    def test_reads_a_line_as_a_string(self):
        self.assertEqual(run_source('let s = input()\nprint(s + "!", s.len())', stdin="hi there\n"), "hi there! 8\n")

    def test_the_prompt_is_printed_without_a_newline(self):
        self.assertEqual(run_source('let n = input("n? ")\nprint("got " + n)', stdin="5\n"), "n? got 5\n")

    def test_line_endings_are_dropped(self):
        src = 'print(input().len(), input().len(), input().len())'
        self.assertEqual(run_source(src, stdin="ab\r\ncd\nlast"), "2 2 4\n")
        # a lone \r stays
        self.assertEqual(run_source("print(input().len())", stdin="a\rb\n"), "3\n")

    def test_lines_come_in_order(self):
        src = "let a = input()\nlet b = input()\nprint(b, a)"
        self.assertEqual(run_source(src, stdin="first\nsecond\n"), "second first\n")

    def test_to_number(self):
        self.assertEqual(run_source('print(input("x: ").to_number() * 2)', stdin=" 21 \n"), "x: 42\n")

    def test_end_of_input_can_be_caught(self):
        src = 'let s = try { input() } catch { e: EndOfInput => { "(" + e.message() + ")" } }\nprint(s)'
        self.assertEqual(run_source(src, stdin=""), "(end of input)\n")
        self.assertEqual(run_source('print(try input() else "default")', stdin=""), "default\n")

    def test_uncaught_end_of_input(self):
        out, exc = run_source_and_error('print("before")\nlet s = input()', stdin="")
        self.assertEqual(out, "before\n")
        self.assertIsInstance(exc, MahRuntimeError)
        self.assertEqual(str(exc), "Uncaught EndOfInput: end of input at position #2:9")

    def test_detach_gives_a_promise(self):
        src = 'let p = detach input("? ")\nprint("asked")\nlet line = p.await\nprint("got " + line)'
        self.assertEqual(run_source(src, stdin="yes\n"), "? asked\ngot yes\n")
        src = 'let p = detach input()\nprint(try p.await else "none left")'
        self.assertEqual(run_source(src, stdin=""), "none left\n")

    def test_timers_run_while_waiting(self):
        src = (
            "let p = detach input()\n"
            "sleep_async(30)\n"
            'print("tick")\n'
            'print("line: " + p.await)\n'
        )
        self.assertEqual(run_source(src, stdin="x\n"), "tick\nline: x\n")

    def test_a_binding_named_input_wins(self):
        self.assertEqual(run_source('fn input(p) { "mine " + p }\nprint(input("a"))'), "mine a\n")
        self.assertEqual(run_source('let input = 3\nprint(input)'), "3\n")

    def test_arguments(self):
        for src, message in [
            ('input("a", "b")', "'input' takes at most one argument (the prompt)"),
            ('input(prompt: "a")', "'input' doesn't take keyword arguments"),
        ]:
            with self.subTest(src=src):
                with self.assertRaises(SyntaxError) as cm:
                    compile_bytes(text=src)
                self.assertIn(message, str(cm.exception))
        out, exc = run_source_and_error("let u: Unknown = 5\ninput(u)", stdin="x\n")
        self.assertEqual(str(exc), "input: the prompt must be a String, got Number at position #2:1")

    def test_bytecode_minor(self):
        self.assertEqual(decode(compile_bytes(text="print(input())")).minor, 10)
        self.assertEqual(decode(compile_bytes(text="print(1)")).minor, 4)

    def test_the_old_native_is_still_there_for_older_files(self):
        import io

        from mah.natives import NATIVES, NativeContext

        ctx = NativeContext(to_string=str, schedule_timer=None)
        old_stdin = sys.stdin
        sys.stdin = io.StringIO("abc 42 7\n")
        try:
            self.assertEqual(NATIVES["io.input"][1](ctx, []), 42)
        finally:
            sys.stdin = old_stdin


class InputCheckerTests(unittest.TestCase):
    def test_signature(self):
        diagnostics, types = check('let s = try input("x") else ""\nlet p = detach input()')
        self.assertEqual(types["s"][-1], "String")
        self.assertEqual(types["p"][-1], "Promise<String>")
        self.assertEqual([d for d in diagnostics if d[0] not in ("implicit",)], [])

    def test_it_throws_end_of_input(self):
        diagnostics, _ = check("let s = input()")
        self.assertIn(("unhandled", "Unhandled error: EndOfInput", 1), [d for d in diagnostics if d[0] == "unhandled"])
        # detached, it's the `.await` that throws
        diagnostics, _ = check("let p = detach input()\nlet s = p.await")
        self.assertIn(("unhandled", "Unhandled error: EndOfInput", 2), [d for d in diagnostics if d[0] == "unhandled"])

    def test_the_prompt_must_be_a_string(self):
        diagnostics, _ = check("let s = try input(5) else \"\"")
        self.assertIn(
            ("mismatch", "Type mismatch in the prompt: expected String, found Number", 1),
            [d for d in diagnostics if d[0] == "mismatch"],
        )

    def test_hover(self):
        src = 'let s = input("x")\n'
        hover = analysis.get_hover(src, 0, 9)
        self.assertIsNotNone(hover)
        self.assertIn("**builtin** `input`", hover["contents"]["value"])
        self.assertIn("EndOfInput", hover["contents"]["value"])


class DelayedStdinTests(unittest.TestCase):
    """Both VMs, with a line that arrives only after a while: the timers
    keep firing meanwhile, and a detached, never-awaited `input` keeps the
    program alive until its line comes."""

    SRC = (
        'let answer = detach input("number: ")\n'
        "for let i in 0..3 {\n"
        "    sleep_async(100)\n"
        '    print("tick", i)\n'
        "}\n"
        'print("got", try answer.await.to_number() else 0)\n'
    )

    def _run(self, vm: str, src: str, delay: float, line: str):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "prog.mh")
            with open(path, "w") as f:
                f.write(src)
            proc = subprocess.Popen(
                [sys.executable, "-m", "mah", "run", "--vm", vm, path],
                cwd=REPO_ROOT,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            time.sleep(delay)
            still_running = proc.poll() is None
            out, err = proc.communicate(line, timeout=30)
            return proc.returncode, out, err, still_running

    def _vms(self):
        vms = ["python"]
        try:
            rust_vm.find_vm()
            vms.append("rust")
        except rust_vm.RustVmNotFound:
            pass
        return vms

    def test_timers_tick_while_input_waits(self):
        for vm in self._vms():
            with self.subTest(vm=vm):
                code, out, err, _ = self._run(vm, self.SRC, 0.8, "42\n")
                self.assertEqual((code, out, err), (0, "number: tick 0\ntick 1\ntick 2\ngot 42\n", ""))

    def test_a_pending_input_keeps_the_program_alive(self):
        src = 'let p = detach input()\nprint("main done")\n'
        for vm in self._vms():
            with self.subTest(vm=vm):
                code, out, err, still_running = self._run(vm, src, 1.0, "late\n")
                self.assertTrue(still_running, "the program ended before its input arrived")
                self.assertEqual((code, out, err), (0, "main done\n", ""))


if __name__ == "__main__":
    unittest.main()
