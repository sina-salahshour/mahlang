"""M41c (docs/REFLECTION.md, "M41c: hooks, function-item impls, rest
parameters"): rest parameters (`...name`, `**name`), `impl Tr for somefn` /
`impl somefn { ... }`, and the four hook traits (WrapFn, WrapParam,
WrapField, WrapStruct) in `std:reflect` -- plus bytecode 1.16 (PARAMS rest
flags, `paramhooks`, the `hooks.*` natives, `fn#<index>` impl keys) and the
tooling around them (checker, formatter, LSP).

Programs run through `tests/support.py`, so `make test-rust` reruns every
behavioral case on the Rust VM (`MAH_TEST_VM=rust`); runtime/tests/vm_diff.py
compares the two VMs' output and load errors directly.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import unittest
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mah.bytecode.decode import decode  # noqa: E402
from mah.bytecode.disasm import disassemble  # noqa: E402
from mah.bytecode.encode import encode  # noqa: E402
from mah.bytecode.format import (  # noqa: E402
    MINOR,
    NATIVE_ARITIES,
    NATIVE_SINCE_MINOR,
    OPCODE_SINCE_MINOR,
    OPCODES,
    MahcFormatError,
)
from mah.bytecode.program import Instr  # noqa: E402
from mah.format import format_source  # noqa: E402
from mah.lsp import analysis  # noqa: E402
from tests import support  # noqa: E402
from tests.support import compile_bytes, compile_program, run_file, run_source, run_source_and_error  # noqa: E402
from tests.test_typecheck import check  # noqa: E402

REFLECT = 'import reflect from "std:reflect"\n'


@contextmanager
def project(files: dict):
    with tempfile.TemporaryDirectory() as td:
        for name, text in files.items():
            with open(os.path.join(td, name), "w", encoding="utf-8") as handle:
                handle.write(text)
        yield td


def lines(text: str) -> list:
    return text.splitlines()


def entry_instrs(program) -> list:
    """The instructions that came from the entry file (not std:reflect or the
    prelude), by the DEBUG section's runs."""
    runs = program.debug.runs
    out = []
    for pc, instr in enumerate(program.code):
        file_index = None
        for start, f, _line, _col in runs:
            if start > pc:
                break
            file_index = f
        if file_index == 0:
            out.append(instr)
    return out


def natives_called(program, instrs) -> list:
    return [program.strings[program.natives[i.args[0]].name] for i in instrs if i.op == "native"]


# ---------------------------------------------------------------------------
# Rest parameters
# ---------------------------------------------------------------------------

SHOW = "fn f(a, b = 2, ...r, **k) { print(a); print(b); print(r); print(k) }\n"


class RestParameterTests(unittest.TestCase):
    def test_no_extras_gives_an_empty_vector_and_map(self):
        self.assertEqual(lines(run_source(SHOW + "f(1)")), ["1", "2", "[]", "[:]"])

    def test_extra_positional_and_keyword_arguments_are_collected(self):
        self.assertEqual(lines(run_source(SHOW + "f(1, 3, 4, 5, x: 6)")), ["1", "3", "[4, 5]", "[x: 6]"])

    def test_a_keyword_that_names_a_parameter_binds_it_and_the_rest_go_to_the_map(self):
        self.assertEqual(lines(run_source(SHOW + "f(1, b: 9, y: 1)")), ["1", "9", "[]", "[y: 1]"])

    def test_the_keyword_map_keeps_the_order_given(self):
        self.assertEqual(lines(run_source(SHOW + "f(1, z: 1, y: 2, x: 3)"))[3], "[z: 1, y: 2, x: 3]")

    def test_spread_calls_bind_rest_parameters_too(self):
        out = run_source(SHOW + 'f(...[1, 2, 3], **["z": 0])')
        self.assertEqual(lines(out), ["1", "2", "[3]", "[z: 0]"])

    def test_the_collections_are_new_values_each_call(self):
        out = run_source("fn f(...r) { r }\nlet a = f(1)\nlet b = f(1)\na.push(2)\nprint(a, b)")
        self.assertEqual(out.strip(), "[1, 2] [1]")

    def test_an_unknown_keyword_without_double_star_is_still_an_argument_error(self):
        _out, exc = run_source_and_error("fn f(a, ...r) { a }\nf(1, z: 2)")
        self.assertIn("'f' got an unexpected keyword argument 'z'", str(exc))

    def test_too_many_positional_arguments_without_a_rest_is_still_an_error(self):
        _out, exc = run_source_and_error("fn f(a, **k) { a }\nf(1, 2)")
        self.assertIn("'f' takes at most 1 positional arguments but 2 were given", str(exc))
        _out, exc = run_source_and_error("fn f(a) { a }\nf(1, 2)")
        self.assertIn("Argument Count is invalid. 'f' accepts 1 arguments but 2 was given", str(exc))

    def test_a_missing_ordinary_argument_is_still_reported(self):
        _out, exc = run_source_and_error("fn f(a, ...r) { a }\nf()")
        self.assertIn("'f' is missing required argument 'a'", str(exc))

    def test_a_keyword_named_like_a_rest_parameter_goes_to_the_map(self):
        out = run_source("fn f(...r, **k) { print(r); print(k) }\nf(r: 1)")
        self.assertEqual(lines(out), ["[]", "[r: 1]"])

    def test_a_keyword_given_by_position_and_name_is_still_an_error(self):
        _out, exc = run_source_and_error("fn f(a, **k) { a }\nf(1, a: 2)")
        self.assertIn("'f' got multiple values for argument 'a'", str(exc))

    def test_arity_counts_rest_parameters(self):
        self.assertEqual(run_source(SHOW + "print(f.arity())").splitlines()[-1], "4")

    def test_every_kind_of_function_takes_them(self):
        src = """
fn top(...a) { a.len() }
fn outer() { fn inner(x, ...rest) { rest.len() }
 inner(1, 2, 3) }
let closure = fn(...a, **k) { a.len() + k.len() }
struct S {}
impl S {
    fn m(self, ...rest) { rest.len() }
    fn s(...rest) { rest.len() }
}
print(top(1, 2), outer(), closure(1, k: 2), S {}.m(1, 2, 3), S.s(1))
"""
        self.assertEqual(run_source(src).strip(), "2 2 2 3 1")

    def test_an_extern_fn_cannot_have_them(self):
        # (the preprocessor only lets std modules write `extern fn`; the parser rejects it first here)
        _program, parser = support.parse_source('extern fn f(...a) -> Number = "io.print"')
        self.assertTrue(any("an extern fn can't have rest parameters" in message for message, _pos in parser.errors))

    def test_parse_errors(self):
        for src, message in (
            ("fn f(...r, a) { }", "a rest parameter ('...' or '**') must come after every ordinary parameter"),
            ("fn f(**k, a) { }", "a rest parameter ('...' or '**') must come after every ordinary parameter"),
            ("fn f(...a, ...b) { }", "a function can have only one '...' rest parameter"),
            ("fn f(**a, **b) { }", "a function can have only one '**' rest parameter"),
            ("fn f(**k, ...r) { }", "the '...' rest parameter must come before the '**' rest parameter"),
            ("fn f(...r = []) { }", "a rest parameter can't have a default value"),
            ("fn f(**k = [:]) { }", "a rest parameter can't have a default value"),
        ):
            with self.subTest(src):
                with self.assertRaises(SyntaxError) as cm:
                    compile_bytes(text=src)
                self.assertIn(message, str(cm.exception))

    def test_a_double_star_elsewhere_is_still_the_exponent(self):
        self.assertEqual(run_source("fn f(a, b) { a ** b }\nprint(f(2, 3))\nprint(2 ** 3 ** 2)").split(), ["8", "512"])

    def test_decorators_on_rest_parameters_see_the_collection(self):
        src = REFLECT + """
fn size(v, info) { v }
impl reflect.WrapParam for size { fn transform(self, v, info) { v.len() } }
fn f(@size ...r, @size **k) { [r, k] }
print(f(1, 2, 3, a: 1))
"""
        self.assertEqual(run_source(src).strip(), "[3, 1]")

    def test_signature_lists_them_apart_from_params(self):
        src = REFLECT + SHOW + """
let sig = reflect.signature(f)
print(sig.params.len())
match sig.rest { some(p) => { print(p.name) } none => { print("no rest") } }
match sig.kwrest { some(p) => { print(p.name) } none => { print("no kwrest") } }
fn plain(a) { a }
print(reflect.signature(plain).rest, reflect.signature(plain).kwrest)
"""
        self.assertEqual(lines(run_source(src)), ["2", "r", "k", "none none"])

    def test_signature_reports_the_annotated_collection_types(self):
        src = REFLECT + """
fn f(...r: Vector<Number>, **k: Map<String, String>) { }
let sig = reflect.signature(f)
match sig.rest {
    some(p) => { match p.type { reflect.TypeRef.Named { type: t, args: args } => { print(t, args.len()) } _ => { print("?") } } }
    none => { print("none") }
}
"""
        self.assertEqual(run_source(src).strip(), "Vector 1")

    def test_the_function_stays_callable_through_reflect_and_detach(self):
        src = REFLECT + """
fn f(a, ...r, **k) { print(a, r, k) }
reflect.call(f, [1, 2], ["x": 3])
let p = detach f(1, 2, y: 3)
p.await
"""
        self.assertEqual(lines(run_source(src)), ["1 [2] [x: 3]", "1 [2] [y: 3]"])


class RestParameterCheckerTests(unittest.TestCase):
    def diagnostics(self, src: str) -> list:
        return [m for _k, m, _l in check(src)[0]]

    def test_the_calls_are_accepted(self):
        src = SHOW + 'f(1)\nf(1, 3, 4, 5, x: 6)\nf(1, b: 9, y: 1)\nf(...[1, 2, 3], **["z": 0])\n'
        self.assertEqual(self.diagnostics(src), [])

    def test_extra_arguments_are_checked_against_the_annotation(self):
        src = 'fn f(a, ...r: Vector<Number>) { }\nf(1, 2, 3)\nf(1, "x")\n'
        self.assertEqual(
            self.diagnostics(src), ["Type mismatch in an argument: expected Number, found String"]
        )

    def test_keyword_rest_values_are_checked_against_the_annotation(self):
        src = 'fn f(**k: Map<String, Number>) { }\nf(a: 1)\nf(b: "x")\n'
        self.assertEqual(
            self.diagnostics(src), ["Type mismatch in an argument: expected Number, found String"]
        )

    def test_without_an_annotation_anything_goes(self):
        self.assertEqual(self.diagnostics('fn f(...r, **k) { }\nf(1, "a", true, x: 1, y: "b")\n'), [])

    def test_without_rest_parameters_the_old_errors_remain(self):
        src = "fn f(a) { a }\nf(1, 2)\nf(1, z: 2)\n"
        self.assertEqual(
            self.diagnostics(src), ["Too many arguments: expected at most 1, found 2", "No parameter named 'z'"]
        )

    def test_a_missing_ordinary_argument_is_reported(self):
        self.assertEqual(self.diagnostics("fn f(a, ...r) { }\nf()\n"), ["Missing argument 'a'"])

    def test_an_unannotated_rest_parameter_is_a_vector_or_map_of_unknown(self):
        _diagnostics, types = check("fn f(...r, **k) { r }\n")
        self.assertEqual(types["f"], ["fn(Vector<Unknown>, Map<String, Unknown>) -> Vector<Unknown>"])

    def test_the_body_sees_the_collection_types(self):
        src = "fn f(...r: Vector<Number>) { let n: Number = r[0]\n n }\n"
        self.assertEqual(self.diagnostics(src), [])
        src = 'fn f(...r: Vector<Number>) { let n: String = r[0]\n n }\n'
        self.assertEqual(len(self.diagnostics(src)), 1)


class RestParameterFormatterTests(unittest.TestCase):
    def test_formats_as_name_type(self):
        src = "fn f(a,b=2,...r : Vector<Number>,**k:Map<String,Number>){print(a)}\n"
        formatted = format_source(src)
        self.assertEqual(formatted, "fn f(a, b = 2, ...r: Vector<Number>, **k: Map<String, Number>) { print(a) }\n")
        self.assertEqual(format_source(formatted), formatted)

    def test_closures_and_methods(self):
        src = "let h = fn( ...x, **y ) { x }\nimpl S { fn m(self, ...rest) { rest } }\n"
        formatted = format_source(src)
        self.assertIn("let h = fn(...x, **y) { x }", formatted)
        self.assertIn("fn m(self, ...rest) { rest }", formatted)
        self.assertEqual(format_source(formatted), formatted)

    def test_a_decorated_rest_parameter(self):
        src = "fn tag(x) { x }\nfn f(@tag(1) ...r, a) {}\n".replace(", a)", ")")
        formatted = format_source(src)
        self.assertIn("fn f(@tag(1) ...r) { }", formatted)
        self.assertEqual(format_source(formatted), formatted)

    def test_a_spread_argument_still_prints_tight(self):
        src = "f( ...xs, **m )\n"
        self.assertEqual(format_source(src), "f(...xs, **m)\n")


# ---------------------------------------------------------------------------
# Function-item impls
# ---------------------------------------------------------------------------

LOG = REFLECT + """
fn log(f, info: reflect.FnInfo) { f }
impl reflect.WrapFn for log { fn wrap(self, f, info) { self(f, info) } }
impl log { fn describe(self) -> String { "logs" } }
fn other() { 1 }
"""


class FunctionImplTests(unittest.TestCase):
    def test_an_impl_for_a_function_adds_methods_to_it(self):
        self.assertEqual(run_source(LOG + "print(log.describe())").strip(), "logs")

    def test_another_function_has_no_such_method(self):
        _out, exc = run_source_and_error(LOG + "print(other.describe())")
        self.assertIn("'Function' has no method 'describe'", str(exc))

    def test_a_closure_has_no_such_method_either(self):
        _out, exc = run_source_and_error(LOG + "let c = fn() { 1 }\nc.describe()")
        self.assertIn("'Function' has no method 'describe'", str(exc))

    def test_a_trait_impl_for_a_function(self):
        src = """
trait Describe { fn describe(self) -> String }
fn greet() { 1 }
impl Describe for greet { fn describe(self) -> String { "greeter" } }
print(greet.describe(), Describe.describe(greet))
"""
        self.assertEqual(run_source(src).strip(), "greeter greeter")

    def test_the_function_value_dispatches_wherever_it_goes(self):
        src = LOG + "let g = log\nprint(g.describe())\nfn call_it(h) { h.describe() }\nprint(call_it(log))"
        self.assertEqual(lines(run_source(src)), ["logs", "logs"])

    def test_the_runtime_type_is_still_function(self):
        out = run_source(LOG + "print(reflect.type_of(log) == Function, reflect.type_of(log))")
        self.assertEqual(out.strip(), "true Function")

    def test_functions_that_are_not_impl_targets_still_have_the_function_methods(self):
        self.assertEqual(run_source(LOG + "print(log.arity(), other.arity())").strip(), "2 0")

    def test_two_functions_have_separate_impls(self):
        src = """
trait Name { fn name(self) -> String }
fn a() { 1 }
fn b() { 2 }
impl Name for a { fn name(self) -> String { "A" } }
impl Name for b { fn name(self) -> String { "B" } }
print(a.name(), b.name())
"""
        self.assertEqual(run_source(src).strip(), "A B")

    def test_the_same_trait_twice_for_one_function_is_refused(self):
        src = "trait Tr { fn m(self) -> String }\nfn a() { 1 }\nimpl Tr for a { fn m(self) -> String { \"x\" } }\nimpl Tr for a { fn m(self) -> String { \"y\" } }\n"
        with self.assertRaisesRegex(Exception, "already implements 'Tr'"):
            compile_bytes(text=src)

    def test_not_a_variable(self):
        src = "trait Tr { fn m(self) -> String }\nlet v = 1\nimpl Tr for v { fn m(self) -> String { \"x\" } }\n"
        with self.assertRaisesRegex(Exception, "impl targets must be a type or a top-level function"):
            compile_bytes(text=src)

    def test_not_a_function_stored_in_a_let(self):
        src = "trait Tr { fn m(self) -> String }\nlet v = fn() { 1 }\nimpl Tr for v { fn m(self) -> String { \"x\" } }\n"
        with self.assertRaisesRegex(Exception, "impl targets must be a type or a top-level function"):
            compile_bytes(text=src)

    def test_not_a_nested_function(self):
        src = (
            "trait Tr { fn m(self) -> String }\n"
            "fn outer() { fn inner() { 1 }\n inner }\n"
            "impl Tr for inner { fn m(self) -> String { \"x\" } }\n"
        )
        with self.assertRaisesRegex(Exception, "impl targets must be a type or a top-level function"):
            compile_bytes(text=src)

    def test_an_unknown_name_is_still_an_undefined_type(self):
        with self.assertRaisesRegex(Exception, "Undefined type 'nothing'"):
            compile_bytes(text="impl nothing { fn m(self) { 1 } }")

    def test_orphan_rule_across_modules(self):
        files = {
            "lib.mh": "export fn f() { 1 }\nexport trait Named { fn name(self) -> String }\n",
            "ok_trait.mh": (
                'import lib from "lib"\nfn local() { 1 }\n'
                'impl lib.Named for local { fn name(self) -> String { "local" } }\nprint(local.name())\n'
            ),
            "ok_own_trait.mh": (
                'import lib from "lib"\ntrait Mine { fn m(self) -> String }\n'
                'impl Mine for lib.f { fn m(self) -> String { "mine" } }\nprint(lib.f.m())\n'
            ),
            "foreign.mh": (
                'import lib from "lib"\nimpl lib.Named for lib.f { fn name(self) -> String { "x" } }\n'
            ),
            "foreign_inherent.mh": 'import lib from "lib"\nimpl lib.f { fn name(self) -> String { "x" } }\n',
            "reflect_foreign.mh": (
                'import lib from "lib"\nimport reflect from "std:reflect"\n'
                'impl reflect.WrapFn for lib.f { fn wrap(self, f, info) { f } }\n'
            ),
            "in_lib.mh": (
                "export fn g() { 1 }\nexport trait Named2 { fn name(self) -> String }\n"
                'impl Named2 for g { fn name(self) -> String { "g" } }\n'
            ),
            "uses_lib.mh": 'import lib from "in_lib"\nprint(lib.g.name())\n',
        }
        with project(files) as td:
            self.assertEqual(run_file(os.path.join(td, "ok_trait.mh")).strip(), "local")
            self.assertEqual(run_file(os.path.join(td, "ok_own_trait.mh")).strip(), "mine")
            self.assertEqual(run_file(os.path.join(td, "uses_lib.mh")).strip(), "g")
            for name in ("foreign.mh", "foreign_inherent.mh", "reflect_foreign.mh"):
                with self.subTest(name):
                    with self.assertRaisesRegex(
                        Exception, r"an impl for the function 'f' must be in its own module or the trait's"
                    ):
                        compile_bytes(path=os.path.join(td, name))

    def test_item_dispatch_survives_wrapping(self):
        src = LOG.replace("fn other() { 1 }", "") + """
trait Describe { fn describe2(self) -> String }
@log
fn greet(n) { "hi " + n }
impl Describe for greet { fn describe2(self) -> String { "greeter" } }
print(greet.describe2(), greet("x"))
"""
        self.assertEqual(run_source(src).strip(), "greeter hi x")

    def test_the_checker_types_item_methods(self):
        src = """
trait Describe { fn describe(self) -> String }
fn log(x: Number) -> Number { x }
impl Describe for log { fn describe(self) -> String { "logs" } }
let s: String = log.describe()
let alias = log
let t: String = alias.describe()
let bad: Number = log.describe()
let f: fn(Number) -> Number = log
"""
        diagnostics = [m for _k, m, _l in check(src)[0]]
        self.assertEqual(diagnostics, ["Type mismatch: expected Number, found String"])

    def test_formatter_roundtrip(self):
        src = "impl Tr for log { fn m(self) { 1 } }\nimpl log {\n    fn d(self) { 1 }\n}\n"
        self.assertEqual(format_source(format_source(src)), format_source(src))


class FunctionImplLspTests(unittest.TestCase):
    TEXT = (
        REFLECT
        + "fn log(f, info: reflect.FnInfo) { f }\n"
        + "impl reflect.WrapFn for log { fn wrap(self, f, info) { self(f, info) } }\n"
        + "impl log { fn describe(self) -> String { \"logs\" } }\n"
        + "print(log.describe())\n"
    )

    def at(self, needle: str, delta: int = 0) -> dict:
        return analysis.offset_to_position(self.TEXT, self.TEXT.index(needle) + delta)

    def test_go_to_definition_from_the_impl_target_lands_on_the_function(self):
        for needle in ("for log", "impl log"):
            with self.subTest(needle):
                at = self.at(needle, len(needle) - 3)
                definition = analysis.get_definition(self.TEXT, at["line"], at["character"], None)
                self.assertEqual(definition["range"]["start"], {"line": 1, "character": 3})

    def test_renaming_the_function_renames_the_impl_targets(self):
        at = self.at("fn log", 3)
        edits = analysis.get_rename_edits(self.TEXT, at["line"], at["character"], "logger", None)["changes"]
        (changes,) = edits.values()
        text = self.TEXT
        for edit in sorted(
            changes, key=lambda e: (e["range"]["start"]["line"], e["range"]["start"]["character"]), reverse=True
        ):
            row, col = edit["range"]["start"]["line"], edit["range"]["start"]["character"]
            end = edit["range"]["end"]["character"]
            parts = text.split("\n")
            parts[row] = parts[row][:col] + edit["newText"] + parts[row][end:]
            text = "\n".join(parts)
        self.assertIn("fn logger(f", text)
        self.assertIn("impl reflect.WrapFn for logger {", text)
        self.assertIn("impl logger {", text)
        self.assertIn("print(logger.describe())", text)
        compile_bytes(text=text)  # still a program

    def test_renaming_from_the_impl_target_renames_the_function_too(self):
        at = self.at("impl log", 6)
        edits = analysis.get_rename_edits(self.TEXT, at["line"], at["character"], "logger", None)["changes"]
        (changes,) = edits.values()
        self.assertEqual(len(changes), 4)

    def test_no_diagnostics(self):
        self.assertEqual(analysis.get_diagnostics(self.TEXT), [])

    def test_hover_shows_rest_parameters(self):
        text = "fn f(a, ...r: Vector<Number>, **k) { a }\nf(1)\n"
        hover = analysis.get_hover(text, 0, 3, None)
        self.assertIn("...r: Vector<Number>", hover["contents"]["value"])
        self.assertIn("**k: Map<String, Unknown>", hover["contents"]["value"])

    def test_the_hidden_import_adds_no_completions(self):
        text = "fn tag(x) { x }\n@tag(1)\nfn f() { }\n"
        labels = {i["label"] for i in analysis.get_completions(text, None, 2, 0)}
        for name in ("__setup_type", "__run_param", "__wrap_fn", "__run_struct", "__run_field", "__setup_param"):
            self.assertNotIn(name, labels)
        self.assertIn("tag", labels)


# ---------------------------------------------------------------------------
# WrapFn
# ---------------------------------------------------------------------------


class WrapFnTests(unittest.TestCase):
    LOGGING = REFLECT + """
fn log(f, info: reflect.FnInfo) {
    fn(...args, **kw) { print("-> " + info.name); f(...args, **kw) }
}
impl reflect.WrapFn for log { fn wrap(self, f, info) { self(f, info) } }
@log
fn greet(name: String, punct: String = "!") { "hi " + name + punct }
print(greet("a"))
print(greet("b", punct: "?"))
print(reflect.signature(greet).params.len())
print(reflect.signature(greet).decorators.len())
"""

    def test_a_logging_wrapper(self):
        self.assertEqual(lines(run_source(self.LOGGING)), ["-> greet", "hi a!", "-> greet", "hi b?", "2", "1"])

    def test_the_wrapper_is_called_from_everywhere(self):
        src = self.LOGGING.split("print(greet")[0] + """
print(reflect.call(greet, ["c"]))
print(greet(...["d"]))
let g = greet
print(g("e"))
"""
        out = lines(run_source(src))
        self.assertEqual(out, ["-> greet", "hi c!", "-> greet", "hi d!", "-> greet", "hi e!"])

    def test_stacked_decorators_wrap_closest_first(self):
        src = REFLECT + """
struct Wrap { name: String }
fn wrap(name) -> Wrap { Wrap { name: name } }
impl reflect.WrapFn for Wrap {
    fn wrap(self, f, info) {
        print("wrapping " + self.name)
        fn(...args, **kw) { print("<" + self.name + ">"); f(...args, **kw) }
    }
}
@wrap("a")
@wrap("b")
fn f(x) { x + 1 }
print(f(1))
"""
        # the closest decorator wraps first; the outermost runs first
        self.assertEqual(lines(run_source(src)), ["wrapping b", "wrapping a", "<a>", "<b>", "2"])

    def test_a_struct_decorator_with_arguments_retries(self):
        src = REFLECT + """
struct Retry { times: Number }
fn retry(n) -> Retry { Retry { times: n } }
impl reflect.WrapFn for Retry {
    fn wrap(self, f, info) {
        fn(...args, **kw) {
            let i = 0
            while true {
                let r = try { some(f(...args, **kw)) } catch { _ => { none } }
                match r {
                    some(v) => { return v }
                    none => {
                        i = i + 1
                        if i >= self.times { return "gave up" }
                    }
                }
            }
        }
    }
}
let calls = [0]
fn flaky() {
    calls[0] = calls[0] + 1
    if calls[0] < 3 { throw RuntimeError.ArgumentError { message: "no" } }
    "ok after " + calls[0].to_string()
}
@retry(5)
fn steady() { flaky() }
print(steady(), calls[0])
"""
        self.assertEqual(run_source(src).strip(), "ok after 3 3")

    def test_wrapping_an_impl_method(self):
        src = REFLECT + """
fn log(f, info: reflect.FnInfo) { fn(...args, **kw) { print("call " + info.name); f(...args, **kw) } }
impl reflect.WrapFn for log { fn wrap(self, f, info) { self(f, info) } }
struct Obj { v: Number }
impl Obj {
    @log
    fn m(self) { self.v }
    fn other(self) { self.m() + 1 }
}
let o = Obj { v: 4 }
print(o.m())
print(o.other())
print(Obj.m(o))
"""
        self.assertEqual(
            lines(run_source(src)), ["call m", "4", "call m", "5", "call m", "4"]
        )

    def test_a_trait_method_implementation_can_be_wrapped(self):
        src = REFLECT + """
fn log(f, info: reflect.FnInfo) { fn(...args, **kw) { "[" + f(...args, **kw) + "]" } }
impl reflect.WrapFn for log { fn wrap(self, f, info) { self(f, info) } }
trait Named { fn name(self) -> String }
struct P {}
impl Named for P {
    @log
    fn name(self) -> String { "p" }
}
print(P {}.name(), Named.name(P {}))
"""
        self.assertEqual(run_source(src).strip(), "[p] [p]")

    def test_a_wrapper_that_returns_a_non_function_fails_at_startup(self):
        src = REFLECT + """
fn bad(f, info) { 5 }
impl reflect.WrapFn for bad { fn wrap(self, f, info) { 5 } }
print("never printed")
@bad
fn f() { 1 }
"""
        out, exc = run_source_and_error(src)
        self.assertEqual(out, "")
        self.assertIsNotNone(exc)
        self.assertEqual(str(exc).split(" at position")[0], "WrapFn.wrap must return a function")

    def test_the_wrapper_error_can_be_the_decorators_own_throw(self):
        src = REFLECT + """
fn boom(f, info) { f }
impl reflect.WrapFn for boom { fn wrap(self, f, info) { throw RuntimeError.ArgumentError { message: "refused " + info.name } } }
@boom
fn f() { 1 }
"""
        _out, exc = run_source_and_error(src)
        self.assertIn("refused f", str(exc))

    def test_a_decorator_without_a_hook_does_not_change_the_function(self):
        src = REFLECT + """
struct Tag { n: Number }
fn tag(n) -> Tag { Tag { n: n } }
@tag(1)
fn f() { "same" }
print(f(), reflect.signature(f).decorators.len())
"""
        self.assertEqual(run_source(src).strip(), "same 1")

    def test_signature_decorators_and_parameters_are_the_originals(self):
        src = REFLECT + """
fn log(f, info) { fn(...args, **kw) { f(...args, **kw) } }
impl reflect.WrapFn for log { fn wrap(self, f, info) { self(f, info) } }
## Greets.
@log
fn greet(name: String, punct: String = "!") -> String { "hi" }
let sig = reflect.signature(greet)
print(sig.name, sig.doc, sig.params.len(), sig.params[1].has_default, sig.rest, sig.decorators.len())
"""
        self.assertEqual(run_source(src).strip(), "greet Greets. 2 true none 1")

    def test_find_finds_a_function_decorator_even_after_it_was_wrapped(self):
        src = REFLECT + """
fn log(f, info) { fn(...args, **kw) { f(...args, **kw) } }
impl reflect.WrapFn for log { fn wrap(self, f, info) { self(f, info) } }
@log
fn greet() { 1 }
@greet
fn user() { 2 }
let found = reflect.find(reflect.signature(user).decorators, greet)
print(found != none)
print(reflect.find(reflect.signature(user).decorators, log) == none)
print(reflect.find(reflect.signature(greet).decorators, log) != none)
print(reflect.find(reflect.signature(user).decorators, user) == none)
"""
        self.assertEqual(lines(run_source(src)), ["true", "true", "true", "true"])

    def test_find_tells_two_closures_of_one_function_apart(self):
        src = REFLECT + """
fn make(n) { fn() { n } }
let a = make(1)
let b = make(2)
let ds = [a]
print(reflect.find(ds, a) != none, reflect.find(ds, b) == none)
"""
        self.assertEqual(run_source(src).strip(), "true true")

    def test_an_anonymous_function_takes_no_decorators_and_gets_no_hooks(self):
        # decorators on closures are a compile error (M41b); a closure keeps its own identity
        src = REFLECT + "let f = fn() { 1 }\nprint(reflect.signature(f).name == \"\", f())"
        self.assertEqual(run_source(src).strip(), "true 1")


# ---------------------------------------------------------------------------
# WrapParam
# ---------------------------------------------------------------------------

HOOKS = REFLECT + """
fn trim(v, info: reflect.ParamInfo) { v }
impl reflect.WrapParam for trim { fn transform(self, v, info) { v.trim() } }
fn upper(v, info) { v }
impl reflect.WrapParam for upper { fn transform(self, v, info) { v.to_upper() } }
fn named(v, info) { v }
impl reflect.WrapParam for named {
    fn transform(self, v, info) { info.name + "#" + info.index.to_string() + ":" + v }
}
"""


class WrapParamTests(unittest.TestCase):
    def test_the_argument_is_transformed(self):
        out = run_source(HOOKS + 'fn hi(@trim name: String) { "[" + name + "]" }\nprint(hi("  x "))')
        self.assertEqual(out.strip(), "[x]")

    def test_the_closest_hook_runs_first(self):
        src = HOOKS + """
fn a(@upper @trim s) { "[" + s + "]" }
fn b(@trim @upper s) { "[" + s + "]" }
print(a("  ab "))
print(b("  ab "))
fn order(v, info) { v }
impl reflect.WrapParam for order { fn transform(self, v, info) { v + "!" } }
fn c(@upper @order s) { s }
print(c("x"))
"""
        self.assertEqual(lines(run_source(src)), ["[AB]", "[AB]", "X!"])

    def test_two_hooks_see_the_previous_result(self):
        src = HOOKS + """
fn first(v, info) { v }
impl reflect.WrapParam for first { fn transform(self, v, info) { v + "1" } }
fn second(v, info) { v }
impl reflect.WrapParam for second { fn transform(self, v, info) { v + "2" } }
fn f(@second @first s) { s }
print(f("x"))
"""
        self.assertEqual(run_source(src).strip(), "x12")

    def test_info_name_and_index(self):
        src = HOOKS + 'fn f(a, @named b, c = 3, @named d = "z") { [b, d] }\nprint(f(1, "z1"))\nprint(f(1, "y", 3, "w"))'
        self.assertEqual(lines(run_source(src)), ["[b#1:z1, d#3:z]", "[b#1:y, d#3:w]"])

    def test_info_function_is_the_original(self):
        src = HOOKS + """
fn which(v, info) { v }
impl reflect.WrapParam for which { fn transform(self, v, info) { reflect.signature(info.function).name } }
fn target(@which x) { x }
print(target(1))
"""
        self.assertEqual(run_source(src).strip(), "target")

    def test_a_hook_that_throws_propagates_and_can_be_caught(self):
        src = HOOKS + """
struct Bad {}
impl reflect.WrapParam for Bad {
    fn transform(self, v, info) { throw RuntimeError.ArgumentError { message: "bad " + info.name } }
}
fn mk() -> Bad { Bad {} }
fn f(@mk() v) { v }
print(try { f(1) } catch { RuntimeError.ArgumentError { message } => { "caught " + message } })
f(2)
"""
        out, exc = run_source_and_error(src)
        self.assertEqual(out.strip(), "caught bad v")
        self.assertIn("bad v", str(exc))

    def test_the_hook_runs_on_every_call_path(self):
        src = HOOKS + """
fn hi(@trim name: String) { "[" + name + "]" }
print(reflect.call(hi, [" r "]))
print(hi(...[" s "]))
print(reflect.call(hi, [], ["name": " k "]))
print(hi(name: " n "))
let p = detach hi(" d ")
print(p.await)
"""
        self.assertEqual(lines(run_source(src)), ["[r]", "[s]", "[k]", "[n]", "[d]"])

    def test_the_hook_sees_a_default_after_it_is_filled(self):
        src = HOOKS + 'fn f(@trim s = "  dflt ") { s }\nprint(f())\nprint(f(" given "))'
        self.assertEqual(lines(run_source(src)), ["dflt", "given"])

    def test_a_decorated_parameter_without_a_hook_behaves_normally(self):
        src = HOOKS + """
struct Doc { text: String }
fn doc(t) -> Doc { Doc { text: t } }
fn f(@doc("a") x, @doc("b") y = 2) { x + y }
print(f(1), f(1, 5))
"""
        self.assertEqual(run_source(src).strip(), "3 6")

    def test_a_hook_that_awaits_works(self):
        src = HOOKS + """
fn slow(v, info) { v }
impl reflect.WrapParam for slow { fn transform(self, v, info) { sleep_async(5); v + 1 } }
fn waits(@slow n) { n }
print(waits(1))
"""
        self.assertEqual(run_source(src).strip(), "2")

    def test_methods_and_closures_in_impls(self):
        src = HOOKS + """
struct S {}
impl S {
    fn m(self, @trim s) { s }
    fn make(@upper s) { s }
}
print(S {}.m("  q "), S.make("w"))
"""
        self.assertEqual(run_source(src).strip(), "q W")

    def test_the_hook_only_exists_for_decorated_parameters(self):
        # (undecorated parameters cost nothing: no `paramhooks` for them)
        program = compile_program(text=HOOKS + "fn f(@trim a, b, @upper c) { a }\nfn g(x) { x }\n")
        mine = [i for i in program.code if i.op == "paramhooks"]
        self.assertEqual(len(mine), 2)
        self.assertEqual(sorted(i.args[1] for i in mine), [0, 2])


# ---------------------------------------------------------------------------
# WrapField and WrapStruct
# ---------------------------------------------------------------------------

FIELDS = REFLECT + """
fn upper(v, info: reflect.FieldInfo) { v }
impl reflect.WrapField for upper {
    fn set(self, v, info) { print("set " + info.name); v.to_upper() }
}
struct Positive {}
impl reflect.WrapStruct for Positive {
    fn construct(self, v, info) {
        print("construct")
        if v.age < 0 { throw RuntimeError.ArgumentError { message: "age < 0" } }
        v
    }
}
fn positive() -> Positive { Positive {} }
"""


class FieldAndStructHookTests(unittest.TestCase):
    USER = FIELDS + "struct User {\n    @upper name: String,\n    age: Number\n}\n"

    def test_a_literal_runs_the_field_hook(self):
        out = run_source(self.USER + 'print(User { name: "ann", age: 1 }.name)')
        self.assertEqual(lines(out), ["set name", "ANN"])

    def test_an_assignment_runs_the_field_hook(self):
        out = run_source(self.USER + 'let u = User { name: "ann", age: 1 }\nu.name = "bob"\nprint(u.name)')
        self.assertEqual(lines(out), ["set name", "set name", "BOB"])

    def test_construct_runs_the_hooks(self):
        out = run_source(self.USER + 'print(reflect.construct(User, ["name": "cy", "age": 1]).name)')
        self.assertEqual(lines(out), ["set name", "CY"])

    def test_assignment_inside_a_method_runs_the_hook(self):
        src = self.USER + """
impl User { fn rename(self, n) { self.name = n } }
let u = User { name: "a", age: 1 }
u.rename("dan")
print(u.name)
"""
        self.assertEqual(lines(run_source(src))[-1], "DAN")

    def test_other_fields_are_assigned_normally(self):
        src = self.USER + 'let u = User { name: "a", age: 1 }\nu.age = 9\nprint(u.age)'
        self.assertEqual(lines(run_source(src)), ["set name", "9"])

    def test_the_hook_runs_once_per_write_without_recursion(self):
        src = self.USER + 'let u = User { name: "a", age: 1 }\nu.name = "b"'
        self.assertEqual(lines(run_source(src)), ["set name", "set name"])

    def test_a_struct_hook_can_reject_a_value(self):
        src = (
            FIELDS
            + "@positive()\nstruct Person { age: Number }\n"
            + 'print(try { Person { age: 0 - 1 } } catch { RuntimeError.ArgumentError { message } => { message } })\n'
            + "print(Person { age: 3 }.age)\n"
        )
        self.assertEqual(lines(run_source(src)), ["construct", "age < 0", "construct", "3"])

    def test_a_rejected_literal_throws_to_the_caller(self):
        src = FIELDS + "@positive()\nstruct Person { age: Number }\nPerson { age: 0 - 1 }\n"
        _out, exc = run_source_and_error(src)
        self.assertIn("age < 0", str(exc))

    def test_field_hooks_run_before_struct_hooks(self):
        src = FIELDS + '@positive()\nstruct P { @upper name: String, age: Number }\nP { name: "a", age: 1 }\n'
        self.assertEqual(lines(run_source(src)), ["set name", "construct"])

    def test_construct_rejects_too(self):
        src = (
            FIELDS
            + "@positive()\nstruct Person { age: Number }\n"
            + 'print(try { reflect.construct(Person, ["age": 0 - 1]) } catch { RuntimeError.ArgumentError { message } => { message } })'
        )
        self.assertEqual(lines(run_source(src)), ["construct", "age < 0"])

    def test_a_struct_hook_can_replace_the_value(self):
        src = REFLECT + """
struct Wrapped { n: Number }
fn double(v, info) { v }
struct Doubler {}
impl reflect.WrapStruct for Doubler {
    fn construct(self, v, info) { Wrapped { n: v.n * 2 } }
}
fn doubler() -> Doubler { Doubler {} }
@doubler()
struct Num { n: Number }
print(Num { n: 21 })
"""
        self.assertEqual(run_source(src).strip(), "Wrapped { n: 42 }")

    def test_info_names_the_field_and_the_struct(self):
        src = REFLECT + """
fn probe(v, info) { v }
impl reflect.WrapField for probe {
    fn set(self, v, info) { print(info.name, info.type); v }
}
struct Probed { @probe a: Number }
Probed { a: 1 }
"""
        self.assertEqual(run_source(src).strip(), "a Probed")

    def test_a_struct_without_hooks_in_a_program_with_decorators_works(self):
        src = self.USER + """
struct Plain { a: Number }
let p = Plain { a: 1 }
p.a = 2
print(p.a)
let q = Plain { a: 3 }
print(q.a)
"""
        self.assertEqual(lines(run_source(src)), ["2", "3"])

    def test_field_assignment_on_non_structs_still_reports_errors(self):
        _out, exc = run_source_and_error(self.USER + "let n = 5\nn.x = 1\n")
        self.assertIn("non-struct", str(exc))

    def test_a_hooked_type_in_another_module(self):
        files = {
            "lib.mh": REFLECT
            + """
export fn upper(v, info) { v }
impl reflect.WrapField for upper { fn set(self, v, info) { v.to_upper() } }
export struct User { @upper name: String }
impl User { fn copy_with(self, n) { Self { name: n } } }
""",
            "main.mh": 'import lib from "lib"\nlet u = lib.User { name: "ann" }\nprint(u.name)\nprint(u.copy_with("zed").name)\nu.name = "bob"\nprint(u.name)\n',
        }
        with project(files) as td:
            self.assertEqual(lines(run_file(os.path.join(td, "main.mh"))), ["ANN", "ZED", "BOB"])

    def test_a_decorator_from_another_module_with_its_impl_there(self):
        files = {
            "lib.mh": REFLECT
            + """
export fn greeter(f, info) { f }
impl reflect.WrapFn for greeter { fn wrap(self, f, info) { fn(...a, **k) { "G:" + f(...a, **k) } } }
""",
            "main.mh": 'import lib from "lib"\n@lib.greeter\nfn hey() { "hey" }\nprint(hey())\n',
        }
        with project(files) as td:
            self.assertEqual(run_file(os.path.join(td, "main.mh")).strip(), "G:hey")

    def test_an_undecorated_struct_literal_has_no_hook_check(self):
        program = compile_program(text=self.USER + 'struct Plain { a: Number }\nlet p = Plain { a: 1 }\nlet u = User { name: "a", age: 1 }\n')
        mine = entry_instrs(program)
        # the User literal asks the VM for hooks; the Plain one doesn't
        names = [program.strings[program.types[i.args[0] - 3].name] for i in mine if i.op == "struct"]
        self.assertEqual(sorted(n for n in names if n in ("Plain", "User")), ["Plain", "User"])
        calls = natives_called(program, mine)
        self.assertEqual(calls.count("hooks.of"), 1)

    def test_assignments_check_for_hooks_only_in_programs_with_decorators(self):
        plain = compile_program(text="struct P { a: Number }\nlet p = P { a: 1 }\np.a = 2\n")
        self.assertNotIn("hooks.of", natives_called(plain, plain.code))
        self.assertEqual([i for i in plain.code if i.op == "paramhooks"], [])
        decorated = compile_program(text=self.USER + "struct P { a: Number }\nlet p = P { a: 1 }\np.a = 2\n")
        self.assertEqual(natives_called(decorated, entry_instrs(decorated)).count("hooks.of"), 1)


# ---------------------------------------------------------------------------
# The implicit import and the minor version
# ---------------------------------------------------------------------------


class ImplicitImportAndMinorTests(unittest.TestCase):
    TAG = "fn tag(x) { x }\n@tag(1)\nfn f() { 1 }\nprint(f())\n"

    def minor(self, src: str) -> int:
        return decode(compile_bytes(text=src)).minor

    def test_a_program_without_the_features_keeps_its_old_minor(self):
        self.assertEqual(self.minor("print(1)"), 4)
        self.assertEqual(self.minor("fn f(a) { a }\nprint(f(1))"), 4)
        self.assertEqual(self.minor('import math from "std:math"\nprint(math.log(1))'), 5)
        self.assertEqual(self.minor('print(" a ".trim())'), 6)
        self.assertEqual(self.minor("struct S { a }\nlet s = S { a: 1 }\ns.a = 2\nprint(s.a)"), 4)

    def test_a_rest_parameter_needs_1_16(self):
        self.assertEqual(self.minor("fn f(...r) { r }\nprint(f(1))"), 16)
        self.assertEqual(self.minor("fn f(**k) { k }\nprint(f(a: 1))"), 16)
        self.assertEqual(self.minor("let f = fn(...r) { r }\nprint(f(1))"), 16)

    def test_a_function_impl_needs_1_16(self):
        src = "trait Tr { fn m(self) -> String }\nfn f() { 1 }\nimpl Tr for f { fn m(self) -> String { \"x\" } }\nprint(f.m())\n"
        self.assertEqual(self.minor(src), 16)
        program = compile_program(text=src)
        keys = [program.strings[i.args[1]] for i in program.code if i.op == "defmethod"]
        self.assertTrue(all(re.fullmatch(r"fn#\d+", k) for k in keys), keys)
        # ... and the key is the function's index
        index = next(i for i, f in enumerate(program.functions) if f.name is not None and program.strings[f.name] == "f")
        self.assertEqual(keys, [f"fn#{index}"])

    def test_a_wrapfn_hook_is_1_16(self):
        self.assertEqual(self.minor(WrapFnTests.LOGGING), 16)

    def test_metadata_only_decorators_are_1_16_too(self):
        # (the hidden std:reflect import and the `hooks.*` natives its setup helpers use)
        self.assertEqual(self.minor(self.TAG), 16)

    def test_std_reflect_and_json_need_1_16(self):
        self.assertEqual(self.minor(REFLECT + "print(reflect.type_of(1))"), 16)
        self.assertEqual(self.minor('import json from "std:json"\nprint(json.parse("1"))'), 16)

    def test_the_implicit_import_runs_next_to_the_users_own_import(self):
        for header in (REFLECT, 'import "std:reflect"\n', "", REFLECT + 'import "std:reflect"\n'):
            with self.subTest(header):
                self.assertEqual(run_source(header + self.TAG).strip(), "1")

    def test_a_user_variable_named_reflect_does_not_clash(self):
        self.assertEqual(run_source("let reflect = 5\n" + self.TAG + "print(reflect)").split(), ["1", "5"])
        self.assertEqual(run_source("fn reflect() { 7 }\n" + self.TAG + "print(reflect())").split(), ["1", "7"])

    def test_a_user_name_like_a_helper_does_not_clash(self):
        src = "fn __setup_type(a) { 1 }\nfn __run_param(a) { 2 }\n" + self.TAG + "print(__setup_type(0), __run_param(0))"
        self.assertEqual(run_source(src).split(), ["1", "1", "2"])

    def test_the_helpers_are_not_reachable_by_name(self):
        for helper in ("__setup_type", "__setup_param", "__wrap_fn", "__run_param", "__run_struct", "__run_field"):
            with self.subTest(helper):
                with self.assertRaisesRegex(Exception, rf"Undefined variable '{helper}'"):
                    compile_bytes(text=self.TAG + f"print({helper})")
                # (what an importer gets for a name that isn't exported)
                with self.assertRaisesRegex(Exception, rf"Undefined variable '__mah_noexport_{helper}'"):
                    compile_bytes(text=REFLECT + self.TAG + f"print(reflect.{helper})")

    def test_the_info_structs_and_traits_are_exported_but_not_in_the_prelude(self):
        out = run_source(
            REFLECT
            + 'print(reflect.FnInfo { name: "a", function: 1 }.name, reflect.TypeInfo { type: Number }.type)'
        )
        self.assertEqual(out.strip(), "a Number")
        with self.assertRaisesRegex(Exception, "Undefined|not exported|WrapFn"):
            compile_bytes(text="struct S {}\nimpl WrapFn for S { fn wrap(self, f, info) { f } }")

    def test_a_flat_import_brings_the_hook_traits_into_scope(self):
        src = 'import "std:reflect"\nstruct S {}\nimpl WrapFn for S { fn wrap(self, f, info) { fn() { 7 } } }\nfn make() -> S { S {} }\n@make()\nfn f() { 1 }\nprint(f())\n'
        self.assertEqual(run_source(src).strip(), "7")

    def test_the_hidden_module_sits_after_the_users_code(self):
        from mah.preprocessor import preprocess

        pp = preprocess(None, self.TAG)
        self.assertIsNotNone(pp.hidden_reflect)
        self.assertTrue(pp.text.startswith(self.TAG))
        self.assertIsNone(preprocess(None, "print(1)").hidden_reflect)
        self.assertIsNone(preprocess(None, REFLECT + self.TAG).hidden_reflect)


# ---------------------------------------------------------------------------
# Bytecode 1.16
# ---------------------------------------------------------------------------


def params_flag_offset(body: bytes) -> int:
    """Where the PARAMS entry of `fn f(a, ...r, **k)` starts in an encoded file
    (`3 params`, then name+flags for each: flags 0, 2, 4, i.e. the flags are
    at offsets 2, 4 and 6)."""
    found = re.search(rb"\x03.\x00.\x02.\x04", body)
    assert found is not None
    return found.start()


class BytecodeTests(unittest.TestCase):
    REST_SRC = "fn f(a, ...r, **k) { }\nprint(1)\n"

    def test_the_format_constants_are_pinned(self):
        self.assertEqual(MINOR, 20)  # M42
        self.assertEqual(OPCODES["paramhooks"], (0x3E, ("N", "N", "A")))
        self.assertEqual(OPCODE_SINCE_MINOR["paramhooks"], 16)
        arities = {
            "hooks.has": 2,
            "hooks.adopt": 2,
            "hooks.same_fn": 2,
            "hooks.set_type": 2,
            "hooks.set_param": 3,
            "hooks.of": 1,
            "hooks.get_field": 2,
            "hooks.set_field": 3,
        }
        for name, arity in arities.items():
            with self.subTest(name):
                self.assertEqual(NATIVE_ARITIES[name], arity)
                self.assertEqual(NATIVE_SINCE_MINOR[name], 16)

    def test_rest_flags_round_trip(self):
        program = compile_program(text=self.REST_SRC)
        fn = next(f for f in program.functions if f.name is not None and program.strings[f.name] == "f")
        self.assertEqual(fn.rest, 3)
        data = encode(program)
        self.assertEqual(decode(data), program)
        self.assertEqual(encode(decode(data)), data)
        body = data[data.index(b"\n") + 1 :] if data.startswith(b"#!") else data
        self.assertGreaterEqual(params_flag_offset(body), 0)
        only_rest = compile_program(text="fn g(a, ...r) { }\nfn h(**k) { }\nprint(1)\n")
        by_name = {only_rest.strings[f.name]: f.rest for f in only_rest.functions if f.name is not None}
        self.assertEqual((by_name["g"], by_name["h"]), (1, 2))

    def test_dis_shows_rest_parameters_and_the_new_opcode(self):
        text = disassemble(decode(compile_bytes(text=self.REST_SRC)))
        self.assertIn("params=(a, ...r, **k)", text)
        text = disassemble(decode(compile_bytes(text=HOOKS + "fn f(@trim a) { a }\n")))
        self.assertRegex(text, r"paramhooks\s+fn=\S+ param=0 dest=")

    def _patched(self, edits: dict, minor: int | None = None) -> bytes:
        data = compile_bytes(text=self.REST_SRC)
        shebang = data.index(b"\n") + 1 if data.startswith(b"#!") else 0
        body = bytearray(data[shebang:])
        at = params_flag_offset(bytes(body))
        for offset, value in edits.items():
            body[at + offset] = value
        if minor is not None:
            body[6:8] = minor.to_bytes(2, "little")
        return bytes(body)

    def assertRejected(self, data: bytes, message: str) -> None:
        with self.assertRaises(MahcFormatError) as cm:
            decode(data)
        self.assertIn(message, str(cm.exception))

    def test_decode_validates_the_rest_flags(self):
        self.assertRejected(self._patched({6: 5}), "is a rest parameter and can't have a default")
        self.assertRejected(self._patched({6: 6}), "is flagged as both a '...' and a '**' rest parameter")
        self.assertRejected(self._patched({6: 8}), "invalid flags byte 8 (only bits 0-2 are defined)")
        self.assertRejected(self._patched({4: 3}), "is a rest parameter and can't have a default")
        self.assertRejected(
            self._patched({2: 2}),
            "the '...' rest parameter must be the last parameter or the one before the '**' rest parameter",
        )
        self.assertRejected(self._patched({4: 4}), "the '**' rest parameter must be the last parameter")
        # `...` last without a `**` is fine, as is `...` before `**`
        decode(self._patched({}))
        decode(self._patched({4: 0, 6: 2}))  # no `**`: just `...`, last

    def test_a_rest_flag_in_an_older_file_is_rejected(self):
        self.assertRejected(self._patched({}, minor=15), "invalid flags byte 2 (only bit 0 is defined)")

    def test_paramhooks_needs_1_16_and_a_real_parameter(self):
        program = compile_program(text=HOOKS + "fn f(@trim a) { a }\n")
        at = next(i for i, instr in enumerate(program.code) if instr.op == "paramhooks")
        program.minor = 15
        with self.assertRaisesRegex(MahcFormatError, r"opcode 'paramhooks' at instruction \d+ requires minor version >= 16"):
            decode(encode(program))
        program = compile_program(text=HOOKS + "fn f(@trim a) { a }\n")
        fn, _index, dest = program.code[at].args
        program.code[at] = Instr("paramhooks", (fn, 7, dest))
        with self.assertRaisesRegex(
            MahcFormatError, rf"'paramhooks' at instruction {at}: parameter index 7 out of range for function {fn}"
        ):
            decode(encode(program))
        program.code[at] = Instr("paramhooks", (9999, 0, dest))
        with self.assertRaisesRegex(
            MahcFormatError, rf"'paramhooks' at instruction {at}: function index 9999 out of range"
        ):
            decode(encode(program))

    def test_function_item_keys_are_validated(self):
        src = "trait Tr { fn m(self) -> String }\nfn f() { 1 }\nimpl Tr for f { fn m(self) -> String { \"x\" } }\n"
        for key in ("fn#9999", "fn#x", "fn#01", "fn#"):
            with self.subTest(key):
                program = compile_program(text=src)
                at = next(
                    i
                    for i, instr in enumerate(program.code)
                    if instr.op == "defmethod" and program.strings[instr.args[1]].startswith("fn#")
                )
                program.strings[program.code[at].args[1]] = key
                with self.assertRaisesRegex(
                    MahcFormatError, rf"'defmethod' at instruction {at}: '{key}' is not a valid function-item key"
                ):
                    decode(encode(program))

    def test_the_hooks_natives_need_1_16(self):
        program = compile_program(text=REFLECT + "print(1)")
        program.minor = 15
        with self.assertRaisesRegex(MahcFormatError, r"requires minor version >= 16"):
            decode(encode(program))

    def test_the_hooks_natives_validate_their_arguments(self):
        from mah.bytecode.program import Const, FunctionDecl, NativeRef, Program

        def program_calling(native: str, arity: int, args: list) -> Program:
            strings = [native, "io.print"]
            constants = []
            code = []
            for i, value in enumerate(args):
                strings.append(value)
                constants.append(Const(5, len(strings) - 1))  # a String constant
                code.append(Instr("loadk", (i, (0, i))))
            code.append(Instr("native", (0, tuple((0, i) for i in range(arity)), (0, 9))))
            code.append(Instr("halt", ()))
            return Program(
                strings=strings,
                constants=constants,
                types=[],
                natives=[NativeRef(0, arity), NativeRef(1, 1)],
                functions=[FunctionDecl(0, 12, 0, None, params=[])],
                code=code,
                debug=None,
                minor=16,
            )

        for native, arity, message in (
            ("hooks.adopt", 2, "hooks.adopt: expected two Functions, got String and String"),
            ("hooks.get_field", 2, "hooks.get_field: expected a struct, got String"),
            ("hooks.set_field", 3, "hooks.set_field: expected a struct, got String"),
            ("hooks.set_type", 2, "hooks.set_type: expected a Type, got String"),
            ("hooks.set_param", 3, "hooks.set_param: expected a Function, got String"),
        ):
            with self.subTest(native):
                data = encode(program_calling(native, arity, ["x"] * arity))
                _out, exc = support._run_capturing(data, "")
                self.assertIn(message, str(exc))
        # `has` wants a String trait name; `same_fn` and `of` accept anything
        data = encode(program_calling("hooks.has", 2, ["x", "y"]))
        _out, exc = support._run_capturing(data, "")
        self.assertIsNone(exc)
        for native, arity in (("hooks.same_fn", 2), ("hooks.of", 1)):
            data = encode(program_calling(native, arity, ["x"] * arity))
            _out, exc = support._run_capturing(data, "")
            self.assertIsNone(exc, native)

    def test_hooks_has_and_of_answer_without_errors(self):
        src = REFLECT + """
struct S {}
print(reflect.type_of(S {}) == S)
"""
        self.assertEqual(run_source(src).strip(), "true")


if __name__ == "__main__":
    unittest.main()
