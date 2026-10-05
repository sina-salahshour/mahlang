"""M37 (docs/contracts/M37_bytes.md): the `Bytes` type -- runtime error
kinds and texts, the checker's view of it, the bytecode minor (1.17), the
META downgrade below 1.17, the decoder's primitive-code rule, and
reflection -- on whichever VM `MAH_TEST_VM` selects. The behavior of every
method is covered by mah/std/bytes.test.mh and mah/std/fs.test.mh; every
rule is implemented in mah/bytes_methods.py.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mah.bytecode.decode import decode  # noqa: E402
from mah.bytecode.encode import encode  # noqa: E402
from mah.bytecode.format import MahcFormatError  # noqa: E402
from tests.support import compile_bytes, compile_program, run_source  # noqa: E402
from tests.test_typecheck import check  # noqa: E402

BYTES = 'import bytes from "std:bytes"\n'
FS = 'import fs from "std:fs"\n'


def out(expr: str) -> str:
    return run_source(f"print({expr})").rstrip("\n")


def error(expr: str, setup: str = "") -> str:
    """`KIND: message` of the runtime error `expr` throws."""
    return run_source(
        setup
        + f"print(try {{ {expr} }} catch {{\n"
        + "  RuntimeError.TypeMismatch { message } => { \"TypeMismatch: \" + message }\n"
        + "  RuntimeError.ArgumentError { message } => { \"ArgumentError: \" + message }\n"
        + "  RuntimeError.IndexOutOfRange { message } => { \"IndexOutOfRange: \" + message }\n"
        + ("  e => { \"other: \" + e.message().replace_all(dir, \"DIR\") }\n" if "let dir" in setup
           else "  e => { \"other: \" + e.message() }\n")
        + "})"
    ).rstrip("\n")


def errors_of(expr: str) -> list[str]:
    """Diagnostics' messages for a snippet."""
    diagnostics, _ = check(expr)
    return [message for _kind, message, _line in diagnostics]


def check_fs(src: str):
    """`check`, minus "Unhandled error: FsError" (the snippets don't catch it)."""
    diagnostics, types = check(src)
    return [d for d in diagnostics if d[0] != "unhandled"], types


def minor_of(src: str) -> int:
    return decode(compile_bytes(text=src)).minor


class ValueTests(unittest.TestCase):
    def test_to_string(self):
        self.assertEqual(out('"hi".to_bytes()'), "Bytes[68 69]")
        self.assertEqual(out('"".to_bytes()'), "Bytes[]")
        self.assertEqual(out('"\\n~".to_bytes()'), "Bytes[0a 7e]")
        self.assertEqual(out('"x" + "a".to_bytes()'), "xBytes[61]")

    def test_index_none_and_negative(self):
        self.assertEqual(out('"abc".to_bytes()[0], "abc".to_bytes()[-1], "abc".to_bytes()[3]'), "97 99 none")

    def test_equality_is_by_contents_and_never_a_vector(self):
        self.assertEqual(out('"a".to_bytes() == "a".to_bytes(), "a".to_bytes() == [97]'), "true false")
        self.assertEqual(out('"a".to_bytes() != "b".to_bytes()'), "true")

    def test_always_truthy(self):
        self.assertEqual(out('if "".to_bytes() { "yes" } else { "no" }'), "yes")

    def test_not_a_map_key(self):
        message = error('let m = [1: 1]\n m["a".to_bytes()]')
        self.assertTrue(message.endswith("Bytes"), message)  # the usual Map key error

    def test_plus(self):
        self.assertEqual(out('"a".to_bytes() + "bc".to_bytes()'), "Bytes[61 62 63]")

    def test_deep_copy_reaches_bytes(self):
        src = (
            'let b = "a".to_bytes()\nlet v = [b]\n'
            "let s = v.copy()\nlet d = v.copy(deep: true)\n"
            "b.push(1)\nprint(s[0], d[0])"
        )
        self.assertEqual(run_source(src).rstrip("\n"), "Bytes[61 01] Bytes[61]")


class ErrorTests(unittest.TestCase):
    def test_byte_must_be_a_number(self):
        self.assertEqual(
            error('"a".to_bytes()[0] = "x"'), "TypeMismatch: Bytes item must be a Number, got String"
        )

    def test_byte_must_be_whole_and_in_range(self):
        for value in ("256", "0 - 1", "1.5"):
            shown = {"256": "256", "0 - 1": "-1", "1.5": "1.5"}[value]
            self.assertEqual(
                error(f'"a".to_bytes()[0] = {value}'),
                f"ArgumentError: Bytes item must be a whole number from 0 to 255, got {shown}",
            )

    def test_index_out_of_range(self):
        self.assertEqual(
            error('"ab".to_bytes()[2] = 1'),
            "IndexOutOfRange: Bytes index 2 is out of range for Bytes of length 2 (use push to add items)",
        )
        self.assertEqual(
            error('"ab".to_bytes()[0 - 3] = 1'),
            "IndexOutOfRange: Bytes index -3 is out of range for Bytes of length 2 (use push to add items)",
        )

    def test_slice_assignment(self):
        self.assertEqual(
            error('"abc".to_bytes()[0..2] = "x".to_bytes()'),
            "TypeMismatch: Can't assign to a Bytes slice (b[a..b] = ...); assign items one at a time",
        )

    def test_index_must_be_a_number(self):
        self.assertEqual(
            error('"a".to_bytes()["0"] = 1'), "TypeMismatch: Bytes index must be a Number, got String"
        )

    def test_push(self):
        self.assertEqual(
            error('"a".to_bytes().push(0 - 1)'),
            "ArgumentError: push: the value must be a whole number from 0 to 255, got -1",
        )
        self.assertEqual(
            error('"a".to_bytes().push("x")'), "TypeMismatch: push: the value must be a Number, got String"
        )

    def test_extend_and_index_of(self):
        self.assertEqual(error('"a".to_bytes().extend([1])'), "TypeMismatch: extend: other must be Bytes, got Vector")
        self.assertEqual(
            error('"a".to_bytes().index_of("a")'), "TypeMismatch: index_of: needle must be Bytes, got String"
        )

    def test_bytes_new(self):
        self.assertEqual(
            error("bytes.new(1.5)", BYTES),
            "ArgumentError: new: size must be a whole number of at least 0, got 1.5",
        )
        self.assertEqual(
            error("bytes.new(0 - 2)", BYTES),
            "ArgumentError: new: size must be a whole number of at least 0, got -2",
        )
        self.assertEqual(
            error("bytes.new(1, 300)", BYTES),
            "ArgumentError: new: fill must be a whole number from 0 to 255, got 300",
        )
        self.assertEqual(
            error('bytes.new(1, "x")', BYTES), "TypeMismatch: new: fill must be a Number, got String"
        )
        self.assertEqual(out_with(BYTES, "bytes.new(2), bytes.new(2, 9), bytes.new()"), "Bytes[00 00] Bytes[09 09] Bytes[]")

    def test_bytes_new_size_not_a_number(self):
        self.assertEqual(error('bytes.new("a")', BYTES), "TypeMismatch: new: size must be a Number, got String")

    def test_bytes_from_vector(self):
        self.assertEqual(
            error("bytes.from_vector([1, 256])", BYTES),
            "ArgumentError: from_vector: item 1 must be a whole number from 0 to 255, got 256",
        )
        self.assertEqual(
            error('bytes.from_vector([1, "x"])', BYTES),
            "TypeMismatch: from_vector: item 1 must be a Number, got String",
        )
        self.assertEqual(
            error("bytes.from_vector(3)", BYTES), "TypeMismatch: from_vector: items must be a Vector, got Number"
        )
        self.assertEqual(
            error("bytes.from_hex(3)", BYTES), "TypeMismatch: from_hex: text must be a String, got Number"
        )
        self.assertEqual(
            error("bytes.from_base64(3)", BYTES), "TypeMismatch: from_base64: text must be a String, got Number"
        )

    def test_from_hex_and_base64_edge_cases(self):
        kind = 'try {{ {} }} catch {{ e: bytes.BytesError => {{ e.kind }} }}'
        cases = {
            'bytes.from_hex("").len()': "0",
            'bytes.from_base64("Zh==").to_hex(), bytes.from_base64("Zg==").to_hex()': "66 66",
            kind.format('bytes.from_hex("abc")'): "invalid_hex",
            kind.format('bytes.from_hex("0g")'): "invalid_hex",
            kind.format('bytes.from_base64("Zg=")'): "invalid_base64",
            kind.format('bytes.from_base64("Z===")'): "invalid_base64",
            kind.format('bytes.from_base64("Zg==Zg==")'): "invalid_base64",
        }
        for expr, expected in cases.items():
            with self.subTest(expr=expr):
                self.assertEqual(out_with(BYTES, expr), expected)

    def test_bytes_error_text_is_cut(self):
        long = "z" * 45
        self.assertEqual(
            out_with(BYTES, f'try {{ bytes.from_hex("{long}") }} catch {{ e => {{ e.message() }} }}'),
            'from_hex: not valid hexadecimal: "' + "z" * 40 + '..."',
        )
        self.assertEqual(
            out_with(BYTES, 'try { bytes.from_base64("a b") } catch { e => { e.message() } }'),
            'from_base64: not valid base64: "a b"',
        )

    def test_fs_native_argument_errors(self):
        self.assertEqual(
            error('fs.write_bytes("/tmp/x", "text")', FS), "TypeMismatch: write_bytes: data must be Bytes, got String"
        )
        self.assertEqual(
            error('fs.append_bytes("/tmp/x", [1])', FS), "TypeMismatch: append_bytes: data must be Bytes, got Vector"
        )

    def test_fs_file_errors(self):
        setup = FS + 'let dir = fs.temp_dir()\ndefer fs.remove(dir, recursive: true)\n'
        body = (
            'fs.write_bytes(dir + "/a", "abc".to_bytes())\n'
            'let f = fs.open(dir + "/a")\n'
            'let w = fs.open(dir + "/b", "w")\n'
        )
        for expr, expected in (
            ("f.read_bytes(0 - 1)", "ArgumentError: file_read_bytes: max must be a whole number of at least 0, got -1"),
            ('f.read_bytes("2")', "TypeMismatch: file_read_bytes: max must be a Number or none, got String"),
            ("f.write_bytes(bytes.new())", "other: write_bytes: the file isn't open for writing: DIR/a"),
            ("w.read_bytes(1)", "other: read_bytes: the file isn't open for reading: DIR/b"),
            ('w.write_bytes("x")', "TypeMismatch: file_write_bytes: data must be Bytes, got String"),
        ):
            with self.subTest(expr=expr):
                # an FsError message ends with the path (`DIR` stands for the temporary directory)
                self.assertEqual(error(expr, setup + BYTES + body), expected)


def out_with(prefix: str, expr: str) -> str:
    return run_source(prefix + f"print({expr})").rstrip("\n")


class CheckerTests(unittest.TestCase):
    def test_index_and_slice_types(self):
        diagnostics, types = check('let b = "a".to_bytes()\nlet x = b[0]\nlet s = b[0..1]\nlet c = b + b')
        self.assertEqual(diagnostics, [])
        self.assertEqual(types["b"], ["Bytes"])
        self.assertEqual(types["x"], ["Number"])
        self.assertEqual(types["s"], ["Bytes"])
        self.assertEqual(types["c"], ["Bytes"])

    def test_to_bytes_is_bytes(self):
        _, types = check('let b = "s".to_bytes()')
        self.assertEqual(types["b"], ["Bytes"])

    def test_bytes_plus_number_is_an_error(self):
        diagnostics, _ = check('let b = "a".to_bytes()\nlet c = b + 1')
        self.assertTrue(diagnostics, "b + 1 should be reported")
        self.assertEqual(diagnostics[0][2], 2)

    def test_iteration_elements_are_numbers(self):
        diagnostics, types = check('for let x in "a".to_bytes() {\n    let y = x\n}')
        self.assertEqual(diagnostics, [])
        self.assertEqual(types["y"], ["Number"])

    def test_method_results(self):
        src = (
            'let b = "a".to_bytes()\n'
            "let n = b.len()\nlet p = b.pop()\nlet h = b.to_hex()\nlet e = b.to_base64()\n"
            "let t = b.to_text()\nlet l = b.to_text_lossy()\nlet v = b.to_vector()\n"
            "let c = b.copy()\nlet i = b.index_of(b)\nlet u = b.push(1)\nlet w = b.extend(b)"
        )
        diagnostics, types = check(src)
        self.assertEqual(diagnostics, [])
        self.assertEqual(types["n"], ["Number"])
        self.assertEqual(types["p"], ["Number"])
        self.assertEqual(types["h"], ["String"])
        self.assertEqual(types["e"], ["String"])
        self.assertEqual(types["t"], ["Option<String>"])
        self.assertEqual(types["l"], ["String"])
        self.assertEqual(types["v"], ["Vector<Number>"])
        self.assertEqual(types["c"], ["Bytes"])
        self.assertEqual(types["i"], ["Option<Number>"])

    def test_wrong_argument_types(self):
        for call in ('b.push("x")', 'b.extend([1])', 'b.index_of("a")'):
            with self.subTest(call=call):
                diagnostics, _ = check(f'let b = "a".to_bytes()\n{call}')
                self.assertTrue(diagnostics, call)

    def test_fs_and_bytes_module_results(self):
        diagnostics, types = check_fs(
            FS + BYTES + 'let got_file = fs.read_bytes("p")\nlet got_new = bytes.new(2)\n'
            'let got_hex = try bytes.from_hex("00") else bytes.new()\n'
            'let got_concat = bytes.concat([got_new, got_file])\nlet got_vec = bytes.from_vector([1])'
        )
        self.assertEqual(diagnostics, [])
        self.assertEqual(types["got_file"], ["Bytes"])
        self.assertEqual(types["got_new"], ["Bytes"])
        self.assertEqual(types["got_hex"], ["Bytes"])
        self.assertEqual(types["got_concat"], ["Bytes"])
        self.assertEqual(types["got_vec"], ["Bytes"])

    def test_file_methods(self):
        diagnostics, types = check_fs(
            FS + 'let got_f = fs.open("p")\nlet got_x = got_f.read_bytes(2)\nlet got_y = got_f.read_bytes()\ngot_f.write_bytes(got_x)'
        )
        self.assertEqual(diagnostics, [])
        self.assertEqual(types["got_x"], ["Bytes"])
        self.assertEqual(types["got_y"], ["Bytes"])

    def test_annotation(self):
        diagnostics, _ = check('fn f(x: Bytes) -> Bytes { x }\nlet b = f("a".to_bytes())')
        self.assertEqual(diagnostics, [])
        diagnostics, _ = check("fn f(x: Bytes) -> Bytes { x }\nlet b = f([1])")
        self.assertTrue(diagnostics)


class VersionTests(unittest.TestCase):
    def test_minor_17(self):
        self.assertEqual(minor_of('print("a".to_bytes())'), 17)
        self.assertEqual(minor_of("print(Bytes)"), 17)
        self.assertEqual(minor_of(FS + 'print(fs.exists("x"))'), 17)
        self.assertEqual(minor_of(BYTES + "print(bytes.new())"), 17)

    def test_a_program_without_bytes_is_unchanged(self):
        self.assertEqual(minor_of("print(1)"), 4)
        self.assertEqual(minor_of('print(" a ".trim())'), 6)
        self.assertEqual(minor_of("print(Number)"), 14)

    def test_a_bytes_annotation_alone_is_not_1_17(self):
        self.assertEqual(minor_of("fn f(x: Bytes) -> Bytes { x }\nprint(1)"), 4)

    def test_to_bytes_in_a_user_method_still_counts(self):
        # any call to a method named `to_bytes` outside the prelude is 1.17
        self.assertEqual(minor_of("struct S { n: Number }\nimpl S { fn to_bytes(self) { 1 } }\nprint(S { n: 1 }.to_bytes())"), 17)


def _sections_with_minor(data: bytes, minor: int) -> bytes:
    """The same file with its minor version byte changed (the header is
    `MAHC`, major as 2 bytes, then minor as 2 bytes)."""
    return data[:6] + bytes([minor, 0]) + data[8:]


class MetaAndDecoderTests(unittest.TestCase):
    def test_meta_writes_bytes_as_primitive_code_8_in_1_17(self):
        program = compile_program(text="fn f(x: Bytes) -> Bytes { x }\nprint(Bytes)")
        self.assertEqual(program.minor, 17)
        params = [p.type for fn in program.meta.functions if fn.has_meta for p in fn.params]
        self.assertEqual([(t.tag, t.kind, t.index) for t in params], [(1, 1, 8)])
        decoded = decode(encode(program))
        again = [p.type for fn in decoded.meta.functions if fn.has_meta for p in fn.params]
        self.assertEqual([(t.tag, t.kind, t.index) for t in again], [(1, 1, 8)])

    def test_meta_downgrades_bytes_to_unknown_below_1_17(self):
        src = "fn f(x: Bytes, y: Vector<Bytes>) -> Bytes { x }\nprint(1)"
        program = compile_program(text=src)
        self.assertEqual(program.minor, 4)
        metas = [fn for fn in program.meta.functions if fn.has_meta]
        self.assertEqual(len(metas), 1)
        fn = metas[0]
        self.assertEqual(fn.params[0].type.tag, 0)  # Unknown
        self.assertEqual(fn.returns.tag, 0)
        vec = fn.params[1].type  # Vector<Bytes>: the Vector stays, its argument is Unknown
        self.assertEqual((vec.tag, vec.kind, vec.index), (1, 1, 4))
        self.assertEqual([a.tag for a in vec.args], [0])
        # and it still decodes and runs
        data = compile_bytes(text=src)
        decode(data)
        self.assertEqual(run_source(src), "1\n")

    def test_struct_fields_downgrade_too(self):
        src = "struct S { data: Bytes }\nprint(1)"
        program = compile_program(text=src)
        self.assertEqual(program.minor, 4)
        decode(encode(program))
        for tm in program.meta.types:
            for field_type, _doc in tm.body:
                self.assertEqual(field_type.tag, 0)

    def test_a_1_16_file_with_loadtype_code_8_is_refused(self):
        data = compile_bytes(text="print(Bytes)")
        self.assertEqual(decode(data).minor, 17)
        with self.assertRaises(MahcFormatError) as ctx:
            decode(_sections_with_minor(data, 16))
        self.assertIn("primitive type code 8 out of range", str(ctx.exception))

    def test_a_1_16_file_with_meta_code_8_is_refused(self):
        data = compile_bytes(text="fn f(x: Bytes) { x }\nprint(Bytes)")
        with self.assertRaises(MahcFormatError) as ctx:
            decode(_sections_with_minor(data, 16))
        self.assertIn("primitive type code 8 out of range", str(ctx.exception))

    def test_code_9_is_never_valid(self):
        from mah.bytecode.program import Instr

        program = compile_program(text="print(Bytes)")
        for i, instr in enumerate(program.code):
            if instr.op == "loadtype" and instr.args[0] == 1 and instr.args[1] == 8:
                program.code[i] = Instr("loadtype", (1, 9))
        with self.assertRaises(MahcFormatError):
            decode(encode(program))

    def test_unsupported_minor_is_18(self):
        data = compile_bytes(text="print(1)")
        with self.assertRaises(MahcFormatError):
            decode(_sections_with_minor(data, 18))


class ReflectionTests(unittest.TestCase):
    def test_type_of(self):
        src = 'import reflect from "std:reflect"\nprint(reflect.type_of("a".to_bytes()), reflect.type_of("a".to_bytes()) == Bytes)'
        self.assertEqual(run_source(src), "Bytes true\n")

    def test_bytes_is_a_type_value(self):
        self.assertEqual(out("Bytes, Bytes == Vector"), "Bytes false")

    def test_signature_in_a_1_17_file(self):
        src = (
            'import reflect from "std:reflect"\n'
            "fn f(x: Bytes) -> Bytes { x }\n"
            "let s = reflect.signature(f)\n"
            "print(s.params[0].type, s.returns)\n"
            "print(Bytes)"
        )
        self.assertEqual(run_source(src), "TypeRef.Named { type: Bytes, args: [] } TypeRef.Named { type: Bytes, args: [] }\nBytes\n")

    def test_signature_below_1_17_reads_the_annotation_as_unknown(self):
        src = (
            'import reflect from "std:reflect"\n'
            "fn f(x: Bytes) -> Number { 1 }\n"
            "let s = reflect.signature(f)\n"
            "print(s.params[0].type)"
        )
        self.assertEqual(minor_of(src), 16)
        self.assertEqual(run_source(src), "TypeRef.Unknown\n")


if __name__ == "__main__":
    unittest.main()
