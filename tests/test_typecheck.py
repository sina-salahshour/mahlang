"""M22: the core type checker (mah/compiler/typecheck.py, mah/compiler/
types.py) -- inference, assignability, operators, generalization,
structs/enums, control flow, patterns, calls, and the implicit-Unknown
reporting of the explicit level. See docs/TYPES.md.

Everything here runs the real front end (preprocess -> lex -> parse ->
resolve) and then the checker directly, and asserts on the diagnostics
(message and line) and on the types the checker inferred for declarations.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mah.bytecode.lower import line_col  # noqa: E402
from mah.compiler.lexer import Lexer  # noqa: E402
from mah.compiler.parser import Parser  # noqa: E402
from mah.compiler.resolve import Resolver  # noqa: E402
from mah.compiler.typecheck import Checker, check_program, reportable  # noqa: E402
from mah.compiler.types import (  # noqa: E402
    NUMBER,
    STRING,
    TCon,
    TFn,
    TParam,
    TUnknown,
    TVar,
    Unifier,
    show,
)
from mah.preprocessor import preprocess  # noqa: E402
from tests.support import EXAMPLES_DIR, compile_bytes  # noqa: E402


def _front_end(src: str):
    pp = preprocess(None, src)
    parser = Parser(Lexer(pp.text))
    program = parser.parse_program()
    assert not parser.errors, parser.errors
    resolver = Resolver(prelude_start=pp.prelude_start)
    resolver.resolve_program(program)
    return pp, program, resolver


def check(src: str):
    """(diagnostics as (kind, message, line) triples, {name: [type, ...]})
    -- every declaration's inferred type, by name, in source order."""
    pp, program, resolver = _front_end(src)
    checker = Checker(resolver)
    diagnostics = checker.check(program)
    out = [(d.kind, d.message, line_col(pp.text, d.position)[0]) for d in diagnostics]
    types: dict = {}
    for position, t in sorted(checker.decl_types.items()):
        if pp.prelude_start is not None and position >= pp.prelude_start:
            continue
        symbol = resolver.position_index.get(position)
        # A method name isn't a variable: take its name from the source.
        name = symbol.name if symbol is not None else pp.text[position:].split("(")[0].split()[0]
        types.setdefault(name, []).append(show(t))
    return out, types


class _Base(unittest.TestCase):
    def assertClean(self, src: str):
        diagnostics, _ = check(src)
        self.assertEqual(diagnostics, [])

    def assertMismatch(self, src: str, message: str, line: int):
        diagnostics, _ = check(src)
        mismatches = [(m, l) for k, m, l in diagnostics if k == "mismatch"]
        self.assertIn((message, line), mismatches, f"got {diagnostics}")

    def assertTypes(self, src: str, **expected):
        diagnostics, types = check(src)
        for name, t in expected.items():
            self.assertIn(name, types, f"no declaration named {name}")
            self.assertEqual(types[name][-1], t, f"type of {name}")


class UnifierTests(unittest.TestCase):
    def test_bind_and_rollback(self):
        u = Unifier()
        v = TVar(1)
        mark = u.mark()
        self.assertTrue(u.unify(v, TCon("Vector", [NUMBER])))
        self.assertEqual(show(v), "Vector<Number>")
        u.rollback(mark)
        self.assertEqual(show(v), "?")

    def test_failed_structure_leaves_nothing_bound(self):
        u = Unifier()
        a, b = TVar(1), TVar(1)
        mark = u.mark()
        # Map<a, b> vs Map<Number, ...> binds `a`, then fails on the second
        # argument; rolling back unbinds `a` too.
        ok = u.unify(TCon("Map", [a, STRING]), TCon("Map", [NUMBER, NUMBER]))
        self.assertFalse(ok)
        u.rollback(mark)
        self.assertEqual(show(a), "?")
        self.assertEqual(show(b), "?")

    def test_occurs_check(self):
        u = Unifier()
        v = TVar(1)
        self.assertFalse(u.unify(v, TCon("Vector", [v])))

    def test_assignability_rules(self):
        u = Unifier()
        none, never = TCon("None"), TCon("Never")
        self.assertTrue(u.assign(none, NUMBER))
        self.assertTrue(u.assign(never, STRING))
        self.assertTrue(u.assign(TUnknown("explicit"), NUMBER))
        self.assertTrue(u.assign(NUMBER, TUnknown("explicit")))
        self.assertFalse(u.assign(NUMBER, STRING))
        # Option/Promise are covariant: Option<None> fits Option<Number>.
        self.assertTrue(u.assign(TCon("Option", [none]), TCon("Option", [NUMBER])))
        # Vector is invariant.
        self.assertFalse(u.assign(TCon("Vector", [none]), TCon("Vector", [NUMBER])))
        # A callback may take fewer parameters than expected, but not more
        # required ones.
        self.assertTrue(u.assign(TFn([NUMBER], NUMBER), TFn([NUMBER, NUMBER], NUMBER)))
        self.assertFalse(u.assign(TFn([NUMBER, NUMBER], NUMBER), TFn([NUMBER], NUMBER)))
        self.assertTrue(u.assign(TFn([NUMBER, NUMBER], NUMBER, required=1), TFn([NUMBER], NUMBER)))
        t = TParam("T")
        self.assertTrue(u.assign(t, t))
        self.assertFalse(u.assign(t, TParam("T")))

    def test_none_does_not_bind_a_variable(self):
        u = Unifier()
        v = TVar(1)
        self.assertTrue(u.assign(TCon("None"), v))
        self.assertIsNone(v.ref)
        self.assertTrue(v.none_seen)
        self.assertTrue(u.assign(NUMBER, v))
        self.assertEqual(show(v), "Number")

    def test_show(self):
        self.assertEqual(show(TFn([NUMBER], TCon("None"))), "fn(Number)")
        self.assertEqual(show(TFn([], STRING)), "fn() -> String")
        self.assertEqual(show(TCon("Map", [STRING, TCon("Vector", [NUMBER])])), "Map<String, Vector<Number>>")


class InferenceTests(_Base):
    def test_literals_and_lets(self):
        self.assertTypes(
            'let a = 1\nlet b = "s"\nlet c = true\nlet d = [1, 2]\nlet e = ["k": 1]\nlet r = 1..3',
            a="Number", b="String", c="Bool", d="Vector<Number>", e="Map<String, Number>", r="Range",
        )

    def test_parameters_inferred_from_operators(self):
        self.assertTypes("fn add(a, b) { a + b }", add="fn(Number, Number) -> Number")

    def test_parameters_inferred_from_calls(self):
        self.assertTypes(
            "fn add(a: Number, b: Number) -> Number { a + b }\nfn g(x, y) { add(x, y) }",
            g="fn(Number, Number) -> Number",
        )

    def test_string_concatenation(self):
        self.assertTypes('fn greet(name) { "hi " + name }', greet="fn(T) -> String")
        self.assertTypes('fn cat(a: String, b) { a + b }', cat="fn(String, T) -> String")

    def test_comparison_defaults_to_number(self):
        self.assertTypes("fn lt(a, b) { a < b }", lt="fn(Number, Number) -> Bool")
        self.assertTypes('fn before_z(a) { a < "z" }', before_z="fn(String) -> Bool")

    def test_string_repetition(self):
        self.assertTypes('fn rep(n) { "ab" * n }', rep="fn(Number) -> String")

    def test_generalization(self):
        self.assertTypes(
            'fn id(x) { x }\nlet n = id(1)\nlet s = id("s")',
            id="fn(T) -> T", n="Number", s="String",
        )
        self.assertTypes("fn mk() { [] }", mk="fn() -> Vector<T>")
        self.assertTypes(
            "fn twice(f, x) { f(f(x)) }\nlet t = twice(fn(n) { n + 1 }, 0)",
            twice="fn(fn(T) -> T, T) -> T", t="Number",
        )

    def test_generic_instances_are_independent(self):
        src = 'fn mk() { [] }\nlet a = mk()\na = [1]\nlet b = mk()\nb = ["s"]'
        self.assertClean(src)
        self.assertTypes(src, a="Vector<Number>", b="Vector<String>")

    def test_local_let_polymorphism(self):
        self.assertClean(
            'fn f() {\n let id = fn(x) { x }\n let a = id(1)\n let b = id("s")\n a\n}'
        )

    def test_closure_capture_is_not_generalized(self):
        self.assertTypes(
            "fn outer(z) {\n let inner = fn() { z + 1 }\n inner()\n}",
            outer="fn(Number) -> Number",
        )

    def test_recursion(self):
        self.assertTypes(
            "fn fact(n) { if n <= 1 { 1 } else { n * fact(n - 1) } }",
            fact="fn(Number) -> Number",
        )

    def test_functions_checked_on_demand(self):
        # The method is checked first (source order) and calls `b`, which
        # is then checked, and generalized, on the spot.
        self.assertTypes(
            "struct C { }\nimpl C {\n fn m(self, x) { b(x) + 1 }\n}\nfn b(y) { y }",
            m="fn(C, Number) -> Number", b="fn(T) -> T",
        )

    def test_none_leaves_a_let_open(self):
        src = "let best = none\nbest = 5"
        self.assertClean(src)
        self.assertTypes(src, best="Number")
        self.assertMismatch(
            'let x = none\nx = 5\nx = "s"', "Type mismatch assigning to 'x': expected Number, found String", 3
        )

    def test_none_is_assignable_to_anything(self):
        self.assertClean('let n: Number = none\nfn f() -> String { return none }\nlet v: Vector<Number> = [none]')

    def test_function_returning_only_none(self):
        self.assertTypes("fn p() { print(1) }", p="fn()")
        self.assertTypes("fn q(b) { if b { return none } }", q="fn(T)")

    def test_top_level_let_used_by_a_method(self):
        src = "struct C { }\nimpl C {\n fn get(self) { LIMIT + 1 }\n}\nlet LIMIT = 10"
        self.assertClean(src)
        self.assertTypes(src, LIMIT="Number", get="fn(C) -> Number")

    def test_async(self):
        self.assertTypes(
            "fn slow() { 42 }\nlet p = detach slow()\nlet v = p.await\nlet t = detach sleep_async(1)",
            p="Promise<Number>", v="Number", t="Promise<None>",
        )

    def test_builtin_expressions(self):
        self.assertTypes("let s = sin(1)\nlet i = input()", s="Number", i="Number")
        self.assertMismatch('let s = sin("x")', "Type mismatch: expected Number, found String", 1)


class MismatchTests(_Base):
    def test_variables_cant_change_type(self):
        self.assertMismatch('let z = 1\nz = "no"', "Type mismatch assigning to 'z': expected Number, found String", 2)

    def test_shadowing_is_a_new_variable(self):
        self.assertClean('let x = 1\nlet x = "s" + x')

    def test_annotation_mismatch(self):
        self.assertMismatch('let x: Number = "s"', "Type mismatch: expected Number, found String", 1)

    def test_return_mismatch(self):
        self.assertMismatch(
            'fn r() -> Number { return "no" }', "Type mismatch in the returned value: expected Number, found String", 1
        )
        self.assertMismatch(
            'fn r() -> Number { "no" }', "Type mismatch in the returned value: expected Number, found String", 1
        )

    def test_generic_parameter_is_rigid(self):
        self.assertMismatch(
            "fn bad<T>(x: T) -> T { 1 }", "Type mismatch in the returned value: expected T, found Number", 1
        )

    def test_declared_generics(self):
        self.assertTypes(
            'fn first<T>(v: Vector<T>) -> T { v[0] }\nlet s = first(["a"])', s="String"
        )

    def test_argument_mismatch(self):
        diagnostics, _ = check('fn add(a, b) { a + b }\nlet bad = add("a", 1)')
        self.assertEqual(
            diagnostics, [("mismatch", "Type mismatch in an argument: expected Number, found String", 2)]
        )

    def test_operators(self):
        self.assertMismatch('let e = "a" - 1', "Type mismatch in the left operand of '-': expected Number, found String", 1)
        self.assertMismatch("let e = true + 1", "Can't apply '+' to Bool and Number", 1)
        self.assertMismatch('let e = 1 < "a"', "Can't apply '<' to Number and String", 1)
        self.assertMismatch('let e = -"a"', "Type mismatch in the negated value: expected Number, found String", 1)
        self.assertClean('let a = "a" + 1\nlet b = 1 + "a"\nlet c = "ab" * 3\nlet d = !5\nlet e = 1 == "a"')

    def test_pending_operator_meets_later_constraint(self):
        # `x + 1` waits (x could be a String); x is then passed to a
        # String parameter, so the sum is a String -- no error.
        self.assertTypes(
            'fn s(t: String) { t }\nfn f(x) {\n let y = x + 1\n s(x)\n y\n}', f="fn(String) -> String"
        )

    def test_mixed_vector_and_map_literals(self):
        self.assertMismatch('let v = [1, "a"]', "Type mismatch in a Vector element: expected Number, found String", 1)
        self.assertMismatch('let m = [1: "a", "b": "c"]', "Type mismatch in a Map key: expected Number, found String", 1)

    def test_one_error_per_mistake(self):
        # The offending argument is reported once; the call's result is
        # still the function's return type, so nothing cascades.
        diagnostics, types = check('fn add(a, b) { a + b }\nlet bad = add("a", 2)\nlet c = bad * 2')
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(types["c"], ["Number"])


class CallTests(_Base):
    def test_keyword_arguments(self):
        self.assertClean("fn k(a, b = 2) { a + b }\nlet x = k(1, b: 3)\nlet y = k(b: 3, a: 1)\nlet z = k(1)")
        self.assertMismatch("fn k(a, b = 2) { a + b }\nk(1, c: 3)", "No parameter named 'c'", 2)
        self.assertMismatch("fn k(a, b = 2) { a + b }\nk(1, a: 3)", "Argument 'a' is given twice", 2)
        self.assertMismatch('fn k(a, b = 2) { a + b }\nk(1, b: "s")', "Type mismatch in an argument: expected Number, found String", 2)

    def test_arity(self):
        self.assertMismatch("fn k(a, b = 2) { a + b }\nk()", "Missing argument 'a'", 2)
        self.assertMismatch("fn k(a, b = 2) { a + b }\nk(1, 2, 3)", "Too many arguments: expected at most 2, found 3", 2)

    def test_default_values_are_checked(self):
        self.assertMismatch('fn k(a: Number = "s") { a }', "Type mismatch: expected Number, found String", 1)

    def test_not_callable(self):
        self.assertMismatch("let x = 5\nx()", "Number is not a function", 2)

    def test_expected_type_flows_into_closures(self):
        self.assertTypes(
            "fn apply(f: fn(Number) -> Number, x) { f(x) }\nlet a = apply(fn(y) { y + 1 }, 2)", y="Number"
        )
        self.assertMismatch(
            'fn apply(f: fn(Number) -> Number, x) { f(x) }\nlet a = apply(fn(y) { y + "a" }, 2)',
            "Type mismatch in the returned value: expected Number, found String",
            2,
        )

    def test_closures_checked_after_other_arguments(self):
        # `x` binds T to String before the closure is checked, so `s` is a
        # String inside it and `s * 2` is a repetition, not arithmetic.
        self.assertTypes(
            'fn map1<T, U>(f: fn(T) -> U, x: T) -> U { f(x) }\nlet r = map1(fn(s) { s * 2 }, "ab")',
            s="String", r="String",
        )

    def test_calling_an_unknown_parameter_infers_its_type(self):
        self.assertTypes("fn call(f) { f(1) + 1 }", call="fn(fn(Number) -> Number) -> Number")

    def test_annotated_function_value(self):
        self.assertTypes('let f: fn(Number) -> String = fn(n) { "" + n }', n="Number")
        self.assertMismatch(
            "let f: fn(Number) -> String = fn(n) { n }",
            "Type mismatch in the returned value: expected String, found Number",
            1,
        )


class StructEnumTests(_Base):
    def test_fields_inferred_program_wide(self):
        src = "struct P { x, y }\nlet p = P { x: 1, y: 2 }\nlet px = p.x"
        self.assertTypes(src, p="P", px="Number")

    def test_field_sites_disagree(self):
        diagnostics, types = check('struct P { x }\nlet a = P { x: 1 }\nlet b = P { x: "s" }\nlet c = b.x')
        self.assertIn(
            (
                "mismatch",
                "Field 'x' of 'P' was inferred as Number elsewhere, but is given String here; annotate the field",
                3,
            ),
            diagnostics,
        )
        # The field is Unknown from then on, and the explicit level asks
        # for an annotation on it.
        self.assertEqual(types["c"], ["Unknown"])
        self.assertIn(("implicit", "Can't infer the type of field 'x' of 'P'; annotate it", 1), diagnostics)

    def test_generic_struct(self):
        self.assertTypes("struct Box<T> { v: T }\nlet b = Box { v: 1 }", b="Box<Number>")
        self.assertMismatch(
            "struct Box<T> { v: T }\nlet b = Box { v: 1 }\nlet s: String = b.v",
            "Type mismatch: expected String, found Number", 3,
        )

    def test_field_assignment(self):
        self.assertMismatch(
            'struct P { x: Number }\nlet p = P { x: 1 }\np.x = "s"', "Type mismatch: expected Number, found String", 3
        )
        self.assertMismatch("struct P { x: Number }\nlet p = P { x: 1 }\np.z = 5", "'P' has no field 'z'", 3)

    def test_field_read_infers_the_struct(self):
        self.assertTypes("struct P { x: Number }\nfn getx(o) { o.x }", getx="fn(P) -> Number")

    def test_field_read_on_an_enum_variant_field(self):
        self.assertTypes(
            "enum Shape { Circle { r: Number }, Empty }\nfn area(c) { 3 * c.r * c.r }", area="fn(Shape) -> Number"
        )

    def test_field_on_non_struct(self):
        self.assertMismatch("let x = 5\nprint(x.foo)", "Number has no field 'foo'", 2)

    def test_enums_and_option(self):
        self.assertTypes(
            "enum E<T> { A { v: T }, B }\nlet a = E.A { v: 1 }\nlet b = E.B\nlet o = some(1)",
            a="E<Number>", b="E<?>", o="Option<Number>",
        )
        self.assertClean("fn half(n) { if n % 2 == 0 { some(n / 2) } else { none } }")
        self.assertTypes("fn half(n) { if n % 2 == 0 { some(n / 2) } else { none } }", half="fn(Number) -> Option<Number>")

    def test_self_in_impl(self):
        src = "struct R { w: Number, h: Number }\nimpl R {\n fn new(w, h) -> Self { Self { w: w, h: h } }\n fn area(self) { self.w * self.h }\n}"
        self.assertClean(src)
        self.assertTypes(src, new="fn(Number, Number) -> R", area="fn(R) -> Number")

    def test_impl_body_errors_are_reported(self):
        self.assertMismatch(
            'struct R { w: Number }\nimpl R {\n fn bad(self) { self.w - "s" }\n}',
            "Type mismatch in the right operand of '-': expected Number, found String", 3,
        )

    def test_native_method_calls_are_typed(self):
        diagnostics, types = check('let v = [1]\nlet n = v.len()\nv.push(-"a")')
        self.assertEqual(types["n"], ["Number"])
        self.assertEqual(
            diagnostics, [("mismatch", "Type mismatch in the negated value: expected Number, found String", 3)]
        )


class MethodTests(_Base):
    def test_inherent_methods_static_calls_and_self(self):
        self.assertTypes(
            "struct Board { c1 }\n"
            "impl Board {\n"
            '    fn new() { Self { c1: "" } }\n'
            "    fn get(self, i) { match i { 1 => { self.c1 } _ => { \"\" } } }\n"
            "}\n"
            "let board = Board.new()\n"
            "let cell = board.get(1)",
            new="fn() -> Board",
            get="fn(Board, Number) -> String",
            board="Board",
            cell="String",
        )

    def test_generic_impl_instantiates_per_call(self):
        self.assertTypes(
            "struct Pair<T> { a: T, b: T }\n"
            "impl<T> Pair<T> {\n"
            "    fn first(self) { self.a }\n"
            "    fn make(x: T) { Pair { a: x, b: x } }\n"
            "}\n"
            "let f = Pair { a: 1, b: 2 }.first()\n"
            'let g = Pair.make("s").first()',
            first="fn(Pair<T>) -> T",
            f="Number",
            g="String",
        )

    def test_trait_impl_and_trait_path_call(self):
        self.assertTypes(
            "trait Shape { fn area(self) -> Number }\n"
            "struct Sq { s }\n"
            "impl Shape for Sq { fn area(self) { self.s * self.s } }\n"
            "let a = Sq { s: 2 }.area()\n"
            "let t = Shape.area(Sq { s: 3 })",
            a="Number",
            t="Number",
        )

    def test_native_methods(self):
        self.assertTypes(
            'let v = [1, 2]\nlet x = v.pop()\nlet m = ["a": 1]\nlet ks = m.keys()\n'
            'let s = "abc".len()\nlet str = 5.to_string()\nlet c = v.copy()',
            x="Number",
            ks="Vector<String>",
            s="Number",
            str="String",
            c="Vector<Number>",
        )

    def test_field_closure_call(self):
        self.assertTypes("struct C { f }\nlet c = C { f: fn(x) { x + 1 } }\nlet r = c.f(2)", r="Number")

    def test_mutually_recursive_methods(self):
        self.assertTypes(
            "struct A { n }\n"
            "impl A {\n"
            "    fn even(self, k) { if k == 0 { true } else { self.odd(k - 1) } }\n"
            "    fn odd(self, k) { if k == 0 { false } else { self.even(k - 1) } }\n"
            "}\n"
            "let e = A { n: 1 }.even(4)",
            even="fn(A, Number) -> Bool",
            e="Bool",
        )

    def test_receiver_inferred_from_unique_method_name(self):
        self.assertTypes(
            "struct B { w }\n"
            'impl B { fn winner(self) { "X" } }\n'
            "fn check(b) { b.winner() }",
            check="fn(B) -> String",
        )

    def test_ambiguous_method_name_leaves_receiver_open(self):
        self.assertTypes(
            "struct P { x }\nstruct Q { y }\n"
            "impl P { fn size(self) { 1 } }\nimpl Q { fn size(self) { 2 } }\n"
            "fn check(b) { b.size() }",
            check="fn(T) -> Unknown",
        )

    def test_method_argument_mismatch(self):
        self.assertMismatch(
            'struct A { n }\nimpl A { fn go(self, k: Number) { k } }\nA { n: 1 }.go("x")',
            "Type mismatch in an argument: expected Number, found String",
            3,
        )


class ControlFlowTests(_Base):
    def test_if_branches(self):
        self.assertTypes("let a = if true { 1 } else { 2 }\nlet b = if true { 1 }", a="Number", b="Number")

    def test_if_branches_disagreeing_is_not_an_error(self):
        diagnostics, types = check('let e = if true { 1 } else { "s" }')
        self.assertEqual(types["e"], ["Unknown"])
        self.assertEqual(diagnostics, [("implicit", "Can't infer the type of 'e'; annotate it", 1)])

    def test_if_statement_branches_dont_join(self):
        # Used as a statement, the branches' values are thrown away, so
        # they don't constrain anything.
        self.assertTypes('fn f(x) {\n if true { x } else { 1 }\n none\n}', f="fn(T)")

    def test_diverging_branch(self):
        self.assertTypes("fn f(c) -> Number {\n let x = if c { 1 } else { return 0 }\n x\n}", x="Number")

    def test_loops(self):
        self.assertTypes("let w = while true { break 5 }", w="Number")
        self.assertMismatch(
            'let w = while true {\n if true { break 5 }\n break "s"\n}',
            "Type mismatch in the loop's break value: expected Number, found String", 3,
        )

    def test_endless_loop_never_returns(self):
        self.assertClean("fn f() -> Number { while true { return 1 } }")

    def test_for_over_builtins(self):
        self.assertTypes(
            'for let a in [1] { }\nfor let b in "xy" { }\nfor let c in 1..3 { }\nfor let d, let i in ["k": true] { }',
            a="Number", b="String", c="Number", d="String", i="Number",
        )
        self.assertMismatch("for let v in 5 { }", "Number can't be iterated", 1)
        self.assertMismatch('for let v: String in [1] { }', "Type mismatch in the loop variable: expected String, found Number", 1)

    def test_indexing(self):
        self.assertTypes(
            'let v = [1]\nlet a = v[0]\nlet m = ["k": true]\nlet b = m["k"]\nlet c = "xy"[0]\nlet d = v[0..1]',
            a="Number", b="Bool", c="String", d="Vector<Number>",
        )
        self.assertMismatch('let v = [1]\nprint(v["a"])', "Type mismatch in the index: expected Number, found String", 2)
        self.assertMismatch('let v = [1]\nv[0] = "s"', "Type mismatch: expected Number, found String", 2)
        self.assertMismatch('let s = "ab"\ns[0] = "c"', "String doesn't support index assignment", 2)


class PatternTests(_Base):
    def test_bindings_get_field_types(self):
        self.assertTypes(
            "struct P { x: Number }\nlet p = P { x: 1 }\nmatch p { P { x: n } => { print(n) } }\nmatch some(\"s\") { some(v) => { } none => { } }",
            n="Number", v="String",
        )

    def test_pattern_mismatch(self):
        self.assertMismatch('match 5 { "a" => { } _ => { } }', "Pattern of type String can't match a value of type Number", 1)
        self.assertMismatch(
            "struct P { x: Number }\nstruct Q { }\nlet p = P { x: 1 }\nmatch p { Q { } => { } _ => { } }",
            "Pattern 'Q' can't match a value of type P", 4,
        )

    def test_pattern_infers_parameter(self):
        self.assertTypes(
            "enum E { A { v: Number }, B }\nfn f(e) { match e { E.A { v } => { v } E.B => { 0 } } }",
            f="fn(E) -> Number",
        )

    def test_match_value(self):
        self.assertTypes('let r = match 3 { 1 => { "one" } _ => { "many" } }', r="String")


class M25ErrorCheckerTests(_Base):
    """M25 (docs/ERRORS.md): minimal checker support for `throw`/`try`/
    `catch`/`throws` -- no error-set inference/checking (M26); just no
    diagnostics for the new expressions used where a type is expected."""

    def test_try_catch_used_as_a_typed_let_value(self):
        self.assertClean(
            "enum E { A }\nimpl Error for E {}\nlet x: Number = try { 1 } catch { _ => { 2 } }"
        )

    def test_throw_in_an_if_else_branch_used_as_a_typed_let_value(self):
        self.assertClean(
            "enum E { A }\nimpl Error for E {}\nlet y: Number = if true { 1 } else { throw E.A }"
        )

    def test_throw_has_type_never(self):
        self.assertTypes(
            "enum E { A }\nimpl Error for E {}\nfn f() -> Number { if true { 1 } else { throw E.A } }",
            f="fn() -> Number",
        )

    def test_type_test_arm_binds_the_named_type(self):
        self.assertTypes(
            "struct Oops { code: Number }\nimpl Error for Oops {}\n"
            "let r = try { 1 } catch { e: Oops => { e.code } }",
            r="Number",
        )

    def test_try_else_joins_body_and_fallback(self):
        self.assertClean("let n: Number = try 1 / 0 else 0")


class ExplicitLevelTests(_Base):
    def test_generalized_parameters_are_fine(self):
        diagnostics, _ = check("fn id(x) { x }")
        self.assertEqual(diagnostics, [])

    def test_uninferable_declaration(self):
        diagnostics, _ = check("let v = []")
        self.assertEqual(diagnostics, [("implicit", "Can't infer the type of 'v'; annotate it", 1)])

    def test_reported_once_per_hole(self):
        diagnostics, _ = check("let v = []\nlet w = v")
        self.assertEqual(diagnostics, [("implicit", "Can't infer the type of 'v'; annotate it", 1)])

    def test_explicit_unknown_is_allowed(self):
        diagnostics, types = check("let u: Unknown = 5\nlet u2 = u.anything")
        self.assertEqual(diagnostics, [])
        self.assertEqual(types["u2"], ["Unknown"])

    def test_levels(self):
        pp, program, resolver = _front_end('let v = []\nlet x: Number = "s"')
        diagnostics = check_program(program, resolver)
        self.assertEqual([d.kind for d in reportable(diagnostics, "loose")], ["mismatch"])
        self.assertEqual([d.kind for d in reportable(diagnostics, "strict")], ["mismatch"])
        self.assertEqual(sorted(d.kind for d in reportable(diagnostics, "explicit")), ["implicit", "mismatch"])
        self.assertTrue(diagnostics[0].text().endswith(f"at position {diagnostics[0].position}"))


class WholeProgramTests(_Base):
    def test_examples_have_no_type_errors(self):
        for name in sorted(os.listdir(EXAMPLES_DIR)):
            if not name.endswith(".mh"):
                continue
            path = os.path.join(EXAMPLES_DIR, name)
            with self.subTest(example=name):
                with open(path, encoding="utf-8") as f:
                    text = f.read()
                pp = preprocess(path, text)
                program = Parser(Lexer(pp.text)).parse_program()
                resolver = Resolver(prelude_start=pp.prelude_start)
                resolver.resolve_program(program)
                mismatches = reportable(check_program(program, resolver), "strict")
                self.assertEqual([d.message for d in mismatches], [])

    def test_checker_does_not_change_codegen(self):
        for name in sorted(os.listdir(EXAMPLES_DIR)):
            if not name.endswith(".mh"):
                continue
            path = os.path.join(EXAMPLES_DIR, name)
            with self.subTest(example=name):
                before = compile_bytes(path=path)
                with open(path, encoding="utf-8") as f:
                    text = f.read()
                pp = preprocess(path, text)
                program = Parser(Lexer(pp.text)).parse_program()
                resolver = Resolver(prelude_start=pp.prelude_start)
                resolver.resolve_program(program)
                check_program(program, resolver)
                from mah.bytecode.encode import encode
                from mah.bytecode.lower import lower
                from mah.compiler.codegen import Codegen

                after = encode(lower(Codegen(resolver.global_frame).generate(program), resolver, pp, target="debug"))
                self.assertEqual(before, after)

    def test_range_bounds_are_numbers(self):
        self.assertMismatch('let r = "a".."z"', "Type mismatch: expected Number, found String", 1)

    def test_prelude_diagnostics_are_never_reported(self):
        # The prelude's own bodies aren't checked yet (M23 annotates it),
        # and a range literal only touches its (annotated) fields.
        diagnostics, _ = check("for let i in 1..3 { print(i) }")
        self.assertEqual(diagnostics, [])


if __name__ == "__main__":
    unittest.main()
