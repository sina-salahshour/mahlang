"""End-to-end behavioral tests for M16: default parameter values and
keyword-argument calls (`f(1, scale: 3)`), and `print(a, b, sep:, end:)`
-- see docs/TESTING.md, docs/MAHC_FORMAT.md's PARAMS section/`jmpset`/
`*kw` opcodes (normative), and the M16 spec's own test list.

Uses `run_source` like the other behavioral test files (never re-plumbs
the pipeline directly). Compile/syntax-error cases use `compile_source`/
`parse_source` instead, matching the existing convention (see
tests/test_error_recovery.py) -- a syntax error surfaces as
`parser.errors`, everything else raises directly.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mah.runtime_values import MahRuntimeError
from tests.support import compile_source, parse_source, run_source


class DefaultsHappyPathTests(unittest.TestCase):
    def test_positional_and_keyword_mixes(self):
        src = (
            "fn area(w, h = 1, scale = 1) { w * h * scale }\n"
            "print(area(2))\n"
            "print(area(2, 3))\n"
            "print(area(2, scale: 3))\n"
            "print(area(w: 2, h: 5))\n"
            "print(area(scale: 2, w: 3, h: 4))\n"
        )
        self.assertEqual(run_source(src), "2\n6\n6\n10\n24\n")

    def test_default_uses_an_earlier_param(self):
        src = (
            'fn greet(name, greeting = "Hello " + name) { greeting }\n'
            'print(greet("mah"))\n'
            'print(greet("mah", "yo"))\n'
        )
        self.assertEqual(run_source(src), "Hello mah\nyo\n")

    def test_default_evaluated_per_call(self):
        # A fresh struct every call that doesn't pass `b` -- proves the
        # default expression runs at call time, not once at closure
        # creation.
        src = "struct B { n }\nfn f(b = B { n: 0 }) { b.n = b.n + 1; b.n }\nprint(f())\nprint(f())"
        self.assertEqual(run_source(src), "1\n1\n")

    def test_default_sees_an_outer_variable_at_call_time(self):
        src = "let base = 10\nfn f(x = base) { x }\nbase = 20\nprint(f())"
        self.assertEqual(run_source(src), "20\n")

    def test_default_expression_calls_functions_and_closures(self):
        src = (
            "fn one() { 1 }\n"
            "fn f(x = one() + 1, g = fn(v) { v * 2 }) { g(x) }\n"
            "print(f())\n"
            "print(f(5))\n"
            "print(f(g: fn(v) { v }))\n"
        )
        self.assertEqual(run_source(src), "4\n10\n2\n")

    def test_recursion_with_a_default_and_a_keyword_argument(self):
        src = (
            "fn sum(n, acc = 0) { if n == 0 { acc } else { sum(n - 1, acc: acc + n) } }\n"
            "print(sum(4))\n"
        )
        self.assertEqual(run_source(src), "10\n")

    def test_closures_returned_with_defaults_keep_their_own(self):
        src = (
            "fn mk(k = 3) { fn(x, y = k) { x * y } }\n"
            "let m = mk()\n"
            "print(m(2))\n"
            "print(m(2, y: 5))\n"
        )
        self.assertEqual(run_source(src), "6\n10\n")

    def test_methods_defaults_and_keyword_args(self):
        src = (
            "struct R { w }\n"
            "impl R {\n"
            "    fn scaled(self, k = 2, add = 0) { self.w * k + add }\n"
            "    fn new(w = 1) { R { w: w } }\n"
            "}\n"
            "let r = R.new(w: 5)\n"
            "print(r.scaled())\n"
            "print(r.scaled(add: 1))\n"
            "print(R.scaled(r, k: 3))\n"
            "print(R.new().w)\n"
        )
        self.assertEqual(run_source(src), "10\n11\n15\n1\n")

    def test_trait_method_impl_default_and_trait_qualified_kwarg_call(self):
        src = (
            "trait S { fn size(self, unit) }\n"
            "struct B { }\n"
            'impl S for B { fn size(self, unit = "cm") { "5" + unit } }\n'
            "print(B {}.size())\n"
            'print(S.size(B {}, unit: "m"))\n'
        )
        self.assertEqual(run_source(src), "5cm\n5m\n")

    def test_field_closure_call_with_kwargs(self):
        src = (
            "struct H { f }\n"
            "let h = H { f: fn(a, b = 1) { a - b } }\n"
            "print(h.f(b: 5, a: 10))\n"
        )
        self.assertEqual(run_source(src), "5\n")

    def test_detach_with_kwargs_plain_function(self):
        src = (
            "fn slow(v, ms = 5) { sleep_async(ms); v }\n"
            "let p = detach slow(ms: 1, v: 7)\n"
            "print(p.await)\n"
        )
        self.assertEqual(run_source(src), "7\n")

    def test_detach_with_kwargs_method(self):
        src = (
            "struct W { }\n"
            "impl W { fn go(self, v = 1) { sleep_async(1); v } }\n"
            "let w = W {}\n"
            "print((detach w.go(v: 3)).await)\n"
        )
        self.assertEqual(run_source(src), "3\n")

    def test_defer_and_defaults_inside_a_function(self):
        src = 'fn f(x = 1) { defer print("bye"); x }\nprint(f())'
        self.assertEqual(run_source(src), "bye\n1\n")


class PrintSepEndTests(unittest.TestCase):
    def test_default_separator_is_a_space(self):
        self.assertEqual(run_source('print("a", "b")'), "a b\n")

    def test_explicit_sep_and_end(self):
        self.assertEqual(run_source('print("a", "b", sep: ", ", end: "!\\n")'), "a, b!\n")

    def test_end_suppresses_the_newline(self):
        self.assertEqual(run_source('print("x", end: "")\nprint("y")'), "xy\n")

    def test_print_with_no_args_prints_just_a_newline(self):
        self.assertEqual(run_source("print()"), "\n")

    def test_empty_sep(self):
        self.assertEqual(run_source('print(1, 2, 3, sep: "")'), "123\n")

    def test_sep_may_be_any_value_formatted_with_to_string(self):
        self.assertEqual(run_source('print("a", "b", sep: 0)'), "a0b\n")

    def test_printable_is_honored_for_sep_join(self):
        src = (
            "struct P { }\n"
            'impl Printable for P { fn to_string(self) { "P!" } }\n'
            'print(P {}, P {}, sep: "-")\n'
        )
        self.assertEqual(run_source(src), "P!-P!\n")

    def test_print_evaluates_every_arg_before_printing_anything(self):
        src = 'fn noisy(v) { print("eval " + v); v }\nprint(noisy(1), noisy(2))'
        self.assertEqual(run_source(src), "eval 1\neval 2\n1 2\n")


class RuntimeErrorTests(unittest.TestCase):
    def test_unexpected_keyword_argument(self):
        src = "fn f(a, b = 1) { a }\nf(1, c: 2)"
        with self.assertRaises(MahRuntimeError) as cm:
            run_source(src)
        self.assertIn("'f' got an unexpected keyword argument 'c'", str(cm.exception))

    def test_multiple_values_for_argument(self):
        src = "fn f(a, b = 1) { a }\nf(1, a: 2)"
        with self.assertRaises(MahRuntimeError) as cm:
            run_source(src)
        self.assertIn("'f' got multiple values for argument 'a'", str(cm.exception))

    def test_missing_required_argument(self):
        src = "fn f(a, b = 1) { a }\nf(b: 2)"
        with self.assertRaises(MahRuntimeError) as cm:
            run_source(src)
        self.assertIn("'f' is missing required argument 'a'", str(cm.exception))

    def test_too_many_positional_arguments_with_defaults(self):
        src = "fn f(a, b = 1) { a }\nf(1, 2, 3)"
        with self.assertRaises(MahRuntimeError) as cm:
            run_source(src)
        self.assertIn("'f' takes at most 2 positional arguments but 3 were given", str(cm.exception))

    def test_no_kwargs_no_defaults_keeps_the_old_message(self):
        src = "fn f(a) { a }\nf()"
        with self.assertRaises(MahRuntimeError) as cm:
            run_source(src)
        self.assertIn("Argument Count is invalid. 'f' accepts 1 arguments but 0 was given", str(cm.exception))

    def test_method_label_and_counts_exclude_the_receiver(self):
        src = "struct S { }\nimpl S { fn m(self, a, b = 1) { a } }\nS {}.m(1, 2, 3)"
        with self.assertRaises(MahRuntimeError) as cm:
            run_source(src)
        self.assertIn("method 'm' takes at most 2 positional arguments but 3 were given", str(cm.exception))

        src2 = "struct S { }\nimpl S { fn m(self, a, b = 1) { a } }\nS {}.m(z: 1)"
        with self.assertRaises(MahRuntimeError) as cm2:
            run_source(src2)
        self.assertIn("method 'm' got an unexpected keyword argument 'z'", str(cm2.exception))

    def test_native_method_with_keyword_argument(self):
        with self.assertRaises(MahRuntimeError) as cm:
            run_source("5.to_string(x: 1)")
        self.assertIn("got an unexpected keyword argument 'x'", str(cm.exception))

    def test_error_location_is_still_reported_in_debug_builds(self):
        src = "fn f(a) { a }\nf(b: 1)"
        with self.assertRaises(MahRuntimeError) as cm:
            run_source(src)
        self.assertIn("at position #2:1", str(cm.exception))


class CompileAndSyntaxErrorTests(unittest.TestCase):
    def test_parameter_needs_a_default_because_an_earlier_one_has_one(self):
        with self.assertRaises(Exception) as cm:
            compile_source(text="fn f(a = 1, b) { a }")
        self.assertIn("needs a default value", str(cm.exception))

    def test_self_cannot_have_a_default(self):
        with self.assertRaises(Exception) as cm:
            compile_source(text="struct S { }\nimpl S { fn m(self = 1) { 1 } }")
        self.assertIn("'self' can't have a default value", str(cm.exception))

    def test_required_trait_method_cannot_declare_defaults(self):
        with self.assertRaises(Exception) as cm:
            compile_source(text="trait T { fn m(self, x = 1) }")
        self.assertIn("is required, so it can't declare default values", str(cm.exception))

    def test_positional_after_keyword_is_a_syntax_error(self):
        _program, parser = parse_source("f(a: 1, 2)")
        self.assertTrue(parser.errors, "expected a recorded syntax error")
        self.assertIn("positional argument after a keyword argument", parser.errors[0][0])

    def test_duplicate_keyword_argument_is_a_syntax_error(self):
        _program, parser = parse_source("f(a: 1, a: 2)")
        self.assertTrue(parser.errors, "expected a recorded syntax error")
        self.assertIn("given more than once", parser.errors[0][0])

    def test_print_rejects_an_unknown_keyword(self):
        _program, parser = parse_source('print(1, sepp: " ")')
        self.assertTrue(parser.errors, "expected a recorded syntax error")
        self.assertIn("print() got an unexpected keyword argument 'sepp'", parser.errors[0][0])

    def test_sin_does_not_take_keyword_arguments(self):
        _program, parser = parse_source("sin(x: 1)")
        self.assertTrue(parser.errors, "expected a recorded syntax error")
        self.assertIn("'sin' doesn't take keyword arguments", parser.errors[0][0])

    def test_a_default_cannot_see_a_later_parameter(self):
        with self.assertRaises(Exception) as cm:
            compile_source(text="fn f(a = b, b = 1) { a }")
        self.assertIn("Undefined variable 'b'", str(cm.exception))


class VerificationProbeTests(unittest.TestCase):
    """Added during verification, beyond the spec's test list."""

    def test_explicit_sep_is_evaluated_even_with_nothing_to_separate(self):
        src = 'fn s() { print("sep evaluated"); "-" }\nprint(sep: s())\nprint(1, sep: s())'
        self.assertEqual(run_source(src), "sep evaluated\n\nsep evaluated\n1\n")

    def test_keyword_values_evaluate_in_written_order(self):
        src = 'fn t(v) { print("eval " + v); v }\nfn f(a, b, c) { a + b + c }\nprint(f(t(1), c: t(3), b: t(2)))'
        self.assertEqual(run_source(src), "eval 1\neval 3\neval 2\n6\n")

    def test_default_can_capture_enclosing_functions_param(self):
        self.assertEqual(run_source('fn outer(a) { fn inner(b = a * 10) { b } inner() }\nprint(outer(3))'), "30\n")

    def test_trait_default_method_with_default_param(self):
        src = 'trait G { fn hi(self, who = "world") { "hi " + who } }\nstruct S { }\nimpl G for S { }\nprint(S {}.hi())\nprint(S {}.hi(who: "you"))'
        self.assertEqual(run_source(src), "hi world\nhi you\n")

    def test_static_path_kwargs_with_default_using_earlier_param(self):
        src = 'struct R { w, h }\nimpl R { fn new(w = 1, h = w) { Self { w: w, h: h } } }\nprint(R.new(h: 7).w, R.new(h: 7).h)\nprint(R.new(3).h)'
        self.assertEqual(run_source(src), "1 7\n3\n")

    def test_defer_reads_a_defaulted_param(self):
        self.assertEqual(run_source('fn f(n, msg = "bye " + n) { defer print(msg); n }\nprint(f(2))'), "bye 2\n2\n")


if __name__ == "__main__":
    unittest.main()
