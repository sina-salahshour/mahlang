---
title: "v0.0.4: projects, kwargs, iterators, and mah format"
date: 2026-09-25
description: "mah init and mah-project.toml, default parameters and keyword arguments, ranges/iterators, Vector and Map, for loops, match guards, type annotation syntax, and the formatter."
tags: [changelog]
version: "0.0.4"
---

This release (milestones M15 through M21b, plus a parser gap fix) is
where Mah stops being "one file at a time" and gains most of the
data-structure and ergonomics work that's still in daily use today.

## Projects

```sh
mah init my-app
cd my-app
mah run
mah build
```

`mah init` scaffolds `mah-project.toml`, `src/main.mh`, and docs written
for coding agents. `mah run`/`mah build` become project-aware, finding
the manifest from any subdirectory.

## Default parameters and keyword arguments

```mah
fn area(w, h = 1, scale = 1) { w * h * scale }
print(area(2, scale: 3))        # 6    keyword argument: `name: value`
print("a", "b", sep: ", ", end: "!\n")   # a, b!
```

## Ranges and lazy iterators

```mah
let odd_squares = (1..=5).map(fn(n) { n * n }).filter(fn(n) { n % 2 == 1 }).reduce()
print(odd_squares)   # [1, 9, 25]
```

`map`/`filter`/`skip`/`take`/`reduce`, range patterns in `match`, and the
new comparison operators `!`/`<=`/`>=` all land together.

## `for` loops as expressions

```mah
let big = for let n in 1.. { if n * n > 50 { break n } }   # 8
```

## Vector and Map, with `x[k]` indexing

```mah
let ages = ["ada": 36, "alan": 41]
for let name, let i in ages {
    print(i, name, ages[name])   # 0 ada 36, then 1 alan 41
}
```

## Match guards, and `detach` on any expression

```mah
fn sign(n) {
    match n {
        0 => { "zero" }
        x if x < 0 => { "negative" }
        _ => { "positive" }
    }
}
```

## Type annotation syntax, and `let` shadowing

```mah
fn add(a: Number, b: Number) -> Number { a + b }
let x = 1
let x = "one: " + x   # shadowing: a new `x`, allowed to read the old one
```

Only the type *names* are checked so far — see [Types](/docs/types) for
where the real checker is headed.

## `mah format`

```sh
mah format ./examples        # rewrite .mh files in the standard layout
mah format --check           # list files that would change, exit 1 if any
```

A whitespace-only formatter that verifies its own output before writing
anything — see [Formatter](/docs/formatter).
