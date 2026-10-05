"""M41b (docs/REFLECTION.md, "M41b: decorators"): decorators as metadata --
the syntax and where it's allowed, the decorator phase (eager, once, per
module, before the module's first statement), the top-level-`let` rule,
bytecode 1.15 (`decorate`, `reflect.decorators`), `std:reflect`'s
`decorators` fields and `find`, and the tooling around them (checker,
doc comments, formatter, LSP).

Programs run through `tests/support.py`, so `make test-rust` reruns every
behavioral case on the Rust VM (`MAH_TEST_VM=rust`); runtime/tests/vm_diff.py
compares the two VMs' output and load errors directly.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mah.bytecode.decode import decode  # noqa: E402
from mah.bytecode.disasm import disassemble  # noqa: E402
from mah.bytecode.encode import encode  # noqa: E402
from mah.bytecode.format import MINOR, MahcFormatError, NATIVE_ARITIES, OPCODES  # noqa: E402
from mah.bytecode.program import Const, FunctionDecl, Instr, NativeRef, Program  # noqa: E402
from mah.compiler import ast_nodes as ast  # noqa: E402
from mah.compiler.driver import compile_to_bytes  # noqa: E402
from mah.compiler.lexer import Lexer, TokenType  # noqa: E402
from mah.compiler.parser import DECORATOR_MISPLACED  # noqa: E402
from mah.format import format_source  # noqa: E402
from mah.lsp import analysis  # noqa: E402
from mah.preprocessor import demangle_message  # noqa: E402
from tests import support  # noqa: E402
from tests.support import compile_bytes, compile_program, parse_source, run_file, run_source  # noqa: E402
from tests.test_typecheck import _front_end  # noqa: E402

REFLECT = 'import reflect from "std:reflect"\n'
MISPLACED = (
    "decorators are only allowed on top-level functions, structs and enums, impl methods, "
    "their parameters, fields and variants"
)

# Every target kind, printed piecewise (a Vector prints its items).
MAIN_PROGRAM = REFLECT + """
struct Route { method: String, path: String }
fn get(path: String) -> Route { Route { method: "GET", path: path } }
let marker = 7
fn tag(t: String) -> String { "tag:" + t }
fn parts(t) {
    match reflect.schema(t) {
        some(reflect.Schema.Struct { type: a, doc: b, type_params: c, fields: fields, decorators: ds }) => { [ds, fields] }
        some(reflect.Schema.Enum { type: a, doc: b, type_params: c, variants: vs, decorators: ds }) => { [ds, vs] }
        _ => { [] }
    }
}
## Fetch.
@get("/users/{id}")
@tag("users")
fn get_user(@tag("path") id: Number, verbose: Bool = false) -> Number { id }
@tag("model")
struct User { @tag("json:user_name") name: String, age: Number }
enum Shape { @tag("round") Circle { r: Number }, Empty }
impl User { @tag("m") fn hello(self) -> String { "hi" } }
let s = reflect.signature(get_user)
print(s.doc)
print(s.decorators[0].path)
print(s.decorators[1])
print(s.params[0].decorators)
print(s.params[1].decorators.len())
let user = parts(User)
print(user[0])
print(user[1][0].decorators)
print(user[1][1].decorators)
let shape = parts(Shape)
print(shape[0].len())
print(shape[1][0].decorators)
print(shape[1][1].decorators)
for let m in reflect.methods(User) { print(m.name, reflect.signature(m.function).decorators) }
"""


@contextmanager
def project(files: dict):
    """A temp directory holding `files` (name -> source); yields its path."""
    with tempfile.TemporaryDirectory() as td:
        for name, source in files.items():
            with open(os.path.join(td, name), "w", encoding="utf-8") as f:
                f.write(source)
        yield td


def run_main(files: dict) -> str:
    with project(files) as td:
        return run_file(os.path.join(td, "main.mh"))


def compile_error(src: str = None, files: dict = None) -> str:
    try:
        if files is not None:
            with project(files) as td:
                compile_to_bytes(path=os.path.join(td, "main.mh"))
        else:
            compile_bytes(text=src)
    except Exception as exc:  # noqa: BLE001
        return demangle_message(str(exc))
    raise AssertionError("expected a compile error")


def run_error(src: str):
    """`(stdout before the error, the error's text)` for a program that ends
    with an uncaught error."""
    out, exc = support._run_capturing(compile_bytes(text=src), "")
    assert exc is not None, "expected an uncaught error"
    return out, str(exc)


class LexerTests(unittest.TestCase):
    def test_at_is_a_token(self):
        lexer = Lexer("@a.b(1) @")
        types = []
        while True:
            tok = lexer.get_next_token()
            types.append(tok.type)
            if tok.type is TokenType.EOF:
                break
        self.assertEqual(
            types,
            [
                TokenType.AT,
                TokenType.ID,
                TokenType.DOT,
                TokenType.ID,
                TokenType.PAREN_OPEN,
                TokenType.NUMBER,
                TokenType.PAREN_CLOSE,
                TokenType.AT,
                TokenType.EOF,
            ],
        )


class ParserTests(unittest.TestCase):
    def parse(self, src: str):
        program, parser = parse_source(src)
        self.assertEqual(parser.errors, [])
        return program

    def test_decorators_are_ordinary_expressions(self):
        program = self.parse('@a\n@lib.b\n@c(1, ...xs, k: 2, **m)\n@Route.make("x")\nfn f() { }\n')
        [stmt] = program
        kinds = [type(d).__name__ for d in stmt.value.decorators]
        self.assertEqual(kinds, ["Ident", "FieldAccess", "Call", "MethodCall"])
        call = stmt.value.decorators[2]
        self.assertEqual(len(call.args), 2)  # 1 and ...xs
        self.assertIsInstance(call.args[1], ast.SpreadArg)
        self.assertEqual([name for name, _v, _p in call.kwargs], ["k", None])

    def test_every_allowed_position(self):
        program = self.parse(
            "@a fn f(@p x, y, @q @r z) { }\n"
            "@b extern fn e(@p x) = \"m.n\"\n"
            "@c struct S { @d x, y }\n"
            "@e enum E { @f A { r }, B, @g @h C }\n"
            "impl S { @i fn m(self, @j z) { } fn n(self) { } }\n"
        )
        f, e, s, en, impl = program
        self.assertEqual([len(d) for d in f.value.param_decorators], [1, 0, 2])
        self.assertEqual(len(f.value.decorators), 1)
        self.assertEqual(len(e.value.decorators), 1)
        self.assertEqual([len(d) for d in s.field_decorators], [1, 0])
        self.assertEqual([len(d) for d in en.variant_decorators], [1, 0, 2])
        m, n = impl.methods
        self.assertEqual((len(m.decorators), [len(d) for d in m.param_decorators]), (1, [0, 1]))
        self.assertEqual(n.decorators, [])
        # the method's function carries the same lists
        self.assertIs(m.fn.decorators, m.decorators)

    def test_newlines_between_decorators_and_the_declaration(self):
        program = self.parse("@a\n\n@b(\n  1,\n  2\n)\n\nstruct S { }\n")
        self.assertEqual(len(program[0].decorators), 2)

    def test_a_decorator_can_be_written_before_export(self):
        # `export` is the preprocessor's; the parser then sees the decorator before the fn
        with project(
            {
                "lib.mh": 'fn get(p) { p }\n@get("/")\nexport fn f() { 1 }\n',
                "main.mh": 'import lib from "lib"\nprint(lib.f())\n',
            }
        ) as td:
            self.assertEqual(run_file(os.path.join(td, "main.mh")), "1\n")

    def test_misplaced_decorators_are_compile_errors(self):
        cases = {
            "a nested fn": "fn outer() {\n    @a\n    fn inner() { }\n}\n",
            "a closure": "let f = @a fn(x) { x }\n",
            "an anonymous fn statement": "@a fn(x) { x }\n",
            "a let": "@a\nlet x = 1\n",
            "a trait method": "trait T { @a fn m(self) }\n",
            "a trait": "@a\ntrait T { fn m(self) }\n",
            "an impl block": "struct S { }\n@a\nimpl S { }\n",
            "a statement": "@a\nprint(1)\n",
            "an expression": "let x = @a\n",
            "a nested struct": "fn f() { @a struct S { } }\n",
            "a closure parameter": "let f = fn(@a x) { x }\n",
            "a nested fn's parameter": "fn outer() { fn inner(@a x) { x } }\n",
            "a trait method's parameter": "trait T { fn m(self, @a x) }\n",
            "a variant's field": "enum E { A { @a x } }\n",
            "a test": '@a\ntest "t" { }\n',
        }
        for name, src in cases.items():
            with self.subTest(name):
                _program, parser = parse_source(src)
                self.assertTrue(parser.errors, "no parse error")
                self.assertIn(MISPLACED, parser.errors[0][0])
                self.assertEqual(MISPLACED, DECORATOR_MISPLACED)
                self.assertIn(MISPLACED, compile_error(src))

    def test_parse_errors_recover_after_a_misplaced_decorator(self):
        # M6 recovery: both mistakes are reported and the rest still parses
        program, parser = parse_source("@a\nlet x = 1\nfn ok() { 1 }\nfn f() { @b fn g() { } }\nlet y = 2\n")
        self.assertEqual(len([e for e in parser.errors if MISPLACED in e[0]]), 2)
        names = [s.name for s in program if isinstance(s, ast.LetStmt)]
        self.assertIn("ok", names)
        self.assertIn("y", names)

    def test_a_decorator_argument_error_is_a_syntax_error(self):
        _program, parser = parse_source("@a(1,\nfn f() { }\n")
        self.assertTrue(parser.errors)


class MetadataTests(unittest.TestCase):
    def test_every_target_kind(self):
        out = run_source(MAIN_PROGRAM)
        self.assertEqual(
            out.splitlines(),
            [
                "Fetch.",
                "/users/{id}",
                "tag:users",
                "[tag:path]",
                "0",
                "[tag:model]",
                "[tag:json:user_name]",
                "[]",
                "0",
                "[tag:round]",
                "[]",
                "hello [tag:m]",
            ],
        )

    def test_the_decorator_values_are_kept_as_they_are(self):
        src = REFLECT + """
struct Route { method: String, path: String }
fn get(path: String) -> Route { Route { method: "GET", path: path } }
fn many(a, b: Number = 2) -> Vector<Unknown> { [a, b] }
@get("/x")
@many(1, b: 3)
@many(...[4], **["b": 5])
fn f() { }
let d = reflect.signature(f).decorators
print(d[0].method, d[0].path)
print(d[1], d[2])
print(d.len())
"""
        self.assertEqual(run_source(src), "GET /x\n[1, 3] [4, 5]\n3\n")

    def test_decorators_on_a_generic_function_and_a_method_parameter(self):
        src = REFLECT + """
fn t(x) { x }
@t("f")
fn first<T>(@t("v") v: Vector<T>) -> T { v[0] }
struct S { }
impl S { fn m(self, @t("p") a, @t("q") @t("r") b) { } }
print(reflect.signature(first).decorators, reflect.signature(first).params[0].decorators)
let sig = reflect.signature(reflect.methods(S)[0].function)
print(sig.params.len(), sig.params[1].decorators, sig.params[2].decorators)
"""
        self.assertEqual(run_source(src), "[f] [v]\n3 [p] [q, r]\n")

    def test_decorators_of_a_trait_impl_method(self):
        src = REFLECT + """
fn t(x) { x }
trait Named { fn name(self) -> String }
struct S { }
impl Named for S { @t("n") fn name(self) -> String { "s" } }
print(reflect.signature(reflect.methods(S)[0].function).decorators)
"""
        self.assertEqual(run_source(src), "[n]\n")

    def test_no_decorators_is_an_empty_vector_and_a_fresh_copy(self):
        src = REFLECT + """
fn t(x) { x }
@t(1)
fn f(a) { }
fn g() { }
print(reflect.signature(g).decorators, reflect.signature(g).decorators.len())
let one = reflect.signature(f).decorators
one.push(99)
print(reflect.signature(f).decorators)
print(one)
"""
        self.assertEqual(run_source(src), "[] 0\n[1]\n[1, 99]\n")

    def test_a_program_without_std_reflect_runs_with_decorators(self):
        src = 'fn t(x) { print("eval", x); x }\n@t(1)\nfn f() { }\nprint("done")\n'
        self.assertEqual(run_source(src), "eval 1\ndone\n")

    def test_anonymous_and_nested_functions_have_no_decorators(self):
        src = REFLECT + "print(reflect.signature(fn(a) { a }).decorators.len())\nfn f() { fn g() { } g }\nprint(reflect.signature(f()).decorators.len())\n"
        self.assertEqual(run_source(src), "0\n0\n")


class FindTests(unittest.TestCase):
    SRC = REFLECT + """
struct Route { method: String, path: String }
struct Other { }
fn get(path: String) -> Route { Route { method: "GET", path: path } }
fn my_fn(x) { x }
fn other_fn() { }
@get("/users/{id}")
@my_fn
@"a string"
fn f() { }
"""

    def test_find_by_type(self):
        src = self.SRC.replace('@"a string"\n', "") + "let s = reflect.signature(f)\nprint(reflect.find(s.decorators, Route))\nprint(reflect.find(s.decorators, Number))\n"
        out = run_source(src)
        self.assertEqual(out.splitlines()[0], "some(Route { method: GET, path: /users/{id} })")
        self.assertEqual(out.splitlines()[1], "none")

    def test_the_found_value_is_usable(self):
        src = self.SRC.replace('@"a string"\n', "") + """
match reflect.find(reflect.signature(f).decorators, Route) {
    some(r) => { print(r.path) }
    none => { print("missing") }
}
"""
        self.assertEqual(run_source(src), "/users/{id}\n")

    def test_find_a_function_by_identity(self):
        src = self.SRC.replace('@"a string"\n', "") + """
let d = reflect.signature(f).decorators
print(reflect.find(d, my_fn) != none)
print(reflect.find(d, other_fn))
"""
        out = run_source(src)
        self.assertEqual(out.splitlines()[1], "none")
        self.assertNotEqual(out.splitlines()[0], "false")

    def test_find_by_value_equality(self):
        src = REFLECT + """
fn t(x) { x }
@t("json")
@t(5)
fn f() { }
let d = reflect.signature(f).decorators
print(reflect.find(d, "json"), reflect.find(d, 5), reflect.find(d, "nope"), reflect.find(d, String), reflect.find(d, Number))
"""
        self.assertEqual(run_source(src), "some(json) some(5) none some(json) some(5)\n")

    def test_find_on_no_decorators(self):
        self.assertEqual(run_source(REFLECT + "print(reflect.find([], Number), reflect.find([], 1))\n"), "none none\n")

    def test_find_first_of_several(self):
        src = REFLECT + """
fn t(x) { x }
@t(1)
@t(2)
fn f() { }
print(reflect.find(reflect.signature(f).decorators, Number))
"""
        self.assertEqual(run_source(src), "some(1)\n")


class EvaluationTests(unittest.TestCase):
    def test_evaluated_eagerly_once_in_source_order(self):
        src = REFLECT + """
fn mark(name: String) -> String { print("eval " + name); name }
print("first statement")
@mark("a")
fn first() { }
@mark("b")
struct S { @mark("c") x }
enum E { @mark("d") A }
impl S { @mark("e") fn m(self, @mark("f") p) { } }
print(reflect.signature(first).decorators)
print(reflect.signature(first).decorators)
"""
        self.assertEqual(
            run_source(src).splitlines(),
            ["eval a", "eval b", "eval c", "eval d", "eval e", "eval f", "first statement", "[a]", "[a]"],
        )

    def test_calling_reflect_twice_does_not_evaluate_again(self):
        src = REFLECT + """
fn tick(x) { print("tick"); x }
@tick(1)
fn f() { }
reflect.signature(f)
reflect.signature(f)
reflect.signature(f).decorators
print("done")
"""
        self.assertEqual(run_source(src), "tick\ndone\n")

    def test_a_statement_before_the_declaration_already_sees_the_decorators(self):
        # (a function can't be named before its declaration, but a type and a
        # method can: the phase runs before the module's first statement)
        src = REFLECT + """
fn mark(name: String) -> String { print("eval " + name); name }
print("top")
print(reflect.methods(S)[0].name)
print(reflect.signature(reflect.methods(S)[0].function).decorators)
match reflect.schema(S) {
    some(reflect.Schema.Struct { type: a, doc: b, type_params: c, fields: fields, decorators: ds }) => { print(ds, fields[0].decorators) }
    _ => { }
}
@mark("late fn")
fn later() { }
@mark("type")
struct S { @mark("field") x }
impl S { @mark("method") fn m(self) { } }
"""
        self.assertEqual(
            run_source(src).splitlines(),
            ["eval late fn", "eval type", "eval field", "eval method", "top", "m", "[method]", "[type] [field]"],
        )

    def test_a_decorator_can_use_a_function_declared_after_the_first_statement(self):
        # the closures of the declarations are created before the phase
        src = REFLECT + """
let x = 1
print("stmt")
fn make(p) { "made " + p }
@make("a")
fn f() { }
print(reflect.signature(f).decorators)
"""
        self.assertEqual(run_source(src), "stmt\n[made a]\n")

    def test_the_phase_goes_at_the_end_when_there_is_no_statement(self):
        src = REFLECT + """
fn mark(name: String) -> String { print("eval " + name); name }
@mark("only")
fn f() { }
"""
        self.assertEqual(run_source(src), "eval only\n")

    def test_top_level_variables_are_fine_in_functions_a_decorator_calls(self):
        # only the *name* of an own-module `let` is checked, not what a called function reads
        src = 'let P = "p"\nfn route(x) { "" + x }\n@route("a")\nfn f() { }\nprint(P)\n'
        self.assertEqual(run_source(src), "p\n")

    def test_decorators_with_a_defer_statement_around(self):
        src = 'fn t(x) { print("eval " + x); x }\ndefer print("deferred")\n@t("a")\nfn f() { }\nprint("body")\n'
        self.assertEqual(run_source(src), "eval a\nbody\ndeferred\n")

    def test_a_program_with_decorators_and_a_let_fn_behaves_as_before(self):
        src = "let f = fn(x) { x + 1 }\nfn g(x) { f(x) }\nfn t(a) { a }\n@t(1)\nfn h() { }\nprint(g(2))\n"
        self.assertEqual(run_source(src), "3\n")

    def test_a_decorator_that_throws_is_an_uncaught_error_at_startup(self):
        src = (
            "struct Oops { message: String }\nimpl Error for Oops { fn message(self) -> String { self.message } }\n"
            'fn boom(x) { throw Oops { message: "bad" + x } }\nprint("never")\n@boom("!")\nfn f() { }\n'
        )
        out, error = run_error(src)
        self.assertEqual(out, "")
        self.assertIn("bad!", error)

    def test_a_decorator_runtime_error_is_reported_like_any_other(self):
        src = "fn boom(x) { x + none }\nprint(\"never\")\n@boom(1)\nfn f() { }\n"
        out, error = run_error(src)
        self.assertEqual(out, "")
        self.assertIn("Cannot apply '+' to Number and Option", error)

    def test_mah_test_builds_run_the_phase_before_the_tests(self):
        src = """import "std:test"
import reflect from "./reflect"
fn mark(name: String) -> String { print("eval " + name); name }
@mark("f")
fn f() { }
test "sees them" {
    assert_eq(reflect.signature(f).decorators.len(), 1)
    print("in test")
}
"""
        # test blocks are declarations: the phase goes at the end of the module's code
        # (the reflect import comes from the standard library directory)
        src = src.replace('"./reflect"', '"std:reflect"')
        out, outcome = support.run_test_case(src, 0)
        self.assertEqual(outcome.status, "ok", outcome)
        self.assertIn("in test", out)


class ModuleTests(unittest.TestCase):
    def test_a_namespaced_factory_that_uses_its_modules_own_variable(self):
        files = {
            "lib.mh": 'let PREFIX = "/api"\nexport fn route(p) { PREFIX + p }\n',
            "main.mh": REFLECT + 'import lib from "lib"\n@lib.route("/x")\nfn f() { }\nprint(reflect.signature(f).decorators)\n',
        }
        self.assertEqual(run_main(files), "[/api/x]\n")

    def test_a_flat_import_works_too(self):
        files = {
            "lib.mh": 'let PREFIX = "/api"\nexport fn route(p) { PREFIX + p }\n',
            "main.mh": REFLECT + 'import "lib"\n@route("/y")\nfn f() { }\nprint(reflect.signature(f).decorators)\n',
        }
        self.assertEqual(run_main(files), "[/api/y]\n")

    def test_a_module_evaluates_its_own_decorators_before_its_first_statement(self):
        files = {
            "lib.mh": (
                'export fn mark(n) { print("eval lib " + n); n }\n'
                '@mark("own")\nexport fn f() { }\n'
                'print("lib statement")\n'
            ),
            "main.mh": REFLECT + 'import lib from "lib"\nprint(reflect.signature(lib.f).decorators)\n',
        }
        self.assertEqual(run_main(files), "eval lib own\nlib statement\n[own]\n")

    def test_nested_imports_evaluate_dependencies_first(self):
        files = {
            "b.mh": (
                'export fn mk(n) { print("eval b " + n); n }\n'
                '@mk("b1")\nfn bf() { }\n'
                '@mk("b2")\nstruct BS { }\n'
                'print("b top")\n'
            ),
            "a.mh": (
                'import "b"\nexport fn mk2(n) { print("eval a " + n); n }\n'
                '@mk("a-uses-b")\n@mk2("a1")\nfn af() { }\n'
                'print("a top")\n'
            ),
            "main.mh": (
                'import a from "a"\nimport reflect from "std:reflect"\nimport "std:reflect"\n'
                '@a.mk2("m1")\nfn mf() { }\n'
                'print("main top")\nprint(signature(mf).decorators)\n'
            ),
        }
        self.assertEqual(
            run_main(files).splitlines(),
            [
                "eval b b1",
                "eval b b2",
                "b top",
                "eval b a-uses-b",
                "eval a a1",
                "a top",
                "eval a m1",
                "main top",
                "[m1]",
            ],
        )

    def test_a_module_without_statements_evaluates_at_the_end_of_its_code(self):
        files = {
            "lib.mh": 'export fn mark(n) { print("eval lib " + n); n }\n@mark("x")\nexport fn f() { }\n',
            "main.mh": 'import lib from "lib"\nprint("main statement")\n',
        }
        self.assertEqual(run_main(files), "eval lib x\nmain statement\n")

    def test_modules_keyed_by_file_not_by_contiguous_segment(self):
        # main's statement comes after `import "lib"` (whose code sits in the
        # middle of main's combined text); main's decorators still belong to main
        files = {
            "lib.mh": 'export fn mark(n) { print("eval lib " + n); n }\n@mark("l")\nfn lf() { }\nprint("lib top")\n',
            "main.mh": (
                'fn mine(n) { print("eval main " + n); n }\n'
                '@mine("m0")\nfn mf() { }\n'
                'import "lib"\n'
                '@mark("m1")\nfn mg() { }\n'
                'print("main top")\n'
            ),
        }
        # (`mark` is lib's, so lib's factory prints "eval lib" for main's second decorator)
        self.assertEqual(
            run_main(files).splitlines(),
            ["eval lib l", "lib top", "eval main m0", "eval lib m1", "main top"],
        )

    def test_another_modules_variable_is_allowed(self):
        files = {
            "lib.mh": 'export let PREFIX = "/api"\nprint("lib ran")\n',
            "main.mh": REFLECT + 'import lib from "lib"\nfn t(x) { x }\n@t(lib.PREFIX)\nfn f() { }\nprint(reflect.signature(f).decorators)\n',
        }
        self.assertEqual(run_main(files), "lib ran\n[/api]\n")

    def test_labels_and_names_that_spell_a_top_level_name_keep_their_meaning(self):
        # M36's exceptions, now with decorators on parameters, fields and variants
        files = {
            "lib.mh": (
                'import reflect from "std:reflect"\n'
                'fn query(name: String) -> String { "q:" + name }\n'
                "fn cwd() { 1 }\n"
                "struct Circle { r: Number }\n"
                'struct S { @query("n") cwd: String, @query("m") name: String }\n'
                'enum Shape { @query("c") Circle { r: Number }, @query("o") Other }\n'
                'export fn describe(@query("p") cwd: String, @query("q") name: String = "x") -> String { cwd + name }\n'
                "export fn info() {\n"
                "    let sig = reflect.signature(describe)\n"
                "    print(sig.params[0].decorators, sig.params[1].decorators, sig.params[0].name)\n"
                "    print(describe(cwd: \"a\", name: \"b\"))\n"
                '    print(S { cwd: "1", name: "2" })\n'
                "    match reflect.schema(Shape) {\n"
                "        some(reflect.Schema.Enum { type: t, doc: d, type_params: tp, variants: vs, decorators: ds }) => {\n"
                "            print(vs[0].name, vs[0].decorators, vs[1].name, vs[0].fields[0].name)\n"
                "        }\n"
                "        _ => { }\n"
                "    }\n"
                "}\n"
            ),
            "main.mh": 'import lib from "lib"\nlib.info()\n',
        }
        self.assertEqual(
            run_main(files).splitlines(),
            ["[q:p] [q:q] cwd", "ab", "S { cwd: 1, name: 2 }", "Circle [q:c] Other r"],
        )

    def test_a_stack_of_decorators_before_export(self):
        files = {
            "lib.mh": 'fn t(x) { x }\n@t(1)\n@t(2) export fn f() { }\n@t(3)\nexport struct S { }\n',
            "main.mh": REFLECT + 'import lib from "lib"\nprint(reflect.signature(lib.f).decorators)\nprint(reflect.schema(lib.S) != none)\n',
        }
        self.assertEqual(run_main(files), "[1, 2]\ntrue\n")


class ForwardReferenceTests(unittest.TestCase):
    """A decorator may name a top-level function or type declared later in
    the module (every closure exists when the phase runs); a top-level `let`
    declared later is still the top-level-variable error."""

    def test_forward_references_everywhere(self):
        src = REFLECT + """
@marker
fn f(@marker p) { 1 }
@Kind
struct S { @marker x }
enum E { @marker A }
struct Holder { }
impl Holder { @marker fn m(self, @marker q) { } }
fn marker() { 0 }
struct Kind { }
let sig = reflect.signature(f)
print(sig.decorators[0] == marker, sig.params[0].decorators[0] == marker)
let hm = reflect.signature(reflect.methods(Holder)[0].function)
print(hm.decorators[0] == marker, hm.params[1].decorators[0] == marker)
match reflect.schema(S) {
    some(reflect.Schema.Struct { type: t, doc: d, type_params: c, fields: fs, decorators: ds }) => {
        print(ds[0] == Kind, fs[0].decorators[0] == marker)
    }
    _ => { }
}
"""
        self.assertEqual(run_source(src), "true true\ntrue true\ntrue true\n")

    def test_a_later_let_is_still_the_top_level_variable_error(self):
        message = compile_error("@x\nfn f() { }\nlet x = 1\n")
        self.assertIn("a decorator can't use the top-level variable 'x'", message)
        self.assertNotIn("Undefined variable", message)

    def test_ordinary_code_keeps_the_declaration_order_rule(self):
        self.assertIn("Undefined variable 'later'", compile_error("print(later())\nfn later() { 1 }\n"))

    def test_the_checker_and_the_lsp_follow_a_forward_reference(self):
        text = "@marker\nfn f() { }\nfn marker() { 0 }\n"
        self.assertEqual(CheckerTests().diagnostics(text), [])
        at = analysis.offset_to_position(text, 1)
        definition = analysis.get_definition(text, at["line"], at["character"], None)
        self.assertEqual(definition["range"]["start"], {"line": 2, "character": 3})
        edits = analysis.get_rename_edits(text, at["line"], at["character"], "tagger", None)["changes"]
        self.assertEqual(_apply(text, next(iter(edits.values()))), text.replace("marker", "tagger"))


class TopLevelVariableTests(unittest.TestCase):
    MESSAGE = "a decorator can't use the top-level variable '%s': it runs before it; use a function or a literal"

    def test_a_decorator_may_not_use_its_own_modules_let(self):
        for src in (
            "let cfg = 1\nfn k(x) { x }\n@k(cfg)\nfn f() { }\n",
            "let cfg = 1\nfn k(x) { x }\n@k(x: cfg)\nfn f() { }\n",
            "let cfg = 1\nfn k(x) { x }\nfn f(@k(cfg) a) { }\n",
            "let cfg = 1\nfn k(x) { x }\n@k(cfg)\nstruct S { }\n",
            "let cfg = 1\nfn k(x) { x }\nstruct S { @k(cfg) a }\n",
            "let cfg = 1\nfn k(x) { x }\nenum E { @k(cfg) A }\n",
            "let cfg = 1\nfn k(x) { x }\nstruct S { }\nimpl S { @k(cfg) fn m(self) { } }\n",
            "let cfg = 1\nfn k(x) { x }\nstruct S { }\nimpl S { fn m(self, @k(cfg) a) { } }\n",
            "let cfg = 1\n@cfg\nfn f() { }\n",
            "let cfg = fn(x) { x }\n@cfg(1)\nfn f() { }\n",
            "let cfg = 1\nfn k(x) { x }\n@k([cfg + 1])\nfn f() { }\n",
        ):
            with self.subTest(src=src):
                self.assertIn(self.MESSAGE % "cfg", compile_error(src))

    def test_the_variable_is_named_as_written_when_the_module_is_imported(self):
        files = {
            "lib.mh": "let cfg = 1\nfn k(x) { x }\n@k(cfg)\nexport fn f() { }\n",
            "main.mh": 'import lib from "lib"\nprint(1)\n',
        }
        message = compile_error(files=files)
        self.assertIn(self.MESSAGE % "cfg", message)
        self.assertNotIn("__mah_m", message)

    def test_functions_types_and_literals_are_always_fine(self):
        src = (
            "struct Route { a }\nenum Kind { A }\nfn k(x) { x }\nlet unrelated = 1\n"
            '@k(k)\n@k(Route)\n@k(Kind.A)\n@k("s")\n@k([1, 2, ["a": 3]])\n@k(-1)\n@k(fn(v) { v })\n@k(Route { a: 1 })\n'
            "fn f() { }\nprint(unrelated)\n"
        )
        self.assertEqual(run_source(src), "1\n")

    def test_a_shadowing_parameter_or_local_is_not_a_top_level_variable(self):
        src = "let cfg = 1\nfn k(x) { x }\n@k(fn(cfg) { cfg })\nfn f() { }\nprint(cfg)\n"
        self.assertEqual(run_source(src), "1\n")

    def test_a_variable_only_used_in_a_function_body_is_fine(self):
        src = "let cfg = 1\nfn f(a) { a + cfg }\nprint(f(1))\n"
        self.assertEqual(run_source(src), "2\n")

    def test_another_modules_variable_in_the_entry_file(self):
        files = {
            "lib.mh": "export let cfg = 5\n",
            "main.mh": 'import lib from "lib"\nfn k(x) { x }\n@k(lib.cfg)\nfn f() { }\nprint("ok")\n',
        }
        self.assertEqual(run_main(files), "ok\n")


class DocCommentTests(unittest.TestCase):
    def test_a_doc_above_decorators_documents_the_declaration(self):
        src = REFLECT + "fn a() { }\nfn b() { }\n## Doc.\n@a\n@b\nfn f() { }\nprint(reflect.signature(f).doc)\n"
        self.assertEqual(run_source(src), "Doc.\n")

    def test_multi_line_docs_and_decorators_on_the_same_line(self):
        src = REFLECT + "fn a() { }\n## One.\n## Two.\n@a @a\nfn f() { }\nprint(reflect.signature(f).doc)\n"
        self.assertEqual(run_source(src), "One.\nTwo.\n")

    def test_a_doc_above_decorated_structs_enums_and_methods(self):
        src = REFLECT + """
fn a() { }
## A struct.
@a
struct S { x }
## An enum.
@a
enum E { X }
struct Holder { }
impl Holder {
    ## A method.
    @a
    fn m(self) { }
}
fn doc(t) {
    match reflect.schema(t) {
        some(reflect.Schema.Struct { type: a, doc: d, type_params: c, fields: fields, decorators: ds }) => { d }
        some(reflect.Schema.Enum { type: a, doc: d, type_params: c, variants: vs, decorators: ds }) => { d }
        _ => { "" }
    }
}
print(doc(S), doc(E), reflect.signature(reflect.methods(Holder)[0].function).doc)
"""
        self.assertEqual(run_source(src), "A struct. An enum. A method.\n")

    def test_inline_decorators_keep_the_doc_of_the_line_above(self):
        src = REFLECT + """
fn a() { }
fn f(
    ## The id.
    @a id: Number,
    ## The name.
    @a @a
    name: String
) { }
struct S {
    ## The x.
    @a x,
    y
}
let sig = reflect.signature(f)
print(sig.params[0].doc, sig.params[1].doc)
match reflect.schema(S) {
    some(reflect.Schema.Struct { type: t, doc: d, type_params: c, fields: fields, decorators: ds }) => { print(fields[0].doc, fields[1].doc == "") }
    _ => { }
}
"""
        self.assertEqual(run_source(src), "The id. The name.\nThe x. true\n")

    def test_a_plain_comment_or_blank_line_between_still_ends_the_doc(self):
        src = REFLECT + "fn a() { }\n## Doc.\n\n@a\nfn f() { }\n## Doc.\n# plain\n@a\nfn g() { }\nprint(reflect.signature(f).doc == \"\", reflect.signature(g).doc == \"\")\n"
        self.assertEqual(run_source(src), "true true\n")


class PreprocessorTests(unittest.TestCase):
    def test_names_in_decorators_are_rewritten_like_any_reference(self):
        from mah.preprocessor import preprocess

        with project(
            {
                "lib.mh": "export fn get(p) { p }\n",
                "main.mh": 'import lib from "lib"\nimport "lib"\n@lib.get("a")\n@get("b")\nfn f() { }\n',
            }
        ) as td:
            pp = preprocess(os.path.join(td, "main.mh"))
        self.assertRegex(pp.text, r'@__mah_m1_get\("a"\)\n@__mah_m1_get\("b"\)\nfn f')


class BytecodeTests(unittest.TestCase):
    SRC = 'fn t(x) { x }\n@t(1)\nfn f(@t(2) a) { }\n@t(3)\nstruct S { @t(4) x }\nenum E { @t(5) A { r } }\n'

    def test_minor_is_16_with_decorators_or_std_reflect(self):
        # M41c: decorators import std:reflect implicitly, and std:reflect (like
        # std:json) uses the 1.16 `hooks.*` natives; a program with neither
        # keeps its old minor.
        self.assertEqual(MINOR, 18)  # M38
        self.assertEqual(decode(compile_bytes(text=self.SRC)).minor, 16)
        self.assertEqual(decode(compile_bytes(text="fn f(a) { a }\nprint(f(1))")).minor, 4)
        self.assertEqual(decode(compile_bytes(text=REFLECT + "print(reflect.type_of(1))")).minor, 16)
        self.assertEqual(decode(compile_bytes(text=REFLECT + "fn t(x) { x }\nprint(reflect.signature(t).decorators)")).minor, 16)

    def test_the_opcode_and_native_are_pinned(self):
        self.assertEqual(OPCODES["decorate"], (0x3D, ("N", "N", "N", "A*")))
        self.assertEqual(NATIVE_ARITIES["reflect.decorators"], 3)

    def test_round_trip_and_determinism(self):
        program = compile_program(text=self.SRC)
        data = encode(program)
        self.assertEqual(decode(data), program)
        self.assertEqual(encode(decode(data)), data)
        self.assertEqual(compile_bytes(text=self.SRC), compile_bytes(text=self.SRC))

    def test_the_instructions(self):
        program = compile_program(text=self.SRC)
        found = [instr.args[:3] for instr in program.code if instr.op == "decorate"]
        kinds = [k for k, _a, _b in found]
        self.assertEqual(kinds, [0, 1, 2, 3, 4])
        # kind 3/4 name the struct/enum's TYPES index; the field/variant index is `b`
        self.assertEqual([b for _k, _a, b in found], [0, 0, 0, 0, 0])

    def test_dis_shows_decorate(self):
        text = disassemble(decode(compile_bytes(text=self.SRC)))
        self.assertIn("decorate", text)
        self.assertIn("target=fn ", text)
        self.assertIn("target=param 0 of fn ", text)
        self.assertIn("target=type S", text)
        self.assertIn("target=field 0 of S", text)
        self.assertIn("target=variant 0 of E", text)

    def test_a_1_14_file_may_not_contain_decorate(self):
        program = compile_program(text="fn t(x) { x }\n@t(1)\nfn f(a) { }\n")
        program.minor = 14
        with self.assertRaisesRegex(MahcFormatError, r"opcode 'decorate' at instruction \d+ requires minor version >= 15"):
            decode(encode(program))

    def test_decode_rejects_bad_targets(self):
        def patched(edit) -> str:
            program = compile_program(text=self.SRC)
            index = next(i for i, instr in enumerate(program.code) if instr.op == "decorate" and instr.args[0] == 3)
            kind, a, b, values = program.code[index].args
            program.code[index] = Instr("decorate", edit(kind, a, b, values))
            with self.assertRaises(MahcFormatError) as cm:
                decode(encode(program))
            return str(cm.exception)

        self.assertRegex(patched(lambda k, a, b, v: (5, a, b, v)), r"'decorate' at instruction \d+: unknown kind 5")
        self.assertRegex(patched(lambda k, a, b, v: (k, a, 9, v)), r"'decorate' at instruction \d+: field index 9 out of range for type \d+")
        self.assertRegex(patched(lambda k, a, b, v: (k, 1, b, v)), r"'decorate' at instruction \d+: type index 1 is not a user type")
        self.assertRegex(patched(lambda k, a, b, v: (k, 99, b, v)), r"'decorate' at instruction \d+: type index 99 is not a user type")
        self.assertRegex(patched(lambda k, a, b, v: (0, 99, b, v)), r"'decorate' at instruction \d+: function index 99 out of range")
        self.assertRegex(patched(lambda k, a, b, v: (1, 1, 9, v)), r"'decorate' at instruction \d+: parameter index 9 out of range for function 1")
        self.assertRegex(patched(lambda k, a, b, v: (4, a, b, v)), r"'decorate' at instruction \d+: type index \d+ is not an enum")
        enum_index = next(
            instr.args[1] for instr in compile_program(text=self.SRC).code if instr.op == "decorate" and instr.args[0] == 4
        )
        self.assertRegex(
            patched(lambda k, a, b, v: (3, enum_index, b, v)), r"'decorate' at instruction \d+: type index \d+ is not a struct"
        )

    def test_decode_accepts_the_valid_shapes(self):
        program = compile_program(text=self.SRC)
        decode(encode(program))  # every kind, well-formed

    def test_a_target_decorated_twice_is_an_internal_error(self):
        src = "fn t(x) { x }\n@t(1)\nfn f() { }\n@t(2)\nfn g() { }\n"
        program = compile_program(text=src)
        indexes = [i for i, instr in enumerate(program.code) if instr.op == "decorate"]
        first = program.code[indexes[0]].args
        second = program.code[indexes[1]].args
        program.code[indexes[1]] = Instr("decorate", (0, first[1], 0, second[3]))
        out, exc = support._run_capturing(encode(program), "")
        self.assertIsNotNone(exc)
        self.assertIn("is decorated twice", str(exc))

    def test_the_reflect_decorators_native(self):
        strings = ["reflect.decorators", "io.print", "hi"]
        program = Program(
            strings=strings,
            constants=[Const(3, 0), Const(5, 2)],
            types=[],
            natives=[NativeRef(0, 3), NativeRef(1, 1)],
            functions=[FunctionDecl(0, 8, 0, None, params=[])],
            code=[
                Instr("loadk", (0, (0, 0))),
                Instr("loadk", (1, (0, 1))),
                Instr("decorate", (0, 0, 0, ((0, 1),))),
                Instr("native", (0, ((0, 0), (0, 0), (0, 0)), (0, 2))),
                Instr("native", (1, ((0, 2),), None)),
                Instr("native", (0, ((0, 0), (0, 0), (0, 0)), (0, 3))),
                Instr("native", (1, ((0, 3),), None)),
                Instr("halt", ()),
            ],
            debug=None,
            minor=15,
            handlers=[],
        )
        out = support._run(encode(program), "")
        self.assertEqual(out, "[hi]\n[hi]\n")
        # (the target's decorators are only read by kind 0 / function 0 here; another is empty)
        program.constants.append(Const(3, 1))
        program.code[3] = Instr("native", (0, ((0, 0), (0, 4), (0, 0)), (0, 2)))
        program.code.insert(0, Instr("loadk", (2, (0, 4))))
        self.assertEqual(support._run(encode(program), ""), "[]\n[hi]\n")

    def test_the_native_needs_minor_15(self):
        program = Program(
            strings=["reflect.decorators"],
            constants=[Const(3, 0)],
            types=[],
            natives=[NativeRef(0, 3)],
            functions=[FunctionDecl(0, 4, 0, None, params=[])],
            code=[
                Instr("loadk", (0, (0, 0))),
                Instr("native", (0, ((0, 0), (0, 0), (0, 0)), (0, 1))),
                Instr("halt", ()),
            ],
            debug=None,
            minor=14,
            handlers=[],
        )
        with self.assertRaisesRegex(MahcFormatError, r"requires minor version >= 15"):
            decode(encode(program))


class CheckerTests(unittest.TestCase):
    def diagnostics(self, src: str):
        from mah.bytecode.lower import line_col
        from mah.compiler.typecheck import Checker

        pp, program, resolver = _front_end(src)
        found = Checker(resolver).check(program)
        return [(d.kind, d.message, line_col(pp.text, d.position), pp.text[d.position : d.position + 1]) for d in found]

    def test_a_well_typed_decorator_has_no_diagnostic(self):
        src = 'fn tag(t: String) -> String { t }\n@tag("x")\nfn f(@tag("y") a: Number) { a }\nstruct S { @tag("z") x: Number }\nenum E { @tag("w") A }\nimpl S { @tag("m") fn m(self, @tag("p") q) { } }\n'
        self.assertEqual(self.diagnostics(src), [])

    def test_a_wrong_argument_type_is_reported_at_the_argument(self):
        src = (
            "fn tag(t: String) -> String { t }\n"
            "@tag(1)\nfn f(@tag(2) a: Number) { a }\n"
            "struct S { @tag(3) x: Number }\n"
            "enum E { @tag(4) A }\n"
            "impl S { @tag(5) fn m(self, @tag(6) z: Number) { z } }\n"
        )
        got = self.diagnostics(src)
        self.assertEqual(
            [(line, text) for _k, _m, (line, _c), text in got],
            [(2, "1"), (3, "2"), (4, "3"), (5, "4"), (6, "5"), (6, "6")],
        )
        self.assertTrue(all("expected String, found Number" in m for _k, m, _p, _t in got))

    def test_parameters_are_not_in_scope_for_their_decorators(self):
        with self.assertRaises(Exception) as cm:
            self.diagnostics("fn tag(t) { t }\nfn f(a, @tag(a) b) { }\n")
        self.assertIn("Undefined variable 'a'", str(cm.exception))

    def test_no_constraint_on_the_resulting_type(self):
        src = 'fn a() { 1 }\nfn b() { "x" }\n@a()\n@b()\n@a\n@[1]\nfn f() { }\n'.replace("@[1]\n", "")
        self.assertEqual(self.diagnostics(src), [])

    def test_the_call_arity_is_checked(self):
        got = self.diagnostics("fn tag(t: String) { t }\n@tag(\"a\", \"b\")\nfn f() { }\n")
        self.assertTrue(got)


class FormatterTests(unittest.TestCase):
    def test_canonical_layout_and_idempotence(self):
        src = (
            '## Doc.\n@get( "/x" ,a:1)   @lib.q\n\n@Route.make(...xs)\nexport   fn f(  @a   @b( 1 )x:Number , y=2 ) { 1 }\n'
            "@m struct S { @a\n x: Number, @b   @c(1) y }\n"
            'enum E { @t("x")   A { r: Number }, @u B }\n'
            "impl S {\n@m fn hi(self, @p   z) { 1 }\n}\n"
        )
        once = format_source(src)
        self.assertEqual(
            once,
            '## Doc.\n@get("/x", a: 1)\n@lib.q\n@Route.make(...xs)\nexport fn f(@a @b(1) x: Number, y = 2) { 1 }\n'
            "@m\nstruct S { @a x: Number, @b @c(1) y }\n"
            'enum E { @t("x") A { r: Number }, @u B }\n'
            "impl S {\n    @m\n    fn hi(self, @p z) { 1 }\n}\n",
        )
        self.assertEqual(format_source(once), once)


class LspTests(unittest.TestCase):
    def locate(self, text: str, needle: str, k: int = 0, delta: int = 0) -> dict:
        offset = -1
        for _ in range(k + 1):
            offset = text.index(needle, offset + 1)
        return analysis.offset_to_position(text, offset + delta)

    def test_hover_definition_and_rename_inside_a_local_decorator(self):
        text = 'fn tag(x) { x }\n@tag("a")\nfn f(@tag("b") p) { p }\nstruct S { @tag("c") x }\n'
        with project({"main.mh": text}) as td:
            path = os.path.join(td, "main.mh")
            at = self.locate(text, "@tag", 0, 1)
            definition = analysis.get_definition(text, at["line"], at["character"], path)
            self.assertEqual(definition["range"]["start"], {"line": 0, "character": 3})
            hover = analysis.get_hover(text, at["line"], at["character"], path)
            self.assertIn("tag", hover["contents"]["value"])
            for k in range(3):
                at = self.locate(text, "@tag", k, 1)
                edits = analysis.get_rename_edits(text, at["line"], at["character"], "label", path)["changes"][path]
                self.assertEqual(len(edits), 4)  # the declaration and the three uses
            renamed = _apply(text, edits)
            self.assertEqual(renamed, text.replace("tag", "label"))

    def test_namespaced_decorator_cross_file(self):
        lib = "export fn factory(p) { p }\n"
        main = 'import lib from "lib"\n@lib.factory("a")\nfn f(@lib.factory("b") p) { p }\nstruct S { @lib.factory("c") x }\n'
        with project({"lib.mh": lib, "main.mh": main}) as td:
            main_path = os.path.join(td, "main.mh")
            lib_path = os.path.join(td, "lib.mh")
            # go to definition from inside `@lib.factory` lands in lib.mh
            at = self.locate(main, "factory", 0)
            definition = analysis.get_definition(main, at["line"], at["character"], main_path)
            self.assertEqual(os.path.realpath(definition["path"]), os.path.realpath(lib_path))
            self.assertEqual(definition["range"]["start"], {"line": 0, "character": 10})
            hover = analysis.get_hover(main, at["line"], at["character"], main_path)
            self.assertIn("factory", hover["contents"]["value"])
            # rename from the use, across files
            result = analysis.get_rename_edits(main, at["line"], at["character"], "make", main_path)["changes"]
            by_name = {os.path.basename(p): edits for p, edits in result.items()}
            self.assertEqual(sorted(by_name), ["lib.mh", "main.mh"])
            self.assertEqual(_apply(lib, by_name["lib.mh"]), "export fn make(p) { p }\n")
            self.assertEqual(_apply(main, by_name["main.mh"]), main.replace("factory", "make"))
            # ... and from the declaration in lib.mh
            at = self.locate(lib, "factory")
            result = analysis.get_rename_edits(lib, at["line"], at["character"], "make", lib_path)["changes"]
            by_name = {os.path.basename(p): edits for p, edits in result.items()}
            self.assertEqual(_apply(main, by_name["main.mh"]), main.replace("factory", "make"))

    def test_completion_inside_a_decorator_matches_an_expression(self):
        # (the loop makes both documents include the prelude, as the hidden
        # std:reflect import of the decorated one does)
        head = 'import lib from "lib"\nfor let _i in [1] { }\nfn local(x) { x }\n'
        with project({"lib.mh": "export fn factory(p) { p }\n", "c.mh": head}) as td:
            path = os.path.join(td, "c.mh")

            def labels(text: str, marker: str) -> list:
                at = analysis.offset_to_position(text, text.index(marker) + len(marker))
                return sorted(
                    i["label"]
                    for i in analysis.get_completions(text, path, at["line"], at["character"])
                    if i["label"] not in ("a", "f")  # the documents' own declarations
                )

            for decorator, expression, marker, expression_marker in (
                (head + "@local\nfn f() { }\n", head + "let a = local\n", "@local", "= local"),
                (head + "@lib.\nfn f() { }\n", head + "let a = lib.\n", "@lib.", "lib."),
            ):
                with self.subTest(marker):
                    self.assertEqual(labels(decorator, marker), labels(expression, expression_marker))
            self.assertIn("local", labels(head + "@local\nfn f() { }\n", "@local"))
            self.assertEqual(labels(head + "@lib.\nfn f() { }\n", "@lib."), ["factory"])

    def test_diagnostics_for_misplaced_decorators_and_top_level_variables(self):
        text = "fn f() { @a fn g() { } }\n"
        messages = [d["message"] for d in analysis.get_diagnostics(text)]
        self.assertTrue(any(MISPLACED in m for m in messages), messages)
        text = "let cfg = 1\nfn k(x) { x }\n@k(cfg)\nfn f() { }\n"
        messages = [d["message"] for d in analysis.get_diagnostics(text)]
        self.assertTrue(any("can't use the top-level variable 'cfg'" in m for m in messages), messages)


def _apply(text: str, edits: list) -> str:
    spans = []
    for edit in edits:
        start = analysis.position_to_offset(text, edit["range"]["start"]["line"], edit["range"]["start"]["character"])
        end = analysis.position_to_offset(text, edit["range"]["end"]["line"], edit["range"]["end"]["character"])
        spans.append((start, end, edit["newText"]))
    for start, end, new in sorted(spans, reverse=True):
        text = text[:start] + new + text[end:]
    return text


if __name__ == "__main__":
    unittest.main()
