"""End-to-end behavioral tests for M12: `trait`/`impl` declarations, method
calls (`x.m(...)`/`Type.m(...)`/`Trait.m(x, ...)`), and the built-in
`Printable` system trait -- see docs/TESTING.md and the M12 spec.

Uses `run_source`/`run_file` like the other behavioral test files (never
re-plumbs the pipeline directly).
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support import run_file, run_source


class HappyPathTests(unittest.TestCase):
    def test_inherent_static_and_methods_and_mutation_through_self(self):
        src = """
        struct Point { x, y }
        impl Point {
            fn new(x, y) { Point { x: x, y: y } }
            fn sum(self) { self.x + self.y }
            fn scale(self, k) { self.x = self.x * k; self.y = self.y * k; }
        }
        let p = Point.new(2, 3)
        print(p.sum())
        p.scale(10)
        print(p.x, p.y)
        """
        # M16: `print(p.x, p.y)` now joins with the default `sep` (a
        # single space) instead of printing each argument on its own line.
        self.assertEqual(run_source(src), "5\n20 30\n")

    def test_self_and_chaining_and_fn_declared_later_in_same_impl(self):
        src = """
        struct Point { x, y }
        impl Point {
            fn origin() { Self { x: 0, y: 0 } }
            fn moved(self, dx) { Self.make(self.x + dx, self.y) }
            fn make(x, y) { Self { x: x, y: y } }
        }
        print(Point.origin().moved(4).x)
        """
        self.assertEqual(run_source(src), "4\n")

    def test_self_in_enum_literal_unit_variant_and_patterns(self):
        src = """
        enum Light { Red, Green, Blink { n } }
        impl Light {
            fn next(self) {
                match self {
                    Self.Red => { Self.Green }
                    Self.Green => { Self.Blink { n: 2 } }
                    Self.Blink { n } => { Self.Red }
                }
            }
        }
        print(Light.Red.next())
        print(Light.Red.next().next())
        """
        self.assertEqual(run_source(src), "Light.Green\nLight.Blink { n: 2 }\n")

    def test_trait_dispatch_across_two_struct_types(self):
        src = """
        trait Shape { fn area(self) fn name(self) }
        struct Rect { w, h }
        struct Square { s }
        impl Shape for Rect { fn area(self) { self.w * self.h } fn name(self) { "rect" } }
        impl Shape for Square { fn area(self) { self.s * self.s } fn name(self) { "square" } }
        fn describe(s) { s.name() + " " + s.area() }
        print(describe(Rect { w: 2, h: 3 }))
        print(describe(Square { s: 4 }))
        """
        self.assertEqual(run_source(src), "rect 6\nsquare 16\n")

    def test_default_method_and_override(self):
        src = """
        trait Greet { fn name(self) fn greet(self) { "hello " + self.name() } }
        struct A { }
        struct B { }
        impl Greet for A { fn name(self) { "a" } }
        impl Greet for B { fn name(self) { "b" } fn greet(self) { "hi " + self.name() } }
        print(A {}.greet())
        print(B {}.greet())
        """
        self.assertEqual(run_source(src), "hello a\nhi b\n")

    def test_user_trait_for_builtin_number_and_string(self):
        src = """
        trait Double { fn double(self) }
        impl Double for Number { fn double(self) { self * 2 } }
        impl Double for String { fn double(self) { self + self } }
        print(21.double())
        print("ab".double())
        """
        self.assertEqual(run_source(src), "42\nabab\n")

    def test_user_trait_for_promise_and_option(self):
        src = """
        trait Ready { fn is_ready(self) }
        impl Ready for Promise {
            fn is_ready(self) { match self { Promise.Settled { value } => { true } _ => { false } } }
        }
        impl Ready for Option {
            fn is_ready(self) { match self { some(x) => { true } none => { false } } }
        }
        fn work() { 5 }
        let p = detach work()
        print(p.is_ready())
        print(some(1).is_ready())
        print(none.is_ready())
        """
        self.assertEqual(run_source(src), "true\ntrue\nfalse\n")

    def test_user_trait_for_bool_and_function(self):
        src = """
        trait Describe { fn describe(self) }
        impl Describe for Bool { fn describe(self) { if self { "yes" } else { "no" } } }
        impl Describe for Function { fn describe(self) { "fn returning " + self() } }
        fn seven() { 7 }
        print(true.describe())
        print(false.describe())
        print(seven.describe())
        """
        self.assertEqual(run_source(src), "yes\nno\nfn returning 7\n")

    def test_printable_for_struct_print_concat_nested_and_explicit_calls(self):
        src = """
        struct Point { x, y }
        impl Printable for Point { fn to_string(self) { "(" + self.x + ", " + self.y + ")" } }
        let p = Point { x: 1, y: 2 }
        print(p)
        print("p = " + p)
        print(some(p))
        print(p.to_string())
        print(Printable.to_string(p))
        """
        self.assertEqual(run_source(src), "(1, 2)\np = (1, 2)\nsome((1, 2))\n(1, 2)\n(1, 2)\n")

    def test_printable_nested_inside_a_struct_without_its_own_impl(self):
        src = """
        struct Point { x, y }
        impl Printable for Point { fn to_string(self) { "P" + self.x } }
        struct Line { a, b }
        print(Line { a: Point { x: 1, y: 0 }, b: Point { x: 2, y: 0 } })
        """
        self.assertEqual(run_source(src), "Line { a: P1, b: P2 }\n")

    def test_printable_for_a_user_enum(self):
        src = """
        enum Color { Red, Green }
        impl Printable for Color {
            fn to_string(self) { match self { Color.Red => { "red" } Color.Green => { "green" } } }
        }
        print(Color.Red)
        print("color: " + Color.Green)
        """
        self.assertEqual(run_source(src), "red\ncolor: green\n")

    def test_native_printable_on_builtins(self):
        src = """
        print(5.to_string() + "!")
        print(Printable.to_string(true))
        print(Number.to_string(3))
        print(none.to_string())
        print(some(2).to_string())
        """
        self.assertEqual(run_source(src), "5!\ntrue\n3\nnone\nsome(2)\n")

    def test_function_values_print_as_fn_name(self):
        src = 'fn foo() { 1 }\nprint(foo)\nprint(fn(x) { x })'
        self.assertEqual(run_source(src), "<fn foo>\n<fn>\n")

    def test_trait_qualified_disambiguation(self):
        src = """
        trait A { fn hi(self) }
        trait B { fn hi(self) }
        struct S { }
        impl A for S { fn hi(self) { "a" } }
        impl B for S { fn hi(self) { "b" } }
        let s = S {}
        print(A.hi(s))
        print(B.hi(s))
        """
        self.assertEqual(run_source(src), "a\nb\n")

    def test_inherent_wins_over_trait_methods(self):
        src = """
        trait A { fn hi(self) }
        trait B { fn hi(self) }
        struct S { }
        impl A for S { fn hi(self) { "a" } }
        impl B for S { fn hi(self) { "b" } }
        let s = S {}
        print(A.hi(s))
        print(B.hi(s))
        impl S { fn hi(self) { "inherent" } }
        print(s.hi())
        """
        self.assertEqual(run_source(src), "a\nb\ninherent\n")

    def test_static_trait_fn_and_type_method_explicit_self_call(self):
        src = """
        trait Zero { fn zero() }
        struct P { v }
        impl Zero for P { fn zero() { P { v: 0 } } }
        impl P { fn get(self) { self.v } }
        print(P.zero().v)
        print(P.get(P { v: 9 }))
        """
        self.assertEqual(run_source(src), "0\n9\n")

    def test_methods_see_globals_declared_later_and_return_self_for_chaining(self):
        src = """
        struct Counter { n }
        impl Counter { fn bump(self) { self.n = self.n + STEP; self } }
        let STEP = 5
        let c = Counter { n: 0 }
        c.bump().bump()
        print(c.n)
        """
        self.assertEqual(run_source(src), "10\n")

    def test_recursive_method(self):
        src = (
            "struct N { v }\n"
            "impl N { fn fact(self, k) { if k < 2 { 1 } else { k * self.fact(k - 1) } } }\n"
            "print(N { v: 0 }.fact(5))"
        )
        self.assertEqual(run_source(src), "120\n")

    def test_order_independence_use_before_declaration(self):
        src = """
        let p = Point.new(1)
        print(p.v)
        impl Point { fn new(v) { Point { v: v } } }
        struct Point { v }
        """
        self.assertEqual(run_source(src), "1\n")

    def test_closure_inside_a_method_captures_self(self):
        src = """
        struct Acc { total }
        impl Acc { fn adder(self) { fn(x) { self.total = self.total + x } } }
        let a = Acc { total: 0 }
        let add = a.adder()
        add(3)
        add(4)
        print(a.total)
        """
        self.assertEqual(run_source(src), "7\n")

    def test_async_inside_a_method(self):
        src = """
        struct T { }
        impl T { fn wait_then(self, v) { sleep_async(10); v } }
        fn run() { T {}.wait_then(9) }
        let p = detach run()
        print("before")
        print(p.await)
        """
        self.assertEqual(run_source(src), "before\n9\n")

    def test_defer_inside_a_method(self):
        src = """
        struct D { }
        impl D { fn go(self) { defer print("deferred"); print("body"); 1 } }
        print(D {}.go())
        """
        self.assertEqual(run_source(src), "body\ndeferred\n1\n")

    def test_variable_shadows_a_type_name_variable_wins_dynamic_call_on_it(self):
        src = (
            "struct Point { v }\n"
            "impl Point { fn new(v) { Point { v: v } } }\n"
            "let Point = 3\n"
            "Point.new(1)"
        )
        with self.assertRaises(Exception) as cm:
            run_source(src)
        self.assertIn("'Number' has no method 'new'", str(cm.exception))

    def test_more_than_400_instructions_now_compile_and_run(self):
        lines = [f"let v{i} = {i}" for i in range(300)]
        lines.append("print(v299)")
        src = "\n".join(lines)
        self.assertEqual(run_source(src), "299\n")


class ErrorCaseTests(unittest.TestCase):
    def _raises(self, src: str, substring: str) -> None:
        with self.assertRaises(Exception) as cm:
            run_source(src)
        self.assertIn(substring, str(cm.exception))

    def test_undefined_trait(self):
        self._raises("struct S { }\nimpl Nope for S { }", "Undefined trait 'Nope'")

    def test_undefined_type(self):
        self._raises("impl Foo { }", "Undefined type 'Foo'")

    def test_inherent_impl_on_builtin_type_rejected(self):
        self._raises("impl Promise { fn f(self) { 1 } }", "built-in type 'Promise'")
        self._raises("impl Number { }", "built-in type 'Number'")

    def test_orphan_rule_rejects_system_trait_for_builtin_type(self):
        self._raises(
            'impl Printable for Number { fn to_string(self) { "x" } }',
            "Cannot implement built-in trait 'Printable' for built-in type 'Number'",
        )

    def test_missing_trait_method(self):
        self._raises(
            "trait T { fn a(self) fn b(self) }\nstruct S { }\nimpl T for S { fn a(self) { 1 } }",
            "missing trait method(s) ['b']",
        )

    def test_extra_method_not_a_member_of_trait(self):
        self._raises(
            "trait T { fn a(self) }\nstruct S { }\nimpl T for S { fn a(self) { 1 } fn c(self) { 2 } }",
            "'c' is not a member of trait 'T'",
        )

    def test_param_count_mismatch(self):
        self._raises(
            "trait T { fn a(self, x) }\nstruct S { }\nimpl T for S { fn a(self) { 1 } }",
            "parameter(s)",
        )

    def test_self_mismatch(self):
        self._raises(
            "trait T { fn a(self) }\nstruct S { }\nimpl T for S { fn a(x) { 1 } }",
            "declares 'a' as a method",
        )

    def test_duplicate_trait_impl(self):
        self._raises(
            "trait T { }\nstruct S { }\nimpl T for S { }\nimpl T for S { }",
            "'S' already implements 'T'",
        )

    def test_duplicate_inherent_across_blocks(self):
        self._raises(
            "struct S { }\nimpl S { fn f(self) { 1 } }\nimpl S { fn f(self) { 2 } }",
            "Duplicate definition of 'f' for 'S'",
        )

    def test_duplicate_method_in_trait(self):
        self._raises("trait T { fn a(self) fn a(self) }", "more than once")

    def test_self_not_first_parameter(self):
        self._raises("struct S { }\nimpl S { fn f(a, self) { 1 } }", "first parameter")
        self._raises("trait T { fn f(a, self) }", "first parameter")

    def test_self_in_a_plain_fn(self):
        self._raises("fn f(self) { 1 }", "first parameter")

    def test_let_self_is_reserved(self):
        self._raises("let self = 1", "'self' is reserved")

    def test_self_outside_an_impl(self):
        self._raises("struct S { }\nfn f() { Self {} }", "'Self' is only valid inside an impl block")
        self._raises("trait T { fn f(self) { Self {} } }", "'Self' is only valid inside an impl block")

    def test_nested_impl_is_rejected(self):
        self._raises("struct S { }\nfn f() { impl S { } }", "only allowed at the top level")

    def test_no_such_method(self):
        self._raises("struct S { }\nS {}.nope()", "'S' has no method 'nope'")

    def test_static_fn_called_as_method(self):
        self._raises(
            "struct S { }\nimpl S { fn make() { 1 } }\nS {}.make()",
            "is a static function of 'S'",
        )

    def test_method_arity_error(self):
        self._raises(
            "struct S { }\nimpl S { fn f(self, a) { a } }\nS {}.f()",
            "accepts 1 arguments but 0 was given",
        )

    def test_unknown_static_fn(self):
        self._raises("struct S { }\nS.nope()", "Type 'S' has no function 'nope'")

    def test_trait_qualified_on_non_implementer(self):
        self._raises(
            "trait T { fn a(self) }\nstruct S { }\nT.a(S {})",
            "'S' does not implement trait 'T'",
        )

    def test_trait_qualified_static_fn_rejected(self):
        self._raises("trait T { fn make() }\nT.make()", "static trait function")

    def test_trait_qualified_with_no_receiver(self):
        self._raises("trait T { fn a(self) }\nT.a()", "needs the receiver")

    def test_trait_vs_struct_name_clash(self):
        self._raises("struct X { }\ntrait X { }", "already declared as a type")
        self._raises("struct Number { }", "built-in type name")

    def test_non_string_to_string_raises(self):
        self._raises(
            'struct P { }\nimpl Printable for P { fn to_string(self) { 5 } }\nprint(P {})',
            "must return a String",
        )

    def test_ambiguous_method_call(self):
        src = """
        trait A { fn hi(self) }
        trait B { fn hi(self) }
        struct S { }
        impl A for S { fn hi(self) { "a" } }
        impl B for S { fn hi(self) { "b" } }
        let s = S {}
        s.hi()
        """
        self._raises(src, "ambiguous")

    def test_to_string_that_suspends_raises(self):
        self._raises(
            'struct P { }\nimpl Printable for P { fn to_string(self) { sleep_async(1); "p" } }\nprint(P {})',
            "cannot suspend",
        )

    def test_impl_method_without_body_is_a_parse_error(self):
        from tests.support import parse_source

        _program, parser = parse_source("struct S { }\nimpl S { fn f(self) }")
        self.assertTrue(parser.errors)


class ImportInteropTests(unittest.TestCase):
    def test_trait_impl_and_method_calls_across_an_import(self):
        lib_src = (
            'export fn describe(x) { "free " + x }\n'
            "trait Describe { fn describe(self) }\n"
            "struct Box { v }\n"
            "impl Describe for Box { fn describe(self) { \"box \" + self.v } }\n"
            "export fn make_box(v) { Box { v: v } }\n"
            "export fn lib_describe(b) { b.describe() }\n"
        )
        main_src = (
            'import "lib.mh"\n'
            "print(describe(1))\n"
            "let b = make_box(2)\n"
            "print(b.describe())\n"
            "print(lib_describe(b))\n"
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            lib_path = os.path.join(tmp_dir, "lib.mh")
            main_path = os.path.join(tmp_dir, "main.mh")
            with open(lib_path, "w", encoding="utf-8") as handle:
                handle.write(lib_src)
            with open(main_path, "w", encoding="utf-8") as handle:
                handle.write(main_src)
            out = run_file(main_path)
        self.assertEqual(out, "free 1\nbox 2\nbox 2\n")


class DeeperScenarioTests(unittest.TestCase):
    """Added during verification, beyond the spec's own test list: static
    calls from nested frame levels (static_address depth arithmetic),
    re-entrant `Printable` (invoke_sync nested inside itself, and running
    control flow/print inside it), and method calls interleaved with
    async suspension."""

    def test_static_call_three_frame_levels_deep(self):
        src = """
        struct P { v }
        impl P { fn new(v) { Self { v: v } } fn get(self) { self.v } }
        fn outer(a) { fn inner(b) { fn innermost() { P.new(a + b).get() } innermost() } inner(10) }
        print(outer(1))
        """
        self.assertEqual(run_source(src), "11\n")

    def test_static_call_from_closure_inside_method(self):
        src = """
        struct P { v }
        impl P {
            fn new(v) { P { v: v } }
            fn plus(self, k) { let f = fn(x) { P.new(self.v + x) }; f(k) }
        }
        print(P.new(1).plus(2).v)
        """
        self.assertEqual(run_source(src), "3\n")

    def test_recursive_printable_through_string_concat(self):
        src = """
        enum Tree { Leaf { v }, Node { l, r } }
        impl Printable for Tree {
            fn to_string(self) {
                match self {
                    Tree.Leaf { v } => { "" + v }
                    Tree.Node { l, r } => { "(" + l + " " + r + ")" }
                }
            }
        }
        print(Tree.Node { l: Tree.Leaf { v: 1 }, r: Tree.Node { l: Tree.Leaf { v: 2 }, r: Tree.Leaf { v: 3 } } })
        """
        self.assertEqual(run_source(src), "(1 (2 3))\n")

    def test_to_string_with_print_loop_break_continue_and_method_call(self):
        src = """
        struct P { n }
        impl P { fn start(self) { self.n } }
        impl Printable for P {
            fn to_string(self) {
                print("formatting")
                let s = ""
                let i = self.start()
                while i < 10 { i = i + 1; if i == 2 { continue } if i > 4 { break } s = s + i }
                s
            }
        }
        print(P { n: 0 })
        print("after")
        """
        self.assertEqual(run_source(src), "formatting\n134\nafter\n")

    def test_trait_default_method_on_builtin_type(self):
        src = """
        trait Greet { fn name(self) fn greet(self) { "hi " + self.name() } }
        impl Greet for Number { fn name(self) { "num" + self } }
        print(5.greet())
        """
        self.assertEqual(run_source(src), "hi num5\n")

    def test_methods_in_interleaved_detached_tasks(self):
        src = """
        struct W { }
        impl W { fn slow(self, v) { sleep_async(20); v * 2 } }
        fn job(v) { W {}.slow(v) + 1 }
        let a = detach job(1)
        let b = detach job(10)
        print(a.await + b.await)
        """
        self.assertEqual(run_source(src), "24\n")

    def test_printable_inside_task_that_suspends_between_prints(self):
        src = """
        struct P { }
        impl Printable for P { fn to_string(self) { "P" } }
        fn job() { print(P {}); sleep_async(5); print("x" + P {}); 1 }
        let t = detach job()
        print("main")
        print(t.await)
        """
        self.assertEqual(run_source(src), "P\nmain\nxP\n1\n")

    def test_top_level_defer_uses_hoisted_printable_impl(self):
        src = """
        struct P { }
        impl Printable for P { fn to_string(self) { "P!" } }
        defer print(P {})
        print("start")
        """
        self.assertEqual(run_source(src), "start\nP!\n")


if __name__ == "__main__":
    unittest.main()
