"""M41a (docs/REFLECTION.md): type values, `##` doc comments, the META
section, spread calls, `std:reflect`, `json.decode`, and the tooling around
them (checker, formatter, LSP, disassembler).

Programs run through `tests/support.py`, so `make test-rust` reruns every
behavioral case on the Rust VM (`MAH_TEST_VM=rust`). mah/std/reflect.test.mh
covers the API again from inside Mah, and runtime/tests/vm_diff.py compares
the two VMs' output and error messages directly.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mah.bytecode.decode import decode  # noqa: E402
from mah.bytecode.disasm import disassemble  # noqa: E402
from mah.bytecode.encode import encode  # noqa: E402
from mah.bytecode.format import MINOR, MahcFormatError  # noqa: E402
from mah.bytecode.program import Instr  # noqa: E402
from mah.compiler.lexer import Lexer, TokenType  # noqa: E402
from mah.format import format_source  # noqa: E402
from mah.lsp import analysis  # noqa: E402
from tests.support import _run, compile_bytes, compile_program, parse_source, run_source  # noqa: E402
from tests.test_typecheck import check as _check_all  # noqa: E402

REFLECT = 'import reflect from "std:reflect"\n'
JSON = 'import json from "std:json"\n'


def check(src: str):
    """The checker's problems for `src` (mismatches, unhandled errors,
    warnings) and every declaration's type -- without the "implicit"
    notes, which the standard library modules a program imports may carry."""
    diagnostics, types = _check_all(src)
    return [d for d in diagnostics if d[0] != "implicit"], types


def _compile_error(src: str) -> str:
    try:
        compile_bytes(text=src)
    except Exception as exc:  # noqa: BLE001
        return str(exc)
    raise AssertionError("expected a compile error")


class TypeValueTests(unittest.TestCase):
    def test_a_bare_type_name_is_a_value(self):
        src = (
            REFLECT
            + "struct User { name: String }\nprint(User)\nlet t = User\nprint(t == User)\n"
            + "print(Number)\nprint(reflect.type_of(User))\n"
        )
        self.assertEqual(run_source(src), "User\ntrue\nNumber\nType\n")

    def test_every_built_in_name_is_a_type_value(self):
        src = "print(Number, String, Bool, Function, Vector, Map, Option, Promise, RuntimeError, None, Type)"
        self.assertEqual(run_source(src), "Number String Bool Function Vector Map Option Promise RuntimeError None Type\n")

    def test_equality_is_identity_of_the_declared_type(self):
        src = (
            "struct A { }\nstruct B { }\nenum E { X }\n"
            "print(A == A, A == B, A != B, A == Number, E == E, Number == Number, Number == String)\n"
            "print(A == 1, 1 == A, none == A)\n"
        )
        self.assertEqual(run_source(src), "true false true false true true false\nfalse false false\n")

    def test_a_variable_named_like_a_type_wins(self):
        self.assertEqual(run_source("let User = 3\nstruct User { name: String }\nprint(User)\n"), "3\n")
        self.assertEqual(run_source("struct User { name: String }\nfn f(User) { User + 1 }\nprint(f(2))\n"), "3\n")
        # ... while a name that isn't a variable is the type
        self.assertEqual(run_source("struct User { }\nfn f(x) { User }\nprint(f(2))\n"), "User\n")

    def test_the_enum_unit_variant_rule_is_unchanged(self):
        self.assertEqual(run_source("enum Shape { Empty }\nprint(Shape.Empty)\nprint(Shape)\n"), "Shape.Empty\nShape\n")
        self.assertEqual(run_source("enum Shape { Empty }\nlet Shape = 5\nprint(Shape)\n"), "5\n")

    def test_self_is_the_impl_type(self):
        src = "struct A { }\nimpl A { fn me(self) { Self } }\nprint(A { }.me() == A)\n"
        self.assertEqual(run_source(src), "true\n")

    def test_type_values_are_values(self):
        src = (
            "struct A { }\nlet types = [A, Number]\nprint(types, types[0] == A)\n"
            "fn same(t, u) { t == u }\nprint(same(A, A), same(A, Vector))\n"
            'print("" + A + "!")\n'
        )
        self.assertEqual(run_source(src), "[A, Number] true\ntrue false\nA!\n")

    def test_types_are_not_map_keys(self):
        src = "struct A { }\nprint(try { [A: 1] } catch { e: RuntimeError => { e.message() } })\n"
        self.assertEqual(run_source(src), "Map keys must be a String, Number, or Bool, got Type\n")

    def test_json_stringify_rejects_a_type(self):
        src = JSON + 'print(try { json.stringify(Number) } catch { e: json.JsonError => { e.message() } })\n'
        self.assertEqual(run_source(src), "stringify: can't write a Type as JSON\n")

    def test_type_values_survive_a_deep_copy(self):
        self.assertEqual(run_source("struct A { }\nlet v = [A, [Number]]\nlet w = v.copy(deep: true)\nprint(w[0] == A, w[1][0] == Number)"), "true true\n")

    def test_a_type_is_printable(self):
        self.assertEqual(run_source("struct A { }\nprint(Printable.to_string(A))\nlet t = A\nprint(t.to_string())\n"), "A\nA\n")

    def test_the_receiver_of_a_static_call_cant_be_spread(self):
        self.assertIn("can't be a spread argument", _compile_error("print(Printable.to_string(...[3]))"))

    def test_a_type_name_that_names_nothing_is_still_an_error(self):
        self.assertIn("Undefined variable 'Nope'", _compile_error("print(Nope)"))

    def test_type_of_each_kind(self):
        src = (
            REFLECT
            + "struct User { name: String }\n"
            + 'print(reflect.type_of(3) == Number, reflect.type_of("a") == String, reflect.type_of(true) == Bool)\n'
            + 'print(reflect.type_of(User { name: "a" }) == User, reflect.type_of([1]) == Vector, reflect.type_of([1: 2]) == Map)\n'
            + "print(reflect.type_of(none), reflect.type_of(some(1)), reflect.type_of(fn() { 1 }), reflect.type_of(User))\n"
        )
        self.assertEqual(run_source(src), "true true true\ntrue true true\nNone Option Function Type\n")


class SpreadCallTests(unittest.TestCase):
    F = "fn f(a, b = 2, c = 3) { print(a + b + c) }\n"

    def test_positional_and_keyword_spreads(self):
        src = self.F + 'f(...[1, 10])\nf(1, **["c": 100])\nf(...[1], b: 5, **["c": 0])\n'
        self.assertEqual(run_source(src), "14\n103\n6\n")

    def test_several_spreads_mixed_with_plain_arguments(self):
        src = (
            "fn g(a, b, c, d, e = 0) { print(a, b, c, d, e) }\n"
            "g(...[1, 2], 3, ...[], ...[4])\n"
            'g(1, ...[2, 3], d: 4, **["e": 5])\n'
            'let extra = ["e": 9]\ng(...[1, 2, 3, 4], **extra)\n'
            "let xs = [7, 8]\ng(0, ...xs, 9)\n"
        )
        self.assertEqual(run_source(src), "1 2 3 4 0\n1 2 3 4 5\n1 2 3 4 9\n0 7 8 9 0\n")

    def test_spread_does_not_change_the_source_collections(self):
        src = "fn f(a, b) { a + b }\nlet xs = [1, 2]\nlet m = [:]\nprint(f(...xs, **m))\nprint(xs, m)\n"
        self.assertEqual(run_source(src), "3\n[1, 2] [:]\n")

    def test_the_arguments_are_evaluated_left_to_right(self):
        src = (
            "let log = []\nfn note(x) { log.push(x); x }\n"
            "fn f(a, b, c, d = 0) { }\n"
            'f(note(1), ...[note(2)], note(3), d: note(4))\nprint(log)\n'
        )
        self.assertEqual(run_source(src), "[1, 2, 3, 4]\n")

    def test_a_keyword_given_twice_is_an_argument_error(self):
        src = self.F + 'print(try { f(1, b: 1, **["b": 2]) } catch { RuntimeError.ArgumentError { message } => { message } })\n'
        self.assertEqual(run_source(src), "keyword argument 'b' given more than once\n")
        src = self.F + 'print(try { f(1, **["b": 1], **["b": 2]) } catch { RuntimeError.ArgumentError { message } => { message } })\n'
        self.assertEqual(run_source(src), "keyword argument 'b' given more than once\n")

    def test_a_positional_and_a_keyword_for_one_parameter(self):
        src = self.F + 'print(try { f(1, **["a": 2]) } catch { RuntimeError.ArgumentError { message } => { message } })\n'
        self.assertEqual(run_source(src), "'f' got multiple values for argument 'a'\n")

    def test_non_vectors_and_non_maps_are_argument_errors(self):
        cases = {
            "f(...3)": "'...' needs a Vector, got Number",
            "f(**3)": "'**' needs a Map, got Number",
            "f(**[1: 2])": "'**' needs String keys, got a Number key",
            'f(...["a"])': None,
        }
        for call, message in cases.items():
            with self.subTest(call=call):
                out = run_source(self.F + f"print(try {{ {call} }} catch {{ RuntimeError.ArgumentError {{ message }} => {{ message }} }})\n")
                if message is not None:
                    self.assertEqual(out, message + "\n")

    def test_an_uncaught_one_is_a_runtime_error(self):
        from mah.runtime_values import MahRuntimeError

        with self.assertRaisesRegex(MahRuntimeError, "'...' needs a Vector, got Number"):
            run_source(self.F + "f(...3)\n")

    def test_method_calls_take_spreads_too(self):
        src = (
            "struct P { n }\nimpl P {\n  fn m(self, a, b = 0) { self.n + a + b }\n  fn make(a, b) { P { n: a * b } }\n}\n"
            "let p = P { n: 100 }\nprint(p.m(...[1, 2]))\nprint(p.m(...[1], b: 10))\nprint(p.m(1, **[\"b\": 20]))\n"
            "print(P.make(...[3, 4]).n)\n"
            'print(try { p.m(...[1], **["a": 1]) } catch { e: RuntimeError => { e.message() } })\n'
        )
        self.assertEqual(run_source(src), "103\n111\n121\n12\nmethod 'm' got multiple values for argument 'a'\n")

    def test_native_and_trait_method_calls(self):
        src = 'print("a-b".split(...["-"]))\nprint(Printable.to_string(3, ...[]))\nlet v = []\nv.push(...[1])\nprint(v)\n'
        self.assertEqual(run_source(src), "[a, b]\n3\n[1]\n")

    def test_spread_of_a_callee_that_isnt_a_function(self):
        src = "let x = 3\nprint(try { x(...[1]) } catch { e: RuntimeError => { e.message() } })\n"
        self.assertEqual(run_source(src), "Tried to call a non-function value (Number)\n")

    def test_detach_with_a_spread_is_a_compile_error(self):
        self.assertIn("spread arguments can't be detached yet", _compile_error(self.F + "detach f(...[1])\n"))
        self.assertIn("spread arguments can't be detached yet", _compile_error(self.F + "detach f(1, **[:])\n"))

    def test_print_and_sleep_async_take_no_spread(self):
        self.assertIn("doesn't take spread arguments", _compile_error("print(...[1])"))

    def test_ranges_are_unaffected(self):
        self.assertEqual(run_source("for let i in 1..3 { print(i) }\n"), "1\n2\n")
        self.assertEqual(run_source("let r = 1..\nprint(r.start)\nprint([1, 2, 3][1..])\nprint(2 ** 3 ** 2, 2 ** 3)\n"), "1\n[2, 3]\n512 8\n")

    def test_exponent_is_unaffected_inside_arguments(self):
        self.assertEqual(run_source("fn f(a, b) { a + b }\nprint(f(2 ** 3, 3 ** 2))\nprint(f(1, b: 2 ** 2))\n"), "17\n5\n")

    def test_lexing_the_ellipsis(self):
        def types(text):
            lexer = Lexer(text)
            out = []
            while True:
                tok = lexer.get_next_token()
                if tok.type is TokenType.EOF:
                    return out
                out.append(tok.type)

        self.assertEqual(types("...xs"), [TokenType.ELLIPSIS, TokenType.ID])
        self.assertEqual(types("1..5"), [TokenType.NUMBER, TokenType.DOTDOT, TokenType.NUMBER])
        self.assertEqual(types("1..=5"), [TokenType.NUMBER, TokenType.DOTDOT_EQ, TokenType.NUMBER])
        self.assertEqual(types("a.."), [TokenType.ID, TokenType.DOTDOT])
        self.assertEqual(types("a.b"), [TokenType.ID, TokenType.DOT, TokenType.ID])

    def test_positional_after_keyword_is_still_a_syntax_error(self):
        for src in ("f(a: 1, ...xs)", "f(k: 1, 2)", "f(**m, ...xs)"):
            with self.subTest(src=src):
                _program, parser = parse_source(src + "\n")
                self.assertEqual(len(parser.errors), 1)
                self.assertIn("positional argument after a keyword argument", parser.errors[0][0])


class DocCommentTests(unittest.TestCase):
    def _docs(self, src: str):
        program, parser = parse_source(src)
        assert not parser.errors, parser.errors
        return program

    def test_docs_attach_to_the_declaration_below(self):
        program = self._docs(
            "## Adds.\n## Twice.\nfn add(\n    ## the first\n    a,\n    b\n) { }\n"
            "## A user.\nstruct User {\n    ## The name.\n    name,\n    age\n}\n"
            "## A shape.\nenum Shape {\n    ## Round.\n    Circle { r },\n    Empty\n}\n"
            "## A trait.\ntrait T { fn m(self) }\n"
            "impl User {\n    ## Greets.\n    fn greet(self) { }\n    fn plain(self) { }\n}\n"
        )
        fn = program[0].value
        self.assertEqual(fn.doc, "Adds.\nTwice.")
        self.assertEqual(fn.param_docs, ["the first", None])
        self.assertEqual((program[1].doc, program[1].field_docs), ("A user.", ["The name.", None]))
        self.assertEqual((program[2].doc, program[2].variant_docs), ("A shape.", ["Round.", None]))
        self.assertEqual(program[3].doc, "A trait.")
        self.assertEqual([m.doc for m in program[4].methods], ["Greets.", None])

    def test_plain_comments_are_never_docs(self):
        program = self._docs("# not a doc\nfn f() { }\n## a doc\n# but this ends it?\nfn g() { }\n")
        self.assertIsNone(program[0].value.doc)
        self.assertIsNone(program[1].value.doc)

    def test_a_blank_line_ends_the_run(self):
        program = self._docs("## far away\n\nfn f() { }\n")
        self.assertIsNone(program[0].value.doc)

    def test_a_doc_belongs_only_to_the_first_thing_on_its_line(self):
        program = self._docs("## The struct.\nstruct User { name, age }\n")
        self.assertEqual(program[0].doc, "The struct.")
        self.assertEqual(program[0].field_docs, [None, None])

    def test_the_marker_and_one_space_are_removed(self):
        program = self._docs("##tight\n##  two spaces\n##\n## end\nfn f() { }\n")
        self.assertEqual(program[0].value.doc, "tight\n two spaces\n\nend")

    def test_a_double_hash_inside_a_string_is_not_a_doc(self):
        program = self._docs('let s = "\n## not a comment\n"\nfn f() { }\n')
        self.assertIsNone(program[1].value.doc)

    def test_docs_of_an_exported_std_function_survive_the_preprocessor(self):
        src = REFLECT + 'import process from "std:process"\nprint(reflect.signature(process.pid).doc)\n'
        self.assertEqual(run_source(src), "This program's process id.\n")

    def test_reflects_own_api_is_documented(self):
        src = REFLECT + "for let f in [reflect.type_of, reflect.signature, reflect.schema, reflect.methods, reflect.implements, reflect.call, reflect.construct, reflect.construct_variant] { print(reflect.signature(f).doc != \"\") }\n"
        self.assertEqual(run_source(src), "true\n" * 8)

    def test_docs_of_a_program_split_across_imports(self):
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "lib.mh"), "w") as f:
                f.write("## Doubles.\nexport fn double(n: Number) -> Number { n * 2 }\n")
            with open(os.path.join(td, "main.mh"), "w") as f:
                f.write(REFLECT + 'import lib from "./lib"\nlet s = reflect.signature(lib.double)\nprint(s.name, s.doc)\n')
            from tests.support import run_file

            self.assertEqual(run_file(os.path.join(td, "main.mh")), "double Doubles.\n")


class SignatureTests(unittest.TestCase):
    SIG = (
        REFLECT
        + "struct MyErr { m: String }\nimpl Error for MyErr { fn message(self) { self.m } }\nfn foo() { 1 }\n"
        + "## Adds.\n## Twice.\nfn add(a: Number, b: Number = 1, c = foo()) -> Number throws MyErr { a + b }\n"
        + "let s = reflect.signature(add)\n"
    )

    def test_name_doc_and_parameters(self):
        out = run_source(
            self.SIG
            + "print(s.name)\nprint(s.doc)\nprint(s.params.len())\n"
            + "let a = s.params[0]\nlet b = s.params[1]\nlet c = s.params[2]\n"
            + "print(a.name, a.has_default, a.default, b.name, b.has_default, b.default, c.name, c.has_default, c.default)\n"
        )
        self.assertEqual(out, "add\nAdds.\nTwice.\n3\na false none b true some(1) c true none\n")

    def test_parameter_types(self):
        out = run_source(
            self.SIG
            + "match s.params[0].type {\n"
            + "  reflect.TypeRef.Named { type: t, args: args } => { print(t == Number, args.len()) }\n"
            + "  _ => { print(\"?\") }\n}\n"
            + "match s.params[2].type {\n  reflect.TypeRef.Unknown => { print(\"Unknown\") }\n  _ => { print(\"?\") }\n}\n"
        )
        self.assertEqual(out, "true 0\nUnknown\n")

    def test_returns_and_throws(self):
        out = run_source(
            self.SIG
            + "match s.returns {\n  reflect.TypeRef.Named { type: t, args: args } => { print(t == Number) }\n  _ => { print(\"?\") }\n}\n"
            + "match s.throws {\n  some(list) => { print(list.len()) }\n  none => { print(\"no clause\") }\n}\n"
            + "match reflect.signature(foo).throws {\n  some(list) => { print(list.len()) }\n  none => { print(\"no clause\") }\n}\n"
            + "match reflect.signature(foo).returns {\n  reflect.TypeRef.Unknown => { print(\"unwritten\") }\n  _ => { print(\"?\") }\n}\n"
        )
        self.assertEqual(out, "true\n1\nno clause\nunwritten\n")

    def test_throws_never_is_an_empty_clause(self):
        out = run_source(
            REFLECT + "fn f() throws never { }\nmatch reflect.signature(f).throws {\n  some(list) => { print(list.len()) }\n  none => { print(\"none\") }\n}\n"
        )
        self.assertEqual(out, "0\n")

    def test_constant_defaults(self):
        out = run_source(
            REFLECT
            + 'fn f(a = 1, b = -2, c = "s", d = true, e = none, f = 1 + 1, g = [1], h = 1.5) { }\n'
            + "let ps = reflect.signature(f).params\nfor let p in ps { print(p.name, p.has_default, p.default) }\n"
        )
        self.assertEqual(
            out,
            "a true some(1)\nb true some(-2)\nc true some(s)\nd true some(true)\ne true some(none)\n"
            "f true none\ng true none\nh true some(1.5)\n",
        )

    def test_generics_and_function_types(self):
        out = run_source(
            REFLECT
            + "fn first<T>(v: Vector<T>) -> T { v[0] }\nfn g(f: fn(Number) -> String) { }\n"
            + "let s = reflect.signature(first)\nprint(s.type_params)\n"
            + "match s.params[0].type {\n"
            + "  reflect.TypeRef.Named { type: t, args: args } => {\n"
            + "    print(t == Vector, args.len())\n"
            + "    match args[0] {\n      reflect.TypeRef.Param { name } => { print(name) }\n      _ => { print(\"?\") }\n    }\n"
            + "  }\n  _ => { print(\"?\") }\n}\n"
            + "match s.returns {\n  reflect.TypeRef.Param { name } => { print(name) }\n  _ => { print(\"?\") }\n}\n"
            + "match reflect.signature(g).params[0].type {\n"
            + "  reflect.TypeRef.Fn { params, returns, throws } => {\n    print(params.len())\n"
            + "    match returns {\n      reflect.TypeRef.Named { type: t, args: a } => { print(t == String) }\n      _ => { print(\"?\") }\n    }\n  }\n"
            + "  _ => { print(\"?\") }\n}\n"
        )
        self.assertEqual(out, "[T]\ntrue 1\nT\nT\n1\ntrue\n")

    def test_self_never_and_trait_types(self):
        out = run_source(
            REFLECT
            + "trait Shape { fn area(self) -> Number }\nstruct Sq { n }\nimpl Sq { fn twin(self) -> Self { self } }\n"
            + "fn take(s: Shape) -> Never { throw 1 }\n"
            + "match reflect.signature(take).params[0].type {\n  reflect.TypeRef.Trait { name, args } => { print(name) }\n  _ => { print(\"?\") }\n}\n"
            + "match reflect.signature(take).returns {\n  reflect.TypeRef.Never => { print(\"Never\") }\n  _ => { print(\"?\") }\n}\n"
            + "match reflect.signature(reflect.methods(Sq)[0].function).returns {\n  reflect.TypeRef.SelfType => { print(\"Self\") }\n  _ => { print(\"?\") }\n}\n"
        )
        self.assertEqual(out, "Shape\nNever\nSelf\n")

    def test_methods_report_self_and_their_docs(self):
        out = run_source(
            REFLECT
            + "struct A { }\nimpl A {\n  ## Says hi.\n  fn hi(self, name: String) -> String { name }\n}\n"
            + "let s = reflect.signature(reflect.methods(A)[0].function)\nprint(s.name, s.doc, s.params.len(), s.params[0].name)\n"
        )
        self.assertEqual(out, "hi Says hi. 2 self\n")

    def test_anonymous_functions_and_builtin_shapes(self):
        out = run_source(REFLECT + 'let s = reflect.signature(fn(x, y = 1) { x })\nprint(s.name == "", s.params.len(), s.params[1].has_default)\n')
        self.assertEqual(out, "true 2 true\n")

    def test_signature_of_a_non_function(self):
        out = run_source(REFLECT + "print(try { reflect.signature(3) } catch { e: RuntimeError => { e.message() } })\n")
        self.assertEqual(out, "reflect.signature: expected a Function, got Number\n")


class SchemaTests(unittest.TestCase):
    S = (
        REFLECT
        + "## A user.\nstruct User {\n    ## The name.\n    name: String,\n    tags: Vector<String>,\n    age\n}\n"
    )

    def test_a_struct_schema(self):
        out = run_source(
            self.S
            + "match reflect.schema(User) {\n"
            + "  some(reflect.Schema.Struct { type: t, doc: doc, type_params: tps, fields: fields, decorators: ds }) => {\n"
            + "    print(t == User, doc, tps.len(), fields.len())\n"
            + "    for let f in fields { print(f.name, \"|\", f.doc, \"|\", f.type) }\n"
            + "  }\n  _ => { print(\"?\") }\n}\n"
        )
        self.assertEqual(
            out,
            "true A user. 0 3\n"
            "name | The name. | TypeRef.Named { type: String, args: [] }\n"
            "tags |  | TypeRef.Named { type: Vector, args: [TypeRef.Named { type: String, args: [] }] }\n"
            "age |  | TypeRef.Unknown\n",
        )

    def test_an_enum_schema(self):
        out = run_source(
            REFLECT
            + "## Shapes.\nenum Shape<T> {\n    ## Round.\n    Circle { r: Number, tag: T },\n    Empty\n}\n"
            + "match reflect.schema(Shape) {\n"
            + "  some(reflect.Schema.Enum { type: t, doc: doc, type_params: tps, variants: vs, decorators: ds }) => {\n"
            + "    print(doc, tps, vs.len())\n"
            + "    for let v in vs {\n      print(v.name, \"|\", v.doc, \"|\", v.fields.len())\n"
            + "      for let f in v.fields { print(\"  \", f.name, f.type) }\n    }\n"
            + "  }\n  _ => { print(\"?\") }\n}\n"
        )
        self.assertEqual(
            out,
            "Shapes. [T] 2\nCircle | Round. | 2\n   r TypeRef.Named { type: Number, args: [] }\n   tag TypeRef.Param { name: T }\nEmpty |  | 0\n",
        )

    def test_a_primitive_has_no_schema(self):
        out = run_source(REFLECT + "print(reflect.schema(Number) == none, reflect.schema(String) == none, reflect.schema(Vector) == none)\n")
        self.assertEqual(out, "true true true\n")

    def test_the_built_in_enums_have_a_schema(self):
        out = run_source(
            REFLECT
            + "match reflect.schema(Option) {\n  some(reflect.Schema.Enum { type: t, doc: d, type_params: p, variants: vs, decorators: ds }) => { print(vs.len(), vs[1].name, vs[1].fields[0].name) }\n  _ => { print(\"?\") }\n}\n"
        )
        self.assertEqual(out, "2 some value\n")

    def test_schema_of_a_non_type(self):
        out = run_source(REFLECT + "print(try { reflect.schema(3) } catch { e: RuntimeError => { e.message() } })\n")
        self.assertEqual(out, "reflect.schema: expected a Type, got Number\n")


class MethodsAndTraitsTests(unittest.TestCase):
    M = (
        REFLECT
        + "struct User { name: String }\n"
        + "impl User {\n    fn new(n: String) -> User { User { name: n } }\n    fn greet(self) -> String { \"hi \" + self.name }\n}\n"
        + "impl Printable for User { fn to_string(self) -> String { \"user\" } }\n"
    )

    def test_methods_are_inherent_first_then_by_trait(self):
        out = run_source(
            self.M
            + "for let m in reflect.methods(User) { print(m.name, m.is_method, m.trait_name) }\n"
            + "let u = User.new(\"ada\")\nprint(reflect.methods(User)[0].function(u))\n"
        )
        self.assertEqual(out, "greet true none\nnew false none\nto_string true some(Printable)\nhi ada\n")

    def test_a_static_function_is_found_and_callable(self):
        out = run_source(self.M + "let new = reflect.methods(User)[1]\nprint(new.name, new.function(\"bo\").name)\n")
        self.assertEqual(out, "new bo\n")

    def test_implements(self):
        out = run_source(
            self.M
            + 'print(reflect.implements(User, "Printable"), reflect.implements(User, "FromJson"), reflect.implements(Number, "Printable"))\n'
            + 'print(reflect.implements(Vector, "Index"), reflect.implements(Number, "Index"), reflect.implements(Type, "Printable"))\n'
        )
        self.assertEqual(out, "true false true\ntrue false true\n")

    def test_a_type_without_impls_has_no_methods(self):
        out = run_source(REFLECT + "struct A { }\nprint(reflect.methods(A).len(), reflect.methods(Number).len())\n")
        self.assertEqual(out, "0 0\n")


class ConstructAndCallTests(unittest.TestCase):
    C = REFLECT + "struct User { name: String, tags: Vector<String>, age }\nenum Shape { Circle { r: Number }, Empty }\n"

    def test_construct_a_struct(self):
        out = run_source(self.C + 'let u = reflect.construct(User, ["age": 1, "name": "a", "tags": []])\nprint(u)\nprint(reflect.type_of(u) == User)\n')
        self.assertEqual(out, "User { name: a, tags: [], age: 1 }\ntrue\n")

    def test_construct_errors_name_the_field(self):
        out = run_source(
            self.C
            + 'print(try { reflect.construct(User, ["name": "a", "age": 1]) } catch { e: reflect.ReflectError => { e.message() } })\n'
            + 'print(try { reflect.construct(User, ["name": "a", "tags": [], "age": 1, "x": 1]) } catch { e: reflect.ReflectError => { e.message() } })\n'
            + 'print(try { reflect.construct(Number, [:]) } catch { e: reflect.ReflectError => { e.message() } })\n'
            + 'print(try { reflect.construct(Shape, [:]) } catch { e: reflect.ReflectError => { e.message() } })\n'
        )
        self.assertEqual(
            out,
            "missing field 'tags' for User\nUser has no field 'x'\n"
            "can't construct Number: it isn't a struct\ncan't construct Shape: it isn't a struct\n",
        )

    def test_construct_a_variant(self):
        out = run_source(
            self.C
            + 'print(reflect.construct_variant(Shape, "Circle", ["r": 2]), reflect.construct_variant(Shape, "Empty", [:]))\n'
            + 'print(reflect.construct_variant(Option, "some", ["value": 1]), reflect.construct_variant(Option, "none", [:]))\n'
            + 'print(try { reflect.construct_variant(Shape, "Nope", [:]) } catch { e: reflect.ReflectError => { e.message() } })\n'
            + 'print(try { reflect.construct_variant(Shape, "Circle", [:]) } catch { e: reflect.ReflectError => { e.message() } })\n'
            + 'print(try { reflect.construct_variant(Shape, "Circle", ["r": 1, "q": 2]) } catch { e: reflect.ReflectError => { e.message() } })\n'
            + 'print(try { reflect.construct_variant(User, "x", [:]) } catch { e: reflect.ReflectError => { e.message() } })\n'
        )
        self.assertEqual(
            out,
            "Shape.Circle { r: 2 } Shape.Empty\nsome(1) none\n"
            "Shape has no variant 'Nope'\nmissing field 'r' for Shape.Circle\nShape.Circle has no field 'q'\n"
            "can't construct a variant of User: it isn't an enum\n",
        )

    def test_call(self):
        out = run_source(
            REFLECT
            + "fn add(a, b = 1, c = 0) { a + b + c }\n"
            + 'print(reflect.call(add, [1], ["b": 5]), reflect.call(add, [1]), reflect.call(add, [1, 2, 3]), reflect.call(add, kwargs: ["a": 4]))\n'
        )
        self.assertEqual(out, "6 2 6 5\n")


class JsonDecodeTests(unittest.TestCase):
    D = (
        JSON
        + "struct P { x: Number, y: Option<Number>, tags: Vector<String> }\n"
        + "struct Q { p: P, m: Map<String, Number>, o: Option<P> }\n"
        + "enum Shape { Circle { r: Number }, Empty }\n"
    )

    def _shape_error(self, expr: str) -> str:
        return run_source(self.D + f"print(try {{ {expr} }} catch {{ json.JsonError.Shape {{ message }} => {{ message }} }})\n").rstrip("\n")

    def test_decode_a_struct(self):
        out = run_source(
            self.D
            + 'let p = json.decode(P, json.parse("{\\"x\\": 1, \\"tags\\": [\\"a\\"]}"))\nprint(p.x, p.y, p.tags)\n'
            + 'print(json.decode(P, json.parse("{\\"x\\": 1, \\"y\\": 2, \\"tags\\": [], \\"extra\\": 0}")).y)\n'
        )
        self.assertEqual(out, "1 none [a]\nsome(2)\n")

    def test_the_error_messages_say_where(self):
        cases = {
            'json.decode(P, json.parse("{\\"x\\": \\"1\\", \\"tags\\": []}"))': "expected a Number for P.x, got String",
            'json.decode(P, json.parse("{\\"tags\\": []}"))': "missing field 'x' for P",
            'json.decode(P, json.parse("{\\"x\\": 1, \\"tags\\": [\\"a\\", 2]}"))': "expected a String for P.tags[1], got Number",
            'json.decode(P, json.parse("[]"))': "expected an object for P, got Vector",
            'json.decode(Q, json.parse("{\\"p\\": {\\"x\\": true, \\"tags\\": []}, \\"m\\": {}}"))': "expected a Number for Q.p.x, got Bool",
            'json.decode(Q, json.parse("{\\"p\\": {\\"x\\": 1, \\"tags\\": []}, \\"m\\": {\\"k\\": \\"v\\"}}"))': 'expected a Number for Q.m["k"], got String',
            'json.decode(Q, json.parse("{\\"p\\": {\\"tags\\": []}, \\"m\\": {}}"))': "missing field 'x' for Q.p",
            'json.decode(Shape, "Nope")': "unknown variant 'Nope' for Shape",
            'json.decode(Shape, 3)': "expected a String or an object for Shape, got Number",
            'json.decode(Shape, json.parse("{\\"Circle\\": {}}"))': "missing field 'r' for Shape.Circle",
            'json.decode(Shape, json.parse("{\\"Circle\\": {\\"r\\": \\"x\\"}}"))': "expected a Number for Shape.Circle.r, got String",
            "json.decode(Number, \"x\")": "expected a Number for Number, got String",
        }
        for expr, message in cases.items():
            with self.subTest(expr=expr):
                self.assertEqual(self._shape_error(expr), message)

    def test_nested_structs_maps_and_options(self):
        out = run_source(
            self.D
            + 'let q = json.decode(Q, json.parse("{\\"p\\": {\\"x\\": 1, \\"tags\\": []}, \\"m\\": {\\"a\\": 1, \\"b\\": 2}, \\"o\\": {\\"x\\": 3, \\"tags\\": [\\"z\\"]}}"))\n'
            + "print(q.p.x, q.m, q.o.unwrap().x, q.o.unwrap().tags)\n"
        )
        self.assertEqual(out, "1 [a: 1, b: 2] 3 [z]\n")

    def test_enum_round_trip(self):
        out = run_source(
            self.D
            + "let c = json.decode(Shape, json.parse(json.stringify(Shape.Circle { r: 2 })))\nprint(c, c.r)\n"
            + 'print(json.decode(Shape, "Empty"))\n'
        )
        self.assertEqual(out, "Shape.Circle { r: 2 } 2\nShape.Empty\n")

    def test_a_type_with_from_json_uses_it(self):
        out = run_source(
            self.D
            + "struct Odd { n: Number }\nimpl json.FromJson for Odd { fn from_json(value: Unknown) -> Odd { Odd { n: 100 } } }\n"
            + "struct Holder { odd: Odd }\n"
            + 'print(json.decode(Odd, 5).n, json.decode(Holder, json.parse("{\\"odd\\": 1}")).odd.n)\n'
        )
        self.assertEqual(out, "100 100\n")

    def test_parse_as(self):
        out = run_source(self.D + 'print(json.parse_as(P, "{\\"x\\": 1, \\"tags\\": []}"))\n')
        self.assertEqual(out, "P { x: 1, y: none, tags: [] }\n")
        out = run_source(self.D + 'print(try { json.parse_as(P, "{") } catch { e: json.JsonError => { "syntax" } })\n')
        self.assertEqual(out, "syntax\n")

    def test_vector_and_map_without_arguments_pass_items_through(self):
        out = run_source(self.D + 'print(json.decode(Vector, [1, "a"]), json.decode(Map, ["k": none]))\n')
        self.assertEqual(out, "[1, a] [k: none]\n")

    def test_unknown_and_unannotated_fields_pass_through(self):
        out = run_source(JSON + "struct Loose { a, b: Unknown }\nprint(json.decode(Loose, json.parse(\"{\\\"a\\\": [1], \\\"b\\\": null}\")))\n")
        self.assertEqual(out, "Loose { a: [1], b: none }\n")

    def test_decode_ref(self):
        out = run_source(
            REFLECT + JSON
            + "struct P { x: Number }\nmatch reflect.schema(P) {\n"
            + "  some(reflect.Schema.Struct { type: t, doc: d, type_params: tp, fields: fs, decorators: ds }) => { print(json.decode_ref(fs[0].type, 5)) }\n"
            + "  _ => { }\n}\n"
        )
        self.assertEqual(out, "5\n")


class CheckerTests(unittest.TestCase):
    def test_decode_is_typed_by_the_type_value(self):
        src = JSON + "struct P { x: Number }\nfn load(v) -> P throws json.JsonError {\n    let p: P = json.decode(P, v)\n    p\n}\n"
        diagnostics, _ = check(src)
        self.assertEqual(diagnostics, [])

    def test_the_wrong_type_is_a_mismatch(self):
        src = JSON + "struct P { x: Number }\nfn load(v) -> Number throws json.JsonError {\n    let n: Number = json.decode(P, v)\n    n\n}\n"
        diagnostics, _ = check(src)
        self.assertEqual([m for k, m, _l in diagnostics if k == "mismatch"], ["Type mismatch: expected Number, found P"])

    def test_a_type_name_has_type_type_of_it(self):
        diagnostics, types = check("struct User { }\nlet t: Type<User> = User\nlet u = User\nlet n = Number\nlet v = Vector\n")
        self.assertEqual(diagnostics, [])
        self.assertEqual(types["u"], ["Type<User>"])
        self.assertEqual(types["n"], ["Type<Number>"])
        self.assertEqual(types["v"], ["Type<Vector<?>>"])

    def test_a_generic_type_gets_fresh_arguments(self):
        diagnostics, types = check("struct Pair<A, B> { a: A, b: B }\nlet t = Pair\n")
        self.assertEqual(diagnostics, [])
        self.assertEqual(types["t"], ["Type<Pair<?, ?>>"])

    def test_the_wrong_type_value_is_a_mismatch(self):
        diagnostics, _ = check("struct User { }\nstruct Other { }\nlet t: Type<User> = Other\n")
        self.assertEqual(
            diagnostics, [("mismatch", "Type mismatch: expected Type<User>, found Type<Other>", 3)]
        )

    def test_spread_calls_check_their_operands_but_not_the_arity(self):
        diagnostics, _ = check('fn f(a: Number, b: String) { }\nf(...[1, 2], **["b": "x"])\nf(...[1], zz: 3)\nlet bad: Number = "x"\n')
        self.assertEqual([m for k, m, _l in diagnostics if k == "mismatch"], ["Type mismatch: expected Number, found String"])

    def test_the_checker_never_feeds_codegen(self):
        # M22's rule, still: compiling with and without the checker gives the same bytes.
        from mah.compiler.driver import compile_to_bytes

        src = (
            JSON + "struct P { x: Number }\nlet t = P\nfn f(a, b = 1) { a + b }\nprint(f(...[1]))\n"
            'print(try { json.parse_as(P, "{\\"x\\": 1}") } else none)\n'
        )
        self.assertEqual(compile_bytes(text=src), compile_to_bytes(text=src, check="strict"))

    def test_type_takes_exactly_one_argument(self):
        self.assertIn("Type 'Type' takes 1 type argument(s), got 0", _compile_error("fn f(t: Type) { }"))


class MetaTests(unittest.TestCase):
    SRC = "struct MyErr { m: String }\n## Adds.\nfn add(a: Number, b = 1) -> Number throws MyErr { a + b }\nprint(add(1))\n"

    def test_a_program_without_annotations_still_reports_names(self):
        out = run_source(REFLECT + "fn f(a, b = 2) { a }\nlet s = reflect.signature(f)\nprint(s.name, s.doc == \"\", s.params.len(), s.params[0].name, s.params[1].has_default)\n")
        self.assertEqual(out, "f true 2 a true\n")

    def _without_meta(self, src: str) -> bytes:
        program = compile_program(text=src)
        self.assertIsNotNone(program.meta)
        program.meta = None
        return encode(program)

    def test_a_file_without_a_meta_section_reports_names_only(self):
        src = (
            REFLECT
            + "struct User { name: String }\n## Adds.\nfn add(a: Number, b: Number = 1) -> Number { a + b }\n"
            + "let s = reflect.signature(add)\n"
            + "match s.params[0].type {\n  reflect.TypeRef.Unknown => { print(\"Unknown\") }\n  _ => { print(\"?\") }\n}\n"
            + "print(s.name, s.params[0].name, s.params[1].has_default, s.params[1].default, s.doc == \"\")\n"
            + "match reflect.schema(User) {\n  some(reflect.Schema.Struct { type: t, doc: d, type_params: tp, fields: fs, decorators: ds }) => {\n"
            + "    print(fs[0].name)\n    match fs[0].type {\n      reflect.TypeRef.Unknown => { print(\"Unknown\") }\n      _ => { print(\"?\") }\n    }\n  }\n  _ => { }\n}\n"
        )
        self.assertEqual(_run(self._without_meta(src), ""), "Unknown\nadd a true none true\nname\nUnknown\n")
        self.assertIn(b"\x82", compile_bytes(text=src))
        self.assertNotEqual(compile_bytes(text=src), self._without_meta(src))

    def test_meta_round_trips(self):
        program = compile_program(text=self.SRC)
        again = decode(encode(program))
        self.assertEqual(again.meta, program.meta)
        self.assertEqual(again, program)
        self.assertEqual(encode(again), encode(program))

    def test_release_builds_keep_meta(self):
        self.assertIsNotNone(decode(compile_bytes(text=self.SRC, target="release")).meta)
        self.assertIsNone(decode(compile_bytes(text=self.SRC, target="release")).debug)

    def test_an_annotated_program_keeps_its_old_minor(self):
        # META is optional and older VMs skip it: annotations and docs alone don't raise the minor.
        plain = decode(compile_bytes(text="print(1)")).minor
        self.assertEqual(plain, 4)
        self.assertEqual(decode(compile_bytes(text=self.SRC)).minor, plain)
        self.assertEqual(decode(compile_bytes(text="struct S { a: Number }\n## d\nfn f<T>(x: T) -> T { x }\nprint(1)")).minor, plain)

    def test_loadtype_a_spread_and_reflect_natives_write_minor_14(self):
        # (M41b raised MINOR to 15, M41c to 16, M37 to 17 and M38 to 18, but only a file that uses those features is written there)
        self.assertEqual(MINOR, 18)
        self.assertEqual(decode(compile_bytes(text="print(Number)")).minor, 14)
        self.assertEqual(decode(compile_bytes(text="fn f(a) { a }\nprint(f(...[1]))")).minor, 14)
        self.assertEqual(decode(compile_bytes(text="fn f(a) { a }\nprint(f(**[\"a\": 1]))")).minor, 14)
        # M41c: std:reflect itself references the 1.16 `hooks.*` natives (find, construct, the hook helpers)
        self.assertEqual(decode(compile_bytes(text=REFLECT + "print(reflect.type_of(1))")).minor, 16)
        # a struct name in a pattern or literal isn't a type value
        self.assertEqual(decode(compile_bytes(text="struct S { a }\nprint(S { a: 1 })")).minor, 4)

    def test_new_opcodes_are_rejected_below_minor_14(self):
        program = compile_program(text="print(Number)")
        self.assertEqual(program.minor, 14)
        program.minor = 13
        with self.assertRaisesRegex(MahcFormatError, r"opcode 'loadtype' at instruction \d+ requires minor version >= 14"):
            decode(encode(program))

    def test_reflect_natives_are_rejected_below_minor_14(self):
        program = compile_program(text=REFLECT + "print(reflect.type_of(1))")
        program.minor = 13
        with self.assertRaisesRegex(MahcFormatError, r"requires minor version >= 14"):
            decode(encode(program))

    def test_a_meta_section_is_skipped_by_an_older_reader(self):
        # Any file with unknown optional sections decodes as before: drop META's bytes and it's the same program.
        program = compile_program(text=self.SRC)
        program.meta = None
        self.assertIsNone(decode(encode(program)).meta)

    def _with_meta(self, payload: bytes) -> bytes:
        program = compile_program(text="print(1)")
        program.meta = None
        return encode(program) + bytes([0x82, len(payload)]) + payload

    def test_invalid_meta_is_rejected(self):
        cases = [
            (bytes([5]), "META: describes 5 function\\(s\\) but FUNCTIONS declares"),
            (bytes([1, 0, 9]), "META: describes 9 type\\(s\\) but TYPES declares"),
            (bytes([1, 3]), "META: invalid flags byte"),
        ]
        for payload, message in cases:
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(MahcFormatError, message):
                    decode(self._with_meta(payload))
        data = self._with_meta(bytes([1, 0, 0]))
        with self.assertRaises(MahcFormatError):
            decode(data + bytes([0x82, 1, 0]))  # duplicate section

    def test_loadtype_validates_its_operands(self):
        program = compile_program(text="print(Number)")
        for kind, index in ((0, 999), (1, 8), (2, 0)):
            with self.subTest(kind=kind, index=index):
                bad = compile_program(text="print(Number)")
                bad.code = [Instr("loadtype", (kind, index, (0, 0)) ) if i.op == "loadtype" else i for i in bad.code]
                with self.assertRaisesRegex(MahcFormatError, "'loadtype' at instruction"):
                    decode(encode(bad))
        del program

    def test_disassembly_shows_meta_and_the_new_opcodes(self):
        text = disassemble(decode(compile_bytes(text=self.SRC + "let t = Number\nprint(add(...[1], **[:]))\n")))
        self.assertIn("META:", text)
        self.assertIn("add(a: Number, b: Unknown = 1) -> Number throws MyErr", text)
        self.assertIn('doc: "Adds."', text)
        self.assertIn("type#3 struct MyErr { m: String }", text)
        self.assertIn("loadtype", text)
        self.assertIn("callspread", text)
        self.assertIn("spread", text)

    def test_disassembly_without_meta_does_not_crash(self):
        program = compile_program(text=self.SRC)
        program.meta = None
        text = disassemble(decode(encode(program)))
        self.assertNotIn("META:", text)

    def test_mah_dis_on_a_file(self):
        import contextlib
        import io

        from mah.cli.main import main

        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "a.mahc")
            src = os.path.join(td, "a.mh")
            with open(src, "w") as f:
                f.write(self.SRC)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["build", src, "-o", out]), 0)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(main(["dis", out]), 0)
        self.assertIn("META:", buf.getvalue())

    def test_annotations_never_change_behavior(self):
        annotated = "fn f(a: Number, b: Number = 2) -> Number { a * b }\nprint(f(3), f(3, 3))\n"
        plain = "fn f(a, b = 2) { a * b }\nprint(f(3), f(3, 3))\n"
        self.assertEqual(run_source(annotated), run_source(plain))

    def test_a_misspelt_annotation_does_not_break_metadata(self):
        # The resolver rejects an unknown type name, so this is about a name that resolves later:
        # a type parameter shadowed only in the annotation's own function.
        out = run_source(REFLECT + "fn f<T, U>(a: T, b: Vector<U>) -> Map<T, U> { [:] }\nprint(reflect.signature(f).type_params)\n")
        self.assertEqual(out, "[T, U]\n")


class FormatterTests(unittest.TestCase):
    def check(self, source: str, expected: str):
        self.assertEqual(format_source(source), expected)
        self.assertEqual(format_source(expected), expected)

    def test_spread_arguments(self):
        self.check(
            "f( ... xs ,  ** m )\nf(a,...xs,k:v,**m)\no.m( ...  [1] )\nlet x=2**3\n",
            "f(...xs, **m)\nf(a, ...xs, k: v, **m)\no.m(...[1])\nlet x = 2 ** 3\n",
        )

    def test_ranges_keep_their_spacing(self):
        self.check("let r=1..5\nlet s=1..=5\nlet t=a..\nprint(v[1..])\n", "let r = 1..5\nlet s = 1..=5\nlet t = a..\nprint(v[1..])\n")

    def test_doc_comments_round_trip(self):
        src = (
            "## A doc.\n## Second line.\nfn f(\n    ## the a\n    a,\n    b\n) { }\n"
            "## A struct.\nstruct S {\n    ## a field\n    x,\n    y\n}\n"
        )
        self.check(src, src)

    def test_a_long_spread_call_breaks_like_any_call(self):
        out = format_source("let v = some_function(...first_arguments_are_long, ...second_arguments_are_longer, **keyword_arguments_here)\n")
        self.assertIn("...first_arguments_are_long,", out)
        self.assertEqual(format_source(out), out)


class LspTests(unittest.TestCase):
    SRC = REFLECT + "## A user.\nstruct User { name: String }\nlet s = reflect.schema(User)\nlet t = User\nprint(Number)\n"

    def _at(self, needle: str, delta: int = 0) -> tuple:
        offset = self.SRC.index(needle) + delta
        pos = analysis.offset_to_position(self.SRC, offset)
        return pos["line"], pos["character"]

    def test_rename_a_struct_renames_its_type_value_uses(self):
        line, col = self._at("schema(User", 7)
        result = analysis.get_rename_edits(self.SRC, line, col, "Person")
        edits = next(iter(result["changes"].values()))
        new = self._apply(edits)
        self.assertIn("struct Person { name: String }", new)
        self.assertIn("reflect.schema(Person)", new)
        self.assertIn("let t = Person", new)
        self.assertNotIn("User", new)

    def _apply(self, edits: list) -> str:
        spans = []
        for e in edits:
            start = analysis.position_to_offset(self.SRC, e["range"]["start"]["line"], e["range"]["start"]["character"])
            end = analysis.position_to_offset(self.SRC, e["range"]["end"]["line"], e["range"]["end"]["character"])
            spans.append((start, end, e["newText"]))
        text = self.SRC
        for start, end, new in sorted(spans, reverse=True):
            text = text[:start] + new + text[end:]
        return text

    def test_hover_on_a_type_value_shows_the_struct(self):
        line, col = self._at("schema(User", 7)
        hover = analysis.get_hover(self.SRC, line, col)
        self.assertIsNotNone(hover)
        self.assertIn("struct", hover["contents"]["value"])
        self.assertIn("User", hover["contents"]["value"])

    def test_go_to_definition_on_a_type_value(self):
        line, col = self._at("= User", 3)
        location = analysis.get_definition(self.SRC, line, col)
        self.assertIsNotNone(location)
        decl = self.SRC.index("struct User") + len("struct ")
        self.assertEqual(analysis.position_to_offset(self.SRC, location["range"]["start"]["line"], location["range"]["start"]["character"]), decl)

    def test_doc_comments_show_in_hover(self):
        src = "## Adds one.\nfn add1(n) { n + 1 }\nprint(add1(1))\n"
        pos = analysis.offset_to_position(src, src.index("add1(1"))
        hover = analysis.get_hover(src, pos["line"], pos["character"])
        self.assertIn("Adds one.", hover["contents"]["value"])
        self.assertNotIn("# Adds", hover["contents"]["value"])

    def test_no_diagnostics_for_the_new_syntax(self):
        src = "fn f(a, b = 1) { a + b }\nlet xs = [1]\nprint(f(...xs, **[:]))\nprint(User)\nstruct User { }\n"
        self.assertEqual(analysis.get_diagnostics(src), [])


if __name__ == "__main__":
    unittest.main()
