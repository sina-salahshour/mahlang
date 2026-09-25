"""Unit tests for compiler/parser.py's AST shape -- these pin down the
precedence/associativity chain and the `fn`-desugaring rule described in
docs/V2_DESIGN.md's M1 milestone, independent of running any code (see
test_language.py for behavioral/end-to-end coverage of the same rules).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mah.compiler.ast_nodes import (
    Binary,
    BindPat,
    Block,
    BoolLit,
    Call,
    DetachExpr,
    EnumDecl,
    EnumLit,
    EnumPat,
    ErrorNode,
    ExprStmt,
    FieldAccess,
    FnExpr,
    IfStmt,
    ImplDecl,
    Ident,
    LetStmt,
    MatchStmt,
    MethodCall,
    NumberLit,
    PrintStmt,
    RangePat,
    StructDecl,
    StructLit,
    StructPat,
    TraitDecl,
    Unary,
    WildcardPat,
)
from mah.compiler.lexer import Lexer
from mah.compiler.parser import Parser


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
        # M6 update (deliberate, not a bug being papered over -- see
        # docs/V2_DESIGN.md's M6 milestone): `parse()` (a bare
        # `parse_program()` call) no longer *raises* on this mistake --
        # the parser is now forgiving, so a missing-semicolon error is
        # collected into `parser.errors` instead of aborting the parse.
        # The underlying rule this test pins down is unchanged (a bare
        # expression statement not last in its block still needs a `;`);
        # only how the mistake is reported changed. Recovery here also
        # exercises a real edge case `_synchronize` has to get right: the
        # very next token (`print`) is itself a valid statement-leading
        # token, so recovery must leave it alone rather than discard it --
        # confirmed below by `print("x")` surviving as a real `PrintStmt`,
        # not swallowed as part of "skip to the next safe point."
        lexer = Lexer('5 + 3\nprint("x")')
        parser = Parser(lexer)
        program = parser.parse_program()  # must not raise
        self.assertEqual(len(parser.errors), 1)
        self.assertEqual(len(program), 2)
        self.assertIsInstance(program[0], ExprStmt)
        self.assertIsInstance(program[0].value, ErrorNode)
        self.assertIsInstance(program[1], PrintStmt)

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


class DetachParsingTests(unittest.TestCase):
    """M10: `detach`'s operand must already be a call expression -- see
    docs/V2_DESIGN.md's M10 milestone for why this is parsed by directly
    consuming `ID ( args )` rather than through the general expression
    grammar."""

    def test_detach_wraps_a_non_call_operand_in_a_closure(self):
        # Intentional change: `detach` used to reject any operand that
        # wasn't a call. Now a non-call operand is wrapped in a synthesized
        # zero-param closure whose call is what gets detached.
        lexer = Lexer("detach 5")
        parser = Parser(lexer)
        program = parser.parse_program()
        self.assertEqual(parser.errors, [])
        expr = program[0].value
        self.assertIsInstance(expr, DetachExpr)
        self.assertIsInstance(expr.call, Call)
        self.assertIsInstance(expr.call.callee, FnExpr)
        self.assertTrue(expr.call.callee.detached)
        self.assertEqual(expr.call.args, [])
        self.assertEqual(expr.call.callee.body.tail.value, 5)

    def test_detach_block_keeps_trailing_await_outside(self):
        program = Parser(Lexer("detach { 1 }.await")).parse_program()
        expr = program[0].value
        self.assertIsInstance(expr, FieldAccess)
        self.assertEqual(expr.field, "await")
        self.assertIsInstance(expr.obj, DetachExpr)
        self.assertIsInstance(expr.obj.call.callee, FnExpr)

    def test_detach_call_is_not_wrapped(self):
        program = Parser(Lexer("detach f(1)")).parse_program()
        expr = program[0].value
        self.assertIsInstance(expr.call, Call)
        self.assertEqual(expr.call.callee.name, "f")


class LspPositionFieldTests(unittest.TestCase):
    """Pin down the new AST position fields added for the LSP's
    struct/enum/variant hover and go-to-definition support -- see
    `compiler/ast_nodes.py`'s LSP note and `compiler/resolve.py`'s
    `type_position_index`."""

    def test_struct_decl_name_position_points_at_name(self):
        src = "struct Point { x, y }\n"
        (stmt,) = parse(src)
        self.assertIsInstance(stmt, StructDecl)
        self.assertEqual(stmt.name_position, src.index("Point"))

    def test_enum_decl_variant_positions_point_at_each_variant_name(self):
        src = "enum Shape { Circle { r }, Empty }\n"
        (stmt,) = parse(src)
        self.assertIsInstance(stmt, EnumDecl)
        self.assertEqual(len(stmt.variant_positions), 2)
        self.assertEqual(stmt.variant_positions[0], src.index("Circle"))
        self.assertEqual(stmt.variant_positions[1], src.index("Empty"))

    def test_enum_lit_type_name_position_points_at_type_while_position_stays_at_variant(self):
        src = "enum Shape { Circle { r }, Empty }\n"
        expr = parse_expr("Shape.Circle { r: 5 }")
        self.assertIsInstance(expr, EnumLit)
        # `.position` is unchanged from before -- still the *variant* name's
        # position within the wrapping `print(...)` expression text.
        wrapped = "print(Shape.Circle { r: 5 })"
        self.assertEqual(expr.position, wrapped.index("Circle"))
        self.assertEqual(expr.type_name_position, wrapped.index("Shape"))


class TraitParsingTests(unittest.TestCase):
    """M12: `trait`/`impl` declarations and method calls -- AST shape only
    (see tests/test_traits.py for end-to-end behavioral coverage)."""

    def test_trait_decl_with_required_and_default_methods(self):
        src = "trait T { fn a(self) fn b(self, x) { x } }"
        (stmt,) = parse(src)
        self.assertIsInstance(stmt, TraitDecl)
        self.assertEqual(stmt.name, "T")
        self.assertEqual(len(stmt.methods), 2)
        self.assertIsNone(stmt.methods[0].fn)
        self.assertTrue(stmt.methods[0].is_method)
        self.assertEqual(stmt.methods[1].params, ["self", "x"])
        self.assertIsInstance(stmt.methods[1].fn, FnExpr)

    def test_impl_trait_for_type(self):
        src = "impl T for S { fn a(self) { 1 } }"
        (stmt,) = parse(src)
        self.assertIsInstance(stmt, ImplDecl)
        self.assertEqual(stmt.type_name, "S")
        self.assertEqual(stmt.trait_name, "T")
        self.assertEqual(len(stmt.methods), 1)
        self.assertIsNotNone(stmt.methods[0].fn)

    def test_inherent_impl_with_no_methods(self):
        (stmt,) = parse("impl S { }")
        self.assertIsInstance(stmt, ImplDecl)
        self.assertIsNone(stmt.trait_name)
        self.assertEqual(stmt.methods, [])

    def test_method_call_shape(self):
        (stmt,) = parse("p.m(1, 2)")
        self.assertIsInstance(stmt, ExprStmt)
        expr = stmt.value
        self.assertIsInstance(expr, MethodCall)
        self.assertIsInstance(expr.obj, Ident)
        self.assertEqual(expr.obj.name, "p")
        self.assertEqual(expr.method, "m")
        self.assertEqual(len(expr.args), 2)

    def test_method_call_on_field_access(self):
        expr = parse_expr("a.b.c()")
        self.assertIsInstance(expr, MethodCall)
        self.assertIsInstance(expr.obj, FieldAccess)
        self.assertIsInstance(expr.obj.obj, Ident)
        self.assertEqual(expr.obj.obj.name, "a")
        self.assertEqual(expr.obj.field, "b")
        self.assertEqual(expr.method, "c")

    def test_field_access_on_method_call(self):
        expr = parse_expr("x.f().g")
        self.assertIsInstance(expr, FieldAccess)
        self.assertIsInstance(expr.obj, MethodCall)
        self.assertEqual(expr.obj.method, "f")
        self.assertEqual(expr.field, "g")


class DetachOperandParsingTests(unittest.TestCase):
    """M13: `detach` on any call chain (`obj.method(args)`, not just plain
    `name(args)`) -- AST shape only (see tests/test_traits_m13.py for
    end-to-end behavioral coverage)."""

    def test_detach_work_await_shape_unchanged(self):
        expr = parse_expr("detach work().await")
        self.assertIsInstance(expr, FieldAccess)
        self.assertEqual(expr.field, "await")
        self.assertIsInstance(expr.obj, DetachExpr)
        self.assertIsInstance(expr.obj.call, Call)

    def test_detach_chained_method_calls_then_await(self):
        expr = parse_expr("detach a.b().c().await")
        self.assertIsInstance(expr, FieldAccess)
        self.assertEqual(expr.field, "await")
        detach = expr.obj
        self.assertIsInstance(detach, DetachExpr)
        outer = detach.call
        self.assertIsInstance(outer, MethodCall)
        self.assertEqual(outer.method, "c")
        inner = outer.obj
        self.assertIsInstance(inner, MethodCall)
        self.assertEqual(inner.method, "b")
        self.assertIsInstance(inner.obj, Ident)
        self.assertEqual(inner.obj.name, "a")

    def test_detach_method_call(self):
        expr = parse_expr("detach p.m(1)")
        self.assertIsInstance(expr, DetachExpr)
        self.assertIsInstance(expr.call, MethodCall)


class KwargsParsingTests(unittest.TestCase):
    """M16: default parameter values / keyword-argument calls -- AST shape
    only (see tests/test_kwargs.py for end-to-end behavioral coverage)."""

    def test_fn_param_defaults(self):
        (stmt,) = parse("fn f(a, b = 2) { a }")
        self.assertIsInstance(stmt, LetStmt)
        fn = stmt.value
        self.assertIsInstance(fn, FnExpr)
        self.assertEqual(fn.params, ["a", "b"])
        self.assertIsNone(fn.defaults[0])
        self.assertIsInstance(fn.defaults[1], NumberLit)
        self.assertEqual(fn.defaults[1].value, 2)

    def test_call_kwargs(self):
        (stmt,) = parse("f(1, x: 2)")
        expr = stmt.value
        self.assertIsInstance(expr, Call)
        self.assertEqual(len(expr.args), 1)
        self.assertEqual(expr.kwargs[0][0], "x")

    def test_method_call_kwargs(self):
        (stmt,) = parse("p.m(y: 1)")
        expr = stmt.value
        self.assertIsInstance(expr, MethodCall)
        self.assertEqual(expr.kwargs[0][0], "y")

    def test_struct_literal_argument_is_still_positional(self):
        expr = parse_expr("f(Point { x: 1 })")
        self.assertIsInstance(expr, Call)
        self.assertEqual(len(expr.args), 1)
        self.assertEqual(expr.kwargs, [])

    def test_print_sep_and_end(self):
        (stmt,) = parse('print(1, 2, sep: "-", end: "")')
        self.assertIsInstance(stmt, PrintStmt)
        self.assertEqual(len(stmt.args), 2)
        self.assertIsNotNone(stmt.sep)
        self.assertIsNotNone(stmt.end)


class M17RangeParsingTests(unittest.TestCase):
    """M17: range expressions/patterns and the `le`/`ge`/`!` operators --
    AST shape only (see tests/test_iterators.py and tests/test_operators.py
    for end-to-end behavioral coverage)."""

    def test_range_desugars_to_range_struct_lit(self):
        expr = parse_expr("1..5")
        self.assertIsInstance(expr, StructLit)
        self.assertEqual(expr.type_name, "Range")
        names = [name for name, _value in expr.fields]
        self.assertEqual(names, ["start", "end", "inclusive"])
        inclusive = dict(expr.fields)["inclusive"]
        self.assertIsInstance(inclusive, BoolLit)
        self.assertFalse(inclusive.value)

    def test_prefix_range_desugars_to_to_range(self):
        expr = parse_expr("..=3")
        self.assertIsInstance(expr, StructLit)
        self.assertEqual(expr.type_name, "ToRange")
        inclusive = dict(expr.fields)["inclusive"]
        self.assertTrue(inclusive.value)

    def test_suffix_only_range_desugars_to_from_range(self):
        (stmt,) = parse("print(1..)")
        (expr,) = stmt.args
        self.assertIsInstance(expr, StructLit)
        self.assertEqual(expr.type_name, "FromRange")
        self.assertEqual([name for name, _v in expr.fields], ["start"])

    def test_if_condition_ending_in_suffix_range_leaves_the_body_alone(self):
        (wrapper,) = parse("if x == 1.. { }")
        stmt = wrapper.value
        self.assertIsInstance(stmt, IfStmt)
        self.assertIsInstance(stmt.cond, StructLit)
        self.assertEqual(stmt.cond.type_name, "FromRange")
        start = dict(stmt.cond.fields)["start"]
        self.assertIsInstance(start, Binary)
        self.assertEqual(start.op, "eq")
        self.assertEqual(stmt.then.stmts, [])
        self.assertIsNone(stmt.then.tail)

    def test_negative_bound_range_pattern(self):
        (wrapper,) = parse('match 1 { -3..=3 => { 1 } }')
        stmt = wrapper.value
        self.assertIsInstance(stmt, MatchStmt)
        pattern = stmt.arms[0].pattern
        self.assertIsInstance(pattern, RangePat)
        self.assertTrue(pattern.inclusive)
        self.assertIsInstance(pattern.lo, NumberLit)
        self.assertEqual(pattern.lo.value, -3)
        self.assertIsInstance(pattern.hi, NumberLit)
        self.assertEqual(pattern.hi.value, 3)


class M17OperatorParsingTests(unittest.TestCase):
    """M17 part B: `<=`/`>=`/`!` -- see tests/test_operators.py for
    end-to-end behavioral coverage."""

    def test_le_binary_op(self):
        expr = parse_expr("a <= b")
        self.assertIsInstance(expr, Binary)
        self.assertEqual(expr.op, "le")

    def test_ge_binary_op(self):
        expr = parse_expr("a >= b")
        self.assertIsInstance(expr, Binary)
        self.assertEqual(expr.op, "ge")

    def test_bang_binds_tighter_than_eq(self):
        expr = parse_expr("!a == b")
        self.assertIsInstance(expr, Binary)
        self.assertEqual(expr.op, "eq")
        self.assertIsInstance(expr.lhs, Unary)
        self.assertEqual(expr.lhs.op, "!")

    def test_bang_binds_looser_than_postfix_method_call(self):
        expr = parse_expr("!x.y()")
        self.assertIsInstance(expr, Unary)
        self.assertEqual(expr.op, "!")
        self.assertIsInstance(expr.operand, MethodCall)


if __name__ == "__main__":
    unittest.main()
