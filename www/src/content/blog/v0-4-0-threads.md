---
title: "v0.4.0: threads, shared variables and channels"
date: 2026-10-08
description: "std:thread runs jobs on other OS threads with detach(t) and t.run, on copies of what they use; shared let variables and lock share state safely; semaphores and channels; deadlocks are reported instead of hanging. Also since 0.3.0: an HTTP server and packages from GitHub."
tags: [changelog]
version: "0.4.0"
---

This release (milestone M44) lets a Mah program use more than one core. A
new module, `std:thread`, runs jobs on other OS threads; `shared let`
variables and the `lock` block share state between them without data races;
semaphores and channels coordinate them. Programs that use any of it need
bytecode **1.21** (`mah build` marks each program with the lowest version it
needs, as before); everything else is unchanged.

On the Rust VM, jobs run **in parallel**. The Python VM runs them on real
threads too, one at a time under the GIL, with exactly the same results.

## Threads are job queues

`thread.spawn()` starts a thread; `detach(t) expr` runs an expression on it,
and `t.run(f, ...args)` runs a function with arguments. Both give a Promise,
like the `detach` you know. Jobs given to one thread run one at a time, in
the order you queued them; `workers: n` makes a pool that runs `n` at once.

```mah
import thread from "std:thread"

fn fib(n) {
    if n < 2 { return n }
    fib(n - 1) + fib(n - 2)
}

let worker = thread.spawn(name: "worker")
let p = detach(worker) fib(18)                 # runs on `worker`
print("fib(18) =", p.await)                    # fib(18) = 2584
print(worker.run(fib, 12).await, "on", detach(worker) { thread.name() }.await)   # 144 on worker
worker.join()
```

Plain `detach f()` and `detach { ... }` still start a task on the current
thread, exactly as before.

## Jobs run on copies

Mah's values are shared by reference inside one thread, which is what makes
closures and `detach` so convenient, and exactly what can't safely cross
threads. So a job gets a **copy** of every global and of everything it
captures, taken when it's queued, and its result comes back as a copy.
Changing a variable in a job changes its own copy:

```mah
import thread from "std:thread"

let greeting = "hello"
let worker = thread.spawn(name: "worker")
let job = detach(worker) {
    greeting = greeting + " from " + thread.name()
    greeting
}
print(job.await, "/", greeting)                # hello from worker / hello
```

An error comes back the same way, so `try { p.await } catch { ... }` works
across threads. File and socket handles are shared by every thread, and
`print` from several threads never tears a line.

One consequence worth knowing: `std:random`'s generator state is a global,
so it's copied too, and **identical jobs draw identical numbers**. Seed each
job differently (`random.seed(thread.id() * 1000 + n)`) or pass a seed as an
argument.

## Shared variables and `lock`

To share state, declare it `shared`. A shared variable lives outside every
thread; reading it gives a copy, assigning it is atomic, and changing it in
place — or reading and writing it together — happens inside `lock`:

```mah
import thread from "std:thread"

shared let hits = 0
shared let seen = []

fn visit(n) {
    for let i in 0..50 {
        lock hits { hits = hits + 1 }
    }
    lock seen { seen.push(n) }
}

let pool = thread.spawn(name: "pool", workers: 4)
let jobs = []
for let n in 0..8 { jobs.push(pool.run(visit, n)) }
for let job in jobs { job.await }
let snapshot = seen
print("hits:", hits, "seen:", snapshot.len())  # hits: 400 seen: 8
pool.join()
```

`lock a, b { ... }` gives the current task those variables for the block;
everyone else waits (and waiting lets the thread's other tasks run). The
changes are written back when the block ends, also on `return`, `break` or a
throw. Locks are re-entrant and served in order.

The obvious trap — `shared let v = []` then `v.push(1)`, which would push
onto a copy and lose it — is a **compile error**: outside `lock v`, method
calls on `v`, assignments into it and `v = ... v ...` are refused, with a
message that says to use `lock`. The indirect versions the compiler can't
see for sure (passing `v` to a function that changes its parameter, changing
the loop variable of `for let item in v`) are checker warnings.

## Deadlocks are errors, not hangs

A lock wait, `.await` or `join` that would wait for itself — say
`lock x { (detach(t) { x = 1 }).await }`, where the job needs the lock the
awaiting task holds — throws `ThreadError` with kind `"deadlock"` at once.
And when every thread is waiting for something only another thread could do,
those waits fail with `"stuck"` instead of hanging forever.

## Semaphores and channels

`thread.semaphore(n)` allows at most `n` holders at once across every
thread. `thread.channel()` is a queue of messages, each copied when sent,
that any thread can send to and receive from; `for let m in ch` receives
until it's closed and empty:

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
print("sum of squares:", sum)                  # sum of squares: 55
pool.join()
```

Everything in `std:thread` throws the new built-in `ThreadError { kind,
message }`. The editor tooling knows the new syntax: `shared` and `lock`
highlight and hover as keywords, a shared variable hovers as one, and `mah
format` writes `detach(t) f(x)` without a space before the `(`.

The full guide is [std:thread](/std/thread); the design, with every rule, is
`docs/contracts/M44_threads.md` in the repository.

## Compatibility

Three things that compiled before behave differently:

- `detach (x) expr` with another expression on the **same line** after the
  `)` is now the thread form (it used to be two statements). `detach (a + b)`
  alone on its line, `detach (f)(x)` and `detach (a) - b` keep their meaning.
- `detach (x)` followed by a statement on the same line (`print`, `let`,
  `return`, ...), or `detach (name)` followed by a `{` block on the next line,
  are now compile errors.
- `ThreadError` is a new built-in name, so a program that declares its own
  `ThreadError` type gets the "built-in name" error. Rename it.

`shared` and `lock` are contextual keywords: variables named `shared` or
`lock` still work.

## Also since 0.3.0

- **An HTTP server** (M42): `std:http`'s `serve(port, handler)` serves
  HTTP/1.1 with keep-alive, limits, graceful shutdown and TLS, with handlers
  that are plain `fn(Request) -> Reply` functions. See [std:http](/std/http).
- **Packages from GitHub** (M43): `[dependencies]` in `mah-project.toml`
  names GitHub repositories; `mah install` fetches and pins them in
  `mah-lock.toml`, and `import x from "pkg:NAME"` uses them. See
  [Projects](/docs/projects).
