---
title: Async
order: 10
section: Language
---

Mah's async is single-threaded and cooperative. Calls are synchronous
unless you `detach` them. To run work on other threads, see
[Threads](#threads) below and [std:thread](/std/thread).

```mah
fn fetch(n) { sleep_async(100); n * 2 }   # a bare sleep_async just waits

let p = detach fetch(21)        # starts it; you get a Promise back
print("meanwhile")
print(p.await)                  # waits for the result: 42
let q = detach "mah".to_upper()  # methods can be detached too
print(q.await)                  # MAH
let r = detach {                # so can any expression: a block, a loop,
    sleep_async(10);            # an `if`, a `match`, `(a + b)`, ...
    2 + 3
};
print(r.await)                  # 5
```

- Detaching a call evaluates its arguments right away, in the caller;
  only the call itself runs as the new task. Any other expression runs
  entirely in the new task and sees surrounding variables by reference
  (it reads their values when it runs, not when you detach it).
- `detach` binds tightly: `detach a + b` is `(detach a) + b`; write
  `detach (a + b)`. `return`, and a `break`/`continue` for a loop
  outside the detached expression, are compile errors inside it.
- `value.await` waits for a Promise. A Promise is an enum:
  `Promise.Pending`, `Promise.Settled { value }`, or `Promise.Failed {
  error }` when the task threw: `.await` then re-throws `error` in the
  awaiting task (see [Errors](/docs/errors)).
- The program exits once the main code is done **and** no detached work
  is still pending.

## Detaching a method call

`detach` accepts any call chain, not just a plain `name(args)`:

```mah
struct Rect { w, h }
impl Rect {
    fn new(w, h) { Self { w: w, h: h } }
    fn grow(self, by) { sleep_async(5); Rect { w: self.w + by, h: self.h + by } }
}
let big = detach Rect.new(1, 1).grow(4)
print(big.await)   # Rect { w: 5, h: 5 }
```

## `sleep_async`

`sleep_async(ms)` pauses. Called plainly (not detached) it just blocks
like any other statement; detach it (or a block containing it) to run it
concurrently with other detached work.

## `input`

`input(prompt = "")` prints the prompt and waits for a line from
standard input, returning it as a String (without the newline). It
throws `EndOfInput` once there are no more lines. Called plainly it waits
like `sleep_async`. Detached, you get a Promise right away, and timers
and other tasks keep running while the user types:

```mah
let answer = detach input("number: ")
for let i in 0..3 {
    sleep_async(500)
    print("still waiting...")
}
let n = try answer.await.to_number() else 0
print("you typed", n)
```

A detached `input` keeps the program running until its line arrives, even
if nothing ever `.await`s it.

## Threads

`detach(t) expr`, with `t` a thread from [std:thread](/std/thread), runs
`expr` on that thread instead of as a task here. It is still a Promise you
`.await`; jobs given to one thread run one at a time, in order:

```mah
import thread from "std:thread"

fn fib(n) {
    if n < 2 { return n }
    fib(n - 1) + fib(n - 2)
}

let worker = thread.spawn(name: "worker")
let a = detach(worker) fib(20)         # runs on `worker`
let b = detach(worker) { fib(15) + 1 } # queued behind it
print(a.await, b.await)                # 6765 611
worker.join()
```

The job runs on a **copy** of every global and of what it captures, taken
when it's queued, so it can't change your variables (it changes its copy);
its result comes back as a copy. To share state between threads, use
`shared let` variables changed inside `atomic { }`, semaphores or channels: see
[std:thread](/std/thread).

It's the thread form only when the operand starts on the same line as
`detach(t)`. `detach (a + b)` with nothing after the `)` is still the
ordinary, same-thread `detach`.
