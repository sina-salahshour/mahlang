---
title: Enums & pattern matching
order: 5
section: Language
---

## Enums

```mah
enum Shape {
    Circle { r },
    Rect { w, h },
    Empty                                   # unit variant (no trailing comma!)
}
let s = Shape.Circle { r: 2 }
let e = Shape.Empty
print(s.r)                                  # 2 (variant fields are fields)
```

Variants are either **unit** (no payload, like `Empty`) or **struct-shaped**
(named fields, like `Circle { r }`). Enum instances are reference types,
exactly like structs, and share the same `getfield`/`setfield` machinery.

The built-in **`Option`** type (`none`/`some(x)`) is itself just an enum:
`none` is `Option.none`, `some(x)` is `Option.some { value: x }`.

## Pattern matching

```mah
fn area(s) {
    match s {
        Shape.Circle { r } => { 3.14 * r * r }
        Shape.Rect { w: width, h } => { width * h }   # rename with `field: pattern`
        Shape.Empty => { 0 }
    }
}
match value {
    0 => { "zero" }                 # literal (Number, String, Bool)
    some(x) => { "got " + x }
    none => { "nothing" }
    Point { x, y } => { x + y }     # struct pattern: list every field
    other => { other }              # binds anything
    _ => { "ignored" }              # wildcard
}
```

- Every arm body is a `{ }` block. Arms are tried top to bottom; if none
  matches it's a runtime error.
- Struct/enum patterns must list **every** field of that struct/variant.

### Range patterns

Match a Number (or String) in a range; bounds must be literals (negative
numbers are fine). A value of another type just doesn't match.

```mah
fn describe(x) {
    match x {
        1..10 => { "between one and 10" }   # 1 <= x < 10
        10..=15 => { "10 to 15" }           # inclusive end
        ..1 => { "less than one" }          # x < 1
        15.. => { "more than 15" }          # x >= 15
    }
}
print(describe(10))                         # 10 to 15
```

### Guards

`pattern if cond => { ... }` only matches when the pattern does and
`cond` (which can use the pattern's bindings) is truthy; otherwise the
next arm is tried.

```mah
fn sign(n) {
    match n {
        0 => { "zero" }
        x if x < 0 => { "negative" }
        _ => { "positive" }
    }
}
```

## `Option` in practice

```mah
fn half(n) {
    if n % 2 == 0 { return some(n // 2) }
    return none
}
match half(7) {
    some(v) => { print(v) }
    none => { print("no half for an odd number") }
}
```
