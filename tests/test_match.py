"""End-to-end behavioral tests for M4: `match` statements and patterns
(literal, wildcard, binding, struct, enum, `some`/`none`) -- see
docs/TESTING.md and docs/V2_DESIGN.md's M4 milestone.

`match` is a statement in M4, not yet an expression (that's M5's "blocks as
expressions" job) -- these tests only ever call `match` for its side
effects (`print`, `return`, `break`), never try to bind its result.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support import run_source


class LiteralAndWildcardTests(unittest.TestCase):
    def test_literal_number_patterns_and_wildcard(self):
        src = """
        fn describe(n) {
            match n {
                0 => { print("zero") }
                1 => { print("one") }
                _ => { print("many") }
            }
        }
        describe(0)
        describe(1)
        describe(5)
        """
        self.assertEqual(run_source(src), "zero\none\nmany\n")

    def test_bare_binding_pattern_captures_whole_value(self):
        src = """
        match 42 {
            x => { print(x) }
        }
        """
        self.assertEqual(run_source(src), "42\n")

    def test_boolean_and_string_literal_patterns(self):
        src = """
        match true {
            true => { print("yes") }
            false => { print("no") }
        }
        match "hi" {
            "hi" => { print("greeting") }
            _ => { print("other") }
        }
        """
        self.assertEqual(run_source(src), "yes\ngreeting\n")


class StructAndEnumPatternTests(unittest.TestCase):
    def test_struct_pattern_shorthand_and_explicit_subpattern(self):
        src = """
        struct Point { x, y }
        let p = Point { x: 1, y: 2 }
        match p {
            Point { x, y } => { print(x); print(y) }
        }
        match p {
            Point { x: 1, y } => { print(y) }
            _ => { print("other") }
        }
        """
        self.assertEqual(run_source(src), "1\n2\n2\n")

    def test_enum_pattern_struct_shaped_and_unit_variant(self):
        src = """
        enum Shape { Circle { r }, Empty }
        fn describe_shape(s) {
            match s {
                Shape.Circle { r } => { print(r) }
                Shape.Empty => { print("empty") }
            }
        }
        describe_shape(Shape.Circle { r: 5 })
        describe_shape(Shape.Empty)
        """
        self.assertEqual(run_source(src), "5\nempty\n")

    def test_some_none_matching(self):
        src = """
        fn half(n) {
            if n % 2 == 0 { return some(n // 2) }
            return none
        }
        fn describe(opt) {
            match opt {
                some(v) => { print(v) }
                none => { print("nothing") }
            }
        }
        describe(half(10))
        describe(half(7))
        """
        self.assertEqual(run_source(src), "5\nnothing\n")

    def test_deeply_nested_pattern(self):
        src = """
        struct Point { x, y }
        enum Shape { Circle { center, r } }
        let s = Shape.Circle { center: Point { x: 3, y: 4 }, r: 10 }
        match s {
            Shape.Circle { center: Point { x, y }, r } => { print(x); print(y); print(r) }
        }
        """
        self.assertEqual(run_source(src), "3\n4\n10\n")

    def test_recursion_enums_and_pattern_matching_linked_list_sum(self):
        # The payoff: this specific composition (branch on which enum
        # variant a recursively-built value holds) was impossible before
        # M4, since M3 alone had no way to branch on an enum's variant.
        src = """
        enum List { Cons { value, next }, Nil }
        fn make_list(n) {
            if n == 0 { return List.Nil }
            return List.Cons { value: n, next: make_list(n - 1) }
        }
        fn sum_list(lst) {
            match lst {
                List.Cons { value, next } => { return value + sum_list(next) }
                List.Nil => { return 0 }
            }
        }
        print(sum_list(make_list(5)))
        """
        self.assertEqual(run_source(src), "15\n")


class MatchControlFlowTests(unittest.TestCase):
    def test_non_exhaustive_match_raises_clean_runtime_error(self):
        src = """
        match 5 {
            1 => { print("one") }
        }
        """
        with self.assertRaises(Exception):
            run_source(src)

    def test_break_continue_inside_match_arm_target_enclosing_loop(self):
        src = """
        let i = 0
        while i < 5 {
            match i {
                3 => { break }
                _ => { }
            }
            i = i + 1
        }
        print(i)
        """
        self.assertEqual(run_source(src), "3\n")

    def test_scrutinee_struct_literal_ambiguity_requires_parens(self):
        src = """
        struct Flag { on }
        match (Flag { on: true }.on) {
            true => { print("good") }
            false => { print("bad") }
        }
        """
        self.assertEqual(run_source(src), "good\n")


class MatchValidationTests(unittest.TestCase):
    def test_struct_pattern_missing_field_raises(self):
        src = """
        struct Point { x, y }
        let p = Point { x: 1, y: 2 }
        match p {
            Point { x } => { }
        }
        """
        with self.assertRaises(Exception):
            run_source(src)

    def test_struct_pattern_unknown_field_raises(self):
        src = """
        struct Point { x, y }
        let p = Point { x: 1, y: 2 }
        match p {
            Point { x, y, z: 1 } => { }
        }
        """
        with self.assertRaises(Exception):
            run_source(src)

    def test_struct_pattern_undeclared_struct_type_raises(self):
        src = """
        let p = 1
        match p {
            NoSuchStruct { x } => { }
        }
        """
        with self.assertRaises(Exception):
            run_source(src)

    def test_enum_pattern_undeclared_enum_type_raises(self):
        src = """
        let x = 1
        match x {
            NoSuchEnum.Foo => { }
        }
        """
        with self.assertRaises(Exception):
            run_source(src)

    def test_enum_pattern_undeclared_variant_raises(self):
        src = """
        enum Shape { Circle { r } }
        match (Shape.Circle { r: 1 }) {
            Shape.NoSuchVariant => { }
        }
        """
        with self.assertRaises(Exception):
            run_source(src)


if __name__ == "__main__":
    unittest.main()
