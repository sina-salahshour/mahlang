---
title: std:async
order: 11
section: Async & system
summary: Wait for many Promises at once (all, race, timeout) and run timers (set_timeout, set_interval).
---

# `std:async`

Tools for working with several tasks at once: wait for all of them, for
the first, or with a deadline; and timers that call a function later or
repeatedly. It builds on the language's own `detach`, `.await` and
`sleep_async` (see [Async](/docs/async)).

```mah
import async from "std:async"

fn fetch(ms: Number, name: String) -> String {
    sleep_async(ms)
    name
}

print(async.all([detach fetch(30, "a"), detach fetch(10, "b")]))        # [a, b]
print(async.race([detach fetch(30, "slow"), detach fetch(5, "fast")]))  # fast
```

## A quick refresher

`detach f()` starts `f` as a background task and gives a **Promise** right
away; `p.await` waits for its value. Mah's concurrency is cooperative and
single-threaded: tasks take turns whenever one waits (`sleep_async`, I/O,
`.await`). `std:async`'s functions take those Promises.

## `all`: wait for everything

`all(promises)` gives every value, **in the same order as the Promises**
(not the order they finished), once all are done. If any fails, it throws
that error as soon as it happens:

```mah
import async from "std:async"

fn square_later(n: Number) -> Number {
    sleep_async(10 - n)        # later items finish first
    n * n
}

let jobs = (1..=4).map(fn(n) { detach square_later(n) }).reduce()
print(async.all(jobs))           # [1, 4, 9, 16]
```

Starting all tasks first and then waiting is what makes them run
**concurrently**: four 10ms waits take about 10ms in total, not 40ms.

## `race`: the first to finish

`race(promises)` gives the value of whichever settles first, or the error
of the first to fail if that comes first. The others keep running in the
background.

## `timeout`: a deadline

`timeout(promise, ms)` gives the value if it arrives within `ms`
milliseconds, and otherwise throws `async.TimeoutError`. The task itself
keeps running.

```mah
import async from "std:async"

fn slow() -> String {
    sleep_async(500)
    "done"
}

try {
    print(async.timeout(detach slow(), 20))
} catch {
    e: async.TimeoutError => { print(e.message(), e.ms) }   # timed out after 20 ms 20
}
```

The type checker knows each function's result type and what it can
throw: `all` of `Promise<Number>`s is a `Vector<Number>`, and `timeout`
adds `TimeoutError` to the errors you need to handle.

All three wait like ordinary calls. To keep doing other work meanwhile,
`detach` them too: `let both = detach async.all([...])`.

## Timers

`set_timeout(f, ms)` calls `f` once after `ms` milliseconds;
`set_interval(f, ms)` calls it repeatedly, `ms` after the previous call
returned. Both give a `TimerId` for `clear_timeout`/`clear_interval`.

```mah
import async from "std:async"

let ticks = [0]
let ticker = async.set_interval(fn() { ticks[0] = ticks[0] + 1 }, 10)
async.set_timeout(fn() {
    async.clear_interval(ticker)
    print("stopped after", ticks[0] >= 3, "ticks")    # stopped after true ticks
}, 55)
```

- An **active interval keeps the program running**; a pending timeout does
  too. Once cleared, they don't. The program above exits right after the
  timeout clears the interval.
- Callbacks have the type `fn() throws never`: they can't throw, so
  handle errors inside them with `try`/`catch`.
- Clearing a timer twice, or after it fired, does nothing.

## Reference

| Function | |
|---|---|
| `all(promises)` | every value, in order, once all are done; throws the first error |
| `race(promises)` | the first value (or error) to arrive |
| `timeout(promise, ms)` | the value, or throws `TimeoutError` after `ms` |
| `set_timeout(f, ms)` | calls `f` once after `ms`; returns a `TimerId` |
| `set_interval(f, ms)` | calls `f` every `ms` until cleared |
| `clear_timeout(id)`, `clear_interval(id)` | stop a timer (both work on either kind) |

`async.TimeoutError { ms }` is `timeout`'s error.
