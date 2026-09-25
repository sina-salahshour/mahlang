"""End-to-end behavioral tests for M17 part B: the `<=`, `>=`, `!`
operators -- see the M17 spec and docs/MAHC_FORMAT.md's version-1.2 notes
(`le`/`ge`/`not` opcodes).

Uses `run_source` like the other behavioral test files (never re-plumbs
the pipeline directly). AST-shape coverage lives in
`tests/test_parser.py`'s `M17OperatorParsingTests`; bytecode-shape
coverage in `tests/test_iterators.py`'s prelude-plumbing tests exercises
the same minor-version machinery `le`/`ge`/`not` share with `matchrange`.
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
from tests.support import compile_program, run_source


class ComparisonTests(unittest.TestCase):
    def test_le(self):
        self.assertEqual(run_source("print(1 <= 2, 2 <= 2, 3 <= 2)"), "true true false\n")

    def test_ge(self):
        self.assertEqual(run_source("print(1 >= 2, 2 >= 2, 3 >= 2)"), "false true true\n")

    def test_string_comparison(self):
        self.assertEqual(run_source('print("a" <= "b", "b" >= "b")'), "true true\n")

    def test_neq_still_works(self):
        self.assertEqual(run_source("print(1 != 2)"), "true\n")

    def test_le_type_error(self):
        with self.assertRaisesRegex(Exception, r"Cannot compare Number and String with '<='"):
            run_source('print(1 <= "a")')

    def test_ge_type_error(self):
        with self.assertRaisesRegex(Exception, r"Cannot compare Bool and Number with '>='"):
            run_source("print(true >= 1)")


class NotTests(unittest.TestCase):
    def test_not_over_various_truthy_falsy_values(self):
        src = 'print(!true, !false, !0, !1, !"", !"x", !none, !some(0))'
        self.assertEqual(run_source(src), "false true true false true false true false\n")

    def test_double_not(self):
        self.assertEqual(run_source("print(!!5)"), "true\n")

    def test_not_of_negated_number(self):
        self.assertEqual(run_source("print(!-1)"), "false\n")

    def test_precedence_not_binds_tighter_than_eq(self):
        self.assertEqual(run_source("print(!1 == false)"), "true\n")

    def test_precedence_arithmetic_binds_tighter_than_le(self):
        self.assertEqual(run_source("print(1 + 1 <= 2)"), "true\n")

    def test_precedence_not_and_ge_combo(self):
        self.assertEqual(run_source("print(!(1 > 2) & 3 >= 3)"), "true\n")

    def test_not_in_if_condition(self):
        src = 'let x = 5\nif !(x < 3) { print("big") }'
        self.assertEqual(run_source(src), "big\n")

    def test_not_in_while_condition(self):
        src = "let i = 0\nwhile !(i >= 3) { i = i + 1 }\nprint(i)"
        self.assertEqual(run_source(src), "3\n")


class RangePatternLexingTests(unittest.TestCase):
    def test_range_patterns_still_lex_correctly_next_to_new_operators(self):
        src = 'match 3 { ..=3 => { print("le") } _ => { print("no") } }'
        self.assertEqual(run_source(src), "le\n")


class BytecodeTests(unittest.TestCase):
    def test_disasm_contains_le_and_not(self):
        program = compile_program(text="print(1 <= 2, !true)")
        text = disassemble(program)
        self.assertIn("le", text)
        self.assertIn("not", text)

    def test_hand_built_minor_1_program_with_not_is_rejected(self):
        fns = [FunctionDecl(0, 1, 0, None, params=[])]
        code = [Instr("not", ((0, 0), (0, 0))), Instr("halt", ())]
        program = Program(
            strings=[], constants=[], types=[], natives=[], functions=fns, code=code, debug=None, minor=1
        )
        with self.assertRaises(MahcFormatError):
            decode(encode(program))


if __name__ == "__main__":
    unittest.main()
