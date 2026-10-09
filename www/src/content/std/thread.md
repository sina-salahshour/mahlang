---
title: std:thread
order: 13.5
section: Async & system
summary: Run jobs on other threads, share variables between them with shared let and atomic blocks, and pass messages through channels.
---

# `std:thread`

Run work on other OS threads. A **thread** is a queue of jobs: you start one
with `thread.spawn()`, hand it jobs with `detach(t) expr` or `t.run(f,
...args)`, and get a Promise for each. Jobs queued on one thread run one at a
time, in the order you queued them; `workers: n` makes a pool that runs `n`
at a time.

```mah
import thread from "std:thread"

fn checksum(data) {
    let total = 0
    for let x in data { total = (total * 31 + x) % 1000003 }
    total
}

let worker = thread.spawn(name: "worker")
let p = detach(worker) checksum([3, 1, 4, 1, 5])   # runs on `worker`
let q = worker.run(checksum, [2, 7, 1, 8])         # the same, for a function and arguments
print(p.await, q.await)
worker.join()                                      # close it and wait for its jobs
```

On the Rust VM jobs run **in parallel**; on the Python VM they run on real
threads too, but one at a time (the GIL), with exactly the same results.
`std:thread` needs bytecode 1.21.

## Jobs run on copies

A job sees a **copy** of every global and of everything it captures, taken
when it is queued. Changing them in the job changes the copy; its result (or
error) comes back as a copy too.

```mah
import thread from "std:thread"

let names = ["ada"]
let worker = thread.spawn()
let job = detach(worker) {
    names.push(thread.name())    # the job's own copy
    names.len()
}
print(job.await, names.len())    # 2 1
```

So ordinary variables need no protection: a job can't change
anything another thread sees, except through `shared` variables, semaphores
and channels (below), files and sockets (their handles are shared by every
thread), and its result.

- `detach(t) expr` is the thread form when the operand starts on the **same
  line** as `detach(t)`. The whole operand runs on the thread, even the
  arguments of `detach(t) f(x)`. `detach (a + b)` with nothing after it is
  still the ordinary same-thread [detach](/docs/async).
- A job's error is caught as usual: `try { p.await } catch { e: MyError =>
  ... }`.
- A Promise can't be sent to another thread: one inside `t.run`'s arguments,
  a job's result or a channel message throws `ThreadError` `not_sendable`. A
  pending Promise a job reaches through a global fails with `foreign_promise`
  when the job awaits it.
- A job ends when its root call has finished and nothing it started is
  pending — a detached task, a timer, or a task waiting in `retry` (below)
  keeps it, and the program, alive.
- **Random numbers**: `std:random`'s state is a global, so each job continues
  the submitter's sequence from the same point, and identical jobs draw
  **identical numbers**. Seed per job, `random.seed(thread.id() * 1000 + n)`,
  or pass a seed as an argument.

## Shared variables and `atomic`

`shared let NAME = value` (top level only) declares a variable every thread
shares. Reading it gives a copy and never waits; assigning it (`NAME =
value`) is atomic; to change it in place (`push`, `x[k] = v`) or to read and
write it together, do it inside `atomic { ... }`:

```mah
import thread from "std:thread"

shared let hits = 0
shared let log: Vector<String> = []

fn visit(n) {
    for let i in 0..10 {
        atomic { hits = hits + 1 }
    }
    atomic { log.push("visit " + n) }
}

let pool = thread.spawn(name: "pool", workers: 4)
let jobs = []
for let n in 0..8 { jobs.push(pool.run(visit, n)) }
for let j in jobs { j.await }
let seen = log                               # a copy
print(hits, seen.len())                      # 80 8
let next = atomic {                          # an atomic's value is its body's
    hits = hits + 1
    hits
}
print(next)                                  # 81
pool.join()
```

`atomic { body }` runs `body` as one **transaction** (software transactional
memory, like Haskell's STM or Clojure's `dosync`):

- It reads a consistent snapshot of the shared variables, and changes them on
  private working copies. When the body ends, the runtime checks that nothing
  it read was changed by another thread in the meantime and publishes all its
  changes at once — no thread ever sees half of them. If something it read
  did change, it throws the working copies away and **runs the body again**
  from the start, on the new values.
- A throw out of it publishes nothing; the error goes on unchanged.
- Its value is the body's value. An `atomic` inside another (directly, or in
  a function it calls) **joins** it: only the outermost one commits. So a
  helper that changes shared variables can wrap its own code in `atomic { }`
  and still be used inside a bigger transaction.
- A transaction that had to run again 8 times runs its next attempt
  **alone**: while it does, every other thread's commits and shared
  assignments wait for it. So every transaction finishes, however busy the
  variables are.

Several variables change together:

```mah
shared let checking = 100
shared let savings = 50

fn transfer(amount) {
    atomic {
        if checking < amount { throw RuntimeError.ArgumentError { message: "not enough" } }
        checking = checking - amount
        savings = savings + amount
    }
}

transfer(30)
let both = atomic { [checking, savings] }    # read both from one snapshot
print(both[0], both[1], both[0] + both[1])   # 70 80 150
```

### Waiting with `retry`

`retry` (only inside `atomic { }`) gives up this run of the transaction and
waits until a shared variable it read changes, then runs it again — the way
to wait for a condition. The task waits like a pending Promise, so the
thread's other tasks keep running:

```mah
import thread from "std:thread"

shared let queue = []

fn take() {
    atomic {
        if queue.len() == 0 { retry }
        queue.pop_start()
    }
}

let worker = thread.spawn()
let got = worker.run(take)                   # waits until the queue has something
atomic { queue.push("job 1") }
print(got.await)                             # job 1
worker.join()
```

Wait with `retry`, not a loop: the body reads a snapshot, so `atomic { while
!ready { } }` never sees `ready` change. A plain read outside `atomic` (`while
!stop { ... }`) does see changes. A job (and the program) stays alive while
one of its tasks waits in `retry`.

### What the compiler checks

Outside `atomic`, these are compile errors, each with a message that says to
wrap the code in `atomic { ... }`: a method call on a shared variable (even
`xs.len()`: read it into a local first), an assignment into it (`xs[0] =
1`), and `x = ... x ...`. A shared variable holding a thread, semaphore or
channel handle (`shared let jobs = thread.channel()`) may call its methods
anywhere.

Inside `atomic`, these are compile errors: `print`, `input`, `.await`,
`sleep_async` and `detach`; `return`, `break` or `continue` leaving the block
(use its value instead); and assigning a non-shared variable declared outside
it (`count = count + 1`: return what you need as the block's value instead).
A function the block calls that does I/O or waits throws `ThreadError`
`in_atomic` at run time: the I/O, file, socket, timer and process functions,
`t.run`/`detach(t)`, `spawn`, `close`, `join`, and every semaphore and channel
operation except `available()`, `len()` and `closed()`. Pure functions
(math, strings, regex, bytes, json, reflect), `std:random`, reading the clock
or the environment, and making a new semaphore or channel are fine.

These act on a **copy** and change nothing shared:

- `let s = xs; s.push(1)`, or `let m = shared_map; m[k] = v`;
- passing `xs` to a function that changes its parameter (`mutate(xs)`);
- changing the loop variable of `for let item in xs` (`item.n = 1`).

Inside `atomic { }` all of these work on the transaction's value; the checker
warns about the last two outside it.

Not undone when a transaction runs again (only shared variables are
transactional): changing an object reached from an outer variable
(`outer.push(x)`), assigning an outer variable from a function or closure the
block calls, `std:random` draws. `ch.len()` and `s.available()` read live
state that isn't part of the snapshot (and doesn't wake a `retry`).

Reads outside `atomic` happen one variable at a time, so `print(a, b)` can
see `a` from before another thread's transaction and `b` from after it. To
read several consistently, read them together: `let both = atomic { [a, b]
}` (a read-only transaction never waits). A shared read happens at its place
in left-to-right evaluation (`f(x, g())` passes the `x` from before `g` ran),
and every read outside `atomic` copies the whole value: read a big one once
into a local, or work inside one `atomic`, rather than reading it in a loop.

`atomic {` needs its `{` on the same line, and in an `if`/`while`/`for`/
`match` head it must be in parentheses: `if (atomic { ready }) { ... }`.
`retry` is the keyword alone on its line, or before `}`/`;`/`,`/`)`/`]`,
inside `atomic`; a variable named `retry` that such a `retry` would see is a
compile error (rename it). Elsewhere `atomic` and `retry` are ordinary names.

## Why the rules

**Why changing a shared variable in place, or `x = f(x)`, needs `atomic`.**
The value of a shared variable lives outside every thread. Outside a
transaction, reading it gives you a copy, so `xs.push(1)` would push onto a
temporary copy and the push would be silently lost. The compiler refuses it
instead of letting the change vanish.

`count = count + 1` has a different problem: it reads, then writes, and
another thread can write in between. Two threads adding one each:

```text
thread A: reads count      (5)
thread B: reads count      (5)
thread A: writes 5 + 1     (6)
thread B: writes 5 + 1     (6)   # one increment is lost: count should be 7
```

Inside `atomic { count = count + 1 }`, thread B's commit notices that `count`
changed after it read it, throws its attempt away and runs the body again on
6, so the result is 7. The compiler catches only the obvious form, where the
read and the write are in the same statement. Split over two statements the
race is still there and compiles:

```mah
shared let count = 0
let y = count          # read...
count = y + 1          # ...write: another thread can write in between
```

So the rule to remember is: **to change a shared value based on itself, or in
place, do it inside `atomic { }`.**

**Why no I/O, waiting or new tasks inside `atomic`.** A transaction can run
more than once, or be thrown away (on a throw or a `retry`). Its changes to
shared variables are private working copies, so throwing them away is safe.
Effects on the outside world can't be taken back:

- a `print` would print twice; an HTTP request or a file write would happen
  twice;
- a channel message sent by an attempt that was then thrown away would still
  be received, as if the attempt had happened; a `recv` would take a message
  that the next run then doesn't get;
- a semaphore permit taken by a discarded attempt would never be given back;
- a task started with `detach` would escape the transaction and run anyway.

Waiting is refused too: an `.await` or a sleep inside a transaction makes the
window for conflicts wider, and while a transaction runs alone (after 8
conflicts) it would hold up every other thread's commits — and if what it
waits for needs one of those commits, nothing could ever move again: the very
deadlock `atomic` is meant to rule out. `retry` is the safe way to wait: it
ends the attempt first, then waits.

The compiler refuses what it can see (`print`, `input`, `.await`,
`sleep_async`, `detach` written in the block). A call can hide an effect,
though: `fn log(m) { print(m) }` called inside `atomic` compiles, so the
runtime checks too and throws `ThreadError` `in_atomic` (`'io.write' can't
run inside 'atomic { }': its body may run more than once`). Haskell enforces
the same rule with its type system (STM code can't do IO), and Clojure with
`io!`.

The pattern is **decide inside, act outside**: return what happened as the
block's value and do the effect after it:

```mah
shared let stock = 3

let sold = atomic {
    if stock > 0 {
        stock = stock - 1
        true
    } else {
        false
    }
}
if sold { print("sold one") }    # runs once, after the commit
```

**Why `shared let` is top level only.** Every thread runs its own copy of the
program, and a local variable exists once per call and is copied into the
jobs that use it. A top-level `shared let` has one identity that every thread
agrees on; the compiler can see every use of it, so it can enforce the rules
above; and its value lives as long as the program does. A `shared let`
inside a function would raise questions with no good answer (one per call?
which one does a job see?). First-class shared cells (`thread.ref`), made at
run time and passed around like values, are planned as a follow-up.

## Waits that can never end

An `.await` or `join` that would wait, through threads, for itself — two jobs
on different threads each joining the other's thread, or `t.join()` from one
of `t`'s own jobs — throws `ThreadError` `deadlock` at once instead of hanging.
When every thread is waiting for something only another thread could do (a
semaphore, a channel, a join, a job, or a `retry` nobody can wake), the waits
fail with `stuck`; a `retry` in an attempt that read no shared variable fails
with `stuck` at once. A wait that only *another running thread* could end (a
server loop, a timer) keeps waiting.

## Semaphores

A counting semaphore allows at most `permits` holders at once, across every
thread. Permits belong to nobody, so give them back with `defer`:

```mah
import thread from "std:thread"

let downloads = thread.semaphore(2)

fn fetch(n) {
    downloads.acquire()          # waits while 2 are running
    defer downloads.release()
    n * 10
}

let pool = thread.spawn(workers: 4)
let jobs = []
for let n in 1..=4 { jobs.push(pool.run(fetch, n)) }
for let j in jobs { print(j.await) }
print(downloads.available())     # 2
```

## Channels

A channel is a queue of messages shared by every thread; each message is
copied when it is sent. `for let m in ch` receives until the channel is
closed and empty:

```mah
import thread from "std:thread"

fn square_all(tasks: thread.Channel, results: thread.Channel) throws ThreadError {
    for let n in tasks { results.send(n * n) }
}

let tasks = thread.channel()
let results = thread.channel()
let pool = thread.spawn(workers: 3)
for let i in 0..3 { pool.run(square_all, tasks, results) }
for let n in 1..=5 { tasks.send(n) }
tasks.close()
let sum = 0
for let i in 0..5 { sum = sum + results.recv() }
print(sum)                       # 55
pool.join()
```

`capacity: n` bounds a channel (a send waits while it's full); `capacity: 0`
makes every send wait until a receiver takes the message.

## Errors

Everything here throws `ThreadError { kind, message }`, a built-in type:

| kind | when |
|---|---|
| `"closed"` | queueing on a closed thread; sending on a closed channel; receiving from a closed, empty one |
| `"full"` | queueing more than `workers + capacity` jobs |
| `"cancelled"` | a queued job dropped by `close(cancel: true)` |
| `"deadlock"` | an `.await` or `join` that would wait, through threads, for itself; `join` from the thread's own job |
| `"stuck"` | a wait that can never finish because every thread is waiting (including a `retry` nobody can wake); `retry` in an attempt that read no shared variable |
| `"not_sendable"` | a Promise inside a value sent to another thread, assigned to a shared variable, or committed by an `atomic` |
| `"foreign_promise"` | awaiting, in a job, a Promise that was still pending when it was copied |
| `"over_release"` | `release()` with every permit free |
| `"in_atomic"` | `.await`, `detach`, I/O or another side effect inside a transaction (`'NAME' can't run inside 'atomic { }': its body may run more than once`) |

With several failed job Promises that nobody awaited, which one the program
reports at exit can differ from run to run.

## Reference

| Function | |
|---|---|
| `spawn(name = none, workers = 1, capacity = none)` | a `Thread`: `workers` OS threads sharing one job queue; `capacity` limits how many jobs may wait to start |
| `id()`, `name()` | the current thread's id and name (`0`, `"main"` on the main thread) |
| `cores()` | how many threads this machine runs at once |
| `semaphore(permits)` | a `Semaphore` |
| `channel(capacity = none)` | a `Channel` (none: never full; 0: hand-over) |

| `Thread` | |
|---|---|
| `id`, `name`, `workers`, `capacity` | how it was made |
| `run(f, ...args)` | queue `f(...args)`, a Promise of its result (`detach(t) expr` queues any expression) |
| `close(cancel = false)` | take no more jobs; `cancel: true` drops the queued ones |
| `join()` | close, then wait until every job has finished |
| `pending()` | jobs queued or running |

| `Semaphore` | |
|---|---|
| `acquire()` | wait for a permit and take it |
| `try_acquire()` | take one if free now: `true`/`false` |
| `release()` | give one back |
| `available()` | free permits |

| `Channel` | |
|---|---|
| `send(value)` | send a copy, waiting while full |
| `recv()` | the next message, waiting for one |
| `try_recv()` | `some(message)` if one is there now, else `none` |
| `close()` | no more sends; queued messages can still be received |
| `len()`, `closed()` | queued messages; whether it's closed |
| `for let m in ch { }` | receive until closed and empty |

Not yet: `or_else` (choosing between transactions), first-class shared cells
(`thread.ref`), acquire timeouts, `select` over several channels,
interrupting a running job.
