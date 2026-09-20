"""End-to-end behavioral tests for M5: `if`/`match`/bare `{ }` blocks as
expressions, and the implicit-return-via-tail this gives function bodies --
see docs/TESTING.md and docs/V2_DESIGN.md's M5 milestone.

These are the scenarios manually verified while landing M5; see that
milestone's write-up for the exact parsing algorithm (`_parse_block_items`)
and codegen unification (`_gen_if_into`/`_gen_match_into`/`_gen_block_into`)
these pin down. `tests/test_parser.py`'s `ExprBlockParsingTests` covers the
same feature at the parser/AST-shape layer.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support import run_source


class BareBlockTests(unittest.TestCase):
    def test_bare_block_tail_is_its_value(self):
        src = """
        let x = { let y = 5; y + 1 }
        print(x)
        """
        self.assertEqual(run_source(src), "6\n")

    def test_empty_block_is_none(self):
        src = """
        let x = {}
        print(x)
        """
        self.assertEqual(run_source(src), "none\n")

    def test_trailing_semicolon_forces_discard_not_tail(self):
        src = """
        let x = { let y = 1; y + 1; }
        print(x)
        """
        self.assertEqual(run_source(src), "none\n")


class IfExpressionTests(unittest.TestCase):
    def test_if_expression_both_branches(self):
        src = """
        let a = if true { 1 } else { 2 }
        let b = if false { 1 } else { 2 }
        print(a)
        print(b)
        """
        self.assertEqual(run_source(src), "1\n2\n")

    def test_if_expression_no_else_false_condition_is_none(self):
        src = """
        let x = if false { 1 }
        print(x)
        """
        self.assertEqual(run_source(src), "none\n")


class MatchExpressionTests(unittest.TestCase):
    def test_match_expression(self):
        src = """
        let x = match 5 {
            5 => { "five" }
            _ => { "other" }
        }
        print(x)
        """
        self.assertEqual(run_source(src), "five\n")


class ImplicitReturnTests(unittest.TestCase):
    def test_plain_tail_expression_is_the_return_value(self):
        src = """
        fn add(a, b) { a + b }
        print(add(2, 3))
        """
        self.assertEqual(run_source(src), "5\n")

    def test_if_expression_tail_is_the_return_value(self):
        src = """
        fn abs(n) {
            if n < 0 { -n } else { n }
        }
        print(abs(-5))
        print(abs(5))
        """
        self.assertEqual(run_source(src), "5\n5\n")

    def test_recursion_with_implicit_return_no_explicit_return_anywhere(self):
        src = """
        fn fact(n) {
            if n == 0 { 1 } else { n * fact(n - 1) }
        }
        print(fact(5))
        """
        self.assertEqual(run_source(src), "120\n")


class RegressionTests(unittest.TestCase):
    """The exact pre-M5 statement styles that must keep working byte-for-
    byte -- see docs/V2_DESIGN.md's M5 milestone for why this is safe."""

    def test_block_shaped_statement_not_last_needs_no_semicolon(self):
        src = """
        if true { print("a") }
        print("b")
        """
        self.assertEqual(run_source(src), "a\nb\n")

    def test_block_shaped_statement_then_explicit_return_in_fn_body(self):
        src = """
        fn f() {
            if true { print("x") }
            return 1
        }
        print(f())
        """
        self.assertEqual(run_source(src), "x\n1\n")


class DeepCompositionTests(unittest.TestCase):
    def test_match_arm_tail_is_an_if_expression(self):
        src = """
        fn classify(n) {
            match n {
                0 => { "zero" }
                _ => {
                    if n < 0 { "negative" } else { "positive" }
                }
            }
        }
        print(classify(0))
        print(classify(-5))
        print(classify(5))
        """
        self.assertEqual(run_source(src), "zero\nnegative\npositive\n")


class WideningTests(unittest.TestCase):
    """M5 deliberately widens accepted syntax in two ways -- see
    docs/V2_DESIGN.md's M5 milestone. Neither breaks any pre-existing
    program; both simply fall out of unifying statement-parsing into the
    general expression grammar."""

    def test_bare_expression_statement_with_explicit_semicolon_is_allowed(self):
        src = """
        5 + 3;
        print("still works")
        """
        self.assertEqual(run_source(src), "still works\n")

    def test_bare_call_statement_without_semicolon_still_needs_no_semicolon(self):
        # Pre-M5, a bare call statement (`foo(1)`) was already free-standing
        # -- never needed a trailing `;`, via parse_stmt's old dedicated
        # ID-led-call branch. M5 routes it through the general expression
        # path instead, but preserves that same freedom (see
        # _parse_block_items's Call/FnExpr exemption) -- this is exactly
        # the style examples/match.mh's describe_number(0)/(1)/(42)
        # sequence relies on.
        src = """
        fn greet(name) {
            print(name)
        }
        greet("a")
        greet("b")
        """
        self.assertEqual(run_source(src), "a\nb\n")


if __name__ == "__main__":
    unittest.main()
