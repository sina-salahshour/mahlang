"""M44 (docs/contracts/M44_threads.md §11.3): the LSP on threads syntax.

`shared` and `lock` are contextual keywords (ordinary `ID` tokens), so hover
recognizes them positionally; a `shared let` hovers as a shared variable;
go-to-definition and rename work through `lock` targets; completion offers
both keywords; resolver errors (E1) and checker warnings (W1) show up as
diagnostics. Calls `mah.lsp.analysis` directly, like the other LSP tests.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mah.lsp import analysis  # noqa: E402


def _pos(text: str, offset: int) -> dict:
    return analysis.offset_to_position(text, offset)


def _hover(text: str, offset: int, path=None):
    p = _pos(text, offset)
    result = analysis.get_hover(text, p["line"], p["character"], path)
    return None if result is None else result["contents"]["value"]


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
    spans.sort(key=lambda s: s[0], reverse=True)
    for start, end, new_text in spans:
        text = text[:start] + new_text + text[end:]
    return text


SOURCE = "shared let n = 0\nlock n { n = n + 1 }\nprint(n)\n"


class ThreadHoverTests(unittest.TestCase):
    def test_shared_is_a_keyword_before_let(self):
        value = _hover(SOURCE, SOURCE.index("shared"))
        self.assertIsNotNone(value)
        self.assertTrue(value.startswith("**keyword** `shared`"), value)
        self.assertIn("lock NAME", value)

    def test_lock_is_a_keyword_before_a_name(self):
        value = _hover(SOURCE, SOURCE.index("lock"))
        self.assertIsNotNone(value)
        self.assertTrue(value.startswith("**keyword** `lock`"), value)
        self.assertIn("written back", value)

    def test_lock_as_a_variable_is_a_variable(self):
        text = "let lock = 2\nprint(lock)\n"
        value = _hover(text, text.index("lock"))
        self.assertIsNotNone(value)
        self.assertTrue(value.startswith("**variable** `lock`"), value)
        value = _hover(text, text.rindex("lock"))
        self.assertTrue(value.startswith("**variable** `lock`"), value)

    def test_shared_as_a_variable_is_a_variable(self):
        text = "let shared = 1\nprint(shared)\n"
        value = _hover(text, text.rindex("shared"))
        self.assertIsNotNone(value)
        self.assertTrue(value.startswith("**variable** `shared`"), value)

    def test_a_shared_variable_hovers_as_one_with_its_type(self):
        value = _hover(SOURCE, SOURCE.index("print(n)") + len("print("))
        self.assertIsNotNone(value)
        self.assertTrue(value.startswith("**shared variable** `n`"), value)
        self.assertIn("n: Number", value)

    def test_detach_hover_mentions_the_thread_form(self):
        value = _hover("let p = detach 1\n", len("let p = "))
        self.assertIn("detach(t) expr", value)


class ThreadNavigationTests(unittest.TestCase):
    def test_definition_from_a_lock_target(self):
        offset = SOURCE.index("lock n") + len("lock ")
        p = _pos(SOURCE, offset)
        result = analysis.get_definition(SOURCE, p["line"], p["character"], None)
        self.assertIsNotNone(result)
        start = analysis.position_to_offset(
            SOURCE, result["range"]["start"]["line"], result["range"]["start"]["character"]
        )
        self.assertEqual(start, SOURCE.index("n = 0"))

    def test_rename_edits_the_declaration_the_lock_target_and_every_use(self):
        offset = SOURCE.index("print(n)") + len("print(")
        p = _pos(SOURCE, offset)
        result = analysis.get_rename_edits(SOURCE, p["line"], p["character"], "hits", None)
        self.assertIsNotNone(result)
        edits = next(iter(result["changes"].values()))
        self.assertEqual(
            _apply_edits(SOURCE, edits),
            "shared let hits = 0\nlock hits { hits = hits + 1 }\nprint(hits)\n",
        )


class ThreadCompletionTests(unittest.TestCase):
    def test_shared_and_lock_are_offered_as_keywords(self):
        items = analysis.get_completions("let x = 1\n")
        keywords = {item["label"] for item in items if item.get("detail") == "keyword"}
        self.assertIn("shared", keywords)
        self.assertIn("lock", keywords)

    def test_a_shared_variable_completes_as_one(self):
        text = "shared let counter = 0\nprint(counter)\n"
        offset = text.index("print(") + len("print(")
        p = _pos(text, offset)
        items = analysis.get_completions(text, None, p["line"], p["character"])
        details = [item["detail"] for item in items if item["label"] == "counter"]
        self.assertIn("shared variable", details)


class ThreadDiagnosticsTests(unittest.TestCase):
    def test_a_method_call_outside_lock_is_an_error(self):
        text = "shared let xs = []\nxs.push(1)\n"
        diagnostics = analysis.get_diagnostics(text)
        messages = [d["message"] for d in diagnostics]
        self.assertTrue(
            any("Method call on shared variable 'xs' outside 'lock xs { }'" in m for m in messages),
            messages,
        )
        error = next(d for d in diagnostics if "Method call on shared variable" in d["message"])
        self.assertEqual(error["severity"], analysis.SEVERITY_ERROR)
        start = analysis.position_to_offset(
            text, error["range"]["start"]["line"], error["range"]["start"]["character"]
        )
        self.assertEqual(start, text.index("xs.push"))

    def test_lock_on_a_plain_variable_is_an_error(self):
        diagnostics = analysis.get_diagnostics("let a = 1\nlock a { a = 2 }\n")
        self.assertTrue(
            any("'lock' takes shared variables, and 'a' is not one" in d["message"] for d in diagnostics),
            diagnostics,
        )

    def test_a_clean_threads_program_has_no_errors(self):
        text = "shared let n = 0\nfn bump() { lock n { n = n + 1 } }\nbump()\nprint(n)\n"
        diagnostics = analysis.get_diagnostics(text)
        self.assertEqual(
            [d for d in diagnostics if d["severity"] == analysis.SEVERITY_ERROR], [], diagnostics
        )

    def test_detach_paren_then_newline_warns_with_a_thread(self):
        text = (
            'import thread from "std:thread"\n'
            "fn work() { 1 }\n"
            "let t = thread.spawn()\n"
            "let p = detach(t)\n"
            "work()\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "main.mh")
            with open(path, "w") as f:
                f.write(text)
            diagnostics = analysis.get_diagnostics(text, path)
        messages = [d["message"] for d in diagnostics]
        self.assertTrue(
            any("is not the thread form, so nothing runs on the thread" in m for m in messages),
            messages,
        )


if __name__ == "__main__":
    unittest.main()
