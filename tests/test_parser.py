"""Unit tests for compiler/parser.py's AST shape -- these pin down the
precedence/associativity chain and the `fn`-desugaring rule described in
docs/V2_DESIGN.md's M1 milestone, independent of running any code (see
test_language.py for behavioral/end-to-end coverage of the same rules).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from compiler.ast_nodes import (
    Binary,
    BindPat,
    BoolLit,
    Call,
    EnumPat,
    ExprStmt,
    FnExpr,
    Ident,
    LetStmt,
    MatchStmt,
    NumberLit,
    StructPat,
    Unary,
    WildcardPat,
)
from compiler.lexer import Lexer
from compiler.parser import Parser


def parse(source: str):
    return Parser(Lexer(source)).parse_program()


def parse_expr(source: str):
    """Parse a single expression by wrapping it in `print(...)` and
    pulling the argument back out -- reuses the real statement grammar
    instead of poking at parser internals."""
    (stmt,) = parse(f"print({source})")
    (expr,) = stmt.args
    return expr


class PrecedenceTests(unittest.TestCase):
    def test_mul_binds_tighter_than_add(self):
        expr = parse_expr("1 + 2 * 3")
        self.assertIsInstance(expr, Binary)
        self.assertEqual(expr.op, "+")
        self.assertIsInstance(expr.rhs, Binary)
        self.assertEqual(expr.rhs.op, "*")

    def test_pow_is_right_associative(self):
        expr = parse_expr("2 ** 3 ** 2")
        self.assertEqual(expr.op, "**")
        self.assertIsInstance(expr.rhs, Binary)  # rhs is itself `3 ** 2`
        self.assertEqual(expr.rhs.op, "**")

    def test_unary_minus_binds_looser_than_pow(self):
        expr = parse_expr("-2 ** 2")
        self.assertIsInstance(expr, Unary)
        self.assertIsInstance(expr.operand, Binary)  # negates the whole `2 ** 2`
        self.assertEqual(expr.operand.op, "**")


class FnDesugaringTests(unittest.TestCase):
    def test_named_fn_statement_desugars_to_let_of_fn_expr(self):
        (stmt,) = parse("fn add(a, b) { return a + b }")
        self.assertIsInstance(stmt, LetStmt)
        self.assertEqual(stmt.name, "add")
        self.assertIsInstance(stmt.value, FnExpr)
        self.assertEqual(stmt.value.params, ["a", "b"])

    def test_anonymous_fn_statement_is_expr_stmt(self):
        (stmt,) = parse("fn(x) { return x }")
        self.assertIsInstance(stmt, ExprStmt)
        self.assertIsInstance(stmt.value, FnExpr)
        self.assertIsNone(stmt.value.name)

    def test_fn_as_expression_position(self):
        (stmt,) = parse("let add = fn(a, b) { return a + b }")
        self.assertIsInstance(stmt, LetStmt)
        self.assertIsInstance(stmt.value, FnExpr)
        self.assertIsNone(stmt.value.name)  # anonymous here -- LetStmt carries the name


class MatchParsingTests(unittest.TestCase):
    def test_match_arms_need_no_comma_separator(self):
        (stmt,) = parse(
            """
            match n {
                0 => { print(0) }
                _ => { print(1) }
            }
            """
        )
        self.assertIsInstance(stmt, MatchStmt)
        self.assertEqual(len(stmt.arms), 2)
        self.assertIsInstance(stmt.arms[0].pattern, NumberLit)
        self.assertIsInstance(stmt.arms[1].pattern, WildcardPat)

    def test_wildcard_vs_bind_pattern(self):
        (stmt,) = parse("match n { _ => { } }")
        self.assertIsInstance(stmt.arms[0].pattern, WildcardPat)

        (stmt,) = parse("match n { x => { } }")
        self.assertIsInstance(stmt.arms[0].pattern, BindPat)
        self.assertEqual(stmt.arms[0].pattern.name, "x")

    def test_literal_patterns_reuse_expression_literal_nodes(self):
        (stmt,) = parse("match n { true => { } }")
        self.assertIsInstance(stmt.arms[0].pattern, BoolLit)
        self.assertTrue(stmt.arms[0].pattern.value)

    def test_struct_pattern_shorthand_field_desugars_to_bind_pat(self):
        (stmt,) = parse("match p { Point { x } => { } }")
        pattern = stmt.arms[0].pattern
        self.assertIsInstance(pattern, StructPat)
        self.assertEqual(pattern.type_name, "Point")
        (field_name, sub) = pattern.fields[0]
        self.assertEqual(field_name, "x")
        self.assertIsInstance(sub, BindPat)
        self.assertEqual(sub.name, "x")

    def test_struct_pattern_explicit_field_pattern(self):
        (stmt,) = parse("match p { Point { x: 1 } => { } }")
        pattern = stmt.arms[0].pattern
        (field_name, sub) = pattern.fields[0]
        self.assertEqual(field_name, "x")
        self.assertIsInstance(sub, NumberLit)

    def test_enum_pattern_unit_variant_has_no_fields(self):
        (stmt,) = parse("match s { Shape.Empty => { } }")
        pattern = stmt.arms[0].pattern
        self.assertIsInstance(pattern, EnumPat)
        self.assertEqual(pattern.type_name, "Shape")
        self.assertEqual(pattern.variant, "Empty")
        self.assertEqual(pattern.fields, [])

    def test_some_none_desugar_to_option_enum_pat(self):
        (stmt,) = parse("match opt { some(v) => { } }")
        pattern = stmt.arms[0].pattern
        self.assertIsInstance(pattern, EnumPat)
        self.assertEqual(pattern.type_name, "Option")
        self.assertEqual(pattern.variant, "some")
        (field_name, sub) = pattern.fields[0]
        self.assertEqual(field_name, "value")
        self.assertIsInstance(sub, BindPat)

        (stmt,) = parse("match opt { none => { } }")
        pattern = stmt.arms[0].pattern
        self.assertEqual((pattern.type_name, pattern.variant, pattern.fields), ("Option", "none", []))


class CallTests(unittest.TestCase):
    def test_call_callee_is_an_ident_node(self):
        expr = parse_expr("foo(1, 2)")
        self.assertIsInstance(expr, Call)
        self.assertIsInstance(expr.callee, Ident)
        self.assertEqual(expr.callee.name, "foo")
        self.assertEqual(len(expr.args), 2)


if __name__ == "__main__":
    unittest.main()
