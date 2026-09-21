"""Tests for M7 -- a real symbol table built into `compiler/resolve.py`'s
normal resolve pass, and the two LSP features built directly on it:
go-to-definition (rebuilt on the resolver's own `Symbol`/`position_index`
records instead of the old, independent token-scanning scope model) and
rename (new).

See docs/V2_DESIGN.md's M7 milestone. Scope, deliberately not covered here
(both by this test file and by the feature itself, as of M7):

  - renaming a struct/enum type name or a struct/enum field name -- those
    live in `Resolver.struct_decls`/`enum_decls`, a separate namespace with
    different reference-tracking needs, out of scope for this milestone;
  - hover/completion/document-symbols are not revived by this milestone.

M11 note: scenario 8 below (`CrossFileRenameRefusalTests`) originally
asserted that a symbol from an import refused rename outright -- M11 lifts
that restriction (see docs/V2_DESIGN.md's M11 milestone and
`tests/test_rename_types_fields_and_cross_file.py` for full cross-file
rename coverage), so this scenario now asserts the opposite: the rename
succeeds and correctly spans both files. Kept here (renamed to
`test_symbol_from_an_import_now_renames_across_files`) rather than deleted,
since it's still a real regression check on the exact boundary M7
originally drew.

Calls `lsp.analysis.get_definition`/`get_rename_edits` directly (no
JSON-RPC/`lsp.server` plumbing needed) -- mirrors
`tests/test_error_recovery.py`'s `LspDiagnosticsSmokeTests` pattern for
exercising `lsp/analysis.py` directly.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mah.lsp import analysis  # noqa: E402


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _pos(text: str, offset: int) -> dict:
    return analysis.offset_to_position(text, offset)


def _definition_at(text: str, offset: int, path=None):
    p = _pos(text, offset)
    return analysis.get_definition(text, p["line"], p["character"], path)


def _rename_at(text: str, offset: int, new_name: str, path=None):
    p = _pos(text, offset)
    return analysis.get_rename_edits(text, p["line"], p["character"], new_name, path)


def _only_edits(rename_result: dict) -> list:
    """A rename result's `changes` always has exactly one key -- M7's
    rename is single-file only (see scope boundary above) -- so there's
    never a need to pick a particular key."""
    return next(iter(rename_result["changes"].values()))


def _apply_edits(text: str, edits: list) -> str:
    """Apply a list of LSP TextEdits to `text`, for asserting on the
    resulting source rather than trusting each edit's range/newText in
    isolation."""
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


# --------------------------------------------------------------------------
# 1-3: go-to-definition
# --------------------------------------------------------------------------

class GoToDefinitionTests(unittest.TestCase):
    def test_variable_use_jumps_to_let_declaration(self):
        text = "let x = 5\nprint(x)\n"
        decl_offset = text.index("x")       # the `x` in `let x = 5`
        use_offset = text.rindex("x")       # the `x` in `print(x)`
        self.assertNotEqual(decl_offset, use_offset)

        result = _definition_at(text, use_offset)
        self.assertIsNotNone(result)
        self.assertIsNone(result["path"])  # current document
        self.assertEqual(
            result["range"],
            analysis.make_range(text, decl_offset, decl_offset + 1),
        )

    def test_function_call_jumps_to_fn_declaration(self):
        text = "fn add(a, b) { return a + b }\nprint(add(1, 2))\n"
        decl_offset = text.index("add")
        use_offset = text.rindex("add")
        self.assertNotEqual(decl_offset, use_offset)

        result = _definition_at(text, use_offset)
        self.assertIsNotNone(result)
        self.assertIsNone(result["path"])
        self.assertEqual(
            result["range"],
            analysis.make_range(text, decl_offset, decl_offset + len("add")),
        )

    def test_parameter_use_inside_body_jumps_to_its_own_signature_site(self):
        """Exercises `FnExpr.param_positions` (M7's small parser addition):
        without it, a parameter's declaration position would fall back to
        the whole `fn` expression's position (pointing at the `fn`
        keyword), not the parameter's own token."""
        text = "fn double(n) { return n * 2 }\n"
        decl_offset = text.index("(n)") + 1  # the `n` between the parens
        use_offset = text.index("n * 2")     # the `n` used in the body
        self.assertNotEqual(decl_offset, use_offset)

        result = _definition_at(text, use_offset)
        self.assertIsNotNone(result)
        self.assertIsNone(result["path"])
        self.assertEqual(
            result["range"],
            analysis.make_range(text, decl_offset, decl_offset + 1),
        )


# --------------------------------------------------------------------------
# 4-6: rename
# --------------------------------------------------------------------------

class RenameTests(unittest.TestCase):
    def test_rename_variable_updates_every_occurrence(self):
        text = "let count = 0\ncount = count + 1\nprint(count)\n"
        cursor_offset = text.rindex("count")  # the `print(count)` argument -- any occurrence works
        result = _rename_at(text, cursor_offset, "total")
        self.assertIsNotNone(result)
        edits = _only_edits(result)
        self.assertEqual(len(edits), 4)
        self.assertTrue(all(e["newText"] == "total" for e in edits))
        self.assertEqual(
            _apply_edits(text, edits),
            "let total = 0\ntotal = total + 1\nprint(total)\n",
        )

    def test_rename_function_updates_declaration_and_call_sites_only(self):
        text = 'fn greet(name) { print(name) }\ngreet("a")\ngreet("b")\n'
        cursor_offset = text.index('greet("a")')
        result = _rename_at(text, cursor_offset, "salute")
        self.assertIsNotNone(result)
        edits = _only_edits(result)
        self.assertEqual(len(edits), 3)
        self.assertTrue(all(e["newText"] == "salute" for e in edits))
        self.assertEqual(
            _apply_edits(text, edits),
            'fn salute(name) { print(name) }\nsalute("a")\nsalute("b")\n',
        )

    def test_rename_shadowed_inner_variable_does_not_touch_outer(self):
        text = (
            "let x = 1\n"
            "if true {\n"
            "    let x = 2\n"
            "    print(x)\n"
            "}\n"
            "print(x)\n"
        )
        inner_print_offset = text.index("print(x)")           # inside the if-block
        outer_print_offset = text.rindex("print(x)")
        self.assertNotEqual(inner_print_offset, outer_print_offset)
        inner_x_use_offset = text.index("x)", inner_print_offset)

        result = _rename_at(text, inner_x_use_offset, "y")
        self.assertIsNotNone(result)
        edits = _only_edits(result)
        self.assertEqual(len(edits), 2)  # the inner `let x` and its `print(x)` only
        self.assertEqual(
            _apply_edits(text, edits),
            (
                "let x = 1\n"
                "if true {\n"
                "    let y = 2\n"
                "    print(y)\n"
                "}\n"
                "print(x)\n"
            ),
        )

    def test_rename_outer_variable_does_not_touch_shadowing_inner(self):
        """The mirror of the previous scenario: renaming the *outer* `x`
        must leave the inner, shadowing `let x = 2`/`print(x)` untouched."""
        text = (
            "let x = 1\n"
            "if true {\n"
            "    let x = 2\n"
            "    print(x)\n"
            "}\n"
            "print(x)\n"
        )
        outer_decl_offset = text.index("x")

        result = _rename_at(text, outer_decl_offset, "y")
        self.assertIsNotNone(result)
        edits = _only_edits(result)
        self.assertEqual(len(edits), 2)  # the outer `let x` and the final `print(x)` only
        self.assertEqual(
            _apply_edits(text, edits),
            (
                "let y = 1\n"
                "if true {\n"
                "    let x = 2\n"
                "    print(x)\n"
                "}\n"
                "print(y)\n"
            ),
        )


# --------------------------------------------------------------------------
# 7: invalid new names refused
# --------------------------------------------------------------------------

class InvalidNewNameTests(unittest.TestCase):
    def test_reserved_keyword_refused(self):
        text = "let x = 5\nprint(x)\n"
        self.assertIsNone(_rename_at(text, text.index("x"), "if"))

    def test_invalid_identifier_refused(self):
        text = "let x = 5\nprint(x)\n"
        self.assertIsNone(_rename_at(text, text.index("x"), "1bad"))


# --------------------------------------------------------------------------
# 8: cross-file symbols now rename correctly (M11 lifts M7's refusal)
# --------------------------------------------------------------------------

class CrossFileRenameRefusalTests(unittest.TestCase):
    def test_symbol_from_an_import_now_renames_across_files(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            # A Makefile marker so `_find_workspace_root` finds this
            # directory deterministically (see analysis.py's docstring).
            with open(os.path.join(tmp_dir, "Makefile"), "w", encoding="utf-8") as handle:
                handle.write("")
            lib_path = os.path.join(tmp_dir, "lib.mh")
            main_path = os.path.join(tmp_dir, "main.mh")
            with open(lib_path, "w", encoding="utf-8") as handle:
                handle.write("export let shared = 10\n")
            with open(main_path, "w", encoding="utf-8") as handle:
                handle.write('import "lib.mh"\nprint(shared)\n')

            with open(main_path, encoding="utf-8") as handle:
                main_text = handle.read()
            use_offset = main_text.index("shared")

            # Go-to-definition still works (it's allowed to jump cross-file).
            definition = _definition_at(main_text, use_offset, main_path)
            self.assertIsNotNone(definition)
            self.assertEqual(definition["path"], lib_path)

            # M11: renaming it now succeeds, and spans BOTH files.
            result = _rename_at(main_text, use_offset, "renamed", main_path)
            self.assertIsNotNone(result)
            self.assertEqual(set(result["changes"].keys()), {lib_path, main_path})
            lib_edits = result["changes"][lib_path]
            main_edits = result["changes"][main_path]
            self.assertEqual(len(lib_edits), 1)
            self.assertEqual(len(main_edits), 1)
            self.assertTrue(all(e["newText"] == "renamed" for e in lib_edits + main_edits))


# --------------------------------------------------------------------------
# 9: a file with a resolve error has no symbol table available
# --------------------------------------------------------------------------

class ResolveErrorTests(unittest.TestCase):
    def test_undefined_variable_yields_no_definition_or_rename(self):
        text = "print(y)\n"  # `y` is never declared -- a resolve error
        offset = text.index("y")
        self.assertIsNone(_definition_at(text, offset))
        self.assertIsNone(_rename_at(text, offset, "z"))


# --------------------------------------------------------------------------
# 11: the old token-scanning scope model is actually gone
# --------------------------------------------------------------------------

class RetiredScopeModelTests(unittest.TestCase):
    def test_old_scope_functions_and_class_removed(self):
        analysis_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mah", "lsp", "analysis.py"
        )
        with open(analysis_path, encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn("def _build_scopes", source)
        self.assertNotIn("def _resolve_declaration", source)
        self.assertNotIn("class _Scope", source)

    def test_lsp_modules_import_cleanly(self):
        from mah.lsp import analysis as analysis_mod  # noqa: F401
        from mah.lsp import server as server_mod  # noqa: F401

        self.assertTrue(hasattr(analysis_mod, "get_definition"))
        self.assertTrue(hasattr(analysis_mod, "get_rename_edits"))
        self.assertTrue(hasattr(server_mod.Server, "_on_textDocument_rename"))


if __name__ == "__main__":
    unittest.main()
