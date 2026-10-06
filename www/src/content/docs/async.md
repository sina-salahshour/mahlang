---
title: Async
order: 10
section: Language
---

Mah's async is single-threaded and cooperative. Calls are synchronous
unless you `detach` them.

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
