# M44 contract: threads (`std:thread`, `detach(t)`), shared variables and `lock`, channels — bytecode 1.21

Written by the thinker (`docs/DEVELOPMENT_WORKFLOW.md`) from the discussion paper
`docs/contracts/M44_threads_options.md` and the user's binding decisions. It is the only spec the
coders work from. Nothing here is open: where a choice was made by judgment it says so, and §15
lists every such choice for the user.

- **M44a "threads"**: `std:thread` (`Thread`, `spawn`, `run`, `close`, `join`, `pending`, `id`,
  `name`, `cores`, `Semaphore`), the `detach(t) expr` form, copy-in/copy-out jobs, `shared let`
  variables, the `lock` construct, `ThreadError`.
- **M44b "channels"**: `Channel` (`send`, `recv`, `try_recv`, `close`, `len`, `closed`, iteration).

Both ship together in **bytecode 1.21** (one MINOR bump, 20 → 21) and are built now. The work is
split in three parts (§13 assigns every file to exactly one):

- **Part 1 "core"** (first): the compiler (parser, AST, resolver, codegen, checker, preprocessor,
  lowering, format tables and MINOR), `mah/std/thread.mh`, the prelude's `ThreadError`, the whole
  Python VM implementation, Python-side tests, `examples/threads.mh`.
- **Part 2 "rust"** (after Part 1, in parallel with Part 3): the Rust VM to parity, `vm_diff`
  cases, `make test-rust` green.
- **Part 3 "tooling+docs"** (after Part 1, in parallel with Part 2): tree-sitter grammar and
  queries, VS Code grammar, LSP, formatter, every doc and www page.

Every coder reads §1–§5 (what the language does), §14 (Mah gotchas) and their own part's sections.
Parity between the Python and Rust VMs is mandatory: every observable behaviour (stdout, stderr,
exit codes, error kinds and messages) is defined once here and both VMs implement it.

---

## 1. Decisions (summary)

1. **Model: isolated VM per job.** A job runs in a fresh VM heap on an OS thread, on a deep copy of
   everything it can reach (the callee, its captured frames, **every global**, the method table,
   decorators and hooks), taken **when the job is queued**. Its result or error comes back as a
   copy and settles the submitter's Promise through the same done-queue path an fs job uses.
   There is no shared Mah heap and no `Rc` → `Arc` migration.
2. **Threads are long-lived job queues.** `thread.spawn(...)` returns a `Thread` handle backed by
   `workers` OS threads (default 1) sharing one FIFO queue. With one worker, jobs run one at a
   time in the order they were queued.
3. **Two ways to queue a job:** `detach(t) expr` (any expression, run on `t`) and
   `t.run(f, ...args)` (a function and arguments). Both return a `Promise`. Plain `detach expr` /
   `detach { }` are unchanged (same-thread task).
4. **Captured mutable variables are silently copied** (no compile error). Mutating them in a job
   changes the job's copy only.
5. **Globals: the whole main frame is copied, per job, at queue time** (the "snapshot").
6. **Shared state** is `shared let NAME = expr` (top level only). Its value lives in one
   process-wide store outside every VM, stored as copied plain data. Reads give a copy; a whole
   assignment is atomic; changing a shared value *in place* is only possible inside
   `lock NAME { ... }`, which checks the value out to the task, lets the body use and mutate it
   freely, and writes it back when the block exits (normally, by `return`/`break`/`continue`, or by
   a throw). Only a read written **lexically inside** that `lock` sees the checked-out value itself;
   every other read is a copy (§5.2). In-place mutation through the variable's name outside a lock
   is a **compile error** (§5.3), so `v.push(1)` can never silently change a copy; the remaining
   indirect forms (passing it to a function that mutates its parameter, mutating a loop variable
   bound from it) are checker warnings and are documented as acting on a copy.
7. **Locks are task-owned, re-entrant, FIFO, await-friendly and deadlock-aware**: waiting for a lock
   is a pending Promise (other tasks and timers keep running); a lock wait or an `.await` that would
   close a cycle of waits (lock → owner, await → the task or job that settles the Promise, join →
   the thread's running jobs) fails at once with `ThreadError` kind `"deadlock"` (§6.4). A run in
   which **every** VM is blocked on waits only other threads could end (locks, semaphores,
   channels, joins, job replies) is detected too: the waits fail with `ThreadError` kind `"stuck"`
   instead of hanging (§6.10).
8. **`Semaphore`** (counting, owner-less) and **`Channel`** (M44b, copying, bounded or not) are
   process-wide objects addressed by id; their handles are plain structs that copy freely.
9. **File and socket handles are shared** by every VM of a run (the handle tables are shared).
10. **Python VM**: real `threading.Thread`s, identical semantics, no speedup (GIL).
11. **Bytecode 1.21**: four new opcodes (`sharedget`, `sharedset`, `sharedlock`, `sharedunlock`) and
    19 `thread.*` natives. No new section, type or constant tag.
12. **Output never tears a line**: each VM collects its output in its own line buffer and hands
    whole lines to the one shared stdout (§6.3), so `print` from several threads interleaves by
    line, never inside one.
13. **Jobs copy `std:random`'s state with the other globals**, so every job of a pool starts the
    same "random" sequence unless it seeds its own (documented prominently, §4.3).

---

## 2. `std:thread` — the surface (M44a + M44b)

### 2.1 The module, exactly

`mah/std/thread.mh` (Part 1 writes this file verbatim; only whitespace may change, and it must
satisfy `format_source(text) == text`, see `tests/test_stdlib.py`):

```mah
# std:thread -- run code on other threads, share variables between them, and
# pass messages (docs/STDLIB.md).
#
#   import thread from "std:thread"
#   let t = thread.spawn(name: "worker")
#   let p = detach(t) checksum(data)     # runs on t, on a copy of what it uses
#   print(p.await)
#   print(t.run(checksum, other).await)  # the same, for a function and arguments
#   t.join()
#
# A job sees a copy of every global and of everything it captures, taken when
# it is queued; changing them changes the copy. Its result (or error) comes
# back as a copy too. Share state with `shared let` variables and `lock`, with
# a Semaphore, or with channels. A thread runs its jobs one at a time, in the
# order they were queued; `workers: n` makes a pool that runs n at a time.

extern fn type_name(value: Unknown) -> String = "value.type_name"
extern fn native_spawn(name: Unknown, workers: Number, capacity: Unknown) -> Vector<
    Unknown
> = "thread.spawn"
extern fn native_submit(t: Unknown, f: Unknown, args: Vector<Unknown>) -> Promise<
    Unknown
> = "thread.submit"
extern fn native_close(id: Number, cancel: Bool) = "thread.close"
extern fn native_join(id: Number) -> Promise<Unknown> = "thread.join"
extern fn native_pending(id: Number) -> Number = "thread.pending"
extern fn native_current() -> Vector<Unknown> = "thread.current"
extern fn native_cores() -> Number = "thread.cores"
extern fn native_semaphore(permits: Number) -> Number = "thread.semaphore_new"
extern fn native_sem_acquire(id: Number) -> Promise<Unknown> = "thread.semaphore_acquire"
extern fn native_sem_try_acquire(id: Number) -> Bool = "thread.semaphore_try_acquire"
extern fn native_sem_release(id: Number) = "thread.semaphore_release"
extern fn native_sem_available(id: Number) -> Number = "thread.semaphore_available"
extern fn native_channel(capacity: Unknown) -> Number = "thread.channel_new"
extern fn native_send(id: Number, value: Unknown) -> Promise<Unknown> = "thread.channel_send"
extern fn native_recv(id: Number) -> Promise<Unknown> = "thread.channel_recv"
extern fn native_try_recv(id: Number) -> Option<Unknown> = "thread.channel_try_recv"
extern fn native_channel_close(id: Number) = "thread.channel_close"
extern fn native_channel_len(id: Number) -> Number = "thread.channel_len"
extern fn native_channel_closed(id: Number) -> Bool = "thread.channel_closed"

fn argument_error(message: String) -> Unknown {
    throw RuntimeError.ArgumentError { message: message }
}

fn whole(n: Unknown, from: Number) -> Bool {
    if type_name(n) != "Number" { return false }
    n >= from & n % 1 == 0
}

# -- threads --------------------------------------------------------------------

## A thread (or a pool of `workers` threads sharing one queue) that runs jobs:
## `detach(t) expr` and `t.run(f, ...args)` queue one and give its Promise.
## `capacity` is how many jobs may wait beyond the running ones (none: no limit).
export struct Thread { id: Number, name: String, workers: Number, capacity: Unknown }

## Starts a thread. `name` (default "thread-ID") shows in errors and in
## `thread.name()`; `workers` threads share the queue; `capacity` limits how many
## jobs may wait to start (none: no limit). A job queued beyond it throws
## ThreadError "full".
export fn spawn(name: Unknown = none, workers: Number = 1, capacity: Unknown = none) -> Thread {
    if name != none {
        if type_name(name) != "String" { argument_error("thread.spawn: name must be a String or none") }
    }
    if !whole(workers, 1) { argument_error("thread.spawn: workers must be a whole number from 1 up") }
    if capacity != none {
        if !whole(capacity, 0) {
            argument_error("thread.spawn: capacity must be none or a whole number from 0 up")
        }
    }
    let r = native_spawn(name, workers, capacity)
    Thread { id: r[0], name: r[1], workers: workers, capacity: capacity }
}

impl Thread {
    ## Queues `f(...args)` on this thread and gives its Promise. `f`, `args` and
    ## every global are copied now; the arguments are bound to `f`'s parameters
    ## now too.
    fn run(self, f: Unknown, ...args: Vector<Unknown>) -> Promise<Unknown> throws ThreadError {
        native_submit(self, f, args)
    }

    ## Stops taking jobs: queueing one throws ThreadError "closed". Queued jobs
    ## still run, unless `cancel` is true: then the ones not started yet fail with
    ## ThreadError "cancelled". A running job is never interrupted.
    fn close(self, cancel: Bool = false) { native_close(self.id, cancel) }

    ## Closes the thread and waits until every job it had has finished.
    fn join(self) throws ThreadError { native_join(self.id).await }

    ## How many jobs are queued or running right now.
    fn pending(self) -> Number { native_pending(self.id) }
}

impl Printable for Thread {
    fn to_string(self) { "Thread(" + self.name + ")" }
}

## The current thread's id: 0 on the main thread, else its Thread's id.
export fn id() -> Number { native_current()[0] }

## The current thread's name: "main" on the main thread, else its Thread's name.
export fn name() -> String { native_current()[1] }

## How many threads this machine can run at once (at least 1).
export fn cores() -> Number { native_cores() }

# -- semaphores -------------------------------------------------------------------

## A counting semaphore shared by every thread: at most `permits` holders at once.
export struct Semaphore { id: Number, permits: Number }

## A semaphore with `permits` free permits.
export fn semaphore(permits: Number) -> Semaphore {
    if !whole(permits, 1) { argument_error("thread.semaphore: permits must be a whole number from 1 up") }
    Semaphore { id: native_semaphore(permits), permits: permits }
}

impl Semaphore {
    ## Waits (other tasks keep running) until a permit is free, and takes it.
    ## Waiters get permits in the order they asked. Give it back with
    ## `defer s.release()`: a permit is not owned by anyone, so one taken by a
    ## task its job abandons is never given back.
    fn acquire(self) { native_sem_acquire(self.id).await }

    ## Takes a permit if one is free right now (and nobody is waiting).
    fn try_acquire(self) -> Bool { native_sem_try_acquire(self.id) }

    ## Gives a permit back. Throws ThreadError "over_release" if all are free.
    fn release(self) throws ThreadError { native_sem_release(self.id) }

    ## How many permits are free right now.
    fn available(self) -> Number { native_sem_available(self.id) }
}

impl Printable for Semaphore {
    fn to_string(self) { "Semaphore(" + self.available() + "/" + self.permits + ")" }
}

# -- channels (M44b) ----------------------------------------------------------------

## A queue of messages shared by every thread. Each message is copied when it is
## sent. `capacity` none: never full; 0: a send waits until a receiver takes it.
export struct Channel { id: Number, capacity: Unknown }

## A new channel.
export fn channel(capacity: Unknown = none) -> Channel {
    if capacity != none {
        if !whole(capacity, 0) {
            argument_error("thread.channel: capacity must be none or a whole number from 0 up")
        }
    }
    Channel { id: native_channel(capacity), capacity: capacity }
}

impl Channel {
    ## Sends a copy of `value`, waiting while the channel is full. Throws
    ## ThreadError "closed" or "not_sendable" (a Promise inside `value`).
    fn send(self, value: Unknown) throws ThreadError { native_send(self.id, value).await }

    ## The next message, waiting for one. Throws ThreadError "closed" once the
    ## channel is closed and empty.
    fn recv(self) -> Unknown throws ThreadError { native_recv(self.id).await }

    ## some(the next message) if one is there right now (or a sender is waiting
    ## to hand one over), else none.
    fn try_recv(self) -> Option<Unknown> { native_try_recv(self.id) }

    ## No more sends; messages already sent can still be received. Waiting
    ## receivers (of an empty channel) and waiting senders fail with "closed".
    fn close(self) { native_channel_close(self.id) }

    ## How many messages are waiting to be received.
    fn len(self) -> Number { native_channel_len(self.id) }

    ## Whether `close` was called.
    fn closed(self) -> Bool { native_channel_closed(self.id) }
}

impl Printable for Channel {
    fn to_string(self) { "Channel(" + self.id + ")" }
}

struct ChannelIter { channel: Channel }

impl Iterator for ChannelIter {
    fn next(self) {
        try { some(self.channel.recv()) } catch {
            e: ThreadError => {
                if e.kind == "closed" { none } else { throw e }
            }
        }
    }
}

## `for let message in channel { ... }` receives until the channel is closed and
## empty.
impl Iterable for Channel {
    fn iter(self) { ChannelIter { channel: self } }
}
```

Part 1 may adjust only what the formatter or the checker forces (line breaks; a type annotation the
strict checker demands). Function names, parameters, defaults, messages and behaviour are fixed.

### 2.2 `ThreadError` (the prelude)

Append to `mah/std/prelude.mh`, after `EndOfInput`'s impl:

```mah
# M44: what threads, shared variables, locks, semaphores and channels throw.
# `kind` is one of "closed", "full", "cancelled", "deadlock", "not_sendable",
# "foreign_promise", "over_release", "stuck".
struct ThreadError { kind: String, message: String }

impl Error for ThreadError {
    fn message(self) { self.message }
}
```

It is a prelude type, so its name stays global (`ThreadError`, never mangled) and both VMs build
it directly as a struct value of type name `ThreadError` with fields `kind`, `message` in that order
(like `EndOfInput`). An uncaught one reports `Uncaught ThreadError: <message>`.

Prelude inclusion (`mah/preprocessor.py`, `_uses_prelude`): `ThreadError` is already a trigger
(every prelude struct name is), which covers every program importing `std:thread`. Add two token
triggers: an `id` token `shared` immediately followed by an `id` token `let`, and an `id` token
`lock` immediately followed by an `id` token (a program using `shared`/`lock` without `std:thread`
can still get a `"deadlock"` or `"not_sendable"` ThreadError).

Every `kind`, when it happens, and its exact `message`:

| kind | raised by | message (exact; `NAME`, `N` substituted) |
|---|---|---|
| `closed` | queuing a job on a closed Thread | `thread 'NAME' is closed` |
| `closed` | `send` on a closed channel; `recv` on a closed, empty channel; a waiting send/recv when `close` is called | `the channel is closed` |
| `full` | queuing a job when queued + running ≥ workers + capacity | `thread 'NAME' is full (N jobs queued or running)` (N = workers + capacity) |
| `cancelled` | a queued job dropped by `close(cancel: true)` | `thread 'NAME' was closed before this job started` |
| `deadlock` | a `lock` wait (or the lock an assignment takes) that would close a cycle | `deadlock: waiting for 'VAR' would never end` (VAR = the shared variable's source name) |
| `deadlock` | an `.await` (or `t.join()`) that would close a cycle through at least one lock, job or join edge (§6.4) | `deadlock: this await would never end (it waits, through locks or threads, for itself)` |
| `deadlock` | `t.join()` from one of `t`'s own jobs | `deadlock: a thread can't join itself` |
| `stuck` | a lock, semaphore, channel, join or job-reply wait when every VM of the run is blocked (§6.10) | `the wait can never finish: every thread is waiting` |
| `not_sendable` | `t.run` args, a job's result, a job's error, a channel message containing a Promise inside a Vector/Map/struct/enum (§4.2) | `a Promise can't be sent to another thread` |
| `not_sendable` | writing back / assigning a shared variable whose value contains such a Promise | `shared variable 'VAR' can't hold a Promise` |
| `foreign_promise` | `.await` in a job of a Promise that was still pending when it was copied | `a Promise from another thread can't be awaited here (it was still pending when it was copied)` |
| `over_release` | `Semaphore.release` when all permits are free | `release without a matching acquire (all N permits are free)` (N = permits) |
| `stuck` | a job whose root task waits on a Promise nothing will settle (and nothing is pending in the job) | `the job never finished: it waits on a Promise nothing will settle` |

Invalid argument *values* to `std:thread` functions throw `RuntimeError.ArgumentError` with the
messages in §2.1. Natives called with a bad id (only reachable by hand-built structs) throw
`RuntimeError.ArgumentError` with `thread: no such thread N` / `thread: no such semaphore N` /
`thread: no such channel N` (N printed with `to_string`). `thread.submit` throws
`RuntimeError.TypeMismatch` with `detach(...) needs a thread.Thread to run on, got T` when its first
argument isn't a struct whose display type name is `Thread` with a Number field `id` (T = display
type name), and `thread.run: f must be a function, got T` when `f` isn't a function.

### 2.3 Jobs and threads: lifecycle

- **Ids**: thread ids, semaphore ids and channel ids are three separate counters, each starting at
  1 per program run. The main thread's id is 0, its name `"main"`. A spawned thread with `name: none`
  is named `thread-ID`.
- **Spawn** starts its `workers` OS threads at once. Idle threads never keep the program alive.
- **Queueing** (`detach(t) expr` or `t.run(f, ...args)`), in this exact order:
  1. validate the Thread value (`TypeMismatch` above) and `f` (`TypeMismatch`);
  2. bind the arguments to `f`'s parameters in the submitting VM (exactly like `detach f(args)`
     does: the same `_bind_params`/`bind_params` call and the same arity/keyword error messages);
     `detach(t) expr` always has a zero-parameter closure and no arguments;
  3. take the snapshot (§4) — may throw `not_sendable` (only for `t.run` arguments);
  4. under the runtime lock: the Thread closed → `closed`; queued + running ≥ workers + capacity
     (capacity not none) → `full`; else append the job to the queue;
  5. return a pending Promise, counted as pending work of the submitting VM (it keeps that VM — and
     so the program — alive until the job's reply arrives).
- **Running**: a worker takes the oldest queued job, runs it in a fresh job VM (§6.5), and replies.
  With `workers: 1` the next job starts only after the previous one finished. With `workers: n`
  jobs start in queue order, at most n at a time.
- **A job is a small program**: its root task is the callee called with the bound arguments. The
  job ends like a program does: when its root task has finished **and** it has no timer, I/O,
  thread job, lock/semaphore/channel wait or join pending. Then, if a task the job detached failed
  and nobody awaited it, the job **fails** with the first such error (fail order); otherwise its
  Promise settles with a copy of the root's value. If the root task fails with an uncaught error,
  the job ends **at once** (its pending work is abandoned, §6.6) and its Promise fails with a copy
  of that error. If the root never finishes and nothing is pending, the job fails with `stuck`.
- **Result/error copy-back**: the value is copied in strict mode (§4.2); if that fails the job fails
  with `not_sendable` instead. An error value is copied the same way; if the error itself contains a
  Promise, the job fails with `not_sendable` instead of it. A copied struct/enum keeps its
  `thrown_at` and backtrace (same program, so the pcs mean the same thing): an uncaught job error
  reported by the main VM is located at its original throw site.
- **An unobserved failed job Promise** is reported at program end exactly like an unobserved failed
  detached task (it is appended to the submitting VM's failed-promise list when it fails). With
  **several** unobserved failures from jobs, which one is reported first depends on the order the
  replies arrived, which is **nondeterministic** (documented); no test or `vm_diff` case may have
  more than one unobserved failing job.
- **A job Promise settled early by hand** (std:async's `settle`/`promise.resolve`/`promise.fail`
  on the user-visible Promise): when the job's reply then arrives, the VM drops the payload,
  removes the pending entry and decrements the pending count, and does **not** append anything to
  the failed-promise list (§6.2).
- **`close(cancel)`**: idempotent. Marks the Thread closed. With `cancel: true`, every job still in
  the queue is removed and its Promise fails with `cancelled`, in queue order. Running jobs continue.
  Workers exit once the queue is empty and the Thread is closed.
- **`join()`**: `close(false)`, then waits (a pending Promise) until every worker of the Thread has
  exited. Settles at once if they already have. From a job running on that same Thread it throws
  `deadlock` ("a thread can't join itself") synchronously, without closing.
- **`pending()`**: queued + running, at this instant.
- **Handles everywhere**: a `Thread`, `Semaphore` or `Channel` struct is plain data (ids); a copy in
  a job names the same object. Jobs may spawn threads and queue jobs on any Thread; threads outlive
  the job that spawned them.
- **Waits that can never end**: a job that awaits a job queued *behind* it on the same
  single-worker Thread is not a cycle the deadlock check sees (the queued job has no task yet); it is
  caught by the quiescence rule (§6.10) only once every VM of the run is blocked, and then fails with
  `stuck`. While some other thread keeps running (a server loop, a timer), it waits (documented).
- **Semaphore permits are owner-less**: a permit taken by a task that its job abandons (the job
  ended while that detached task was still running, §6.6) is never given back. The docs say to
  release with `defer s.release()` (§2.1).

### 2.4 `thread.current`, `cores`

`thread.current` returns `[0, "main"]` in the main VM and `[id, name]` of the job's Thread in a
job VM. `thread.cores` returns `os.cpu_count() or 1` (Python) /
`std::thread::available_parallelism()` or 1 (Rust); never compared between VMs.

---

## 3. `detach(t) expr`

### 3.1 The exact rule

`detach` is still a primary expression. After the `detach` token:

1. If the current token is **not** `(`: today's rule, unchanged (`_parse_detach` as it is).
2. If it is `(`: parse `( expr )` exactly like `_parse_primary`'s parenthesis branch does (struct
   literals allowed inside, `expect(PAREN_CLOSE)`), giving `inner`. Then look at the **current
   token** (the one after `)`):
   - it is the **thread form** when that token starts on the **same line** as the `)` ends
     (`self._on_same_line()`) **and** its type is one of: `ID`, `NUMBER`, `STRING`, `TRUE`,
     `FALSE`, `NONE`, `SOME`, `FN`, `DETACH`, `SLEEP_ASYNC`, `IF`, `MATCH`, `FOR`, `WHILE`, `TRY`,
     `THROW`, `BANG`, or `BRACE_OPEN` — the last only while `self._struct_literal_allowed` is true
     (so never in an `if`/`while`/`for`/`match` head). Then `inner` is the thread expression and
     the operand is parsed with `self._parse_primary()`;
   - it is a **compile error** (a `SyntaxError` raised by the parser, at the position of the
     offending token) when that token is on the same line and its type is `PRINT`, `LET`, `RETURN`,
     `DEFER`, `BREAK` or `CONTINUE`: `detach(t) needs an expression; write 'detach(t) { ... }'`
     (those statements can't be an operand, and today's meaning — `detach (t)` then a statement on
     the same line — is never what was meant);
   - it is the same compile error, with the message `detach(t) and its operand must be on the same
     line; write 'detach(t) {' on one line`, when `inner` is a bare `Ident` or a `FieldAccess` chain
     of `Ident`s **and** that token is `BRACE_OPEN` on a **later** line (today this meant "detach the
     read of a variable, then run a bare block on this thread" — useless, and silently not threaded);
   - otherwise `inner` **is the operand's start**, exactly as today: operand =
     `self._parse_postfix_from(inner)`; the parser also records `inner` on the node as
     `paren_head` (§8.2) so the checker can warn when it is a `Thread` (§8.6).
3. Peel trailing `.await`s off the operand as today. In the thread form the operand is **always**
   wrapped in the synthesized zero-parameter closure (`FnExpr(..., detached=True)`, the same shape
   `_parse_detach` builds for a non-call operand) — also when it is a call, a method call or
   `sleep_async(...)`.
4. Build `DetachExpr(call=..., position=detach_tok.position, thread=inner, thread_position=<the
   '(' token's position>)` (both new fields default `None`), then put the `.await`s back on top.

| source | parses as |
|---|---|
| `detach(t) f(x)` / `detach (t) f(x)` | thread form: `f(x)` runs on `t` (whitespace never matters) |
| `detach(t) { a + b }` | thread form, block operand |
| `detach(t) f(x).await` | `(detach(t) f(x)).await` |
| `detach(pool.next()) work()` | thread form; the thread expression is any expression |
| `detach (a + b)`, `detach (a + b).await` | today's meaning (`)` followed by nothing / `.`) |
| `detach (f)(x)` | today's meaning: detach the call `(f)(x)` (`(` is not a start token) |
| `detach (v)[0]` | today's meaning (`[` is not a start token) |
| `detach (a) - b` | today's meaning: `(detach (a)) - b` |
| `detach(t)` then a newline, then `foo()` | today's meaning: `detach (t)`, then a statement `foo()` (checker warning if `t` is a `Thread`, §8.6) |
| `detach(t)` then a newline, then `{ ... }` | **compile error**: `detach(t) and its operand must be on the same line; ...` |
| `detach(t) print("hi")`, `detach(t) let x = 1`, `detach(t) return` | **compile error**: `detach(t) needs an expression; write 'detach(t) { ... }'` |
| `if detach(t) { ... }` | today's meaning (`{` not allowed in a condition head) |
| `detach(t) [1, 2]`, `detach(t) (a + b)`, `detach(t) -x` | **not** the thread form (`[`, `(`, `-` excluded): today's meaning (index/call/subtract `t`), which fails at run time; the checker warns when `t` is a `Thread` (§8.6). Write `detach(t) { [1, 2] }` |

The only programs whose meaning changes are ones with another expression on the **same line**
right after `detach (x)` — previously two statements on one line (`detach (x) foo()`) — and the two
new compile errors above (a same-line statement after `detach (x)`, and `detach (name)` followed by a
block on the next line). Together with the new prelude name `ThreadError` (§2.2: a program that
declares its own `ThreadError` type in a file that includes the prelude now gets the existing
"built-in name" error), these are the **M44 compatibility notes** (V2_DESIGN, the blog post,
`docs/ERRORS.md`).

### 3.2 Semantics

`detach(t) expr` ≡ queue, on `t`, a job whose callee is the synthesized closure (whose defining frame
is the current frame), with no arguments (§2.3). The thread expression is evaluated first, then the
closure is created, then the job is queued; the value is the job's Promise. Everything inside `expr`
— including a call's callee and argument expressions — is evaluated **on the thread**, against the
snapshot taken at queue time. `return`, and `break`/`continue` leaving `expr`, are compile errors
(the existing `_reject_in_detached`, because the closure has `detached=True`).

---

## 4. Copy semantics

### 4.1 What a snapshot contains (in this order, one shared memo)

1. **Strict roots**: the bound argument values of `t.run` (none for `detach(t)`).
2. **Environment roots**: the callee closure (and so its defining frame, its static chain, and the
   **main frame — every global**); every user method-table entry whose target is a Mah closure
   (`(type name, method name, inherent-or-trait name, closure, is_method)`); the decorator table;
   `hook_types`; `hook_params`; the `fn_items` flag.
3. Plain data: the submitter's process environment table (`process.env_*`), copied.

Taken synchronously in the submitting VM when the job is queued (§2.3 step 3). Justification: what
a job sees is exactly the state at the line that queued it, independent of when a worker picks it
up; the submitting VM's heap is never touched by another thread (in Rust it can't be: it is `Rc`).

Not copied: other tasks, timers, pending I/O, the defer stacks, the regex cache, `shared`
variables (they are not in any frame — §5), the native method-table entries (each VM builds its
own).

### 4.2 The copy, value by value

Two modes cross threads. **Strict** for strict roots, job results/errors, channel messages and
shared-variable values; **environment** for everything reached through a frame or a closure (a
frame switches the walk to environment mode for everything below it). A third, same-VM mode,
**local**, is used only for a non-lexically-locked read of a shared variable the task holds (§6.4
`sharedget`): it is the strict walk except that a Promise is kept as the same object (never refused,
never copied).

| value | copy |
|---|---|
| `none`, Bool, Number, String, `Type` | the same value (immutable; Python keeps the very objects, `NONE_VALUE` stays the singleton) |
| Vector, Map, Bytes | a new object with copied contents (Map: same insertion order, same original keys) |
| struct, enum (incl. `RuntimeError`, `some(x)`, `ThreadError`) | a new object, same type name/variant, copied fields, same `thrown_at` and backtrace |
| function (closure) | a new closure: same FUNCTIONS index (so same code, params, name, rest flags), same `identity`, defining frame = the copy of its defining frame |
| frame | a new frame: copied slots, static parent = copy of the parent (or none) |
| the "absent" parameter sentinel | itself |
| **Promise, strict mode** | **refused**: the whole copy fails with `not_sendable` |
| Promise, environment mode | copied **by state**: Settled → a Settled copy (value copied in environment mode); Failed → a Failed copy (error copied in environment mode); **Pending → a Failed Promise whose error is `ThreadError{kind: "foreign_promise"}`**. Every copied Promise has `observed = true` (it is never reported as an unobserved failure) and no waiters. |

- **Identity and cycles are preserved** within one copy: an object reached twice (from anywhere in
  the snapshot — two globals, an argument and a global, a cycle) becomes one object in the copy. The
  memo is keyed by object identity. The strict-mode Promise check happens **before** the memo
  lookup, and strict roots are walked first, so a Promise inside a sent Vector is always refused even
  if that Vector is also a global.
- **The walk is iterative** (explicit work list, no recursion), in both VMs: allocate an empty
  "shell" for each heap object on first sight (and memoize it), fill shells afterwards.
- Every copy is fully independent of its source: nothing in it is shared with the VM it came from.

### 4.3 Consequences (documented)

- Mutating a global or a captured variable in a job changes the job's copy only; the job's next run
  starts from a new snapshot. Nothing in a thread's heap survives from one job to the next.
- A function stored in a channel message or a shared variable carries a copy of the globals it saw.
- **`std:random`'s generator state is a global**: a job continues the same sequence as the
  submitter at queue time, so `pool.run(monte_carlo)` four times in a row gives **four identical
  results**. Kept (it follows decision 4), and documented prominently — in std:thread's module
  page, the STDLIB "threads" section, the language reference's Threads section and as a comment in
  any example that uses random numbers in a job — with the remedy: seed per job, e.g.
  `random.seed(thread.id() * 1000 + n)` at the top of the job, or pass a different seed as a `t.run`
  argument.
- `time.monotonic_ms` counts from program start in every VM of the run (§6.3).
- Reading a Promise global in a job: Settled ones work, Pending ones throw `foreign_promise` when
  awaited.

---

## 5. Shared variables and `lock`

### 5.1 Syntax

```
shared_let := "shared" let_stmt                          -- top level of a file only
lock_expr  := "lock" target { "," target } block
target     := ID { "." ID }                              -- after preprocessing: one ID
```

- `shared` and `lock` are **contextual**: no new lexer keywords; `let shared = [1]`,
  `let lock = 2`, `fn f(lock) { lock + 1 }` keep working.
  - `shared` is the keyword when it is an `ID` token `shared` at the start of a block item and the
    next token is `LET` on the same line. `export shared let NAME = ...` is allowed (preprocessor
    and formatter changes, §8.1/§11.4).
  - `lock` is the keyword when `_parse_primary` sees an `ID` token `lock` whose next token is an `ID`
    on the same line. `lock` is an expression (block-shaped, like `if`): its value is the body's
    value; as a statement it needs no `;`.
- Examples: `shared let counter = 0`, `shared let log: Vector<String> = []`,
  `lock counter { counter = counter + 1 }`, `lock a, b { a = a + b }`,
  `let next = lock counter { counter = counter + 1; counter }`, `lock lib.hits { ... }` (a shared
  variable another module exports; the preprocessor rewrites `lib.hits` to one ID).

### 5.2 Semantics

- **Storage**: one process-wide store maps a shared variable's **index** (assigned by the resolver,
  §8.3) to a strict-mode copy of its value. A variable never written reads as `none`.
  `shared let x = e` evaluates `e` and stores it (an assignment, below) when the top-level code
  reaches it; a job that reads `x` before that sees `none`.
- **Reads** come in two kinds, decided at compile time (the `sharedget` mode operand, §7.1):
  - a **lexically locked read** — the Ident is written lexically inside `lock x { }` (the §5.3
    definition: same function, a `defer` closure included, nested `fn`s and `detach` closures
    excluded) — returns the task's **working object itself** (no copy), so `x.push(1)` and
    `let a = x; a.push(1)` inside the block mutate the working copy;
  - **every other read** returns a **fresh copy**: of the task's working value if the current task
    holds `x`'s lock (so a helper called from inside `lock x { }` still sees the caller's
    uncommitted changes, but can never mutate them through a read), else of the store's committed
    value. Never waits, never sees another task's uncommitted working copy ("read committed").
  A function therefore behaves the same whether or not its caller holds the lock:
  `fn count_with(v) { let s = xs; s.push(v); s.len() }` never changes `xs`, called from anywhere.
- **Read point and cost**: a shared read happens at its position in left-to-right evaluation (the
  `sharedget` runs when the expression is generated, unlike a plain variable, whose slot is read
  when the consuming instruction runs): in `f(x, g())` where `g` assigns `x`, `f` gets the value `x`
  had **before** `g` ran. Every non-locked read copies the whole value, so reading a big shared
  Vector inside a loop is O(n) per read: read it once into a local (`let snapshot = big`) or work
  inside one `lock`. Both are documented (§11.5).
- **Assignment** `x = e` outside a lock held by the current task: evaluate `e` first, then acquire
  `x`'s lock (may wait, like `lock`), set, release (writes back). So a plain assignment is atomic
  and never interleaves with someone else's `lock x` block.
- **`lock x { body }`**: acquire `x` (wait while another task holds it), give the task a working
  copy (a fresh copy of the store's value), run `body`; inside it every **lexically locked** read of
  `x` returns the working copy itself (no copy: `let a = x; a.push(1)` mutates the working copy), and
  every assignment to `x` by the holding task — lexically inside or in a helper it calls — replaces
  the working copy (no lock round trip). When the block exits — normally, by
  `return`/`break`/`continue`, or by a throw — the working copy is written back (strict copy) and
  the lock released. **Write-back happens on throws too** (changes made before the throw stay).
  If the working copy contains a Promise (strict copy fails), the lock is released **without**
  write-back (the variable keeps its old value), and then:
  - when the block is exiting **normally** (end of body, `return`, `break`, `continue`):
    `ThreadError not_sendable` ("shared variable 'VAR' can't hold a Promise") is thrown at the
    block's end;
  - when the block is exiting because of a **throw** E: nothing is thrown — E keeps unwinding
    unchanged (a failed write-back never replaces the error in flight, unlike an ordinary error
    thrown in a `defer`, docs/ERRORS.md). Mechanism: the `lock` body has its own handler region
    whose handler marks the task's held entries for the block's targets as unwinding at their
    current depth and re-throws; the release that exits that depth sees the mark and swallows the
    refusal (§6.4 `mark`/`release`, §7.1 the `sharedunlock` mode operand, §8.5).
- `lock a, b { }` acquires `a`, then `b` (source order), releases `b` then `a`.
- **Owner = task** (not OS thread). A lock is **re-entrant** for the task holding it (a depth count;
  only the outermost exit writes back and releases); the working copy is shared by all levels. A
  *different* task — including one the holder detached, or one on the same thread — waits.
- **Implicit runtime calls belong to their caller's task.** The VM runs `Printable.to_string`
  (for `print`, `+` with a String, string conversion) and `Error.message` in a sub-task
  (`invoke_sync`). That sub-task **inherits the calling task's identity**: the same task `id` and
  the very same `held` map (by reference, not a copy). So inside such an impl, reading `x` while the
  caller holds `x` sees the caller's working value (as a copy, the read not being lexically locked),
  `lock x { }` re-enters (depth + 1), `x = v` replaces the caller's working value, and releases
  balance against the same entry. Tasks created by `detach`, `detach(t)` and jobs always get fresh
  ids and an empty `held`. (Mechanism: each VM keeps a stack of the tasks it is stepping, §9.4 /
  §10.6; the sub-task copies identity from its top.)
- **Waiting is a pending Promise**: the acquire yields a Promise that is already settled when the
  lock is free (no suspension), pending otherwise; the wait counts as pending work (keeps the
  program alive); other tasks and timers keep running. Waiters are served **FIFO**; on release the
  lock passes directly to the first waiter (no barging).
- **Deadlock detection** follows a wait-for graph kept in the runtime (§6.4) whose edges are:
  a task waiting for a lock → the lock's owner task; a task suspended on `.await` of a Promise
  whose producer is known → the producer (a same-VM `detach` task, or a job's root task once the
  job has started); a task suspended on `t.join()` → the root task of every job running on `t`.
  - When task T asks for a lock owned by task U (U ≠ T) and the graph leads from U back to T, T's
    acquire fails at once with `deadlock` (`waiting for 'VAR' would never end`, VAR = the lock T
    asked for) and T does not wait.
  - When task T is about to suspend on `.await` (or `join`) and the graph leads from the producer
    back to T through at least one lock, job or join edge, the await fails at once with `deadlock`
    (`this await would never end (it waits, through locks or threads, for itself)`) and T does not
    suspend. So `lock x { (detach { x = 1 }).await }` and `lock x { (detach(t) { x = 1 }).await }`
    throw `deadlock` instead of hanging. A cycle of plain same-VM awaits (no lock, job or join edge,
    possible before M44) is not reported: its behaviour is unchanged.
  - Not edges (never detected as `deadlock`): semaphores, channels, a job still queued (it has no
    task yet), Promises with no recorded producer (timers, I/O, `std:async`'s hand-made Promises).
    Those are left to the quiescence rule (§6.10).
- **Quiescence** (§6.10): when every VM of the run is blocked and only waits that other threads
  could end are pending (lock, semaphore, channel, join, job reply), nothing can ever happen: the
  waits fail with `ThreadError stuck` (`the wait can never finish: every thread is waiting`)
  instead of the program hanging.
- A lock wait inside an implicit runtime call (`to_string`, `Error.message` called by the VM) that
  would suspend (another task holds the lock) fails with the existing "cannot suspend" error;
  re-entering a lock the calling task holds never suspends (previous bullet).
- A job that ends while one of its tasks still holds locks releases them **without write-back**
  (§6.6).

### 5.3 Compile-time rules (resolver errors; exact messages)

"Lexically inside `lock x`" means inside the `lock` body that names `x`, in the same function, not
inside a nested `fn`/closure — except the closure of a `defer` (which runs before the lock is
released) does keep the enclosing locks. The closures `detach`/`detach(t)` create do **not**.

| # | rule | message (`NAME` = the variable's name as written in source) |
|---|---|---|
| E1 | a method call whose receiver is `x` or an index/field chain rooted at `x` (`x.m()`, `x[0].m()`, `x.f.m()`) outside a lock on `x`, unless `x` is a **handle variable** (below) and the receiver is `x` itself | `Method call on shared variable 'NAME' outside 'lock NAME { }': it would act on a copy; write 'lock NAME { ... }' at position P` (P = the root Ident) |
| E2 | an assignment into `x` (`x[k] = v`, `x.f = v`, `x.f[k] = v`) outside a lock on `x` | `Assignment into shared variable 'NAME' outside 'lock NAME { }': it would change a copy; write 'lock NAME { ... }' at position P` (P = the assignment) |
| E3 | `x = e` where `e` mentions `x` anywhere (including in a nested closure), outside a lock on `x` | `'NAME = ...' reads shared variable 'NAME' outside 'lock NAME { }': another thread can change it in between; write 'lock NAME { ... }' at position P` (P = the assignment) |
| E4 | a `lock` target that isn't a shared variable (a plain variable, a parameter, a function, a field path) | `'lock' takes shared variables, and 'TARGET' is not one at position P` (TARGET = the target as written, e.g. `p.x`) |
| E5 | `shared let` anywhere but the top level of a file | `'shared let' is only allowed at the top level of a file at position P` (P = the `shared` token) |
| E6 | the same variable twice in one `lock` | `'NAME' is locked twice in one 'lock' at position P` (P = the second) |
| E7 | a `let`/`shared let`/`fn` redeclaring a shared variable's name in the same scope | `'NAME' is a shared variable and can't be declared again in the same scope at position P` |

**Handle variables** (the E1 exemption): a `shared let` whose declared type annotation names
std:thread's `Thread`, `Semaphore` or `Channel`, or, with no annotation, whose initializer is a
direct call of std:thread's `spawn`, `semaphore` or `channel` (`shared let jobs = thread.channel()`).
These structs are plain ids; a method call on one never changes the shared value, so
`jobs.send(1)` / `jobs.recv()` are allowed outside a lock. The resolver recognises them
syntactically: after preprocessing, the annotation's type name or the callee's Ident name is
`P + "Thread"`/`"Semaphore"`/`"Channel"` or `P + "spawn"`/`"semaphore"`/`"channel"`, where `P` is
std:thread's module prefix (`__mah_m<idx>_`, from `pp`, §8.1; no prefix when std:thread isn't
imported, so nothing is a handle variable). E2 and E3 still apply to handle variables. A shared
variable that receives a handle later (`shared let ch = none` … `ch = thread.channel()`) is not one
unless annotated (`shared let ch: Unknown` is not enough; `shared let ch: thread.Channel = ...`
is). (A plain `let ch = thread.channel()` is usually what you want: every job's copy names the
same channel.)

**What the compile-time rules can't see** — documented in §11.5 and the language reference, and
partly warned by the checker (§8.6): every one of these acts on a copy and changes nothing shared:
passing a shared variable to a function that mutates its parameter (`mutate(xs)`); mutating a loop
variable bound from a shared iterable (`for let item in xs { item.n = 1 }`); mutating a local copy
(`let s = xs; s.push(1)`, `let m = shared_map; m[k] = v`). And a read-compute-write split across
function calls (`x = g()` where `g` reads `x`) is a lost update E3 can't see. The rule users are
given: **to change a shared value based on itself, or in place, do it inside `lock`.**

An undefined `lock` target gives the existing `Undefined variable 'x' at position P`. A `shared let`
redeclaring an existing name in the same scope gives the existing "variable is already defined"
error (no shadowing). Inner scopes (function parameters, `let`s in functions) may shadow a shared
name as usual. Reading is always allowed (`print(x)`, `x + 1`, `x[0]`, `x.f`, `for let i in x`,
`f(x)`, `x.await` is a read). The names in messages are the source names (the existing demangling of
compile errors applies to module variables).

### 5.4 Interactions

- **Silent copy of globals** (§4): shared variables have no frame slot, so they are never copied
  into a snapshot: every job reads and writes the one store.
- **Checker** (§8.6): a `shared let` is typed exactly like a `let`; reads and assignments are checked
  against its type; `lock` has the type of its body; two warnings (W2, W3; never errors) point at the copy
  traps the resolver can't see.
- **LSP** (§11.3): hover shows `**shared variable** \`NAME\`` and its type; `shared`/`lock` hover as
  keywords; rename and go-to-definition work through `lock` targets.
- **Formatter** (§11.4): `shared let` and `lock a, b { }` print with single spaces;
  `export shared let` is understood.

---

## 6. The runtime model (both VMs)

### 6.1 The process-wide runtime

One runtime object per program run (Python: per `_execute` call; Rust: per `run`), shared by the main
VM and every job VM. **One mutex** (`rt.lock`) guards all of its mutable state; pools use condition
variables on that same mutex. Never block (sleep, join, I/O) while holding it; sending into a VM's
done queue under it is fine.

State:
- `program` (Rust: `Arc<decode::Program>`; Python: the immutable `LinkedProgram`), `args`,
  `test_mode` (Rust), `started` (the run's start instant).
- `shared: map index → copied value` (absent = `none`).
- `locks: map index → { owner: (task id, vm id) or none, waiters: FIFO of (task id, vm, pending ref) }`;
  `waiting_on: map task id → index` (the lock edges of the wait-for graph).
- `awaiting: map task id → producer` (the await edges, §6.4): a producer is `Task(task id)`,
  `Job(job id)` or `Join(thread id)`; `job_roots: map job id → root task id` (a job's entry exists
  from the moment its root task is created until its VM's teardown); `tracking: bool` (false until
  the first `sharedlock` or `thread.spawn` of the run, then true forever; while false no await edge
  is recorded and no await check runs, so programs without locks or threads pay nothing).
- `threads: map id → Pool`, `next_thread` (from 1), `next_job` (from 1); `semaphores: map id →
  {permits, available, waiters FIFO}`, `next_semaphore`; `channels: map id → {queue FIFO of copies,
  capacity, closed, receivers FIFO, senders FIFO of (waiter, copy)}`, `next_channel`.
- Quiescence (§6.10): `vms: map vm id → VM record {done queue/sender, blocked: bool}` for every live
  VM (main + every job VM, registered when created, removed by `forget_vm`), `blocked_count`.
- The shared handle tables (files, sockets) and the single stdin reader (§6.3).
- The shared stdout (§6.3), `next_vm` (main VM is 0), Python only: `active`, `stopped`,
  `exit_code`, `root_done` (the main VM's done queue).

A **waiter** names a VM (its id and its done queue/sender) and a pending entry in that VM. Waking a
waiter = posting a completion to its VM's done queue; the VM settles its own Promise on its own
thread (Promises never cross threads). **Every post the runtime makes goes through one function,
`post(vm, item)`, called with `rt.lock` held**: it clears that VM's `blocked` flag (decrementing
`blocked_count`) before sending, so the quiescence count never counts a VM that is about to wake.

Task ids are **process-unique** (Python: a module-level `itertools.count(1)` in `runtime_values`;
Rust: a `static AtomicU64` starting at 1) and are assigned when a `Task` is created. Each task has
`held`: map index → `[working value, depth, unwinding]` (`unwinding` = 0, or the depth at which a
throw is leaving the lock, §6.4 `mark`); empty until it locks something. `held` is a shared
reference (Python: the dict object; Rust: `Rc<RefCell<..>>`) so an implicit-call sub-task can use
its caller's (§5.2).

Each pending entry a VM creates for a runtime wait (lock, semaphore, channel send/recv, join, job
reply) is flagged **internal**; the VM keeps `internal` = how many of its pending entries are
internal (fs/socket/process/stdin entries are external). Quiescence (§6.10) only considers VMs
whose pending entries are all internal.

### 6.2 Completions (what arrives in a VM's done queue)

Existing: a job's plain value (fs/socket/process), a stdin line. New:

| completion | payload | the VM does |
|---|---|---|
| job | ok/err + a copy | a job's reply (including `cancelled`): resolve (ok) or fail (err) the job Promise with the copied value (Python: the copy is already exclusively owned, use it as is; Rust: materialize it, §10.5); a failed job Promise is also appended to the VM's failed-promise list |
| settle | ok/err + a copy (or `none`) | the same for joins, send acknowledgements and channel-closed failures of waiting sends/receives; never appended to the failed-promise list (the std:thread wrapper awaits it at once) |
| lock | index + a copy of the store value | `task.held[index] = [materialized copy, 1, 0]`, then resolve the Promise with `none` |
| stuck | none | §6.10: fail **every** internal pending wait of this VM with `ThreadError stuck` |
| abandon (Python only) | none | raise `Abandoned` (sent by `rt.shutdown()` to every live job VM, §9.2) |
| sem | semaphore id | resolve the Promise with `none` |
| recv | channel id + a copy | resolve the Promise with the materialized copy |
| exit (Python only) | code | raise `ProgramExit(code)` in the main VM |

Each pending entry is removed and the VM's pending count decremented exactly as for today's I/O.
A completion whose pending entry is gone (the wait already failed with `stuck`, §6.10) is dropped.
For `job`: if the job Promise is **no longer Pending** (settled early by hand), the payload is
dropped, the pending entry removed and the count decremented, and nothing is appended to the
failed-promise list. Both VMs do exactly this.

### 6.3 Shared process resources

- **stdout — a line never tears.** `print(a, b)` compiles to one `write` (native `io.write`) per
  piece — each argument, each separator, the end text (codegen `_gen_print`) — so "one locked write
  per call" would not keep a line whole. Instead **every VM (main and job) has its own line
  buffer**, and only whole lines reach the one shared writer:
  - `io.write`/`io.print` (and every other stdout write a native makes, such as `input`'s prompt)
    **append** to the VM's line buffer. If the appended text contains `\n`, the VM hands
    everything up to and including the **last** `\n` to the shared writer in **one** write under
    the shared lock, and keeps the rest.
  - The VM hands over **whatever is left** (a partial line too) at every flush point: before
    blocking in `next_event` (waiting for a completion), before sleeping for a timer (Python's
    `drain_next_timer` gains this flush; Rust already has it), in `input()`'s native before the
    read is queued and in the legacy `io.input` before it reads, at the end of a job (before its
    reply is posted, so a job's output precedes the settling of its Promise), at program end, on
    every error exit before the error is written to stderr, and in `process.exit`.
  - The shared writer: Rust `Arc<Mutex<BufWriter<Stdout>>>` in the runtime (replacing the per-VM
    `BufWriter`); Python `sys.stdout` (looked up at each hand-over, as today) behind
    `rt.stdout_lock`. A flush point also flushes the shared writer.
  So lines from different threads interleave whole, in the order they were completed; a
  `print(..., end: "")` without a newline stays in its VM's buffer until that VM's next `\n` or
  flush point. With a single VM the bytes written and their order are exactly today's. **Rust must
  flush on every exit path** of `run` (including errors) and in `exit_program`, since the writer is
  no longer dropped with the VM.
- **stdin**: one reader thread per run (started on first use); a read request carries the
  requesting VM's done queue, lines are handed out in request order. `input()` in a job works.
- **files and sockets**: one handle table each per run, shared; ids are allocated under the table's
  lock. Only the **main** VM closes the tables (Rust: `IoHub` gets `owns_tables: bool`; only the
  owner's `Drop` closes sockets; Python: `_IoHub.close()` closes files/sockets only when it owns
  them). A handle closed by one thread is closed for all.
- **process environment**: per VM; a job starts with a copy of the submitter's table at queue time.
  `process.args()` is the same in every VM.
- **time**: `time.monotonic_ms` counts from the run's start (`rt.started`) in every VM;
  `time.now_ms` is wall time.
- **`process.exit(code)` in a job** exits the whole process with `code` as soon as possible;
  **the first exit wins** in both VMs:
  - Rust: every way the process ends (`exit_program` on any thread, and `main.rs`'s final
    `std::process::exit` after the VM thread returns) goes through one `pub fn exit_process(code)
    -> !` guarded by a process-wide `static EXITING: AtomicBool`: the first caller
    (`swap(true)` returned false) flushes its VM's line buffer and the shared writer and calls
    `std::process::exit` (or takes the test-mode path); every later caller parks forever
    (`loop { std::thread::park() }`), so `libc::exit` never runs twice concurrently.
  - Python: the worker catches `ProgramExit`, sets `rt.exit_code` (first one wins), `rt.stopped =
    True`, posts `exit` to `rt.root_done`; the main VM raises `ProgramExit(code)` when it next polls
    (§9.4).
  Output other threads produce in between may or may not appear.

### 6.4 Lock algorithms and the wait-for graph (`rt.lock` held throughout each, unless noted)

**The wait-for graph.** Nodes are task ids. A task's out-edges:
- **lock edge**: `waiting_on[T] = k` → the owner task of `locks[k]` (none: no edge);
- **await edges**: `awaiting[T] = Task(u)` → `u` (a *plain* edge); `Job(j)` → `job_roots[j]` if
  the job has started (else no edge); `Join(id)` → `job_roots[j]` for every job `j` currently running
  on thread `id` (several edges). Job and join edges are *strong*, like lock edges.

`cycle(start, T)`: depth-first search from task `start` over these edges with a visited set; true
when it reaches `T` along a path that, together with the edge being added (T → start), contains at
least one strong edge (a lock, job or join edge). (A cycle of plain same-VM awaits only is not
reported: its behaviour is unchanged from before M44.)

**Producers.** A Promise records who settles it (Python: `PromiseInstance.producer`, a new slot,
default `None`, holding `("task", id)`, `("job", id)` or `("join", id)`; Rust: `PromiseData.producer: Option<Producer>`, `enum Producer { Task(u64), Job(u64),
Join(u64) }`): the Promise of a same-VM `detach` task → `Task(that task's id)` (set by
`spawn_detached`); a job Promise → `Job(job id)` (set by `thread.submit`; the job id comes from
`rt.next_job`); the Promise of `thread.join` → `Join(thread id)`. Every other Promise has none.

`await_check(T, p)` — at the `await` opcode, when `p` is Pending and the task is about to suspend,
only if `rt.tracking` and `p` has a producer: under `rt.lock`, if `cycle(producer's first node(s),
T)` → do not suspend; throw `ThreadError deadlock` ("this await would never end ...") in `T` at the
`await` (catchable there). Else record `awaiting[T] = producer` and suspend. The entry is removed
(under `rt.lock`) when `T` resumes — its awaited Promise settled — or when its VM is torn down
(§6.6). A task records at most one edge at a time (Python: the task carries `awaits_edge: bool` so
resuming only takes the lock when an edge was recorded). For `Job(j)` whose root has not started,
the edge is still recorded: `cycle` resolves it to the root lazily, so the root's own lock request
later sees it.

`acquire(task T, index k, name)` (the `sharedlock` opcode). Sets `rt.tracking = true`.
1. `k in T.held` → depth += 1; return a Settled `none` Promise.
2. `locks[k].owner` is none → owner = (T.id, vm id); `T.held[k] = [copy of shared[k] (or none), 1,
   0]`; return Settled `none`.
3. owner (U, _) is another task → if `cycle(U, T)` (the new edge T → U is a lock edge, so it is
   strong) → return a **Failed** Promise with `ThreadError deadlock` naming `name` (not added to the
   failed-promise list; the codegen awaits it at once).
4. Else append waiter (T.id, this VM, a new **internal** pending entry recording (Promise, T, k)) to
   `locks[k].waiters`, `waiting_on[T.id] = k`; return the pending Promise.

Both checks run under the same `rt.lock` as the edge they add, so of two tasks closing one cycle
from two threads, the second always sees the first's edge: the cycle is detected exactly once, by
whichever request comes second (which one that is may differ between runs; tests print only the
`kind`).

`mark(task T, index k)` (`sharedunlock` with mode 1, emitted only in a `lock` body's handler, §8.5;
no `rt.lock` needed): `k in T.held` → `entry.unwinding = entry.depth`; else nothing.

`release(task T, index k, name)` (`sharedunlock` with mode 0):
1. `entry = T.held[k]` (missing → `RuntimeError.Internal` `sharedunlock without holding the lock`);
   `leaving_by_throw = (entry.unwinding == entry.depth)`; if so `entry.unwinding = 0`;
   depth -= 1; depth > 0 → done.
2. Remove `k` from `T.held`; strict-copy the working value (outside `rt.lock`); on refusal skip the
   write and, **unless** `leaving_by_throw`, remember the `not_sendable` error.
3. Under `rt.lock`: write `shared[k]` (unless refused); `grant_next(k)`.
4. Throw the remembered error, if any (the lock is already released). A refusal while
   `leaving_by_throw` is dropped: the error in flight keeps unwinding unchanged (§5.2).

`grant_next(k)`: owner = none; while waiters: pop the first `w`; `waiting_on.pop(w.task)`; owner =
(w.task, w.vm); `post` `lock` (k, copy of shared[k]) to w's VM; stop.

`sharedget k, mode` (task T; mode from the resolver, §7.1):
- mode 1 (**lexically locked read**): `T.held[k]`'s working value itself (missing →
  `RuntimeError.Internal` `sharedget without holding the lock`; codegen never emits that);
- mode 0: `k in T.held` → a **local copy** of the working value (no `rt.lock`): the §4.2 walk in a
  third mode, *local*, which copies containers, structs, enums, closures and frames like the other
  modes but keeps every Promise as **the same object** (the copy stays in this VM, so a Promise the
  body put in the working value is still the live one; the write-back refuses it later anyway);
  else a fresh copy of `shared[k]` (or `none`) taken under `rt.lock`.

`sharedset k, v` (task T): `k in T.held` → replace the working value with `v` (no copy); else
`RuntimeError.Internal` `sharedset without holding the lock` (codegen never emits that, §8.5).

### 6.5 Pools, workers, job VMs

Pool: `{ id, name, workers, capacity, jobs FIFO, running, running set (job ids), closed, alive
(live workers), joiners, cv }`. Every job gets an id from `rt.next_job` when it is queued (the
`Job(id)` producer of its Promise, §6.4); its root task's id goes into `job_roots` when the job VM
creates the root task, under `rt.lock`, before the root runs. Worker loop (one per worker, started by `spawn`):

```
loop:
  with rt.lock:
    while not jobs and not closed (and, Python, not rt.stopped): cv.wait()
    if Python and rt.stopped: alive -= 1; return
    if not jobs:                       # closed and drained
      alive -= 1
      if alive == 0: post settle(ok, none) to every joiner; joiners = []
      check_quiescence()
      return
    job = jobs.pop_front(); running += 1; add job.id to the running set
    vm_id = next_vm; vms[vm_id] = new VM record   # live from this instant (§6.10)
  outcome = run_job(job, vm_id)        # outside the lock; teardown (§6.6)
  with rt.lock:
    running -= 1; remove job.id from the running set
    remove vms[vm_id]                  # only now (§6.6 step 3)
    post job(outcome) to job.reply     # ignored if that VM is gone
    check_quiescence()                 # §6.10
```

`run_job(job)` creates a **job VM**: same program (Rust: the worker links `rt.program` once, when
the worker starts, and reuses that `LinkedProgram` for every job; Python: `rt.linked`), a fresh
heap, its own timers/IoHub/done queue/pending map/failed-promise list, `vm id = next_vm`, the job's
env table, the native method table built as for any VM, then the snapshot's user method entries,
decorators, hook tables and `fn_items` restored, then the copied callee/arguments. It drives the
root task (a task whose `watching_promise` is the job's root Promise; the VM's "main task" is a dummy
never-run task, so a root failure fails the root Promise instead of being fatal) and loops:

```
drive(root)
loop:
  if root Promise failed: mark it observed; outcome = err(strict copy of the error); break
  if root Promise settled and no timers and nothing pending: break
  if not next_event(): break           # nothing can happen any more
if outcome not set:
  if root Promise still pending: outcome = err(ThreadError stuck)
  elif the first unobserved failed promise exists: mark it observed; outcome = err(copy of its error)
  else: outcome = ok(strict copy of the root value)     # refusal → err(ThreadError not_sendable)
teardown (§6.6)
```

An unexpected host failure inside `run_job` (a Python exception escaping, a Rust `Err` that isn't a
Mah error, a Rust panic caught with `catch_unwind`) gives `err(RuntimeError.Internal { message })`
with the host's text.

### 6.6 Teardown of a job VM (`forget_vm`)

After the job loop, before replying: hand over the VM's line buffer (§6.3); then, under `rt.lock`:
1. remove this VM's waiters from every lock (and their `waiting_on`), semaphore, channel (receivers
   and senders; a waiting sender's message is dropped) and pool joiner list;
2. every lock whose owner's vm id is this VM: `grant_next` **without writing back** (after step 1,
   so the lock never passes to another task of this dying VM -- review fix: granting first could
   hand it to such a waiter, whose grant was then dropped with the VM and the lock never freed);
3. remove every `awaiting` entry of this VM's tasks, the job's `job_roots` entry, and the VM's
   record in `vms` (decrementing `blocked_count` if it was flagged blocked).

Then (outside the lock) drain this VM's done queue without blocking: a `sem` completion → give the
permit back (`semaphore release` logic, without the over-release check); a `recv` completion → put
the message back at the **front** of that channel (delivering it to the first waiting receiver if
there is one, else `queue.push_front`); anything else is dropped. Python additionally marks the VM
dead and closes its hub (not the shared tables).

### 6.7 Semaphore algorithms (`rt.lock` held)

- `acquire`: `available > 0` and no waiters → available -= 1, Settled `none`; else append waiter,
  pending Promise.
- `try_acquire`: `available > 0` and no waiters → available -= 1, `true`; else `false`.
- `release`: waiters → pop the first, post `sem`; elif `available == permits` → throw
  `over_release`; else available += 1.
- `available`: the count.

### 6.8 Channel algorithms (`rt.lock` held; M44b)

- `send(v)`: strict-copy `v` first (outside the lock; refusal → throw `not_sendable`
  synchronously). Closed → throw `closed` synchronously. A receiver waiting → pop the first, post
  `recv` (id, copy) to it, return Settled `none`. Else if capacity is none or `len(queue) <
  capacity` → push back, return Settled `none`. Else append (waiter, copy) to senders, return a
  pending Promise (settled with `none` when its message enters the queue or is handed over; failed
  with `closed` if the channel closes first).
- `recv()`: queue non-empty → pop front `m`; then if a sender waits, pop it, push its copy back and
  post `settle(ok, none)` to it; return Settled `m` (materialized). Else a sender waits (capacity 0)
  → pop it, post `settle(ok, none)` to it, return Settled with its copy. Else closed → return a
  **Failed** Promise with `closed`. Else append a receiver waiter, pending Promise.
- `try_recv()`: exactly `recv`'s first two branches, without waiting: queue non-empty → pop front
  (refilling from a waiting sender and settling it, as `recv` does) and return `some(m)`; else a
  sender waits (capacity 0) → **take that sender's message and post `settle(ok, none)` to it**,
  exactly as `recv` does, and return `some(its copy)`; otherwise (queue empty and no sender waiting,
  closed or not) `none`. Both VMs do exactly this.
- `close()`: idempotent; closed = true; every waiting receiver gets `settle(err, ThreadError
  closed)`; every waiting sender gets `settle(err, ThreadError closed)` (its message dropped).
  Queued messages stay receivable.
- `len()`: queue length. `closed()`: the flag.

### 6.9 Program end

Unchanged rule, wider "pending": the main VM ends once its main task has finished and nothing is
pending — now including queued/running jobs it submitted, lock/semaphore/channel waits and joins.
Then the existing unobserved-failure report runs (it includes failed job Promises). Idle threads
don't keep the program alive. Python then calls `rt.shutdown()` (§9.4); Rust's process exits.
Because every runtime wait counts as pending work, a run where those waits can never be satisfied
would block forever; §6.10 turns that into a `stuck` error instead.

### 6.10 Quiescence: waits that can never finish (both VMs, identical)

A VM is **blocked** when it is inside `next_event` about to wait with no timeout for a completion,
and every one of its pending entries is internal (`pending == internal`, §6.1), it has **no
timers**, and its done queue is empty. Such a VM can only be woken by another VM (through `post`).

`check_quiescence()` (with `rt.lock` held):
```
live     = len(vms)                                   # main + every job VM alive
starting = finishing non-empty                        # a job VM torn down, reply not posted yet
           or any pool p with (p.jobs non-empty or p.closed) and p.running < p.alive
if blocked_count == live and not starting:
    post stuck to the main VM                          # vm 0 is always in `vms` and blocked here
```

Where it runs:
1. **A VM about to block**: in `next_event`, after the non-blocking `get` found nothing, when the
   VM satisfies the blocked condition: take `rt.lock`; if the done queue is still empty, set its
   `blocked` flag, `blocked_count += 1`, `check_quiescence()`; release the lock; then wait on the
   done queue (Python with a test deadline: `get(timeout=left)` as today; the deadline doesn't make
   a VM unblocked). Python reads the queue size under `rt.lock` (`queue.empty()`); Rust uses
   `try_recv` under the lock and keeps an item it gets (then doesn't block). The waking `post`
   clears the flag (§6.1); a VM never clears its own flag except in `forget_vm`.
2. **A job VM ends** (in the worker, after `running -= 1`, under the same `rt.lock` hold), and
   **a worker exits**.

`finishing` (review fix): `forget_vm` removes a job VM from `vms` before the worker posts the
job's reply, so for that moment the job is neither a live VM nor a pending start. Without it, a VM
blocking in that window (typically the main VM awaiting that very reply) saw `blocked_count ==
live` and got a false `stuck`. `forget_vm` adds the job id to `rt.finishing`; the worker removes it
under the `rt.lock` hold that posts the reply.

The main VM handles `stuck` (§6.2) like this: under `rt.lock`, for each of its internal pending
entries, in creation order: remove the waiter from the runtime (lock waiters and `waiting_on`,
semaphore waiters, channel receivers/senders — a waiting sender's message is dropped —, pool
joiners; a job-reply entry has no runtime waiter), remove the pending entry (`pending -= 1`,
`internal -= 1`), and fail its Promise with `ThreadError{kind: "stuck", message: "the wait can never
finish: every thread is waiting"}`. A job reply that arrives later finds no pending entry and is
dropped (§6.2). Then the main VM continues: the failed waits throw in their tasks, which may catch
them and go on (if it blocks again with everything still stuck, the rule fires again).

Only the main VM's waits are failed: it is always part of a quiescent run (it is alive until the
program ends), so this is deterministic, and a main VM whose waits all fail either recovers or
ends the program (then Python's `rt.shutdown()` abandons the blocked job VMs, §9.2, and Rust's
process exits). Examples that now fail with `stuck` instead of hanging: `for let m in ch` on a
channel no job will send to or close; `sem.acquire()` with every permit held by tasks that are
themselves waiting; two jobs on different threads that `join` each other's thread when one of them
hasn't started; awaiting a job queued behind the awaiting job on a single-worker thread.

What it does not catch (documented): a run where some VM can still make progress — a timer, a
socket accept, stdin, a job still computing — even if the waits you care about are hopeless. A
server loop keeps such a program alive by design.

---

## 7. Bytecode 1.21

### 7.1 Opcodes

| name | code | operands | meaning |
|---|---|---|---|
| `sharedget` | `0x70` | `N` index, `S` name, `N` mode, `A` dest | §6.4 read; mode 1 = lexically locked (the working object itself), 0 = a copy |
| `sharedset` | `0x71` | `N` index, `S` name, `A` src | §6.4 set (only while held) |
| `sharedlock` | `0x72` | `N` index, `S` name, `A` dest | §6.4 acquire; dest ← a Promise |
| `sharedunlock` | `0x73` | `N` index, `S` name, `N` mode | mode 0: §6.4 release; mode 1: §6.4 `mark` (a `lock` body is being left by a throw) |

`N` is a varuint (any size; Rust keeps `u64`), `S` a string index (the variable's demangled source
name, used only in messages), the mode a second varuint `N` (like `loadtype`'s kind): 0 or 1;
linking refuses any other value with `RuntimeError.Internal` `bad mode M for 'OPCODE'` (both VMs;
a compiler never writes one). All four have since-minor 21 (`OPCODE_SINCE_MINOR`, Rust
`opcode_info`): a file below 21 using one is refused with the existing message `opcode 'NAME' at
instruction I requires minor version >= 21, but this file's minor version is M`.

IR tuples (codegen → `lower.py`, the 4-slot shape):

| IR | lowered |
|---|---|
| `("sharedget", (index, mode), name, dest)` | `Instr("sharedget", (index, intern(name), mode, dest))` |
| `("sharedset", index, name, src)` | `Instr("sharedset", (index, intern(name), src))` |
| `("sharedlock", index, name, dest)` | `Instr("sharedlock", (index, intern(name), dest))` |
| `("sharedunlock", (index, mode), name, None)` | `Instr("sharedunlock", (index, intern(name), mode))` |

Python linked forms: `("sharedget", k, name, mode, dest)`, `("sharedset", k, name, src)`,
`("sharedlock", k, name, dest)`, `("sharedunlock", k, name, mode)` (names as `str`, modes as
`int`). `mah dis` prints the mode as `locked`/`copy` (sharedget) and `release`/`mark`
(sharedunlock).

### 7.2 Natives (all since minor 21)

| name | arity | arguments → result |
|---|---|---|
| `thread.spawn` | 3 | `name` (String/none), `workers`, `capacity` (Number/none) → `[id, name]` |
| `thread.submit` | 3 | a `Thread` struct, a function, a Vector of positional args → Promise (§2.3) |
| `thread.close` | 2 | `id`, `cancel` (Bool) → `none` |
| `thread.join` | 1 | `id` → Promise of `none` (throws `deadlock` from its own job) |
| `thread.pending` | 1 | `id` → Number |
| `thread.current` | 0 | → `[id, name]` |
| `thread.cores` | 0 | → Number |
| `thread.semaphore_new` | 1 | `permits` → id |
| `thread.semaphore_acquire` | 1 | `id` → Promise of `none` |
| `thread.semaphore_try_acquire` | 1 | `id` → Bool |
| `thread.semaphore_release` | 1 | `id` → `none` (throws `over_release`) |
| `thread.semaphore_available` | 1 | `id` → Number |
| `thread.channel_new` | 1 | `capacity` (Number/none) → id |
| `thread.channel_send` | 2 | `id`, value → Promise of `none` (throws `not_sendable`/`closed`) |
| `thread.channel_recv` | 1 | `id` → Promise of the message |
| `thread.channel_try_recv` | 1 | `id` → `some(message)` / `none` |
| `thread.channel_close` | 1 | `id` → `none` |
| `thread.channel_len` | 1 | `id` → Number |
| `thread.channel_closed` | 1 | `id` → Bool |

Natives throw `ThreadError` **as thrown Mah values** (Python: `raise MahThrow(thread_error(...))`;
Rust: `Err(RuntimeError::thrown_value(...))`), so `try`/`catch` sees them at the call.

### 7.3 Versioning and every pin that moves

- `mah/bytecode/format.py`: `MINOR = 21`; the docstring paragraph "M44 bumps MINOR to 21: the
  shared-variable opcodes `sharedget`/`sharedset`/`sharedlock`/`sharedunlock` and the `thread.*`
  natives behind std:thread (docs/MAHC_FORMAT.md #4.4/#4.6/#6.11)"; `OPCODES` rows; four
  `OPCODE_SINCE_MINOR` entries = 21; 19 `NATIVE_ARITIES` rows under `# M44 (1.21): std:thread.`;
  19 `NATIVE_SINCE_MINOR` entries = 21.
- `_file_minor` needs no change (table driven): a program that declares a shared variable, uses
  `lock`, or imports `std:thread` is written as 1.21.
- `runtime/src/decode.rs`: `MINOR = 21`; `assert_eq!(MINOR, 21)` in its test; opcode table rows;
  `native_since_minor` rows.
- Tests asserting `MINOR == 20` change to 21: `tests/test_decorators.py` (`# M42` → `# M44`),
  `tests/test_hooks.py`, `tests/test_http.py`, `tests/test_reflection.py`, `tests/test_socket.py`,
  `tests/test_tls_server.py`. `tests/test_bytecode.py`: `test_unsupported_minor_version` and
  `test_a_newer_file_names_the_natives_this_vm_lacks` use `data[6] = 22` (comment: "M44: this VM
  implements minor version 21, so the smallest unsupported one is 22"); its per-example minor map
  gains `"threads.mh": 21`. Nothing else pins 20 (`minor_of(...) == 20` assertions for std:socket /
  std:http programs stay 20: they don't use 1.21 features).
- `mah --version` / `mah-vm --version` print the new minor automatically.

---

## 8. Part 1 — the compiler

### 8.1 `mah/preprocessor.py`

- `analyze_module`: `export` followed by `id shared`, `id let`, `id NAME` → `NAME` is exported and
  top level (advance past `export` only, like the existing exportable-declaration branch).
- The rewrite pass (the `export` branch near "drop the `export` keyword only"): `export` followed by
  `shared` `let` drops the `export` keyword only (today it would take the "bare `export name`" path
  and drop `export shared`).
- `_uses_prelude`: the two triggers of §2.2.
- `Preprocessed.std_prefix(name: str) -> Optional[str]`: the mangling prefix
  (`__mah_m<idx>_`) of the std module `std:<name>` if the program includes it, else `None` (from
  `module_index`). The resolver (handle variables, §5.3) and the checker (`Thread` recognition,
  §8.6) use `std_prefix("thread")`; with no preprocessor (a bare `compile_source` of text without
  imports) it is `None`, so nothing is a std:thread type.

### 8.2 `mah/compiler/ast_nodes.py`

- `LetStmt`: `shared: bool = field(default=False)` (part of the AST shape; the formatter's
  normalized-AST comparison includes it) and `shared_index: Optional[int] = field(default=None,
  repr=False)`. For a shared let, `position` is the `shared` token's position.
- `Ident`: `shared_index: Optional[int] = field(default=None, repr=False)`, `shared_name:
  Optional[str] = field(default=None, repr=False)` (the demangled source name, via
  `runtime_values.display_name`), `shared_locked: bool = field(default=False, repr=False)` — set
  by the resolver on **every** shared Ident (a read or an assignment target) written lexically
  inside a `lock` on its variable (§5.3's definition). A read with it is a lexically locked read
  (`sharedget` mode 1); an assignment target with it is a plain `sharedset`.
- `DetachExpr`: `thread: Optional[object] = field(default=None)`, `thread_position:
  Optional[int] = field(default=None, repr=False)` and `paren_head: Optional[object] =
  field(default=None, repr=False)` (§3.1: the parenthesized expression that turned out to be the
  operand's start, in today's form; only the checker reads it).
- New nodes (dataclasses):
  - `LockExpr(targets: list, block: Block, position: int)` — `targets` are the parsed target nodes
    (`Ident`, or a `FieldAccess` chain for a dotted target before preprocessing); `block` is the
    desugared block (§8.3); `position` is the `lock` token.
  - `LockAcquire(name: str, position: int)` and `LockRelease(name: str, position: int)` — `name` is
    the target as text (`"a"`, or `"lib.hits"` if dotted); resolver-filled
    `shared_index: Optional[int]`, `shared_name: Optional[str]` (`field(default=None, repr=False)`).
- Update the module docstring with a short M44 paragraph.

### 8.3 `mah/compiler/parser.py`

- `_parse_detach`: §3.1 exactly, including its two compile errors (raised as the parser's usual
  `SyntaxError` with the exact §3.1 messages, at the offending token's position) and `paren_head`.
- `shared let`: in `_parse_block_items`, before the `_STATEMENT_LEADING` check: if the current token
  is `ID` `shared` and the next token (`self.lexer.peek_token()`) is `LET` and on the same line
  (no `\n` in the source text between the end of `shared` and the start of `let`): consume `shared`,
  `stmt = self.parse_stmt()` (a `LetStmt`), set `stmt.shared = True`, `stmt.position = <shared
  token position>`, append, continue. (Top-level-only is the resolver's E5.)
- `lock`: in `_parse_primary`'s `ID` branch, before the existing handling: if `tok.literal ==
  "lock"` and the next token is `ID` on the same line → `self.advance()` and `return
  self._parse_lock(tok)`.
- `_parse_lock(lock_tok)`: parse targets: `target := ID ("." ID)*` (an `Ident`, or nested
  `FieldAccess`), separated by `,`; then `body = self.parse_block()` (anything but `{` → the usual
  "Invalid syntax" error). Build, for each target `t` (text `name`, position `p`):
  `ExprStmt(value=LockAcquire(name, p), position=p)` then
  `DeferStmt(closure_expr=FnExpr(name=None, params=[], body=Block(stmts=[ExprStmt(value=LockRelease(name, p), position=p)], position=p, tail=None), position=lock_tok.position, name_position=None, param_positions=[]), position=lock_tok.position)`.
  The desugared block is `Block(stmts=[...those, in target order...], tail=body,
  position=lock_tok.position)`. Return `LockExpr(targets, block, lock_tok.position)` (no postfix
  chain).
- `_parse_block_items`: add `LockExpr` to the block-shaped tuple that needs no `;`.
- Parser docstrings: a short M44 note in `_parse_detach`.

### 8.4 `mah/compiler/resolve.py`

- `Symbol`: add `shared_index` to `__slots__` (default `None`); kind `"shared"` for shared lets.
- Counter `self._shared_count = 0`; a stack `self._held_locks: list[frozenset]` starting as
  `[frozenset()]`.
- `_bind_ident(ident)`: look the name up like `_resolve_ident_address` (recording the reference, the
  same `NameError` when undefined); if the symbol is shared set `ident.shared_index`,
  `ident.shared_name` and leave `address` `None`, else set `ident.address` as today. Replace the
  three `_resolve_ident_address` call sites (plain `Ident`, `FieldAccess` with an `Ident` object,
  `MethodCall` with an `Ident` object) with it. Scope entries for shared symbols are
  `(frame_level_of_main, None, symbol)`; nothing else may read their slot.
- `LetStmt` with `shared`: E5 unless `self._at_top_level()`; resolve the value first (even for an
  `FnExpr` value: no early self-declaration); then E7/"already defined" checks; declare a symbol of
  kind `"shared"` with `shared_index = self._shared_count` (then increment), `top_level_let = True`,
  the usual type hint; set `stmt.shared_index`. Any later `let`/`fn`/`shared let` declaring that
  name **in the same scope** raises E7 (check in `_declare`, before the shadowing logic).
- `LockExpr`: for each target: not an `Ident` → E4 (TARGET = dotted text); `_bind_ident` it;
  not shared → E4; duplicate → E6. Push `self._held_locks[-1] | {indices}`, `self.resolve_expr(
  expr.block)`, pop.
- `LockAcquire`/`LockRelease`: look the name up **without recording a reference** (walk
  `self.scopes` like `_lookup` but don't append to `references` or `position_index`); it must be a
  shared symbol (else E4); set `shared_index`/`shared_name`.
- `_resolve_fn_expr`: push `frozenset()` on `_held_locks` around the body, except when the `FnExpr`
  is a `DeferStmt`'s closure (pass a flag from the `DeferStmt` case), which keeps the current set.
- `_bind_ident` of a shared symbol also sets `ident.shared_locked = shared_index in
  self._held_locks[-1]` (reads and targets alike).
- **Handle variables** (§5.3): when declaring a shared let, mark the symbol `handle = True` if
  `P = pp.std_prefix("thread")` is not `None` and either the let's type annotation's name is
  `P + "Thread"`/`"Semaphore"`/`"Channel"`, or it has no annotation and its value is a `Call` whose
  callee is an `Ident` named `P + "spawn"`/`"semaphore"`/`"channel"`. (`Symbol.__slots__` gains
  `handle`, default `False`; the resolver receives `pp` as it already does for demangling.)
- E1: in the `MethodCall` case, after binding/resolving `expr.obj`: walk `obj` through
  `FieldAccess.obj`/`Index.obj` to the root; a shared `Ident` whose index is not in
  `_held_locks[-1]` → E1 (position of the root Ident) — **unless** the symbol is a handle variable
  and `obj` is that `Ident` itself (`jobs.send(1)` is fine; `jobs.x.m()` is still E1).
- `AssignStmt`, after resolving target and value: target `Ident` shared → if held: set
  `target.shared_locked = True`; else if any `Ident` in the value subtree (generic dataclass walk,
  descending into nested `FnExpr`s) has the same `shared_index` → E3. Target `FieldAccess`/`Index`
  whose root is a shared `Ident` not held → E2.
- `DetachExpr`: resolve `expr.thread` (when set) before `expr.call`.
- `_type_hint`: unchanged (`DetachExpr` → `"Promise"`).
- The resolver exposes `self.shared_names: list[str]` (index → demangled name) for the LSP.

### 8.5 `mah/compiler/codegen.py`

- `Ident` read with `shared_index`: `dest = self._temp()`; emit `("sharedget", (idx, 1 if
  ident.shared_locked else 0), name, dest)`; return `dest`. (The read happens here, at the
  expression's place in left-to-right order — §5.2 "Read point".)
- `_gen_store` for an `Ident` with `shared_index`: `shared_locked` → emit `("sharedset", idx, name,
  src)`; else `_gen_shared_store(idx, name, src)`:
  ```
  p = temp; emit ("sharedlock", idx, name, p)
  r = temp; emit ("await", p, None, r)
  emit ("sharedset", idx, name, src)
  emit ("sharedunlock", (idx, 0), name, None)
  ```
- `LetStmt` with `shared`: `src = gen_expr(value)`; `_gen_shared_store(stmt.shared_index, name,
  src)`.
- `LockAcquire`: `p = temp; ("sharedlock", ...p); r = temp; ("await", p, None, r)`; return `r`.
  `LockRelease`: emit `("sharedunlock", (idx, 0), name, None)`; return a temp loaded with `none`
  (`("ld", NONE_VALUE, None, t)`).
- `LockExpr` → `_gen_lock(expr, dest)`: exactly `gen_block`'s code for `expr.block` (its
  `deferpush`, its statements — the acquires and the release `defer`s —, the tail, the normal-exit
  `_emit_defer_unwind(1)`, and `return`/`break`/`continue` unwinding through the generic paths),
  except that **the tail (the user's body) is wrapped in its own handler region** (`_open_region`
  right before the tail's code, `_close_region` right after it):
  ```
  ...deferpush, acquire a, defer release a, acquire b, defer release b...
  R = _open_region()
  v = gen_expr(tail); ("=", v, None, dest)
  segments = _close_region(R)
  jmp END
  HANDLER:                                  # entries: _emit_handler_entries(segments, HANDLER, err)
    for each target, in source order: ("sharedunlock", (idx, 1), name, None)     # mark
    ("throw", err, None, None)              # re-throw: the enclosing handler drains the defers
  END:
  ..._emit_defer_unwind(1) (normal exit)...
  ```
  The region covers only the body, never the acquires: a failing acquire (`deadlock`) leaves the
  locks taken so far unmarked, which is safe because their working values are fresh copies of the
  store and can't fail write-back. A throw caught **inside** the body never reaches the handler.
  `return`/`break`/`continue` leave through `_emit_defer_unwind`, not the handler, so they are
  normal exits. Nested functions pause the region as usual (`_pause_regions`).
- `DetachExpr` with `thread`: `t = gen_expr(expr.thread)`; `c = gen_expr(expr.call.callee)` (the
  synthesized closure); `v = temp; ("vector", (), None, v)`; `dest = temp; ("native",
  "thread.submit", (t, c, v), dest)`; return `dest`. (The `input`/`sleep_async`/method-call
  special cases apply only when `thread` is `None`.)

### 8.6 `mah/compiler/typecheck.py`

- **std:thread's `Thread`** is the struct type whose (mangled) name is `pp.std_prefix("thread") +
  "Thread"` (§8.1); a user's own `struct Thread` is not it. Likewise for `Semaphore`/`Channel`.
- `DetachExpr` with `thread`: check `thread` first; if its type is **known** — anything but Unknown
  (and not the type of a bare `none`) — and is not std:thread's `Thread` struct, primitives
  included → a `"mismatch"` diagnostic `detach(...) needs a thread.Thread, got T` at
  `expr.position` (T = the type's display text: `Number`, `String`, `Vector<Number>`, `Point`, a
  user's own `Thread`, ...). The result is `Promise<T>` of the closure call's type, with the same
  `ESet` handling as today.
- **Warnings** (kind `"warning"`: reported at every level, never errors; exact messages, position
  in parentheses):
  - W1 — `DetachExpr` without `thread` whose `paren_head` has type std:thread's `Thread`
    (`detach(t)` then a newline then `foo()`, `detach(t) [1, 2]`, `detach(t) -x`, ...):
    `'detach (...)' here is not the thread form, so nothing runs on the thread; put the operand on
    the same line as 'detach(t)', or write 'detach(t) { ... }'` (the `detach` token).
  - W2 — a shared `Ident` without `shared_locked` used **directly** as an argument of a call
    (`f(xs)`, `obj.m(xs)`, `T { f: xs }` is not a call and isn't warned), when the variable's type
    is a Vector, Map, Bytes, struct or enum type that isn't a std:thread handle (Unknown, Number,
    String, Bool and Option of those are not warned): `shared variable 'NAME' is passed as a copy:
    changes the callee makes to it are lost; to change it, call inside 'lock NAME { ... }'` (the
    Ident).
  - W3 — `for let ITEM in X { ... }` where `X` is a shared `Ident` without `shared_locked`, and the
    body (not nested functions) contains an assignment into `ITEM` (`ITEM.f = v`, `ITEM[k] = v`, or
    deeper chains rooted at `ITEM`): `'ITEM' is a copy of an element of shared variable 'NAME':
    assigning into it changes nothing shared; loop inside 'lock NAME { ... }'` (the first such
    assignment).
  Inside `lock NAME { }` (lexically) neither W2 nor W3 fires: there the argument or the iterable is
  the working object itself and changes stick.
- `LockExpr` → the type of `expr.block`; `LockAcquire`/`LockRelease` → `none`, no errors added
  (a `deadlock`/`not_sendable` ThreadError from the VM is untracked, like a `RuntimeError`).
- `std:thread`'s methods that can throw `ThreadError` declare `throws ThreadError` (like
  `std:socket`'s `throws SocketError`), so in a strict project a top-level call of one needs a
  `try`, exactly as for sockets; inside functions the errors are inferred as usual.
- `shared` lets are checked exactly like lets. `examples/threads.mh` and `mah/std/thread.mh` must
  have no strict diagnostics (`tests/test_typecheck.py`'s whole-program test). If the checker trips
  on the new nodes, fix the checker; never change the example's output.

### 8.7 `mah/bytecode/lower.py`, `format.py`, `encode`/`decode`/`disasm`

`lower.py`: the four mappings of §7.1. `format.py`: §7.3. `encode.py`, `decode.py`, `disasm.py`
are table driven: verify that `mah dis` shows the new instructions with their name operand and that
`decode` refuses them below minor 21 (tests in §12.1); change them only if that fails.

---

## 9. Part 1 — the Python VM

### 9.1 `mah/runtime_values.py`

- Move `_Absent`/`ABSENT` here from `code_interpreter.py` (re-export from `code_interpreter` so every
  existing import keeps working).
- `_TASK_IDS = itertools.count(1)`; `Task.__slots__` gains `"id"`, `"held"`, `"awaits_edge"`;
  `__init__` sets `self.id = next(_TASK_IDS)`, `self.held = {}`, `self.awaits_edge = False`.
  `held` entries are 3-item lists `[working, depth, unwinding]`.
- `PromiseInstance.__slots__` gains `"producer"` (default `None`; §6.4).

### 9.2 `mah/thread_runtime.py` (new; must not import `mah.compiler`/`mah.preprocessor`/`mah.lsp`)

- `class NotSendable(Exception)`; `class Abandoned(BaseException)` (a job VM told to stop).
- `thread_error(kind, message) -> StructInstance("ThreadError", {"kind": kind, "message": message})`.
- `copy_values(roots: list[tuple[object, bool]]) -> list` — §4.2, one memo for all roots, iterative,
  roots copied in list order (callers put strict roots first). `copy_value(v, strict=True)`.
  Promise env copies: `PromiseInstance()` with `variant`/`fields` set directly and `observed =
  True`. Closures: `Closure(code_address, <frame shell>, slot_count, param_count, name, params,
  index, rest)` then `identity` copied. Frames: `Frame([], None)` shells filled later. The memo maps
  `id(obj) → (obj, copy)` (keeping `obj` alive).
- `class ThreadRuntime` (§6.1): `__init__(self, linked, args)`; `lock = threading.Lock()`;
  `stdout_lock = threading.Lock()`; the maps; `tables = HandleTables()`; `stdin` (one reader:
  requests are `(done_queue, promise)`; started lazily; reads `sys.stdin` as captured at start);
  `started = time.monotonic()`; `decimal_context = decimal.getcontext().copy()`;
  `vm_ids = itertools.count(1)`; `active = stopped = False`; `exit_code = None`; `root_done = None`
  (set by `_execute`). Methods: `post(vt, item)` (§6.1), the §6.4/§6.7/§6.8 algorithms,
  `await_check`, `check_quiescence`/`about_to_block(vt)` (§6.10), `spawn`, `submit`, `close`,
  `join`, `forget_vm(vt)`, `request_exit(code)`, and `shutdown()`:
  1. `with stdout_lock: stopped = True` (so no job VM can be inside a write when it flips);
  2. `with lock:` every pool closed, its queue cleared without replies, `cv.notify_all()`; then
     `post(vt, (None, "abandon", None))` to **every live job VM** in `vms`, so a job VM blocked in
     `io.done.get()` with no timeout wakes and raises `Abandoned` from `settle_io`;
  3. stdin reader stopped.
  In the in-process `mah test` runner this runs once per test (`_execute`'s `finally`), so a timed-out
  or failed test leaks no blocked job thread.
- **Stack size**: the first `spawn` of the process calls `threading.stack_size(64 * 1024 * 1024)`
  once (wrapped in `try/except (ValueError, RuntimeError)`, ignoring a platform that refuses)
  before starting any worker, because the VM's recursive helpers (`to_str`, equality, the
  formatter) would overflow the platform default (512 KiB on macOS) on values the main thread
  handles.
- `class HandleTables`: `files`, `sockets` dicts, `next_file`, `next_socket` (from 1), `lock`.
- `class Pool`, `class Job` (fields: `id`, `callee`, `bound`, `methods`, `decorators`, `hook_types`,
  `hook_params`, `fn_items`, `env`, `reply` (the submitter's done queue), `promise`), waiter records.
- `class VmThreads` (one per VM): `rt`, `vm_id`, `io`, `pool` (None in the main VM), `dead`,
  `make_job` (installed by `_execute_with`), `blocked` (the §6.10 flag, guarded by `rt.lock`),
  `internal` (count, §6.1), and the opcode helpers `get(task, k, mode)`, `set(task, k, v)`,
  `lock(task, k, name) -> PromiseInstance`, `unlock(task, k, name, mode)`, plus `poll()` (§9.4) and
  the VM's **line buffer** `out` (§6.3), a small object with `write(text)` and `flush()`:
  - `write(text)`: append to a list of pieces; if `"\n" in text`, join, split at the last `"\n"`,
    hand the head over, keep the tail;
  - `flush()`: hand over everything left, then flush `sys.stdout`;
  - hand-over: `with rt.stdout_lock:` if `rt.stopped` and this is a job VM → raise `Abandoned`
    (checked **inside** the lock, so it can't race with `shutdown`, which flips `stopped` holding
    the same lock); else `sys.stdout.write(text)`.
  `NativeContext.stdout` returns `ctx.thread.out` when `ctx.thread` is set (§9.5), so every native
  that writes to stdout (`io.write`, `io.print`, `input`'s prompt + its `flush()`) goes through it.
- Worker threads: `threading.Thread(target=_worker, args=(rt, pool), daemon=True,
  name=f"mah-{pool.name}-{i}")`; `_worker` first calls `decimal.setcontext(rt.decimal_context.copy())`,
  then runs §6.5, calling `code_interpreter.run_job` (imported inside the function). It catches
  `ProgramExit` (→ flush the job VM's line buffer, `rt.request_exit(code)`, worker returns without
  replying), `Abandoned` (returns without replying) and every other exception (→
  `err(RuntimeError.Internal)`). After each job, under `rt.lock`: `running -= 1`, remove the job id
  from the pool's running set, `check_quiescence()`; a worker that exits calls it too.

### 9.3 `mah/thread_natives.py` (new)

`NATIVES = {"thread.spawn": (3, _spawn), ...}` for the 19 natives of §7.2; each impl takes
`(ctx, args)` and delegates to `ctx.thread` (`VmThreads`) / `ctx.thread.rt`. `thread.submit` calls
`ctx.thread.make_job(closure, values)` for steps 2–3 of §2.3. Registered in `mah/natives.py`
(`NATIVES.update(_THREAD_NATIVES)` after the socket natives).

### 9.4 `mah/code_interpreter.py`

- `_IoHub.__init__(self, args=(), env=None, runtime=None, owns_tables=True)`: `files`/`sockets`
  are the runtime's shared dicts; `add_file`/`add_socket` allocate ids under `tables.lock`;
  `env` = the given copy or a fresh snapshot; `read_line` goes through `runtime.stdin`;
  `close()` closes files/sockets only when `owns_tables`.
- `_execute(linked, test_slot=None, deadline=None, args=())`: create `done`-owning root hub and
  `rt = ThreadRuntime(linked, args)`, `rt.root_done = io.done`, `vt = VmThreads(rt, 0, io, None)`,
  `rt.vms[0] = vt` (the main VM is live for the whole run, §6.10);
  `try: return _execute_with(linked, io, test_slot, deadline, vt) finally: rt.shutdown();
  io.close()`.
- `_execute_with(linked, io, test_slot, deadline, threads, job=None)`:
  - **Task stack**: a local list `stepping`; `step_task` appends its task on entry and pops it in a
    `finally`. `invoke_sync` creates its sub-task as today and then, if `stepping` is non-empty,
    sets `sub_task.id = stepping[-1].id` and `sub_task.held = stepping[-1].held` (the same dict
    object), §5.2. (A `to_string` called from a native during a task's step finds that task on
    top; nothing else changes.) `spawn_detached` and the job root keep fresh identities, and
    `spawn_detached` sets `promise.producer = ("task", new_task.id)`.
  - **Await**: in the `await` opcode, when the Promise is Pending and the task is about to suspend:
    `if threads.rt.tracking and promise.producer is not None:` `threads.rt.await_check(task,
    promise)` (raises the `deadlock` MahThrow in the task, or records the edge and sets
    `task.awaits_edge`). Where a suspended task is resumed, `if task.awaits_edge:` remove its
    `awaiting` entry under `rt.lock` and clear the flag.
  - `NativeContext(..., started=threads.rt.started, thread=threads)`; install
    `threads.make_job = make_job`, a nested function: bind (`_bind_params` with the closure's
    label), copy per §4.1 (strict args first via `copy_values`), return a `Job`.
  - `_link_instr` and `_exec`: the four opcodes (§6.4, via `threads`).
  - `step_task`: count steps unconditionally; every 4096 steps: the deadline check (as today) and,
    if `threads.rt.active`, `threads.poll()`. `poll()`: main VM → `rt.exit_code is not None` →
    `raise ProgramExit(rt.exit_code)`; job VM → `rt.stopped` → `raise Abandoned()`.
  - `next_event`: poll first; flush the line buffer (`threads.out.flush()`, replacing today's
    `sys.stdout.flush()`) before any blocking wait; before a wait with no timers, call
    `threads.rt.about_to_block(threads)` (§6.10: sets the flag only if `io.pending ==
    threads.internal` and the done queue is empty, then `check_quiescence`); then
    `io.done.get(timeout=...)` as today. `drain_next_timer` flushes the line buffer before
    sleeping. `settle_io` handles the §6.2 kinds (`"job"`, `"settle"`, `"lock"`, `"sem"`, `"recv"`,
    `"stuck"`, `"abandon"`, `"exit"`), each item shaped `(promise, kind, payload)` (`stuck`,
    `abandon`: `(None, kind, None)`; `exit`: `(None, "exit", code)`); every internal entry it
    settles also decrements `threads.internal`.
  - Every end path of the VM (normal end, uncaught error before it is written to stderr,
    `ProgramExit`, `_Timeout`) flushes the line buffer.
  - With `job`: restore the job's tables, build the root as §6.5 (dummy `main_task`), run the job
    loop, return `(ok: bool, copied value)`; without `job`: today's code.
- `run_job(linked, rt, pool, job) -> (bool, object)`: new hub `_IoHub(rt.args, env=job.env,
  runtime=rt, owns_tables=False)`, `vt = VmThreads(rt, next(rt.vm_ids), io, pool)`; `try: return
  _execute_with(linked, io, None, None, vt, job) finally: rt.forget_vm(vt)` (+ the §6.6 drain,
  `vt.dead = True`, `io.close()`).
- `rt.active = True` is set by `spawn`.

### 9.5 `mah/natives.py`

`NativeContext` gains `thread` (slot, default `None`) and an optional `started`; `monotonic_ms` uses
it. The `stdout` property returns `self.thread.out` (the VM's line buffer, §9.2) when `thread` is
set, else `sys.stdout` (unchanged behaviour for any other caller); `_io_print`/`_io_write`/
`_io_read_line` stay as they are and so go through the line buffer. `_io_input` (legacy
`io.input`) calls `ctx.stdout.flush()` before reading.

---

## 10. Part 2 — the Rust VM (parity)

Read §2–§7 and §9 (the Python design is the reference); implement the same observable behaviour.

### 10.1 `runtime/src/decode.rs`

`MINOR = 21`; `opcode_info`: `0x70 => ("sharedget", Some(21))`, `0x71 sharedset`, `0x72
sharedlock`, `0x73 sharedunlock`; decode arms: `N` = `pr.varuint()?` (u64), `S` = `decode_s`,
`A` = `decode_addr`, the mode = `pr.varuint()?`; `RawInstr::{SharedGet { index: u64, name: usize,
mode: u64, dest }, SharedSet { index, name, src }, SharedLock { index, name, dest }, SharedUnlock {
index, name, mode: u64 }}` (link refuses a mode other than 0/1, §7.1); `native_since_minor`:
the 19 names → 21. Tests: `assert_eq!(MINOR, 21)`; a minor-20 file using `0x70` is refused with
`opcode 'sharedget' at instruction 0 requires minor version >= 21, but this file's minor version is
20`; each new opcode decodes. Add `const _: fn() = || { fn f<T: Send + Sync>() {} f::<Program>(); };`.

### 10.2 `runtime/src/decimal.rs`

`Decimal` holds `Rc<BigUint>` (not `Send`). Add `pub struct SendDecimal { neg: bool, exp: i64,
coeff: SendCoeff }`, `enum SendCoeff { Small(u64), Big(BigUint) }` (`BigUint` is plain data),
`Decimal::to_send(&self) -> SendDecimal` and `Decimal::from_send(SendDecimal) -> Decimal` (exact,
no re-canonicalization needed).

### 10.3 `runtime/src/vm/value.rs`

`Task` gains `pub id: u64` (from `static NEXT_TASK_ID: AtomicU64 = AtomicU64::new(1)`, taken in
`new_task`), `pub held: Rc<RefCell<Vec<Held>>>` with `pub struct Held { index: u64, value: Value,
depth: u32, unwinding: u32 }` (shared by reference with implicit-call sub-tasks, §5.2), and `pub
awaits_edge: bool`. `PromiseData` gains `pub producer: Option<Producer>` (§6.4).

### 10.4 `runtime/src/vm/link.rs`

`LinkedInstr::{SharedGet { index: u64, name: Rc<str>, locked: bool, dest }, SharedSet { .., src },
SharedLock { .., dest }, SharedUnlock { index, name, mark: bool }}`; 19 `NativeFn` variants (`ThreadSpawn`, `ThreadSubmit`,
`ThreadClose`, `ThreadJoin`, `ThreadPending`, `ThreadCurrent`, `ThreadCores`, `SemaphoreNew`,
`SemaphoreAcquire`, `SemaphoreTryAcquire`, `SemaphoreRelease`, `SemaphoreAvailable`, `ChannelNew`,
`ChannelSend`, `ChannelRecv`, `ChannelTryRecv`, `ChannelClose`, `ChannelLen`, `ChannelClosed`) and
their `native_by_name` rows.

### 10.5 `runtime/src/vm/thread.rs` (new)

- `SendValue { None, Absent, Bool(bool), Number(SendDecimal), Str(String), Type { kind: u8, index:
  usize, name: String }, Ref(usize) }`.
- `SendNode { Vector(Vec<SendValue>), Map(Vec<(SendValue, SendValue)>), Bytes(Vec<u8>), Struct {
  type_name: String, fields: Vec<(String, SendValue)>, thrown_at: Option<usize>, backtrace:
  Option<Vec<usize>> }, Enum { type_name, variant: String, fields, thrown_at, backtrace },
  Promise(Result<SendValue, SendValue>) /* settled / failed */, Closure { func: usize, identity:
  usize, frame: usize }, Frame { slots: Vec<SendValue>, parent: Option<usize> } }`.
- `SendGraph { nodes: Vec<SendNode> }`, `Payload { graph: SendGraph, root: SendValue }`, and a
  `Snapshot { graph, callee: SendValue, args: Vec<SendValue>, methods: Vec<(String, String,
  Option<String>, SendValue, bool)>, decorators: Vec<((u64, usize, usize), SendValue)>,
  hook_types: Vec<(String, SendValue)>, hook_params: Vec<((usize, usize), SendValue)>, fn_items:
  bool, env: BTreeMap<String, String> }`. This is a **new type, not an extension of
  `fs::IoValue`** (IoValue has no identity, cycles or closures; fs/socket jobs keep using it).
- `copy_out` (iterative, memo keyed by `Rc::as_ptr(..) as *const () as usize`, strict check before
  memo) and `copy_in(&mut Vm, &SendGraph, &SendValue) -> Value` (pass 1: allocate every non-closure
  shell; pass 2: closures, `func = vm.linked.functions[func].clone()`; pass 3: fill; Maps rebuilt
  with `index_assign` in order; Promises get `settled`/`failed` and `observed = true`).
- `ThreadRuntime` (§6.1) behind `Arc`, with `state: Mutex<RtState>`, pool `Condvar`s used with that
  mutex, `stdout: Arc<Mutex<BufWriter<Stdout>>>`, `files: FileTable`, `sockets: SocketTable`, the
  stdin reader (`Sender<(u64, Sender<(u64, Completion)>)>` started lazily), `started: Instant`,
  `program: Arc<Program>`, `args`, `test_mode`, `next_vm: AtomicU64`.
- `Job { snapshot: Snapshot, reply: Sender<(u64, Completion)>, reply_id: u64 }`; the submitter
  registers `reply_id` in its `pending` map like any I/O. Every `Send*` type, `Payload` and
  `Snapshot` derive `Clone` (a lock grant clones the stored graph); add a compile-time
  `Send + Sync` assertion for `SendGraph` and `Job`.
- Workers: `std::thread::Builder::new().name(format!("mah-{name}-{i}")).stack_size(VM_STACK_SIZE)`
  — the **same** constant the main VM thread uses: move `STACK_SIZE` (`1 << 30`) from
  `runtime/src/main.rs` to `pub const VM_STACK_SIZE: usize = 1 << 30;` in `runtime/src/vm/mod.rs`
  and use it in both places (it is reserved virtual memory, not committed), so a value or recursion
  depth that works on the main thread works in a job; link once; jobs per §6.5 with
  `std::panic::catch_unwind(AssertUnwindSafe(..))`. After each job, under the state mutex:
  `running -= 1`, the job leaves the pool's running set, `check_quiescence()`.
- The 19 natives (§7.2) as `pub fn x(vm: &mut Vm, args: Vec<Value>) -> RResult<Value>`, dispatched
  from `natives.rs`.

### 10.6 `runtime/src/vm/exec.rs`

- `Vm` gains `rt: Arc<ThreadRuntime>`, `vm_id: u64`, `thread_info: (u64, Rc<str>)`, `line:
  Vec<u8>` (the VM's line buffer, §6.3), `internal: usize` (§6.1) and `stepping: Vec<TaskRef>` (the
  task stack); `started` comes from `rt`.
  - `write_stdout(s)`: `line.extend_from_slice(s)`; if `s` contains `b'\n'`, find the last `\n` in
    `line`, lock the shared writer once, `write_all(&line[..=pos])`, drain that prefix.
  - `flush_stdout()`: hand over all of `line` (one locked write) and flush the shared writer; called
    at the existing flush points (`next_event` before blocking, `drain_next_timer` before sleeping,
    `io.read_line`/`io.input`) plus job end, every return path of `run` and `exit_program`.
  - `exit_program(code)` calls `exit_process(code)` (§6.3: the process-wide first-exit-wins guard,
    in `vm/mod.rs`; `main.rs` calls it too instead of `std::process::exit`).
- `step_task` pushes its task on `stepping` and pops it on every return path; `invoke_sync` sets the
  sub-task's `id` and `held` (an `Rc` clone) from `stepping.last()` when there is one (§5.2).
  `spawn_detached` sets `producer = Some(Producer::Task(id))`. The `Await` opcode on a Pending
  Promise runs the §6.4 `await_check` when `rt` is tracking and the Promise has a producer; resuming
  a task with `awaits_edge` removes its edge.
- `next_event`: before blocking with no timers, the §6.10 `about_to_block` step (under the state
  mutex: `done_rx.try_recv()` — an item found is settled instead of blocking —, else flag the VM
  blocked and `check_quiescence()`).
- `IoHub`: `files`/`sockets` are clones of the runtime's tables; `owns_tables`; `Drop` closes
  sockets only when it owns them; `lock_waits: HashMap<u64, (TaskRef, u64)>`; `read_line` goes
  through the runtime's stdin reader.
- `Completion::{Job(Result<Payload, Payload>), Settle(Result<Payload, Payload>), Lock(Payload), Sem(u64), Recv(u64, Payload), Stuck}`;
  `settle_io` per §6.2 (for `Job`, a Promise that is no longer Pending drops the payload).
- `exec_one`: the four opcodes per §6.4.
- `Vm::make_job(&mut self, closure, values) -> RResult<Job>` (binding + snapshot, §2.3/§4.1).
- One constructor shared by `run` and the job runner; `run` creates the runtime from the program
  and **flushes stdout on every return path**; `run_job` per §6.5/§6.6.
- `execute`, `execute_test` take the program as `Arc<Program>`; `vm/mod.rs`: `mod thread;`,
  `run_bytes`/`run_test_bytes` decode into an `Arc` directly, `run_program(&Program)` wraps a
  clone in an `Arc`.

### 10.7 `runtime/tests/vm_diff.py`

Add the inline cases of §12.2, named `threads_*`, immediately after `std_http_server`. Every
`examples/*.mh` (including `threads.mh`) is already compared.

---

## 11. Part 3 — tooling

### 11.1 `syntax-highlight/grammar.js` (+ `src/` regenerated, `queries/mah/highlights.scm`)

- `shared_let_stmt: seq("shared", $.let_stmt)` (use the grammar's existing `let` rule name) in the
  statement choice.
- `lock_expr: seq("lock", sep1(field("target", $._lock_target), ","), field("body", $.block))`
  with `_lock_target: seq($.identifier, repeat(seq(".", $.identifier)))`, in the expression
  choice.
- `detach_expr`: add the alternative `seq("detach", token.immediate("("), field("thread",
  $.expr), ")", field("operand", $.expr))` (highlighting approximation: the thread form is only
  recognised with `(` right after `detach`), keeping the existing precedence; add whatever
  `conflicts` entry `tree-sitter generate` asks for.
- `highlights.scm`: `"shared" @keyword`, `"lock" @keyword`.
- Regenerate: `cd syntax-highlight && pnpm install && pnpm exec tree-sitter generate` (or `npx
  tree-sitter generate`), commit the regenerated `src/`. `tree-sitter parse ../examples/threads.mh`
  must show no `ERROR` node; `let shared = 1` and `let lock = 2` must still parse as plain lets.

### 11.2 `editors/vscode/syntaxes/mah.tmLanguage.json`

Keyword patterns `\bshared(?=\s+let\b)` and `\block(?=\s+[A-Za-z_])` scoped like `detach`.

### 11.3 `mah/lsp/analysis.py` (+ `tests/test_lsp_threads.py`, new)

- `KEYWORD_DOCS["shared"]`, `KEYWORD_DOCS["lock"]` (texts: §11.5 summaries with a short example);
  `"detach"` gains a sentence and example for `detach(t) expr`.
- `_is_thread_contextual_keyword(token, tokens)`: `shared` followed by `let` on the same line;
  `lock` followed by an `ID` on the same line. Hover renders them as `**keyword**`.
- Kind label `"shared"` → `shared variable` in hover and in completion detail.
- Tests: hover on `shared` in `shared let n = 0` (keyword); hover on `lock` in `lock n { }`
  (keyword); hover on `lock` in `let lock = 2` (variable); hover on `n` in `print(n)` →
  `**shared variable** \`n\`` with `n: Number`; go-to-definition from the `n` in `lock n { }` to the
  declaration; rename of `n` edits the declaration, the `lock` target and every use; completion
  offers `shared` and `lock` as keywords; a document with E1 shows that diagnostic.

### 11.4 `mah/format/formatter.py` (+ `tests/test_format.py`)

- `_blank_module_syntax`: `export` followed by `ID shared` and then `LET` → blank `export` only and
  record a statement start (like `export let`).
- `_Facts.detach_thread_opens`: the `(` token of every `DetachExpr` with `thread`
  (`thread_position`); `_wanted_space` returns `False` before such a `(`. The `)` then gets the
  default single space before the operand: canonical `detach(t) f(x)`.
- `shared let` and `lock a, b { }` need nothing else (check with the golden tests).
- Golden tests: `detach (t)   f(x)` → `detach(t) f(x)`; `detach (a + b)` unchanged;
  `detach(t){ 1 }` → `detach(t) { 1 }`; `shared  let x=1` → `shared let x = 1`;
  `export shared let x = 1` unchanged; `lock a,b{a=b}` → `lock a, b { a = b }`; a multi-line lock
  body indents like any block.

### 11.5 Wording to reuse in docs and LSP

- **shared**: "`shared let NAME = value` (top level only) declares a variable every thread shares.
  Reading it gives a copy; assigning it is atomic; to change it in place (`push`, `x[k] = v`) or to
  read and write it together, use `lock NAME { ... }`."
- **lock**: "`lock a, b { body }` gives this task the shared variables for the block: other tasks
  and threads wait, the body changes them in place, and the changes are written back when the block
  ends (also on `return`, `break` or a throw). Re-entrant; waiting lets other tasks run; a wait that
  would deadlock throws ThreadError."

The language reference's Threads section, the STDLIB `std:thread` section and the www module page
also say, in these words or close:

- **"These act on a copy and change nothing shared"**: `let s = xs; s.push(1)`; `let m = shared_map;
  m[k] = v`; passing a shared variable to a function that changes its parameter (`mutate(xs)`);
  changing the loop variable of `for let item in xs` (`item.n = 1`). Inside `lock xs { }` all of
  these work on the shared value itself. The checker warns about the last two (§8.6 W2/W3).
- **"Read-compute-write needs `lock`"**: `x = g()` where `g` reads `x` loses updates made in between
  (no compile error can see through the call); write `lock x { x = g() }`.
- **Where a read happens and what it costs**: a shared read happens at its place in left-to-right
  evaluation (`f(x, g())` passes the `x` from before `g` ran, even if `g` assigns `x`), and every
  read outside `lock` copies the whole value — read a big shared value once into a local, or work
  inside one `lock`, rather than reading it in a loop.
- **Random numbers in jobs** (§4.3): every job starts from a copy of the submitter's generator
  state, so identical jobs draw identical numbers; seed per job (`random.seed(thread.id() * 1000 +
  n)`) or pass a seed as an argument.
- **Semaphore permits** belong to nobody: release them with `defer s.release()`; a permit held by a
  task that its job abandons is lost.
- **Waits that can never finish** fail with `ThreadError` `stuck` once every thread is waiting
  (§6.10); deadlocks through locks, job and join awaits fail with `deadlock` (§6.4); a wait that
  only *another running thread* could end keeps waiting.
- **Unobserved failures**: with several failed job Promises nobody awaited, which one the program
  reports at exit can differ from run to run.

---

## 12. Tests

Outputs below are exact (stdout). All are deterministic: the main VM prints, jobs print only where
noted, and every cross-thread order is forced by awaits, channels or timers.

### 12.1 Part 1 (Python side; `make test` green)

**`tests/test_parser.py`** — class `ThreadSyntaxTests`:
- `detach(t) f(x)` and `detach (t) f(x)` → `DetachExpr` with `thread=Ident('t')`, `call` = `Call`
  of an `FnExpr` (`detached=True`, no params) whose body tail is `Call(Ident f, [Ident x])`.
- `detach (a + b)` → `thread is None`, closure-wrapped `Binary`; `detach (1 + 2).await` → a
  `FieldAccess(await)` over a `DetachExpr` with `thread is None`; `detach (f)(x)` → `thread is None`,
  `call = Call(Ident f, [Ident x])`; `detach (a) - b` → `Binary(-)` over `DetachExpr`.
- `"detach(t)\nfoo()"` → two statements, the first `DetachExpr(thread=None)`.
- `detach(t) f(x).await` → `FieldAccess(await)` over `DetachExpr(thread=Ident t)`.
- `if detach(t) { 1 } { 2 }`… not tested (absurd); instead `while detach (p) { break }` parses
  `DetachExpr(thread=None)` as the condition.
- `detach (t)[1]` → `thread is None`, `paren_head = Ident('t')`.
- Compile errors (`compile_source` raises, message contains the exact §3.1 text):
  `detach(t) print("hi")`, `detach(t) let x = 1` and `fn f() { detach(t) return }` →
  `detach(t) needs an expression; write 'detach(t) { ... }'`; `"detach(t)\n{ 1 }"` and
  `"detach(a.b)\n{ 1 }"` → `detach(t) and its operand must be on the same line; write 'detach(t) {'
  on one line`; `"detach (a + b)\n{ 1 }"` is **not** an error (today's meaning: two statements).
- `shared let x = 1` → `LetStmt(shared=True)` with `position` at `shared`; `let shared = [1]` →
  plain `LetStmt` named `shared`; `lock a, b { a }` → `LockExpr` with two `Ident` targets and a
  block of `[ExprStmt(LockAcquire a), DeferStmt, ExprStmt(LockAcquire b), DeferStmt]`, tail =
  the body; `let lock = 2\nprint(lock)` parses as before.

**`tests/test_threads.py`** (new; runs through `tests/support.py`, so `make test-rust` runs it on
the Rust VM too). Every program imports `thread from "std:thread"` unless noted. Programs T1–T12
are run with `run_source` and compared exactly:

T1 (basics):
```mah
import thread from "std:thread"
let t = thread.spawn(name: "worker")
fn square(n) { n * n }
let p = detach(t) square(7)
print(p.await)
print(t.run(square, 9).await)
print(t, t.name, t.workers, t.capacity)
print(thread.id(), thread.name())
print(detach(t) { [thread.id(), thread.name()] }.await)
let u = thread.spawn()
print(u.name, u.id)
t.join()
u.join()
print(t.pending(), thread.cores() >= 1)
```
→ `49\n81\nThread(worker) worker 1 none\n0 main\n[1, worker]\nthread-2 2\n0 true\n`

T2 (globals are copied at queue time):
```mah
import thread from "std:thread"
let t = thread.spawn()
let counter = 0
let items = [1, 2]
let p = detach(t) {
    counter = counter + 100
    items.push(3)
    [counter, items.len()]
}
counter = 5
print(p.await)
print(counter, items)
```
→ `[100, 3]\n5 [1, 2]\n`

T3 (identity and cycles):
```mah
import thread from "std:thread"
struct Node { value: Number, next: Unknown }
let t = thread.spawn()
let a = Node { value: 1, next: none }
let b = Node { value: 2, next: a }
a.next = b
let pair = [a, a]
let r = t.run(fn(v) {
    v[0].value = 10
    [v[1].value, v[0].next.next.value, v[0].next.value]
}, pair)
print(r.await, a.value)
```
→ `[10, 10, 2] 1\n`

T4 (closures copy their frames):
```mah
import thread from "std:thread"
let t = thread.spawn()
fn make_counter() {
    let n = 0
    fn() {
        n = n + 1
        n
    }
}
let c = make_counter()
c()
print(t.run(fn() {
    c()
    c()
}).await, c())
```
→ `3 2\n`

T5 (methods and Printable travel with the snapshot):
```mah
import thread from "std:thread"
let t = thread.spawn()
struct P { x: Number }
impl P {
    fn double(self) { self.x * 2 }
}
impl Printable for P {
    fn to_string(self) { "P(" + self.x + ")" }
}
print(t.run(fn(p) { p.double() }, P { x: 4 }).await)
print(detach(t) { "got " + P { x: 1 } }.await)
```
→ `8\ngot P(1)\n`

T6 (errors and Promises):
```mah
import thread from "std:thread"
let t = thread.spawn()
struct Oops { why: String }
impl Error for Oops {
    fn message(self) { "oops: " + self.why }
}
let q = detach(t) { throw Oops { why: "late" } }
try { q.await } catch {
    e: Oops => { print("caught", e.why, e.message()) }
}
let pr = detach sleep_async(1)
try { t.run(fn(x) { x }, [pr]) } catch {
    e: ThreadError => { print(e.kind) }
}
let g = detach sleep_async(1)
print(detach(t) {
    try {
        g.await
        "awaited"
    } catch {
        e: ThreadError => { e.kind }
    }
}.await)
let done = detach { 42 }
print(detach(t) { done.await + 1 }.await)
let bad = detach(t) { [detach { 1 }] }
try { bad.await } catch {
    e: ThreadError => { print(e.kind, e.message) }
}
```
→ `caught late oops: late\nnot_sendable\nforeign_promise\n43\nnot_sendable a Promise can't be sent to another thread\n`

T7 (shared variables and lock; no std:thread needed for the first part):
```mah
import thread from "std:thread"
shared let n = 0
shared let xs = []
fn bump() {
    lock n {
        n = n + 1
        n
    }
}
print(lock n {
    bump()
    bump()
})
print(n)
lock xs { xs.push("a") }
let mine = xs
lock xs { xs.push("b") }
print(xs, mine)
let t = thread.spawn()
print(detach(t) {
    lock xs { xs.push("c") }
    lock xs { xs.len() }
}.await)
print(xs)
xs = ["reset"]
print(t.run(fn() { xs }).await)
try {
    lock xs {
        xs.push("d")
        throw RuntimeError.ArgumentError { message: "stop" }
    }
} catch {
    e => { print("caught", e.message()) }
}
print(xs)
shared let slot = none
try { slot = [detach { 1 }] } catch {
    e: ThreadError => { print(e.kind, e.message, slot) }
}
```
→ `2\n2\n[a, b] [a]\n3\n[a, b, c]\n[reset]\ncaught stop\n[reset, d]\nnot_sendable shared variable 'slot' can't hold a Promise none\n`

T8 (a pool and shared counters):
```mah
import thread from "std:thread"
shared let total = 0
shared let log = []
let pool = thread.spawn(name: "pool", workers: 4)
fn work(n) {
    for let i in 0..100 {
        lock total { total = total + 1 }
    }
    lock log { log.push(n) }
    n * 2
}
let jobs = []
for let n in 0..8 { jobs.push(pool.run(work, n)) }
let doubled = 0
for let j in jobs { doubled = doubled + j.await }
let snapshot = log
let sum = 0
for let n in snapshot { sum = sum + n }
print(total, snapshot.len(), sum, doubled)
pool.join()
```
→ `800 8 28 56\n`

T9 (deadlock detection, one thread, timer-ordered; no std:thread import):
```mah
shared let a = 0
shared let b = 0
let p1 = detach {
    lock a {
        sleep_async(20)
        lock b { "p1 got both" }
    }
}
let p2 = detach {
    lock b {
        sleep_async(60)
        try {
            lock a { "p2 got both" }
        } catch {
            e: ThreadError => { e.kind + " | " + e.message }
        }
    }
}
print(p1.await)
print(p2.await)
```
→ `p1 got both\ndeadlock | deadlock: waiting for 'a' would never end\n`

T10 (semaphores):
```mah
import thread from "std:thread"
let s = thread.semaphore(1)
print(s.try_acquire(), s.try_acquire(), s.available())
s.release()
print(s.available(), s)
try { s.release() } catch {
    e: ThreadError => { print(e.kind, e.message) }
}
let gate = thread.semaphore(2)
shared let inside = 0
shared let most = 0
fn job(n) {
    gate.acquire()
    defer gate.release()
    lock inside, most {
        inside = inside + 1
        if inside > most { most = inside }
    }
    sleep_async(20)
    lock inside { inside = inside - 1 }
    n
}
let pool = thread.spawn(workers: 4)
let ps = []
for let n in 0..6 { ps.push(pool.run(job, n)) }
let total = 0
for let p in ps { total = total + p.await }
print(most <= 2, most >= 1, inside, total, gate.available())
pool.join()
```
→ `true false 0\n1 Semaphore(1/1)\nover_release release without a matching acquire (all 1 permits are free)\ntrue true 0 15 2\n`

T11 (channels, M44b):
```mah
import thread from "std:thread"
let c1 = thread.channel(capacity: 1)
c1.send("a")
print(c1.len())
let blocked = detach c1.send("b")
print(c1.recv(), c1.recv())
blocked.await
c1.close()
print(c1.try_recv(), c1.closed(), c1.len())
try { c1.send("x") } catch {
    e: ThreadError => { print(e.kind, e.message) }
}
try { c1.recv() } catch {
    e: ThreadError => { print(e.kind) }
}
let tasks = thread.channel()
let results = thread.channel()
let workers = thread.spawn(name: "workers", workers: 3)
fn worker() {
    let sum = 0
    for let job in tasks { sum = sum + job }
    results.send(sum)
}
for let i in 0..3 { workers.run(worker) }
for let n in 1..=10 { tasks.send(n) }
tasks.close()
let total = 0
for let i in 0..3 { total = total + results.recv() }
print(total)
let r = thread.channel(capacity: 0)
let receiver = detach r.recv()
r.send("hand-off")
print(receiver.await, r.try_recv())
workers.join()
```
→ `1\na b\nnone true 0\nclosed the channel is closed\nclosed\n55\nhand-off none\n`

T12 (capacity, cancel, close, join):
```mah
import thread from "std:thread"
let solo = thread.spawn(name: "solo", capacity: 1)
let started = thread.channel()
let gate = thread.channel()
let first = detach(solo) {
    started.send(1)
    gate.recv()
}
started.recv()
let second = detach(solo) { "second" }
try { detach(solo) { "third" } } catch {
    e: ThreadError => { print(e.kind, e.message) }
}
print(solo.pending())
solo.close(cancel: true)
try { second.await } catch {
    e: ThreadError => { print(e.kind, e.message) }
}
try { solo.run(fn() { 1 }) } catch {
    e: ThreadError => { print(e.kind, e.message) }
}
gate.send("go")
print(first.await)
solo.join()
print(solo.pending())
let me = thread.spawn(name: "me")
print(detach(me) {
    try {
        me.join()
        "joined"
    } catch {
        e: ThreadError => { e.message }
    }
}.await)
me.join()
```
→ `full thread 'solo' is full (2 jobs queued or running)\n2\ncancelled thread 'solo' was closed before this job started\nclosed thread 'solo' is closed\ngo\n0\ndeadlock: a thread can't join itself\n`

T13 (modules): a temporary directory with `lib.mh`:
```mah
export shared let hits = 0
export fn hit() {
    lock hits { hits = hits + 1 }
}
```
and `main.mh`:
```mah
import lib from "lib.mh"
import thread from "std:thread"
let t = thread.spawn()
lib.hit()
t.run(lib.hit).await
lock lib.hits { lib.hits = lib.hits + 1 }
print(lib.hits)
```
`run_file(main.mh)` → `3\n`.

T14 (contextual words stay identifiers; no import): `let shared = [1]\nlet lock = 2\nfn f(lock) { lock + 1 }\nprint(shared, lock, f(lock))` → `[1] 2 3\n`; and `print(detach (1 + 2).await)` → `3\n`.

T15 (print never tears): 
```mah
import thread from "std:thread"
let pool = thread.spawn(workers: 4)
fn shout(n) {
    for let i in 0..25 { print("line " + n + "-" + i) }
}
for let n in 0..4 { pool.run(shout, n) }
pool.join()
```
→ `sorted(out.splitlines()) == sorted(f"line {n}-{i}" for n in range(4) for i in range(25))`.

T19 (implicit runtime calls share their caller's task, §5.2; no import):
```mah
shared let n = 0
struct P { x: Number }
impl Printable for P {
    fn to_string(self) {
        lock n { n = n + 1 }
        "P(" + self.x + ", n=" + n + ")"
    }
}
lock n {
    n = 10
    print(P { x: 1 })
}
print(n)
```
→ `P(1, n=11)\n11\n` (the `lock n` inside `to_string` re-enters the caller's lock instead of
failing with "cannot suspend"; the read after it sees the caller's working value, 11).

T20 (only lexically locked reads alias the working value, §5.2):
```mah
shared let xs = [1]
fn count_with(v) {
    let s = xs
    s.push(v)
    s.len()
}
print(count_with(2), xs)
print(lock xs {
    xs.push(5)
    [count_with(9), xs.len()]
}, xs)
```
→ `2 [1]\n[3, 2] [1, 5]\n` (inside the lock the helper sees `[1, 5]` but its push changes only
its copy).

T21 (a failed write-back never replaces the error in flight):
```mah
shared let slot = []
struct Boom { why: String }
impl Error for Boom {
    fn message(self) { "boom: " + self.why }
}
try {
    lock slot {
        slot.push(detach { 1 })
        throw Boom { why: "first" }
    }
} catch {
    e: Boom => { print("caught", e.message()) }
    e: ThreadError => { print("wrong", e.kind) }
}
print(slot)
try {
    lock slot { slot.push(detach { 2 }) }
} catch {
    e: ThreadError => { print(e.kind) }
}
print(slot)
```
→ `caught boom: first\n[]\nnot_sendable\n[]\n`.

T22 (an await that closes a cycle through a lock; one thread, no import):
```mah
shared let x = 0
let p = none
try {
    lock x {
        p = detach { x = 1 }
        p.await
    }
} catch {
    e: ThreadError => { print(e.kind, "|", e.message) }
}
p.await
print(x)
```
→ `deadlock | deadlock: this await would never end (it waits, through locks or threads, for itself)\n1\n`.

T23 (the same across threads; which side detects it varies, the kind doesn't):
```mah
import thread from "std:thread"
shared let x = 0
let t = thread.spawn()
try {
    lock x { detach(t) { x = 1 }.await }
} catch {
    e: ThreadError => { print(e.kind) }
}
t.join()
print("joined")
```
→ `deadlock\njoined\n` (never a hang).

T24 (quiescence, §6.10):
```mah
import thread from "std:thread"
let ch = thread.channel()
try { ch.recv() } catch {
    e: ThreadError => { print(e.kind, "|", e.message) }
}
let t = thread.spawn()
let p = detach(t) { ch.recv() }
try { p.await } catch {
    e: ThreadError => { print(e.kind) }
}
let s = thread.semaphore(1)
s.acquire()
try { s.acquire() } catch {
    e: ThreadError => { print(e.kind) }
}
print("end")
```
→ `stuck | the wait can never finish: every thread is waiting\nstuck\nstuck\nend\n`, exit code 0
(the job blocked on `ch.recv()` is abandoned at program end).

T25 (handle variables need no lock):
```mah
import thread from "std:thread"
shared let jobs = thread.channel()
shared let gate: thread.Semaphore = thread.semaphore(1)
jobs.send(1)
gate.acquire()
print(jobs.recv(), gate.available(), jobs.len())
gate.release()
```
→ `1 0 0\n`.

Wait-for graph unit tests (`tests/test_threads.py::WaitForGraphTests`, Python only; Part 2 mirrors
them as `#[test]`s in `thread.rs`): build a `ThreadRuntime` and set `locks`, `waiting_on`,
`awaiting`, `job_roots` and a pool's running set by hand; assert `cycle` for: lock → lock (true);
await `Task` → lock back (true); await `Job` whose root waits on a lock the awaiter owns (true);
the same `Job` not started yet (false, then true once `job_roots` has it); `Join` → a running job's
root that waits on the joiner's lock (true); a plain `Task` await cycle with no lock edge (false).

Subprocess tests (compile to a temp `.mahc`; run `python -m mah runc FILE`, or `find_vm() run FILE`
when `MAH_TEST_VM=rust`):
- T16 exit: `print("before")` then `let p = detach(t) { process.exit(3) }` then `p.await` then
  `print("never")` (imports `std:thread`, `std:process`) → exit code 3, stdout `before\n`,
  stderr empty.
- T17 keep-alive: `detach(t) {\n    sleep_async(100)\n    print("late")\n}\nprint("main done")`
  → exit 0, stdout `main done\nlate\n`.
- T18 uncaught job error: program A, line 3 = `let p = detach(t) { throw RuntimeError.ArgumentError { message: "bad" } }`;
  program B identical but with `detach(t)` replaced by `detach   ` (same columns, same-thread
  task). Both exit 1, stdout empty, identical stderr, which starts with
  `RuntimeError: bad at position #3:`.

Compile-error tests (each a `compile_source` call asserting the exception text contains the §5.3
message with the right name): E1 `shared let v = []\nv.push(1)`; E1 through an index
`shared let v = [[1]]\nv[0].push(2)`; E2 `shared let v = [1]\nv[0] = 2`; E2 field
`struct S { a: Number }\nshared let s = S { a: 1 }\ns.a = 2`; E3 `shared let n = 0\nn = n + 1`;
E3 through a closure `shared let n = 0\nn = (fn() { n })()`; E1 on a non-handle shared variable
`shared let c = none\nc.send(1)`; a handle variable compiles
`import thread from "std:thread"\nshared let c = thread.channel()\nc.send(1)` but
`import thread from "std:thread"\nshared let c = thread.channel()\nc.id.to_string()` is E1; E4
`let m = 0\nlock m { }`; E4 dotted `struct S { a: Number }\nlet p = S { a: 1 }\nlock p.a { }`; E5
`fn f() {\n    shared let x = 1\n}`; E6 `shared let a = 0\nlock a, a { }`; E7
`shared let a = 0\nlet a = 1`; nested closure inside a lock still errors
`shared let v = []\nlock v { let f = fn() { v.push(1) } }` (E1); a `detach` inside a lock errors
`shared let v = []\nlock v { detach { v.push(1) } }` (E1); a `defer` inside a lock is fine
`shared let v = []\nlock v { defer v.push(1) }` compiles; `return` inside `detach(t) { }` is the
existing "can't leave a detached expression" error.

Bytecode tests (`tests/test_bytecode.py` additions): a program with `shared let x = 1\nlock x { x = 2 }`
is written with minor 21 and `mah dis` output contains `sharedlock`, `sharedset`, `sharedunlock`,
`sharedget` lines; flipping its minor byte to 20 makes `decode` raise `opcode 'sharedlock' at
instruction I requires minor version >= 21, but this file's minor version is 20` (I = the first
such instruction); `import thread from "std:thread"\nprint(thread.cores() > 0)` is minor 21.

Checker tests (`tests/test_typecheck.py`, class `ThreadTypeTests`): `let p = detach(t) 1` with `t`
a `thread.Thread` gives `p: Promise<Number>`; `detach(5) 1` reports `detach(...) needs a
thread.Thread, got Number`; `detach("x") 1` → `got String`; `struct Thread { id: Number }\nlet u =
Thread { id: 1 }\ndetach(u) 1` → `got Thread` (a user's own `Thread` is not std:thread's);
`let v = lock n { n + 1 }` (n a shared Number) gives `v: Number`. Warnings (kind `"warning"`, exact
§8.6 text): W1 for `import thread from "std:thread"\nlet t = thread.spawn()\ndetach(t)\nprint(1)`;
W2 for `shared let xs = [1]\nfn f(v) { v.push(2) }\nf(xs)` and none for
`... lock xs { f(xs) }`; W3 for `struct S { n: Number }\nshared let ss = [S { n: 1 }]\nfor let s
in ss { s.n = 2 }` and none inside `lock ss { ... }`; no warning for `shared let n = 0\nprint(n +
1)` or `fn g(v) { v }\ng(n)` (a Number).

**`mah/std/thread.test.mh`** (new; `import "std:test"`, `import thread from "./thread"`): one
`test` per area, asserting with `assert_eq`/`assert_throws`: spawn defaults (`name`
`"thread-1"`, `workers` 1, `capacity` none) and the three argument errors (message text); run and
detach results; copies don't leak back; identity preserved; errors cross back (a user error struct
caught by type); `not_sendable` for a Promise argument; `foreign_promise`; shared counter with 4
workers × 50 increments = 200; lock re-entrancy (nested `lock` in a helper); write-back on throw;
semaphore try/acquire/release/over_release; channel FIFO order of `[1, 2, 3]` sent from a job and
received in the main VM; bounded channel `len`; closed channel errors; iteration over a closed
channel; `close(cancel: true)` and `join`; `thread.id()`/`thread.name()` in a job. (Each test runs
in its own run, so ids restart at 1 in every test.)

**`examples/threads.mh`** (new) — exactly (the body is in `main` with a top-level `try`, like
`examples/sockets.mh`, because the strict checker reports a `throws ThreadError` call made at the top
level):
```mah
# M44: threads -- jobs on other threads, shared variables, locks and channels.
import thread from "std:thread"

# Shared variables live outside every thread; `lock` changes them safely.
shared let hits = 0
shared let seen = []

fn fib(n) {
    if n < 2 { return n }
    fib(n - 1) + fib(n - 2)
}

fn visit(n) {
    for let i in 0..50 {
        lock hits { hits = hits + 1 }
    }
    lock seen { seen.push(n) }
}

fn square_all(tasks: thread.Channel, results: thread.Channel) throws ThreadError {
    for let n in tasks { results.send(n * n) }
}

fn main() throws ThreadError {
    # 1. A job runs on another thread, on a copy of what it uses.
    let worker = thread.spawn(name: "worker")
    let p = detach(worker) fib(18)
    print("fib(18) =", p.await)
    print(worker.run(fib, 12).await, "on", detach(worker) { thread.name() }.await)

    # 2. Variables are copied when the job is queued.
    let greeting = "hello"
    let copy_job = detach(worker) {
        greeting = greeting + " from " + thread.name()
        greeting
    }
    print(copy_job.await, "/", greeting)

    # 3. Shared variables: every job changes the same ones.
    let pool = thread.spawn(name: "pool", workers: 4)
    let jobs = []
    for let n in 0..8 { jobs.push(pool.run(visit, n)) }
    for let job in jobs { job.await }
    let snapshot = seen
    let total = 0
    for let n in snapshot { total = total + n }
    print("hits:", hits, "seen:", snapshot.len(), "sum:", total)

    # 4. Channels pass messages between threads.
    let tasks = thread.channel()
    let results = thread.channel()
    for let i in 0..3 { pool.run(square_all, tasks, results) }
    for let n in 1..=5 { tasks.send(n) }
    tasks.close()
    let sum = 0
    for let i in 0..5 { sum = sum + results.recv() }
    print("sum of squares:", sum)

    # 5. A job's error comes back through its Promise.
    let failing = detach(worker) { throw RuntimeError.ArgumentError { message: "bad input" } }
    print(try { failing.await } catch { e => { "caught: " + e.message() } })

    pool.join()
    worker.join()
    print("done")
}

try { main() } catch {
    e: ThreadError => { print("failed:", e.message()) }
}
```
Golden (`tests/test_examples.py::test_threads`, after `test_http_server`):
`fib(18) = 2584\n144 on worker\nhello from worker / hello\nhits: 400 seen: 8 sum: 28\nsum of squares: 55\ncaught: bad input\ndone\n`.

### 12.2 Part 2 (`make test-rust` and `vm_diff.py` green)

- Everything in §12.1 passes with `MAH_TEST_VM=rust` (`tests/test_threads.py`,
  `tests/test_stdlib.py` running `thread.test.mh` on Rust, `tests/test_examples.py`).
- `runtime/tests/vm_diff.py` inline cases (empty stdin; compare exit code, stdout, stderr):
  `threads_basic` = T1 + T2 + T3 + T4 + T5 concatenated (one `import`, distinct names: prefix each
  part's names), `threads_errors` = T6, `threads_shared` = T7, `threads_pool` = T8,
  `threads_deadlock` = T9, `threads_semaphore` = T10, `threads_channels` = T11, `threads_close` =
  T12, `threads_exit` = T16, `threads_keepalive` = T17, `threads_uncaught` = T18 program A,
  `threads_implicit` = T19, `threads_reads` = T20, `threads_writeback` = T21,
  `threads_await_deadlock` = T22, `threads_await_deadlock_job` = T23, `threads_stuck` = T24,
  `threads_handles` = T25. No case has more than one unobserved failing job (§2.3).
- `cargo test` (decode tests of §10.1).

### 12.3 Part 3

`tests/test_lsp_threads.py` (§11.3), `tests/test_format.py` goldens (§11.4), and
`tests/test_project.py` stays green (every new `mah` block in the template reference compiles).

---

## 13. Work split — every file, exactly one part

| file | part | change |
|---|---|---|
| `mah/preprocessor.py` | 1 | §8.1 |
| `mah/compiler/ast_nodes.py` | 1 | §8.2 |
| `mah/compiler/parser.py` | 1 | §8.3 |
| `mah/compiler/resolve.py` | 1 | §8.4 |
| `mah/compiler/codegen.py` | 1 | §8.5 |
| `mah/compiler/typecheck.py` | 1 | §8.6 |
| `mah/bytecode/format.py`, `mah/bytecode/lower.py` | 1 | §7, §8.7 |
| `mah/bytecode/encode.py`, `decode.py`, `disasm.py` | 1 | only if §8.7's checks fail |
| `mah/runtime_values.py` | 1 | §9.1 |
| `mah/thread_runtime.py` (new), `mah/thread_natives.py` (new) | 1 | §9.2, §9.3 |
| `mah/code_interpreter.py`, `mah/natives.py` | 1 | §9.4, §9.5 |
| `mah/std/thread.mh` (new), `mah/std/prelude.mh` | 1 | §2.1, §2.2 |
| `mah/std/thread.test.mh` (new) | 1 | §12.1 |
| `examples/threads.mh` (new) | 1 | §12.1 |
| `tests/test_threads.py` (new), `tests/test_parser.py`, `tests/test_typecheck.py`, `tests/test_examples.py`, `tests/test_bytecode.py` | 1 | §12.1 |
| `tests/test_decorators.py`, `tests/test_hooks.py`, `tests/test_http.py`, `tests/test_reflection.py`, `tests/test_socket.py`, `tests/test_tls_server.py` | 1 | MINOR pin 20 → 21 |
| `runtime/src/decode.rs`, `runtime/src/decimal.rs` | 2 | §10.1, §10.2 |
| `runtime/src/vm/value.rs`, `link.rs`, `exec.rs`, `natives.rs`, `mod.rs`, `thread.rs` (new) | 2 | §10.3–§10.6 |
| `runtime/src/main.rs` | 2 | use `vm::VM_STACK_SIZE` and `exit_process` (§6.3, §10.5) |
| `runtime/src/lib.rs` | 2 | only if the `Arc<Program>` plumbing needs it |
| `runtime/tests/vm_diff.py` | 2 | §10.7 |
| `syntax-highlight/grammar.js`, `syntax-highlight/src/**`, `syntax-highlight/queries/mah/highlights.scm` | 3 | §11.1 |
| `editors/vscode/syntaxes/mah.tmLanguage.json` | 3 | §11.2 |
| `mah/lsp/analysis.py`, `tests/test_lsp_threads.py` (new) | 3 | §11.3 |
| `mah/format/formatter.py`, `tests/test_format.py` | 3 | §11.4 |
| `docs/MAHC_FORMAT.md` | 3 | header "version 1.21"; §3 minor rule "(1.21) writes 21 when the file uses a `shared*` opcode or lists a `thread.*` native (every program importing std:thread)"; §4.4 the 19 rows of §7.2 and a prose block (§2.3, §6.7, §6.8); §4.6 the four rows of §7.1; §6.4 "pending" widened (§6.9); new **§6.11 Threads, jobs and shared variables** (§2.3, §4, §5.2, §6.1–§6.6 condensed, normative); §7 a **1.21** entry |
| `docs/STDLIB.md` | 3 | new "## Phase 5: threads" with "### `std:thread`" before "## `std:test` and `mah test`" (API, copy rules, shared/lock, semaphores, channels, ThreadError table, "bytecode 1.21", "parallel on the Rust VM, concurrent on the Python VM") |
| `docs/V2_DESIGN.md` | 3 | entries **M44a** and **M44b** after M43 (the decisions of §1, files, the 1.21 bump, test changes, judgment calls of §15, the M44
compatibility notes of §3.1 including the `ThreadError` prelude name) and the Status paragraph: "M0 through M41c, M37 to M39, M41s, M42, M43 and M44a/M44b", "(currently 1.21)", `std:thread` in the module list, "threads (`std:thread`, `detach(t)`, `shared`/`lock`, channels) landed with M44" and the NEXT_PHASES pointer without "multithreaded `detach`" |
| `docs/NEXT_PHASES.md` | 3 | "Optional multithreading" marked landed (M44, pointing to this contract), plus §16's deferred items as the follow-ups |
| `docs/ERRORS.md` | 3 | `ThreadError` (prelude), its kinds (incl. `stuck` and the await `deadlock`), the job/lock sources of errors, a failed write-back during a throw never replacing the error, unobserved failed job Promises (order nondeterministic with several), and the **M44 compatibility notes** of §3.1 (`detach (x) expr` on one line, the two new `detach(t)` compile errors, the new prelude name `ThreadError`) |
| `docs/RUST_VM.md` | 3 | a "Threads" section: `ThreadRuntime`, per-worker linking, `SendGraph`, shared stdout/handle tables |
| `docs/FORMAT.md` | 3 | `detach(t)` spacing, `shared let`, `lock` |
| `mah/project/templates/docs/mah-language.md` | 3 | Async section: `detach(t)` + the rule of §3.1 (short); new "## Threads" section after Async (spawn/run/join, copies, `shared let`, `lock`, Semaphore, channels, ThreadError; compiling examples); "Standard library" list gains `std:thread`; "Not available": remove nothing, add "lock timeouts, mutexes other than `shared`+`lock`, killing a running job, `select` over channels" |
| `mah/project/templates/AGENTS.md` | 3 | `std:thread` in the std list |
| `www/src/content/std/thread.md` (new) | 3 | the module page, following the other std pages |
| `www/src/content/docs/async.md`, `standard-library.md`, `errors.md` | 3 | `detach(t)` + a link to threads; `std:thread` row; `ThreadError` |
| `www/src/content/blog/v0-4-0-threads.md` (new) | 3 | changelog post (`tags: [changelog]`, `version: "0.4.0"`) on M44 — including a "Compatibility" paragraph with the §3.1 notes (a program declaring its own `ThreadError` type now gets the "built-in name" error) and the "random numbers in jobs" note of §4.3 — with a short "also since 0.3.0" list naming M42 (HTTP server) and M43 (packages) |

Nothing in `mah-todo-demo` or the backend framework is touched. `docs/contracts/M44_threads_options.md`
stays as is.

Order: Part 1 lands (`make test` green; `make test-rust` may fail only on 1.21 programs). Parts 2
and 3 branch from it. Part 3's docs describe the behaviour defined here, not Part 1's
implementation; where Part 1 reports a forced deviation, the thinker updates this contract first.

---

## 14. Mah gotchas for anyone writing Mah here

- `&` and `|` are the boolean operators (no `&&`/`||`), lowest precedence, and **both sides are
  evaluated** — guard a type check with a nested `if` (as `whole` in §2.1 does).
- `print(a, b)` separates with one space; Strings print without quotes; a Vector of Strings prints
  `[a, b]`.
- A statement that isn't block- or call-shaped needs `;` if more code follows on the same line
  (`n = n + 1; n`). `try { } catch { }` arms need `{ }` bodies.
- `detach a + b` is `(detach a) + b`; `detach(t) [x]` is not the thread form (§3.1) — use braces.
- `for let x in v` (with `let`); ranges `0..n`, `1..=n`.
- Reading a shared variable outside `lock` gives a copy (even from a helper called inside a
  `lock`): `let s = shared_vec; s.len()` calls a method on that copy, and changing `s` changes
  nothing shared. To change the shared value, do it lexically inside `lock shared_vec { }`.
- `print("a" + n)` writes the text and the newline separately; a VM's line buffer (§6.3) keeps lines
  whole — never write a test that relies on one `write` per `print`.

---

## 15. Judgment decisions (for the user)

1. **`detach(t)` rule**: thread form iff the token after `)` is on the same line and starts an
   operand from a fixed token set (§3.1); `(`, `[`, `-` and operators excluded, so every existing
   `detach (expr)`… meaning is kept except "another expression on the same line after `detach (x)`".
2. **The thread form always closure-wraps its operand**: even `detach(t) f(x)` evaluates `f` and `x`
   on the thread, against the snapshot (one rule; differs from same-thread `detach f(x)` only for
   side-effecting argument expressions).
3. **`t.run(f, ...args)` is the function form**; `thread.spawn()` takes no function. Arguments are
   bound in the submitter (arity errors at the call site).
4. **Snapshot at queue time, per job**, synchronously in the submitter (justified in §4.1).
5. **A job is a small program**: it drains its own timers/I/O/waits before replying, and an
   unobserved failed sub-task fails the job; a failing root ends it at once.
6. **Promises reached through frames are copied by state** (Settled/Failed copied, Pending →
   `foreign_promise`), so a global Promise never makes a whole program unthreadable; Promises
   inside sent containers are refused (`not_sendable`).
7. **In-place mutation of a shared variable outside `lock` is a compile error** (E1/E2), and so is
   `x = f(x)` outside a lock (E3), rather than proxies. Method calls on **handle variables**
   (`shared let ch = thread.channel()`) are exempt from E1; the indirect copy traps (passing to a
   mutating function, mutating a loop variable) are checker **warnings** (W2/W3), and
   read-compute-write through a helper (`x = g()`) is documented, not detected.
7a. **Two kinds of read**: only a read written lexically inside `lock x { }` returns the working
   object; every other read is a copy (of the holder's working value when the task holds the lock),
   so a helper behaves the same whatever its caller holds.
7b. **Implicit runtime calls** (`to_string`, `Error.message`) run as their caller's task (same id,
   same held locks).
7c. **A failed write-back during a throw is dropped** (the in-flight error wins), unlike an error
   thrown by a user `defer`.
8. **Checkout/write-back locks**, write-back also on throws; re-entrant per task; FIFO; plain
   assignment = lock + set + release; reads never wait (read committed).
9. **No `Mutex` type**: a `shared let m = none` + `lock m { }` is the mutex. `Semaphore` is the only
   extra primitive; no `Condition`/`WaitGroup` (channels and `async.all` cover them).
10. **Deadlock detection** covers cycles through lock waits, awaits of same-VM detached tasks, job
    awaits (once the job started) and joins, as long as the cycle contains a lock, job or join edge;
    plus self-join. Everything else that can never finish (semaphores, channels, queued jobs) is
    caught by the **quiescence rule** (§6.10) — every VM blocked on runtime waits → the main VM's
    waits fail with `stuck` — rather than hanging.
11. **`ThreadError` lives in the prelude** (global name, VM-constructible like `EndOfInput`) instead
    of new `RuntimeError` variants (those would change the TYPES layout by minor).
12. **Thread options**: `name`, `workers` (a pool sharing one FIFO queue), `capacity` (in-flight
    limit = workers + capacity; `full` is thrown, never blocks, because `detach` can't suspend
    mid-expression). `close(cancel:)` keeps queued jobs by default.
13. **Shared process resources**: one stdout writer fed whole lines by a per-VM line buffer, one
    stdin reader, shared
    file/socket tables closed only by the main VM, env table copied per job, monotonic time from
    program start, random state copied with the globals.
14. **Stacks and exit**: Rust workers use the main VM thread's stack size (`VM_STACK_SIZE` =
    1 GiB, virtual), Python sets `threading.stack_size` to 64 MiB before the first worker; Rust's
    process exit is first-wins behind an `AtomicBool`. Also a new `SendGraph` (not `IoValue`),
    `Arc<Program>` linked once per worker, `SendDecimal` because `Decimal` holds an `Rc`.
15. **Python**: copies are cloned object graphs (no serialization); the main VM polls for
    `process.exit` from a job every 4096 instructions and at every event wait.
16. **One MINOR (21) for M44a and M44b.** `std:thread` methods declare `throws ThreadError`
    (checked like `std:socket`'s errors); VM-raised `ThreadError`s from `lock` and job Promises are
    untracked, like `RuntimeError`.
17. **No `MAH_THREADS=inline` mode**: tests are made deterministic by construction instead.

## 16. Deferred (not in M44)

Copying only the globals a job uses (free-variable slicing); lock and acquire timeouts; deadlock
detection through semaphores, channels and not-yet-started jobs (left to the quiescence rule,
§6.10), and through cycles of plain same-VM awaits; reporting *which* wait is hopeless while some
other thread is still busy; `select` over several channels; `Condition`,
`WaitGroup`, atomic counters without `lock`; interrupting a running job; thread-local storage;
priorities; transferring a socket to a thread (handles are already shared); a free-threaded
CPython or subinterpreter backend; a `MAH_THREADS=inline` debugging mode.

Also deferred, from the contract review:
- **Returning semaphore permits of abandoned tasks** (tracking permits per VM and giving them back
  in `forget_vm`): permits stay owner-less; the docs say to release with `defer` (§2.1, §2.3, §11.5).
- **Making several unobserved job failures report deterministically**: the order stays the order
  replies arrive (documented, §2.3); tests avoid it.
- **Cheaper shared reads** (copy-on-write or immutable sharing of shared values): every non-locked
  read copies (documented cost, §5.2, §11.5).
- **A runtime test for a job Promise settled by hand**: no exported std function settles another
  Promise today, so the §6.2 guard is specified for both VMs but only reachable from std code.
