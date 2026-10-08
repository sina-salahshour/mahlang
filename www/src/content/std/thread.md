---
title: std:thread
order: 13.5
section: Async & system
summary: Run jobs on other threads, share variables between them with shared let and lock, and pass messages through channels.
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

So there's nothing to lock for ordinary variables: a job can't change
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
- **Random numbers**: `std:random`'s state is a global, so each job continues
  the submitter's sequence from the same point, and identical jobs draw
  **identical numbers**. Seed per job, `random.seed(thread.id() * 1000 + n)`,
  or pass a seed as an argument.

## Shared variables and `lock`

`shared let NAME = value` (top level only) declares a variable every thread
shares. Reading it gives a copy; assigning it is atomic; to change it in
place (`push`, `x[k] = v`) or to read and write it together, use `lock NAME {
... }`:

```mah
import thread from "std:thread"

shared let hits = 0
shared let log: Vector<String> = []

fn visit(n) {
    for let i in 0..10 {
        lock hits { hits = hits + 1 }
    }
    lock log { log.push("visit " + n) }
}

let pool = thread.spawn(name: "pool", workers: 4)
let jobs = []
for let n in 0..8 { jobs.push(pool.run(visit, n)) }
for let j in jobs { j.await }
let seen = log                               # a copy
print(hits, seen.len())                      # 80 8
let next = lock hits { hits = hits + 1; hits }   # a lock's value is its body's
print(next)                                  # 81
pool.join()
```

`lock a, b { body }` gives this task the shared variables for the block:
other tasks and threads wait, the body changes them in place, and the changes
are written back when the block ends (also on `return`, `break` or a throw).
It's re-entrant, waiters are served in order, and waiting lets the thread's
other tasks run.

The compiler stops the common mistakes: outside `lock xs`, a method call on
`xs` (even `xs.len()`: read it into a local first), an assignment into it
(`xs[0] = 1`) and `x = ... x ...` are errors. A shared variable holding a
thread, semaphore or channel handle (`shared let jobs = thread.channel()`)
may call its methods anywhere.

These act on a **copy** and change nothing shared:

- `let s = xs; s.push(1)`, or `let m = shared_map; m[k] = v`;
- passing `xs` to a function that changes its parameter (`mutate(xs)`);
- changing the loop variable of `for let item in xs` (`item.n = 1`).

Inside `lock xs { }` all of these work on the shared value itself; the
checker warns about the last two outside it. And **read-compute-write needs
`lock`**: `x = g()` where `g` reads `x` loses updates made in between; write
`lock x { x = g() }`.

A shared read happens at its place in left-to-right evaluation (`f(x, g())`
passes the `x` from before `g` ran), and every read outside `lock` copies the
whole value: read a big one once into a local, or work inside one `lock`,
rather than reading it in a loop.

## Deadlocks

A wait that would close a cycle — a `lock` waiting for a task that waits for
this one, or `lock x { (detach(t) { x = 1 }).await }` — throws `ThreadError`
`deadlock` at once instead of hanging. When every thread is waiting for
something only another thread could do (a lock, a semaphore, a channel, a
join, a job), the waits fail with `stuck`. A wait that only *another running
thread* could end (a server loop, a timer) keeps waiting.

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
| `"deadlock"` | a lock, `.await` or `join` that would wait for itself |
| `"stuck"` | a wait that can never finish because every thread is waiting |
| `"not_sendable"` | a Promise inside a value sent to another thread or stored in a shared variable |
| `"foreign_promise"` | awaiting, in a job, a Promise that was still pending when it was copied |
| `"over_release"` | `release()` with every permit free |

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

Not yet: lock timeouts, `select` over several channels, interrupting a running
job.
