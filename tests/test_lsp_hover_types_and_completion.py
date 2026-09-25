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


class TraitHoverDefinitionRenameTests(unittest.TestCase):
    """M12: hover/go-to-definition/rename on `trait`/`impl` -- keyword
    hover, the trait namespace (`type_position_index`'s new `("trait",
    name)` entries), and `Self` deliberately staying out of the struct's
    own rename targets."""

    SHAPE_SRC = (
        "trait Shape {\n"
        "\tfn area(self)\n"
        "\tfn name(self) { \"s\" }\n"
        "}\n"
        "struct Rect { w }\n"
        "impl Shape for Rect {\n"
        "\tfn area(self) { self.w }\n"
        "}\n"
        "print(Shape.area(Rect { w: 1 }))\n"
    )

    def test_hover_on_trait_keyword_and_impl_keyword(self):
        src = "trait T { fn a(self) }\nimpl T for T { }\n"
        line, col = _find(src, "trait")
        hover = analysis.get_hover(src, line, col)
        self.assertIsNotNone(hover)
        self.assertIn("trait", hover["contents"]["value"])

        line, col = _find(src, "impl")
        hover = analysis.get_hover(src, line, col)
        self.assertIsNotNone(hover)
        self.assertIn("impl", hover["contents"]["value"])

    def test_hover_on_trait_name_at_declaration(self):
        line, col = _find(self.SHAPE_SRC, "Shape", occurrence=0)
        hover = analysis.get_hover(self.SHAPE_SRC, line, col)
        self.assertIsNotNone(hover)
        value = hover["contents"]["value"]
        self.assertIn("**trait** `Shape`", value)
        self.assertIn("fn area(self)", value)
        self.assertIn("fn name(self) { ... }", value)

    def test_goto_definition_from_impl_header_trait_name_lands_on_declaration(self):
        line, col = _find(self.SHAPE_SRC, "Shape", occurrence=1)
        result = analysis.get_definition(self.SHAPE_SRC, line, col)
        self.assertIsNotNone(result)
        self.assertIsNone(result["path"])
        self.assertEqual(result["range"]["start"]["line"], 0)

    def test_goto_definition_from_impl_header_type_name_lands_on_declaration(self):
        line, col = _find(self.SHAPE_SRC, "Rect", occurrence=1)
        result = analysis.get_definition(self.SHAPE_SRC, line, col)
        self.assertIsNotNone(result)
        self.assertIsNone(result["path"])
        self.assertEqual(result["range"]["start"]["line"], 4)

    def test_rename_trait_name_touches_declaration_impl_header_and_call(self):
        line, col = _find(self.SHAPE_SRC, "Shape", occurrence=0)
        result = analysis.get_rename_edits(self.SHAPE_SRC, line, col, "Figure")
        self.assertIsNotNone(result)
        (edits,) = result["changes"].values()
        self.assertEqual(len(edits), 3)

    def test_rename_type_name_includes_impl_header_occurrence(self):
        line, col = _find(self.SHAPE_SRC, "Rect", occurrence=0)
        result = analysis.get_rename_edits(self.SHAPE_SRC, line, col, "Box")
        self.assertIsNotNone(result)
        (edits,) = result["changes"].values()
        self.assertEqual(len(edits), 3)

    def test_self_is_not_a_rename_target_of_the_struct(self):
        src = "struct P { v }\nimpl P {\n\tfn new(v) { Self { v: v } }\n}\n"
        self_line, self_col = _find(src, "Self")
        line, col = _find(src, "P", occurrence=0)
        result = analysis.get_rename_edits(src, line, col, "Q")
        self.assertIsNotNone(result)
        (edits,) = result["changes"].values()
        self.assertEqual(len(edits), 2)
        for edit in edits:
            self.assertFalse(
                edit["range"]["start"]["line"] == self_line
                and edit["range"]["start"]["character"] == self_col
            )


class MethodNavigationTests(unittest.TestCase):
    """M13: hover/go-to-definition/completion for method names -- see
    `compiler/resolve.py`'s 'method indexes' (`method_call_index`/
    `method_decl_index`/`member_block_ranges`) and `lsp/analysis.py`'s
    `_method_at_position`/`_method_hover_value`/`_member_access_completions`."""

    SRC = (
        "trait Shape {\n"
        "\tfn area(self)\n"
        "\tfn describe(self) { \"area \" + self.area() }\n"
        "}\n"
        "struct Rect { w, h }\n"
        "struct Sq { s }\n"
        "impl Rect {\n"
        "\t# makes a rect\n"
        "\tfn new(w, h) { Self { w: w, h: h } }\n"
        "}\n"
        "impl Shape for Rect {\n"
        "\tfn area(self) { self.w * self.h }\n"
        "}\n"
        "impl Shape for Sq {\n"
        "\tfn area(self) { self.s * self.s }\n"
        "}\n"
        "let r = Rect.new(2, 3)\n"
        "print(r.area())\n"
        "fn any(x) { x.area() }\n"
    )

    def test_hover_on_new_in_static_call(self):
        line, col = _find(self.SRC, "Rect.new")
        col += len("Rect.")
        hover = analysis.get_hover(self.SRC, line, col)
        self.assertIsNotNone(hover)
        value = hover["contents"]["value"]
        self.assertIn("impl Rect: fn new(w, h)", value)
        self.assertIn("makes a rect", value)

    def test_hover_on_area_with_known_receiver_type(self):
        line, col = _find(self.SRC, "r.area")
        col += len("r.")
        hover = analysis.get_hover(self.SRC, line, col)
        self.assertIsNotNone(hover)
        value = hover["contents"]["value"]
        self.assertIn("on `Rect`", value)
        self.assertIn("impl Shape for Rect: fn area(self)", value)
        self.assertNotIn("Sq", value)

    def test_hover_on_area_with_unknown_receiver_type(self):
        line, col = _find(self.SRC, "x.area")
        col += len("x.")
        hover = analysis.get_hover(self.SRC, line, col)
        self.assertIsNotNone(hover)
        value = hover["contents"]["value"]
        self.assertIn("isn't known statically", value)
        self.assertIn("impl Shape for Rect", value)
        self.assertIn("impl Shape for Sq", value)

    def test_hover_on_trait_declaration_method(self):
        line, col = _find(self.SRC, "area", occurrence=0)
        hover = analysis.get_hover(self.SRC, line, col)
        self.assertIsNotNone(hover)
        value = hover["contents"]["value"]
        self.assertIn("trait method", value)
        self.assertIn("required", value)
        self.assertIn("Implemented by: Rect, Sq", value)

    def test_hover_on_impl_method_declaration_shows_trait_it_implements(self):
        # Occurrence 3 of "area" is the `fn area` inside `impl Shape for Rect`
        # (0: trait's own decl, 1: inside the "area " string literal, 2: the
        # `self.area()` call in the trait default body, 3: the impl decl).
        line, col = _find(self.SRC, "area", occurrence=3)
        hover = analysis.get_hover(self.SRC, line, col)
        self.assertIsNotNone(hover)
        self.assertIn("implements `Shape.area`", hover["contents"]["value"])

    def test_definition_from_static_call_new(self):
        line, col = _find(self.SRC, "Rect.new")
        col += len("Rect.")
        result = analysis.get_definition(self.SRC, line, col)
        self.assertIsNotNone(result)
        self.assertEqual(result["range"]["start"]["line"], 8)

    def test_definition_from_known_receiver_call(self):
        line, col = _find(self.SRC, "r.area")
        col += len("r.")
        result = analysis.get_definition(self.SRC, line, col)
        self.assertIsNotNone(result)
        self.assertEqual(result["range"]["start"]["line"], 11)

    def test_definition_from_unknown_receiver_call_returns_a_list(self):
        line, col = _find(self.SRC, "x.area")
        col += len("x.")
        result = analysis.get_definition(self.SRC, line, col)
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 2)
        lines = sorted(item["range"]["start"]["line"] for item in result)
        self.assertEqual(lines, [11, 14])

    def test_definition_from_self_call_inside_trait_default(self):
        line, col = _find(self.SRC, "area", occurrence=2)  # self.area()
        result = analysis.get_definition(self.SRC, line, col)
        self.assertIsNotNone(result)
        self.assertEqual(result["range"]["start"]["line"], 1)

    def test_definition_from_impl_method_name_lands_on_trait_declaration(self):
        line, col = _find(self.SRC, "area", occurrence=3)  # impl Shape for Rect's own `fn area`
        result = analysis.get_definition(self.SRC, line, col)
        self.assertIsNotNone(result)
        self.assertEqual(result["range"]["start"]["line"], 1)

    def _completion_labels(self, suffix: str) -> dict:
        text = self.SRC + suffix
        line = text.count("\n")
        col = len(text) - (text.rfind("\n") + 1)
        items = analysis.get_completions(text, None, line, col)
        return {item["label"]: item for item in items}

    def test_completion_on_variable_with_known_type(self):
        labels = self._completion_labels("r.")
        self.assertIn("area", labels)
        self.assertIn("describe", labels)
        self.assertIn("w", labels)
        self.assertIn("h", labels)
        self.assertNotIn("to_string", labels)  # Rect has no Printable impl
        self.assertNotIn("new", labels)  # static, not a method

    def test_completion_on_type_name(self):
        labels = self._completion_labels("Rect.")
        self.assertIn("new", labels)
        self.assertIn("area", labels)
        self.assertIn("describe", labels)

    def test_completion_on_trait_name(self):
        labels = self._completion_labels("Shape.")
        self.assertEqual(set(labels), {"area", "describe"})

    def test_completion_for_an_unknown_receiver(self):
        text = self.SRC + "fn g(y) { y. }"
        idx = text.index("y. }") + len("y.")
        line = text.count("\n", 0, idx)
        col = idx - (text.rfind("\n", 0, idx) + 1)
        items = analysis.get_completions(text, None, line, col)
        labels = {item["label"]: item for item in items}
        self.assertIn("area", labels)
        self.assertIn("describe", labels)
        self.assertIn("Rect", labels["area"]["detail"])
        self.assertIn("Sq", labels["area"]["detail"])

    def test_completion_on_a_number_literal(self):
        labels = self._completion_labels("5.")
        self.assertIn("to_string", labels)

    def test_server_definition_responds_with_a_list_for_multiple_candidates(self):
        # Constructing a real `Server` needs no stdin/stdout I/O for this --
        # only `_on_textDocument_definition`'s own list-handling logic is
        # under test, so `analysis.get_definition` is monkeypatched to
        # return a 2-item list and `_respond` is faked to record its args
        # (mirroring how `TraitHoverDefinitionRenameTests` and friends in
        # this file drive `analysis` functions directly rather than
        # spinning up a real Server -- there's no existing Server-level
        # test in this repo to otherwise mirror).
        from mah.lsp.server import Server

        srv = Server(None, None)
        recorded = {}

        def fake_respond(request_id, result):
            recorded["result"] = result

        srv._respond = fake_respond
        srv._documents["file:///x.mh"] = "x"

        fake_target = [
            {"path": None, "range": {"start": {"line": 11, "character": 4}, "end": {"line": 11, "character": 8}}},
            {"path": None, "range": {"start": {"line": 14, "character": 4}, "end": {"line": 14, "character": 8}}},
        ]
        original = analysis.get_definition
        analysis.get_definition = lambda *a, **k: fake_target
        try:
            srv._on_textDocument_definition(
                1, {"textDocument": {"uri": "file:///x.mh"}, "position": {"line": 0, "character": 0}}
            )
        finally:
            analysis.get_definition = original

        result = recorded["result"]
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 2)
        for item in result:
            self.assertIn("uri", item)
            self.assertIn("range", item)


class ServerCapabilitiesTests(unittest.TestCase):
    def test_completion_provider_advertised_in_server_source(self):
        server_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mah", "lsp", "server.py"
        )
        with open(server_path, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("completionProvider", source)


class M17PreludeLspTests(unittest.TestCase):
    """M17: the prelude (ranges/iterators) is inlined like an imported
    module -- completion must hide its internals, and hover/go-to-
    definition into it must work like into any other imported file."""

    def test_completion_after_a_range_value_excludes_prelude_internals(self):
        src = "let x = 1..5\nx."
        line, col = _find(src, "x.")
        completions = analysis.get_completions(src, line=line, character=col + 2, path=None)
        labels = {item["label"] for item in completions}
        # Prelude adapter/param names never leak into member completion...
        self.assertNotIn("source", labels)
        self.assertNotIn("remaining", labels)
        # ...and Range's own real members (fields + Iterable's adapters) do
        # show up.
        self.assertIn("start", labels)
        self.assertIn("map", labels)

    def test_general_completion_excludes_prelude_helper_types_but_keeps_range(self):
        src = "let x = 1..5\n"
        completions = analysis.get_completions(src, line=1, character=0, path=None)
        labels = {item["label"] for item in completions}
        self.assertNotIn("__Iter", labels)
        self.assertNotIn("__NoInitial", labels)
        self.assertIn("Range", labels)

    def test_go_to_definition_on_range_lands_in_the_prelude(self):
        src = "let r = Range { start: 1, end: 2, inclusive: false }\n"
        line, col = _find(src, "Range")
        location = analysis.get_definition(src, line=line, character=col + 1, path=None)
        self.assertIsNotNone(location)
        self.assertTrue(location["path"].endswith("prelude.mh"))


class CollectionMethodTests(unittest.TestCase):
    """M19: Vector/Map native methods are known to hover and completion,
    typed from the literal a variable was bound to."""

    def _complete(self, src):
        line = src.count("\n")
        col = len(src.split("\n")[-1])
        return {item["label"] for item in analysis.get_completions(src, None, line, col)}

    def test_vector_completion(self):
        labels = self._complete("let v = [1]\nv.")
        self.assertTrue({"len", "push", "pop", "push_start", "pop_start", "copy", "index"} <= labels)
        self.assertNotIn("keys", labels)

    def test_map_completion_includes_prelude_methods(self):
        labels = self._complete('let m = ["a": 1]\nfor let k in m { }\nm.')
        self.assertTrue({"len", "keys", "values", "entries", "has", "remove", "copy", "map"} <= labels)
        self.assertNotIn("push", labels)

    def test_hover_on_native_method(self):
        src = "let v = [1]\nv.push(2)\n"
        line, col = _find(src, "push")
        hover = analysis.get_hover(src, line, col)
        self.assertIn("`push` on `Vector`", hover["contents"]["value"])
        self.assertIn("fn push(self, value)", hover["contents"]["value"])


if __name__ == "__main__":
    unittest.main()
