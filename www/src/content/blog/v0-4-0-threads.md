---
title: "v0.4.0: threads, shared variables and channels"
date: 2026-10-08
description: "std:thread runs jobs on other OS threads with detach(t) and t.run, on copies of what they use; shared let variables and atomic blocks share state safely; semaphores and channels; waits that can never end are reported instead of hanging. Also since 0.3.0: an HTTP server and packages from GitHub."
tags: [changelog]
version: "0.4.0"
---

This release (milestones M44 and M45) lets a Mah program use more than one core. A
new module, `std:thread`, runs jobs on other OS threads; `shared let`
variables and `atomic { }` blocks (software transactional memory) share state
between them without data races;
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

## Shared variables and `atomic`

To share state, declare it `shared`. A shared variable lives outside every
thread: reading it gives a copy and never waits, and assigning it is atomic.
Changing it in place — or reading and writing it together — happens inside an
`atomic { }` block:

```mah
import thread from "std:thread"

shared let hits = 0
shared let seen = []

fn visit(n) {
    for let i in 0..50 {
        atomic { hits = hits + 1 }
    }
    atomic { seen.push(n) }
}

let pool = thread.spawn(name: "pool", workers: 4)
let jobs = []
for let n in 0..8 { jobs.push(pool.run(visit, n)) }
for let job in jobs { job.await }
let snapshot = seen
print("hits:", hits, "seen:", snapshot.len())  # hits: 400 seen: 8
pool.join()
```

`atomic { body }` is **software transactional memory**, the model of
Haskell's STM and Clojure's `dosync`. The body reads a consistent snapshot of
the shared variables and changes private working copies of them. When it
ends, the runtime checks that nothing it read was changed by another thread
in the meantime, and publishes all its changes at once; no thread ever sees
half of them. If something it read did change, the working copies are thrown
away and the body **runs again** from the start, on the new values. There
are no locks to take, so there's no lock order to get wrong:

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
let both = atomic { [checking, savings] }      # both from one snapshot
print(both[0] + both[1])                       # 150, whatever other threads do
```

A throw out of `atomic` publishes nothing. Its value is the body's value. An
`atomic` inside another one — written there, or in a function it calls —
joins it, so helpers that wrap their own changes in `atomic { }` compose into
bigger transactions. And a transaction that had to run again 8 times runs its
next attempt alone, with every other thread's commits waiting for it, so a
busy variable can't starve anyone.

**Waiting for a condition** is `retry`: it gives up this run of the
transaction and waits until a shared variable it read changes, then runs it
again. That replaces condition variables; a blocking queue is five lines:

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
let got = worker.run(take)                     # waits for something to take
atomic { queue.push("job 1") }
print(got.await)                               # job 1
worker.join()
```

The waiting task is a pending Promise, so its thread's other tasks keep
running, and a `retry` nobody can ever wake fails with `ThreadError`
`"stuck"` instead of hanging.

### Why the compiler is strict about shared variables

**Changing one in place, or `x = f(x)`, outside `atomic` is a compile
error.** Outside a transaction a read gives you a copy, so `shared let v =
[]` then `v.push(1)` would push onto a temporary copy, and the push would be
silently lost. `count = count + 1` is a read followed by a write, and another
thread can write in between:

```text
thread A: reads count      (5)
thread B: reads count      (5)
thread A: writes 5 + 1     (6)
thread B: writes 5 + 1     (6)   # one increment is lost
```

Inside `atomic { count = count + 1 }`, B's commit notices that `count`
changed after B read it, and B's body runs again on 6. So the compiler
refuses both forms with a message that says to wrap the code in `atomic {
... }`. To be honest about the limit: it only catches the obvious form, the
read and the write in the same statement. `let y = count` followed by `count
= y + 1` compiles and has exactly the same race. Passing a shared variable to
a function that changes its parameter, or changing the loop variable of `for
let item in v`, are checker warnings. The rule to remember: **to change a
shared value based on itself, or in place, do it inside `atomic { }`.**

**Inside `atomic`, there's no I/O and no waiting.** A transaction can run
more than once, or be thrown away. Its changes to shared variables are
private copies, so undoing them is free; effects on the outside world can't
be undone. A `print` would print twice. An HTTP request or a file write would
happen twice. A channel message sent by an attempt that was then thrown away
would still be received, as if it had happened; a `recv` would take a
message that the next run never sees. A semaphore permit taken by a discarded
attempt would never come back, and a `detach`ed task would escape the
transaction altogether. Waiting is out for a second reason: an `.await` or a
sleep inside a transaction widens the window for conflicts, and while a
transaction runs alone it would hold up every other thread's commits — if
what it waits for needs one of those commits, we'd have built a deadlock.
`retry` is the safe way to wait, because it ends the attempt first.

So `print`, `input`, `.await`, `sleep_async` and `detach` written inside
`atomic` are compile errors. A function call can hide an effect, though —
`fn log(m) { print(m) }` called inside the block compiles fine — so the
runtime checks too: I/O, files, sockets, timers, starting or joining jobs,
channels and semaphores throw `ThreadError` `"in_atomic"` inside a
transaction. Pure functions, `std:random` and clock reads are fine. Haskell
enforces the same rule with its type system (STM code can't do IO), Clojure
with `io!`. The pattern is *decide inside, act outside*:

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
if sold { print("sold one") }                  # runs once, after the commit
```

**`shared let` is top level only.** Every thread runs its own copy of the
program, and a local variable exists once per call and is copied into the
jobs that use it. A top-level `shared let` has one identity that every thread
agrees on; the compiler can see every use of it, which is what lets it
enforce the rules above; and its value lives as long as the program does.
First-class shared cells, `thread.ref`, that you make at run time and pass
around like any value, are planned as a follow-up.

## Waits that can never end are errors

An `.await` or `join` that would wait, through threads, for itself — two
jobs each joining the other's thread, or a job joining its own thread —
throws `ThreadError` with kind `"deadlock"` at once. And when every thread is
waiting for something only another thread could do — a semaphore, a channel,
a job, or a `retry` nobody can wake — those waits fail with `"stuck"` instead
of hanging forever.

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
message }`. The editor tooling knows the new syntax: `shared`, `atomic` and
`retry` highlight and hover as keywords, a shared variable hovers as one, and `mah
format` writes `detach(t) f(x)` without a space before the `(`.

The full guide is [std:thread](/std/thread); the design, with every rule, is in
`docs/contracts/M44_threads.md` and `docs/contracts/M45_atomic.md` in the
repository.

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

`shared`, `atomic` and `retry` are contextual keywords: variables named
`shared`, `atomic` or `retry` still work (a `retry` alone at the end of a
statement inside `atomic` is the keyword; a variable of that name visible
there is a compile error asking you to rename it).

## Also since 0.3.0

- **An HTTP server** (M42): `std:http`'s `serve(port, handler)` serves
  HTTP/1.1 with keep-alive, limits, graceful shutdown and TLS, with handlers
  that are plain `fn(Request) -> Reply` functions. See [std:http](/std/http).
- **Packages from GitHub** (M43): `[dependencies]` in `mah-project.toml`
  names GitHub repositories; `mah install` fetches and pins them in
  `mah-lock.toml`, and `import x from "pkg:NAME"` uses them. See
  [Projects](/docs/projects).
