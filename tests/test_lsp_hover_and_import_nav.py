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

from mah.lsp import analysis

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

    def test_hover_on_loop_keywords(self):
        src = "let x = for let v, let i in 1..3 { if v > 1 { break v } continue }\nwhile false { }\n"
        for kw in ("for", "in", "break", "continue", "while"):
            line, col = _find(src, kw)
            hover = analysis.get_hover(src, line, col)
            self.assertIsNotNone(hover, f"hover on {kw!r} returned None")
            self.assertIn(f"**keyword** `{kw}`", hover["contents"]["value"])
        line, col = _find(src, "for")
        self.assertIn("Iterable", analysis.get_hover(src, line, col)["contents"]["value"])

    def test_for_loop_bindings_hover_and_definition(self):
        src = "for let item, let idx in 1..3 {\n    print(item, idx)\n}\n"
        for name in ("item", "idx"):
            decl_line, decl_col = _find(src, name)
            use_line, use_col = _find(src, name, occurrence=1)
            hover = analysis.get_hover(src, use_line, use_col)
            self.assertIsNotNone(hover)
            self.assertIn(f"`{name}`", hover["contents"]["value"])
            definition = analysis.get_definition(src, use_line, use_col)
            self.assertEqual(definition["range"]["start"], {"line": decl_line, "character": decl_col})

    def test_hover_on_undefined_identifier_returns_none(self):
        # No symbol table entry (undefined reference) -- must degrade to
        # None, not crash.
        hover = analysis.get_hover("print(nope)\n", 0, 6)
        self.assertIsNone(hover)


class AsyncHoverTests(unittest.TestCase):
    """M10: `defer` (a pre-existing gap -- landed in M9 but never added to
    KEYWORD_TOKENS/KEYWORD_DOCS), plus the new `detach`/`sleep_async`
    keywords and `.await`'s positional (non-token) hover."""

    def test_hover_on_defer(self):
        src = 'fn f() { defer print("x") }\n'
        line, col = _find(src, "defer")
        hover = analysis.get_hover(src, line, col)
        self.assertIsNotNone(hover)
        self.assertIn("keyword", hover["contents"]["value"])
        self.assertIn("defer", hover["contents"]["value"])

    def test_hover_on_detach(self):
        src = "fn foo() { }\ndetach foo()\n"
        line, col = _find(src, "detach")
        hover = analysis.get_hover(src, line, col)
        self.assertIsNotNone(hover)
        self.assertIn("keyword", hover["contents"]["value"])
        self.assertIn("detach", hover["contents"]["value"])

    def test_hover_on_sleep_async(self):
        src = "let p = sleep_async(10)\n"
        line, col = _find(src, "sleep_async")
        hover = analysis.get_hover(src, line, col)
        self.assertIsNotNone(hover)
        self.assertIn("builtin", hover["contents"]["value"])
        self.assertIn("sleep_async", hover["contents"]["value"])

    def test_hover_on_await_field(self):
        src = "let p = sleep_async(10)\nprint(p.await)\n"
        line, col = _find(src, "await")
        hover = analysis.get_hover(src, line, col)
        self.assertIsNotNone(hover)
        self.assertIn("keyword", hover["contents"]["value"])
        self.assertIn("await", hover["contents"]["value"])

    def test_hover_on_bare_await_identifier_is_not_treated_as_keyword(self):
        # An ordinary variable named `await` (not preceded by `.`) must not
        # be mistaken for the `.await` pseudo-field.
        src = "let await = 5\nprint(await)\n"
        line, col = _find(src, "await", occurrence=1)
        hover = analysis.get_hover(src, line, col)
        self.assertIsNotNone(hover)
        self.assertNotIn("**keyword**", hover["contents"]["value"])


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


class PathToUriTests(unittest.TestCase):
    """Regression tests for a bug found via a real cross-file go-to-
    definition in VS Code: `mah.lsp.server.path_to_uri` used to build a
    URI as `"file://" + urllib.request.pathname2url(path)`, but a CPython
    stdlib behavior change made `pathname2url` itself prepend an
    authority-separating `//` before an absolute POSIX path's leading `/`
    -- combined with `path_to_uri`'s own `"file://"` prefix, this produced
    a malformed 5-slash URI (`file://///tmp/...` instead of the correct
    `file:///tmp/...`). Same-file go-to-definition never calls this
    function at all (see `_on_textDocument_definition`'s `if target_path`
    check), which is why this was invisible in every prior test -- they
    all either called `analysis.get_definition` directly (bypassing
    `server.py`'s URI layer entirely) or exercised only same-file jumps.
    """

    def test_path_to_uri_produces_exactly_three_slashes(self):
        from mah.lsp import server

        uri = server.path_to_uri("/tmp/some/file.mh")
        self.assertEqual(uri, "file:///tmp/some/file.mh")
        self.assertNotIn("////", uri)

    def test_server_cross_file_definition_produces_well_formed_uri(self):
        """End-to-end: drive `Server._on_textDocument_definition` (not
        just `analysis.get_definition`) for a real cross-file jump, the
        exact path the bug lived in."""
        from mah.lsp import server

        tmp_dir = os.path.join(EXAMPLES_DIR, "_path_to_uri_regression")
        os.makedirs(tmp_dir, exist_ok=True)
        lib_path = os.path.join(tmp_dir, "lib.mh")
        entry_path = os.path.join(tmp_dir, "entry.mh")
        try:
            with open(lib_path, "w") as f:
                f.write("export fn helper(n) {\n\treturn n\n}\n")
            entry_text = 'import lib from "lib"\n\nprint(lib.helper(1))\n'
            with open(entry_path, "w") as f:
                f.write(entry_text)

            responses = []
            srv = server.Server(stdin=None, stdout=None)
            srv._respond = lambda request_id, result: responses.append(result)  # noqa: SLF001

            entry_uri = server.path_to_uri(entry_path)
            srv._documents[entry_uri] = entry_text  # noqa: SLF001

            idx = entry_text.index("lib.helper") + len("lib.")
            line = entry_text.count("\n", 0, idx)
            col = idx - (entry_text.rfind("\n", 0, idx) + 1)
            srv._on_textDocument_definition(  # noqa: SLF001
                1, {"textDocument": {"uri": entry_uri}, "position": {"line": line, "character": col}}
            )

            self.assertEqual(len(responses), 1)
            result = responses[0]
            self.assertIsNotNone(result)
            self.assertNotIn("////", result["uri"])
            self.assertTrue(result["uri"].startswith("file:///"))
            self.assertEqual(server.uri_to_path(result["uri"]), os.path.realpath(lib_path))
        finally:
            for name in ("lib.mh", "entry.mh"):
                p = os.path.join(tmp_dir, name)
                if os.path.exists(p):
                    os.remove(p)
            if os.path.isdir(tmp_dir):
                os.rmdir(tmp_dir)


if __name__ == "__main__":
    unittest.main()
