---
title: Structs
order: 4
section: Language
---

```mah
struct Point { x, y }                       # fields have no types
let p = Point { x: 1, y: 2 }                # every field, exactly once
                                             # (no trailing commas in field lists)
p.x = 10                                    # fields are mutable
print(p)                                    # Point { x: 10, y: 2 }
```

Structs are **reference types**: assigning a struct copies the reference,
not the value, so mutating it through one variable is visible through
any alias (the same as closures' captured frames and Vectors/Maps).

```mah
struct Line { start, end }
let p3 = Point { x: 4, y: 6 }
let l = Line { start: Point { x: 0, y: 0 }, end: p3 }
l.end.x = 100
print(l.end.x)   # 100
print(p3.x)      # 100 -- same object
```

## The `if`/`while` struct-literal ambiguity

A bare struct literal can't appear directly in an `if`/`elif`/`while`
condition or after `for ... in`, because `if x { ... }` would be
ambiguous between `x` being a plain identifier and `x { ... }` being a
struct literal starting the condition. Parenthesize to disambiguate:

```mah
if (Point { x: 1, y: 2 }.x > 0) { print("positive") }
for let v in (Countdown { from: 3 }) { print(v) }
```

## Field access and validation

- A struct literal always names its type explicitly, so **missing
  fields, extra fields, and duplicate fields in the literal are
  compile-time errors**, naming the specific field(s).
- Field *access* through a variable (`p.x`) is checked at **runtime**,
  since there's no static type system yet that knows what a given
  expression's value will be — reading or writing an unknown field on a
  struct value is a runtime error.

## Custom printing: `Printable`

```mah
struct Point { x, y }
let p = Point { x: 10, y: 2 }
impl Printable for Point {
    fn to_string(self) { "(" + self.x + ", " + self.y + ")" }
}
print(p)            # (10, 2), also used by "text " + p
```

Without an `impl Printable`, values print structurally: `Point { x: 1, y: 2 }`.
`to_string` must return a String. See [Traits](/docs/traits) for methods
and `impl` blocks in general.

## Indexing your own types

Implement the built-in `Index`/`IndexAssign` traits to support `x[k]` and
`x[k] = v` on your own struct:

```mah
struct Grid { width, cells }
impl Index for Grid {
    fn index(self, p) { self.cells[p.y * self.width + p.x] }
}
impl IndexAssign for Grid {
    fn index_assign(self, p, value) { self.cells[p.y * self.width + p.x] = value }
}
struct Point { x, y }
let g = Grid { width: 2, cells: [0, 0, 0, 0] }
g[Point { x: 1, y: 1 }] = 9
print(g[Point { x: 1, y: 1 }])  # 9
```
