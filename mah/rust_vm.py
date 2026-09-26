"""Finding and running `mah-vm`, the native Rust runtime in `runtime/` --
see docs/RUST_VM.md. The CLI uses it for `--vm rust` and to build
self-contained executables; nothing here imports the Python VM.

Lookup order: `$MAH_VM` (a path to the binary), then `mah-vm` next to the
`mah` package (the installed layout, `make install-mah`), then the repo's
own Cargo build (`runtime/target/release/mah-vm`, `make vm`), then `PATH`.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile

_PACKAGE_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

NOT_FOUND_MESSAGE = (
    "error: the Rust runtime (mah-vm) isn't built -- run 'make vm' in the mah repo, "
    "or set MAH_VM to the mah-vm binary"
)


class RustVmNotFound(Exception):
    pass


def find_vm() -> str:
    override = os.environ.get("MAH_VM")
    if override:
        if not os.path.isfile(override):
            raise RustVmNotFound(f"error: MAH_VM points to '{override}', which doesn't exist")
        return override
    for candidate in (
        os.path.join(_PACKAGE_PARENT, "mah-vm"),
        os.path.join(_PACKAGE_PARENT, "runtime", "target", "release", "mah-vm"),
    ):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    on_path = shutil.which("mah-vm")
    if on_path:
        return on_path
    raise RustVmNotFound(NOT_FOUND_MESSAGE)


def vm_version(vm: str) -> tuple[str, str]:
    """`(version, target)` from `mah-vm --version` (`mah-vm 0.1.0 (x86_64-linux)`)."""
    out = subprocess.run([vm, "--version"], capture_output=True, text=True, check=True).stdout
    m = re.fullmatch(r"mah-vm (\S+) \((\S+)\)\s*", out)
    if m is None:
        raise RustVmNotFound(f"error: unexpected 'mah-vm --version' output: {out.strip()!r}")
    return m.group(1), m.group(2)


def run_file(path: str) -> int:
    """Run a `.mahc` file (or bundle) with the Rust VM, sharing this
    process's stdin/stdout/stderr; returns its exit code (0, 1 for a
    runtime error, 2 for an invalid file -- the same codes `mah runc` uses)."""
    vm = find_vm()
    sys.stdout.flush()
    sys.stderr.flush()
    return subprocess.run([vm, "run", path]).returncode


def run_bytes(data: bytes) -> int:
    """`run_file` for bytes compiled in memory (`mah run --vm rust`)."""
    fd, path = tempfile.mkstemp(suffix=".mahc", prefix="mah-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return run_file(path)
    finally:
        os.unlink(path)
