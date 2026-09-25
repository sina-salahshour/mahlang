"""M21: type annotation syntax (parsed and name-checked, otherwise ignored)
and same-scope `let` shadowing. See docs/TYPES.md.

Run with: python -m unittest tests.test_type_syntax -v
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mah.compiler.ast_nodes import FnType, ForStmt, ImplDecl, LetStmt, NamedType, NumberLit, TraitDecl
from mah.compiler.lexer import Lexer, TokenType
from mah.lsp import analysis
from mah.preprocessor import PRELUDE_TRIGGERS
from tests.support import compile_bytes, parse_source, run_file, run_source


def _parse_ok(src: str) -> list:
    program, parser = parse_source(src)
    if parser.errors:
        raise AssertionError(parser.errors)
    return program


def _shape(texpr):
    """A NamedType/FnType as a comparable plain value, positions dropped."""
    if texpr is None:
        return None
    if isinstance(texpr, FnType):
        return ("fn", [_shape(p) for p in texpr.params], _shape(texpr.ret))
    return (texpr.name, [_shape(a) for a in texpr.args])


class LexerTests(unittest.TestCase):
    def test_arrow_is_one_token(self):
        lexer = Lexer("a -> b")
        lexer.get_next_token()
        tok = lexer.get_next_token()
        self.assertEqual((tok.type, tok.literal), (TokenType.ARROW, "->"))


class ParsingTests(unittest.TestCase):
    def test_param_and_return_annotations(self):
        [stmt] = _parse_ok("fn add(a: Number, b: Number = 1) -> Number { a + b }")
        fn = stmt.value
        self.assertEqual([_shape(t) for t in fn.param_types], [("Number", []), ("Number", [])])
        self.assertIsInstance(fn.defaults[1], NumberLit)
        self.assertEqual(_shape(fn.return_type), ("Number", []))

    def test_unannotated_params_get_none(self):
        [stmt] = _parse_ok("fn f(a, b: String) { }")
        self.assertEqual([_shape(t) for t in stmt.value.param_types], [None, ("String", [])])
        self.assertIsNone(stmt.value.return_type)

    def test_type_params_with_bounds_and_defaults(self):
        [stmt] = _parse_ok(
            "trait Other { }\n"
            "fn first<T: Printable + Other, U = Vector<T>>(v: Vector<T>) -> T { v[0] }"
        )[1:]
        t, u = stmt.value.type_params
        self.assertEqual((t.name, [_shape(b) for b in t.bounds], t.default), ("T", [("Printable", []), ("Other", [])], None))
        self.assertEqual((u.name, u.bounds, _shape(u.default)), ("U", [], ("Vector", [("T", [])])))

    def test_fn_types(self):
        program = _parse_ok("let f: fn(Number, String) -> Bool = fn(a, b) { true }\nlet g: fn() = fn() { }")
        self.assertEqual(_shape(program[0].type_ann), ("fn", [("Number", []), ("String", [])], ("Bool", [])))
        self.assertEqual(_shape(program[1].type_ann), ("fn", [], None))

    def test_nested_type_args_close_with_two_gt_tokens(self):
        [stmt] = _parse_ok("let v: Vector<Vector<Number>> = []")
        self.assertEqual(_shape(stmt.type_ann), ("Vector", [("Vector", [("Number", [])])]))

    def test_struct_type_params_and_field_types(self):
        [decl] = _parse_ok("struct Pair<A, B> { left: A, right }")
        self.assertEqual([tp.name for tp in decl.type_params], ["A", "B"])
        self.assertEqual([_shape(t) for t in decl.field_types], [("A", []), None])

    def test_enum_variant_field_types(self):
        [decl] = _parse_ok("enum Tree<T> { Leaf, Node { value: T, next } }")
        self.assertEqual([tp.name for tp in decl.type_params], ["T"])
        self.assertEqual(
            [[_shape(t) for t in types] for types in decl.variant_field_types], [[], [("T", []), None]]
        )

    def test_generic_impl_header(self):
        program = _parse_ok("impl<T> Iterable<T> for Vector<T> { fn iter(self) { none } }")
        impl = program[0]
        self.assertIsInstance(impl, ImplDecl)
        self.assertEqual([tp.name for tp in impl.type_params], ["T"])
        self.assertEqual((impl.trait_name, [_shape(a) for a in impl.trait_args]), ("Iterable", [("T", [])]))
        self.assertEqual((impl.type_name, [_shape(a) for a in impl.type_args]), ("Vector", [("T", [])]))

    def test_bodyless_trait_method_return_type(self):
        [trait] = _parse_ok("trait Iter<T> { fn next(self) -> Option<T>\n fn size(self) -> Number }")
        self.assertIsInstance(trait, TraitDecl)
        method = trait.methods[0]
        self.assertIsNone(method.fn)
        self.assertEqual(_shape(method.return_type), ("Option", [("T", [])]))

    def test_method_with_body_shares_annotations_with_its_fn(self):
        [impl] = _parse_ok("struct P { x }\nimpl P { fn get<T>(self, d: T) -> Number { self.x } }")[1:]
        method = impl.methods[0]
        self.assertIs(method.fn.return_type, method.return_type)
        self.assertEqual([_shape(t) for t in method.fn.param_types], [None, ("T", [])])

    def test_for_binding_annotations(self):
        [stmt] = _parse_ok("for let v: Number, let i: Number in 0..3 { }")
        loop = stmt.value
        self.assertIsInstance(loop, ForStmt)
        self.assertEqual((_shape(loop.value_type), _shape(loop.index_type)), (("Number", []), ("Number", [])))

    def test_let_annotation(self):
        [stmt] = _parse_ok("let name: String = \"mah\"")
        self.assertIsInstance(stmt, LetStmt)
        self.assertEqual(_shape(stmt.type_ann), ("String", []))

    def test_self_cannot_be_annotated(self):
        _program, parser = parse_source("struct P { x }\nimpl P { fn m(self: Number) { } }")
        self.assertTrue(any("'self' can't have a type annotation" in message for message, _pos in parser.errors))


class ValidationTests(unittest.TestCase):
    def _fails(self, src: str, message: str):
        with self.assertRaises(Exception) as ctx:
            run_source(src)
        self.assertIn(message, str(ctx.exception))

    def test_unknown_type(self):
        self._fails("let x: Foo = 1", "Unknown type 'Foo'")

    def test_too_many_type_args(self):
        self._fails("let x: Vector<Number, Number> = []", "Type 'Vector' takes 1 type argument(s), got 2")

    def test_missing_type_args(self):
        self._fails("let x: Vector = []", "Type 'Vector' takes 1 type argument(s), got 0")

    def test_function_is_not_a_type(self):
        self._fails("let x: Function = fn() {}", "'Function' can't be used as a type")

    def test_self_outside_trait_or_impl(self):
        self._fails("fn f(x: Self) { }", "'Self' is only a type inside a trait or impl")

    def test_type_param_shadowing_a_type(self):
        self._fails("fn f<Number>(x: Number) { }", "Type parameter 'Number' shadows a type")

    def test_duplicate_type_param(self):
        self._fails("fn f<T, T>(x: T) { }", "Duplicate type parameter 'T'")

    def test_bound_must_be_a_trait(self):
        self._fails("struct P { x }\nfn f<T: P>(x: T) { }", "Bound 'P' on 'T' is not a trait")

    def test_type_param_default_ordering(self):
        self._fails("fn f<A = Number, B>(x: A) { }", "needs a default")

    def test_undeclared_type_param(self):
        self._fails("fn f(x: T) { }", "Unknown type 'T'")

    def test_type_params_are_scoped_to_their_fn(self):
        self.assertEqual(run_source("fn outer<T>(x: T) { let g = fn(y: T) { y }; g(x) }\nprint(outer(3))"), "3\n")
        self._fails("fn a<T>(x: T) { }\nfn b(y: T) { }", "Unknown type 'T'")

    def test_types_declared_after_use(self):
        self.assertEqual(run_source("fn f(p: Point) { p.x }\nstruct Point { x }\nprint(f(Point { x: 5 }))"), "5\n")

    def test_inherent_impl_must_use_its_own_params(self):
        self._fails(
            "struct Pair<A, B> { a, b }\nimpl<A, B> Pair<B, A> { }", "An inherent impl must be for"
        )

    def test_inherent_impl_without_args_on_generic_struct(self):
        src = "struct Pair<A, B> { a: A, b: B }\nimpl Pair { fn left(self) { self.a } }\nprint(Pair { a: 1, b: 2 }.left())"
        self.assertEqual(run_source(src), "1\n")

    def test_self_inside_trait(self):
        self.assertEqual(run_source("trait Show { fn show(self) -> Self }\nprint(1)"), "1\n")

    def test_impl_header_args_may_be_omitted(self):
        # Every impl written before M21 omits them, e.g. for a generic trait.
        src = "struct P { x }\nimpl Iterable for P { fn iter(self) { [self.x].iter() } }\nfor let v in (P { x: 4 }) { print(v) }"
        self.assertEqual(run_source(src), "4\n")

    def test_impl_header_args_are_checked_when_written(self):
        self._fails("struct P { x }\nimpl Iterable<Number, Number> for P { fn iter(self) { [] } }",
                    "Type 'Iterable' takes 1 type argument(s), got 2")


_ANNOTATED = """\
fn add(a: Number, b: Number = 1) -> Number { a + b }
let x: Number = add(2)
struct Pair<A, B> { left: A, right: B }
let p: Pair<Number, String> = Pair { left: 1, right: "a" }
enum Tree<T> { Leaf, Node { value: T } }
fn first<T>(v: Vector<T>) -> T { v[0] }
trait Show<T> { fn show(self, extra: T) -> String }
impl Show<Number> for Pair { fn show(self, extra: Number) -> String { "" + self.left + extra } }
for let v: Number, let i: Number in [1, 2] { print(first([v]), i) }
let f: fn(Number) -> String = fn(n: Number) -> String { "" + n }
print(x, p.show(3), f(4))
"""


# `_ANNOTATED` with every annotation, `<...>` list and `-> T` removed.
_PLAIN = """\
fn add(a, b = 1) { a + b }
let x = add(2)
struct Pair { left, right }
let p = Pair { left: 1, right: "a" }
enum Tree { Leaf, Node { value } }
fn first(v) { v[0] }
trait Show { fn show(self, extra) }
impl Show for Pair { fn show(self, extra) { "" + self.left + extra } }
for let v, let i in [1, 2] { print(first([v]), i) }
let f = fn(n) { "" + n }
print(x, p.show(3), f(4))
"""


class RuntimeUnchangedTests(unittest.TestCase):
    def test_annotations_compile_to_identical_bytecode(self):
        self.assertEqual(
            compile_bytes(text=_ANNOTATED, target="release"),
            compile_bytes(text=_PLAIN, target="release"),
        )

    def test_annotated_program_runs(self):
        self.assertEqual(run_source(_ANNOTATED), "1 0\n2 1\n3 13 4\n")

    def test_annotations_are_not_type_checked_yet(self):
        # M21 only checks type names. M22's checker will turn this into a
        # (loose-mode) warning.
        self.assertEqual(run_source('let x: Number = "not checked yet"\nprint(x)'), "not checked yet\n")


class ShadowingTests(unittest.TestCase):
    def test_let_shadows_in_the_same_scope(self):
        self.assertEqual(run_source('let x = 1; let x = "s" + x; print(x)'), "s1\n")

    def test_closure_keeps_the_old_variable(self):
        self.assertEqual(run_source("let x = 1; let get = fn() { x }; let x = 2; print(get(), x)"), "1 2\n")

    def test_shadowing_inside_a_function(self):
        self.assertEqual(run_source("fn f() { let x = 1; let x = x + 1; x }\nprint(f())"), "2\n")

    def test_let_may_shadow_a_fn(self):
        self.assertEqual(run_source("fn x() { 1 }\nlet x = 2\nprint(x)"), "2\n")

    def test_fn_cannot_redeclare(self):
        for src in ("fn g() { 1 }\nfn g() { 2 }", "let x = 1\nfn x() { 2 }"):
            with self.assertRaisesRegex(Exception, "already defined"):
                run_source(src)

    def test_params_and_bindings_cannot_repeat(self):
        for src in ("fn f(a, a) { }", "for let x, let x in [1] { }", "struct P { a, b }\nmatch (P { a: 1, b: 2 }) { P { a: x, b: x } => { } }"):
            with self.assertRaisesRegex(Exception, "already defined"):
                run_source(src)


def _pos(text: str, offset: int) -> dict:
    return analysis.offset_to_position(text, offset)


def _apply_rename(text: str, offset: int, new_name: str) -> str:
    p = _pos(text, offset)
    result = analysis.get_rename_edits(text, p["line"], p["character"], new_name, None)
    [edits] = result["changes"].values()
    spans = []
    for edit in edits:
        start = analysis.position_to_offset(text, edit["range"]["start"]["line"], edit["range"]["start"]["character"])
        end = analysis.position_to_offset(text, edit["range"]["end"]["line"], edit["range"]["end"]["character"])
        spans.append((start, end, edit["newText"]))
    for start, end, new_text in sorted(spans, reverse=True):
        text = text[:start] + new_text + text[end:]
    return text


class LspTests(unittest.TestCase):
    def test_rename_struct_covers_annotations(self):
        text = "struct Point { x }\nfn f(p: Point) -> Point { p }\n"
        self.assertEqual(
            _apply_rename(text, text.index("Point"), "Spot"),
            "struct Spot { x }\nfn f(p: Spot) -> Spot { p }\n",
        )

    def test_definition_from_an_annotation(self):
        text = "struct Point { x }\nfn f(p: Point) { p }\n"
        p = _pos(text, text.index("Point", 20))
        location = analysis.get_definition(text, p["line"], p["character"], None)
        start = location["range"]["start"]
        self.assertEqual((start["line"], start["character"]), (0, 7))

    def test_rename_a_shadowing_let_touches_only_its_own_uses(self):
        text = "let x = 1\nlet x = x + 1\nprint(x)\n"
        second = text.index("x", text.index("\n"))
        self.assertEqual(_apply_rename(text, second, "y"), "let x = 1\nlet y = x + 1\nprint(y)\n")

    def test_hover_and_diagnostics_on_annotated_code(self):
        text = _ANNOTATED
        self.assertEqual(analysis.get_diagnostics(text, None), [])
        p = _pos(text, text.index("first"))
        analysis.get_hover(text, p["line"], p["character"], None)  # must not raise


class PreprocessorTests(unittest.TestCase):
    def test_prelude_triggers_unchanged(self):
        self.assertEqual(
            sorted(PRELUDE_TRIGGERS),
            ['Filtered', 'FilteredIterator', 'FromRange', 'FromRangeIterator', 'Iterable', 'Iterator',
             'MapEntry', 'Mapped', 'MappedIterator', 'Range', 'RangeIterator', 'Skipped',
             'SkippedIterator', 'StringIterator', 'Taken', 'TakenIterator', 'ToRange', 'VectorIterator',
             '__Iter', '__NoInitial', 'call', 'call_reduce', 'collect', 'entries', 'filter', 'iter',
             'map', 'next', 'reduce', 'require_number', 'skip', 'take', 'to_string'],
        )

    def test_generic_exported_fn_imports(self):
        with tempfile.TemporaryDirectory() as directory:
            with open(os.path.join(directory, "lib.mh"), "w", encoding="utf-8") as f:
                f.write("export fn first<T>(v: Vector<T>) -> T { v[0] }\nexport let answer: Number = 42\n")
            main = os.path.join(directory, "main.mh")
            with open(main, "w", encoding="utf-8") as f:
                f.write('import "lib.mh"\nimport m from "lib"\nprint(first([7]), m.first([8]), answer, m.answer)\n')
            self.assertEqual(run_file(main), "7 8 42 42\n")


if __name__ == "__main__":
    unittest.main()
