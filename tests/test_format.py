"""M21b: `mah format` -- see docs/FORMAT.md.

Run with: python -m unittest tests.test_format -v
"""

import glob
import io
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mah.format import FormatError, FormatOptions, format_source
from mah.format import formatter
from mah.format.cli import run_format
from mah.lsp import analysis

_ROOT = os.path.join(os.path.dirname(__file__), "..")


def _corpus():
    """Every example, the prelude, and every code block of the language
    reference users get from `mah init`."""
    sources = []
    for path in sorted(glob.glob(os.path.join(_ROOT, "examples", "*.mh"))) + [
        os.path.join(_ROOT, "mah", "std", "prelude.mh")
    ]:
        with open(path, encoding="utf-8") as f:
            sources.append((os.path.basename(path), f.read()))
    with open(os.path.join(_ROOT, "mah", "project", "templates", "docs", "mah-language.md"), encoding="utf-8") as f:
        for k, block in enumerate(re.findall(r"```mah\n(.*?)```", f.read(), re.DOTALL)):
            sources.append((f"mah-language.md block {k}", block))
    return sources


class GoldenTests(unittest.TestCase):
    def check(self, source: str, expected: str):
        self.assertEqual(format_source(source), expected)
        self.assertEqual(format_source(expected), expected)  # idempotent

    def test_spacing(self):
        self.check(
            "let x=-a+b*2-(c)\nprint(!x,a..b,a..=b,..3,x.y.z,v[0],f(1),-2**2,a - -b,x.await)\n"
            "let r = 1..\nlet s=\"ab\"[0]\n",
            "let x = -a + b * 2 - (c)\n"
            "print(!x, a..b, a..=b, ..3, x.y.z, v[0], f(1), -2 ** 2, a - -b, x.await)\n"
            "let r = 1..\nlet s = \"ab\"[0]\n",
        )

    def test_tokens_that_would_merge_keep_a_space(self):
        # `100..` followed by `=>`: printed tight, `..=>` would lex as `..=`, `>`.
        self.check(
            "match x { 100.. => { 1 } _ => { 2 } }\n",
            "match x {\n    100.. => { 1 }\n    _ => { 2 }\n}\n",
        )

    def test_blank_lines(self):
        self.check(
            "\n\nlet a = 1\n\n\n\nlet b = 2\nfn f() {\n\n    a\n\n}\n\n",
            "let a = 1\n\nlet b = 2\nfn f() {\n    a\n}\n",
        )

    def test_comments(self):
        self.check(
            "# head\nlet a = [ # open\n    1, # one\n    # before two\n    2\n    # end\n]\n"
            "let b = 2 # bee\nlet cc = 3 # cee\n\nfn g() {\n    # inside\n    b\n    # before close\n}\n",
            "# head\nlet a = [ # open\n    1,    # one\n    # before two\n    2\n    # end\n]\n"
            "let b = 2  # bee\nlet cc = 3 # cee\n\nfn g() {\n    # inside\n    b\n    # before close\n}\n",
        )

    def test_comment_after_a_closing_bracket_does_not_break_the_group(self):
        self.check("let x=add( 2 )   # trailing\n", "let x = add(2) # trailing\n")

    def test_lists(self):
        self.check(
            "let short = [1,2,3]\nlet kept = [\n  1, 2]\nlet p = P {x: 1, y: 2}\n"
            "let m = [\"a\": 1, \"b\": 2]\nlet e = [:]\n",
            "let short = [1, 2, 3]\nlet kept = [\n    1,\n    2\n]\nlet p = P { x: 1, y: 2 }\n"
            "let m = [\"a\": 1, \"b\": 2]\nlet e = [:]\n",
        )

    def test_long_call_breaks_one_argument_per_line(self):
        args = ", ".join(f"argument_{n}" for n in range(8))
        expected_args = "".join(f"    argument_{n},\n" for n in range(7)) + "    argument_7\n"
        self.check(f"call_something({args}, more)\n".replace(", more", ""), f"call_something(\n{expected_args})\n")

    def test_line_width_option(self):
        self.assertEqual(
            format_source("f(aaaa, bbbb)\n", FormatOptions(line_width=10)),
            "f(\n    aaaa,\n    bbbb\n)\n",
        )
        self.assertEqual(
            format_source("fn f() {\n1\n}\n", FormatOptions(indent=2)), "fn f() {\n  1\n}\n"
        )

    def test_blocks(self):
        self.check(
            "fn one() { 1 }\nfn two() { let a = 1; a }\nlet f = fn() {}\nwhile x { }\n"
            "match x { 1 => { \"a\" } _ => { \"b\" } }\n",
            "fn one() { 1 }\nfn two() {\n    let a = 1;\n    a\n}\nlet f = fn() { }\nwhile x { }\n"
            "match x {\n    1 => { \"a\" }\n    _ => { \"b\" }\n}\n",
        )

    def test_if_chain_breaks_together(self):
        self.check("if a { b } elif c { d } else { e }\n", "if a { b } elif c { d } else { e }\n")
        long_value = "\"" + "x" * 80 + "\""
        self.check(
            f"if a {{ {long_value} }} else {{ 1 }}\n",
            f"if a {{\n    {long_value}\n}} else {{\n    1\n}}\n",
        )

    def test_trailing_closure_is_hugged(self):
        self.check(
            "v.map(fn(x) { let y = x; y })\nprint(v.map(fn(c) { let d = c; d }).reduce())\n",
            "v.map(fn(x) {\n    let y = x;\n    y\n})\nprint(v.map(fn(c) {\n    let d = c;\n    d\n}).reduce())\n",
        )

    def test_type_annotations(self):
        self.check(
            "fn f<T:Printable,U=Vector<T>>(a:T,b:fn(Number)->String)->Map<String,Vector<T>>{a}\n"
            "impl<T> Tr<T> for Vector<T> { fn m(self)->T { self[0] } }\nstruct S<A> { a:A }\n",
            "fn f<T: Printable, U = Vector<T>>(a: T, b: fn(Number) -> String) -> Map<String, Vector<T>> { a }\n"
            "impl<T> Tr<T> for Vector<T> {\n    fn m(self) -> T { self[0] }\n}\nstruct S<A> { a: A }\n",
        )

    def test_modules(self):
        self.check(
            "import \"lib.mh\"\nimport m from \"lib\"\nexport fn f(){1}\nexport let a=2\nexport f\nprint(m.a)\n",
            "import \"lib.mh\"\nimport m from \"lib\"\nexport fn f() { 1 }\nexport let a = 2\nexport f\nprint(m.a)\n",
        )

    def test_patterns(self):
        self.check(
            "match n { -1 => { 0 } 1..5 => { 1 } ..=0 => { 2 } P { x, y: q } if q > 1 => { 3 } some(v) => { v } }\n",
            "match n {\n    -1 => { 0 }\n    1..5 => { 1 }\n    ..=0 => { 2 }\n"
            "    P { x, y: q } if q > 1 => { 3 }\n    some(v) => { v }\n}\n",
        )

    def test_statements_split_but_desugared_forms_stay_whole(self):
        self.check(
            "let a = 1; let b = 2\ndefer print(\"x\")\nlet p = detach { 1 }\nlet q = detach for let i in v { }\n",
            "let a = 1;\nlet b = 2\ndefer print(\"x\")\nlet p = detach { 1 }\nlet q = detach for let i in v { }\n",
        )

    def test_calls_on_any_expression(self):
        self.check(
            "print(fn(x) { x + 1 }(5), f(1)(2), hs[0](3), { 1 }[0])\n",
            "print(fn(x) { x + 1 }(5), f(1)(2), hs[0](3), { 1 }[0])\n",
        )

    def test_statement_starting_with_a_paren_stays_its_own_line(self):
        # Parentheses have no AST node, so the statement's first recorded
        # position is the `g` inside them.
        self.check("print(g())\n(g)()\nlet v = { ((g))() }\n", "print(g())\n(g)()\nlet v = { ((g))() }\n")

    def test_empty_and_comment_only_files(self):
        self.assertEqual(format_source(""), "")
        self.assertEqual(format_source("\n\n# just this\n\n"), "# just this\n")


class CorpusTests(unittest.TestCase):
    def test_every_source_formats_verifies_and_is_idempotent(self):
        for name, source in _corpus():
            with self.subTest(name):
                once = format_source(source)
                self.assertEqual(format_source(once), once)


class SafetyTests(unittest.TestCase):
    def test_syntax_error_is_reported(self):
        with self.assertRaisesRegex(FormatError, r"syntax error at 2:1"):
            format_source("let x = \n")

    def test_unterminated_string(self):
        with self.assertRaisesRegex(FormatError, "syntax error"):
            format_source('let x = "abc\n')

    def _with_broken_render(self, change):
        real = formatter._render

        def broken(text, options):
            formatted, *rest = real(text, options)
            return (change(formatted), *rest)

        return mock.patch.object(formatter, "_render", broken)

    def test_verification_catches_changed_tokens(self):
        with self._with_broken_render(lambda out: out.replace("1", "2")):
            with self.assertRaisesRegex(FormatError, "tokens changed"):
                format_source("let x = 1\n")

    def test_verification_catches_lost_comments(self):
        with self._with_broken_render(lambda out: out.replace("# c", "")):
            with self.assertRaisesRegex(FormatError, "comments changed"):
                format_source("let x = 1 # c\n")

    def test_verification_catches_a_newline_before_an_index(self):
        # Same tokens, but `v\n[0]` is two statements, not an index.
        with self._with_broken_render(lambda out: out.replace("v[0]", "v\n[0]")):
            with self.assertRaisesRegex(FormatError, "meaning changed"):
                format_source("let v = [1]\nlet a = v[0]\n")


class CliTests(unittest.TestCase):
    def run_cli(self, paths, check=False, cwd=None, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        status = run_format(paths, check, stdin=io.StringIO(stdin), stdout=out, stderr=err, cwd=cwd)
        return status, out.getvalue(), err.getvalue()

    def test_check_then_format_in_place(self):
        with tempfile.TemporaryDirectory() as directory:
            messy = os.path.join(directory, "a.mh")
            clean = os.path.join(directory, "b.mh")
            with open(messy, "w") as f:
                f.write("let x=1\n")
            with open(clean, "w") as f:
                f.write("let y = 2\n")
            status, out, _err = self.run_cli([], check=True, cwd=directory)
            self.assertEqual((status, out), (1, "a.mh\n"))
            with open(messy) as f:
                self.assertEqual(f.read(), "let x=1\n")  # --check writes nothing
            status, out, _err = self.run_cli([], cwd=directory)
            self.assertEqual((status, out), (0, "formatted a.mh\n"))
            with open(messy) as f:
                self.assertEqual(f.read(), "let x = 1\n")
            self.assertEqual(self.run_cli([], check=True, cwd=directory)[0], 0)

    def test_defaults_to_the_project_root_and_skips_build(self):
        with tempfile.TemporaryDirectory() as directory:
            with open(os.path.join(directory, "mah-project.toml"), "w") as f:
                f.write("")
            os.makedirs(os.path.join(directory, "src"))
            os.makedirs(os.path.join(directory, "build"))
            for rel in ("src/main.mh", "build/skip.mh"):
                with open(os.path.join(directory, rel), "w") as f:
                    f.write("let x=1\n")
            status, out, _err = self.run_cli([], check=True, cwd=os.path.join(directory, "src"))
            self.assertEqual((status, out), (1, "main.mh\n"))

    def test_syntax_error_leaves_the_file_and_continues(self):
        with tempfile.TemporaryDirectory() as directory:
            for name, text in (("bad.mh", "let x = \n"), ("good.mh", "let y=2\n")):
                with open(os.path.join(directory, name), "w") as f:
                    f.write(text)
            status, out, err = self.run_cli([], cwd=directory)
            self.assertEqual(status, 1)
            self.assertIn("can't format bad.mh: syntax error", err)
            self.assertEqual(out, "formatted good.mh\n")
            with open(os.path.join(directory, "bad.mh")) as f:
                self.assertEqual(f.read(), "let x = \n")

    def test_stdin(self):
        self.assertEqual(self.run_cli(["-"], stdin="let  y=[1,2]\n")[:2], (0, "let y = [1, 2]\n"))

    def test_missing_path(self):
        status, _out, err = self.run_cli(["nope.mh"], cwd=tempfile.gettempdir())
        self.assertEqual(status, 1)
        self.assertIn("no such file", err)


class LspTests(unittest.TestCase):
    def test_formatting_edit_replaces_the_document(self):
        [edit] = analysis.get_formatting_edits("let x=1\n")
        self.assertEqual(edit["newText"], "let x = 1\n")
        self.assertEqual(edit["range"]["start"], {"line": 0, "character": 0})
        self.assertEqual(edit["range"]["end"], {"line": 1, "character": 0})

    def test_no_edits_when_formatted_or_broken(self):
        self.assertEqual(analysis.get_formatting_edits("let x = 1\n"), [])
        self.assertEqual(analysis.get_formatting_edits("let x = \n"), [])


if __name__ == "__main__":
    unittest.main()
