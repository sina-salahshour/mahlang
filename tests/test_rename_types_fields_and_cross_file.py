"""Tests for M11 -- extending the LSP's rename feature (single-file-only
since M7) to cover:

  - cross-file rename of variables/functions/parameters (a real workspace-
    wide reverse-import-graph search, not just the currently open file's
    own import closure);
  - struct/enum type name rename;
  - enum variant name rename;
  - struct/enum field name rename, in declarations, literals, and
    *explicit* (non-shorthand) patterns -- never through plain field
    access (`p.x`), which stays deliberately refused (unsound without a
    real type system).

See docs/V2_DESIGN.md's M11 milestone, `compiler/resolve.py`'s
`field_position_index`/`type_position_index` docstrings, and
`lsp/analysis.py`'s `get_rename_edits`/`_rename_variable_cross_file`.

Follows `tests/test_lsp_rename.py`'s own helper/assertion patterns
(direct calls into `lsp.analysis`, no JSON-RPC plumbing needed, except for
the one test that specifically exercises `lsp.server.Server`'s
multi-file-URI passthrough).
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mah.lsp import analysis  # noqa: E402


# --------------------------------------------------------------------------
# Helpers (mirrors tests/test_lsp_rename.py)
# --------------------------------------------------------------------------

def _pos(text: str, offset: int) -> dict:
    return analysis.offset_to_position(text, offset)


def _rename_at(text: str, offset: int, new_name: str, path=None):
    p = _pos(text, offset)
    return analysis.get_rename_edits(text, p["line"], p["character"], new_name, path)


def _definition_at(text: str, offset: int, path=None):
    p = _pos(text, offset)
    return analysis.get_definition(text, p["line"], p["character"], path)


def _apply_edits(text: str, edits: list) -> str:
    spans = []
    for edit in edits:
        start = analysis.position_to_offset(
            text, edit["range"]["start"]["line"], edit["range"]["start"]["character"]
        )
        end = analysis.position_to_offset(
            text, edit["range"]["end"]["line"], edit["range"]["end"]["character"]
        )
        spans.append((start, end, edit["newText"]))
    spans.sort(key=lambda s: s[0], reverse=True)  # apply back-to-front
    result = text
    for start, end, new_text in spans:
        result = result[:start] + new_text + result[end:]
    return result


def _only_edits(rename_result: dict) -> list:
    """For a single-file rename result -- asserts there's exactly one key
    and returns its edits."""
    changes = rename_result["changes"]
    assert len(changes) == 1, f"expected exactly one file in changes, got {list(changes)}"
    return next(iter(changes.values()))


# --------------------------------------------------------------------------
# 1-2: struct/enum type name rename
# --------------------------------------------------------------------------

class TypeNameRenameTests(unittest.TestCase):
    def test_rename_struct_type_name_updates_decl_literal_and_pattern(self):
        text = (
            "struct Point { x, y }\n"
            "let p = Point { x: 1, y: 2 }\n"
            "match p { Point { x, y } => { print(x) } }\n"
        )
        decl_offset = text.index("Point")
        result = _rename_at(text, decl_offset, "Coord")
        self.assertIsNotNone(result)
        edits = _only_edits(result)
        # declaration + literal use + pattern use = 3 occurrences
        self.assertEqual(len(edits), 3)
        self.assertTrue(all(e["newText"] == "Coord" for e in edits))
        applied = _apply_edits(text, edits)
        self.assertEqual(applied.count("Coord"), 3)
        self.assertNotIn("Point", applied)

    def test_rename_enum_type_name_updates_decl_literal_and_pattern(self):
        text = (
            "enum Shape { Circle { r }, Empty }\n"
            "let s = Shape.Circle { r: 5 }\n"
            "match s { Shape.Circle { r } => { print(r) } Shape.Empty => { print(0) } }\n"
        )
        decl_offset = text.index("Shape")
        result = _rename_at(text, decl_offset, "Figure")
        self.assertIsNotNone(result)
        edits = _only_edits(result)
        # declaration + literal use + 2 pattern uses (Circle arm, Empty arm) = 4
        self.assertEqual(len(edits), 4)
        self.assertTrue(all(e["newText"] == "Figure" for e in edits))
        applied = _apply_edits(text, edits)
        self.assertEqual(applied.count("Figure"), 4)
        self.assertNotIn("Shape", applied)


# --------------------------------------------------------------------------
# 3: enum variant name rename, scoped to that variant only
# --------------------------------------------------------------------------

class VariantNameRenameTests(unittest.TestCase):
    def test_rename_variant_updates_uses_but_not_sibling_variant(self):
        text = (
            "enum Shape { Circle { r }, Empty }\n"
            "let s = Shape.Circle { r: 5 }\n"
            "match s { Shape.Circle { r } => { print(r) } Shape.Empty => { print(0) } }\n"
        )
        use_offset = text.index("Circle")  # declaration site
        result = _rename_at(text, use_offset, "Round")
        self.assertIsNotNone(result)
        edits = _only_edits(result)
        # declaration + literal use + pattern use = 3
        self.assertEqual(len(edits), 3)
        self.assertTrue(all(e["newText"] == "Round" for e in edits))
        applied = _apply_edits(text, edits)
        self.assertEqual(applied.count("Round"), 3)
        self.assertNotIn("Circle", applied)
        # Sibling variant untouched.
        self.assertIn("Empty", applied)


# --------------------------------------------------------------------------
# 4: struct field name rename, scoped to the targeted struct only
# --------------------------------------------------------------------------

class StructFieldRenameTests(unittest.TestCase):
    def test_rename_struct_field_does_not_touch_unrelated_struct_with_same_field_name(self):
        text = (
            "struct Point { x, y }\n"
            "struct Vec2 { x, y }\n"
            "let p = Point { x: 1, y: 2 }\n"
            "let v = Vec2 { x: 3, y: 4 }\n"
            "match p { Point { x: px, y: py } => { print(px) } }\n"
        )
        decl_offset = text.index("x")  # the `x` in `struct Point { x, y }`
        result = _rename_at(text, decl_offset, "px_coord")
        self.assertIsNotNone(result)
        edits = _only_edits(result)
        # Point's field decl + Point-literal's `x:` label + Point-pattern's
        # explicit `x:` label = 3 -- Vec2's own `x` must be untouched.
        self.assertEqual(len(edits), 3)
        self.assertTrue(all(e["newText"] == "px_coord" for e in edits))
        applied = _apply_edits(text, edits)
        self.assertIn("struct Point { px_coord, y }", applied)
        self.assertIn("struct Vec2 { x, y }", applied)  # untouched
        self.assertIn("Point { px_coord: 1, y: 2 }", applied)
        self.assertIn("Vec2 { x: 3, y: 4 }", applied)  # untouched
        self.assertIn("Point { px_coord: px, y: py }", applied)


# --------------------------------------------------------------------------
# 5: renaming at a shorthand pattern field position renames the local
# variable binding (today's M7 behavior), never the field itself
# --------------------------------------------------------------------------

class ShorthandPatternFieldRenameTests(unittest.TestCase):
    def test_shorthand_field_rename_is_pure_variable_rename(self):
        text = (
            "struct Point { x, y }\n"
            "let p = Point { x: 1, y: 2 }\n"
            "match p { Point { x, y } => { print(x) } }\n"
        )
        shorthand_offset = text.index("{ x, y } =>") + 2  # the bare `x`
        self.assertEqual(text[shorthand_offset], "x")
        result = _rename_at(text, shorthand_offset, "renamed_var")
        self.assertIsNotNone(result)
        edits = _only_edits(result)
        # Only the shorthand binding itself + its use in print(x) -- NOT
        # the struct's own field declaration, NOT the literal's `x:` label.
        self.assertEqual(len(edits), 2)
        applied = _apply_edits(text, edits)
        self.assertIn("struct Point { x, y }", applied)  # field decl untouched
        self.assertIn("Point { x: 1, y: 2 }", applied)   # literal label untouched
        self.assertIn("Point { renamed_var, y } => { print(renamed_var) }", applied)


# --------------------------------------------------------------------------
# 6: enum variant field name rename, scoped to that variant only
# --------------------------------------------------------------------------

class EnumVariantFieldRenameTests(unittest.TestCase):
    def test_rename_variant_field_does_not_touch_other_variant_with_same_field_name(self):
        text = (
            "enum Shape {\n"
            "\tCircle { r },\n"
            "\tSquare { r },\n"
            "\tEmpty\n"
            "}\n"
            "let c = Shape.Circle { r: 5 }\n"
            "let sq = Shape.Square { r: 9 }\n"
            "match c { Shape.Circle { r: radius } => { print(radius) } _ => { print(0) } }\n"
        )
        decl_offset = text.index("r }")  # `r` in `Circle { r }`
        self.assertEqual(text[decl_offset], "r")
        result = _rename_at(text, decl_offset, "radius_field")
        self.assertIsNotNone(result)
        edits = _only_edits(result)
        # Circle's field decl + Circle-literal's `r:` label + Circle-pattern's
        # explicit `r:` label = 3 -- Square's own `r` must be untouched.
        self.assertEqual(len(edits), 3)
        applied = _apply_edits(text, edits)
        self.assertIn("Circle { radius_field }", applied)
        self.assertIn("Square { r }", applied)  # untouched
        self.assertIn("Shape.Circle { radius_field: 5 }", applied)
        self.assertIn("Shape.Square { r: 9 }", applied)  # untouched
        self.assertIn("Shape.Circle { radius_field: radius }", applied)


# --------------------------------------------------------------------------
# 7: field ACCESS is deliberately refused, not guessed at
# --------------------------------------------------------------------------

class FieldAccessRenameRefusalTests(unittest.TestCase):
    def test_plain_field_access_rename_refused(self):
        text = (
            "struct Point { x, y }\n"
            "let p = Point { x: 1, y: 2 }\n"
            "print(p.x)\n"
        )
        access_offset = text.rindex("p.x") + 2  # the `x` in `p.x`
        self.assertEqual(text[access_offset], "x")
        self.assertIsNone(_rename_at(text, access_offset, "renamed"))

    def test_plain_field_access_assignment_rename_refused(self):
        text = (
            "struct Point { x, y }\n"
            "let p = Point { x: 1, y: 2 }\n"
            "p.x = 9\n"
        )
        access_offset = text.rindex("p.x") + 2
        self.assertEqual(text[access_offset], "x")
        self.assertIsNone(_rename_at(text, access_offset, "renamed"))


# --------------------------------------------------------------------------
# 8-10: cross-file variable/function rename
# --------------------------------------------------------------------------

def _write(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


class CrossFileRenameTests(unittest.TestCase):
    def _make_workspace(self, tmp_dir: str, importer2_body: str = None):
        # A Makefile marker so `_find_workspace_root` finds `tmp_dir`
        # deterministically, matching its exact documented fallback
        # behavior (walk up looking for `.git`/`Makefile`).
        _write(os.path.join(tmp_dir, "Makefile"), "")
        lib_path = os.path.join(tmp_dir, "lib.mh")
        importer_ns_path = os.path.join(tmp_dir, "importer_ns.mh")
        importer_flat_path = os.path.join(tmp_dir, "importer_flat.mh")
        _write(lib_path, "export fn square(n) {\n\treturn n * n\n}\n")
        _write(importer_ns_path, 'import math from "lib"\nprint(math.square(3))\n')
        _write(
            importer_flat_path,
            importer2_body or 'import "lib"\nprint(square(4))\n',
        )
        return lib_path, importer_ns_path, importer_flat_path

    def test_rename_from_one_importer_touches_all_three_files(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            lib_path, importer_ns_path, importer_flat_path = self._make_workspace(tmp_dir)

            with open(importer_ns_path, encoding="utf-8") as handle:
                ns_text = handle.read()
            use_offset = ns_text.index("math.square") + len("math.")

            result = _rename_at(ns_text, use_offset, "sq", importer_ns_path)
            self.assertIsNotNone(result)
            changes = result["changes"]
            self.assertEqual(set(changes.keys()), {lib_path, importer_ns_path, importer_flat_path})

            lib_edits = changes[lib_path]
            self.assertEqual(len(lib_edits), 1)
            self.assertEqual(lib_edits[0]["newText"], "sq")

            ns_edits = changes[importer_ns_path]
            self.assertEqual(len(ns_edits), 1)
            applied_ns = _apply_edits(ns_text, ns_edits)
            self.assertIn("math.sq(3)", applied_ns)

            with open(importer_flat_path, encoding="utf-8") as handle:
                flat_text = handle.read()
            flat_edits = changes[importer_flat_path]
            self.assertEqual(len(flat_edits), 1)
            applied_flat = _apply_edits(flat_text, flat_edits)
            self.assertIn("sq(4)", applied_flat)

            with open(lib_path, encoding="utf-8") as handle:
                lib_text = handle.read()
            applied_lib = _apply_edits(lib_text, lib_edits)
            self.assertIn("fn sq(n)", applied_lib)

    def test_rename_from_declaring_file_itself_touches_both_importers(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            lib_path, importer_ns_path, importer_flat_path = self._make_workspace(tmp_dir)

            with open(lib_path, encoding="utf-8") as handle:
                lib_text = handle.read()
            decl_offset = lib_text.index("square")

            result = _rename_at(lib_text, decl_offset, "sq", lib_path)
            self.assertIsNotNone(result)
            changes = result["changes"]
            self.assertEqual(set(changes.keys()), {lib_path, importer_ns_path, importer_flat_path})
            self.assertEqual(len(changes[lib_path]), 1)
            self.assertEqual(len(changes[importer_ns_path]), 1)
            self.assertEqual(len(changes[importer_flat_path]), 1)

    def test_cross_file_rename_refuses_when_an_importer_has_a_syntax_error(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            lib_path, importer_ns_path, importer_flat_path = self._make_workspace(
                tmp_dir, importer2_body='import "lib"\nprint(square(4)\n'  # missing `)`
            )
            with open(importer_ns_path, encoding="utf-8") as handle:
                ns_text = handle.read()
            use_offset = ns_text.index("math.square") + len("math.")
            result = _rename_at(ns_text, use_offset, "sq", importer_ns_path)
            self.assertIsNone(result)


# --------------------------------------------------------------------------
# 11: single-file M7 rename (no imports at all) still works unchanged
# --------------------------------------------------------------------------

class SingleFileRegressionTests(unittest.TestCase):
    def test_single_file_variable_rename_with_no_imports_still_works(self):
        text = "let total = 0\ntotal = total + 5\nprint(total)\n"
        cursor_offset = text.rindex("total")
        result = _rename_at(text, cursor_offset, "sum")
        self.assertIsNotNone(result)
        edits = _only_edits(result)
        self.assertEqual(len(edits), 4)
        self.assertEqual(
            _apply_edits(text, edits),
            "let sum = 0\nsum = sum + 5\nprint(sum)\n",
        )


# --------------------------------------------------------------------------
# 12: server-level multi-file passthrough
# --------------------------------------------------------------------------

class ServerMultiFileRenameTests(unittest.TestCase):
    def test_server_rename_produces_multi_uri_workspace_edit(self):
        from mah.lsp import server

        with tempfile.TemporaryDirectory() as tmp_dir:
            _write(os.path.join(tmp_dir, "Makefile"), "")
            lib_path = os.path.join(tmp_dir, "lib.mh")
            importer_ns_path = os.path.join(tmp_dir, "importer_ns.mh")
            importer_flat_path = os.path.join(tmp_dir, "importer_flat.mh")
            _write(lib_path, "export fn square(n) {\n\treturn n * n\n}\n")
            _write(importer_ns_path, 'import math from "lib"\nprint(math.square(3))\n')
            _write(importer_flat_path, 'import "lib"\nprint(square(4))\n')

            with open(importer_ns_path, encoding="utf-8") as handle:
                ns_text = handle.read()

            responses = []
            srv = server.Server(stdin=None, stdout=None)
            srv._respond = lambda request_id, result: responses.append(result)  # noqa: SLF001

            entry_uri = server.path_to_uri(importer_ns_path)
            srv._documents[entry_uri] = ns_text  # noqa: SLF001

            idx = ns_text.index("math.square") + len("math.")
            line = ns_text.count("\n", 0, idx)
            col = idx - (ns_text.rfind("\n", 0, idx) + 1)

            srv._on_textDocument_rename(  # noqa: SLF001
                1,
                {
                    "textDocument": {"uri": entry_uri},
                    "position": {"line": line, "character": col},
                    "newName": "sq",
                },
            )

            self.assertEqual(len(responses), 1)
            result = responses[0]
            self.assertIsNotNone(result)
            changes = result["changes"]
            self.assertEqual(len(changes), 3)

            expected_uris = {
                entry_uri,
                server.path_to_uri(lib_path),
                server.path_to_uri(importer_flat_path),
            }
            self.assertEqual(set(changes.keys()), expected_uris)
            for uri, edits in changes.items():
                self.assertTrue(uri.startswith("file:///"))
                self.assertEqual(len(edits), 1)
                self.assertEqual(edits[0]["newText"], "sq")


if __name__ == "__main__":
    unittest.main()
