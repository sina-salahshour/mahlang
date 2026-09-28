---
title: "v0.2.0: typed, checked errors and the static type checker"
date: 2026-09-28
description: "throw/try/catch with a built-in Error trait and RuntimeError enum on both VMs, a checker that infers what every function throws, and the first static type checker with mah check."
tags: [changelog]
version: "0.2.0"
---

This release (milestones M22 through M26) adds two things Mah didn't
have: a static type checker, and errors you can throw, catch, and have
checked. Neither costs anything at run time. Types and error sets are
erased before codegen, and the only bytecode change is 1.4's handler table
for `try`.

## Errors: `throw`, `try`, `catch`

Anything implementing the built-in `Error` trait can be thrown. `try {
... } catch { arms }` takes `match` arms plus a type-test pattern (`e:
Type`). An error no arm matches is re-thrown automatically, and
`try EXPR else FALLBACK` catches everything.

```mah
enum ParseError { Empty, BadDigit }
impl Error for ParseError {}

fn first_digit(s) {
    if s.len() == 0 { throw ParseError.Empty }
    let c = s.char_at(0)
    if c < "0" | c > "9" { throw ParseError.BadDigit }
    c
}

let a = try { first_digit("") } catch {
    ParseError.Empty => { "was empty" }
    e: ParseError => { "bad: " + e.to_string() }
}
let b = try first_digit("x") else "?"
let c = try { 1 / 0 } catch { e: RuntimeError => { e.message } }
print(a, b, c)     # was empty ? Division by zero
```

Everything the VM itself used to abort on (division by zero, a missing
method, a bad index, a `match` with no arm for a value, ...) is now a
value of the built-in `RuntimeError` enum, catchable like any other error.
An uncaught one still prints exactly the message and location it always
did. `defer`red blocks run while an error unwinds past them. A detached
task that throws fails its Promise (the new `Promise.Failed { error }`)
instead of stopping the program, and `.await` re-throws the error. Both
VMs implement all of this identically, with bytecode format 1.4.

## Checked errors

The checker infers what every function can throw, with no annotations:
its body's `throw`s, plus what it calls and awaits, minus what a `try`
around them fully handles. An error that can reach the top of the program
is reported, and a `throws` clause (on a function, or on a function type
such as `fn(String) -> String throws never`) is checked against the body.
Leaving `throws` off means "inferred".

```mah
fn load(s) { first_digit(s) }                  # fn load(s: String) -> String throws ParseError
fn safe(s) { try load(s) else "0" }            # fn safe(s: String) -> String
fn strict(s: String) -> String throws never { load(s) }
load("")
```

```text
$ mah check        # both samples above, in one src/main.mh
warning: 'strict' can throw ParseError, which isn't in its throws list at position #20:1
warning: Unhandled error: ParseError at position #21:1
2 warnings
```

Those are warnings in `loose` mode and errors in `strict`/`explicit`. A
`catch` arm for an error the body can never throw is always just a
warning. Callbacks are tracked per call, `RuntimeError`s are catchable but
never tracked, and hover shows each function's `throws`. See
[Errors](/docs/errors).

## The static type checker

Optional annotations everywhere a name is declared, with the rest
inferred, generics included: `fn id(x) { x }` is `fn<T>(T) -> T`, and `fn
add(a, b) { a + b }` is `fn(Number, Number) -> Number`. Method calls on
your own types and on `String`/`Vector`/`Map` are typed too. How strict it
is comes from `mah-project.toml`:

```toml
[types]
check = "loose"      # "loose" | "strict" | "explicit"
```

`mah check` prints every diagnostic, and the editor shows them as you type,
along with inferred types on hover. See [Types](/docs/types).

## Editors

The tree-sitter grammar (Neovim) and the VS Code TextMate grammar now
highlight `throw`, `try`, `catch`, `throws` and `never`. The last three
only count as keywords where they can be one, so `let catch = 1` still
works.
