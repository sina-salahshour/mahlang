"""Tests for the `.mahc` bytecode format and the VM that runs it --
docs/MAHC_FORMAT.md (normative) and its M14 implementation. See
docs/TESTING.md for the testing policy this file is part of.

Covers: LEB128 encoding primitives, round-trip/determinism of `encode`/
`decode` over every example program, loader validation (`MahcFormatError`
for every malformed-file case docs/MAHC_FORMAT.md #3/#4 calls out), build
targets (`debug` vs `release`) and runtime error locations, the VM's
independence from the compiler/LSP packages, the `mah build`/`runc`/`run`/
`dis` CLI subcommands, and the handful of semantics docs/MAHC_FORMAT.md #5/
#6 pins down precisely (Decimal-only Numbers, strict `eq`/`lt`/`gt` typing,
`input()`'s end-of-input error, function value printing).
"""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mah.bytecode.decode import decode
from mah.bytecode.disasm import disassemble
from mah.bytecode.encode import encode
from mah.bytecode.format import SEC_PARAMS, MahcFormatError
from mah.bytecode.leb128 import read_varint, read_varuint, write_varint, write_varuint
from mah.bytecode.program import Const, FunctionDecl, Instr, NativeRef, Program
from mah.cli.main import main as cli_main
from mah.code_interpreter import run_bytes
from mah.runtime_values import MahRuntimeError
from tests.support import EXAMPLES_DIR, compile_bytes, compile_program, example_path, run_file, run_source

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_EXAMPLE_FILES = sorted(f for f in os.listdir(EXAMPLES_DIR) if f.endswith(".mh"))


# ---------------------------------------------------------------------------
# 1. Encoding primitives
# ---------------------------------------------------------------------------

class Leb128Tests(unittest.TestCase):
    def test_varuint_round_trips(self):
        for n in (0, 1, 127, 128, 300, 2**35, 2**70):
            data = write_varuint(n)
            value, pos = read_varuint(data, 0)
            self.assertEqual(value, n)
            self.assertEqual(pos, len(data))

    def test_varint_round_trips(self):
        for n in (0, -1, 1, -64, 64, -(2**70), 2**70):
            data = write_varint(n)
            value, pos = read_varint(data, 0)
            self.assertEqual(value, n)
            self.assertEqual(pos, len(data))

    def test_known_bytes(self):
        self.assertEqual(write_varuint(300), b"\xac\x02")
        self.assertEqual(write_varint(-1), b"\x01")


# ---------------------------------------------------------------------------
# 2-5. Round trip / determinism, every examples/*.mh
# ---------------------------------------------------------------------------

class RoundTripTests(unittest.TestCase):
    def test_decode_encode_round_trip_every_example(self):
        for name in _EXAMPLE_FILES:
            with self.subTest(example=name):
                program = compile_program(path=example_path(name))
                self.assertEqual(decode(encode(program)), program)
                data = encode(program)
                self.assertEqual(encode(decode(data)), data)

    def test_compiling_twice_is_deterministic(self):
        for name in _EXAMPLE_FILES:
            with self.subTest(example=name):
                a = compile_bytes(path=example_path(name))
                b = compile_bytes(path=example_path(name))
                self.assertEqual(a, b)

    def test_bytes_start_with_magic_and_version(self):
        for name in _EXAMPLE_FILES:
            with self.subTest(example=name):
                data = compile_bytes(path=example_path(name))
                # M19: the reference encoder now writes minor version 3
                # (Vectors and Maps; 2 was M17's `matchrange` etc.) -- see
                # docs/MAHC_FORMAT.md #7.
                self.assertEqual(data[:8], b"MAHC\x01\x00\x03\x00")

    def test_decoded_bytes_run_the_same_as_the_source(self):
        for name, stdin in (("traits.mh", ""), ("enums.mh", "")):
            with self.subTest(example=name):
                data = compile_bytes(path=example_path(name))
                out = io.StringIO()
                old_stdin = sys.stdin
                sys.stdin = io.StringIO(stdin)
                try:
                    with contextlib.redirect_stdout(out):
                        run_bytes(data)
                finally:
                    sys.stdin = old_stdin
                self.assertEqual(out.getvalue(), run_file(example_path(name), stdin=stdin))


# ---------------------------------------------------------------------------
# 6-16. Loader validation
# ---------------------------------------------------------------------------

def _minimal_program(code, *, natives=None, functions=None, strings=None, constants=None) -> Program:
    return Program(
        strings=strings if strings is not None else [],
        constants=constants if constants is not None else [],
        types=[],
        natives=natives if natives is not None else [],
        functions=functions if functions is not None else [FunctionDecl(0, 1, 0, None)],
        code=code,
        debug=None,
    )


class LoaderValidationTests(unittest.TestCase):
    def test_bad_magic(self):
        data = compile_bytes(text="print(1)")
        bad = b"XXXX" + data[4:]
        with self.assertRaises(MahcFormatError) as cm:
            decode(bad)
        self.assertIn("magic", str(cm.exception))

    def test_unsupported_major_version(self):
        data = bytearray(compile_bytes(text="print(1)"))
        data[4] = 2
        with self.assertRaises(MahcFormatError) as cm:
            decode(bytes(data))
        self.assertIn("major", str(cm.exception))

    def test_unsupported_minor_version(self):
        # M19: this VM now implements minor version 3, so the smallest
        # genuinely unsupported minor version is 4.
        data = bytearray(compile_bytes(text="print(1)"))
        data[6] = 4
        with self.assertRaises(MahcFormatError) as cm:
            decode(bytes(data))
        self.assertIn("minor", str(cm.exception))

    def test_truncated_file(self):
        data = compile_bytes(text="print(1)")
        with self.assertRaises(MahcFormatError) as cm:
            decode(data[:-1])
        self.assertIn("truncated", str(cm.exception))

    def test_unknown_required_section_after_code(self):
        # M16: 0x07 is now PARAMS (a known required section in 1.1), so the
        # first genuinely unknown required id is 0x08; re-adding 0x07 is a
        # duplicate instead -- both must be rejected, with the right reason.
        data = compile_bytes(text="print(1)", target="release")
        with self.assertRaises(MahcFormatError) as cm:
            decode(data + bytes([0x08]) + write_varuint(0))
        self.assertIn("unknown required section 0x08", str(cm.exception))
        with self.assertRaises(MahcFormatError) as cm:
            decode(data + bytes([0x07]) + write_varuint(0))
        self.assertIn("duplicate required section 0x07", str(cm.exception))

    def test_unknown_opcode(self):
        program = _minimal_program([Instr("halt", ())])
        data = bytearray(encode(program))
        self.assertEqual(data[-1], 0x00)  # the halt opcode byte, last byte of the file
        data[-1] = 0xFF
        with self.assertRaises(MahcFormatError) as cm:
            decode(bytes(data))
        self.assertIn("opcode", str(cm.exception))

    def test_jump_target_out_of_range(self):
        program = _minimal_program([Instr("jmp", (5,)), Instr("halt", ())])
        with self.assertRaises(MahcFormatError) as cm:
            decode(encode(program))
        self.assertIn("jump target", str(cm.exception))

    def test_string_index_out_of_range(self):
        program = _minimal_program(
            [Instr("getfield", ((0, 0), 5, (0, 0))), Instr("halt", ())],
            strings=[],
        )
        with self.assertRaises(MahcFormatError) as cm:
            decode(encode(program))
        self.assertIn("string index", str(cm.exception))

    def test_native_arg_count_mismatch(self):
        program = _minimal_program(
            [Instr("native", (0, (), None)), Instr("halt", ())],
            strings=["io.print"],
            natives=[NativeRef(0, 1)],
        )
        with self.assertRaises(MahcFormatError) as cm:
            decode(encode(program))
        self.assertIn("native", str(cm.exception))

    def test_unsupported_native_rejected_before_any_output(self):
        program = _minimal_program(
            [
                Instr("loadk", (0, (0, 0))),
                Instr("native", (0, ((0, 0),), None)),   # io.print(slot0)
                Instr("native", (1, ((0, 0),), (0, 0))), # net.connect(slot0)
                Instr("halt", ()),
            ],
            strings=["hello", "io.print", "net.connect"],
            constants=[Const(5, 0)],
            natives=[NativeRef(1, 1), NativeRef(2, 1)],
        )
        data = encode(program)
        decode(data)  # structurally valid -- the file itself is fine
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaises(MahcFormatError) as cm:
                run_bytes(data)
        self.assertIn("net.connect", str(cm.exception))
        self.assertEqual(out.getvalue(), "")

    def test_unknown_optional_section_is_skipped(self):
        data = compile_bytes(text='print("hi")')
        bad = data + bytes([0x90]) + write_varuint(3) + b"xyz"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            run_bytes(bad)
        self.assertEqual(out.getvalue(), "hi\n")


# ---------------------------------------------------------------------------
# 17-21. Build targets and runtime error locations
# ---------------------------------------------------------------------------

class BuildTargetTests(unittest.TestCase):
    def test_release_has_no_debug_and_is_smaller(self):
        debug = compile_bytes(path=example_path("traits.mh"), target="debug")
        release = compile_bytes(path=example_path("traits.mh"), target="release")
        self.assertIsNone(decode(release).debug)
        self.assertIsNotNone(decode(debug).debug)
        self.assertLess(len(release), len(debug))

    def test_debug_runtime_error_has_entry_file_location(self):
        src = "let a = 1\nstruct S { }\nS {}.nope()\n"
        data = compile_bytes(text=src, target="debug")
        with self.assertRaises(MahRuntimeError) as cm:
            run_bytes(data)
        self.assertIn("'S' has no method 'nope' at position #3:6", str(cm.exception))

    def test_release_runtime_error_has_no_location(self):
        src = "let a = 1\nstruct S { }\nS {}.nope()\n"
        data = compile_bytes(text=src, target="release")
        with self.assertRaises(MahRuntimeError) as cm:
            run_bytes(data)
        self.assertEqual(str(cm.exception), "'S' has no method 'nope'")

    def test_error_inside_an_imported_file_is_located_by_that_file(self):
        with tempfile.TemporaryDirectory() as td:
            lib_path = os.path.join(td, "lib.mh")
            main_path = os.path.join(td, "main.mh")
            with open(lib_path, "w") as f:
                f.write("export fn boom() {\n    let s = 1\n    s.nope()\n}\n")
            with open(main_path, "w") as f:
                f.write('import "lib.mh"\nboom()\n')
            with self.assertRaises(MahRuntimeError) as cm:
                run_file(main_path)
            self.assertIn("at position lib.mh#3:", str(cm.exception))

    def test_error_inside_a_nested_task_is_located_exactly_once(self):
        src = (
            "struct Bad { }\n"
            "impl Printable for Bad { fn to_string(self) { 1 / 0 } }\n"
            "print(Bad {})\n"
        )
        with self.assertRaises(MahRuntimeError) as cm:
            run_source(src)
        message = str(cm.exception)
        self.assertIn("Division by zero", message)
        self.assertEqual(message.count("at position"), 1)


# ---------------------------------------------------------------------------
# 22. VM independence
# ---------------------------------------------------------------------------

class VmIndependenceTests(unittest.TestCase):
    def test_code_interpreter_never_imports_the_compiler_or_lsp(self):
        script = (
            "import mah.code_interpreter, sys\n"
            "bad = [m for m in sys.modules if m.startswith(('mah.compiler', 'mah.lsp')) "
            "or m == 'mah.preprocessor']\n"
            "assert not bad, bad\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=_REPO_ROOT, capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)


# ---------------------------------------------------------------------------
# 23-29. CLI
# ---------------------------------------------------------------------------

class CliTests(unittest.TestCase):
    def _run_main(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli_main(argv)
        return rc, out.getvalue()

    def test_build_creates_default_output_next_to_source(self):
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "prog.mh")
            with open(src, "w") as f:
                f.write('print("hi")')
            rc, _ = self._run_main(["build", src])
            self.assertEqual(rc, 0)
            self.assertTrue(os.path.exists(os.path.join(td, "prog.mahc")))

    def test_build_with_custom_out(self):
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "prog.mh")
            with open(src, "w") as f:
                f.write('print("hi")')
            out_dir = os.path.join(td, "out")
            os.makedirs(out_dir)
            custom = os.path.join(out_dir, "custom.mahc")
            rc, _ = self._run_main(["build", src, "-o", custom])
            self.assertEqual(rc, 0)
            self.assertTrue(os.path.exists(custom))

    def test_runc_output_matches_run(self):
        for name in ("traits.mh", "async_demo.mh"):
            with self.subTest(example=name):
                with tempfile.TemporaryDirectory() as td:
                    src = os.path.join(td, name)
                    with open(example_path(name)) as f:
                        content = f.read()
                    with open(src, "w") as f:
                        f.write(content)
                    mahc = os.path.join(td, name[:-3] + ".mahc")
                    self._run_main(["build", src])
                    _rc, runc_out = self._run_main(["runc", mahc])
                    _rc, run_out = self._run_main(["run", src])
                    self.assertEqual(runc_out, run_out)

    def test_bare_mahc_argument_runs_it(self):
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "prog.mh")
            with open(src, "w") as f:
                f.write('print("hi")')
            self._run_main(["build", src])
            mahc = os.path.join(td, "prog.mahc")
            rc, out = self._run_main([mahc])
            self.assertEqual(rc, 0)
            self.assertEqual(out, "hi\n")

    def test_dis_output_contains_expected_opcodes(self):
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "traits.mh")
            with open(example_path("traits.mh")) as f:
                content = f.read()
            with open(src, "w") as f:
                f.write(content)
            self._run_main(["build", src])
            mahc = os.path.join(td, "traits.mahc")
            _rc, out = self._run_main(["dis", mahc])
            self.assertIn("callmethod", out)
            # M16: `print(...)` now compiles to `io.write` (once per piece:
            # each argument, `sep`, and `end`), not `io.print` -- see
            # docs/MAHC_FORMAT.md #4.4/codegen.py's `_gen_print`.
            self.assertIn("io.write", out)

    def test_runc_on_garbage_file_exits_2(self):
        with tempfile.TemporaryDirectory() as td:
            garbage = os.path.join(td, "bad.mahc")
            with open(garbage, "wb") as f:
                f.write(b"not a mahc file")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = cli_main(["runc", garbage])
            self.assertEqual(rc, 2)
            self.assertIn("invalid .mahc file", err.getvalue())

    def test_build_release_has_no_debug_section(self):
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "prog.mh")
            with open(src, "w") as f:
                f.write('print("hi")')
            out_path = os.path.join(td, "prog.mahc")
            rc, _ = self._run_main(["build", src, "-o", out_path, "--target", "release"])
            self.assertEqual(rc, 0)
            with open(out_path, "rb") as f:
                data = f.read()
            self.assertIsNone(decode(data).debug)


# ---------------------------------------------------------------------------
# 30-35. Semantics fixed by the spec
# ---------------------------------------------------------------------------

class SemanticsTests(unittest.TestCase):
    def test_equality_is_strictly_typed(self):
        self.assertEqual(run_source("print(true == 1)"), "false\n")
        self.assertEqual(run_source("print(1 == 1.0)"), "true\n")
        self.assertEqual(run_source('print("a" < "b")'), "true\n")
        self.assertEqual(run_source("print(none == none)"), "true\n")
        self.assertEqual(run_source("print(some(1) == some(1))"), "false\n")

    def test_type_errors_raise_clean_messages(self):
        with self.assertRaises(MahRuntimeError) as cm:
            run_source("print(1 + true)")
        self.assertIn("Cannot apply '+' to Number and Bool", str(cm.exception))

        with self.assertRaises(MahRuntimeError) as cm:
            run_source("print(1 / 0)")
        self.assertIn("Division by zero", str(cm.exception))

        with self.assertRaises(MahRuntimeError) as cm:
            run_source('print(1 < "a")')
        self.assertIn("Cannot compare Number and String", str(cm.exception))

    def test_arithmetic_and_string_repeat(self):
        self.assertEqual(run_source("print(1.25 * 2)"), "2.5\n")
        self.assertEqual(run_source("print(0.1 + 0.2)"), "0.3\n")
        self.assertEqual(run_source("print(10 / 4)"), "2.5\n")
        self.assertEqual(run_source("print(7 // 2)"), "3\n")
        self.assertEqual(run_source("print(-7 // 2)"), "-3\n")
        self.assertEqual(run_source("print(-7 % 3)"), "-1\n")
        self.assertEqual(run_source('print("ab" * 3)'), "ababab\n")
        self.assertEqual(run_source('print(3 * "x")'), "xxx\n")
        self.assertEqual(run_source("print(sin(0))"), "0\n")
        self.assertEqual(run_source("print(2 ** 10)"), "1024\n")

    def test_struct_field_declaration_order_wins_over_literal_order(self):
        src = "struct P { x, y }\nprint(P { y: 2, x: 1 })"
        self.assertEqual(run_source(src), "P { x: 1, y: 2 }\n")

    def test_input_native(self):
        self.assertEqual(run_source("let a = input()\nprint(a + 1)", stdin="41\n"), "42\n")
        with self.assertRaises(MahRuntimeError) as cm:
            run_source("let a = input()\nprint(a + 1)", stdin="")
        self.assertIn("input: end of input", str(cm.exception))

    def test_function_printing(self):
        self.assertEqual(run_source("print(fn(x) { x })"), "<fn>\n")
        with tempfile.TemporaryDirectory() as td:
            lib_path = os.path.join(td, "lib.mh")
            main_path = os.path.join(td, "main.mh")
            with open(lib_path, "w") as f:
                f.write("export fn square(n) {\n    return n ** 2\n}\n")
            with open(main_path, "w") as f:
                f.write('import "lib.mh"\nprint(square)\n')
            self.assertEqual(run_file(main_path), "<fn square>\n")


class CliRuntimeErrorTests(unittest.TestCase):
    """Added during verification: a runtime error from `run`/`runc` is
    printed as `RuntimeError: <message>` on stderr with exit code 1, not as
    a Python traceback line naming `mah.runtime_values.MahRuntimeError`."""

    def test_runc_and_run_report_runtime_errors_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "prog.mh")
            with open(src, "w") as f:
                f.write('print("hi")\nlet s = 1\ns.nope()\n')
            self.assertEqual(cli_main(["build", src]), 0)
            for argv in (["runc", os.path.join(tmp, "prog.mahc")], ["run", src]):
                with self.subTest(argv=argv[0]):
                    out, err = io.StringIO(), io.StringIO()
                    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                        code = cli_main(argv)
                    self.assertEqual(code, 1)
                    self.assertEqual(out.getvalue(), "hi\n")
                    self.assertEqual(
                        err.getvalue(), "RuntimeError: 'Number' has no method 'nope' at position #3:3\n"
                    )


def _strip_section(data: bytes, section_id: int) -> bytes:
    """Remove one section (by id) from an already-encoded `.mahc` file,
    keeping everything else byte-for-byte -- used to build a "well-formed
    except this required section is missing" file out of a real compiled
    one, rather than hand-assembling the whole thing."""
    header, body = data[:8], data[8:]
    out = bytearray(header)
    pos = 0
    while pos < len(body):
        sec_id = body[pos]
        length, next_pos = read_varuint(body, pos + 1)
        end = next_pos + length
        if sec_id != section_id:
            out += body[pos:end]
        pos = end
    return bytes(out)


class ParamsAndKwargsBytecodeTests(unittest.TestCase):
    """M16: default parameter values + keyword-argument calls -- the
    PARAMS section (docs/MAHC_FORMAT.md #4.5a), `jmpset`, the `*kw`
    opcodes/operand kind `S*`, and minor-version gating (#7)."""

    def test_header_is_minor_1_and_params_section_has_names_and_defaults(self):
        data = compile_bytes(text="fn f(a, b = 1) { a }")
        # M17: the reference encoder always writes the CURRENT minor
        # version (now 3, since M19), regardless of which features a given
        # program actually uses -- this test's own name predates that bump
        # but still exercises exactly what it says (PARAMS names/defaults).
        self.assertEqual(data[:8], b"MAHC\x01\x00\x03\x00")
        program = decode(data)
        fn = next(
            f for f in program.functions if f.name is not None and program.strings[f.name] == "f"
        )
        resolved = [(program.strings[name_idx], has_default) for name_idx, has_default in fn.params]
        self.assertEqual(resolved, [("a", False), ("b", True)])

    def test_disasm_of_defaults_and_kwargs_contains_jmpset_and_callkw(self):
        program = compile_program(
            text=(
                "fn area(w, h = 1, scale = 1) { w * h * scale }\n"
                "print(area(2))\n"
                "print(area(2, scale: 3))\n"
            )
        )
        text = disassemble(program)
        self.assertIn("jmpset", text)
        self.assertIn("callkw", text)

    def test_hand_built_minor_0_program_without_params_runs(self):
        program = _minimal_program(
            [
                Instr("loadk", (0, (0, 0))),
                Instr("native", (0, ((0, 0),), None)),
                Instr("halt", ()),
            ],
            strings=["hi", "io.print"],
            constants=[Const(5, 0)],
            natives=[NativeRef(1, 1)],
        )
        self.assertIsNone(program.functions[0].params)
        data = encode(program)
        self.assertEqual(data[:8], b"MAHC\x01\x00\x00\x00")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            run_bytes(data)
        self.assertEqual(out.getvalue(), "hi\n")

    def test_minor_0_program_with_callkw_is_rejected(self):
        program = _minimal_program([Instr("callkw", ((0, 0), (), ())), Instr("halt", ())])
        with self.assertRaises(MahcFormatError) as cm:
            decode(encode(program))
        self.assertIn("callkw", str(cm.exception))

    def test_minor_0_program_with_io_write_native_is_rejected(self):
        program = _minimal_program(
            [Instr("native", (0, ((0, 0),), None)), Instr("halt", ())],
            strings=["hi", "io.write"],
            constants=[Const(5, 0)],
            natives=[NativeRef(1, 1)],
        )
        with self.assertRaises(MahcFormatError) as cm:
            decode(encode(program))
        self.assertIn("io.write", str(cm.exception))

    def test_minor_1_file_missing_params_section_is_rejected(self):
        data = compile_bytes(text="print(1)")
        stripped = _strip_section(data, SEC_PARAMS)
        with self.assertRaises(MahcFormatError) as cm:
            decode(stripped)
        self.assertIn("0x07", str(cm.exception))

    def test_round_trip_still_holds_for_every_example(self):
        # M16: every example is now compiled/round-tripped at minor 1
        # (PARAMS section, possibly `jmpset`/`callkw` if it uses defaults
        # or keyword arguments) -- RoundTripTests above already exercises
        # this on every `make test` run; this just names the requirement
        # explicitly for this milestone.
        for name in _EXAMPLE_FILES:
            with self.subTest(example=name):
                program = compile_program(path=example_path(name))
                self.assertEqual(decode(encode(program)), program)


if __name__ == "__main__":
    unittest.main()
