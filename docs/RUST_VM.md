# The Rust VM (`mah-vm`)

`runtime/` is a second implementation of the `.mahc` machine
([MAHC_FORMAT.md](MAHC_FORMAT.md)), written in Rust with **only the
standard library** (no crates). The Python VM (`mah/code_interpreter.py`)
stays the reference: the Rust one must print the same output, fail with the
same messages and exit codes, and reject the same malformed files.

```sh
make vm                                   # cargo build --release -> runtime/target/release/mah-vm
mah run --vm rust prog.mh                 # compile with Python, run on mah-vm
mah runc --vm rust prog.mahc              # run compiled bytecode on mah-vm
mah build --self-contained prog.mh        # one executable file with mah-vm inside
make test-rust                            # the whole test suite, with programs run on mah-vm
```

In a project, `mah-project.toml` can set both per project:

```toml
[run]
vm = "rust"              # `mah run` uses mah-vm (default "python"; --vm overrides)

[[target]]
name = "dist"
profile = "release"
out = "build/app"
self-contained = true    # this target is a standalone executable
```

`mah build --self-contained` in a project bundles every target it builds.
(`[run] vm` applies to project-mode `mah run`, not to `mah run some/file.mh`,
which ignores the manifest like all single-file commands.)

`mah-vm` itself is small: `mah-vm run FILE` runs a `.mahc` file (plain,
shebang'd, or a self-contained bundle) and `mah-vm --version` prints
`mah-vm 0.1.0 (x86_64-linux)`. The CLI finds it through `$MAH_VM`, then
next to the installed `mah` package (`make install-mah` copies it there if
it's built), then `runtime/target/release/`, then `PATH`
(`mah/rust_vm.py`).

## Layout

| file | what |
|---|---|
| `runtime/src/decode.rs` | port of `mah/bytecode/decode.py`, same validation and messages |
| `runtime/src/vm.rs` | port of `mah/code_interpreter.py` + `mah/natives.py` |
| `runtime/src/decimal.rs`, `bigint.rs` | Mah's `Number` (below) |
| `runtime/src/bundle.rs` | reads self-contained bundles (below) |
| `runtime/src/main.rs` | the `mah-vm` command |
| `runtime/tests/*.py` | differential tests against the Python implementation |

## Numbers

A Mah `Number` is Python's `decimal.Decimal` in the reference VM's
context: 28 significant digits, round-half-even, exponents within ±999999,
and InvalidOperation/DivisionByZero/Overflow as runtime errors (their
message is Python's `str()` of the exception, e.g.
`[<class 'decimal.InvalidOperation'>]`). `decimal.rs` implements that from
scratch on its own big integers, including correctly rounded `exp`/`ln`,
so `**` with a fractional exponent gives the same digits as Python.

Only a number's value is observable in Mah (printing ignores trailing
zeros, equality and Map keys are numeric), so `decimal.rs` keeps every
value canonical (no trailing zeros, no negative zero) instead of copying
Python's exponent bookkeeping. Small values stay inline (no allocation).

`0 ** n` with `n < 0` raises `Division by zero` in both VMs; `decimal`
alone would produce an Infinity that Mah can't represent.

Integer powers copy libmpdec's algorithm exactly (binary exponentiation at
`28 + digits(n) + 2` digits, then a final rounding). That double rounding
occasionally differs from the mathematically correct last digit, and Mah
matches Python here rather than being "more right". Integers print every
digit in both VMs (`print(10 ** 5000)`), formatted from the decimal itself
rather than through Python's `int`, which refuses past 4300 digits.

Known differences, all corner cases: `input()` treats only Unicode `Nd`
characters as digits (Python's `isdigit` also accepts superscripts etc.,
which then fail to convert), and the Rust VM has no Python-style recursion
limit.

## Self-contained executables

`mah build --self-contained prog.mh` writes one file that runs on a machine
without `mah` (or Python) installed, as long as it has the same OS and CPU
as the `mah-vm` it was built with. It's a `/bin/sh` script with the runtime
and the bytecode appended (no Rust build step involved):

```
#!/bin/sh
# mah-bundle v1
# vm-version: 0.1.0
# vm-target: x86_64-linux
# vm-sha256: <sha256 of the mah-vm bytes>
# vm-offset: 000000001234        absolute byte offsets and sizes,
# vm-size: 000000567890          zero-padded to 12 digits so the
# mahc-offset: 000000569124      header's length doesn't depend
# mahc-size: 000000002345        on their values
                                 (blank line ends the header)
...shell code...
<mah-vm bytes><.mahc bytes>
```

On first run the script checks the platform (`uname`), copies the runtime
out of itself with `tail -c | head -c` into
`${XDG_CACHE_HOME:-~/.cache}/mah/vm/<hash>/mah-vm` (falling back to
`$TMPDIR`), and then `exec`s `mah-vm run <the file itself>`. Later runs
reuse the cached copy, and programs built with the same runtime share it.
The runtime finds the bytecode through the header.

The same header is read by `mah/bytecode/bundle.py`, so `mah runc`,
`mah runc --vm rust` and `mah dis` all accept a bundle. `mah dis` prints
which runtime a file carries on its second line:

```
MAHC version 1.3
runtime: rust mah-vm 0.1.0 (x86_64-linux), self-contained, 612344 bytes
```

(or `runtime: none (plain bytecode, runs on an installed mah)`), then
the usual disassembly of the bytecode part.

## Testing

- `make test-rust` builds `mah-vm`, runs `cargo test`, then runs the
  **entire** Python test suite with `MAH_TEST_VM=rust`, which makes
  `tests/support.py` run every program on `mah-vm` and turn its exit
  status back into the exceptions the tests expect.
- `tests/test_rust_vm.py` (part of `make test`) checks that every example
  prints the same on both VMs, and covers `--vm rust`, bundles and `dis`.
  It builds `mah-vm` with cargo if needed and skips only without cargo.
- `runtime/tests/decimal_diff.py` compares `decimal.rs` with Python's
  `decimal` on tens of thousands of random operations;
  `runtime/tests/vm_diff.py` compares the two VMs on examples, error cases
  and malformed files.

A change to the language or the VM has to land in both implementations,
with the Python one as the reference.
