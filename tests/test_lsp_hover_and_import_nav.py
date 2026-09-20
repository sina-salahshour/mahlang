"""Tests for the post-M7 LSP follow-up: hover revived on the real symbol
table (was left disabled by M6/M7, per user request after trying the LSP
directly), and go-to-definition on an `import` directive's path string or
namespace identifier jumping to the imported file.

See docs/V2_DESIGN.md's M7 milestone (updated with a follow-up note) and
`lsp/analysis.py`'s `get_hover`/`get_definition` docstrings.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lsp import analysis

EXAMPLES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples")


def _find(text: str, needle: str, occurrence: int = 0):
    idx = -1
    for _ in range(occurrence + 1):
        idx = text.index(needle, idx + 1)
    line = text.count("\n", 0, idx)
    col = idx - (text.rfind("\n", 0, idx) + 1)
    return line, col


class HoverTests(unittest.TestCase):
    def test_hover_on_keyword(self):
        src = "let x = 5\nprint(x)\n"
        line, col = _find(src, "let")
        hover = analysis.get_hover(src, line, col)
        self.assertIsNotNone(hover)
        self.assertIn("keyword", hover["contents"]["value"])
        self.assertIn("let", hover["contents"]["value"])

    def test_hover_on_variable_use(self):
        src = "let x = 5\nprint(x)\n"
        line, col = _find(src, "x", occurrence=1)  # the print(x) use
        hover = analysis.get_hover(src, line, col)
        self.assertIsNotNone(hover)
        self.assertIn("variable", hover["contents"]["value"])
        self.assertIn("`x`", hover["contents"]["value"])

    def test_hover_on_function_use(self):
        src = "fn add(a, b) { return a + b }\nprint(add(1, 2))\n"
        line, col = _find(src, "add", occurrence=1)
        hover = analysis.get_hover(src, line, col)
        self.assertIsNotNone(hover)
        self.assertIn("function", hover["contents"]["value"])

    def test_hover_on_parameter_use(self):
        src = "fn add(a, b) { return a + b }\nprint(add(1, 2))\n"
        line, col = _find(src, "a", occurrence=1)  # inside `return a + b`
        hover = analysis.get_hover(src, line, col)
        self.assertIsNotNone(hover)
        self.assertIn("parameter", hover["contents"]["value"])

    def test_hover_on_builtin(self):
        src = "print(5)\n"
        line, col = _find(src, "print")
        hover = analysis.get_hover(src, line, col)
        self.assertIsNotNone(hover)
        self.assertIn("builtin", hover["contents"]["value"])

    def test_hover_on_struct_enum_match_keywords(self):
        src = "struct Point { x, y }\nenum Shape { Empty }\nmatch 1 { _ => { } }\n"
        for kw in ("struct", "enum", "match"):
            line, col = _find(src, kw)
            hover = analysis.get_hover(src, line, col)
            self.assertIsNotNone(hover, f"hover on {kw!r} returned None")
            self.assertIn(kw, hover["contents"]["value"])

    def test_hover_on_undefined_identifier_returns_none(self):
        # No symbol table entry (undefined reference) -- must degrade to
        # None, not crash.
        hover = analysis.get_hover("print(nope)\n", 0, 6)
        self.assertIsNone(hover)


class ImportGotoDefinitionTests(unittest.TestCase):
    def test_namespaced_import_path_string_jumps_to_file(self):
        path = os.path.join(EXAMPLES_DIR, "import_demo.mh")
        text = open(path).read()
        # Anchor on the *real* `import math from "mathlib"` statement --
        # this file's leading comment block also mentions the same text as
        # prose documentation, so use rindex (the real statement comes
        # last) rather than index (which would land inside the comment).
        idx = text.rindex('from "mathlib"') + len('from "')
        line = text.count("\n", 0, idx)
        col = idx - (text.rfind("\n", 0, idx) + 1)
        result = analysis.get_definition(text, line, col, path)
        self.assertIsNotNone(result)
        self.assertTrue(result["path"].endswith("mathlib.mh"))

    def test_namespace_identifier_jumps_to_file(self):
        path = os.path.join(EXAMPLES_DIR, "import_demo.mh")
        text = open(path).read()
        idx = text.rindex("import math from") + len("import ")
        line = text.count("\n", 0, idx)
        col = idx - (text.rfind("\n", 0, idx) + 1)
        result = analysis.get_definition(text, line, col, path)
        self.assertIsNotNone(result)
        self.assertTrue(result["path"].endswith("mathlib.mh"))

    def test_flat_import_path_string_jumps_to_file(self):
        path = os.path.join(EXAMPLES_DIR, "_flat_import_nav_test.mh")
        src = 'import "mathlib"\nprint(square(4))\n'
        with open(path, "w") as f:
            f.write(src)
        try:
            idx = src.index('"mathlib"') + 1
            line = src.count("\n", 0, idx)
            col = idx - (src.rfind("\n", 0, idx) + 1)
            result = analysis.get_definition(src, line, col, path)
            self.assertIsNotNone(result)
            self.assertTrue(result["path"].endswith("mathlib.mh"))
        finally:
            os.remove(path)

    def test_unresolved_import_path_returns_none(self):
        src = 'import "no_such_module"\n'
        idx = src.index('"no_such_module"') + 1
        result = analysis.get_definition(src, 0, idx, None)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
