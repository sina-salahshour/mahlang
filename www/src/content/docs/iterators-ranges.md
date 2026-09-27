---
title: Iterators & ranges
order: 8
section: Language
---

## Ranges

```mah
let r = 5..10                   # Range: 5, 6, 7, 8, 9
let i = 5..=10                  # Range including 10
let from = 1..                  # FromRange: 1, 2, 3, ... forever
let upto = ..10                 # ToRange: only for `match` (it has no start)
print(r)                        # 5..10
print(r.start, r.end)           # 5 10
```

- Only ranges of Numbers can be iterated (they count up by 1). String
  ranges like `"a"..="z"` work in `match` patterns (see
  [Enums & pattern matching](/docs/enums-pattern-matching)), but
  iterating one is a runtime error.
- A range's end must be on the same line as its `..`: `let f = 1..` at
  the end of a line is an open-ended `FromRange`, and the next line is a
  new statement.
- Programs can't declare their own types or traits named `Range`,
  `FromRange`, `ToRange`, `Iterable`, `Iterator`, or the adapter types
  (`Mapped`, `Filtered`, `Skipped`, `Taken`, and their `...Iterator`
  types), `VectorIterator`, or `MapEntry`.

## Lazy adapters: `map`, `filter`, `skip`, `take`, `reduce`

```mah
let total = (1..=5).reduce(fn(acc, x) { acc + x })            # 15
let squares = (1..).map(fn(x) { x * x }).take(3)               # lazy: nothing runs yet
print(squares.reduce(fn(acc, x) { acc + ", " + x }))           # 1, 4, 9
let odd_positions = (1..10).filter(fn(v, i) { i % 2 == 0 })    # the index is optional
print(odd_positions.skip(1).reduce(fn(acc, x) { acc + x }, 0)) # 3 + 5 + 7 + 9 = 24
print("mah".map(fn(c) { c + "!" }).reduce(fn(a, b) { a + b }))   # m!a!h!
```

**Parenthesize a range before calling a method on it**: `(1..10).map(f)`.
Without the parentheses, `1..10.map(f)` means `1..(10.map(f))`.

Every **`Iterable`** (ranges except `ToRange`, Strings, Vectors, Maps,
and your own types that implement it) has these methods, all lazy
except `reduce`:

- `map(f)`: `f(value)` or `f(value, index)` gives each new item.
- `filter(f)`: keeps items where `f(value)` or `f(value, index)` is
  truthy.
- `skip(n)`, `take(n)`: skip the first `n` items, or stop after `n`.
- `reduce(f, initial)`: like JavaScript: `f(acc, value)` or `f(acc,
  value, index)`. Without `initial` the first item starts the
  accumulator, and an empty Iterable gives `none`.
- `reduce()` with no arguments collects the items into a new Vector:
  `(1..4).reduce()` is `[1, 2, 3]`, `"ab".reduce()` is `[a, b]`. There's
  no separate `collect`.

An Iterable can be iterated again (each pass starts over). Nothing
consumes an infinite `1..` except `take`, so `(1..).reduce(f)` never
ends.

## Your own iterable types

```mah
struct Countdown { from }
struct CountdownIter { n }
impl Iterable for Countdown {
    fn iter(self) { CountdownIter { n: self.from } }
}
impl Iterator for CountdownIter {
    fn next(self) {
        if self.n > 0 { let v = self.n; self.n = self.n - 1; some(v) } else { none }
    }
}
print(Countdown { from: 3 }.map(fn(x) { x * 10 }).reduce(fn(a, b) { a + " " + b }))   # 30 20 10
let it = (1..3).iter()          # or pull items by hand
print(it.next(), it.next(), it.next())                         # some(1) some(2) none
```

## `for` over any Iterable

```mah
for let n, let i in (1..=2).map(fn(x) { x * 10 }) {
    print(i, n)                 # 0 10, then 1 20
}
```

See [Syntax basics](/docs/syntax-basics) for `for`/`while` as
expressions, `break value`, and the index-binding form.
