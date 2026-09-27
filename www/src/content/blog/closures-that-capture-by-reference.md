---
title: "How Mah closures capture by reference"
date: 2026-09-20
description: "A quick look at the activation-record model behind Mah's closures, and why it gives you JavaScript-style capture instead of Python's."
tags: [design, internals]
---

A common surprise coming from Python: in Mah, a closure captures its
outer variables **by reference**, not by value or by name lookup at call
time. Two calls to the same outer function get two independent captured
frames, and mutating a captured variable later is visible inside the
closure.

```mah
fn counter() {
    let n = 0
    fn() { n = n + 1; n }           # captures n by reference
}
let c = counter()
c()
print(c())                          # 2
```

## Why: heap frames, not a flat stack

Every function call gets a heap-allocated `Frame` (`slots` plus a
`static_parent` pointer), instead of reusing a shared, flat array of
stack slots. Frames are linked into a static chain that mirrors lexical
nesting, and every variable reference compiles down to a `(depth, slot)`
pair: how many static-parent hops to follow, then which slot in that
frame. A `Closure` value is just a code address plus a pointer to the
frame it was defined in (`defining_frame`) — so calling it later still
has a live link to the exact frame whose variables it closed over.

This is the classic static-chain-pointer / dynamic-chain-pointer
(SCP/DCP) technique from activation-record-based language
implementation, applied directly: the static chain is what closures walk
to find captured variables, and the dynamic chain (an explicit
return-address stack, separate from frames) is what makes calls and
returns work, including recursion, without ever reusing or aliasing a
frame.

## What this buys you

- A closure returned from a function keeps working correctly even after
  the function that created it has returned — its captured frame is a
  heap object, not a stack frame that got popped.
- Two closures created by two different calls to the same function never
  interfere with each other's captured state.
- The same heap-object shape (reference semantics, not copied on
  assignment) is used for structs and enums too, so `let p2 = p1` sharing
  the same object, or a closure and a struct both holding a live
  reference to the same mutable state, behave consistently across the
  whole language.

See [Functions & closures](/docs/functions-closures) for the full
picture, including default parameters, keyword arguments, and `defer`.
