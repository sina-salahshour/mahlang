# M44 discussion paper: optional multithreading for `detach`

Status: **discussion only, not a contract.** Nothing is built until the
decisions at the end are made. Then this becomes `M44_threads.md`.

## 1. How it works today

- **One OS thread runs all Mah code, on both VMs.** `detach expr` starts the
  task *eagerly and synchronously* and runs it until it finishes or `.await`s
  a pending Promise (V2_DESIGN.md M10 at line 1876, M20 at 2682;
  `spawn_detached`: `runtime/src/vm/exec.rs:1238`,
  `mah/code_interpreter.py:1317`). Resolving a Promise runs its waiters
  synchronously (`exec.rs:1252`, `runtime_values.py:275`). There is no
  ready queue: `drive` (`exec.rs:1287`, `code_interpreter.py:1960`) is
  re-entered from `resolve_promise`.
- **Worker threads exist, but never touch Mah values.** `IoHub`
  (`exec.rs:561`, `code_interpreter.py:1028`) runs blocking fs/socket/stdin
  jobs on `std::thread::spawn` / `threading.Thread` (`exec.rs:604`,
  `code_interpreter.py:1054`). A job returns plain data (`IoValue`,
  `fs.rs:25`). The scheduler turns it into Mah values on the VM thread
  (`settle_io`, `next_event` at `exec.rs:1378`). The pending-I/O rule keeps
  the program alive while any job is pending. This is message passing
  already, with a small plain-data type.
- **Heap model, Rust.** Every heap value is `Rc<RefCell<_>>`: Struct, Enum,
  Promise, Vector, Map, Bytes, frames, tasks (`value.rs:27,167,200,216,279,
  282,308`). Strings are `Rc<str>`. `ClosureData.identity` is a `Cell`
  (`value.rs:85-91`). Nothing is `Send`. `Vm<'p>` borrows a
  `LinkedProgram` that is also full of `Rc<str>` (`exec.rs:504`,
  `link.rs:577`). There are about 250 `Rc`/`RefCell` sites in ~12k lines.
  Only the fs and socket handle tables are already `Arc<Mutex>`
  (`fs.rs:57-63`, `socket.rs:17-55`).
- **Heap model, Python.** These are plain mutable objects (`Frame`,
  `Closure`, `VectorValue`, `BytesValue`, `MapValue`; `runtime_values.py:101-206`).
  The GIL serializes bytecode, so Mah code gets no CPU parallelism on threads.
- **Closures capture frames by reference.** A closure holds its
  `defining_frame` and walks `static_parent` (`value.rs:22-63`). Every
  top-level function reaches the **main frame**, where all globals live
  (MAHC_FORMAT.md section 6, line 799). Through M20's desugaring, a detached
  block is a closure that captures the caller's locals by reference.

## 2. The core problem

Any function value can reach the whole object graph: its static chain, the
main frame, and from there every Vector, Map, Bytes and Promise. Sharing
that graph across OS threads means one of three things:
(a) **copy** the graph, (b) make it **immutable**, or (c) put a **lock**
around it. Today's semantics, where a detached block mutates a captured
variable and the caller sees it, only survive with (c).

## 3. Options

### A: isolated VM per thread, deep copy in and out (web workers)

```mah
let p = detach(thread) checksum(big_bytes)   // or: thread.spawn(checksum, big_bytes)
let sum = p.await
```

- **Semantics.** The callee and its arguments are deep-copied into a fresh
  VM on a new OS thread. The copy keeps identity and cycles through a memo
  table, the way structured clone does. The closure's reachable frames,
  including the main frame, are copied too. The result is deep-copied back
  and settles the Promise through `IoHub.done_tx`, exactly like an fs job.
  Mutating captured state changes the copy only.
- **Rules.**
  - A Promise cannot cross threads, and copying one is an error.
  - File and socket handles can be shared (the tables are already `Arc`)
    or moved (to be decided).
  - The compiler rejects assignment to a captured variable inside
    `detach(thread)`. That turns the silent-copy footgun into an error.
- **Rust cost.** There is **no Rc to Arc migration.** Each thread decodes
  and links its own `LinkedProgram` from a shared `Arc<[u8]>` of the
  `.mahc` file. A new `thread.rs` holds a `SendValue` tree (an extension
  of `IoValue`) plus copy-out and copy-in. Estimate: about 500-700 lines
  across 3-4 files.
- **Python cost.** About 400 lines. The thread runs a second `run_code`
  instance on a `threading.Thread`. Semantics are identical, but there is
  **no speedup** under the GIL. Optional backends: a free-threaded
  CPython 3.13t+ build or 3.14 subinterpreters could give real
  parallelism with the same code. `multiprocessing` would also work,
  because `SendValue` is plain data that can be pickled.
- **Determinism.** No shared state, so a program's results are the same
  on every run unless it is timing-dependent. Only the interleaving of
  `print` differs, and stdout is locked per write. An
  `MAH_THREADS=inline` mode runs thread bodies synchronously on the VM
  thread. Isolation makes that mode semantically identical, so goldens
  and `tests/vm_diff.py` stay deterministic.
- **Bytecode.** Two choices:
  - A `thread.*` native under `NATIVE_SINCE_MINOR`. This needs no opcode,
    only the next free MINOR (1.20 or 1.21, depending on what M42 claims).
  - `detach(thread)` syntax, which needs a flag on `detach` or a new opcode.

### B: freeze and share (Arc for immutables)

`let cfg = freeze(config)` makes a deeply immutable value. Only frozen
values, Numbers, Strings and functions whose captures are frozen may cross
threads, with zero copying.

- **Rust cost.** This is the large migration: `Rc<str>` and every immutable
  path become `Arc`, plus a `Value::Frozen*` family or a frozen flag that is
  checked on every mutation. About 250 sites across ~10 files, an estimated
  2-3k changed lines. Atomic refcounts slow single-threaded code by an
  estimated 5-15%.
- **Python.** Python would need a frozen wrapper and checks in every
  mutating native. There is still no speedup.
- **Language.** This adds a new concept that leaks into types (`Frozen<T>`?).
  Closures over mutable frames cannot be frozen, so most functions need A's
  copying anyway.

### C: actors and channels (`std:thread` + `std:channel`)

```mah
let (tx, rx) = channel.new()          // illustrative; Mah has no tuples, so a struct
thread.spawn(fn() { for job in jobs(rx) { tx2.send(work(job)) } })
let r = rx.recv().await
```

- **Semantics.** This is A plus long-lived workers. Messages are deep-copied.
  `recv` returns a pending Promise settled through the IoHub, so the
  pending-I/O rule extends naturally. The cost is A's plus about 300 lines
  per VM (`Arc<Mutex<VecDeque<SendValue>>>` in Rust, `queue.Queue` in
  Python).
- **Fit.** A thread-per-core HTTP worker pool for the M42 server, or for the
  future framework repository, needs exactly this, together with
  **transferable sockets**.

### D: one shared heap with locks

Every `Rc<RefCell>` becomes `Arc<Mutex|RwLock>`, and today's
capture-by-reference semantics are kept.

- **Rust cost.** All ~250 sites plus every `borrow()` call, an estimated
  4-6k lines touched, with a slowdown on every heap access.
- **Mah semantics.** Data races are possible: two threads appending to one
  Vector get nondeterministic results, and deadlocks are possible too.
- **Python.** Only a free-threaded build matches the Rust semantics.
  Elsewhere the Python VM would be strictly "less parallel", and VM-diff
  parity becomes untestable.
- **Verdict.** Not recommended.

## 4. Comparison

| | A copy/isolate | B freeze | C actors | D shared+locks |
|---|---|---|---|---|
| Rust work | ~600 lines | ~2-3k | A + ~300 | ~4-6k |
| Single-thread slowdown | none | 5-15% | none | high |
| Closures/mutation | copied; capture-assign rejected | frozen only | copied messages | shared, racy |
| Python parity | exact semantics, no speedup | same | same | only free-threaded |
| Deterministic tests | yes (inline mode) | yes | mostly (message order) | no |

## 5. Recommendation

1. Do **A** first, as the native `thread.spawn(f, ...args) -> Promise`.
   Ordinary `.await`, the IoHub settle path and the pending-I/O rule are
   reused unchanged, and there is no heap migration. Add
   `detach(thread) expr` as sugar only once the capture rule is
   settled.
2. Then add **C**'s channels and transferable sockets in a follow-up,
   sized for the HTTP server's worker pool.
3. Drop **B** and **D**. B's cost buys little over A, and D breaks
   determinism and Python parity.
4. Python VM: run the thread on a real `threading.Thread` for semantics
   (CPU parallelism only on free-threaded builds). Document "parallel on
   the Rust VM, concurrent on the Python VM".

## 6. Decisions for you

1. **Model:** A (+ later C), or something else?
2. **Surface:** `thread.spawn(f, args)` only, `detach(thread) expr`, or both?
3. **Captured mutable variables in a threaded body:** a compile error,
   silent copy, or a warning?
4. **Globals:** copy the whole main frame, or only the slots the function
   uses (the resolver's free-variable sets; smaller copies, more compiler
   work)?
5. **Handles:** are file and socket handles shared, moved (the sender loses
   access) or refused across threads?
6. **Python backend:** threads only, or also an opt-in process or
   subinterpreter backend for real speedup?
7. **Limits:** a thread pool size (a cap, defaulting to the core count) or
   one OS thread per spawn?
8. **Errors:** confirm that an uncaught error in a thread fails its Promise
   like a detached task (M25), and that `process.exit` in a thread exits
   the process.
9. **Milestone split:** M44a (spawn) and M44b (channels and transferable
   sockets), each taking one MINOR, or a single M44?
