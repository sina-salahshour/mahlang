---
title: "v0.0.2: a hand-written compiler and the v2 language core"
date: 2026-09-20
description: "The v2 rewrite: a hand-written lexer/parser, heap-allocated closures, structs, enums, pattern matching, expression blocks, a forgiving parser, and the first LSP."
tags: [changelog]
version: "0.0.2"
---

This release replaces the generated-parser prototype with a completely
hand-written compiler pipeline, and lands the core of the language as it
exists today (milestones M0 through M8 in `docs/V2_DESIGN.md`).

## Hand-written lexer, parser, and a real calling convention

The AST, lexer, and parser were rewritten by hand (`compiler/lexer.py`,
`compiler/parser.py`, `compiler/ast_nodes.py`), and the interpreter moved
from a flat shared array to heap-allocated `Frame`s linked by a static
chain pointer — the classic activation-record technique. That's what
makes this valid, with `next` returning independent counts across
independent calls:

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

Recursion, anonymous functions as values, and functions returning
functions all work from this point on.

## Structs and enums

`struct Name { field, ... }` and `Name { field: expr, ... }` construct
heap-allocated, reference-semantics instances:

```mah
struct Point { x, y }
fn add(a, b) {
    return Point { x: a.x + b.x, y: a.y + b.y }
}
print(add(Point { x: 1, y: 2 }, Point { x: 3, y: 4 }))   # Point { x: 4, y: 6 }
```

`enum Name { Variant, Variant2 { field } }` follows immediately after,
sharing the same field storage as structs — and the built-in `Option`
type (`none`/`some(x)`) is unified into the same machinery rather than
staying a bespoke placeholder.

## Pattern matching and expression blocks

```mah
fn area(s) {
    match s {
        Shape.Circle { r } => { 3 * r * r }
        Shape.Empty => { 0 }
    }
}
```

`if`/`match`/bare `{ }` blocks all became expressions, so a function body
is just a block whose trailing expression is its implicit return value.

## A forgiving parser, and the first LSP

The parser now recovers from a syntax error instead of aborting the whole
parse, producing an error node in place of what it couldn't read and
continuing — so a language server can report every mistake in a file in
one pass. `mah lsp` and cross-file rename landed on top of that, along
with tree-sitter/tooling sync for editor syntax highlighting.
