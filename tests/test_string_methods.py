"""M29 (docs/STDLIB.md "String methods"): the native String methods,
`Vector.join`, `to_number`/`NumberParseError`, and the Option helpers --
on whichever VM `MAH_TEST_VM` selects (so `make test-rust` checks the Rust
port against the same expectations). Every rule is defined in
mah/string_methods.py.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mah.bytecode.decode import decode  # noqa: E402
from mah.lsp import analysis  # noqa: E402
from mah.runtime_values import MahRuntimeError  # noqa: E402
from tests.support import compile_bytes, run_source, run_source_and_error  # noqa: E402
from tests.test_typecheck import check  # noqa: E402


def out(expr: str) -> str:
    return run_source(f"print({expr})").rstrip("\n")


def error(expr: str) -> str:
    return out(f"try {{ {expr} }} catch {{ e => {{ e.message() }} }}")


class SplitTests(unittest.TestCase):
    def test_on_whitespace(self):
        self.assertEqual(out('"  one two\\t\\nthree  ".split()'), "[one, two, three]")
        self.assertEqual(out('"".split().len(), "   ".split().len()'), "0 0")
        # Unicode whitespace too (no-break space, em space).
        self.assertEqual(out('"a\\u00a0b\\u2003c".split()'), "[a, b, c]")

    def test_on_a_separator(self):
        self.assertEqual(out('"a,b,,c".split(",")'), "[a, b, , c]")
        self.assertEqual(out('"abc".split(",")'), "[abc]")
        self.assertEqual(out('"a--b".split("--")'), "[a, b]")

    def test_limit(self):
        self.assertEqual(out('"k=v=w".split("=", 1)'), "[k, v=w]")
        self.assertEqual(out('" a b c ".split(limit: 1)'), "[a, b c ]")
        self.assertEqual(out('"a b".split(limit: 0)'), "[a b]")

    def test_errors(self):
        self.assertEqual(error('"a".split("")'), "split: sep can't be empty")
        self.assertEqual(error('"a".split(limit: "2")'), "split: limit must be a Number, got String")
        self.assertEqual(error('"a".split(",", 0 - 1)'), "split: limit must be a whole number of at least 0, got -1")


class TrimPadTests(unittest.TestCase):
    def test_trim(self):
        self.assertEqual(out('"|" + "  x y  ".trim() + "|"'), "|x y|")
        self.assertEqual(out('"|" + "  x ".trim_start() + "|" + "  x ".trim_end() + "|"'), "|x |  x|")
        self.assertEqual(out('"|" + " \\u3000x\\u2028".trim() + "|"'), "|x|")

    def test_pad(self):
        self.assertEqual(out('"7".pad_start(3, "0"), "abc".pad_start(2), "日本".pad_start(4, "*")'), "007 abc **日本")
        self.assertEqual(out('"ab".pad_end(5, "xy") + "|", "x".pad_end(3) + "|"'), "abxyx| x  |")
        self.assertEqual(error('"x".pad_start(3, "")'), "pad_start: fill can't be empty")
        self.assertEqual(error('"x".pad_end(1.5)'), "pad_end: width must be a whole number of at least 0, got 1.5")


class SearchReplaceTests(unittest.TestCase):
    def test_replace(self):
        self.assertEqual(out('"aXbXc".replace("X", "-"), "aXbXc".replace_all("X", "-")'), "a-bXc a-b-c")
        self.assertEqual(out('"ab".replace("", "x"), "ab".replace_all("", "-")'), "xab -a-b-")
        self.assertEqual(error('"a".replace(1, "b")'), "replace: from must be a String, got Number")

    def test_predicates(self):
        self.assertEqual(
            out('"hello".starts_with("he"), "hello".ends_with("lo"), "hello".contains("ell"), "hello".contains("z")'),
            "true true true false",
        )
        self.assertEqual(error('"a".contains(5)'), "contains: part must be a String, got Number")

    def test_index_of_counts_code_points(self):
        self.assertEqual(out('"héllo wörld".index_of("w"), "hello".index_of("z"), "x".index_of("")'), "some(6) none some(0)")

    def test_repeat(self):
        self.assertEqual(out('"ab".repeat(3), "ab".repeat(0) + "|"'), "ababab |")
        self.assertEqual(error('"ab".repeat(0 - 1)'), "repeat: count must be a whole number of at least 0, got -1")

    def test_case(self):
        self.assertEqual(out('"Straße".to_upper(), "ÀB".to_lower(), "ΣΑΣ".to_lower()'), "STRASSE àb σας")


class LinesJoinTests(unittest.TestCase):
    def test_lines(self):
        self.assertEqual(out('"a\\r\\nb\\n".lines(), "".lines(), "a\\n\\nb".lines()'), "[a, b] [] [a, , b]")
        # a `\\r` not followed by `\\n` stays
        self.assertEqual(out('"x\\ry".lines().len(), "x\\ry".lines()[0].len()'), "1 3")

    def test_join(self):
        self.assertEqual(out('["x", 1, true, none, [2]].join(", ")'), "x, 1, true, none, [2]")
        self.assertEqual(out('[1, 2].join(), [].join("-") + "|"'), "12 |")
        self.assertEqual(error("[1].join(5)"), "join: sep must be a String, got Number")

    def test_join_uses_printable(self):
        src = 'struct P { }\nimpl Printable for P { fn to_string(self) { "P!" } }\nprint([P { }, P { }].join("+"))'
        self.assertEqual(run_source(src), "P!+P!\n")


class NumberTests(unittest.TestCase):
    def test_parse_number(self):
        self.assertEqual(
            out('" 42 ".parse_number(), "-1.5e2".parse_number(), ".5".parse_number(), "1.".parse_number()'),
            "42 -150 0.5 1",
        )
        self.assertEqual(out('"abc".parse_number(), "0x10".parse_number(), "1e123456".parse_number()'), "none none none")

    def test_to_number(self):
        self.assertEqual(out('"42".to_number() + 1'), "43")
        self.assertEqual(
            out('try { "abc".to_number() } catch { e: NumberParseError => { e.text + " / " + e.message() } }'),
            'abc / not a number: "abc"',
        )

    def test_an_uncaught_one_is_located_at_the_call(self):
        _out, exc = run_source_and_error('let x = 1\n"7x".to_number()')
        self.assertIsInstance(exc, MahRuntimeError)
        self.assertEqual(str(exc), 'Uncaught NumberParseError: not a number: "7x" at position #2:6')


class OptionTests(unittest.TestCase):
    def test_helpers(self):
        self.assertEqual(
            out('"x".index_of("x").unwrap(), "x".index_of("y").unwrap_or(0 - 1), some(1).is_some(), none.is_none()'),
            "0 -1 true true",
        )
        self.assertEqual(error('"x".index_of("y").unwrap()'), "unwrap: the value is none")


class CheckerTests(unittest.TestCase):
    def test_signatures(self):
        _d, types = check(
            'let a = " x ".trim()\nlet b = "a,b".split(",")\nlet c = [1].join()\nlet d = "x".index_of("x")\n'
            'let e = "x".lines()\nlet f = "5".parse_number()'
        )
        got = {k: types[k][-1] for k in "abcdef"}
        self.assertEqual(
            got,
            {"a": "String", "b": "Vector<String>", "c": "String", "d": "Option<Number>", "e": "Vector<String>", "f": "Number"},
        )
        diagnostics, _ = check('let r = "x".repeat("2")')
        self.assertIn(("mismatch", "Type mismatch in an argument: expected Number, found String", 1), diagnostics)

    def test_to_number_throws_number_parse_error(self):
        diagnostics, types = check('let n = "5".to_number()')
        self.assertEqual(types["n"][-1], "Number")
        self.assertIn(("unhandled", "Unhandled error: NumberParseError", 1), [d for d in diagnostics if d[0] == "unhandled"])
        diagnostics, _ = check('let n = try "5".to_number() else 0')
        self.assertEqual([d for d in diagnostics if d[0] != "implicit"], [])

    def test_option_helpers_are_typed(self):
        _d, types = check('let v = "x".index_of("x").unwrap()\nlet w = "x".index_of("x").is_some()')
        self.assertEqual((types["v"][-1], types["w"][-1]), ("Number", "Bool"))


class VersionAndEditorTests(unittest.TestCase):
    def test_bytecode_minor(self):
        self.assertEqual(decode(compile_bytes(text='print(" a ".trim())')).minor, 6)
        self.assertEqual(decode(compile_bytes(text="print(1)")).minor, 4)

    def test_static_path_call(self):
        self.assertEqual(run_source('print(String.trim(" a ") + "|")'), "a|\n")

    def test_completion_lists_the_methods(self):
        src = 'let s = "abc"\nprint(s.)\n'
        labels = {i["label"] for i in analysis.get_completions(src, None, 1, 8)}
        self.assertTrue({"split", "trim", "index_of", "pad_start", "to_upper"} <= labels)


if __name__ == "__main__":
    unittest.main()
