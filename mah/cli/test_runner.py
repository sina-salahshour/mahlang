"""`mah test` (M28, docs/MAH_TEST.md): find every `*.test.mh` file under
the project, compile each once with its TESTS table, run every test in a
fresh VM (the Python VM in-process, or `mah-vm test FILE N`), and report.

Output mirrors `cargo test`:

    running 4 tests
    test src/calc.test.mh::adds two numbers ... ok
    test src/calc.test.mh::rejects bad input ... FAILED
    ...
    failures:

    ---- src/calc.test.mh::rejects bad input ----
    assert_eq failed at src/calc.test.mh:13
      actual:   "ab"
      expected: "abc"

    test result: FAILED. 2 passed; 1 failed; 1 skipped; finished in 0.04s
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import subprocess
import sys
import tempfile
import time

from .. import rust_vm
from ..bytecode.decode import decode
from ..code_interpreter import run_test_bytes
from ..compiler.driver import TEST_SUFFIX, _location_label, compile_to_bytes
from ..preprocessor import demangle_message, preprocess
from ..project.manifest import MANIFEST_NAME, MahProjectError, find_manifest, load_project
from ..test_outcome import TestOutcome, parse_outcome

_SKIPPED_DIRS = {"build"}


def discover(root: str) -> list[str]:
    """Every `*.test.mh` under `root`, sorted, skipping `build/` and hidden
    directories."""
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in _SKIPPED_DIRS)
        for name in sorted(filenames):
            if name.endswith(TEST_SUFFIX):
                found.append(os.path.join(dirpath, name))
    return found


class _TestCase:
    def __init__(self, rel: str, name: str, index: int, data: bytes):
        self.rel = rel
        self.name = name
        self.index = index
        self.data = data
        self.outcome: TestOutcome | None = None
        self.stdout = ""

    @property
    def label(self) -> str:
        return f"{self.rel}::{self.name}"


def _run_python(case: _TestCase, timeout: float | None) -> None:
    out = io.StringIO()
    old_stdin = sys.stdin
    sys.stdin = io.StringIO("")
    try:
        with contextlib.redirect_stdout(out):
            case.outcome = run_test_bytes(case.data, case.index, timeout)
    finally:
        sys.stdin = old_stdin
    case.stdout = out.getvalue()


def _run_rust(case: _TestCase, vm: str, path: str, timeout: float | None) -> None:
    try:
        result = subprocess.run(
            [vm, "test", path, str(case.index)], capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL
        )
    except subprocess.TimeoutExpired as e:
        stdout = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        case.stdout = stdout
        case.outcome = TestOutcome("timeout", f"took longer than {timeout:g}s")
        return
    case.stdout = result.stdout
    outcome = parse_outcome(result.stderr) if result.returncode == 0 else None
    case.outcome = outcome or TestOutcome("failed", result.stderr.strip() or f"mah-vm exited with {result.returncode}")


def _display_file(rel: str, file: str | None) -> str:
    if file is None:
        return rel
    if file.startswith("std:") or file.startswith("pkg:") or file.startswith("<"):
        return file
    return os.path.normpath(os.path.join(os.path.dirname(rel), file))


def _failure_report(case: _TestCase) -> list[str]:
    outcome = case.outcome
    lines = demangle_message(outcome.message).split("\n") if outcome.message else ["failed"]
    frames = outcome.frames
    here = next((line for file, line in frames if file is None), None)
    if here is not None:
        lines[0] += f" at {case.rel}:{here}"
    # An assertion's own frames (inside std:test) aren't news; any other
    # error gets its Mah stack trace.
    if len(frames) > 1 and frames[0][0] != "std:test":
        lines.append("stack trace:")
        lines += [f"  at {_display_file(case.rel, file)}:{line}" for file, line in frames]
    if outcome.status == "timeout":
        lines = [f"timed out: {outcome.message}"]
    if case.stdout:
        lines.append("stdout:")
        lines += ["  " + line for line in case.stdout.rstrip("\n").split("\n")]
    return lines


def _locate(path: str, message: str) -> str:
    """A resolve error cites a raw combined-text offset (`at position
    1234`); turn it into `file#line:col`, as `mah run` does."""
    m = re.search(r"at position '?(\d+)'?$", message)
    if m is None:
        return message
    try:
        pp = preprocess(path)
        label = _location_label(pp, pp.entry_path, int(m.group(1)))
    except Exception:  # noqa: BLE001 -- keep the unlocated message
        return message
    if label is None:
        return message
    if label.startswith("#"):
        label = os.path.basename(path) + label
    return message[: m.start()] + f"at position {label}"


def run(filter_text: str | None, file: str | None, vm: str | None, timeout_ms: int | None) -> int:
    start = time.monotonic()
    if file is not None:
        if not file.endswith(TEST_SUFFIX) or not os.path.isfile(file):
            print(f"error: {file} is not a test file (a *{TEST_SUFFIX} file)", file=sys.stderr)
            return 2
        manifest = find_manifest(os.path.dirname(os.path.abspath(file)))
        root = os.path.dirname(manifest) if manifest else os.path.dirname(os.path.abspath(file))
        files = [os.path.abspath(file)]
    else:
        manifest = find_manifest(os.getcwd())
        if manifest is None:
            print(
                f"error: no {MANIFEST_NAME} found in {os.getcwd()} or its parents (use --file to run one test file)",
                file=sys.stderr,
            )
            return 2
        root = os.path.dirname(manifest)
        files = discover(root)
    if vm is None:
        vm = "python"
        if manifest is not None:
            try:
                vm = load_project(manifest).run_vm
            except MahProjectError as e:
                print(f"error: {e}", file=sys.stderr)
                return 2
    vm_path = None
    if vm == "rust":
        try:
            vm_path = rust_vm.find_vm()
        except rust_vm.RustVmNotFound as e:
            print(e, file=sys.stderr)
            return 2
    timeout = timeout_ms / 1000.0 if timeout_ms is not None else None

    cases: list[_TestCase] = []
    compile_errors: list[tuple[str, str]] = []
    for path in files:
        rel = os.path.relpath(path, root)
        try:
            data = compile_to_bytes(path=path, test=True)
        except Exception as e:  # noqa: BLE001 -- any compile error is reported per file
            message = demangle_message(str(e.args[0] if e.args else e))
            compile_errors.append((rel, _locate(path, message)))
            continue
        program = decode(data)
        for index, entry in enumerate(program.tests):
            case = _TestCase(rel, program.strings[entry.name], index, data)
            if filter_text is None or filter_text in case.name or filter_text in case.label:
                cases.append(case)

    for rel, message in compile_errors:
        print(f"error: {rel} doesn't compile: {message}", file=sys.stderr)

    print(f"running {len(cases)} test{'' if len(cases) == 1 else 's'}")
    temp_paths: dict[int, str] = {}
    try:
        for case in cases:
            if vm == "rust":
                key = id(case.data)
                if key not in temp_paths:
                    fd, temp_paths[key] = tempfile.mkstemp(suffix=".mahc", prefix="mah-test-")
                    with os.fdopen(fd, "wb") as f:
                        f.write(case.data)
                _run_rust(case, vm_path, temp_paths[key], timeout)
            else:
                _run_python(case, timeout)
            status = case.outcome.status
            if status == "ok":
                shown = "ok"
            elif status == "skipped":
                shown = f"skipped ({case.outcome.message})" if case.outcome.message else "skipped"
            else:
                shown = "FAILED"
            if case.outcome.leftover:
                shown += " (warning: pending timers or tasks were cancelled)"
            print(f"test {case.label} ... {shown}", flush=True)
    finally:
        for path in temp_paths.values():
            os.unlink(path)

    failed = [c for c in cases if c.outcome.status in ("failed", "timeout")]
    passed = sum(1 for c in cases if c.outcome.status == "ok")
    skipped = sum(1 for c in cases if c.outcome.status == "skipped")
    if failed:
        print("\nfailures:")
        for case in failed:
            print(f"\n---- {case.label} ----")
            print("\n".join(_failure_report(case)))
    ok = not failed and not compile_errors
    elapsed = time.monotonic() - start
    extra = f"; {len(compile_errors)} file(s) didn't compile" if compile_errors else ""
    print(
        f"\ntest result: {'ok' if ok else 'FAILED'}. {passed} passed; {len(failed)} failed; "
        f"{skipped} skipped{extra}; finished in {elapsed:.2f}s"
    )
    return 0 if ok else 1
