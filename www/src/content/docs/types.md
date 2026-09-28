---
title: Types
order: 12
section: Language
---

Declarations can carry optional type annotations, and a static type
checker infers the rest: most code needs no annotations at all.
Annotations never change how a program runs. They're erased before
codegen, so the `.mahc` bytecode and every program's behavior are the same
whatever the checker concludes. An unknown type name or a wrong number of
`<...>` arguments is always a compile error.

## Syntax

```mah
fn add(a: Number, b: Number = 1) -> Number { a + b }
let label: String = "total"
let scale: fn(Number) -> Number = fn(n: Number) -> Number { n * 2 }

struct Pair<A, B> { left: A, right: B }        # generic struct
enum Tree<T> { Leaf, Node { value: T } }       # generic enum
fn first<T>(v: Vector<T>) -> T { v[0] }        # generic function
fn show<T: Printable>(x: T) -> String { x.to_string() }   # bound: T implements Printable

trait Container<T> {
    fn get(self, i: Number) -> T
}
impl<T> Container<T> for Vector<T> {
    fn get(self, i) { self[i] }
}
for let v: Number, let i: Number in [10, 20] { print(i, v) }
print(add(label.len()), scale(4), first([5]), show(Pair { left: 1, right: "x" }.left))
```

- Types: `Number`, `String`, `Bool`, `Vector<T>`, `Map<K, V>`, `Option<T>`,
  `Promise<T>`, your structs/enums/traits (with their `<...>` arguments),
  a type parameter, `fn(A, B) -> R` (no `->` means it returns `none`),
  `Self` (inside `trait`/`impl`), `None` (the type of `none`), `Never`,
  and `Unknown` (anything).
- `self` is never annotated. Type arguments are never written at a call
  (`first(v)`, not `first<Number>(v)`).
- `Function` is not a type in annotations: write `fn(...) -> ...`.
- In `impl` headers the `<...>` can be left off (`impl Iterable for P`).

## The checker

```sh
mah check                   # the project's level, from mah-project.toml
mah check src/main.mh --level strict
```

How strict it is is set per project, in `mah-project.toml`:

```toml
[types]
check = "loose"
```

| level | effect |
|---|---|
| `loose` (default) | type mismatches are editor warnings; `mah run`/`mah build` don't check at all |
| `strict` | type mismatches are compile errors, in the editor and for `mah run`/`mah build` |
| `explicit` | `strict`, plus every declaration whose type can't be inferred must be annotated |

A file outside a project is `loose`. The editor (the LSP) shows the
diagnostics as you type. The checker also works out what every function
can throw and reports errors nothing catches, at the same levels: see
[Errors](/docs/errors).

### What it infers

```mah
fn add(a, b) { a + b }          # fn(Number, Number) -> Number
fn id(x) { x }                  # fn<T>(T) -> T: generic, inferred
let n = id(1)                   # Number
let s = id("s")                 # String
struct P { x, y }               # field types come from how P is built
let p = P { x: 1, y: 2 }        # so p.x is a Number
let best = none                 # open until the first real value...
best = 5                        # ...makes it a Number
print(add(n, p.x), s, best)
```

The checker is stricter than the runtime in a few places:

- A variable keeps one type: `let x = 1` then `x = "s"` is an error
  (shadow it with a new `let x = ...` instead).
- `[...]` and `[k: v]` literals hold one element type, and range bounds
  are Numbers.
- `none` fits any type.
- `+`, `*`, and comparisons on values whose type isn't known yet default
  to `Number`, so annotate `a: String` if `fn f(a, b) { a + b }` is meant
  to concatenate.
- An `if`/`match` whose branches have different types is fine, but its
  value is `Unknown`.
- `Unknown` (written explicitly) turns checking off for a value.

### Not checked yet

This is the checker's first version. Method calls (`v.len()`,
`"abc".map(...)`) give `Unknown` (their arguments are still checked), and
trait-typed values and bounds aren't checked. Those come next, along with
a fully typed standard library, so that `"abc".map(fn(c) { ... })` knows
`c` is a `String`, and hover/completion driven by inferred types.
