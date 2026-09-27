---
title: "v0.0.3: defer, async, traits, and portable bytecode"
date: 2026-09-24
description: "Zig-style defer, cooperative async with detach/.await, cross-file rename, Rust-style traits and impl, and the first portable .mahc bytecode format."
tags: [changelog]
version: "0.0.3"
---

Milestones M9 through M14: the language grows real control-flow and
data-modeling tools, and the compiler gains a durable output format.

## `defer`

Block-scoped, Zig-style, LIFO, runs on every exit path:

```mah
fn work() {
    print("open")
    defer print("close")        # runs when the enclosing block exits
    print("working")
}                               # prints open, working, close
```

## Cooperative async

```mah
fn slow(n) { sleep_async(10); n * 2 }
let a = detach slow(21)
let b = detach { sleep_async(5); "from a block" }
print(a.await, b.await)   # 42 from a block
```

Single-threaded and cooperative: `detach` starts a call or any other
expression as a task and hands back a `Promise`; `.await` waits for it.
The program exits once the main code is done *and* no detached work is
still pending.

## Traits, `impl`, method calls, `Printable`

```mah
struct Rect { w, h }
trait Shape {
    fn area(self)
    fn describe(self) { "a shape with area " + self.area() }
}
impl Shape for Rect {
    fn area(self) { self.w * self.h }
}
print(Rect { w: 2, h: 3 }.describe())   # a shape with area 6
```

Required and default methods, static functions (`Type.name(...)`),
trait-qualified calls for method-name collisions, and the built-in
`Printable` trait for custom `print`/`+` formatting all land here. A
follow-up patch (M13) extends `detach` to work on method calls too, and
lets `p.f()` call a closure stored in a plain field when no method `f`
exists.

## Cross-file rename

The LSP's rename now follows a name across every file that imports it,
and extends to struct/enum/variant/field names, including their uses in
type annotations.

## Portable `.mahc` bytecode

```sh
mah build ./examples/structs.mh     # -> structs.mahc
mah runc ./examples/structs.mahc    # run compiled bytecode
mah dis ./examples/structs.mahc     # show it disassembled
```

The compiler now has a real output artifact: a documented, portable
bytecode format (`docs/MAHC_FORMAT.md`) that a VM in any language could
implement — which is exactly what happens two releases later, in v0.1.0.
