"""End-to-end behavioral tests for M2: `struct` declarations, struct
literals, and field read/write (reference semantics, chained field access,
the if/while struct-literal-ambiguity restriction, and compile-time
literal validation vs. runtime access validation). See
docs/TESTING.md and docs/V2_DESIGN.md's M2 milestone.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support import run_source


class BasicStructTests(unittest.TestCase):
    def test_declaration_literal_and_field_reads(self):
        src = """
        struct Point { x, y }
        let p = Point { x: 1, y: 2 }
        print(p.x)
        print(p.y)
        """
        self.assertEqual(run_source(src), "1\n2\n")

    def test_field_mutation_visible_through_alias(self):
        src = """
        struct Point { x, y }
        let p = Point { x: 1, y: 2 }
        let q = p
        q.x = 100
        print(p.x)
        """
        self.assertEqual(run_source(src), "100\n")

    def test_nested_struct_literals_and_chained_field_access(self):
        src = """
        struct Point { x, y }
        struct Line { start, end }
        let l = Line { start: Point { x: 0, y: 0 }, end: Point { x: 1, y: 1 } }
        print(l.start.x)
        print(l.end.y)
        """
        self.assertEqual(run_source(src), "0\n1\n")

    def test_structs_interop_with_closures_and_functions(self):
        src = """
        struct Point { x, y }
        fn make_point(x, y) { return Point { x: x, y: y } }
        fn add_points(a, b) { return Point { x: a.x + b.x, y: a.y + b.y } }
        let p1 = make_point(1, 2)
        let p2 = make_point(3, 4)
        let p3 = add_points(p1, p2)
        print(p3.x)
        print(p3.y)
        """
        self.assertEqual(run_source(src), "4\n6\n")

    def test_nested_field_assignment_two_levels_deep(self):
        src = """
        struct Inner { v }
        struct Outer { inner }
        let o = Outer { inner: Inner { v: 1 } }
        o.inner.v = 99
        print(o.inner.v)
        """
        self.assertEqual(run_source(src), "99\n")

    def test_field_access_binds_tighter_than_unary_minus_and_pow(self):
        src = """
        struct P { x }
        let p = P { x: 3 }
        print(-p.x)
        print(p.x ** 2)
        """
        self.assertEqual(run_source(src), "-3\n9\n")

    def test_print_whole_struct(self):
        src = """
        struct Point { x, y }
        let p = Point { x: 1, y: 2 }
        print(p)
        """
        self.assertEqual(run_source(src), "Point { x: 1, y: 2 }\n")


class StructLiteralAmbiguityTests(unittest.TestCase):
    def test_bare_struct_literal_in_if_condition_is_a_syntax_error(self):
        src = """
        struct Flag { on }
        if Flag { on: true } { print("bad") }
        """
        with self.assertRaises(Exception):
            run_source(src)

    def test_parenthesized_struct_literal_in_if_condition_works(self):
        src = """
        struct Flag { on }
        if (Flag { on: true }.on) { print("good") }
        """
        self.assertEqual(run_source(src), "good\n")

    def test_plain_identifier_condition_still_works(self):
        src = """
        let x = true
        if x { print("hi") }
        """
        self.assertEqual(run_source(src), "hi\n")


class StructLiteralValidationTests(unittest.TestCase):
    def test_missing_required_field_raises(self):
        src = """
        struct Point { x, y }
        let p = Point { x: 1 }
        """
        with self.assertRaises(Exception):
            run_source(src)

    def test_extra_unknown_field_raises(self):
        src = """
        struct Point { x, y }
        let p = Point { x: 1, y: 2, z: 3 }
        """
        with self.assertRaises(Exception):
            run_source(src)

    def test_duplicate_field_in_literal_raises(self):
        src = """
        struct Point { x, y }
        let p = Point { x: 1, x: 2 }
        """
        with self.assertRaises(Exception):
            run_source(src)

    def test_duplicate_field_in_declaration_raises(self):
        src = "struct Point { x, x }"
        with self.assertRaises(Exception):
            run_source(src)

    def test_redeclaring_struct_name_raises(self):
        src = """
        struct Point { x, y }
        struct Point { a, b }
        """
        with self.assertRaises(Exception):
            run_source(src)

    def test_undeclared_struct_type_in_literal_raises(self):
        src = "let p = NoSuchStruct { x: 1 }"
        with self.assertRaises(Exception):
            run_source(src)


class RuntimeFieldAccessErrorTests(unittest.TestCase):
    def test_unknown_field_read_raises(self):
        src = """
        struct Point { x, y }
        let p = Point { x: 1, y: 2 }
        print(p.z)
        """
        with self.assertRaises(Exception):
            run_source(src)

    def test_field_access_on_non_struct_value_raises(self):
        src = """
        let x = 5
        print(x.foo)
        """
        with self.assertRaises(Exception):
            run_source(src)

    def test_field_assignment_to_unknown_field_raises(self):
        src = """
        struct Point { x, y }
        let p = Point { x: 1, y: 2 }
        p.z = 5
        """
        with self.assertRaises(Exception):
            run_source(src)


if __name__ == "__main__":
    unittest.main()
