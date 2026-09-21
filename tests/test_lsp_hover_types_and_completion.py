"""Tests for the LSP follow-up covering:

  - hover on a *use* of a locally-declared or imported function/variable
    showing its declaration's own leading doc comment, and a demangled
    display name for imported symbols;
  - hover and go-to-definition on struct/enum type names and enum variant
    names (a previously-untracked namespace, see `compiler/resolve.py`'s
    `type_position_index`);
  - completion revived on top of the real resolver (`analysis.get_completions`),
    including import-path-string completion.

See `lsp/analysis.py`'s `get_hover`/`get_definition`/`get_completions`
docstrings, and `compiler/resolve.py`'s `type_position_index` docstring.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mah.lsp import analysis

EXAMPLES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples")


def _find(text: str, needle: str, occurrence: int = 0):
    idx = -1
    for _ in range(occurrence + 1):
        idx = text.index(needle, idx + 1)
    line = text.count("\n", 0, idx)
    col = idx - (text.rfind("\n", 0, idx) + 1)
    return line, col


class HoverDocCommentTests(unittest.TestCase):
    def test_hover_on_use_of_locally_declared_function_shows_doc_comment(self):
        src = (
            "# doubles a number\n"
            "fn double(n) {\n"
            "\treturn n * 2\n"
            "}\n"
            "print(double(4))\n"
        )
        line, col = _find(src, "double", occurrence=1)  # the print(double(4)) use
        hover = analysis.get_hover(src, line, col)
        self.assertIsNotNone(hover)
        self.assertIn("doubles a number", hover["contents"]["value"])

    def test_hover_on_use_of_imported_symbol_shows_doc_and_demangled_name(self):
        path = os.path.join(EXAMPLES_DIR, "import_demo.mh")
        with open(path) as f:
            text = f.read()
        line, col = _find(text, "math.square")
        col += len("math.")  # land on "square" itself
        hover = analysis.get_hover(text, line, col, path)
        self.assertIsNotNone(hover)
        value = hover["contents"]["value"]
        self.assertIn("squares a number", value)
        self.assertIn("`square`", value)
        self.assertNotIn("__mah_m", value)


class HoverStructEnumTests(unittest.TestCase):
    STRUCT_SRC = (
        "struct Point { x, y }\n"
        "let p = Point { x: 1, y: 2 }\n"
    )

    ENUM_SRC = (
        "enum Shape {\n"
        "\tCircle { r },\n"
        "\tEmpty\n"
        "}\n"
        "let s = Shape.Circle { r: 5 }\n"
    )

    def test_hover_on_struct_name_at_declaration(self):
        line, col = _find(self.STRUCT_SRC, "Point", occurrence=0)
        hover = analysis.get_hover(self.STRUCT_SRC, line, col)
        self.assertIsNotNone(hover)
        value = hover["contents"]["value"]
        self.assertIn("struct", value)
        self.assertIn("x", value)
        self.assertIn("y", value)

    def test_hover_on_struct_name_at_use(self):
        line, col = _find(self.STRUCT_SRC, "Point", occurrence=1)
        hover = analysis.get_hover(self.STRUCT_SRC, line, col)
        self.assertIsNotNone(hover)
        value = hover["contents"]["value"]
        self.assertIn("struct", value)
        self.assertIn("x", value)
        self.assertIn("y", value)

    def test_hover_on_enum_name_at_declaration(self):
        line, col = _find(self.ENUM_SRC, "Shape", occurrence=0)
        hover = analysis.get_hover(self.ENUM_SRC, line, col)
        self.assertIsNotNone(hover)
        value = hover["contents"]["value"]
        self.assertIn("enum", value)
        self.assertIn("Circle", value)
        self.assertIn("Empty", value)

    def test_hover_on_enum_name_at_use(self):
        line, col = _find(self.ENUM_SRC, "Shape", occurrence=1)
        hover = analysis.get_hover(self.ENUM_SRC, line, col)
        self.assertIsNotNone(hover)
        value = hover["contents"]["value"]
        self.assertIn("enum", value)
        self.assertIn("Circle", value)
        self.assertIn("Empty", value)

    def test_hover_on_enum_variant_name_shows_only_its_own_fields(self):
        line, col = _find(self.ENUM_SRC, "Circle", occurrence=1)  # the use site
        hover = analysis.get_hover(self.ENUM_SRC, line, col)
        self.assertIsNotNone(hover)
        value = hover["contents"]["value"]
        self.assertIn("variant", value)
        self.assertIn("Circle", value)
        self.assertIn("r", value)
        # Should not describe the *whole* enum's variant list -- "Empty"
        # is a sibling variant with no relation to Circle's own fields.
        self.assertNotIn("Empty", value)


class HoverFieldTests(unittest.TestCase):
    """M11: field-name hover in declarations/literals/explicit patterns
    (never plain field access -- see docs/V2_DESIGN.md's M11 milestone)."""

    STRUCT_SRC = (
        "struct Point { x, y }\n"
        "let p = Point { x: 1, y: 2 }\n"
    )

    ENUM_SRC = (
        "enum Shape {\n"
        "\tCircle { r },\n"
        "\tEmpty\n"
        "}\n"
        "let s = Shape.Circle { r: 5 }\n"
    )

    def test_hover_on_struct_field_label_shows_field_of_struct(self):
        line, col = _find(self.STRUCT_SRC, "x", occurrence=1)  # the `x:` label in the literal
        hover = analysis.get_hover(self.STRUCT_SRC, line, col)
        self.assertIsNotNone(hover)
        value = hover["contents"]["value"]
        self.assertIn("field", value)
        self.assertIn("x", value)
        self.assertIn("Point", value)

    def test_hover_on_struct_field_declaration_shows_field_of_struct(self):
        line, col = _find(self.STRUCT_SRC, "x", occurrence=0)  # in `struct Point { x, y }`
        hover = analysis.get_hover(self.STRUCT_SRC, line, col)
        self.assertIsNotNone(hover)
        value = hover["contents"]["value"]
        self.assertIn("field", value)
        self.assertIn("Point", value)

    def test_hover_on_variant_field_label_shows_field_of_variant(self):
        idx = self.ENUM_SRC.index("r: 5")  # the `r:` label in the literal
        line = self.ENUM_SRC.count("\n", 0, idx)
        col = idx - (self.ENUM_SRC.rfind("\n", 0, idx) + 1)
        hover = analysis.get_hover(self.ENUM_SRC, line, col)
        self.assertIsNotNone(hover)
        value = hover["contents"]["value"]
        self.assertIn("field", value)
        self.assertIn("Shape.Circle", value)

    def test_hover_on_plain_field_access_is_not_a_field_hover(self):
        src = self.STRUCT_SRC + "print(p.x)\n"
        line, col = _find(src, "p.x")
        col += 2  # land on the `x` after `p.`
        hover = analysis.get_hover(src, line, col)
        # `p.x` is plain field access -- deliberately not covered (unsound
        # without a real type system) -- so this must not claim to be a
        # "field" hover at all (it may return None, or find nothing useful).
        if hover is not None:
            self.assertNotIn("**field**", hover["contents"]["value"])


class GotoDefinitionFieldTests(unittest.TestCase):
    """M11: go-to-definition on a field literal's label jumps to that
    field's own declaration position within the struct/enum."""

    STRUCT_SRC = (
        "struct Point { x, y }\n"
        "let p = Point { x: 1, y: 2 }\n"
    )

    ENUM_SRC = (
        "enum Shape {\n"
        "\tCircle { r },\n"
        "\tEmpty\n"
        "}\n"
        "let s = Shape.Circle { r: 5 }\n"
    )

    def test_goto_definition_on_struct_field_literal_label_lands_on_declaration(self):
        use_line, use_col = _find(self.STRUCT_SRC, "x", occurrence=1)
        result = analysis.get_definition(self.STRUCT_SRC, use_line, use_col)
        self.assertIsNotNone(result)
        self.assertIsNone(result["path"])

        decl_line, decl_col = _find(self.STRUCT_SRC, "x", occurrence=0)
        self.assertEqual(result["range"]["start"]["line"], decl_line)
        self.assertEqual(result["range"]["start"]["character"], decl_col)

    def test_goto_definition_on_variant_field_literal_label_lands_on_declaration(self):
        use_idx = self.ENUM_SRC.index("r: 5")
        use_line = self.ENUM_SRC.count("\n", 0, use_idx)
        use_col = use_idx - (self.ENUM_SRC.rfind("\n", 0, use_idx) + 1)
        result = analysis.get_definition(self.ENUM_SRC, use_line, use_col)
        self.assertIsNotNone(result)
        self.assertIsNone(result["path"])

        decl_idx = self.ENUM_SRC.index("{ r }") + 2
        decl_line = self.ENUM_SRC.count("\n", 0, decl_idx)
        decl_col = decl_idx - (self.ENUM_SRC.rfind("\n", 0, decl_idx) + 1)
        self.assertEqual(result["range"]["start"]["line"], decl_line)
        self.assertEqual(result["range"]["start"]["character"], decl_col)

    def test_goto_definition_on_plain_field_access_returns_none(self):
        src = self.STRUCT_SRC + "print(p.x)\n"
        line, col = _find(src, "p.x")
        col += 2
        result = analysis.get_definition(src, line, col)
        self.assertIsNone(result)


class GotoDefinitionStructEnumTests(unittest.TestCase):
    ENUM_SRC = (
        "enum Shape {\n"
        "\tCircle { r },\n"
        "\tEmpty\n"
        "}\n"
        "let s = Shape.Circle { r: 5 }\n"
    )

    STRUCT_SRC = (
        "struct Point { x, y }\n"
        "let p = Point { x: 1, y: 2 }\n"
    )

    def test_goto_definition_on_enum_variant_use_lands_on_its_own_declaration(self):
        use_line, use_col = _find(self.ENUM_SRC, "Circle", occurrence=1)
        result = analysis.get_definition(self.ENUM_SRC, use_line, use_col)
        self.assertIsNotNone(result)
        self.assertIsNone(result["path"])  # same (entry) document

        decl_line, decl_col = _find(self.ENUM_SRC, "Circle", occurrence=0)
        self.assertEqual(result["range"]["start"]["line"], decl_line)
        self.assertEqual(result["range"]["start"]["character"], decl_col)

    def test_goto_definition_on_struct_type_use_lands_on_declaration(self):
        use_line, use_col = _find(self.STRUCT_SRC, "Point", occurrence=1)
        result = analysis.get_definition(self.STRUCT_SRC, use_line, use_col)
        self.assertIsNotNone(result)
        self.assertIsNone(result["path"])

        decl_line, decl_col = _find(self.STRUCT_SRC, "Point", occurrence=0)
        self.assertEqual(result["range"]["start"]["line"], decl_line)
        self.assertEqual(result["range"]["start"]["character"], decl_col)


class CompletionGeneralTests(unittest.TestCase):
    def test_general_completion_includes_symbols_keywords_and_builtins(self):
        src = "let x = 1\nfn add(a, b) { return a + b }\n"
        items = analysis.get_completions(src)
        labels = {item["label"] for item in items}
        self.assertIn("x", labels)
        self.assertIn("add", labels)
        self.assertIn("let", labels)
        self.assertIn("fn", labels)
        self.assertIn("if", labels)
        self.assertIn("print", labels)

    def test_no_attribute_error_regression(self):
        # This is the exact bug being fixed: get_completions used to crash
        # with AttributeError: type object 'TokenType' has no attribute
        # 'Def' on any input at all, because it called dead code left over
        # from before a compiler rewrite.
        src = "let x = 1\nprint(x)\n"
        try:
            analysis.get_completions(src, None, 0, 0)
        except AttributeError as e:
            self.fail(f"get_completions raised AttributeError: {e}")


class CompletionNamespaceTests(unittest.TestCase):
    def test_namespace_member_completion_is_exclusive(self):
        path = os.path.join(EXAMPLES_DIR, "import_demo.mh")
        with open(path) as f:
            text = f.read()
        idx = text.index("math.square") + len("math.")
        line = text.count("\n", 0, idx)
        col = idx - (text.rfind("\n", 0, idx) + 1)
        items = analysis.get_completions(text, path, line, col)
        labels = {item["label"] for item in items}
        # Only mathlib's exported members should appear -- no keywords,
        # builtins, or other unrelated symbols.
        self.assertIn("square", labels)
        self.assertIn("cube", labels)
        self.assertIn("is_even", labels)
        self.assertIn("answer", labels)
        self.assertNotIn("let", labels)
        self.assertNotIn("print", labels)
        self.assertNotIn("n", labels)  # local var in import_demo.mh


class CompletionImportPathTests(unittest.TestCase):
    def test_import_string_completion_lists_mh_files(self):
        tmp_path = os.path.join(EXAMPLES_DIR, "_completion_import_test.mh")
        src = 'import "mat'
        with open(tmp_path, "w") as f:
            f.write(src)
        try:
            line = 0
            col = len(src)
            items = analysis.get_completions(src, tmp_path, line, col)
            labels = {item["label"]: item for item in items}
            self.assertIn("mathlib", labels)
            self.assertEqual(labels["mathlib"]["kind"], analysis.COMPLETION_FILE)
        finally:
            os.remove(tmp_path)


class ServerCapabilitiesTests(unittest.TestCase):
    def test_completion_provider_advertised_in_server_source(self):
        server_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mah", "lsp", "server.py"
        )
        with open(server_path, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("completionProvider", source)


if __name__ == "__main__":
    unittest.main()
