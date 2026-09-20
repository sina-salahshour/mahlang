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
    Block,
    BoolLit,
    Call,
    EnumPat,
    ExprStmt,
    FnExpr,
    IfStmt,
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


def parse_match_stmt(source: str) -> MatchStmt:
    """Parse a program consisting of a single bare `match` and return its
    `MatchStmt` node. M5 note: since `match` now parses through the general
    expression grammar (so it can be used as an expression -- see
    docs/V2_DESIGN.md's M5 milestone), a bare `match` that is the last (here
    the *only*) item in a block with no trailing `;` is that block's *tail*
    -- and `parse_program` folds a top-level program's tail into a
    discarded `ExprStmt`, since nothing consumes a program's value. So the
    `MatchStmt` these tests inspect is now nested one level inside an
    `ExprStmt` when parsed bare like this; unwrap it here rather than in
    every test. This is a deliberate M5 AST-shape consequence, not a parser
    bug: `match n { ... }` followed immediately by more code (no semicolon)
    still parses `MatchStmt` directly into `stmts` unwrapped, exactly as
    M4 left it -- only the "sole trailing item" case changed."""
    (stmt,) = parse(source)
    if isinstance(stmt, ExprStmt):
        stmt = stmt.value
    assert isinstance(stmt, MatchStmt)
    return stmt


class MatchParsingTests(unittest.TestCase):
    def test_match_arms_need_no_comma_separator(self):
        stmt = parse_match_stmt(
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
        stmt = parse_match_stmt("match n { _ => { } }")
        self.assertIsInstance(stmt.arms[0].pattern, WildcardPat)

        stmt = parse_match_stmt("match n { x => { } }")
        self.assertIsInstance(stmt.arms[0].pattern, BindPat)
        self.assertEqual(stmt.arms[0].pattern.name, "x")

    def test_literal_patterns_reuse_expression_literal_nodes(self):
        stmt = parse_match_stmt("match n { true => { } }")
        self.assertIsInstance(stmt.arms[0].pattern, BoolLit)
        self.assertTrue(stmt.arms[0].pattern.value)

    def test_struct_pattern_shorthand_field_desugars_to_bind_pat(self):
        stmt = parse_match_stmt("match p { Point { x } => { } }")
        pattern = stmt.arms[0].pattern
        self.assertIsInstance(pattern, StructPat)
        self.assertEqual(pattern.type_name, "Point")
        (field_name, sub) = pattern.fields[0]
        self.assertEqual(field_name, "x")
        self.assertIsInstance(sub, BindPat)
        self.assertEqual(sub.name, "x")

    def test_struct_pattern_explicit_field_pattern(self):
        stmt = parse_match_stmt("match p { Point { x: 1 } => { } }")
        pattern = stmt.arms[0].pattern
        (field_name, sub) = pattern.fields[0]
        self.assertEqual(field_name, "x")
        self.assertIsInstance(sub, NumberLit)

    def test_enum_pattern_unit_variant_has_no_fields(self):
        stmt = parse_match_stmt("match s { Shape.Empty => { } }")
        pattern = stmt.arms[0].pattern
        self.assertIsInstance(pattern, EnumPat)
        self.assertEqual(pattern.type_name, "Shape")
        self.assertEqual(pattern.variant, "Empty")
        self.assertEqual(pattern.fields, [])

    def test_some_none_desugar_to_option_enum_pat(self):
        stmt = parse_match_stmt("match opt { some(v) => { } }")
        pattern = stmt.arms[0].pattern
        self.assertIsInstance(pattern, EnumPat)
        self.assertEqual(pattern.type_name, "Option")
        self.assertEqual(pattern.variant, "some")
        (field_name, sub) = pattern.fields[0]
        self.assertEqual(field_name, "value")
        self.assertIsInstance(sub, BindPat)

        stmt = parse_match_stmt("match opt { none => { } }")
        pattern = stmt.arms[0].pattern
        self.assertEqual((pattern.type_name, pattern.variant, pattern.fields), ("Option", "none", []))


class ExprBlockParsingTests(unittest.TestCase):
    """M5: narrow parser-layer coverage for the new block-item algorithm --
    see tests/test_expr_blocks.py for end-to-end behavioral coverage."""

    def test_bare_block_used_mid_block_needs_no_semicolon(self):
        # A block-shaped statement (if/match/bare block) not last in its
        # block is wrapped as ExprStmt(value=IfStmt(...)) -- see
        # docs/V2_DESIGN.md's M5 milestone: it needs no semicolon here
        # (this is the critical pre-M5 regression check), it just isn't a
        # bare top-level IfStmt any more the way M0-M4 emitted it.
        (if_stmt, print_stmt) = parse('if true { print("a") }\nprint("b")')
        self.assertIsInstance(if_stmt, ExprStmt)
        self.assertIsInstance(if_stmt.value, IfStmt)
        self.assertEqual(print_stmt.args[0].value, "b")

    def test_block_tail_populated_when_no_trailing_semicolon(self):
        (let_stmt,) = parse("let x = { let y = 1; y + 1 }")
        block = let_stmt.value
        self.assertIsInstance(block, Block)
        self.assertEqual(len(block.stmts), 1)
        self.assertIsInstance(block.tail, Binary)

    def test_block_tail_is_none_when_trailing_semicolon_present(self):
        (let_stmt,) = parse("let x = { let y = 1; y + 1; }")
        block = let_stmt.value
        self.assertEqual(len(block.stmts), 2)
        self.assertIsNone(block.tail)

    def test_if_is_parsed_as_a_primary_expression(self):
        (let_stmt,) = parse("let x = if true { 1 } else { 2 }")
        self.assertIsInstance(let_stmt.value, IfStmt)

    def test_bare_expression_statement_requires_semicolon_unless_last(self):
        with self.assertRaises(SyntaxError):
            parse('5 + 3\nprint("x")')

    def test_bare_expression_statement_allowed_with_explicit_semicolon(self):
        (expr_stmt, print_stmt) = parse('5 + 3;\nprint("x")')
        self.assertIsInstance(expr_stmt, ExprStmt)
        self.assertIsInstance(expr_stmt.value, Binary)


class CallTests(unittest.TestCase):
    def test_call_callee_is_an_ident_node(self):
        expr = parse_expr("foo(1, 2)")
        self.assertIsInstance(expr, Call)
        self.assertIsInstance(expr.callee, Ident)
        self.assertEqual(expr.callee.name, "foo")
        self.assertEqual(len(expr.args), 2)


if __name__ == "__main__":
    unittest.main()
