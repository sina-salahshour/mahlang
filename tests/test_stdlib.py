"""M27 (docs/STDLIB.md, Phase 0): the standard library's foundations --
`std:` imports, `extern fn`, native table versioning (see also
tests/test_bytecode.py's newer-minor tests) -- and its first module,
`std:math`. `sin`/`cos` stopped being lexer keywords in the same change.

Every program here runs on whichever VM `MAH_TEST_VM` selects (see
tests/support.py), so `make test-rust` checks the Rust natives too.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mah.compiler.lexer import Lexer  # noqa: E402
from mah.compiler.parser import Parser  # noqa: E402
from mah.compiler.resolve import Resolver  # noqa: E402
from mah.format.formatter import format_source  # noqa: E402
from mah.lsp import analysis  # noqa: E402
from mah.preprocessor import STD_DIR, preprocess  # noqa: E402
from mah.runtime_values import MahRuntimeError  # noqa: E402
from tests.support import compile_bytes, run_file, run_source, run_source_and_error  # noqa: E402
from tests.test_typecheck import check  # noqa: E402

MATH = 'import math from "std:math"\n'


def _compile_error(src: str) -> str:
    with self_raises() as box:
        compile_bytes(text=src)
    return box[0]


class self_raises:
    """`with self_raises() as box:` -- captures the message of whatever the
    block raises (the compile-error form tests need, without a TestCase)."""

    def __enter__(self):
        self.box = []
        return self.box

    def __exit__(self, exc_type, exc, _tb):
        if exc is None:
            raise AssertionError("expected a compile error")
        self.box.append(str(exc))
        return True


class StdImportTests(unittest.TestCase):
    def test_namespace_import(self):
        self.assertEqual(run_source(MATH + "print(math.sqrt(16), math.pi)"), "4 3.141592653589793238462643383\n")

    def test_flat_import(self):
        self.assertEqual(run_source('import "std:math"\nprint(max(2, 7), round(e, 3))'), "7 2.718\n")

    def test_std_modules_are_found_from_any_directory(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "main.mh")
            with open(path, "w", encoding="utf-8") as f:
                f.write(MATH + "print(math.abs(0 - 2))\n")
            self.assertEqual(run_file(path), "2\n")

    def test_unknown_std_module(self):
        self.assertIn("unknown standard library module 'std:nope'", _compile_error('import "std:nope"'))

    def test_the_prelude_is_not_a_std_module(self):
        self.assertIn("unknown standard library module 'std:prelude'", _compile_error('import "std:prelude"'))

    def test_std_never_falls_back_to_a_user_file(self):
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "std:math.mh"), "w", encoding="utf-8") as f:
                f.write("export let pi = 3\n")
            path = os.path.join(td, "main.mh")
            with open(path, "w", encoding="utf-8") as f:
                f.write(MATH + "print(math.pi)\n")
            self.assertEqual(run_file(path), "3.141592653589793238462643383\n")

    def test_runtime_errors_in_std_code_are_located_at_the_call(self):
        # M30: like the prelude's (M29), an uncaught error from inside a std
        # module is located at the program's own call into it.
        out, exc = run_source_and_error(MATH + "print(math.sqrt(0 - 1))")
        self.assertEqual(out, "")
        self.assertIsInstance(exc, MahRuntimeError)
        self.assertEqual(str(exc), "sqrt: argument out of range at position #2:12")


class ExternFnTests(unittest.TestCase):
    def test_extern_fn_is_rejected_outside_the_standard_library(self):
        self.assertIn(
            "'extern fn' is only allowed in standard library modules",
            _compile_error('extern fn f(x: Number) -> Number = "math.sin"'),
        )

    def test_extern_fn_is_rejected_in_an_imported_user_file(self):
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "lib.mh"), "w", encoding="utf-8") as f:
                f.write('export extern fn f(x: Number) -> Number = "math.sin"\n')
            pp = preprocess(os.path.join(td, "main.mh"), 'import "lib.mh"\n')
            self.assertEqual(len(pp.errors), 1)
            self.assertIn("only allowed in standard library modules (used in 'lib.mh')", pp.errors[0][0])

    def test_extern_is_still_an_ordinary_name(self):
        self.assertEqual(run_source("let extern = 2\nprint(extern + 1)"), "3\n")

    def _resolve(self, src: str):
        # Parse + resolve directly, bypassing the preprocessor's std-only
        # gate, to reach the resolver's own checks.
        parser = Parser(Lexer(src))
        program = parser.parse_program()
        if parser.errors:
            raise SyntaxError(parser.errors[0][0])
        Resolver().resolve_program(program)
        return program

    def test_the_native_must_exist(self):
        with self.assertRaises(NameError) as cm:
            self._resolve('extern fn f(x) = "math.nope"')
        self.assertIn("Unknown native 'math.nope'", str(cm.exception))

    def test_the_arity_must_match(self):
        with self.assertRaises(SyntaxError) as cm:
            self._resolve('extern fn f(x, y) = "math.sin"')
        self.assertIn("Native 'math.sin' takes 1 argument(s), but the extern fn declares 2", str(cm.exception))

    def test_parameters_cannot_have_defaults(self):
        with self.assertRaises(SyntaxError) as cm:
            self._resolve('extern fn f(x = 1) = "math.sin"')
        self.assertIn("can't have defaults", str(cm.exception))

    def test_an_extern_fn_is_a_first_class_function(self):
        program = self._resolve('extern fn f(y: Number, x: Number) -> Number = "math.atan2"\nlet g = f')
        fn = program[0].value
        self.assertEqual(fn.native, "math.atan2")
        self.assertEqual(fn.params, ["y", "x"])
        self.assertEqual(run_source(MATH + "let f = math.atan2\nprint(f(0, 1))"), "0\n")

    def test_formatter_keeps_extern_fn(self):
        src = 'export extern fn tan(x: Number) -> Number = "math.tan"\n'
        self.assertEqual(format_source(src), src)

    def test_every_std_module_formats_unchanged(self):
        for name in sorted(os.listdir(STD_DIR)):
            if name.endswith(".mh") and name != "prelude.mh":
                with self.subTest(module=name):
                    with open(os.path.join(STD_DIR, name), encoding="utf-8") as f:
                        text = f.read()
                    self.assertEqual(format_source(text), text)


class StdMathTests(unittest.TestCase):
    def run_math(self, expr: str) -> str:
        return run_source(MATH + f"print({expr})").strip()

    def test_constants(self):
        self.assertEqual(self.run_math("math.pi, math.e"), "3.141592653589793238462643383 2.718281828459045235360287471")

    def test_exact_functions(self):
        self.assertEqual(self.run_math("math.sqrt(2)"), "1.414213562373095048801688724")
        self.assertEqual(self.run_math("math.sqrt(0), math.sqrt(9), math.pow(2, 10), math.pow(4, 0.5)"), "0 3 1024 2")
        self.assertEqual(self.run_math("math.abs(0 - 3), math.abs(3), math.min(3, 1), math.max(3, 1)"), "3 3 1 3")
        self.assertEqual(self.run_math("math.clamp(15, 0, 10), math.clamp(0 - 5, 0, 10), math.clamp(5, 0, 10)"), "10 0 5")

    def test_rounding(self):
        self.assertEqual(
            self.run_math("math.floor(2.7), math.floor(0 - 2.1), math.floor(3), math.floor(0 - 3)"), "2 -3 3 -3"
        )
        self.assertEqual(self.run_math("math.ceil(2.1), math.ceil(0 - 2.7), math.ceil(3)"), "3 -2 3")
        self.assertEqual(self.run_math("math.round(2.5), math.round(0 - 2.5), math.round(2.4)"), "3 -3 2")
        self.assertEqual(self.run_math("math.round(3.14159, 2), math.round(1250, 0 - 2)"), "3.14 1300")

    def test_float_natives(self):
        self.assertEqual(self.run_math("math.sin(0), math.cos(0), math.atan(0)"), "0 1 0")
        self.assertEqual(self.run_math("math.tan(1)"), "1.5574077246549023")
        self.assertEqual(self.run_math("math.asin(1), math.acos(1)"), "1.5707963267948966 0")
        self.assertEqual(self.run_math("math.atan2(1, 1), math.atan2(0 - 1, 0 - 1)"), "0.7853981633974483 -2.356194490192345")
        self.assertEqual(self.run_math("math.exp(1), math.log(math.e), math.log10(1000)"), "2.718281828459045 1 3")

    def test_domain_errors_are_catchable_runtime_errors(self):
        src = MATH + (
            "print(try { math.log(0) } catch { RuntimeError.ArgumentError { message } => { message } })\n"
            "print(try { math.asin(2) } catch { e: RuntimeError => { e.message } })\n"
            "print(try { math.exp(100000) } catch { e: RuntimeError => { e.message } })\n"
            "print(try { math.sqrt(0 - 1) } catch { e: RuntimeError => { e.message } })\n"
        )
        self.assertEqual(
            run_source(src),
            "log: argument out of range\nasin: argument out of range\n"
            "exp: argument out of range\nsqrt: argument out of range\n",
        )

    def test_a_non_number_reaching_a_native(self):
        src = MATH + 'let s: Unknown = "x"\nprint(try { math.tan(s) } catch { RuntimeError.TypeMismatch { message } => { message } })'
        self.assertEqual(run_source(src), "tan: expected a Number, got String\n")

    def test_checker_types_std_functions(self):
        diagnostics, types = check(MATH + "let r = math.sqrt(2)\nlet f = math.floor")
        self.assertEqual(diagnostics, [])
        self.assertEqual(types["r"][-1], "Number")  # (math.mh has its own `r`s)
        self.assertEqual(types["f"][-1], "fn(Number) -> Number")
        diagnostics, _ = check(MATH + 'let r = math.sqrt("x")')
        # (`check` numbers lines in the combined text, std module included.)
        self.assertEqual(
            [(k, m) for k, m, _line in diagnostics],
            [("mismatch", "Type mismatch in an argument: expected Number, found String")],
        )

    def test_std_math_is_clean_at_explicit(self):
        # The standard library stays fully typed (docs/TYPES.md's
        # "Strictness"): nothing in it may be an implicit Unknown.
        diagnostics, _ = check(MATH)
        self.assertEqual(diagnostics, [])


class StdModuleTestFilesTests(unittest.TestCase):
    """M28: each std module can ship its own Mah tests, `mah/std/<name>.test.mh`
    -- all of them must pass, on the VM `MAH_TEST_VM` selects."""

    def test_every_std_test_file_passes(self):
        import contextlib
        import io

        from mah.cli.main import main

        vm = "rust" if os.environ.get("MAH_TEST_VM") == "rust" else "python"
        files = sorted(f for f in os.listdir(STD_DIR) if f.endswith(".test.mh"))
        self.assertIn("math.test.mh", files)
        for name in files:
            with self.subTest(file=name):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = main(["test", "--file", os.path.join(STD_DIR, name), "--vm", vm])
                self.assertEqual(code, 0, out.getvalue())


class BuiltinSinCosTests(unittest.TestCase):
    """`sin`/`cos` aren't keywords any more: an unbound call is still the
    built-in (same bytecode as before), and any binding of the name wins."""

    def test_the_builtin_still_works(self):
        self.assertEqual(run_source("print(sin(0), cos(0))"), "0 1\n")

    def test_a_user_function_named_sin_wins(self):
        self.assertEqual(run_source('fn sin(x) { "mine " + x }\nprint(sin(1))'), "mine 1\n")

    def test_a_local_binding_wins(self):
        self.assertEqual(run_source("fn f(cos) { cos * 2 }\nprint(f(4))"), "8\n")

    def test_the_builtin_takes_exactly_one_argument(self):
        self.assertIn("'sin' can only have one argument", _compile_error("sin(1, 2)"))
        self.assertIn("'cos' can only have one argument", _compile_error("cos()"))

    def test_the_builtin_is_still_typed(self):
        diagnostics, types = check('let s = sin(1)\nlet t = sin("x")')
        self.assertEqual(types["s"], ["Number"])
        self.assertIn(("mismatch", "Type mismatch: expected Number, found String", 2), diagnostics)

    def test_std_math_sin_is_a_method_like_member(self):
        self.assertEqual(run_source(MATH + "print(math.sin(0) + math.cos(0))"), "1\n")


class StdLspTests(unittest.TestCase):
    def test_hover_on_a_std_function(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "main.mh")
            src = MATH + "print(math.sqrt(2))\n"
            hover = analysis.get_hover(src, 1, src.splitlines()[1].index("sqrt"), path)
            self.assertIsNotNone(hover)
            value = hover["contents"]["value"]
            self.assertIn("fn sqrt(x: Number) -> Number", value)
            self.assertIn("The square root of `x`", value)
            self.assertIn("*declared in `std:math`*", value)

    def test_hover_on_the_builtin_sin(self):
        hover = analysis.get_hover("print(sin(1))\n", 0, 7)
        self.assertIsNotNone(hover)
        self.assertIn("**builtin** `sin`", hover["contents"]["value"])

    def test_hover_on_extern(self):
        src = 'export extern fn tan(x: Number) -> Number = "math.tan"\n'
        hover = analysis.get_hover(src, 0, 8)
        self.assertIsNotNone(hover)
        self.assertIn("**keyword** `extern`", hover["contents"]["value"])

    def test_extern_outside_std_is_an_editor_diagnostic(self):
        diagnostics = analysis.get_diagnostics('extern fn f(x) = "math.sin"\n')
        self.assertEqual(len(diagnostics), 1)
        self.assertIn("only allowed in standard library modules", diagnostics[0]["message"])

    def test_go_to_definition_lands_in_the_std_file(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "main.mh")
            src = MATH + "print(math.sqrt(2))\n"
            location = analysis.get_definition(src, 1, src.splitlines()[1].index("sqrt"), path)
            self.assertIsNotNone(location)
            self.assertEqual(location["path"], os.path.join(STD_DIR, "math.mh"))
            self.assertEqual(location["range"]["start"]["line"], 17)  # `export fn sqrt`


class DataModuleTests(unittest.TestCase):
    """M30: std:path, std:json and std:csv (their behavior is covered by
    mah/std/*.test.mh, run on both VMs above), the 1.7 natives behind them,
    and what the checker and the editor see of them."""

    def test_bytecode_minor(self):
        from mah.bytecode.decode import decode

        self.assertEqual(decode(compile_bytes(text='import json from "std:json"\nprint(json.parse("1"))')).minor, 7)
        self.assertEqual(decode(compile_bytes(text='import csv from "std:csv"\nprint(csv.parse("a"))')).minor, 7)
        # std:path is plain Mah over the 1.6 String methods
        self.assertEqual(decode(compile_bytes(text='import path from "std:path"\nprint(path.dirname("a/b"))')).minor, 6)

    def test_natives(self):
        from decimal import Decimal

        from mah.natives import NATIVES
        from mah.runtime_values import NONE_VALUE, EnumInstance

        def call(name, *args):
            return NATIVES[name][1](None, list(args))

        self.assertEqual(call("value.type_name", NONE_VALUE), "None")
        self.assertEqual(call("value.type_name", Decimal(1)), "Number")
        some = EnumInstance("Option", "some", {"value": Decimal(1)})
        self.assertEqual(call("value.variant", some), "some")
        self.assertIs(call("value.variant", NONE_VALUE), NONE_VALUE)
        self.assertIs(call("value.fields", "x"), NONE_VALUE)
        self.assertEqual(call("string.chars", "a😀").items, ["a", "😀"])
        self.assertEqual(call("string.code_point", "😀"), Decimal(0x1F600))
        self.assertEqual(call("string.from_code_point", Decimal(233)), "é")
        for name, arg, message in [
            ("string.code_point", "ab", "code_point: expected one character, got 2"),
            ("string.from_code_point", Decimal(0xD800), "from_code_point: not a Unicode scalar value"),
            ("string.from_code_point", Decimal("1.5"), "from_code_point: not a Unicode scalar value"),
            ("string.chars", Decimal(1), "chars: expected a String, got Number"),
        ]:
            with self.subTest(name=name, arg=arg):
                with self.assertRaises(MahRuntimeError) as cm:
                    call(name, arg)
                self.assertEqual(str(cm.exception), message)

    def test_an_uncaught_json_error_is_located_at_the_call(self):
        _out, exc = run_source_and_error('import json from "std:json"\nlet x = 1\njson.parse("[1,")')
        self.assertEqual(
            str(exc), "Uncaught JsonError: expected a value, found end of input at line 1, column 4 at position #3:6"
        )

    def test_checker_types(self):
        diagnostics, types = check(
            'import json from "std:json"\nimport csv from "std:csv"\nimport path from "std:path"\n'
            'let v = try json.parse("1") else none\nlet rows = try csv.parse("a") else []\n'
            'let recs = try csv.parse_records("a") else []\nlet d = path.dirname("a/b")'
        )
        self.assertEqual([d for d in diagnostics if d[0] not in ("implicit",)], [])
        got = {k: types[k][-1] for k in ("v", "rows", "recs", "d")}
        self.assertEqual(
            got,
            {"v": "Unknown", "rows": "Vector<Vector<String>>", "recs": "Vector<Map<String, String>>", "d": "String"},
        )
        diagnostics, _ = check('import json from "std:json"\nlet v = json.parse("1")')
        self.assertIn("Unhandled error: JsonError", [d[1] for d in diagnostics if d[0] == "unhandled"])

    def test_completion_lists_the_exports(self):
        src = 'import json from "std:json"\njson.\n'
        labels = {i["label"] for i in analysis.get_completions(src, None, 1, 5)}
        self.assertTrue({"parse", "stringify", "field", "as_number"} <= labels)
        self.assertFalse({"JsonReader", "quote", "type_name"} & labels)


if __name__ == "__main__":
    unittest.main()
