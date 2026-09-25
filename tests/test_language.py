"""End-to-end behavioral tests for language features implemented so far
(M0: real booleans, precedence; M1: heap frames, recursion, closures,
implicit `none`, runtime arity checks). See docs/TESTING.md.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support import run_source


class BooleanTests(unittest.TestCase):
    def test_literals_print_lowercase(self):
        self.assertEqual(run_source("print(true)\nprint(false)"), "true\nfalse\n")

    def test_comparisons_produce_real_booleans(self):
        self.assertEqual(run_source("print(1 < 2)\nprint(1 > 2)"), "true\nfalse\n")

    def test_bool_usable_directly_in_if(self):
        self.assertEqual(
            run_source('let a = true\nif a { print("yes") }'), "yes\n"
        )


class PrecedenceTests(unittest.TestCase):
    def test_unary_minus_binds_looser_than_pow(self):
        self.assertEqual(run_source("print(-2**2)").strip(), "-4")

    def test_pow_is_right_associative(self):
        self.assertEqual(run_source("print(2**3**2)").strip(), "512")

    def test_mul_before_add(self):
        self.assertEqual(run_source("print(2 * 3 + 1)").strip(), "7")

    def test_eq_compares_after_arithmetic(self):
        self.assertEqual(run_source("print(1 + 2 == 3)").strip(), "true")


class StringTests(unittest.TestCase):
    def test_concatenation_and_repetition(self):
        self.assertEqual(run_source('print("a" + "b")').strip(), "ab")
        self.assertEqual(run_source('print("ab" * 2)').strip(), "abab")


class RecursionTests(unittest.TestCase):
    def test_factorial(self):
        src = """
        fn fact(n) {
            if n == 0 { return 1 }
            return n * fact(n - 1)
        }
        print(fact(5))
        """
        self.assertEqual(run_source(src).strip(), "120")

    def test_fibonacci(self):
        src = """
        fn fib(n) {
            if n < 2 { return n }
            return fib(n - 1) + fib(n - 2)
        }
        print(fib(10))
        """
        self.assertEqual(run_source(src).strip(), "55")


class ClosureTests(unittest.TestCase):
    def test_captures_outer_variable_by_reference(self):
        src = """
        fn make_counter() {
            let count = 0
            fn increment() {
                count = count + 1
                return count
            }
            return increment
        }
        let counter = make_counter()
        print(counter())
        print(counter())
        print(counter())
        """
        self.assertEqual(run_source(src).split(), ["1", "2", "3"])

    def test_independent_closures_do_not_share_state(self):
        src = """
        fn make_counter() {
            let count = 0
            fn increment() { count = count + 1; return count }
            return increment
        }
        let c1 = make_counter()
        let c2 = make_counter()
        print(c1())
        print(c1())
        print(c2())
        print(c1())
        """
        self.assertEqual(run_source(src).split(), ["1", "2", "1", "3"])

    def test_anonymous_fn_assigned_and_called(self):
        self.assertEqual(
            run_source("let add = fn(a, b) { return a + b }\nprint(add(2, 3))").strip(),
            "5",
        )

    def test_deeply_nested_capture(self):
        src = """
        fn outer(x) {
            fn middle(y) {
                fn inner(z) { return x + y + z }
                return inner
            }
            return middle
        }
        let f = outer(1)
        let g = f(10)
        print(g(100))
        print(g(200))
        let h = f(20)
        print(h(100))
        print(g(300))
        """
        self.assertEqual(run_source(src).split(), ["111", "211", "121", "311"])

    def test_closure_returned_from_function_outlives_the_call(self):
        # The whole point of heap frames: `make_adder`'s frame must stay
        # alive after `make_adder` itself returns, since `add5` still
        # references it.
        src = """
        fn make_adder(x) {
            fn adder(y) { return x + y }
            return adder
        }
        let add5 = make_adder(5)
        print(add5(1))
        print(add5(2))
        """
        self.assertEqual(run_source(src).split(), ["6", "7"])


class NoneTests(unittest.TestCase):
    def test_implicit_return_is_none(self):
        self.assertEqual(run_source("fn nothing() { }\nprint(nothing())").strip(), "none")

    def test_none_is_falsy(self):
        src = """
        fn nothing() { }
        if nothing() {
            print("truthy")
        } else {
            print("falsy")
        }
        """
        self.assertEqual(run_source(src).strip(), "falsy")


class ErrorTests(unittest.TestCase):
    def test_undefined_variable_raises(self):
        with self.assertRaises(NameError):
            run_source("print(nope)")

    def test_duplicate_let_in_same_scope_shadows(self):
        # Intended change (M21): a same-scope `let` redeclaration used to be
        # an error. It now declares a new variable, Rust-style.
        self.assertEqual(run_source("let x = 1\nlet x = 2\nprint(x)"), "2\n")

    def test_duplicate_fn_in_same_scope_raises(self):
        with self.assertRaises(NameError):
            run_source("fn g() { 1 }\nfn g() { 2 }")

    def test_shadowing_in_nested_block_is_allowed(self):
        # A `let` in a nested block may reuse an outer name -- only
        # *same-scope* redeclaration is an error.
        src = """
        let x = 1
        if true {
            let x = 2
            print(x)
        }
        print(x)
        """
        self.assertEqual(run_source(src).split(), ["2", "1"])

    def test_arity_mismatch_raises_a_clean_error(self):
        src = "fn add(a, b) { return a + b }\nprint(add(1))"
        with self.assertRaises(Exception):
            run_source(src)

    def test_calling_a_non_function_raises(self):
        with self.assertRaises(Exception):
            run_source("let x = 5\nprint(x())")

    def test_break_outside_loop_raises(self):
        with self.assertRaises(Exception):
            run_source("break")

    def test_continue_outside_loop_raises(self):
        with self.assertRaises(Exception):
            run_source("continue")

    def test_break_inside_nested_fn_body_raises_cleanly(self):
        # Found while landing M9 (defer): a loop can never actually span a
        # function boundary in Mah, so `break`/`continue` written inside a
        # nested `fn`'s own body -- always legal syntax, even before M9 --
        # must raise the same clean "used outside a loop" error as a
        # top-level bare `break`, not silently target the ENCLOSING loop's
        # jump (which used to corrupt execution: the jump landed in the
        # outer loop's code with the inner closure's own frame still
        # current, never popped via `ret`).
        src = """
        let i = 0
        while i < 5 {
            let f = fn() { break }
            f()
            i = i + 1
        }
        print(i)
        """
        with self.assertRaises(Exception):
            run_source(src)

    def test_continue_inside_nested_fn_body_raises_cleanly(self):
        src = """
        let i = 0
        while i < 5 {
            let f = fn() { continue }
            f()
            i = i + 1
        }
        print(i)
        """
        with self.assertRaises(Exception):
            run_source(src)

    def test_return_outside_function_raises(self):
        with self.assertRaises(Exception):
            run_source("return 1")


if __name__ == "__main__":
    unittest.main()


class CallAnyExpressionTests(unittest.TestCase):
    """A call's `(` may follow any expression, not just a name or
    `.method`: `f(1)(2)`, `handlers[0](x)`, `(fn(x) { x })(5)`. Like an
    index's `[`, it must be on the same line as what it calls."""

    def test_call_the_result_of_a_call(self):
        src = "fn adder(a) { fn(b) { a + b } }\nprint(adder(1)(2))\nfn f() { fn() { fn() { 3 } } }\nprint(f()()())"
        self.assertEqual(run_source(src).split(), ["3", "3"])

    def test_call_an_indexed_function(self):
        self.assertEqual(run_source("let hs = [fn(x) { x * 2 }]\nprint(hs[0](21))").strip(), "42")

    def test_call_a_parenthesized_or_inline_function(self):
        self.assertEqual(
            run_source("print((fn(x) { x + 1 })(5))\nprint(fn(x) { x + 1 }(5))").split(), ["6", "6"]
        )

    def test_keyword_arguments_and_detach(self):
        src = (
            "fn f(a) { fn(b, c = 0) { a + b + c } }\n"
            "print(f(1)(2, c: 3))\n"
            "let p = detach f(1)(2)\n"
            "print(p.await)"
        )
        self.assertEqual(run_source(src).split(), ["6", "3"])

    def test_paren_on_the_next_line_is_not_a_call(self):
        # `(g)()` on its own line is a new statement, not a call of the
        # previous line's value.
        self.assertEqual(run_source("fn g() { 7 }\nprint(g())\n(g)()").strip(), "7")
        with self.assertRaises(SyntaxError):
            run_source("fn f(x) { x }\nprint(f(1)\n(2))")

    def test_calling_a_non_function_is_a_runtime_error(self):
        with self.assertRaises(Exception):
            run_source("fn f() { 5 }\nprint(f()(1))")
