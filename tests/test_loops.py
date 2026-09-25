"""End-to-end tests for `for` loops (`for let v[, let i] in xs { ... }`),
`break value`, and loops as expressions (`let x = while true { break 10 }`).

Uses `run_source`/`parse_source` like the other behavioral test files.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mah.compiler.ast_nodes import BreakStmt, ExprStmt, ForStmt, LetStmt, WhileStmt
from mah.preprocessor import preprocess
from tests.support import parse_source, run_source


def parse(source: str):
    program, parser = parse_source(source)
    if parser.errors:
        raise SyntaxError(parser.errors[0][0])
    return program


class ForParsingTests(unittest.TestCase):
    def test_value_only(self):
        (stmt,) = parse("for let x in xs { }")
        self.assertIsInstance(stmt, ExprStmt)
        loop = stmt.value
        self.assertIsInstance(loop, ForStmt)
        self.assertEqual(loop.value_name, "x")
        self.assertIsNone(loop.index_name)

    def test_value_and_index(self):
        (stmt,) = parse("for let x, let i in 1..3 { }")
        loop = stmt.value
        self.assertEqual((loop.value_name, loop.index_name), ("x", "i"))
        self.assertEqual(loop.index_position, "for let x, let i".index("i"))

    def test_loops_are_expressions(self):
        (stmt,) = parse("let x = while true { break 10 }")
        self.assertIsInstance(stmt, LetStmt)
        self.assertIsInstance(stmt.value, WhileStmt)
        (brk,) = stmt.value.body.stmts
        self.assertIsInstance(brk, BreakStmt)
        self.assertIsNotNone(brk.value)
        (stmt,) = parse("let y = for let v in xs { break v }")
        self.assertIsInstance(stmt.value, ForStmt)

    def test_loop_statements_need_no_semicolon(self):
        stmts = parse("while false { }\nfor let v in xs { }\nprint(1)")
        self.assertEqual(len(stmts), 3)

    def test_break_value_must_be_on_the_same_line(self):
        (stmt,) = parse("while true {\n    break\n    foo()\n}")
        (brk,) = stmt.value.body.stmts
        self.assertIsNone(brk.value)
        self.assertIsNotNone(stmt.value.body.tail)  # `foo()` stays its own item

    def test_bare_break_before_brace_and_semicolon(self):
        (stmt,) = parse("while true { if a { break } break; }")
        self.assertIsNone(stmt.value.body.stmts[-1].value)


class ForLoopTests(unittest.TestCase):
    def test_range(self):
        self.assertEqual(run_source("for let v in 1..4 { print(v) }"), "1\n2\n3\n")

    def test_inclusive_range_with_index(self):
        src = "for let v, let i in 5..=7 { print(i, v) }"
        self.assertEqual(run_source(src), "0 5\n1 6\n2 7\n")

    def test_string(self):
        self.assertEqual(run_source('for let c in "héy" { print(c) }'), "h\né\ny\n")

    def test_adapters(self):
        src = "for let v, let i in (1..10).filter(fn(n) { n % 3 == 0 }).map(fn(n) { n * 10 }) { print(i, v) }"
        self.assertEqual(run_source(src), "0 30\n1 60\n2 90\n")

    def test_empty(self):
        self.assertEqual(run_source('for let v in 3..3 { print(v) }\nprint("done")'), "done\n")

    def test_user_iterable(self):
        src = """
        struct Countdown { from }
        struct CountdownIter { n }
        impl Iterable for Countdown {
            fn iter(self) { CountdownIter { n: self.from } }
        }
        impl Iterator for CountdownIter {
            fn next(self) {
                if self.n > 0 { self.n = self.n - 1; some(self.n + 1) } else { none }
            }
        }
        let c = Countdown { from: 3 }
        for let v, let i in c { print(i, v) }
        for let v in (Countdown { from: 2 }) { print(v) }
        """
        self.assertEqual(run_source(src), "0 3\n1 2\n2 1\n2\n1\n")

    def test_iterable_is_restarted_each_loop(self):
        src = "let r = 1..3\nfor let v in r { print(v) }\nfor let v in r { print(v) }"
        self.assertEqual(run_source(src), "1\n2\n1\n2\n")

    def test_iterable_evaluated_once(self):
        src = """
        let calls = 0
        fn make() { calls = calls + 1; 1..4 }
        for let v in make() { }
        print(calls)
        """
        self.assertEqual(run_source(src), "1\n")

    def test_nested(self):
        src = "for let a in 1..3 { for let b in 1..3 { print(a, b) } }"
        self.assertEqual(run_source(src), "1 1\n1 2\n2 1\n2 2\n")

    def test_nested_same_names_shadow(self):
        src = "for let v in 1..3 { for let v in 10..12 { print(v) } print(v) }"
        self.assertEqual(run_source(src), "10\n11\n1\n10\n11\n2\n")

    def test_continue(self):
        src = "for let v, let i in 1..6 { if v % 2 == 0 { continue } print(i, v) }"
        # the index still counts skipped items
        self.assertEqual(run_source(src), "0 1\n2 3\n4 5\n")

    def test_break(self):
        src = "for let v in 1.. { if v > 3 { break } print(v) }"
        self.assertEqual(run_source(src), "1\n2\n3\n")

    def test_return_from_inside_a_for(self):
        src = """
        fn first_over(xs, limit) {
            for let v in xs { if v > limit { return v } }
            none
        }
        print(first_over(1..10, 4))
        print(first_over(1..3, 4))
        """
        self.assertEqual(run_source(src), "5\nnone\n")

    def test_bindings_are_scoped_to_the_loop(self):
        with self.assertRaisesRegex(Exception, r"Undefined variable 'v'"):
            run_source("for let v in 1..3 { }\nprint(v)")
        with self.assertRaisesRegex(Exception, r"Undefined variable 'i'"):
            run_source("for let v, let i in 1..3 { }\nprint(i)")

    def test_closures_capture_the_loop_variable(self):
        src = "let f = none\nfor let v in 1..3 { f = fn() { v } }\nprint(f())"
        self.assertEqual(run_source(src), "2\n")

    def test_defer_runs_every_iteration_and_on_break(self):
        src = """
        for let v in 1..5 {
            defer print("end", v)
            if v == 2 { continue }
            if v == 3 { break }
            print("body", v)
        }
        """
        self.assertEqual(run_source(src), "body 1\nend 1\nend 2\nend 3\n")

    def test_for_inside_a_function_and_async(self):
        src = """
        fn total(xs) {
            let sum = 0
            for let v in xs { sleep_async(1); sum = sum + v }
            sum
        }
        print(detach total(1..=4).await)
        """
        self.assertEqual(run_source(src), "10\n")


class LoopValueTests(unittest.TestCase):
    def test_while_break_value(self):
        self.assertEqual(run_source("let x = while true { break 10 }\nprint(x)"), "10\n")

    def test_while_without_break_is_none(self):
        self.assertEqual(run_source("let x = while false { }\nprint(x)"), "none\n")

    def test_bare_break_is_none(self):
        self.assertEqual(run_source("let x = while true { break }\nprint(x)"), "none\n")

    def test_for_break_value(self):
        src = "let y = for let n in 1.. { if n * n > 50 { break n } }\nprint(y)"
        self.assertEqual(run_source(src), "8\n")

    def test_for_exhausted_is_none(self):
        src = "let y = for let n in 1..3 { if n > 5 { break n } }\nprint(y)"
        self.assertEqual(run_source(src), "none\n")

    def test_each_run_starts_at_none(self):
        # each run of the loop starts over at `none`
        src = """
        fn find(limit) { for let n in 1..5 { if n == limit { break n * 10 } } }
        print(find(3))
        print(find(9))
        print(find(2))
        """
        self.assertEqual(run_source(src), "30\nnone\n20\n")

    def test_break_value_is_an_expression(self):
        src = 'let s = while true { break if true { "a" + "b" } else { "c" } }\nprint(s)'
        self.assertEqual(run_source(src), "ab\n")

    def test_break_value_only_leaves_the_innermost_loop(self):
        src = """
        let outer = for let a in 1..4 {
            let inner = for let b in 1..4 { if a * b == 4 { break b } }
            match inner { none => { } _ => { break "" + a + "x" + inner } }
        }
        print(outer)
        """
        self.assertEqual(run_source(src), "2x2\n")

    def test_loop_as_function_tail(self):
        src = "fn f() { let i = 0\n while true { i = i + 1\n if i == 3 { break i * 2 } } }\nprint(f())"
        self.assertEqual(run_source(src), "6\n")

    def test_loop_as_call_argument(self):
        self.assertEqual(run_source("print(while true { break 7 })"), "7\n")

    def test_break_value_evaluated_before_defers(self):
        src = """
        let x = 1
        let y = while true {
            defer x = 100
            break x
        }
        print(y, x)
        """
        self.assertEqual(run_source(src), "1 100\n")


class LoopErrorTests(unittest.TestCase):
    def test_non_iterable(self):
        with self.assertRaisesRegex(Exception, r"'Number' does not implement trait 'Iterable'"):
            run_source("for let v in 5 { }")

    def test_iter_must_return_an_iterator(self):
        src = """
        struct P { }
        impl Iterable for P { fn iter(self) { 7 } }
        for let v in (P { }) { }
        """
        with self.assertRaisesRegex(Exception, r"'Number' does not implement trait 'Iterator'"):
            run_source(src)

    def test_to_range_is_not_iterable(self):
        with self.assertRaisesRegex(Exception, r"'ToRange' does not implement trait 'Iterable'"):
            run_source("for let v in ..5 { }")

    def test_let_is_required(self):
        with self.assertRaisesRegex(Exception, r"Expected 'let' before a 'for' loop variable"):
            run_source("for x in 1..3 { }")
        with self.assertRaisesRegex(Exception, r"Expected 'let' before a 'for' loop variable"):
            run_source("for let x, i in 1..3 { }")

    def test_in_is_required(self):
        with self.assertRaisesRegex(Exception, r"Invalid syntax"):
            run_source("for let x 1..3 { }")

    def test_in_is_reserved(self):
        with self.assertRaisesRegex(Exception, r"Invalid syntax"):
            run_source("let in = 3")

    def test_duplicate_binding_names(self):
        with self.assertRaisesRegex(Exception, r"variable is already defined a"):
            run_source("for let a, let a in 1..3 { }")

    def test_break_with_value_outside_a_loop(self):
        with self.assertRaisesRegex(Exception, r"'break' used outside a loop"):
            run_source("break 5")

    def test_break_value_inside_a_closure_in_a_loop(self):
        with self.assertRaisesRegex(Exception, r"'break' used outside a loop"):
            run_source("for let v in 1..3 { let f = fn() { break v } }")

    def test_continue_outside_a_loop(self):
        with self.assertRaisesRegex(Exception, r"'continue' used outside a loop"):
            run_source("continue")


class PreludeTriggerTests(unittest.TestCase):
    def test_for_let_includes_the_prelude(self):
        self.assertIsNotNone(preprocess(None, 'for let c in "ab" { print(c) }').prelude_start)

    def test_impl_for_does_not(self):
        src = "struct P { }\nimpl P { fn f(self) { 1 } }\nprint(1)"
        self.assertIsNone(preprocess(None, src).prelude_start)
        src = "struct P { }\ntrait T { fn f(self) }\nimpl T for P { fn f(self) { 1 } }"
        self.assertIsNone(preprocess(None, src).prelude_start)


if __name__ == "__main__":
    unittest.main()
