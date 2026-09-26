"""The native Rust runtime (`runtime/`, `mah-vm`) and what the CLI builds
on it: `--vm rust`, `build --self-contained` bundles, and `mah dis` on
them -- see docs/RUST_VM.md.

`BundleTests` need no Rust at all: they bundle a stand-in shell script as
the "runtime", which is enough to exercise the header, the `/bin/sh` stub
(unpacking, caching, the platform check) and every reader. `RustVmTests`
need the real binary: they build it with cargo when it isn't built yet and
skip only when cargo isn't installed. (`make test-rust` additionally runs
this whole suite's programs on the Rust VM.)
"""

from __future__ import annotations

import contextlib
import io
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mah import rust_vm
from mah.bytecode import bundle
from mah.bytecode.decode import decode
from mah.bytecode.format import MahcFormatError
from mah.cli.main import main as cli_main
from mah.runtime_values import MahRuntimeError
from tests.support import EXAMPLES_DIR, compile_bytes, example_path, run_source

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_POSIX = os.name == "posix"


def _host_target() -> str:
    """The `<arch>-<os>` name the bundle stub computes for this machine."""
    arch = {"arm64": "aarch64", "amd64": "x86_64"}.get(platform.machine().lower(), platform.machine())
    system = platform.system().lower()
    return f"{arch}-{'macos' if system == 'darwin' else system}"


def _run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli_main(argv)
    return rc, out.getvalue(), err.getvalue()


# A stand-in runtime: reports how the stub invoked it.
_FAKE_VM = b'#!/bin/sh\necho "fake vm: $1 $2"\n'


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.dir = self.td.name
        self.mahc = compile_bytes(text='print("hi")')

    def tearDown(self):
        self.td.cleanup()

    def _write_bundle(self, target=None, vm=_FAKE_VM):
        path = os.path.join(self.dir, "prog")
        with open(path, "wb") as f:
            f.write(bundle.build(vm, "9.9.9", target or _host_target(), self.mahc))
        os.chmod(path, 0o755)
        return path

    def test_split_round_trips(self):
        data = bundle.build(_FAKE_VM, "9.9.9", "x86_64-linux", self.mahc)
        info, mahc = bundle.split(data)
        self.assertEqual(mahc, self.mahc)
        self.assertEqual((info.vm_version, info.vm_target, info.vm_size), ("9.9.9", "x86_64-linux", len(_FAKE_VM)))

    def test_plain_files_are_not_bundles(self):
        self.assertEqual(bundle.split(self.mahc), (None, self.mahc))

    def test_decode_reads_the_bytecode_inside_a_bundle(self):
        data = bundle.build(_FAKE_VM, "9.9.9", "x86_64-linux", self.mahc)
        self.assertEqual(decode(data), decode(self.mahc))

    def test_truncated_bundle_is_invalid(self):
        data = bundle.build(_FAKE_VM, "9.9.9", "x86_64-linux", self.mahc)
        with self.assertRaises(MahcFormatError) as cm:
            decode(data[:-1])
        self.assertIn("invalid self-contained bundle", str(cm.exception))

    def test_runc_runs_a_bundle_on_the_python_vm(self):
        rc, out, _err = _run_main(["runc", self._write_bundle()])
        self.assertEqual((rc, out), (0, "hi\n"))

    def test_dis_shows_the_bundled_runtime(self):
        rc, out, _err = _run_main(["dis", self._write_bundle(target="x86_64-linux")])
        self.assertEqual(rc, 0)
        lines = out.splitlines()
        self.assertEqual(lines[0], "MAHC version 1.3")
        self.assertEqual(
            lines[1], f"runtime: rust mah-vm 9.9.9 (x86_64-linux), self-contained, {len(_FAKE_VM)} bytes"
        )
        self.assertIn("io.write", out)

    def test_dis_on_plain_bytecode_says_no_runtime(self):
        path = os.path.join(self.dir, "prog.mahc")
        with open(path, "wb") as f:
            f.write(self.mahc)
        rc, out, _err = _run_main(["dis", path])
        self.assertEqual(rc, 0)
        self.assertEqual(out.splitlines()[1], "runtime: none (plain bytecode, runs on an installed mah)")

    @unittest.skipUnless(_POSIX, "the bundle stub is a /bin/sh script")
    def test_stub_unpacks_the_runtime_once_and_execs_it(self):
        path = self._write_bundle()
        env = dict(os.environ, XDG_CACHE_HOME=os.path.join(self.dir, "cache"))
        for _ in range(2):  # the second run uses the cached copy
            result = subprocess.run([path], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertEqual(result.stdout, f"fake vm: run {path}\n")
        vm_dirs = os.listdir(os.path.join(self.dir, "cache", "mah", "vm"))
        self.assertEqual(len(vm_dirs), 1)
        with open(os.path.join(self.dir, "cache", "mah", "vm", vm_dirs[0], "mah-vm"), "rb") as f:
            self.assertEqual(f.read(), _FAKE_VM)

    @unittest.skipUnless(_POSIX, "the bundle stub is a /bin/sh script")
    def test_stub_refuses_another_platform(self):
        path = self._write_bundle(target="sparc-plan9")
        env = dict(os.environ, XDG_CACHE_HOME=os.path.join(self.dir, "cache"))
        result = subprocess.run([path], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 126)
        self.assertIn("this program was built for sparc-plan9", result.stderr)

    def test_self_contained_build_without_a_runtime_fails_cleanly(self):
        src = os.path.join(self.dir, "prog.mh")
        with open(src, "w") as f:
            f.write('print("hi")')
        old = os.environ.get("MAH_VM")
        os.environ["MAH_VM"] = os.path.join(self.dir, "missing")
        try:
            rc, _out, err = _run_main(["build", "--self-contained", src])
        finally:
            if old is None:
                del os.environ["MAH_VM"]
            else:
                os.environ["MAH_VM"] = old
        self.assertEqual(rc, 2)
        self.assertIn("MAH_VM points to", err)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "prog.mahc")))


class PowZeroTests(unittest.TestCase):
    def test_zero_to_a_negative_power_is_division_by_zero(self):
        # `decimal` alone would give an unprintable Infinity
        with self.assertRaises(MahRuntimeError) as cm:
            run_source("print(0 ** (0 - 1))")
        self.assertIn("Division by zero", str(cm.exception))


class BigNumberPrintingTests(unittest.TestCase):
    def test_huge_integers_print_every_digit(self):
        # formatted from the Decimal itself: Python's int -> str conversion
        # refuses past 4300 digits
        self.assertEqual(run_source("print(10 ** 5000)"), "1" + "0" * 5000 + "\n")
        self.assertEqual(run_source("print(0 - 2 ** 100)"), "-1267650600228229401496703205000\n")


def _ensure_vm() -> str | None:
    """The mah-vm to test: rebuilt first whenever cargo is available (a
    no-op when it's up to date), so these tests never run a stale binary."""
    if shutil.which("cargo") is not None and "MAH_VM" not in os.environ:
        subprocess.run(["cargo", "build", "--release"], cwd=os.path.join(_REPO_ROOT, "runtime"), check=True,
                       capture_output=True)
    try:
        return rust_vm.find_vm()
    except rust_vm.RustVmNotFound:
        return None


# stdin for the examples that read a number
_EXAMPLE_STDIN = {"decimal_to_binary.mh": "13\n", "new_decimal_to_binary.mh": "13\n", "binary_to_decimal.mh": "1101\n"}


@unittest.skipUnless(_POSIX, "runs executables")
class RustVmTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.vm = _ensure_vm()
        if cls.vm is None:
            raise unittest.SkipTest("mah-vm isn't built and cargo isn't installed")

    def _mah(self, *args, stdin=""):
        return subprocess.run(
            [sys.executable, "-m", "mah", *args], cwd=_REPO_ROOT, input=stdin, capture_output=True, text=True
        )

    def test_version(self):
        version, target = rust_vm.vm_version(self.vm)
        self.assertRegex(version, r"^\d+\.\d+\.\d+$")
        self.assertEqual(target, _host_target())

    def test_every_example_runs_the_same_on_both_vms(self):
        for name in sorted(f for f in os.listdir(EXAMPLES_DIR) if f.endswith(".mh")):
            with self.subTest(example=name):
                stdin = _EXAMPLE_STDIN.get(name, "")
                py = self._mah("run", example_path(name), stdin=stdin)
                rs = self._mah("run", "--vm", "rust", example_path(name), stdin=stdin)
                self.assertEqual((rs.returncode, rs.stdout, rs.stderr), (py.returncode, py.stdout, py.stderr))

    def test_runtime_errors_match(self):
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "prog.mh")
            with open(src, "w") as f:
                f.write('print("before")\nlet v = [1, 2]\nv.push(1, 2)\n')
            mahc = os.path.join(td, "prog.mahc")
            self.assertEqual(self._mah("build", src, "-o", mahc).returncode, 0)
            py = self._mah("runc", mahc)
            rs = self._mah("runc", "--vm", "rust", mahc)
            self.assertEqual(py.returncode, 1)
            self.assertEqual((rs.returncode, rs.stdout, rs.stderr), (py.returncode, py.stdout, py.stderr))

    def test_self_contained_executable(self):
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "prog.mh")
            with open(src, "w") as f:
                f.write('print("hello from a bundle", 6 * 7)\n')
            out = os.path.join(td, "prog")
            built = self._mah("build", "--self-contained", src, "-o", out)
            self.assertEqual(built.returncode, 0, msg=built.stderr)
            self.assertTrue(os.access(out, os.X_OK))
            # no mah on PATH, a fresh cache: only the file itself is needed
            env = {"PATH": "/usr/bin:/bin", "XDG_CACHE_HOME": os.path.join(td, "cache"), "HOME": td}
            result = subprocess.run([out], env=env, capture_output=True, text=True)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "hello from a bundle 42\n", ""))
            version, target = rust_vm.vm_version(self.vm)
            dis = self._mah("dis", out)
            self.assertIn(f"runtime: rust mah-vm {version} ({target}), self-contained", dis.stdout)
            # the Python VM can still run it, and so can `mah-vm run` directly
            self.assertEqual(self._mah("runc", out).stdout, "hello from a bundle 42\n")


if __name__ == "__main__":
    unittest.main()
