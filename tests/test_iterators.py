"""End-to-end behavioral tests for M17 part A: ranges (`1..10`, `1..=10`,
`1..`, `..10`, `..=10`), range patterns in `match`, and the `Iterable`/
`Iterator` prelude (`map`/`filter`/`skip`/`take`/`reduce`, String
iteration, `String.len`/`String.char_at`/`Function.arity`) -- see the M17
spec and docs/MAHC_FORMAT.md's version-1.2 notes.

Uses `run_source`/`compile_program`/`parse_source` like the other
behavioral test files (never re-plumbs the pipeline directly).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mah.bytecode.decode import decode
from mah.bytecode.disasm import disassemble
from mah.bytecode.encode import encode
from mah.bytecode.format import MahcFormatError
from mah.bytecode.program import FunctionDecl, Instr, Program
from mah.preprocessor import PRELUDE_TRIGGERS, preprocess, PRELUDE_PATH
from mah.compiler.lexer import Lexer
from mah.compiler.parser import Parser
from tests.support import compile_program, run_source


class RangeValueTests(unittest.TestCase):
    def test_ranges_print(self):
        src = 'print(5..10)\nprint(5..=10)\nprint(1..)\nprint(..10)\nprint(..=10)'
        self.assertEqual(run_source(src), "5..10\n5..=10\n1..\n..10\n..=10\n")

    def test_range_fields(self):
        src = "let r = 2..5\nprint(r.start, r.end, r.inclusive)"
        self.assertEqual(run_source(src), "2 5 false\n")

    def test_precedence_range_end_is_additive(self):
        src = "let n = 3\nprint(1..n + 1)"
        self.assertEqual(run_source(src), "1..4\n")

    def test_range_destructures_in_match(self):
        src = "match 1..5 { Range { start, end, inclusive } => { print(start + end) } }"
        self.assertEqual(run_source(src), "6\n")


class RangePatternTests(unittest.TestCase):
    def test_describe_ranges(self):
        src = """
        fn describe(x) {
            match x {
                1..10 => { "between one and 10" }
                10..15 => { "between 10 and 15" }
                ..1 => { "less than one" }
                15.. => { "15 or more" }
            }
        }
        print(describe(1))
        print(describe(9.5))
        print(describe(10))
        print(describe(14))
        print(describe(15))
        print(describe(0))
        print(describe(-3))
        print(describe(100))
        """
        expected = (
            "between one and 10\n"
            "between one and 10\n"
            "between 10 and 15\n"
            "between 10 and 15\n"
            "15 or more\n"
            "less than one\n"
            "less than one\n"
            "15 or more\n"
        )
        self.assertEqual(run_source(src), expected)

    def test_inclusive_ranges(self):
        src = """
        fn g(x) { match x { 1..=5 => { "low" } ..=10 => { "mid-or-below" } _ => { "high" } } }
        print(g(5)); print(g(6)); print(g(10)); print(g(11)); print(g(0))
        """
        self.assertEqual(run_source(src), "low\nmid-or-below\nmid-or-below\nhigh\nmid-or-below\n")

    def test_negative_bounds_and_literals(self):
        src = """
        fn h(x) { match x { -10..-5 => { "a" } -5 => { "b" } -4..=0 => { "c" } _ => { "d" } } }
        print(h(-10)); print(h(-6)); print(h(-5)); print(h(-1)); print(h(0)); print(h(1))
        """
        self.assertEqual(run_source(src), "a\na\nb\nc\nc\nd\n")

    def test_string_ranges(self):
        src = """
        fn k(c) { match c { "a"..="m" => { "first half" } "n"..="z" => { "second half" } _ => { "other" } } }
        print(k("c")); print(k("z")); print(k("A"))
        """
        self.assertEqual(run_source(src), "first half\nsecond half\nother\n")

    def test_type_mismatch_never_matches_or_raises(self):
        src = 'match "5" { 1..10 => { print("num") } _ => { print("not a number") } }'
        self.assertEqual(run_source(src), "not a number\n")
        src2 = 'match true { 0..=1 => { print("x") } _ => { print("bool isn\'t a number") } }'
        self.assertEqual(run_source(src2), "bool isn't a number\n")

    def test_range_pattern_nests_inside_other_patterns(self):
        src = (
            'match some(7) { some(1..5) => { print("small") } '
            'some(5..) => { print("big") } none => { print("none") } }'
        )
        self.assertEqual(run_source(src), "big\n")


class IterationTests(unittest.TestCase):
    def test_reduce_basic(self):
        self.assertEqual(run_source("print((1..5).reduce(fn(a, b) { a + b }))"), "10\n")
        self.assertEqual(run_source("print((1..=5).reduce(fn(a, b) { a + b }))"), "15\n")

    def test_reduce_with_initial_and_index(self):
        src = "print((1..4).reduce(fn(acc, v, i) { acc + v * i }, 100))"
        self.assertEqual(run_source(src), "108\n")

    def test_reduce_without_initial_starts_index_at_one(self):
        src = 'print((5..8).reduce(fn(acc, v, i) { acc + "," + v + "@" + i }))'
        self.assertEqual(run_source(src), "5,6@1,7@2\n")

    def test_reduce_of_empty_iterable(self):
        self.assertEqual(run_source("print((5..5).reduce(fn(a, b) { a + b }))"), "none\n")
        self.assertEqual(run_source("print((5..5).reduce(fn(a, b) { a + b }, 0))"), "0\n")

    def test_map_filter_with_and_without_index(self):
        self.assertEqual(
            run_source('print((1..6).map(fn(x) { x * 10 }).reduce(fn(a, b) { a + " " + b }, ""))'),
            " 10 20 30 40 50\n",
        )
        self.assertEqual(
            run_source('print((1..6).filter(fn(v, i) { i % 2 == 0 }).reduce(fn(a, b) { a + "" + b }, ""))'),
            "135\n",
        )
        self.assertEqual(
            run_source("print((1..6).map(fn(v, i) { v * i }).reduce(fn(a, b) { a + b }))"),
            "40\n",
        )

    def test_skip_take_and_infinite_ranges(self):
        self.assertEqual(
            run_source('print((1..).skip(3).take(4).reduce(fn(a, b) { a + "," + b }))'), "4,5,6,7\n"
        )
        self.assertEqual(
            run_source("print((1..).filter(fn(x) { x % 7 == 0 }).take(3).reduce(fn(a, b) { a + b }))"),
            "42\n",
        )
        self.assertEqual(
            run_source('print((1..10).take(0).reduce(fn(a, b) { a + b }, "empty"))'), "empty\n"
        )
        self.assertEqual(
            run_source('print((1..3).skip(10).reduce(fn(a, b) { a + b }, "empty"))'), "empty\n"
        )

    def test_laziness(self):
        src = """
        let calls = 0
        let m = (1..).map(fn(x) { calls = calls + 1; x })
        print(calls)
        print(m.take(3).reduce(fn(a, b) { a + b }))
        print(calls)
        """
        self.assertEqual(run_source(src), "0\n6\n3\n")

    def test_restartable(self):
        src = """
        let evens = (1..10).filter(fn(x) { x % 2 == 0 })
        print(evens.reduce(fn(a, b) { a + b }))
        print(evens.reduce(fn(a, b) { a + b }))
        """
        self.assertEqual(run_source(src), "20\n20\n")

    def test_manual_iteration(self):
        src = """
        let it = (1..3).iter()
        print(it.next())
        print(it.next())
        print(it.next())
        """
        self.assertEqual(run_source(src), "some(1)\nsome(2)\nnone\n")

    def test_strings(self):
        self.assertEqual(
            run_source('print("hello".map(fn(c) { c + c }).reduce(fn(a, b) { a + b }, ""))'),
            "hheelllloo\n",
        )
        self.assertEqual(run_source('print("héllo".len())'), "5\n")
        self.assertEqual(run_source('print("abc".char_at(1))'), "b\n")
        self.assertEqual(
            run_source('print("banana".filter(fn(c) { c == "a" }).reduce(fn(n, c) { n + 1 }, 0))'),
            "3\n",
        )
        self.assertEqual(run_source('print("".reduce(fn(a, b) { a + b }))'), "none\n")

    def test_user_iterable_gets_adapters(self):
        src = """
        struct Countdown { from }
        struct CountdownIter { n }
        impl Iterable for Countdown { fn iter(self) { CountdownIter { n: self.from } } }
        impl Iterator for CountdownIter {
            fn next(self) { if self.n > 0 { let v = self.n; self.n = self.n - 1; some(v) } else { none } }
        }
        print(Countdown { from: 5 }.map(fn(x) { x * 2 }).reduce(fn(a, b) { a + "," + b }))
        """
        self.assertEqual(run_source(src), "10,8,6,4,2\n")

    def test_inherent_method_wins_over_iterable_default(self):
        src = """
        struct X { }
        impl Iterable for X { fn iter(self) { X {} } }
        impl Iterator for X { fn next(self) { none } }
        impl X { fn take(self, n) { "mine" } }
        print(X {}.take(2))
        """
        self.assertEqual(run_source(src), "mine\n")

    def test_function_arity(self):
        src = 'fn two(a, b) { a }\nfn dflt(a, b = 1) { a }\nprint(two.arity(), dflt.arity())'
        self.assertEqual(run_source(src), "2 2\n")

    def test_async_inside_map_callback(self):
        src = """
        fn slow(x) { sleep_async(1); x * 2 }
        fn run() { (1..4).map(fn(x) { slow(x) }).reduce(fn(a, b) { a + b }) }
        print((detach run()).await)
        """
        self.assertEqual(run_source(src), "12\n")


class ErrorTests(unittest.TestCase):
    def test_to_range_is_not_iterable(self):
        with self.assertRaisesRegex(Exception, r"'ToRange' has no method 'iter'"):
            run_source("(..5).iter()")

    def test_number_has_no_map(self):
        with self.assertRaisesRegex(Exception, r"'Number' has no method 'map'"):
            run_source("5.map(fn(x) { x })")

    def test_char_at_errors(self):
        with self.assertRaisesRegex(
            Exception, r"char_at index 5 is out of range for a String of length 3"
        ):
            run_source('"abc".char_at(5)')
        with self.assertRaisesRegex(Exception, r"char_at index must be a Number, got String"):
            run_source('"abc".char_at("x")')
        with self.assertRaisesRegex(Exception, r"method 'len' accepts 0 arguments but 1 was given"):
            run_source('"abc".len(1)')

    def test_map_callback_arity_mismatch(self):
        with self.assertRaisesRegex(Exception, r"accepts 3 arguments but 2 was given"):
            run_source("(1..3).map(fn(a, b, c) { a }).reduce(fn(x, y) { x })")

    def test_orphan_rules(self):
        with self.assertRaisesRegex(
            Exception, r"Cannot implement built-in trait 'Iterable' for built-in type 'Number'"
        ):
            run_source("impl Iterable for Number { fn iter(self) { 1 } }")
        with self.assertRaisesRegex(Exception, r"Cannot define inherent methods on built-in type 'Range'"):
            run_source("impl Range { fn f(self) { 1 } }")
        # Post-M17 review: a program that never uses ranges/iterators may
        # declare its own `Range` (declaring a name isn't a use of the
        # prelude, so the prelude isn't included and nothing clashes). Using
        # ranges too is a clear "built-in name" error at the user's
        # declaration. (Previously any `struct Range` was rejected.)
        self.assertEqual(run_source("struct Range { a }\nprint(Range { a: 1 }.a)"), "1\n")
        with self.assertRaisesRegex(Exception, r"'Range' is a built-in name"):
            run_source("struct Range { a }\nprint(1..2)")
        src = """
        trait Mine { fn m(self) }
        impl Mine for Range { fn m(self) { self.start } }
        print((3..4).m())
        """
        self.assertEqual(run_source(src), "3\n")

    def test_error_in_user_callback_is_located_in_user_code(self):
        with self.assertRaises(Exception) as cm:
            run_source("(1..3).map(fn(x) { x.nope() }).reduce(fn(a, b) { a })")
        message = str(cm.exception)
        self.assertIn("'Number' has no method 'nope'", message)
        self.assertIn("at position #1:", message)

    def test_error_inside_prelude_code_is_located_in_the_prelude(self):
        src = """
        struct Z { }
        impl Iterable for Z { fn iter(self) { 5 } }
        Z {}.map(fn(x) { x }).reduce(fn(a, b) { a })
        """
        with self.assertRaises(Exception) as cm:
            run_source(src)
        message = str(cm.exception)
        self.assertIn("'Number' has no method 'next'", message)
        self.assertIn("at position <prelude>#", message)


class SyntaxErrorTests(unittest.TestCase):
    def _errors(self, src: str):
        parser = Parser(Lexer(src))
        parser.parse_program()
        return parser.errors

    def test_bare_dotdot_needs_end_value(self):
        errors = self._errors("let x = ..")
        self.assertTrue(any("needs an end value" in msg for msg, _pos in errors))

    def test_dangling_dotdot_eq_needs_end_value(self):
        errors = self._errors("let x = 1..=")
        self.assertTrue(any("'..=' needs an end value" in msg for msg, _pos in errors))

    def test_ranges_cannot_chain(self):
        errors = self._errors("let x = 1..2..3")
        self.assertTrue(any("can't be chained" in msg for msg, _pos in errors))

    def test_bare_dotdot_eq_pattern_is_an_error(self):
        errors = self._errors("match 1 { ..=  => { 1 } }")
        self.assertTrue(errors)

    def test_range_pattern_bound_must_be_a_literal(self):
        errors = self._errors("match 1 { a..5 => { 1 } }")
        self.assertTrue(errors)

    def test_range_pattern_bounds_must_agree_in_type(self):
        with self.assertRaisesRegex(
            Exception, r"Range pattern bounds must be both Numbers or both Strings"
        ):
            run_source('match 1 { 1.."a" => { 1 } }')


class PreludePlumbingTests(unittest.TestCase):
    def test_program_without_prelude_use_gets_no_prelude(self):
        program = compile_program(text='print("hi")')
        type_names = {program.strings[t.name] for t in program.types}
        self.assertNotIn("Range", type_names)
        file_names = (
            {program.strings[i] for i in program.debug.files} if program.debug is not None else set()
        )
        self.assertNotIn("<prelude>", file_names)

    def test_program_using_a_range_gets_the_prelude(self):
        program = compile_program(text="print(1..2)")
        type_names = {program.strings[t.name] for t in program.types}
        self.assertIn("Range", type_names)
        file_names = {program.strings[i] for i in program.debug.files}
        self.assertIn("<prelude>", file_names)

    def test_field_named_map_still_triggers_the_over_approximation(self):
        # Over-approximation is fine -- a struct FIELD happening to be
        # named `map` (nothing to do with the prelude's `Iterable.map`)
        # still includes the prelude; the program still runs correctly.
        src = "struct S { map }\nlet s = S { map: 5 }\nprint(s.map)"
        self.assertEqual(run_source(src), "5\n")

    def test_prelude_triggers_contain_the_expected_names(self):
        for name in ("Iterable", "Iterator", "map", "reduce", "Range", "FromRange"):
            self.assertIn(name, PRELUDE_TRIGGERS)

    def test_prelude_declares_only_hoisted_item_kinds(self):
        with open(PRELUDE_PATH, encoding="utf-8") as f:
            source = f.read()
        parser = Parser(Lexer(source))
        program = parser.parse_program()
        self.assertEqual(parser.errors, [])
        allowed = {"StructDecl", "EnumDecl", "TraitDecl", "ImplDecl"}
        for stmt in program:
            self.assertIn(type(stmt).__name__, allowed)

    def test_round_trip_with_ranges_and_range_patterns(self):
        program = compile_program(
            text='match 1..10 { 1..5 => { print("a") } _ => { print("b") } }'
        )
        self.assertEqual(program.minor, 2)
        self.assertEqual(decode(encode(program)), program)

    def test_hand_built_minor_1_program_with_matchrange_is_rejected(self):
        fns = [FunctionDecl(0, 1, 0, None, params=[])]
        code = [
            Instr("matchrange", ((0, 0), (0, 0), None, False, (0, 0))),
            Instr("halt", ()),
        ]
        program = Program(
            strings=[], constants=[], types=[], natives=[], functions=fns, code=code, debug=None, minor=1
        )
        with self.assertRaises(MahcFormatError):
            decode(encode(program))


class StringLiteralDecodingTests(unittest.TestCase):
    """Added during verification: M17 fixed raw non-ASCII text in string
    literals (`"héllo".len()` is 5), and escapes must keep working next to
    it, including `\\u`/`\\x` ones (an intermediate fix crashed on them)."""

    def test_raw_non_ascii_and_escapes_together(self):
        src = 'print("caf\\u00e9", "\\x41\\tB", "h\u00e9llo".len(), "\u20ac\\n".len())'
        self.assertEqual(run_source(src), "caf\u00e9 A\tB 5 2\n")

    def test_backslash_before_non_ascii_is_kept(self):
        self.assertEqual(run_source('print("a\\\u20acb")'), "a\\\u20acb\n")


class RangeNewlineRuleTests(unittest.TestCase):
    """Added during verification: Mah has no significant newlines, so an
    open-ended `1..` at the end of a line must not swallow the next line.
    Rule: a range's end value must start on the operator's own line."""

    def test_open_range_at_end_of_line_leaves_next_line_alone(self):
        src = "fn foo(x) { x }\nlet f = 1..\nprint(foo(f))\nlet g = 2..\nlet r = 1..foo(3)\nprint(g, r)"
        self.assertEqual(run_source(src), "1..\n2.. 1..3\n")

    def test_prefix_range_end_on_next_line_is_an_error_with_location(self):
        from mah.compiler.driver import compile_to_program

        with self.assertRaises(SyntaxError) as cm:
            compile_to_program(text="let h = ..\n10\n")
        self.assertIn("needs an end value at position #1:9", str(cm.exception))


class PreludeNameAndStringRangeTests(unittest.TestCase):
    """Added during the post-M17 correctness review."""

    def test_own_type_named_like_a_prelude_type_works_without_iterators(self):
        # declaring and using your own `Taken` isn't a use of the prelude
        self.assertEqual(run_source("struct Taken { x }\nprint(Taken { x: 1 }.x)"), "1\n")
        self.assertEqual(run_source("trait Iterable { fn a(self) }\nprint(2)"), "2\n")

    def test_own_type_named_like_a_prelude_type_with_iterators_is_a_clear_error(self):
        with self.assertRaises(Exception) as cm:
            run_source("struct Taken { x }\nprint((1..3).take(1).reduce(fn(a, b) { a }))")
        self.assertIn("'Taken' is a built-in name (declared by the prelude", str(cm.exception))
        self.assertIn("at position 7", str(cm.exception))  # the user's declaration, not the prelude's

    def test_string_ranges_match_but_cannot_be_iterated(self):
        self.assertEqual(run_source('print(match "q" { "a"..="m" => { "first" } _ => { "second" } })'), "second\n")
        for src in ('("a".."c").take(2).reduce(fn(a, b) { a })', '("a"..).take(2).reduce(fn(a, b) { a })'):
            with self.subTest(src=src):
                with self.assertRaises(Exception) as cm:
                    run_source(src)
                self.assertIn("only_ranges_of_numbers_can_be_iterated", str(cm.exception))

    def test_decimal_ranges_count_by_one(self):
        self.assertEqual(run_source('print((0.5..3).reduce(fn(a, b) { a + "," + b }))'), "0.5,1.5,2.5\n")


if __name__ == "__main__":
    unittest.main()
