"""M28 (docs/MAH_TEST.md): `test` blocks, `.test.mh` files, `std:test`, the
TESTS bytecode section, the per-test VM entry point, and the `mah test`
runner.

Outcomes go through `tests.support.run_test_case`, so `make test-rust`
checks the Rust VM's `mah-vm test` entry point too.
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mah.bytecode.decode import decode  # noqa: E402
from mah.bytecode.disasm import disassemble  # noqa: E402
from mah.bytecode.format import MahcFormatError  # noqa: E402
from mah.cli.main import main  # noqa: E402
from mah.code_interpreter import run_test_bytes  # noqa: E402
from mah.compiler.driver import compile_to_bytes  # noqa: E402
from mah.compiler.lexer import Lexer  # noqa: E402
from mah.compiler.parser import Parser  # noqa: E402
from mah.format.formatter import format_source  # noqa: E402
from mah.lsp import analysis  # noqa: E402
from mah.preprocessor import preprocess  # noqa: E402
from mah.test_outcome import TestOutcome, format_outcome, parse_outcome  # noqa: E402
from tests.support import run_source, run_test_case  # noqa: E402
from tests.test_typecheck import check  # noqa: E402

STD = 'import "std:test"\n'


def _write(root: str, rel: str, text: str) -> str:
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def _mah(argv: list, cwd: str) -> tuple:
    """Run the `mah` CLI in-process in `cwd`: `(exit code, stdout, stderr)`."""
    out, err = io.StringIO(), io.StringIO()
    old = os.getcwd()
    os.chdir(cwd)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = main(argv)
            except SystemExit as e:  # the CLI's compile-error path exits directly
                code = e.code
    finally:
        os.chdir(old)
    return code, out.getvalue(), err.getvalue()


class SyntaxTests(unittest.TestCase):
    def parse(self, src: str, allow_tests: bool = True):
        parser = Parser(Lexer(src), allow_tests=allow_tests)
        program = parser.parse_program()
        return program, parser.errors

    def test_a_test_block_parses_in_a_test_file(self):
        program, errors = self.parse('test "adds" { 1 }')
        self.assertEqual(errors, [])
        self.assertEqual(type(program[0]).__name__, "TestDecl")
        self.assertEqual(program[0].name, "adds")

    def test_test_is_still_an_ordinary_name(self):
        _program, errors = self.parse("let test = 1\ntest = 2\nfn f(test) { test(1) }")
        self.assertEqual(errors, [])
        self.assertEqual(run_source("let test = 1\nprint(test + 1)"), "2\n")

    def test_a_test_block_outside_a_test_file(self):
        _program, errors = self.parse('test "x" { 1 }', allow_tests=False)
        self.assertIn("'test' blocks are only allowed in *.test.mh files", errors[0][0])

    def test_test_blocks_only_at_the_top_level(self):
        _program, errors = self.parse('fn f() {\ntest "x" { 1 }\n}')
        self.assertTrue(errors)

    def test_duplicate_names(self):
        with self.assertRaises(SyntaxError) as cm:
            compile_to_bytes(text='test "a" { 1 }\ntest "a" { 2 }', test=True)
        self.assertIn("Two tests are named 'a'", str(cm.exception))

    def test_only_declarations_at_the_top_level(self):
        with self.assertRaises(SyntaxError) as cm:
            compile_to_bytes(text='fn f() { 1 }\nprint(f())\ntest "a" { 1 }', test=True)
        self.assertIn("A test file may only contain declarations and 'test' blocks", str(cm.exception))
        self.assertIn("#2:1", str(cm.exception))
        # `let`s, functions, types and impls are all fine.
        compile_to_bytes(
            text='let k = 2\nstruct P { x }\nenum E { A }\ntrait T { fn t(self) }\nimpl T for P { fn t(self) { 1 } }\n'
            'fn f() { k }\ntest "a" { f() }',
            test=True,
        )

    def test_formatter_round_trip(self):
        src = 'import "std:test"\n\ntest "adds" {\n    assert_eq(1 + 1, 2)\n}\n'
        self.assertEqual(format_source(src), src)
        self.assertEqual(format_source('test   "x"   {\nassert( true )\n}\n'), 'test "x" {\n    assert(true)\n}\n')


class FileRulesTests(unittest.TestCase):
    def test_run_and_build_refuse_a_test_file(self):
        with tempfile.TemporaryDirectory() as td:
            path = _write(td, "a.test.mh", 'test "x" { 1 }\n')
            with self.assertRaises(SyntaxError) as cm:
                compile_to_bytes(path=path)
            self.assertIn("'a.test.mh' is a test file; run it with `mah test`", str(cm.exception))
            # (the CLI re-raises a compile error with no position for its
            # launcher to print, as for every other such error)
            for command in ("run", "build"):
                with self.assertRaises(SyntaxError):
                    _mah([command, path], td)

    def test_nothing_can_import_a_test_file(self):
        with tempfile.TemporaryDirectory() as td:
            _write(td, "a.test.mh", 'test "x" { 1 }\n')
            pp = preprocess(os.path.join(td, "main.mh"), 'import "a.test.mh"\n')
            self.assertIn("can't import the test file 'a.test.mh'", pp.errors[0][0])

    def test_test_blocks_must_be_in_the_test_file_itself(self):
        with tempfile.TemporaryDirectory() as td:
            _write(td, "lib.mh", "export fn f() { 1 }\n")
            path = _write(td, "lib.test.mh", 'import "lib.mh"\ntest "x" { f() }\n')
            self.assertEqual(len(decode(compile_to_bytes(path=path, test=True)).tests), 1)

    def test_a_sibling_test_file_sees_private_names(self):
        with tempfile.TemporaryDirectory() as td:
            _write(td, "calc.mh", "export fn add(a, b) { a + b }\nfn secret() { 42 }\n")
            ok = _write(td, "calc.test.mh", 'import calc from "./calc"\nimport "calc.mh"\ntest "x" { calc.secret() + secret() }\n')
            compile_to_bytes(path=ok, test=True)
            other = _write(td, "other.test.mh", 'import calc from "./calc"\ntest "x" { calc.secret() }\n')
            with self.assertRaises(Exception) as cm:
                compile_to_bytes(path=other, test=True)
            self.assertIn("secret", str(cm.exception))
            # A normal importer still sees only exports.
            main = _write(td, "main.mh", 'import calc from "./calc"\nprint(calc.secret())\n')
            with self.assertRaises(Exception):
                compile_to_bytes(path=main)


class BytecodeTests(unittest.TestCase):
    def test_the_tests_table(self):
        data = compile_to_bytes(text='fn f() { 1 }\n\ntest "one" { f() }\ntest "two" { 2 }', test=True)
        program = decode(data)
        self.assertEqual([program.strings[t.name] for t in program.tests], ["one", "two"])
        self.assertEqual([t.line for t in program.tests], [3, 4])
        self.assertIn("TESTS:\n  'one' slot=", disassemble(program))
        self.assertIn("SEC_TESTS", open(os.path.join(os.path.dirname(__file__), "..", "mah", "bytecode", "format.py")).read())

    def test_normal_builds_have_no_tests_table(self):
        self.assertEqual(decode(compile_to_bytes(text="print(1)")).tests, [])

    def test_a_bad_index(self):
        data = compile_to_bytes(text='test "one" { 1 }', test=True)
        with self.assertRaises(MahcFormatError):
            run_test_bytes(data, 5)

    def test_outcome_text_round_trip(self):
        outcome = TestOutcome("failed", "line one\nline two", [("std:test", 50), (None, 7)], True)
        self.assertEqual(parse_outcome(format_outcome(outcome)), outcome)
        self.assertIsNone(parse_outcome("RuntimeError: nope"))


class OutcomeTests(unittest.TestCase):
    """Each test runs in a fresh VM, on the VM MAH_TEST_VM selects."""

    SRC = STD + """
fn helper(x) {
    assert_eq(x, 3)
}

let counter = [0]

test "passes" {
    assert_eq(1 + 2, 3)
    counter.push(1)
    assert_eq(counter.len(), 2)
}

test "fails" {
    print("some output")
    assert_eq(2 + 2, 5, "math")
}

test "fails in a helper" {
    helper(4)
}

test "skipped" {
    skip("later")
}

test "runtime error" {
    let x = 1 / 0
}

test "async" {
    sleep_async(5)
    assert(true)
}

test "leftover timers" {
    let p = detach sleep_async(50)
}

test "isolated" {
    # the "passes" test pushed to `counter`, in its own VM
    assert_eq(counter.len(), 1)
}
"""

    def run_test(self, index: int):
        return run_test_case(self.SRC, index)

    def test_ok(self):
        out, outcome = self.run_test(0)
        self.assertEqual((out, outcome), ("", TestOutcome("ok")))

    def test_failed_assertion_is_located_at_the_assertion(self):
        out, outcome = self.run_test(1)
        self.assertEqual(out, "some output\n")
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.message, "assert_eq failed: math\n  actual:   4\n  expected: 5")
        self.assertEqual(outcome.frames[0][0], "std:test")
        self.assertEqual(outcome.frames[1:], [(None, 17)])

    def test_frames_go_through_helpers(self):
        _out, outcome = self.run_test(2)
        self.assertEqual(outcome.frames[1:], [(None, 4), (None, 21)])

    def test_skip(self):
        self.assertEqual(self.run_test(3)[1], TestOutcome("skipped", "later"))

    def test_runtime_error(self):
        _out, outcome = self.run_test(4)
        self.assertEqual((outcome.status, outcome.message, outcome.frames), ("failed", "Division by zero", [(None, 29)]))

    def test_async_test(self):
        self.assertEqual(self.run_test(5)[1], TestOutcome("ok"))

    def test_leftover_timers_are_dropped_with_a_warning(self):
        self.assertEqual(self.run_test(6)[1], TestOutcome("ok", leftover=True))

    def test_each_test_starts_from_a_fresh_vm(self):
        self.assertEqual(self.run_test(7)[1], TestOutcome("ok"))

    def test_a_failing_top_level_let_fails_the_test(self):
        _out, outcome = run_test_case('let x = 1 / 0\ntest "t" { 1 }', 0)
        self.assertEqual(outcome.status, "failed")
        self.assertIn("Division by zero", outcome.message)

    def test_timeout(self):
        data = compile_to_bytes(text='test "loop" { while true { } }', test=True)
        outcome = run_test_bytes(data, 0, timeout=0.2)
        self.assertEqual(outcome.status, "timeout")


class StdTestTests(unittest.TestCase):
    def test_assertions(self):
        src = STD + (
            'print(try { assert(false, "why") } catch { e: AssertionError => { e.message } })\n'
            'print(try { assert_eq("1", 1) } catch { e: AssertionError => { e.message } })\n'
            "print(try { assert_ne(2, 2) } catch { e: AssertionError => { e.message } })\n"
            "print(try { assert_throws(fn() { 1 }) } catch { e: AssertionError => { e.message } })\n"
            'print(try { fail("stop") } catch { e: AssertionError => { e.message } })\n'
            'print(try { skip("later") } catch { e: SkipTest => { e.reason } })\n'
            "print(assert_throws(fn() { 1 / 0 }).message)\n"
            "assert(1)\nassert_eq(2, 2)\nassert_ne(1, 2)\n"
        )
        self.assertEqual(
            run_source(src),
            "assert failed: why\n"
            'assert_eq failed\n  actual:   "1"\n  expected: 1\n'
            "assert_ne failed\n  both: 2\n"
            "assert_throws failed: the function returned normally\n"
            "failed: stop\n"
            "later\n"
            "Division by zero\n",
        )

    def test_std_test_is_clean_at_explicit(self):
        diagnostics, _ = check(STD)
        self.assertEqual(diagnostics, [])

    def test_a_test_body_s_errors_are_not_unhandled(self):
        # The runner catches whatever a test throws; the checker agrees.
        from mah.compiler import typecheck
        from mah.compiler.driver import _parse_and_resolve

        _pp, program, resolver = _parse_and_resolve(path=None, text=STD + 'test "t" { assert(false) }', test=True)
        self.assertEqual(
            [d.message for d in typecheck.check_program(program, resolver) if d.kind == "unhandled"], []
        )


class CliTests(unittest.TestCase):
    def project(self, td: str) -> str:
        _write(td, "mah-project.toml", '[package]\nname = "p"\nversion = "0.1.0"\n')
        _write(td, "src/main.mh", "export fn add(a, b) { a + b }\nfn secret() { 7 }\n")
        _write(
            td,
            "src/main.test.mh",
            STD + 'import main from "./main"\n\ntest "adds" {\n    assert_eq(main.add(2, 3), 5)\n}\n\n'
            'test "secret" {\n    assert_eq(main.secret(), 8)\n}\n\ntest "later" {\n    skip("soon")\n}\n',
        )
        _write(td, "build/ignored.test.mh", 'test "never" { 1 }\n')
        _write(td, ".hidden/ignored.test.mh", 'test "never" { 1 }\n')
        return td

    def vm_args(self) -> list:
        return ["--vm", "rust"] if os.environ.get("MAH_TEST_VM") == "rust" else ["--vm", "python"]

    def test_a_run_with_a_failure(self):
        with tempfile.TemporaryDirectory() as td:
            code, out, _err = _mah(["test", *self.vm_args()], self.project(td))
            self.assertEqual(code, 1)
            self.assertIn("running 3 tests\n", out)
            self.assertIn("test src/main.test.mh::adds ... ok\n", out)
            self.assertIn("test src/main.test.mh::secret ... FAILED\n", out)
            self.assertIn("test src/main.test.mh::later ... skipped (soon)\n", out)
            self.assertIn(
                "---- src/main.test.mh::secret ----\nassert_eq failed at src/main.test.mh:9\n"
                "  actual:   7\n  expected: 8\n",
                out,
            )
            self.assertIn("test result: FAILED. 1 passed; 1 failed; 1 skipped; finished in", out)
            self.assertNotIn("never", out)

    def test_a_filter(self):
        with tempfile.TemporaryDirectory() as td:
            code, out, _err = _mah(["test", "add", *self.vm_args()], self.project(td))
            self.assertEqual(code, 0)
            self.assertIn("running 1 test\n", out)
            self.assertIn("test result: ok. 1 passed; 0 failed; 0 skipped", out)
            code, out, _err = _mah(["test", "main.test.mh::later", *self.vm_args()], td)
            self.assertIn("running 1 test\n", out)

    def test_one_file(self):
        with tempfile.TemporaryDirectory() as td:
            path = _write(td, "x.test.mh", STD + 'test "t" { assert(true) }\n')
            code, out, _err = _mah(["test", "--file", path, *self.vm_args()], td)
            self.assertEqual(code, 0)
            self.assertIn("test x.test.mh::t ... ok", out)

    def test_a_file_that_does_not_compile(self):
        with tempfile.TemporaryDirectory() as td:
            self.project(td)
            _write(td, "src/bad.test.mh", 'print(1)\ntest "t" { 1 }\n')
            code, out, err = _mah(["test", *self.vm_args()], td)
            self.assertEqual(code, 1)
            self.assertIn("error: src/bad.test.mh doesn't compile", err)
            self.assertIn("1 file(s) didn't compile", out)

    def test_the_example(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples", "testing", "shapes.test.mh")
        code, out, _err = _mah(["test", "--file", path, *self.vm_args()], os.path.dirname(path))
        self.assertEqual(code, 0, out)
        self.assertIn("test result: ok. 4 passed; 0 failed; 1 skipped", out)

    def test_compile_errors_are_located(self):
        with tempfile.TemporaryDirectory() as td:
            path = _write(td, "x.test.mh", 'test "t" { nope() }\n')
            _code, _out, err = _mah(["test", "--file", path], td)
            self.assertIn("Undefined variable 'nope' at position x.test.mh#1:12", err)

    def test_no_project(self):
        with tempfile.TemporaryDirectory() as td:
            code, _out, err = _mah(["test"], td)
            self.assertEqual(code, 2)
            self.assertIn("no mah-project.toml found", err)

    def test_timeout_flag(self):
        with tempfile.TemporaryDirectory() as td:
            path = _write(td, "x.test.mh", 'test "spin" { while true { } }\n')
            code, out, _err = _mah(["test", "--file", path, "--timeout", "200", *self.vm_args()], td)
            self.assertEqual(code, 1)
            self.assertIn("timed out: took longer than 0.2s", out)

    def test_other_errors_get_a_stack_trace(self):
        with tempfile.TemporaryDirectory() as td:
            path = _write(td, "x.test.mh", 'fn boom() {\n    1 / 0\n}\n\ntest "t" {\n    boom()\n}\n')
            _code, out, _err = _mah(["test", "--file", path, *self.vm_args()], td)
            self.assertIn("Division by zero at x.test.mh:2\nstack trace:\n  at x.test.mh:2\n  at x.test.mh:6\n", out)


class LspTests(unittest.TestCase):
    SRC = STD + 'let k = 1\n\ntest "adds" {\n    assert_eq(k, 1)\n}\n'

    def test_document_symbols_list_tests(self):
        symbols = analysis.get_document_symbols(self.SRC)
        self.assertIn(("adds", "test"), [(s["name"], s["detail"]) for s in symbols])
        self.assertIn(("k", "let k"), [(s["name"], s["detail"]) for s in symbols])

    def test_hover_on_test(self):
        hover = analysis.get_hover(self.SRC, 3, 1)
        self.assertIn("**keyword** `test`", hover["contents"]["value"])

    def test_diagnostics_in_a_test_file(self):
        self.assertEqual(analysis.get_diagnostics(self.SRC, "/tmp/x.test.mh"), [])
        self.assertTrue(analysis.get_diagnostics(self.SRC, "/tmp/x.mh"))


if __name__ == "__main__":
    unittest.main()
