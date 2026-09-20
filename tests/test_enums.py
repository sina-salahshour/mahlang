"""End-to-end behavioral tests for M3: `enum` declarations (unit + struct-
shaped variants), enum literal construction (`Type.Variant` /
`Type.Variant { ... }`), and the built-in `Option` type (`none`/`some(x)`)
unified with M1's `NONE_VALUE` singleton. See docs/TESTING.md and
docs/V2_DESIGN.md's M3 milestone.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support import run_source


class BasicEnumTests(unittest.TestCase):
    def test_unit_variant_construction_and_printing(self):
        src = """
        enum Color { Red, Green, Blue }
        let c = Color.Red
        print(c)
        """
        self.assertEqual(run_source(src), "Color.Red\n")

    def test_struct_shaped_variant_construction_and_field_read(self):
        src = """
        enum Shape { Circle { r }, Square { s } }
        let c = Shape.Circle { r: 5 }
        print(c.r)
        """
        self.assertEqual(run_source(src), "5\n")

    def test_mixed_unit_and_struct_shaped_variants_printing(self):
        src = """
        enum Shape { Circle { r }, Empty }
        let a = Shape.Circle { r: 2 }
        let b = Shape.Empty
        print(a)
        print(b)
        """
        self.assertEqual(run_source(src), "Shape.Circle { r: 2 }\nShape.Empty\n")

    def test_enums_interop_with_closures_and_functions(self):
        src = """
        enum Shape { Circle { r } }
        fn area(s) { return 3 * s.r * s.r }
        print(area(Shape.Circle { r: 2 }))
        """
        self.assertEqual(run_source(src), "12\n")

    def test_field_mutation_on_enum_instance(self):
        src = """
        let b = some(1)
        b.value = 2
        print(b.value)
        """
        self.assertEqual(run_source(src), "2\n")

    def test_struct_and_enum_may_share_a_name(self):
        src = """
        struct Point { x }
        enum Point { A }
        let p1 = Point { x: 1 }
        let p2 = Point.A
        print(p1.x)
        print(p2)
        """
        self.assertEqual(run_source(src), "1\nPoint.A\n")


class OptionTests(unittest.TestCase):
    def test_none_and_some_construction_printing_and_field_read(self):
        src = """
        let a = none
        let b = some(42)
        print(a)
        print(b)
        print(b.value)
        """
        self.assertEqual(run_source(src), "none\nsome(42)\n42\n")

    def test_none_is_falsy_some_is_always_truthy(self):
        src = """
        if none { print("bad1") } else { print("none is falsy") }
        if some(false) { print("some is truthy") } else { print("bad2") }
        if some(0) { print("some zero is truthy too") } else { print("bad3") }
        """
        self.assertEqual(
            run_source(src),
            "none is falsy\nsome is truthy\nsome zero is truthy too\n",
        )

    def test_implicit_none_return_is_byte_identical_to_m1(self):
        src = """
        fn nothing() { }
        print(nothing())
        """
        self.assertEqual(run_source(src), "none\n")


class EnumValidationTests(unittest.TestCase):
    def test_struct_shaped_variant_literal_missing_field_raises(self):
        src = """
        enum Shape { Circle { r } }
        let c = Shape.Circle { }
        """
        with self.assertRaises(Exception):
            run_source(src)

    def test_struct_shaped_variant_literal_unknown_field_raises(self):
        src = """
        enum Shape { Circle { r } }
        let c = Shape.Circle { r: 1, z: 2 }
        """
        with self.assertRaises(Exception):
            run_source(src)

    def test_duplicate_field_in_variant_literal_raises(self):
        src = """
        enum Shape { Circle { r } }
        let c = Shape.Circle { r: 1, r: 2 }
        """
        with self.assertRaises(Exception):
            run_source(src)

    def test_duplicate_field_in_variant_declaration_raises(self):
        src = "enum Shape { Circle { r, r } }"
        with self.assertRaises(Exception):
            run_source(src)

    def test_duplicate_variant_name_in_declaration_raises(self):
        src = "enum Shape { Circle { r }, Circle { s } }"
        with self.assertRaises(Exception):
            run_source(src)

    def test_undeclared_enum_type_in_literal_raises(self):
        src = "let x = NoSuchEnum.Foo"
        with self.assertRaises(Exception):
            run_source(src)

    def test_assigning_to_a_bare_unit_variant_raises_cleanly(self):
        # `Shape.Empty` (no braces) is a bare enum unit-variant
        # construction, not a reference to anything -- it can never be a
        # valid assignment target. Must fail with a clean compile-time
        # error, not a raw Python TypeError from codegen trying to read an
        # address that was never resolved (see docs/V2_DESIGN.md's M3
        # milestone).
        src = """
        enum Shape { Empty }
        Shape.Empty = 5
        """
        with self.assertRaises(Exception):
            run_source(src)

    def test_undeclared_variant_of_declared_enum_raises(self):
        src = """
        enum Shape { Circle { r } }
        let x = Shape.NoSuchVariant
        """
        with self.assertRaises(Exception):
            run_source(src)

    def test_struct_shaped_variant_without_braces_raises(self):
        src = """
        enum Shape { Circle { r } }
        let x = Shape.Circle
        """
        with self.assertRaises(Exception):
            run_source(src)

    def test_redeclaring_enum_name_raises(self):
        src = """
        enum Shape { A }
        enum Shape { B }
        """
        with self.assertRaises(Exception):
            run_source(src)

    def test_redeclaring_builtin_option_raises(self):
        src = "enum Option { Foo }"
        with self.assertRaises(Exception):
            run_source(src)


if __name__ == "__main__":
    unittest.main()
