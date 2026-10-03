---
title: Functions & closures
order: 3
section: Language
---

```mah
fn add(a, b) { a + b }             # tail expression is the return value
fn early(n) {
    if n < 0 { return "negative" }
    return                          # bare return gives none
}
let twice = fn(f, x) { f(f(x)) }   # anonymous function value
print(twice(fn(v) { v * 2 }, 3))   # 12
fn adder(a) { fn(b) { a + b } }   # functions can return functions...
print(adder(1)(2))                 # ...and any value can be called: 3
print((fn(x) { x + 1 })(5))        # 6

fn counter() {
    let n = 0
    fn() { n = n + 1; n }           # captures n by reference
}
let c = counter()
c()
print(c())                          # 2
```

- Recursion works.
- A call's `(` must be on the same line as what it calls (like an
  index's `[`): a line starting with `(` is a new statement. A statement
  that *starts* with an anonymous `fn` is a function value, not a call,
  so write `(fn() { ... })()` to call one immediately.
- A top-level `fn` can only call functions **declared above it** (except
  inside `impl` blocks — see [Traits](/docs/traits)).

## Closures capture by reference

Every function call gets a heap-allocated `Frame` linked to its lexically
enclosing frame by a static chain pointer. That's what lets a closure
capture an outer variable **by reference, like JavaScript** (not by
value/name like Python): a closure that outlives the call that created it
still sees later mutations of the variables it captured, and two calls to
the same outer function produce two independent captured frames.

```mah
let counter = fn() {
    let count = 0
    return fn() {
        count = count + 1
        return count
    }
}
let next = counter()
print(next())   # 1
print(next())   # 2
```

## Default values and keyword arguments

```mah
fn area(w, h = 1, scale = 1) { w * h * scale }
print(area(2))                  # 2
print(area(2, 3))               # 6
print(area(2, scale: 3))        # 6    keyword argument: `name: value`
print(area(h: 5, w: 2))         # 10   any parameter can be passed by keyword

fn greet(name, greeting = "Hello, " + name) { greeting }   # defaults can use earlier parameters
print(greet("mah"))             # Hello, mah

print("a", "b", sep: ", ", end: "!\n")   # a, b!
print("no newline", end: "")
```

- Parameters with defaults must come after the ones without.
- A default is evaluated **on every call** that doesn't pass that
  argument, so `fn f(b = B { n: 0 })` gets a fresh struct each time.
- At a call site, positional arguments come first, then `name: value`
  pairs. Passing an unknown keyword, the same parameter twice, too many
  arguments, or leaving out a parameter without a default is a runtime
  error.
- Works the same for methods (`r.scaled(k: 3)`, `Rect.new(w: 2)`) and
  detached calls (`detach fetch(url: u)`). `self` can't have a default,
  and a trait's required (bodyless) methods can't declare defaults (put
  them on the impl).

## Spread calls

`...xs` in an argument list expands a Vector into positional arguments,
and `**m` expands a Map with String keys into keyword arguments:

```mah
fn f(a, b = 2, c = 3) { a + b + c }
print(f(...[1, 10]))                 # 14
print(f(1, **["c": 100]))            # 103
print(f(...[1], b: 5, **["c": 0]))   # 6
print("a-b".split(...["-"]))         # [a, b]   method calls too
```

Any number of each, mixed with ordinary arguments; positional ones come
before keyword ones. A keyword given twice (by name, or through a Map), a
non-Vector after `...` and a non-Map after `**` are
`RuntimeError.ArgumentError`s. `**` is still the exponent everywhere else.
`detach f(...xs)` isn't allowed yet.

## `defer`

```mah
fn work() {
    print("open")
    defer print("close")        # runs when the enclosing block exits
    print("working")
}                               # prints open, working, close
```

Deferred statements run when their **block** exits (normally or via
`return`/`break`/`continue`), last-deferred first — Zig-style,
block-scoped, LIFO:

```mah
struct Resource { name }
fn open(name) {
    print("opening " + name)
    return Resource { name: name }
}
fn close(r) {
    print("closing " + r.name)
}
fn process(name) {
    let r = open(name)
    defer close(r)          # runs whether this returns early or falls through
    if r.name == "bad" {
        return
    }
    print("using " + r.name)
}
process("alpha")   # opening alpha / using alpha / closing alpha
process("bad")     # opening bad / closing bad -- close() still ran
```

A function can also carry [decorators](/docs/decorators): values attached
with `@name(...)` above it or before a parameter, read back with
`std:reflect`.
