"""M44/M45 (docs/contracts/M44_threads.md §11.3, M45_atomic.md §11.3): the
LSP on threads syntax.

`shared`, `atomic` and `retry` are contextual keywords (ordinary `ID`
tokens), so hover recognizes them positionally, exactly where the compiler
reads them as keywords; a `shared let` hovers as a shared variable; rename
works through `atomic` bodies; completion offers the three keywords;
resolver errors (E1, E6b, E9) and checker warnings (W1) show up as
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


SOURCE = "shared let n = 0\natomic { n = n + 1 }\nprint(n)\n"


class ThreadHoverTests(unittest.TestCase):
    def test_shared_is_a_keyword_before_let(self):
        value = _hover(SOURCE, SOURCE.index("shared"))
        self.assertIsNotNone(value)
        self.assertTrue(value.startswith("**keyword** `shared`"), value)
        self.assertIn("atomic { ... }", value)

    def test_atomic_is_a_keyword_before_a_block(self):
        value = _hover(SOURCE, SOURCE.index("atomic"))
        self.assertIsNotNone(value)
        self.assertTrue(value.startswith("**keyword** `atomic`"), value)
        self.assertIn("transaction", value)

    def test_retry_is_a_keyword_inside_atomic(self):
        text = "shared let n = 0\natomic {\n    if n == 0 { retry }\n}\n"
        value = _hover(text, text.index("retry"))
        self.assertIsNotNone(value)
        self.assertTrue(value.startswith("**keyword** `retry`"), value)

    def test_atomic_as_a_variable_is_a_variable(self):
        text = "let atomic = 2\nprint(atomic)\n"
        value = _hover(text, text.rindex("atomic"))
        self.assertIsNotNone(value)
        self.assertTrue(value.startswith("**variable** `atomic`"), value)

    def test_retry_as_a_variable_is_a_variable(self):
        text = "let retry = 2\nprint(retry)\n"
        value = _hover(text, text.rindex("retry"))
        self.assertIsNotNone(value)
        self.assertTrue(value.startswith("**variable** `retry`"), value)

    def test_a_keyword_retry_with_a_variable_in_scope_is_the_keyword_and_e6b(self):
        text = "let retry = 1\natomic {\n    let y = retry\n}\n"
        value = _hover(text, text.rindex("retry"))
        self.assertIsNotNone(value)
        self.assertTrue(value.startswith("**keyword** `retry`"), value)
        messages = [d["message"] for d in analysis.get_diagnostics(text)]
        self.assertTrue(
            any(
                "'retry' here is the keyword (it ends this 'atomic { }' run); rename the variable 'retry'"
                in m
                for m in messages
            ),
            messages,
        )

    def test_lock_is_an_ordinary_name_again(self):
        text = "let lock = 2\nprint(lock)\n"
        value = _hover(text, text.rindex("lock"))
        self.assertIsNotNone(value)
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


class ThreadSymbolTests(unittest.TestCase):
    def test_an_atomic_block_is_not_a_document_symbol(self):
        text = "shared let n = 0\nfn f() { atomic { n = n + 1 } }\n"
        names = [s["name"] for s in analysis.get_document_symbols(text)]
        self.assertEqual(sorted(names), ["f", "n"])


class ThreadNavigationTests(unittest.TestCase):
    def test_definition_from_inside_an_atomic_body(self):
        offset = SOURCE.index("atomic { n") + len("atomic { ")
        p = _pos(SOURCE, offset)
        result = analysis.get_definition(SOURCE, p["line"], p["character"], None)
        self.assertIsNotNone(result)
        start = analysis.position_to_offset(
            SOURCE, result["range"]["start"]["line"], result["range"]["start"]["character"]
        )
        self.assertEqual(start, SOURCE.index("n = 0"))

    def test_rename_edits_the_declaration_the_atomic_body_and_every_use(self):
        offset = SOURCE.index("print(n)") + len("print(")
        p = _pos(SOURCE, offset)
        result = analysis.get_rename_edits(SOURCE, p["line"], p["character"], "hits", None)
        self.assertIsNotNone(result)
        edits = next(iter(result["changes"].values()))
        self.assertEqual(
            _apply_edits(SOURCE, edits),
            "shared let hits = 0\natomic { hits = hits + 1 }\nprint(hits)\n",
        )


class ThreadCompletionTests(unittest.TestCase):
    def test_shared_atomic_and_retry_are_offered_as_keywords(self):
        items = analysis.get_completions("let x = 1\n")
        keywords = {item["label"] for item in items if item.get("detail") == "keyword"}
        self.assertIn("shared", keywords)
        self.assertIn("atomic", keywords)
        self.assertIn("retry", keywords)
        self.assertNotIn("lock", keywords)

    def test_a_shared_variable_completes_as_one(self):
        text = "shared let counter = 0\nprint(counter)\n"
        offset = text.index("print(") + len("print(")
        p = _pos(text, offset)
        items = analysis.get_completions(text, None, p["line"], p["character"])
        details = [item["detail"] for item in items if item["label"] == "counter"]
        self.assertIn("shared variable", details)


class ThreadDiagnosticsTests(unittest.TestCase):
    def test_a_method_call_outside_atomic_is_an_error(self):
        text = "shared let xs = []\nxs.push(1)\n"
        diagnostics = analysis.get_diagnostics(text)
        messages = [d["message"] for d in diagnostics]
        self.assertTrue(
            any(
                "Method call on shared variable 'xs' outside 'atomic { }': it would act on a copy; "
                "wrap it in 'atomic { ... }'" in m
                for m in messages
            ),
            messages,
        )
        error = next(d for d in diagnostics if "Method call on shared variable" in d["message"])
        self.assertEqual(error["severity"], analysis.SEVERITY_ERROR)
        start = analysis.position_to_offset(
            text, error["range"]["start"]["line"], error["range"]["start"]["character"]
        )
        self.assertEqual(start, text.index("xs.push"))

    def test_assigning_an_outer_variable_inside_atomic_is_an_error(self):
        diagnostics = analysis.get_diagnostics("let total = 0\natomic { total = total + 1 }\n")
        self.assertTrue(
            any(
                "'total' is declared outside 'atomic { }', and changing it there isn't undone"
                in d["message"]
                for d in diagnostics
            ),
            diagnostics,
        )

    def test_a_clean_threads_program_has_no_errors(self):
        text = "shared let n = 0\nfn bump() { atomic { n = n + 1 } }\nbump()\nprint(n)\n"
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
