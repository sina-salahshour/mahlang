"""Self-contained executables (`mah build --self-contained`): one file
holding a `/bin/sh` stub, the native `mah-vm` runtime binary, and the
program's `.mahc` bytes -- see docs/RUST_VM.md. Running the file runs the
stub, which copies the embedded runtime into a cache directory (once) and
execs it on the file itself; the runtime then finds the `.mahc` part
through the header. Everything else that reads `.mahc` files (`decode`,
`mah runc`, `mah dis`) goes through `split` first, so a bundle works
anywhere a plain `.mahc` file does.

Layout (header lines are ASCII; offsets are absolute and zero-padded to
12 digits, so the header's length doesn't depend on their values):

    #!/bin/sh
    # mah-bundle v1
    # vm-version: 0.1.0
    # vm-target: x86_64-linux
    # vm-sha256: <hex>
    # vm-offset: 000000001234
    # vm-size: 000000567890
    # mahc-offset: 000000569124
    # mahc-size: 000000002345
    ...shell code...
    <mah-vm bytes><.mahc bytes>

`runtime/src/bundle.rs` parses the same header.
"""

from __future__ import annotations

import hashlib
from typing import NamedTuple

from .format import MahcFormatError

BUNDLE_PREFIX = b"#!/bin/sh\n# mah-bundle v1\n"

_NUMBER_WIDTH = 12


class BundleInfo(NamedTuple):
    vm_version: str
    vm_target: str
    vm_sha256: str
    vm_size: int
    mahc_size: int


def _stub(vm_version: str, vm_target: str, vm_sha256: str, vm_offset: int, vm_size: int,
          mahc_offset: int, mahc_size: int) -> bytes:
    def num(n: int) -> str:
        return str(n).zfill(_NUMBER_WIDTH)

    # The cache directory is keyed by the runtime's hash, so programs built
    # with the same mah-vm share one extracted copy. The copy is written
    # under a temporary name and renamed into place, so a concurrent or
    # interrupted first run never leaves a half-written runtime behind.
    key = vm_sha256[:16]
    return f"""#!/bin/sh
# mah-bundle v1
# vm-version: {vm_version}
# vm-target: {vm_target}
# vm-sha256: {vm_sha256}
# vm-offset: {num(vm_offset)}
# vm-size: {num(vm_size)}
# mahc-offset: {num(mahc_offset)}
# mahc-size: {num(mahc_size)}

# A self-contained Mah program (built by `mah build --self-contained`):
# this script, then the mah-vm runtime, then the program's bytecode.
# `mah dis` on this file shows the bytecode.
os=$(uname -s | tr '[:upper:]' '[:lower:]')
arch=$(uname -m)
case $os in darwin) os=macos ;; esac
case $arch in arm64) arch=aarch64 ;; amd64) arch=x86_64 ;; esac
if [ "$arch-$os" != "{vm_target}" ]; then
    echo "error: this program was built for {vm_target}, not $arch-$os" >&2
    exit 126
fi
dir=${{XDG_CACHE_HOME:-$HOME/.cache}}/mah/vm/{key}
if ! mkdir -p "$dir" 2>/dev/null || [ ! -w "$dir" ]; then
    dir=${{TMPDIR:-/tmp}}/mah-vm-$(id -u)-{key}
    mkdir -p "$dir" || exit 126
fi
vm=$dir/mah-vm
if [ ! -x "$vm" ]; then
    tmp=$vm.$$
    tail -c +{num(vm_offset + 1)} "$0" | head -c {num(vm_size)} > "$tmp" && chmod +x "$tmp" && mv -f "$tmp" "$vm" || {{
        rm -f "$tmp"
        echo "error: could not unpack the Mah runtime into $dir" >&2
        exit 126
    }}
fi
exec "$vm" run "$0"
exit 127
""".encode("ascii")


def build(vm_bytes: bytes, vm_version: str, vm_target: str, mahc: bytes) -> bytes:
    """The self-contained executable for `mahc` (plain `.mahc` bytes, no
    shebang) running on the `mah-vm` binary `vm_bytes`."""
    sha = hashlib.sha256(vm_bytes).hexdigest()
    # The stub's length doesn't depend on the offsets (fixed-width), so
    # measure it once with placeholder zeros.
    stub_len = len(_stub(vm_version, vm_target, sha, 0, 0, 0, 0))
    vm_offset = stub_len
    mahc_offset = vm_offset + len(vm_bytes)
    stub = _stub(vm_version, vm_target, sha, vm_offset, len(vm_bytes), mahc_offset, len(mahc))
    assert len(stub) == stub_len
    return stub + vm_bytes + mahc


def split(data: bytes) -> tuple[BundleInfo | None, bytes]:
    """`(info, mahc_bytes)` for a bundle, `(None, data)` for anything else
    (a plain or shebang'd `.mahc` file, which `decode` handles)."""
    if not data.startswith(BUNDLE_PREFIX):
        return None, data
    fields: dict[str, str] = {}
    for raw in data[len(BUNDLE_PREFIX):].split(b"\n", 16)[:16]:
        if not raw.startswith(b"# "):
            break
        key, sep, value = raw[2:].decode("ascii", "replace").partition(": ")
        if sep:
            fields[key] = value

    def number(key: str) -> int:
        value = fields.get(key)
        if value is None or not value.isdigit():
            raise MahcFormatError(f"invalid self-contained bundle: missing or bad '{key}'")
        return int(value)

    vm_size = number("vm-size")
    mahc_offset = number("mahc-offset")
    mahc_size = number("mahc-size")
    if mahc_offset + mahc_size > len(data):
        raise MahcFormatError("invalid self-contained bundle: bytecode extends past the end of the file")
    info = BundleInfo(
        fields.get("vm-version", "?"), fields.get("vm-target", "?"), fields.get("vm-sha256", ""), vm_size, mahc_size
    )
    return info, data[mahc_offset : mahc_offset + mahc_size]
