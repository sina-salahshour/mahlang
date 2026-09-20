"""Tests for M6 -- the forgiving (error-recovering) parser.

See docs/V2_DESIGN.md's M6 milestone. The core idea: a syntax error no
longer aborts the whole parse. `Parser` collects `(message, position)`
pairs into `self.errors` instead of raising, substitutes an `ErrorNode`
(wrapped in an `ExprStmt`) for the broken item, and resynchronizes at the
next safe token so the rest of the file still parses. This is a *parser*-
level feature only: `mah.py build`/`run` (and, by extension, any test that
goes through the full pipeline) must still refuse outright to execute a
program that had any parse error, collected or not -- forgiving parsing
exists for a tool (the LSP) to keep giving useful information across a
broken file, not to let broken programs run.

Uses `tests/support.py`'s new `parse_source` helper (parse-only, returns
`(program, parser)` so `parser.errors` can be inspected directly) for the
parser-level scenarios, and `mah.generate_code` directly for the
run-refusal scenario, since `tests/support.py`'s existing `compile_source`/
`run_source` helpers don't check `parser.errors` at all (only `mah.py`'s
`generate_code` was changed to do that -- see the milestone's mah.py
section).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mah.compiler.ast_nodes import ErrorNode, ExprStmt, LetStmt, PrintStmt
from mah.preprocessor import preprocess
from tests.support import parse_source

from mah.cli import main as mah  # noqa: E402 -- the CLI module, for generate_code


class OneErrorRecoveryTests(unittest.TestCase):
    """Scenario 1: one syntax error, more valid code after it, in the same
    block -- parsing must succeed (no exception from parse_program itself),
    collect exactly one error, and keep the statements before *and* after
    the mistake."""

    def test_parsing_continues_past_one_bad_statement(self):
        # `let y = )` is broken (`)` can't start an expression) -- the
        # whole `let y = ...` statement is discarded as one ErrorNode, but
        # `let x = 1` before it and `print(x)` after it must still show up,
        # structurally intact, in the returned program.
        source = "let x = 1\nlet y = )\nprint(x)"
        program, parser = parse_source(source)  # must not raise

        self.assertEqual(len(parser.errors), 1)
        message, position = parser.errors[0]
        self.assertIn("position", message)
        self.assertIsInstance(position, int)

        self.assertEqual(len(program), 3)

        self.assertIsInstance(program[0], LetStmt)
        self.assertEqual(program[0].name, "x")

        self.assertIsInstance(program[1], ExprStmt)
        self.assertIsInstance(program[1].value, ErrorNode)

        self.assertIsInstance(program[2], PrintStmt)


class MultipleErrorRecoveryTests(unittest.TestCase):
    """Scenario 2: multiple, unrelated syntax errors in one file are ALL
    collected, not just the first."""

    def test_two_unrelated_errors_both_collected(self):
        source = "let a = )\nlet b = 2\nlet c = ,\nprint(b)"
        program, parser = parse_source(source)  # must not raise

        self.assertGreaterEqual(len(parser.errors), 2)
        # The good statement sandwiched between the two mistakes must have
        # survived recovery.
        good_lets = [s for s in program if isinstance(s, LetStmt)]
        self.assertEqual([s.name for s in good_lets], ["b"])
        error_stmts = [
            s for s in program if isinstance(s, ExprStmt) and isinstance(s.value, ErrorNode)
        ]
        self.assertEqual(len(error_stmts), 2)

    def test_three_independent_broken_lines(self):
        # A third, differently-shaped mistake (an unclosed struct-literal-
        # shaped match arm is out of scope -- keep it simple: another bare
        # broken expression) to make sure collection isn't accidentally
        # capped at two.
        source = "let a = )\nlet b = *\nlet c = ,\nprint(1)"
        _program, parser = parse_source(source)
        self.assertGreaterEqual(len(parser.errors), 3)


class RunRefusalTests(unittest.TestCase):
    """Scenario 3: even though the parser itself doesn't raise, the full
    pipeline (mah.py's generate_code) must still refuse to build/run a
    program with any collected parse error -- forgiving parsing is for
    tooling, not a license to execute broken programs."""

    def test_generate_code_refuses_a_program_with_parse_errors(self):
        source = "let a = )\nlet b = ,\nprint(1)"
        pp = preprocess(None, source)

        with self.assertRaises(SyntaxError) as ctx:
            mah.generate_code(pp.text, pp)

        message = str(ctx.exception)
        # Loose content check (not an exact string match): the combined
        # message should carry evidence of BOTH collected errors, not just
        # the first -- one line per error is how _format_parser_errors
        # joins them.
        self.assertGreaterEqual(message.count("Invalid syntax"), 2)

    def test_valid_program_still_builds_via_generate_code(self):
        # Regression guard on the new early-return itself: a clean program
        # must still compile exactly as before (no parser.errors -> no
        # early raise -> falls through to resolve/codegen).
        source = "let a = 1\nprint(a)"
        pp = preprocess(None, source)
        buf = mah.generate_code(pp.text, pp)
        self.assertIsNotNone(buf)


class RegressionTests(unittest.TestCase):
    """Scenario 4 (partial -- the full regression check is `make test`
    staying green): a clean, error-free snippet must still parse with an
    empty `parser.errors` list and an unaffected AST shape."""

    def test_clean_program_has_no_collected_errors(self):
        source = "let x = 1\nif x { print(x) } else { print(0) }\nwhile x { x = x - 1 }"
        program, parser = parse_source(source)
        self.assertEqual(parser.errors, [])
        self.assertTrue(all(not isinstance(s, ExprStmt) or not isinstance(s.value, ErrorNode) for s in program))


class LspDiagnosticsSmokeTests(unittest.TestCase):
    """Scenario 5: lsp/analysis.py must import cleanly (it used to fail
    with `ModuleNotFoundError: No module named 'actions'` before M6) and
    its `get_diagnostics` must report every parse error in the buffer, not
    just one."""

    def test_lsp_modules_import_cleanly(self):
        # Import in a subprocess-free way: just exercise the import here.
        # This used to raise ModuleNotFoundError (actions.py deleted in M0)
        # / ImportError (compiler.ir_generator gone) before M6's fix.
        from mah.lsp import analysis  # noqa: F401
        from mah.lsp import server  # noqa: F401

    def test_get_diagnostics_reports_multiple_parse_errors(self):
        from mah.lsp import analysis

        source = "let a = )\nlet b = ,\nprint(1)"
        diagnostics = analysis.get_diagnostics(source)
        self.assertGreaterEqual(len(diagnostics), 2)
        for diag in diagnostics:
            self.assertIn("range", diag)
            self.assertIn("message", diag)


class TerminationSafetyTests(unittest.TestCase):
    """Scenario 6: a direct test of the synchronization termination
    guarantee, not just a hope that it holds. A file that is nothing but a
    long run of syntactically-invalid-but-individually-lexable punctuation
    (no letters/digits at all, so nothing can ever start a valid
    statement/expression) must still finish parsing quickly and produce a
    non-empty list of collected errors, rather than looping forever."""

    def test_pure_punctuation_garbage_terminates(self):
        # Every character below is a real, lexable single/two-char token
        # (see compiler/lexer.py's _SINGLE_CHAR/_TWO_CHAR) that can never
        # start a statement or expression on its own -- this exercises
        # `_synchronize`'s token-skipping loop (and its belt-and-suspenders
        # forced-advance check) directly, without touching the separate,
        # unrelated question of the raw lexer's own behavior on a
        # character it can't tokenize at all (e.g. an unsupported symbol),
        # which this milestone's design does not attempt to make
        # recoverable.
        garbage = ")" * 5 + "}" * 3 + "," * 4 + ":" * 3 + "=" * 3 + "<" * 2 + ">" * 2 + "&" * 2 + "|" * 2 + "%" * 2 + "*" * 2 + "/" * 2
        program, parser = parse_source(garbage)  # must terminate, not hang
        self.assertTrue(len(parser.errors) > 0)
        # Every top-level item became an ErrorNode -- nothing here could
        # ever have parsed as real code.
        self.assertTrue(all(isinstance(s, ExprStmt) and isinstance(s.value, ErrorNode) for s in program))

    def test_unclosed_nested_blocks_terminate(self):
        # The mirror case of the test above: unmatched *opening* braces
        # that run all the way to EOF without ever closing, at increasing
        # nesting depth. This used to hang (found during independent
        # verification of this milestone, not by the original test suite):
        # `_parse_block_items`'s own "have I reached end_type?" check only
        # ever compared against `end_type` (e.g. BRACE_CLOSE for a nested
        # block), never against EOF -- so a block whose source ends before
        # its `}` ever appears would loop forever re-attempting to parse
        # "one more item" out of nothing at EOF. Fixed by also breaking on
        # EOF regardless of `end_type` (see `_parse_block_items`'s
        # docstring for the full cascade-of-catches explanation of what
        # happens next -- briefly: it still doesn't hang, and still doesn't
        # raise out of `parse_program()`, it just produces one `ErrorNode`
        # per level of unclosed nesting).
        program, parser = parse_source("fn f() { let x = 1 { { { {")
        self.assertTrue(len(parser.errors) > 0)

        program2, parser2 = parse_source("if true {")
        self.assertTrue(len(parser2.errors) > 0)

        program3, parser3 = parse_source("struct Point {")
        self.assertTrue(len(parser3.errors) > 0)


if __name__ == "__main__":
    unittest.main()
