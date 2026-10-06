"""M41s -- module-scoped type names (docs/V2_DESIGN.md's M41s milestone).

An imported module's top-level `struct`/`enum`/`trait` names are renamed
like its `fn`/`let` names (`__mah_m{idx}_{Name}`), exported with `export
struct ...` and reached as `lib.Point` (namespaced import) or a bare `Point`
(flat import). Runtime-visible type names show the declared name
(docs/MAHC_FORMAT.md #4.3) on both VMs, while dispatch keeps the full name.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mah.compiler.driver import compile_to_bytes  # noqa: E402
from mah.lsp import analysis  # noqa: E402
from mah.preprocessor import demangle_message  # noqa: E402
from mah.rust_vm import RustVmNotFound, find_vm  # noqa: E402
from tests import support  # noqa: E402
from tests.support import run_file, run_source  # noqa: E402


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


def compile_error(files: dict) -> str:
    with project(files) as td:
        try:
            compile_to_bytes(path=os.path.join(td, "main.mh"))
        except Exception as exc:  # noqa: BLE001
            return demangle_message(str(exc))
    raise AssertionError("expected a compile error")


def run_on_both(files: dict) -> list:
    """`(stdout, error text or None)` on the Python VM and, when it's built,
    on the Rust VM."""
    with project(files) as td:
        data = compile_to_bytes(path=os.path.join(td, "main.mh"))
    results = []
    saved = os.environ.get("MAH_TEST_VM")
    vms = ["python"]
    try:
        find_vm()
        vms.append("rust")
    except RustVmNotFound:
        pass
    try:
        for vm in vms:
            os.environ["MAH_TEST_VM"] = vm
            out, exc = support._run_capturing(data, "")
            results.append((out, None if exc is None else str(exc)))
    finally:
        if saved is None:
            os.environ.pop("MAH_TEST_VM", None)
        else:
            os.environ["MAH_TEST_VM"] = saved
    return results


LIB_POINT = (
    "export struct Point { x: Number }\n"
    "export enum Shape { Circle { r: Number }, Empty }\n"
    "export fn make(x: Number) -> Point { Point { x: x } }\n"
    "export fn shape(r: Number) -> Shape { Shape.Circle { r: r } }\n"
    "struct Hidden { a: Number }\n"
    "export fn hidden() { Hidden { a: 1 } }\n"
)


class RegressionTests(unittest.TestCase):
    def test_user_types_named_like_reflect_types_next_to_std_json(self):
        for name in ("Field", "Param", "Method", "Signature", "Schema", "Variant", "TypeRef", "ReflectError"):
            with self.subTest(name=name):
                src = f'import json from "std:json"\nstruct {name} {{ name: String }}\nprint({name} {{ name: "a" }}.name)\n'
                self.assertEqual(run_source(src), "a\n")

    def test_user_types_named_like_reflect_types_next_to_std_reflect(self):
        for name in ("Param", "Method", "TypeRef", "Field"):
            with self.subTest(name=name):
                src = f'import reflect from "std:reflect"\nstruct {name} {{ name: String }}\nprint({name} {{ name: "a" }}.name)\n'
                self.assertEqual(run_source(src), "a\n")

    def test_user_struct_still_decodable_with_json(self):
        src = (
            'import json from "std:json"\nstruct Field { name: String }\n'
            'print(json.decode(Field, json.parse("{\\"name\\": \\"z\\"}")))\n'
        )
        self.assertEqual(run_source(src), "Field { name: z }\n")


class CoexistingNamesTests(unittest.TestCase):
    FILES = {
        "a.mh": (
            "struct Request { a: Number }\n"
            "impl Request { fn who(self) { \"a\" } }\n"
            "export fn make() { Request { a: 1 } }\n"
            "export fn read(r) { r.a }\n"
            "export fn who(r) { r.who() }\n"
        ),
        "b.mh": (
            "struct Request { b: Number }\n"
            "impl Request { fn who(self) { \"b\" } }\n"
            "export fn make() { Request { b: 2 } }\n"
            "export fn read(r) { r.b }\n"
            "export fn who(r) { r.who() }\n"
        ),
        "main.mh": (
            'import a from "./a"\nimport b from "./b"\n'
            "struct Request { c: Number }\n"
            "impl Request { fn who(self) { \"c\" } }\n"
            "let ra = a.make()\nlet rb = b.make()\nlet rc = Request { c: 3 }\n"
            "print(ra)\nprint(rb)\nprint(rc)\n"
            "print(a.read(ra), b.read(rb), rc.c)\n"
            "print(a.who(ra), b.who(rb), rc.who())\n"
            "print(ra.who(), rb.who())\n"
        ),
    }

    def test_all_three_coexist(self):
        self.assertEqual(
            run_main(self.FILES),
            "Request { a: 1 }\nRequest { b: 2 }\nRequest { c: 3 }\n1 2 3\na b c\na b\n",
        )


OOPS = "export struct Oops { why: String }\nimpl Error for Oops { fn message(self) { self.why } }\n"


class ExportedTypeTests(unittest.TestCase):
    def test_namespaced_uses(self):
        main = (
            'import lib from "./lib"\nimport reflect from "std:reflect"\n'
            "let p: lib.Point = lib.Point { x: 1 }\n"
            "print(p, lib.make(2))\n"
            "let v: Vector<lib.Point> = [lib.make(3)]\nprint(v)\n"
            "fn radius(s: lib.Shape) {\n"
            "    match s {\n"
            "        lib.Shape.Circle { r } => { r }\n"
            "        lib.Shape.Empty => { 0 }\n"
            "    }\n}\n"
            "print(radius(lib.shape(5)), radius(lib.Shape.Empty))\n"
            "impl Printable for lib.Point { fn to_string(self) -> String { \"P\" + self.x } }\n"
            "print(p)\n"
            "print(reflect.schema(lib.Point).unwrap().fields[0].name)\n"
        )
        self.assertEqual(
            run_main({"lib.mh": LIB_POINT, "main.mh": main}),
            "P1 P2\n[P3]\n5 0\nP1\nx\n",
        )

    def test_flat_import_uses_bare_names(self):
        main = (
            'import "./lib"\n'
            "let p: Point = Point { x: 4 }\nprint(p, make(5))\n"
            "match shape(2) { Shape.Circle { r } => { print(r) }\n Shape.Empty => { print(\"e\") } }\n"
        )
        self.assertEqual(run_main({"lib.mh": LIB_POINT, "main.mh": main}), "Point { x: 4 } Point { x: 5 }\n2\n")

    def test_flat_import_can_be_shadowed_by_a_namespace_of_another_module(self):
        other = "export struct Point { y: Number }\n"
        main = (
            'import lib from "./lib"\nimport other from "./other"\n'
            "print(lib.Point { x: 1 }, other.Point { y: 2 })\n"
        )
        self.assertEqual(run_main({"lib.mh": LIB_POINT, "other.mh": other, "main.mh": main}), "Point { x: 1 } Point { y: 2 }\n")

    def test_export_of_a_type_declared_elsewhere_in_the_file(self):
        lib = "struct Box { v: Number }\nexport Box\nexport fn box(v) { Box { v: v } }\n"
        main = 'import lib from "./lib"\nprint(lib.box(1), lib.Box { v: 2 })\n'
        self.assertEqual(run_main({"lib.mh": lib, "main.mh": main}), "Box { v: 1 } Box { v: 2 }\n")

    def test_variant_named_like_a_type_is_not_renamed(self):
        lib = (
            "export struct Param { name: String }\n"
            "export enum TypeRef { Param { name: String }, Other }\n"
            "export fn a() { TypeRef.Param { name: \"v\" } }\n"
            "export fn b() { Param { name: \"s\" } }\n"
        )
        main = 'import lib from "./lib"\nprint(lib.a())\nprint(lib.b())\nprint(lib.TypeRef.Param { name: "x" })\n'
        self.assertEqual(
            run_main({"lib.mh": lib, "main.mh": main}),
            "TypeRef.Param { name: v }\nParam { name: s }\nTypeRef.Param { name: x }\n",
        )

    def test_field_named_like_a_type_is_a_label(self):
        lib = "export struct Wrap { Point: Number }\nstruct Point { x: Number }\nexport fn w() { Wrap { Point: 1 } }\n"
        main = 'import lib from "./lib"\nprint(lib.w().Point)\n'
        self.assertEqual(run_main({"lib.mh": lib, "main.mh": main}), "1\n")

    def test_impl_on_an_imported_type(self):
        main = (
            'import lib from "./lib"\n'
            "impl lib.Point { fn double(self) { self.x * 2 } }\n"
            "print(lib.make(4).double())\n"
        )
        self.assertEqual(run_main({"lib.mh": LIB_POINT, "main.mh": main}), "8\n")

    def test_catch_arm_naming_an_imported_type(self):
        lib = OOPS + "export fn boom() { throw Oops { why: \"w\" } }\n"
        main = (
            'import lib from "./lib"\n'
            "print(try { lib.boom() } catch { e: lib.Oops => { e.why } })\n"
            "print(try { lib.boom() } catch { lib.Oops { why } => { why } })\n"
        )
        self.assertEqual(run_main({"lib.mh": lib, "main.mh": main}), "w\nw\n")

    def test_generic_and_type_value_positions(self):
        main = (
            'import lib from "./lib"\nimport json from "std:json"\n'
            'print(json.decode(lib.Point, json.parse("{\\"x\\": 7}")))\n'
            "print(lib.Point == lib.Point)\n"
        )
        self.assertEqual(run_main({"lib.mh": LIB_POINT, "main.mh": main}), "Point { x: 7 }\ntrue\n")

    def test_flat_imported_type_inside_a_function_type(self):
        # `fn(Point)` in an annotation is a type, not a closure with a
        # parameter named Point: the name must still be renamed, there and
        # in the body after it.
        main = (
            'import "./lib"\n'
            "fn each(f: fn(Point) -> Vector<Point>, n: Number) { print(f(Point { x: n })) }\n"
            "each(fn(p) { [p] }, 3)\n"
            "fn maker() -> fn(Number) -> Point { fn(n) { Point { x: n } } }\n"
            "print(maker()(4))\n"
            "let pick: Vector<fn(Point)> = [fn(p) { print(p.x) }]\n"
            "pick[0](Point { x: 5 })\n"
        )
        self.assertEqual(
            run_main({"lib.mh": LIB_POINT, "main.mh": main}),
            "[Point { x: 3 }]\nPoint { x: 4 }\n5\n",
        )

    def test_parameter_named_like_a_flat_imported_type_still_shadows_it(self):
        main = 'import "./lib"\nfn f(Point: Number) -> Number { Point + 1 }\nprint(f(Point: 1), (fn(Point) { Point })(2))\n'
        self.assertEqual(run_main({"lib.mh": LIB_POINT, "main.mh": main}), "2 2\n")


class NotExportedTests(unittest.TestCase):
    def test_struct_literal(self):
        error = compile_error(
            {"lib.mh": LIB_POINT, "main.mh": 'import lib from "./lib"\nlet h = lib.Hidden { a: 1 }\n'}
        )
        self.assertIn("Undefined struct type 'Hidden (not exported)' at position", error)

    def test_annotation(self):
        error = compile_error(
            {"lib.mh": LIB_POINT, "main.mh": 'import lib from "./lib"\nlet h: lib.Hidden = lib.hidden()\n'}
        )
        self.assertIn("Unknown type 'Hidden (not exported)' at position", error)

    def test_flat_import_does_not_bring_private_types(self):
        error = compile_error({"lib.mh": LIB_POINT, "main.mh": 'import "./lib"\nlet h = Hidden { a: 1 }\n'})
        self.assertIn("Undefined struct type 'Hidden'", error)

    def test_a_private_type_still_works_inside_its_module(self):
        main = 'import lib from "./lib"\nprint(lib.hidden())\n'
        self.assertEqual(run_main({"lib.mh": LIB_POINT, "main.mh": main}), "Hidden { a: 1 }\n")


class DisplayNameTests(unittest.TestCase):
    def test_type_value_prints_declared_name(self):
        main = 'import lib from "./lib"\nprint(lib.Point)\nprint(lib.Shape)\nprint(lib.Shape.Empty)\n'
        for out, err in run_on_both({"lib.mh": LIB_POINT, "main.mh": main}):
            self.assertIsNone(err)
            self.assertEqual(out, "Point\nShape\nShape.Empty\n")

    def test_no_such_field_error_names_the_declared_type(self):
        main = 'import lib from "./lib"\nprint(lib.make(1).nope)\n'
        for out, err in run_on_both({"lib.mh": LIB_POINT, "main.mh": main}):
            self.assertEqual(err, "'Point' has no field 'nope' at position #2:19")

    def test_no_such_method_error_names_the_declared_type(self):
        main = 'import lib from "./lib"\nprint(lib.make(1).nope())\n'
        for out, err in run_on_both({"lib.mh": LIB_POINT, "main.mh": main}):
            self.assertIsNotNone(err)
            self.assertIn("Point", err)
            self.assertNotIn("__mah_m", err)

    def test_match_failure_names_the_declared_type(self):
        main = 'import lib from "./lib"\nmatch lib.shape(1) { lib.Shape.Empty => { print("e") } }\n'
        for out, err in run_on_both({"lib.mh": LIB_POINT, "main.mh": main}):
            self.assertIsNotNone(err)
            self.assertNotIn("__mah_m", err)

    def test_value_type_name_and_json_messages(self):
        main = (
            'import lib from "./lib"\nimport json from "std:json"\n'
            'print(try { json.decode(lib.Point, json.parse("{\\"x\\": \\"a\\"}")) } '
            "catch { json.JsonError.Shape { message } => { message } })\n"
        )
        for out, err in run_on_both({"lib.mh": LIB_POINT, "main.mh": main}):
            self.assertIsNone(err)
            self.assertEqual(out, "expected a Number for Point.x, got String\n")

    def test_uncaught_module_error_shows_the_declared_name(self):
        lib = "export struct Oops { why: String }\nimpl Error for Oops { fn message(self) { \"\" + self.why } }\nexport fn boom() { throw Oops { why: \"w\" } }\n"
        main = 'import lib from "./lib"\nlib.boom()\n'
        for out, err in run_on_both({"lib.mh": lib, "main.mh": main}):
            self.assertIsNotNone(err)
            self.assertTrue(err.startswith("Uncaught Oops"), err)
            self.assertNotIn("__mah_m", err)

    def test_uncaught_module_error_with_message(self):
        lib = (
            "export struct Oops { why: String }\nimpl Error for Oops { fn message(self) { self.why } }\n"
            "export fn boom() { throw Oops { why: \"w\" } }\n"
        )
        main = 'import lib from "./lib"\nlib.boom()\n'
        for out, err in run_on_both({"lib.mh": lib, "main.mh": main}):
            self.assertTrue(err.startswith("Uncaught Oops: w"), err)

    def test_reflect_results_use_declared_names(self):
        lib = (
            "export trait Named { fn name(self) -> String }\n"
            "export struct Thing { n: Number }\n"
            "impl Named for Thing { fn name(self) -> String { \"t\" } }\n"
        )
        main = (
            'import lib from "./lib"\nimport reflect from "std:reflect"\n'
            "let ms = reflect.methods(lib.Thing)\n"
            "print(ms[0].name, ms[0].trait_name)\n"
            "print(reflect.implements(lib.Thing, \"Named\"), reflect.implements(lib.Thing, \"Other\"))\n"
            "print(reflect.type_of(lib.Thing { n: 1 }))\n"
        )
        for out, err in run_on_both({"lib.mh": lib, "main.mh": main}):
            self.assertIsNone(err)
            self.assertEqual(out, "name some(Named)\ntrue false\nThing\n")


class TraitTests(unittest.TestCase):
    LIB = "export trait Named { fn name(self) -> String }\n"

    def test_module_trait_dispatch_and_implements(self):
        main = (
            'import lib from "./lib"\nimport reflect from "std:reflect"\n'
            "struct Mine { a: Number }\n"
            'impl lib.Named for Mine { fn name(self) -> String { "mine" } }\n'
            "print(Mine { a: 1 }.name())\n"
            'print(reflect.implements(Mine, "Named"))\n'
        )
        for out, err in run_on_both({"lib.mh": self.LIB, "main.mh": main}):
            self.assertIsNone(err)
            self.assertEqual(out, "mine\ntrue\n")

    def test_generic_bound_by_a_module_trait(self):
        lib = self.LIB + "export fn describe<T: Named>(x: T) -> String { x.name() }\n"
        main = (
            'import lib from "./lib"\nstruct Mine { a: Number }\n'
            'impl lib.Named for Mine { fn name(self) -> String { "mine" } }\n'
            "print(lib.describe(Mine { a: 1 }))\n"
        )
        self.assertEqual(run_main({"lib.mh": lib, "main.mh": main}), "mine\n")


class PreludeTests(unittest.TestCase):
    def test_module_declaring_a_prelude_name(self):
        lib = (
            "struct Range { a: Number }\n"
            "export fn make() { Range { a: 9 } }\n"
            "export fn count() { let n = 0\n for let i in 1..4 { n = n + i }\n n }\n"
        )
        main = 'import lib from "./lib"\nprint(lib.make())\nprint(lib.count())\nprint((1..3).end)\nfor let i in 1..3 { print(i) }\n'
        self.assertEqual(run_main({"lib.mh": lib, "main.mh": main}), "Range { a: 9 }\n6\n3\n1\n2\n")

    def test_module_declaring_an_iterator_like_prelude_trait_name(self):
        lib = "export trait Error { fn code(self) -> Number }\nexport struct Mine { a: Number }\nimpl Error for Mine { fn code(self) -> Number { 3 } }\n"
        main = 'import lib from "./lib"\nprint(Mine { a: 1 }.code())\n'.replace("Mine", "lib.Mine")
        self.assertEqual(run_main({"lib.mh": lib, "main.mh": main}), "3\n")


class StdCatchTests(unittest.TestCase):
    def test_catch_a_std_error_type(self):
        src = (
            'import json from "std:json"\n'
            'print(try { json.parse("{") } catch { json.JsonError.Syntax { message, line, column } => { line } })\n'
        )
        self.assertEqual(run_source(src), "1\n")

    def test_catch_by_type_annotation(self):
        src = 'import json from "std:json"\nprint(try { json.parse("{") } catch { e: json.JsonError => { "bad" } })\n'
        self.assertEqual(run_source(src), "bad\n")

    def test_std_error_uncaught_report_uses_the_declared_name(self):
        src = 'import json from "std:json"\njson.parse("{")\n'
        for out, err in run_on_both({"main.mh": src}):
            self.assertTrue(err.startswith("Uncaught JsonError"), err)


class MahTestOutputTests(unittest.TestCase):
    def test_failure_report_shows_the_declared_type_name(self):
        from tests.test_mah_test import _mah

        lib = "export struct Oops { why: String }\nimpl Error for Oops { fn message(self) { self.why } }\nexport fn boom() { throw Oops { why: \"w\" } }\n"
        test = 'import "std:test"\nimport lib from "./lib"\ntest "t" { lib.boom() }\ntest "u" { assert_eq(1, 2) }\n'
        for vm in ("python", "rust"):
            if vm == "rust":
                try:
                    find_vm()
                except RustVmNotFound:
                    continue
            with project({"lib.mh": lib, "x.test.mh": test}) as td:
                code, out, _err = _mah(["test", "--file", os.path.join(td, "x.test.mh"), "--vm", vm], td)
            self.assertEqual(code, 1)
            self.assertIn("Uncaught Oops: w", out)
            self.assertIn("assert_eq failed", out)
            self.assertNotIn("__mah_m", out)


class DisassemblerTests(unittest.TestCase):
    def test_disassembly_of_a_module_type_does_not_crash(self):
        from mah.bytecode.decode import decode
        from mah.bytecode.disasm import disassemble

        with project({"lib.mh": LIB_POINT, "main.mh": 'import lib from "./lib"\nprint(lib.make(1))\n'}) as td:
            data = compile_to_bytes(path=os.path.join(td, "main.mh"))
        text = disassemble(decode(data))
        self.assertIn("Point", text)


# ---------------------------------------------------------------------------
# Checker and language server
# ---------------------------------------------------------------------------


class CheckerTests(unittest.TestCase):
    def _diagnostics(self, files: dict) -> list:
        with project(files) as td:
            main = os.path.join(td, "main.mh")
            with open(main, encoding="utf-8") as f:
                text = f.read()
            return analysis.get_diagnostics(text, main)

    def test_matching_annotation_has_no_diagnostics(self):
        main = 'import lib from "./lib"\nlet p: lib.Point = lib.make(1)\nprint(p)\n'
        self.assertEqual(self._diagnostics({"lib.mh": LIB_POINT, "main.mh": main}), [])

    def test_mismatch_shows_the_declared_name(self):
        main = 'import lib from "./lib"\nlet p: lib.Point = 5\n'
        diagnostics = self._diagnostics({"lib.mh": LIB_POINT, "main.mh": main})
        self.assertTrue(diagnostics)
        for d in diagnostics:
            self.assertNotIn("__mah_m", d["message"])
        self.assertTrue(any("Point" in d["message"] for d in diagnostics), diagnostics)

    def test_mah_check_style_messages_are_demangled(self):
        with project(
            {"lib.mh": LIB_POINT, "main.mh": 'import lib from "./lib"\nlet p: lib.Point = 5\n'}
        ) as td:
            try:
                compile_to_bytes(path=os.path.join(td, "main.mh"), check="strict")
            except Exception as exc:  # noqa: BLE001
                self.assertNotIn("__mah_m", str(exc))
                self.assertIn("Point", str(exc))
            else:
                self.fail("expected a type error")


def _at(text: str, needle: str, nth: int = 0, delta: int = 0) -> dict:
    offset = -1
    for _ in range(nth + 1):
        offset = text.index(needle, offset + 1)
    return analysis.offset_to_position(text, offset + delta)


def _apply(text: str, edits: list) -> str:
    spans = []
    for edit in edits:
        start = analysis.position_to_offset(text, edit["range"]["start"]["line"], edit["range"]["start"]["character"])
        end = analysis.position_to_offset(text, edit["range"]["end"]["line"], edit["range"]["end"]["character"])
        spans.append((start, end, edit["newText"]))
    for start, end, new in sorted(spans, reverse=True):
        text = text[:start] + new + text[end:]
    return text


class LanguageServerTests(unittest.TestCase):
    LIB = "export struct Point { x: Number }\nexport fn make() -> Point { Point { x: 1 } }\n"
    MAIN = (
        'import lib from "./lib"\n'
        "let a: lib.Point = lib.make()\n"
        "let b = lib.Point { x: 2 }\n"
        "print(a, b)\n"
    )
    FLAT = 'import "./lib"\nlet c: Point = make()\nprint(c, Point { x: 3 })\n'

    def test_definition_of_a_namespaced_type_lands_in_the_module(self):
        with project({"lib.mh": self.LIB, "main.mh": self.MAIN}) as td:
            main = os.path.join(td, "main.mh")
            pos = _at(self.MAIN, "Point")
            result = analysis.get_definition(self.MAIN, pos["line"], pos["character"], main)
            self.assertEqual(result["path"], os.path.join(td, "lib.mh"))
            self.assertEqual(result["range"]["start"], {"line": 0, "character": 14})

    def test_definition_of_a_flat_type_lands_in_the_module(self):
        with project({"lib.mh": self.LIB, "main.mh": self.FLAT}) as td:
            main = os.path.join(td, "main.mh")
            pos = _at(self.FLAT, "Point")
            result = analysis.get_definition(self.FLAT, pos["line"], pos["character"], main)
            self.assertEqual(result["path"], os.path.join(td, "lib.mh"))

    def test_rename_from_the_declaration_edits_every_file(self):
        with project({"lib.mh": self.LIB, "main.mh": self.MAIN, "flat.mh": self.FLAT}) as td:
            lib_path = os.path.join(td, "lib.mh")
            pos = _at(self.LIB, "Point")
            result = analysis.get_rename_edits(self.LIB, pos["line"], pos["character"], "Vec2", lib_path)
            self.assertIsNotNone(result)
            changes = result["changes"]
            self.assertEqual(set(changes), {lib_path, os.path.join(td, "main.mh"), os.path.join(td, "flat.mh")})
            self.assertEqual(
                _apply(self.LIB, changes[lib_path]),
                "export struct Vec2 { x: Number }\nexport fn make() -> Vec2 { Vec2 { x: 1 } }\n",
            )
            self.assertEqual(
                _apply(self.MAIN, changes[os.path.join(td, "main.mh")]),
                self.MAIN.replace("lib.Point", "lib.Vec2"),
            )
            self.assertEqual(
                _apply(self.FLAT, changes[os.path.join(td, "flat.mh")]),
                self.FLAT.replace("Point", "Vec2"),
            )

    def test_rename_from_an_importer_edits_every_file(self):
        with project({"lib.mh": self.LIB, "main.mh": self.MAIN, "flat.mh": self.FLAT}) as td:
            main = os.path.join(td, "main.mh")
            pos = _at(self.MAIN, "Point", 0)
            result = analysis.get_rename_edits(self.MAIN, pos["line"], pos["character"], "Vec2", main)
            self.assertIsNotNone(result)
            changes = result["changes"]
            self.assertEqual(set(changes), {os.path.join(td, "lib.mh"), main, os.path.join(td, "flat.mh")})
            self.assertEqual(_apply(self.MAIN, changes[main]), self.MAIN.replace("lib.Point", "lib.Vec2"))

    def test_rename_refuses_a_standard_library_type(self):
        src = 'import json from "std:json"\nlet e: json.JsonError = 1\n'
        pos = _at(src, "JsonError")
        self.assertIsNone(analysis.get_rename_edits(src, pos["line"], pos["character"], "Oops", None))

    def test_hover_shows_the_declared_name(self):
        with project({"lib.mh": self.LIB, "main.mh": self.MAIN}) as td:
            main = os.path.join(td, "main.mh")
            pos = _at(self.MAIN, "Point")
            hover = analysis.get_hover(self.MAIN, pos["line"], pos["character"], main)
            if hover is not None:
                self.assertNotIn("__mah_m", hover["contents"]["value"])

    def test_completion_does_not_leak_mangled_names(self):
        with project({"lib.mh": self.LIB, "main.mh": self.MAIN + "\n"}) as td:
            main = os.path.join(td, "main.mh")
            pos = analysis.offset_to_position(self.MAIN + "\n", len(self.MAIN) + 1)
            items = analysis.get_completions(self.MAIN + "\n", main, pos["line"], pos["character"]) or []
            for item in items:
                self.assertNotIn("__mah_m", item["label"])


class FormatterTests(unittest.TestCase):
    def test_export_struct_enum_trait_round_trip(self):
        from mah.format.formatter import format_source

        src = (
            "export struct Point { x: Number }\n\n"
            "export enum Shape { Circle { r: Number }, Empty }\n\n"
            "export trait Named {\n    fn name(self) -> String\n}\n"
        )
        self.assertEqual(format_source(src), src)

    def test_dotted_type_paths_round_trip(self):
        from mah.format.formatter import format_source

        src = (
            'import lib from "./lib"\n\n'
            "let p: lib.Point = lib.Point { x: 1 }\n"
            "match p {\n    lib.Shape.Circle { r } => { r }\n    _ => { 0 }\n}\n"
            "impl lib.Named for Mine { }\n"
        )
        self.assertEqual(format_source(src), src)


if __name__ == "__main__":
    unittest.main()
