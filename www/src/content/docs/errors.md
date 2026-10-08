---
title: Errors
order: 16
section: Language
---

Errors in Mah are thrown, not returned: a failing call throws a value, and
the code around it reads straight through until some `try` catches it.
Anything thrown must be a struct or enum that implements the built-in
`Error` trait.

```mah
enum ParseError { Empty, BadDigit }
impl Error for ParseError {
    fn message(self) {
        match self {
            ParseError.Empty => { "empty input" }
            ParseError.BadDigit => { "not a digit" }
        }
    }
}

fn first_digit(s) {
    if s.len() == 0 { throw ParseError.Empty }
    let c = s.char_at(0)
    if c < "0" | c > "9" { throw ParseError.BadDigit }
    c
}

print(try first_digit("7x") else "?")    # 7
print(try first_digit("") else "?")      # ?
```

`throw` is an expression that never produces a value, so it fits anywhere
one is expected: `let n = if ok { x } else { throw E.Oops }`. `Error`'s
default `message()` is the value's own `to_string()`; override it, as
above, for a nicer message.

## Catching

`try { ... } catch { arms }` is an expression. Its arms are `match` arms,
guards included, plus one extra pattern: `name: Type` (or `_: Type`)
matches any instance of that type.

```mah
fn describe(s) {
    try { first_digit(s) } catch {
        ParseError.Empty => { "was empty" }
        e: ParseError => { "other: " + e.message() }
    }
}
print(describe(""))     # was empty
print(describe("x"))    # other: not a digit
```

An error no arm matches is re-thrown to whatever encloses the `try`, so a
`catch` only handles what it names. `try EXPR else FALLBACK` (or
`try { ... } else FALLBACK`) catches everything. `_ => { ... }` and
`e => { ... }` catch everything too, and `throw e` inside one re-throws
what it caught.

## Runtime errors

Every failure the VM itself raises (division by zero, calling a missing
method, an out-of-range index, a `match` with no arm for a value, ...) is a
value of the built-in `RuntimeError` enum, and can be caught the same way:

```mah
print(try { 1 / 0 } catch { RuntimeError.DivisionByZero { message } => { message } })
print(try { 1 % 0 } catch { e: RuntimeError => { e.message } })
```

Both print `Division by zero`. Every variant carries a `message`.

## Cleanup, tasks, and uncaught errors

- `defer`red blocks still run, in order, while an error unwinds past them.
- A detached task that throws fails its Promise (`Promise.Failed { error }`)
  instead of stopping the program. `.await` re-throws the error in the
  task that awaits it.
- An error nothing catches stops the program with `Uncaught` and its
  message, located at the `throw` (a detached task's failed Promise that
  nobody awaited is reported when the program ends).
- A job on another thread ([std:thread](/std/thread)) that throws fails its
  Promise with a **copy** of the error, so `try { p.await } catch { e:
  MyError => ... }` works across threads.

## `ThreadError`

Threads, `shared` variables, `lock`, semaphores and channels throw the
built-in `ThreadError { kind, message }`. Its `kind` is one of `"closed"`,
`"full"`, `"cancelled"`, `"deadlock"` (a lock or `.await` that would wait
for itself), `"stuck"` (every thread is waiting, so the wait can never
finish), `"not_sendable"` (a Promise sent to another thread),
`"foreign_promise"` and `"over_release"`:

```mah
import thread from "std:thread"

let t = thread.spawn()
t.close()
print(try { t.run(fn() { 1 }) } catch { e: ThreadError => { e.kind } })   # closed
```

A `lock` block left by a throw still writes its changes back; if that fails,
the original error keeps going. Since `ThreadError` is a built-in name, a
type of your own can't be called `ThreadError` any more.

## What the checker knows

The type checker works out what every function can throw, its **error
set**, without any annotation. A function throws what its body throws,
plus what the functions it calls, the Promises it awaits, and its
`defer`red blocks throw, minus whatever a `try` around them fully handles.
Hovering a function in your editor shows it:

```mah
fn digit_or_zero(s) { try first_digit(s) else "0" }   # fn digit_or_zero(s: String) -> String
fn strict_digit(s) { first_digit(s) }    # fn strict_digit(s: String) -> String throws ParseError
```

An arm handles a whole type when it has no guard and covers every value of
it: `e: T`, a catch-all, or arms for every variant of an enum. One variant,
or a guarded arm, handles only part of it, so the type stays in the set.
Callbacks are tracked per call: in `fn apply(f) { f() }`, a call passing a
function that throws throws too, and a call passing one that doesn't,
doesn't.

What the checker reports, at each [strictness level](/docs/types):

| Diagnostic | `loose` | `strict` / `explicit` |
|---|---|---|
| `Unhandled error: ParseError` (an error that can reach the top of the program) | warning | error |
| a function throwing something its `throws` list doesn't include | warning | error |
| throwing a value whose type doesn't implement `Error` | warning | error |
| `X is never thrown here` (a `catch` arm for an error the body can't throw) | warning | warning |
| `Can't infer what this throws` (calling an `Unknown` value) | — | `explicit` only |

`RuntimeError`s are catchable but never tracked: almost any code can
divide or index, so tracking them would put them in every set.

## Declaring what a function throws

A `throws` clause after the return type is optional. With one, callers see
exactly what it lists, and the checker makes sure the body can't throw
anything else; `throws never` says it throws nothing. Function types take
one too. Without a `throws` clause, a function type's error set is
inferred, just as it would be for the function itself.

```mah
fn load(s: String) -> String throws ParseError { first_digit(s) }
fn pure(x: Number) -> Number throws never { x * 2 }

fn run_safely(cb: fn() -> String throws never) -> String { cb() }
fn run_any(cb: fn() -> String) -> String { cb() }   # throws what `cb` throws

print(run_safely(fn() { "ok" }))                    # ok
print(try run_any(fn() { load("x") }) else "failed")  # failed
```

`catch`, `throws` and `never` are only keywords in these positions, so
they still work as ordinary names elsewhere (`let catch = 1`).
