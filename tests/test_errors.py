"""End-to-end behavioral tests for M25 (typed, catchable errors: syntax +
runtime, both VMs -- see docs/ERRORS.md and docs/MAHC_FORMAT.md #4). Runs
through the normal `tests/support.py` harness, so `make test-rust` (which
sets `MAH_TEST_VM=rust`) runs every one of these on the Rust VM too.

M26 (error-set inference/checking) is out of scope here -- see
tests/test_typecheck.py for the handful of M25-related checker cases
(the ones this milestone's spec calls out: no diagnostics for a `try`/
`throw` used where a type is expected).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mah.runtime_values import MahRuntimeError
from tests.support import run_source, run_source_and_error


class BasicCatchTests(unittest.TestCase):
    def test_catch_by_type_binds_the_value(self):
        src = """struct Oops { code }
impl Error for Oops { fn message(self) { "oops " + self.code } }
let r = try { throw Oops { code: 7 }; 1 } catch { e: Oops => { e.code } }
print(r)
"""
        self.assertEqual(run_source(src), "7\n")

    def test_variant_arm_unmatched_rethrown_to_outer_try(self):
        src = """enum E { A, B }
impl Error for E {}
fn f(x) { if x == 1 { throw E.A } throw E.B }
fn g(x) { try { f(x) } catch { E.A => { "caught A" } } }
print(g(1))
print(try { g(2) } catch { E.B => { "outer got B" } })
"""
        self.assertEqual(run_source(src), "caught A\nouter got B\n")

    def test_runtime_error_variant_is_catchable(self):
        src = 'print(try { 1 / 0 } catch { RuntimeError.DivisionByZero { message } => { message } })\n'
        self.assertEqual(run_source(src), "Division by zero\n")


class TryElseTests(unittest.TestCase):
    def test_try_expr_else_on_a_runtime_error(self):
        self.assertEqual(run_source("print(try 1 / 0 else -1)\n"), "-1\n")

    def test_try_expr_else_on_success(self):
        self.assertEqual(run_source("print(try 5 else 0)\n"), "5\n")

    def test_try_block_else(self):
        self.assertEqual(run_source("print(try { 1 / 0 } else 9)\n"), "9\n")


class DeferInteractionTests(unittest.TestCase):
    def test_defer_runs_in_order_while_unwinding(self):
        src = """struct Oops { code }
impl Error for Oops {}
fn f() { defer print("f defer"); throw Oops { code: 1 } }
try { defer print("block defer"); f() } catch { _ => { print("handled") } }
print("after")
"""
        self.assertEqual(run_source(src), "f defer\nblock defer\nhandled\nafter\n")

    def test_error_thrown_while_draining_replaces_it_and_draining_continues(self):
        src = """enum E { A, B }
impl Error for E {}
fn f() { defer print("first registered"); defer throw E.B; throw E.A }
print(try { f() } catch { E.A => { "A" } E.B => { "B" } })
"""
        self.assertEqual(run_source(src), "first registered\nB\n")


class UncaughtErrorTests(unittest.TestCase):
    def test_uncaught_user_error_message_and_position(self):
        src = """struct Oops { code }
impl Error for Oops { fn message(self) { "bad " + self.code } }
throw Oops { code: 3 }
"""
        out, exc = run_source_and_error(src)
        self.assertEqual(out, "")
        self.assertIsInstance(exc, MahRuntimeError)
        self.assertEqual(str(exc), "Uncaught Oops: bad 3 at position #3:1")

    def test_default_message_is_to_string(self):
        src = """enum E { A }
impl Error for E {}
throw E.A
"""
        out, exc = run_source_and_error(src)
        self.assertEqual(out, "")
        self.assertIsInstance(exc, MahRuntimeError)
        self.assertEqual(str(exc), "Uncaught E: E.A at position #3:1")

    def test_unobserved_failed_detached_task_reported_at_program_end(self):
        # The column is the `throw` inside `work` -- verified against the
        # actual compiler output (see the spec's own note to double-check
        # this, not just copy it): `fn work() { throw E.A }`'s `throw`
        # starts right after `fn work() { ` (12 characters), i.e. column 13.
        src = """enum E { A }
impl Error for E {}
fn work() { throw E.A }
detach work()
print("main done")
"""
        out, exc = run_source_and_error(src)
        self.assertEqual(out, "main done\n")
        self.assertIsInstance(exc, MahRuntimeError)
        self.assertEqual(str(exc), "Uncaught E: E.A at position #3:13")


class ThrowNonErrorTests(unittest.TestCase):
    def test_throwing_a_number_is_uncaught_type_mismatch(self):
        out, exc = run_source_and_error("throw 5\n")
        self.assertEqual(out, "")
        self.assertIsInstance(exc, MahRuntimeError)
        self.assertEqual(
            str(exc),
            "Cannot throw a value of type 'Number': it does not implement Error at position #1:1",
        )

    def test_throwing_a_number_is_catchable_as_type_mismatch(self):
        src = "print(try { throw 5 } catch { RuntimeError.TypeMismatch { message } => { message } })\n"
        self.assertEqual(
            run_source(src),
            "Cannot throw a value of type 'Number': it does not implement Error\n",
        )


class RuntimeErrorCatchAllTests(unittest.TestCase):
    def test_message_method_on_a_runtime_error_via_catch_all(self):
        self.assertEqual(
            run_source("print(try { 1 % 0 } catch { e => { e.message() } })\n"),
            "Division by zero\n",
        )

    def test_type_test_on_runtime_error(self):
        self.assertEqual(
            run_source('print(try { "a" - 1 } catch { e: RuntimeError => { e.message } })\n'),
            "Cannot apply '-' to String and Number\n",
        )

    def test_match_failed(self):
        src = 'print(try { match 3 { 1 => { 0 } } } catch { RuntimeError.MatchFailed { message } => { message } })\n'
        self.assertEqual(run_source(src), "No pattern in 'match' matched the value\n")


class ScopingTests(unittest.TestCase):
    def test_closure_created_inside_try_body_is_not_covered_by_it(self):
        src = """enum E { A }
impl Error for E {}
let f = try { fn() { throw E.A } } catch { _ => { none } }
print(try { f(); "no" } catch { E.A => { "yes" } })
"""
        self.assertEqual(run_source(src), "yes\n")

    def test_break_out_of_try_inside_a_loop(self):
        src = """let i = 0
while true { try { i = i + 1; if i == 3 { break } } catch { _ => { } } }
print(i)
"""
        self.assertEqual(run_source(src), "3\n")

    def test_to_string_that_throws_is_catchable_at_the_print(self):
        src = """enum E { A }
impl Error for E {}
struct S {}
impl Printable for S { fn to_string(self) { throw E.A } }
print(try { print(S {}); "no" } catch { E.A => { "yes" } })
"""
        self.assertEqual(run_source(src), "yes\n")


class GuardTests(unittest.TestCase):
    def test_guards_in_catch_arms_and_elseless_fallthrough(self):
        src = """struct Code { n }
impl Error for Code {}
fn h(n) { try { throw Code { n: n } } catch { Code { n } if n > 5 => { "big" } Code { n } => { "small" } } }
print(h(9))
print(h(1))
"""
        self.assertEqual(run_source(src), "big\nsmall\n")


class ContextualKeywordTests(unittest.TestCase):
    def test_catch_throws_never_still_usable_as_identifiers(self):
        src = "let catch = 1\nlet throws = 2\nlet never = 3\nprint(catch + throws + never)\n"
        self.assertEqual(run_source(src), "6\n")


class AwaitAndDetachTests(unittest.TestCase):
    def test_await_rethrows_a_detached_tasks_error(self):
        src = """enum E { A }
impl Error for E {}
fn work() { sleep_async(1); throw E.A }
let p = detach work()
print("started")
print(try { p.await } catch { E.A => { "caught from task" } })
"""
        self.assertEqual(run_source(src), "started\ncaught from task\n")


class RuntimeErrorKindTests(unittest.TestCase):
    """M25 spec #4.5: a unit test in each VM's own test suite pins at
    least one message per RuntimeError variant -- here, one Mah-level
    catch per variant this milestone's classification table names (every
    site that's actually reachable from ordinary Mah code; `Internal` is
    exercised via the "cannot suspend" case elsewhere)."""

    def _variant(self, src: str) -> str:
        return run_source(src)

    def test_division_by_zero(self):
        self.assertEqual(
            self._variant('print(try { 1 / 0 } catch { RuntimeError.DivisionByZero { message } => { message } })\n'),
            "Division by zero\n",
        )

    def test_type_mismatch(self):
        self.assertEqual(
            self._variant(
                'print(try { 1 + true } catch { RuntimeError.TypeMismatch { message } => { message } })\n'
            ),
            "Cannot apply '+' to Number and Bool\n",
        )

    def test_no_such_field(self):
        src = """struct P { x }
print(try { P { x: 1 }.y } catch { RuntimeError.NoSuchField { message } => { message } })
"""
        self.assertEqual(self._variant(src), "'P' has no field 'y'\n")

    def test_no_such_method(self):
        src = """struct P { x }
print(try { P { x: 1 }.nope() } catch { RuntimeError.NoSuchMethod { message } => { message } })
"""
        self.assertEqual(self._variant(src), "'P' has no method 'nope'\n")

    def test_argument_error(self):
        src = """fn f(a, b) { a + b }
print(try { f(1) } catch { RuntimeError.ArgumentError { message } => { message } })
"""
        self.assertEqual(
            self._variant(src),
            "Argument Count is invalid. 'f' accepts 2 arguments but 1 was given\n",
        )

    def test_index_out_of_range(self):
        src = 'print(try { "a".char_at(5) } catch { RuntimeError.IndexOutOfRange { message } => { message } })\n'
        self.assertEqual(
            self._variant(src), "char_at index 5 is out of range for a String of length 1\n"
        )

    def test_match_failed_variant(self):
        src = 'print(try { match 3 { 1 => { 0 } } } catch { RuntimeError.MatchFailed { message } => { message } })\n'
        self.assertEqual(self._variant(src), "No pattern in 'match' matched the value\n")


class ExistingRuntimeErrorMessagesUnchangedTests(unittest.TestCase):
    """Spec #19: an UNCAUGHT runtime error still produces exactly today's
    text (no new location wording, no variant name leaking in) -- these
    mirror a couple of tests/test_language.py's own pre-M25 assertions."""

    def test_division_by_zero_uncaught(self):
        out, exc = run_source_and_error("print(1 / 0)\n")
        self.assertEqual(out, "")
        self.assertIsInstance(exc, MahRuntimeError)
        self.assertEqual(str(exc), "Division by zero at position #1:9")

    def test_no_such_method_uncaught(self):
        out, exc = run_source_and_error("struct P { x }\nP { x: 1 }.nope()\n")
        self.assertEqual(out, "")
        self.assertIsInstance(exc, MahRuntimeError)
        self.assertEqual(str(exc), "'P' has no method 'nope' at position #2:12")


if __name__ == "__main__":
    unittest.main()
