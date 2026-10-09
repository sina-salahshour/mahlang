# The Rust VM (`mah-vm`)

`runtime/` is a second implementation of the `.mahc` machine
([MAHC_FORMAT.md](MAHC_FORMAT.md)), written in Rust with **the standard
library plus three crates**: `regex` (M32, for `std:regex`, with no default
features), and `rustls` (with only its `ring` provider) plus `webpki-roots`
(M39, for `socket.start_tls`'s TLS and its fallback trusted roots; M42, for `socket.start_tls_server`'s), per `docs/STDLIB.md`'s rule that each dependency is small,
vetted, and named with its reason in `runtime/Cargo.toml`. The Python VM (`mah/code_interpreter.py`)
stays the reference: the Rust one must print the same output, fail with the
same messages and exit codes, and reject the same malformed files.

```sh
make vm                                   # cargo build --release -> runtime/target/release/mah-vm
mah run --vm rust prog.mh                 # compile with Python, run on mah-vm
mah runc --vm rust prog.mahc              # run compiled bytecode on mah-vm
mah run --vm rust prog.mh -- a "b c"      # program arguments (process.args()) go after `--`
mah-vm run prog.mahc a "b c"              # the same, straight on the VM: every token after PATH
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

`mah-vm` itself is small: `mah-vm run FILE [ARGS...]` runs a `.mahc` file (plain,
shebang'd, or a self-contained bundle) whose program arguments
(`std:process`'s `args()`) are every token after FILE; `mah-vm test FILE INDEX` runs one
test of a `mah test` build and prints its outcome on stderr (M28,
docs/MAHC_FORMAT.md §6.10; `mah test --vm rust` drives it), and `mah-vm
--version` prints `mah-vm 0.2.0 (x86_64-linux) bytecode 1.7` -- the last
part is the newest `.mahc` version it loads. The CLI finds it through
`$MAH_VM`, then next to the installed `mah` package (`make install-mah`
copies it there if it's built), then `runtime/target/release/`, then `PATH`
(`mah/rust_vm.py`). `mah build --self-contained` refuses to bundle a
program that needs a newer bytecode version than that mah-vm loads (one
too old to print `bytecode` is trusted with 1.4 only) and says to rebuild it
with `make vm` (and `make install-mah`), rather than writing an executable
that fails with "unsupported minor version" when run.

## Layout

| file | what |
|---|---|
| `runtime/src/decode.rs` | port of `mah/bytecode/decode.py`, same validation and messages |
| `runtime/src/vm/` | port of `mah/code_interpreter.py` (`exec.rs`: the step loop, scheduler and I/O hub; `link.rs`; `value.rs`) + `mah/natives.py` (`natives.rs`), `mah/string_methods.py` (`methods.rs`), `mah/fs_natives.py` (`fs.rs`, std:fs's natives and the open-file table) `mah/bytes_methods.py` (`bytes.rs`, the `Bytes` methods and `std:bytes`'s natives), `mah/process_natives.py` (`process.rs`, std:process's natives), `mah/socket_natives.py` (`socket.rs`, std:socket's natives and the socket table) `mah/reflect_natives.py` (`reflect.rs`, std:reflect's natives; the META section is parsed in `decode.rs`) and `mah/thread_runtime.py` + `mah/thread_natives.py` (`thread.rs`, M44: the thread runtime, copies and std:thread's natives) |
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

Known differences, all corner cases: the pre-1.10 `io.input` native
(kept for older files) treats only Unicode `Nd` characters as digits
(Python's `isdigit` also accepts superscripts etc., which then fail to
convert), and the Rust VM has no Python-style recursion limit. Since
1.10 both VMs read standard input for `input()` on a reader thread and
settle its Promise from the scheduler loop (`IoHub` in
`runtime/src/vm/exec.rs`, `_IoHub` in `mah/code_interpreter.py`), so a
detached `input()` waits alongside timers identically.

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
`$TMPDIR`), and then `exec`s `mah-vm run <the file itself> "$@"`, passing the program's own arguments on. Later runs
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

## Threads

M44 (docs/contracts/M44_threads.md, docs/MAHC_FORMAT.md §6.11), with M45's
transactions (docs/contracts/M45_atomic.md), runs jobs on
real OS threads, so on this VM they run **in parallel** (the Python VM has
the same semantics under the GIL). The design keeps every Mah heap
single-threaded: values are `Rc`/`RefCell` and never cross threads; only
plain copies do.

- **`ThreadRuntime`** (`runtime/src/vm/thread.rs`), one per run behind an
  `Arc` and shared by the main VM and every job VM: one `Mutex<RtState>`
  (the shared-variable store and its versions, the exclusivity token, the
  `retry` watchers, the wait-for graph, pools,
  semaphores, channels, live VMs and the quiescence count) with the pools'
  `Condvar`s on that same mutex; the shared stdout (`Arc<Mutex<BufWriter<
  Stdout>>>`); the shared file and socket tables; the one stdin reader; the
  run's start instant (`time.monotonic_ms` counts from it everywhere); the
  `Arc<Program>`, the arguments and the test mode. Nothing blocks while
  holding the mutex; waking a waiter sends a `Completion` into its VM's
  done channel, and that VM settles its own Promise.
- **Transactions** (`atomic { }`): a task's `tx` is an
  `Rc<RefCell<Tx>>` shared, by reference, with the sub-task of an implicit
  `to_string`/`message` call (`invoke_sync`). `thread.rs` has the opcode
  functions (`get`, `set`, `begin`, `end`, `abort`, `retry`), the commit
  (`tx_commit`: wait for the exclusivity token if another transaction holds
  it, validate the read set's versions, bump the clock, publish, wake the
  `retry` watchers) and `same_copy` over two `Payload` graphs. A conflict or
  a `retry` is a `RuntimeError` whose `restart` is `Some(TxSignal::Conflict |
  TxSignal::Retry)`: every `?` propagates it unchanged (out of `invoke_sync`
  too), and `step_task_inner` handles it first, in the owning task, by
  restoring the pc, frame and stack depths of the outermost `atomicbegin`.
  A `retry` waits as `Wait::Retry` on a pending Promise whose continuation
  has `restart: true` (resolving it re-runs the `atomicbegin`). Exclusivity
  waits use `excl_cv`, a `Condvar` on the runtime mutex, with a 50 ms
  timeout; no `RefCell` borrow is held across one. `ATOMIC_REFUSED_NATIVES`
  is linked into `LinkedInstr::Native`'s `atomic` field, so the `in_atomic`
  check costs one `Option` test per native call.
- **`SendGraph`**: the copy format that crosses threads, a new type rather
  than an extension of `fs::IoValue` (which has no identity, cycles or
  closures; fs and socket jobs keep using it). `copy_out` walks a value
  iteratively with a memo keyed by `Rc` pointer, so shared objects and
  cycles survive, and refuses Promises in strict mode before the memo;
  `copy_in` rebuilds it in the receiving VM in three passes (shells,
  closures over the receiver's linked functions, fields). Numbers cross as
  `SendDecimal`, because `Decimal` holds an `Rc`. A job's `Snapshot` is a
  `SendGraph` of the callee, its arguments, the user method table,
  decorators and hooks, plus the environment table.
- **Workers** are `std::thread`s named `mah-NAME-I`, with the same stack
  size as the main VM thread (`VM_STACK_SIZE`, 1 GiB of reserved virtual
  memory), so a recursion depth that works on the main thread works in a
  job. Each worker links the program once and reuses that `LinkedProgram`
  for every job; a job runs in a fresh `Vm` (its own heap, timers, `IoHub`
  and done channel) under `catch_unwind`, so a panic fails only that job,
  with `RuntimeError.Internal`.
- **Output**: each `Vm` collects stdout in its own line buffer and hands
  whole lines to the shared writer, so lines from different threads never
  tear; every exit path of `run` flushes. Every way the process ends goes
  through `exit_process` (first exit wins, behind an `AtomicBool`), so a
  `process.exit` from a job and the main thread's own end never race.
- **Shared tables**: only the main VM's `IoHub` owns the file and socket
  tables and closes them when it ends; a handle closed by one thread is
  closed for all.

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
