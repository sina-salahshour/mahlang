"""Tests for M22's wiring around the static type checker (see
docs/TYPES.md): the `[types] check` manifest key (mah/project/manifest.py),
`mah check`/compile-time errors in strict/explicit (mah/cli/main.py,
mah/compiler/driver.py), and LSP diagnostics (mah/lsp/analysis.py).

The checker itself (mah/compiler/typecheck.py) is developed separately and
is mocked throughout via `unittest.mock.patch("mah.compiler.typecheck.
check_program", ...)` -- see the module docstring's note that every call
site must go through `typecheck.check_program(...)` (never `from .typecheck
import check_program`) so this patching works. One test at the very end
uses the real checker, unmocked, as an end-to-end smoke test of the wiring.
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mah.bytecode.lower import line_col
from mah.cli.main import main as cli_main
from mah.compiler import driver, typecheck
from mah.compiler.typecheck import TypeDiagnostic
from mah.lsp import analysis
from mah.project.manifest import MahProjectError, check_level_for, find_manifest, load_project


def _run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli_main(argv)
    return rc, out.getvalue(), err.getvalue()


@contextlib.contextmanager
def _chdir(path):
    old = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)


def _diagnostics(src: str, mismatch_token: str, implicit_token: str) -> list[TypeDiagnostic]:
    """A "mismatch" and an "implicit" diagnostic, positioned at real tokens
    in `src` (found with `src.index`, as the milestone spec asks)."""
    return [
        TypeDiagnostic("mismatch", "expected Number, found String", src.index(mismatch_token)),
        TypeDiagnostic("implicit", "can't infer the type of 'x'; annotate it", src.index(implicit_token)),
    ]


def _located(src: str, token: str) -> str:
    """`#line:col` for `token`'s first occurrence in `src` -- the label a
    diagnostic at that position should be rendered with."""
    line, col = line_col(src, src.index(token))
    return f"#{line}:{col}"


SRC = "let x = 1\nprint(x)\n"


# ---------------------------------------------------------------------------
# manifest: [types] check
# ---------------------------------------------------------------------------

class ManifestTypesTests(unittest.TestCase):
    def _write(self, td, text):
        path = os.path.join(td, "mah-project.toml")
        with open(path, "w") as f:
            f.write(text)
        return path

    def test_default_is_loose(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._write(td, '[package]\nname = "x"\nversion = "1"\n')
            project = load_project(path)
            self.assertEqual(project.type_check, "loose")

    def test_each_valid_level(self):
        for level in typecheck.CHECK_LEVELS:
            with tempfile.TemporaryDirectory() as td:
                path = self._write(
                    td, f'[package]\nname = "x"\nversion = "1"\n\n[types]\ncheck = "{level}"\n'
                )
                project = load_project(path)
                self.assertEqual(project.type_check, level)

    def test_bad_value(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._write(td, '[package]\nname = "x"\nversion = "1"\n\n[types]\ncheck = "fast"\n')
            with self.assertRaises(MahProjectError) as cm:
                load_project(path)
            self.assertIn('types.check must be "loose", "strict" or "explicit"', str(cm.exception))
            self.assertTrue(str(cm.exception).startswith(f"{path}: "))

    def test_unknown_key(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._write(td, '[package]\nname = "x"\nversion = "1"\n\n[types]\nlevel = "strict"\n')
            with self.assertRaises(MahProjectError) as cm:
                load_project(path)
            self.assertIn("unknown key 'types.level'", str(cm.exception))

    def test_non_table(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._write(td, 'types = "strict"\n\n[package]\nname = "x"\nversion = "1"\n')
            with self.assertRaises(MahProjectError) as cm:
                load_project(path)
            self.assertIn("types must be a table, written [types]", str(cm.exception))


class CheckLevelForTests(unittest.TestCase):
    def test_no_path_is_loose(self):
        self.assertEqual(check_level_for(None), "loose")

    def test_no_manifest_is_loose(self):
        with tempfile.TemporaryDirectory() as td:
            real_td = os.path.realpath(td)
            if find_manifest(real_td) is not None:
                self.skipTest("a mah-project.toml exists above the temp dir")
            path = os.path.join(td, "main.mh")
            open(path, "w").close()
            self.assertEqual(check_level_for(path), "loose")

    def test_nested_file_in_project_honors_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "mah-project.toml"), "w") as f:
                f.write('[package]\nname = "x"\nversion = "1"\n\n[types]\ncheck = "strict"\n')
            nested = os.path.join(td, "src", "deeper")
            os.makedirs(nested)
            path = os.path.join(nested, "file.mh")
            open(path, "w").close()
            self.assertEqual(check_level_for(path), "strict")

    def test_invalid_manifest_is_loose(self):
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "mah-project.toml"), "w") as f:
                f.write("this is not [valid toml")
            path = os.path.join(td, "main.mh")
            open(path, "w").close()
            self.assertEqual(check_level_for(path), "loose")


# ---------------------------------------------------------------------------
# driver: compile_to_program/compile_to_bytes `check=`
# ---------------------------------------------------------------------------

class DriverCheckTests(unittest.TestCase):
    def test_loose_never_calls_the_checker(self):
        with mock.patch("mah.compiler.typecheck.check_program") as checker:
            driver.compile_to_bytes(text=SRC, check="loose")
            checker.assert_not_called()

    def test_strict_raises_with_located_mismatch_only(self):
        diags = _diagnostics(SRC, "1", "x")
        with mock.patch("mah.compiler.typecheck.check_program", return_value=diags):
            with self.assertRaises(SyntaxError) as cm:
                driver.compile_to_bytes(text=SRC, check="strict")
        message = str(cm.exception)
        located = _located(SRC, "1")
        self.assertIn(f"type error: expected Number, found String at position {located}", message)
        self.assertNotIn("annotate it", message)

    def test_explicit_includes_both_diagnostics(self):
        diags = _diagnostics(SRC, "1", "x")
        with mock.patch("mah.compiler.typecheck.check_program", return_value=diags):
            with self.assertRaises(SyntaxError) as cm:
                driver.compile_to_bytes(text=SRC, check="explicit")
        message = str(cm.exception)
        self.assertIn(f"type error: expected Number, found String at position {_located(SRC, '1')}", message)
        self.assertIn(
            f"type error: can't infer the type of 'x'; annotate it at position {_located(SRC, 'x')}", message
        )
        self.assertEqual(len(message.splitlines()), 2)

    def test_errors_real_checker_at_strict(self):
        # M26: an unhandled error fails a strict build; a catch arm for an
        # error that's never thrown is only a warning, so it doesn't.
        prelude = "enum A { X }\nimpl Error for A {}\nstruct B { }\nimpl Error for B {}\nfn f() { throw A.X }\n"
        with self.assertRaises(SyntaxError) as cm:
            driver.compile_to_bytes(text=prelude + "f()\n", check="strict")
        self.assertIn("type error: Unhandled error: A", str(cm.exception))
        data = driver.compile_to_bytes(
            text=prelude + "try { f() } catch { e: A => { 1 }\ne: B => { 2 } }\n", check="strict"
        )
        self.assertIsInstance(data, bytes)

    def test_empty_diagnostics_compiles_fine_at_strict(self):
        with mock.patch("mah.compiler.typecheck.check_program", return_value=[]):
            data = driver.compile_to_bytes(text=SRC, check="strict")
        self.assertIsInstance(data, bytes)
        self.assertGreater(len(data), 0)


# ---------------------------------------------------------------------------
# CLI: `mah check`, `mah run`
# ---------------------------------------------------------------------------

class CliCheckTests(unittest.TestCase):
    def _init_project(self, td):
        with _chdir(td):
            _run_main(["init", "."])

    def _set_level(self, td, level):
        path = os.path.join(td, "mah-project.toml")
        with open(path) as f:
            text = f.read()
        self.assertIn('check = "loose"', text)
        with open(path, "w") as f:
            f.write(text.replace('check = "loose"', f'check = "{level}"'))

    def _main_src(self, td):
        with open(os.path.join(td, "src", "main.mh")) as f:
            return f.read()

    def test_check_at_loose(self):
        with tempfile.TemporaryDirectory() as td:
            self._init_project(td)
            src = self._main_src(td)
            diags = _diagnostics(src, "name", "greet")
            with mock.patch("mah.compiler.typecheck.check_program", return_value=diags):
                with _chdir(td):
                    rc, out, _err = _run_main(["check"])
            self.assertEqual(rc, 0)
            self.assertIn(f"warning: expected Number, found String at position {_located(src, 'name')}", out)
            self.assertNotIn("annotate it", out)
            self.assertIn("1 warning", out)

    def test_check_at_strict(self):
        with tempfile.TemporaryDirectory() as td:
            self._init_project(td)
            self._set_level(td, "strict")
            src = self._main_src(td)
            diags = _diagnostics(src, "name", "greet")
            with mock.patch("mah.compiler.typecheck.check_program", return_value=diags):
                with _chdir(td):
                    rc, out, _err = _run_main(["check"])
            self.assertEqual(rc, 1)
            self.assertIn(f"error: expected Number, found String at position {_located(src, 'name')}", out)
            self.assertNotIn("annotate it", out)
            self.assertIn("1 error", out)

    def test_check_at_explicit(self):
        with tempfile.TemporaryDirectory() as td:
            self._init_project(td)
            self._set_level(td, "explicit")
            src = self._main_src(td)
            diags = _diagnostics(src, "name", "greet")
            with mock.patch("mah.compiler.typecheck.check_program", return_value=diags):
                with _chdir(td):
                    rc, out, _err = _run_main(["check"])
            self.assertEqual(rc, 1)
            self.assertIn(f"error: expected Number, found String at position {_located(src, 'name')}", out)
            self.assertIn("annotate it", out)
            self.assertIn("2 errors", out)

    def test_check_level_flag_overrides_project(self):
        with tempfile.TemporaryDirectory() as td:
            self._init_project(td)  # stays "loose" in the manifest
            src = self._main_src(td)
            diags = _diagnostics(src, "name", "greet")
            with mock.patch("mah.compiler.typecheck.check_program", return_value=diags):
                with _chdir(td):
                    rc, out, _err = _run_main(["check", "--level", "strict"])
            self.assertEqual(rc, 1)
            self.assertIn("error: expected Number, found String", out)

    def test_run_in_strict_project_raises_with_located_message(self):
        with tempfile.TemporaryDirectory() as td:
            self._init_project(td)
            self._set_level(td, "strict")
            src = self._main_src(td)
            diags = _diagnostics(src, "name", "greet")
            with mock.patch("mah.compiler.typecheck.check_program", return_value=diags):
                with _chdir(td):
                    with self.assertRaises(SyntaxError) as cm:
                        _run_main(["run"])
            message = str(cm.exception)
            self.assertIn(
                f"type error: expected Number, found String at position {_located(src, 'name')}", message
            )

    def test_run_in_loose_project_runs_normally(self):
        with tempfile.TemporaryDirectory() as td:
            self._init_project(td)
            with _chdir(td):
                rc, out, _err = _run_main(["run"])
            self.assertEqual(rc, 0)
            self.assertTrue(out.startswith("Hello, "))


class RealCheckerSmokeTest(unittest.TestCase):
    """No mock: the real checker (still being developed alongside this
    file) wired all the way through `mah check` on a trivial, valid
    program at the default "loose" level."""

    def test_mah_check_no_type_errors(self):
        with tempfile.TemporaryDirectory() as td:
            with _chdir(td):
                _run_main(["init", "."])
                rc, out, _err = _run_main(["check"])
            self.assertEqual(rc, 0)
            self.assertIn("no type errors", out)


# ---------------------------------------------------------------------------
# LSP diagnostics
# ---------------------------------------------------------------------------

class LspDiagnosticsTests(unittest.TestCase):
    def _project_file(self, td, level=None):
        with open(os.path.join(td, "mah-project.toml"), "w") as f:
            check_line = f'\n[types]\ncheck = "{level}"\n' if level else ""
            f.write(f'[package]\nname = "x"\nversion = "1"\n{check_line}')
        src_dir = os.path.join(td, "src")
        os.makedirs(src_dir)
        path = os.path.join(src_dir, "main.mh")
        with open(path, "w") as f:
            f.write(SRC)
        return path

    def test_loose_reports_only_mismatch_as_warning(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._project_file(td)  # default "loose"
            diags = _diagnostics(SRC, "1", "x")
            with mock.patch("mah.compiler.typecheck.check_program", return_value=diags):
                result = analysis.get_diagnostics(SRC, path)
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["severity"], analysis.SEVERITY_WARNING)
            self.assertEqual(result[0]["message"], "expected Number, found String")

    def test_strict_reports_mismatch_as_error(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._project_file(td, level="strict")
            diags = _diagnostics(SRC, "1", "x")
            with mock.patch("mah.compiler.typecheck.check_program", return_value=diags):
                result = analysis.get_diagnostics(SRC, path)
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["severity"], analysis.SEVERITY_ERROR)

    def test_explicit_reports_both_as_errors(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._project_file(td, level="explicit")
            diags = _diagnostics(SRC, "1", "x")
            with mock.patch("mah.compiler.typecheck.check_program", return_value=diags):
                result = analysis.get_diagnostics(SRC, path)
            self.assertEqual(len(result), 2)
            for d in result:
                self.assertEqual(d["severity"], analysis.SEVERITY_ERROR)

    def test_range_covers_the_right_token(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._project_file(td)
            diags = _diagnostics(SRC, "1", "x")
            with mock.patch("mah.compiler.typecheck.check_program", return_value=diags):
                result = analysis.get_diagnostics(SRC, path)
            mismatch_pos = SRC.index("1")
            expected = analysis.make_range(SRC, mismatch_pos, mismatch_pos + 1)
            self.assertEqual(result[0]["range"], expected)


if __name__ == "__main__":
    unittest.main()
