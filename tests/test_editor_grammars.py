"""M45 (docs/contracts/M45_atomic.md §11.1, §11.2, §11.5): the editor and
website highlighters know `atomic`/`retry` and no longer know `lock`.

None of these grammars is run by the rest of the suite (tree-sitter and
VS Code aren't test dependencies), so this checks the committed files
themselves: the VS Code patterns behave as documented (their regexes are
plain enough for Python's `re`), the tree-sitter grammar, its generated
`grammar.json` and `highlights.scm` have `atomic` and no `lock`, and the
www highlighter's keyword list matches.
"""

import json
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts: str) -> str:
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as handle:
        return handle.read()


def _control_keyword_patterns() -> list:
    grammar = json.loads(_read("editors", "vscode", "syntaxes", "mah.tmLanguage.json"))
    return [p["match"] for p in grammar["repository"]["control-keywords"]["patterns"]]


def _keywords_in(line: str) -> list:
    """The words of `line` the control-keyword patterns color."""
    found = []
    for pattern in _control_keyword_patterns():
        found.extend(m.group(0) for m in re.finditer(pattern, line))
    return found


class VsCodeGrammarTests(unittest.TestCase):
    def test_atomic_is_a_keyword_only_before_a_brace(self):
        self.assertIn("atomic", _keywords_in("atomic { n = n + 1 }"))
        self.assertIn("atomic", _keywords_in("let v = atomic {"))
        self.assertNotIn("atomic", _keywords_in("let atomic = 2"))
        self.assertNotIn("atomic", _keywords_in("print(atomic + 1)"))

    def test_retry_is_a_keyword_only_at_the_end_of_a_statement(self):
        self.assertIn("retry", _keywords_in("    if q.len() == 0 { retry }"))
        self.assertIn("retry", _keywords_in("    retry"))
        self.assertIn("retry", _keywords_in("    retry; 1"))
        self.assertNotIn("retry", _keywords_in("let retry = 3"))
        self.assertNotIn("retry", _keywords_in("print(retry + 1)"))

    def test_lock_is_no_keyword(self):
        self.assertEqual(_keywords_in("lock n { n = n + 1 }"), [])
        for pattern in _control_keyword_patterns():
            self.assertNotIn("lock", pattern)


class TreeSitterGrammarTests(unittest.TestCase):
    def test_grammar_has_atomic_expr_and_no_lock_expr(self):
        source = _read("syntax-highlight", "grammar.js")
        self.assertIn("atomic_expr: ($) => seq(\"atomic\", field(\"body\", $.block))", source)
        self.assertNotIn("lock_expr", source)
        generated = json.loads(_read("syntax-highlight", "src", "grammar.json"))
        self.assertIn("atomic_expr", generated["rules"])
        self.assertNotIn("lock_expr", generated["rules"])
        self.assertNotIn("_lock_target", generated["rules"])

    def test_highlights_color_atomic_and_statement_retry_last(self):
        source = _read("syntax-highlight", "queries", "mah", "highlights.scm")
        self.assertIn('"atomic" @keyword', source)
        self.assertNotIn('"lock"', source)
        retry = '((expr_stmt (expr (identifier) @keyword)) (#eq? @keyword "retry"))'
        self.assertIn(retry, source)
        # last-pattern-wins: the retry capture must come after the generic
        # identifier captures
        self.assertGreater(source.index(retry), source.rindex("(identifier) @variable"))


class WebsiteHighlighterTests(unittest.TestCase):
    def test_keyword_list_has_atomic_and_retry_and_no_lock(self):
        source = _read("www", "src", "lib", "markdown", "highlight-mah.ts")
        keywords = source[source.index("const KEYWORDS") : source.index("]);")]
        words = set(re.findall(r"'([a-z_]+)'", keywords))
        self.assertTrue({"shared", "atomic", "retry"} <= words, words)
        self.assertNotIn("lock", words)


if __name__ == "__main__":
    unittest.main()
