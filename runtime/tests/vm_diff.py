#!/usr/bin/env python3
"""Differential test: every `examples/*.mh` file, plus a batch of small
inline programs covering runtime-error message paths, plus a batch of
malformed `.mahc` files (built by editing bytes of a compiled file, like
`tests/test_bytecode.py` does) -- run through both `python3 -m mah runc`
and `runtime/target/release/mah-vm run` with the same stdin, and compare
exit code, stdout, and stderr exactly.

Usage (from the repo root or anywhere):
    python3 runtime/tests/vm_diff.py [--vm PATH] [-v]

This is the real acceptance test for the Rust VM port (see the spec this
was written from: runtime/src/{decode,vm,bundle,main}.rs must behave
identically to `mah/code_interpreter.py` + `mah/bytecode/decode.py` +
`mah/natives.py`). Python 3 stdlib only -- no pytest/unittest dependency
(just enough structure to print a pass/fail summary and every mismatch).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EXAMPLES_DIR = os.path.join(REPO_ROOT, "examples")
DEFAULT_VM = os.path.join(REPO_ROOT, "runtime", "target", "release", "mah-vm")

# Example programs that call `input()` at least once -- fed "12\n" on
# stdin; everything else gets empty stdin.
EXAMPLES_NEEDING_INPUT = {
    "binary_to_decimal.mh",
    "decimal_to_binary.mh",
    "new_decimal_to_binary.mh",
}


class Result:
    def __init__(self, name: str):
        self.name = name
        self.passed = True
        self.detail = ""

    def fail(self, detail: str) -> None:
        self.passed = False
        self.detail = detail


def run_python_mahc(mahc_path: str, stdin_data: bytes) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "mah", "runc", mahc_path],
        input=stdin_data,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": REPO_ROOT},
        timeout=30,
    )


def run_rust_mahc(vm_path: str, mahc_path: str, stdin_data: bytes) -> subprocess.CompletedProcess:
    return subprocess.run(
        [vm_path, "run", mahc_path],
        input=stdin_data,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=REPO_ROOT,
        timeout=30,
    )


def compare(name: str, py: subprocess.CompletedProcess, rs: subprocess.CompletedProcess) -> Result:
    r = Result(name)
    problems = []
    if py.returncode != rs.returncode:
        problems.append(f"exit code: python={py.returncode} rust={rs.returncode}")
    if py.stdout != rs.stdout:
        problems.append(f"stdout differs:\n  python={py.stdout!r}\n  rust  ={rs.stdout!r}")
    if py.stderr != rs.stderr:
        problems.append(f"stderr differs:\n  python={py.stderr!r}\n  rust  ={rs.stderr!r}")
    if problems:
        r.fail("; ".join(problems))
    return r


def compile_example(path: str, tmpdir: str) -> str:
    out = os.path.join(tmpdir, os.path.basename(path) + ".mahc")
    subprocess.run(
        [sys.executable, "-m", "mah", "build", path, "-o", out],
        check=True,
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": REPO_ROOT},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return out


def compile_source(text: str, tmpdir: str, name: str, target: str = "debug") -> str:
    src = os.path.join(tmpdir, f"{name}.mh")
    with open(src, "w") as f:
        f.write(text)
    out = os.path.join(tmpdir, f"{name}.mahc")
    subprocess.run(
        [sys.executable, "-m", "mah", "build", src, "-o", out, "--target", target],
        check=True,
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": REPO_ROOT},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return out


def run_examples(vm_path: str, tmpdir: str, verbose: bool) -> list[Result]:
    results = []
    for fname in sorted(f for f in os.listdir(EXAMPLES_DIR) if f.endswith(".mh")):
        path = os.path.join(EXAMPLES_DIR, fname)
        try:
            mahc = compile_example(path, tmpdir)
        except subprocess.CalledProcessError as e:
            r = Result(fname)
            r.fail(f"failed to compile: {e.stderr.decode('utf-8', 'replace')}")
            results.append(r)
            continue
        stdin_data = b"12\n" if fname in EXAMPLES_NEEDING_INPUT else b""
        py = run_python_mahc(mahc, stdin_data)
        rs = run_rust_mahc(vm_path, mahc, stdin_data)
        r = compare(fname, py, rs)
        results.append(r)
        if verbose:
            print(f"  {'ok' if r.passed else 'FAIL'}  {fname}")
    return results


# ---------------------------------------------------------------------------
# ~30 small inline programs covering runtime-error message paths.
# ---------------------------------------------------------------------------

RUNTIME_ERROR_PROGRAMS: list[tuple[str, str, bytes]] = [
    ("call_too_few_args", """
fn f(a, b) { return a + b }
f(1)
""", b""),
    ("call_too_many_args", """
fn f(a, b) { return a + b }
f(1, 2, 3)
""", b""),
    ("call_non_function", """
let x = 5
x()
""", b""),
    ("detach_non_function", """
let x = 5
detach x()
""", b""),
    ("kwargs_unexpected", """
fn f(a) { return a }
f(a: 1, b: 2)
""", b""),
    ("kwargs_multiple_values", """
fn f(a) { return a }
f(1, a: 2)
""", b""),
    ("kwargs_missing_required", """
fn f(a, b) { return a + b }
f(a: 1)
""", b""),
    ("kwargs_too_many_positional", """
fn f(a, b = 2) { return a + b }
f(1, 2, 3)
""", b""),
    ("method_no_such_method", """
struct Point { x, y }
let p = Point { x: 1, y: 2 }
p.frobnicate()
""", b""),
    ("method_field_not_function", """
struct Box { value }
let b = Box { value: 5 }
b.value()
""", b""),
    ("static_function_as_method", """
struct S {}
trait T { fn f() {} }
impl T for S { fn f() { return 1 } }
let s = S {}
s.f()
""", b""),
    ("ambiguous_trait_method", """
struct S {}
trait A { fn go(self) {} }
trait B { fn go(self) {} }
impl A for S { fn go(self) { return 1 } }
impl B for S { fn go(self) { return 2 } }
let s = S {}
s.go()
""", b""),
    ("no_field", """
struct Point { x, y }
let p = Point { x: 1, y: 2 }
print(p.z)
""", b""),
    ("setfield_no_field", """
struct Point { x, y }
let p = Point { x: 1, y: 2 }
p.z = 3
""", b""),
    ("getfield_non_struct", """
let x = 5
print(x.y)
""", b""),
    ("division_by_zero", """
let x = 1 / 0
""", b""),
    ("idiv_by_zero", """
let x = 1 // 0
""", b""),
    ("mod_by_zero", """
let x = 1 % 0
""", b""),
    ("pow_zero_neg", """
let n = -1
let x = 0 ** n
""", b""),
    ("add_type_mismatch", """
struct S {}
let x = S {} + 1
""", b""),
    ("compare_type_mismatch", """
struct S {}
let x = S {} < 1
""", b""),
    ("negate_non_number", """
let x = -"hi"
""", b""),
    ("char_at_out_of_range", """
let s = "hi"
print(s.char_at(10))
""", b""),
    ("char_at_bad_type", """
let s = "hi"
print(s.char_at("x"))
""", b""),
    ("map_key_bad_type", """
let m = [:]
struct S {}
m[S {}] = 1
""", b""),
    ("vector_index_assign_out_of_range", """
let v = [1, 2]
v[10] = 5
""", b""),
    ("await_non_promise", """
let x = 5
let y = x.await
""", b""),
    ("match_fail", """
let x = 5
match x {
    100 => { print("no") }
}
""", b""),
    ("printable_returns_non_string", """
struct S {}
impl Printable for S {
    fn to_string(self) { return 5 }
}
print(S {})
""", b""),
    ("nested_error_in_to_string", """
struct S {}
impl Printable for S {
    fn to_string(self) { return 1 / 0 }
}
print(S {})
""", b""),
    ("error_inside_detach", """
fn boom() { return 1 / 0 }
let p = detach boom()
""", b""),
    ("input_end_of_input", """
let x = input()
""", b""),
    ("vector_slice_assign", """
let v = [1, 2, 3]
v[0..1] = [9]
""", b""),
]


def run_inline_programs(vm_path: str, tmpdir: str, verbose: bool) -> list[Result]:
    results = []
    for name, source, stdin_data in RUNTIME_ERROR_PROGRAMS:
        try:
            mahc = compile_source(source, tmpdir, name)
        except subprocess.CalledProcessError as e:
            r = Result(name)
            r.fail(f"failed to compile: {e.stderr.decode('utf-8', 'replace')}")
            results.append(r)
            continue
        py = run_python_mahc(mahc, stdin_data)
        rs = run_rust_mahc(vm_path, mahc, stdin_data)
        r = compare(name, py, rs)
        results.append(r)
        if verbose:
            print(f"  {'ok' if r.passed else 'FAIL'}  {name}")
    return results


# ---------------------------------------------------------------------------
# Malformed files -- built by editing bytes of a compiled file.
# ---------------------------------------------------------------------------

def build_malformed_cases(tmpdir: str) -> list[tuple[str, bytes]]:
    good_path = compile_source("print(1)\n", tmpdir, "good_for_malformed")
    with open(good_path, "rb") as f:
        good = f.read()
    # Strip the shebang line so byte offsets below are predictable.
    nl = good.index(b"\n")
    body = good[nl + 1 :]

    cases = []
    cases.append(("bad_magic", b"XXXX" + body[4:]))
    major_bad = bytearray(body)
    major_bad[4:6] = (99).to_bytes(2, "little")
    cases.append(("bad_major_version", bytes(major_bad)))
    minor_bad = bytearray(body)
    minor_bad[6:8] = (999).to_bytes(2, "little")
    cases.append(("bad_minor_version", bytes(minor_bad)))
    cases.append(("truncated_at_10_bytes", body[:10]))
    cases.append(("truncated_at_magic", body[:2]))
    cases.append(("empty_file", b""))

    # Unknown opcode: find the CODE section (id 0x06) and flip its first
    # instruction's opcode byte to an unused value (0x7E, never assigned).
    idx = body.index(bytes([0x06]))
    # section header is (id u8, length varuint); payload starts after that.
    # Since this file is tiny, the length varuint is a single byte.
    length = body[idx + 1]
    payload_start = idx + 2
    corrupted = bytearray(body)
    # payload = count varuint (1 byte, since few instructions) + opcodes...
    opcode_pos = payload_start + 1
    corrupted[opcode_pos] = 0x7E
    cases.append(("unknown_opcode", bytes(corrupted)))
    _ = length
    return cases


def run_malformed_cases(vm_path: str, tmpdir: str, verbose: bool) -> list[Result]:
    results = []
    for name, data in build_malformed_cases(tmpdir):
        path = os.path.join(tmpdir, f"malformed_{name}.mahc")
        with open(path, "wb") as f:
            f.write(data)
        py = run_python_mahc(path, b"")
        rs = run_rust_mahc(vm_path, path, b"")
        r = compare(f"malformed:{name}", py, rs)
        results.append(r)
        if verbose:
            print(f"  {'ok' if r.passed else 'FAIL'}  malformed:{name}")
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vm", default=DEFAULT_VM, help="path to the mah-vm binary")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if not os.path.isfile(args.vm):
        print(f"error: mah-vm binary not found at {args.vm} (build it with `cargo build --release`)", file=sys.stderr)
        return 2

    all_results: list[Result] = []
    with tempfile.TemporaryDirectory() as tmpdir:
        print("== examples/*.mh ==")
        all_results += run_examples(args.vm, tmpdir, args.verbose)
        print("== inline runtime-error programs ==")
        all_results += run_inline_programs(args.vm, tmpdir, args.verbose)
        print("== malformed files ==")
        all_results += run_malformed_cases(args.vm, tmpdir, args.verbose)

    passed = [r for r in all_results if r.passed]
    failed = [r for r in all_results if not r.passed]
    print()
    print(f"{len(passed)} passed, {len(failed)} failed, {len(all_results)} total")
    if failed:
        print()
        print("Failures:")
        for r in failed:
            print(f"- {r.name}: {r.detail}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
