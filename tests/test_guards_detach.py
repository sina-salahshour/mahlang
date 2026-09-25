"""Match guards (`pattern if cond => ...`) and `detach` on any expression.

Run with: python -m unittest tests.test_guards_detach -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mah.compiler.ast_nodes import Binary, MatchStmt
from tests.support import parse_source, run_source


class MatchGuardTests(unittest.TestCase):
    def test_guard_parses_onto_the_arm(self):
        program, parser = parse_source("match 1 { x if x > 0 => { 1 } _ => { 2 } }")
        self.assertEqual(parser.errors, [])
        match = program[0].value
        self.assertIsInstance(match, MatchStmt)
        self.assertIsInstance(match.arms[0].guard, Binary)
        self.assertIsNone(match.arms[1].guard)

    def test_guard_uses_bindings_and_falls_through(self):
        src = (
            "fn classify(n) {\n"
            "    match n {\n"
            "        x if x < 0 => { \"negative\" }\n"
            "        0 => { \"zero\" }\n"
            "        x if x % 2 == 0 => { \"even\" }\n"
            "        _ => { \"odd\" }\n"
            "    }\n"
            "}\n"
            "print(classify(-3), classify(0), classify(4), classify(7))\n"
        )
        self.assertEqual(run_source(src), "negative zero even odd\n")

    def test_guard_on_enum_pattern(self):
        src = (
            "enum Shape { Circle { r }, Square { side } }\n"
            "fn size(s) {\n"
            "    match s {\n"
            "        Shape.Circle { r } if r > 10 => { \"big\" }\n"
            "        Shape.Circle { r } => { \"small\" }\n"
            "        _ => { \"other\" }\n"
            "    }\n"
            "}\n"
            "print(size(Shape.Circle { r: 20 }), size(Shape.Circle { r: 2 }), size(Shape.Square { side: 1 }))\n"
        )
        self.assertEqual(run_source(src), "big small other\n")

    def test_guard_can_use_outer_variables_and_calls(self):
        src = (
            "let limit = 5\n"
            "fn over(x) { x > limit }\n"
            "print(match 7 { n if over(n) => { \"over\" } _ => { \"under\" } })\n"
        )
        self.assertEqual(run_source(src), "over\n")

    def test_guard_runs_only_after_the_pattern_matches(self):
        src = (
            "let calls = 0\n"
            "fn check() { calls = calls + 1; true }\n"
            "match 3 { 1 if check() => { } 3 if check() => { } _ => { } }\n"
            "print(calls)\n"
        )
        self.assertEqual(run_source(src), "1\n")

    def test_all_guards_failing_is_a_match_error(self):
        with self.assertRaisesRegex(Exception, r"No pattern in 'match' matched"):
            run_source("match 5 { x if x > 10 => { 1 } }")

    def test_guard_bindings_do_not_leak(self):
        with self.assertRaisesRegex(Exception, r"Undefined variable 'y'"):
            run_source("match 5 { x if y > 1 => { 1 } y => { 2 } }")


class DetachAnyExpressionTests(unittest.TestCase):
    def test_detach_block_gives_a_promise_of_its_tail(self):
        src = (
            "let name = \"Mah\"\n"
            "let x = detach {\n"
            "    sleep_async(5);\n"
            "    2 + 3\n"
            "};\n"
            "print(\"Hello, \" + name + \"! \" + x.await)\n"
        )
        self.assertEqual(run_source(src), "Hello, Mah! 5\n")

    def test_detach_for_loop_runs_concurrently(self):
        src = (
            "let total = 0\n"
            "let p = detach for let i in 0..5 { total = total + i; sleep_async(1); }\n"
            "print(\"before\", total)\n"
            "p.await\n"
            "print(\"after\", total)\n"
        )
        self.assertEqual(run_source(src), "before 0\nafter 10\n")

    def test_detach_while_loop_value(self):
        src = (
            "let n = 0\n"
            "let w = detach while n < 3 { n = n + 1; sleep_async(1); if n == 3 { break n * 10 } }\n"
            "print(w.await)\n"
        )
        self.assertEqual(run_source(src), "30\n")

    def test_detach_other_expressions(self):
        src = (
            "let v = [1, 2, 3]\n"
            "struct S { f }\n"
            "let s = S { f: 9 }\n"
            "print(detach (1 + 2).await)\n"
            "print(detach if true { \"yes\" } else { \"no\" }.await)\n"
            "print(detach match 2 { x if x > 1 => { \"big\" } _ => { \"small\" } }.await)\n"
            "print(detach v[1].await)\n"
            "print(detach s.f.await)\n"
            "print(detach 5.await)\n"
        )
        self.assertEqual(run_source(src), "3\nyes\nbig\n2\n9\n5\n")

    def test_detach_call_still_evaluates_arguments_eagerly(self):
        src = (
            "let x = 1\n"
            "fn show(v) { sleep_async(1); v }\n"
            "let p = detach show(x)\n"
            "x = 2\n"
            "print(p.await)\n"
        )
        self.assertEqual(run_source(src), "1\n")

    def test_detached_block_captures_by_reference(self):
        src = (
            "let x = 1\n"
            "let p = detach { sleep_async(1); x }\n"
            "x = 2\n"
            "print(p.await)\n"
        )
        self.assertEqual(run_source(src), "2\n")

    def test_bare_detached_block_statement(self):
        src = "detach { print(\"bg\") }\nprint(\"fg\")\n"
        self.assertEqual(run_source(src), "bg\nfg\n")

    def test_loops_inside_a_detached_block_can_break_and_continue(self):
        src = (
            "let p = detach {\n"
            "    let s = 0;\n"
            "    for let i in 0..5 { if i == 1 { continue } if i == 4 { break } s = s + i; }\n"
            "    s\n"
            "};\n"
            "print(p.await)\n"
        )
        self.assertEqual(run_source(src), "5\n")

    def test_nested_fn_inside_a_detached_block_can_return(self):
        src = "let p = detach { let f = fn() { return 4 }; f() }\nprint(p.await)\n"
        self.assertEqual(run_source(src), "4\n")

    def test_return_cannot_leave_a_detached_expression(self):
        with self.assertRaisesRegex(Exception, r"'return' can't leave a detached expression"):
            run_source("fn f() { let p = detach { return 1 }; p.await }\nprint(f())")

    def test_break_cannot_leave_a_detached_expression(self):
        with self.assertRaisesRegex(Exception, r"'break' can't leave a detached expression"):
            run_source("while true { let p = detach { break }; }")

    def test_continue_cannot_leave_a_detached_expression(self):
        with self.assertRaisesRegex(Exception, r"'continue' can't leave a detached expression"):
            run_source("for let i in 0..3 { detach { continue }; }")


if __name__ == "__main__":
    unittest.main()
