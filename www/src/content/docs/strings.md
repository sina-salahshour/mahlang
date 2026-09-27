---
title: Strings
order: 9
section: Language
---

Strings are immutable, UTF-8 text values.

```mah
let greeting = "Hello"
let target = "World"
let message = greeting + " " + target + "!"
print(message)                       # Hello World!

if greeting == "Hello" { print("greeting is indeed Hello") }
if greeting != target { print("greeting and target are different") }

let escaped = "First line\nSecond line"
print(escaped)
```

- Literals: `"hi\n"`, with escapes `\n`, `\t`, `\"`, `\\`.
- `+` concatenates: `"n = " + 5` is `"n = 5"` (the non-String side is
  converted to text automatically). `"ab" * 3` is `"ababab"`.
- `==`/`!=` compare by value.
- `s.len()` — length in characters (Unicode code points, not bytes).
- `s.char_at(i)` — the character at index `i`, as a one-character String.

## Indexing and slicing (read-only)

```mah
let s = "héllo"
print(s[1], s[-1], s[9], s[1..3], s[..=1], s[2..])   # é o none él hé llo
```

- `s[0]` is the first character, `s[-1]` the last, `s[1..3]` a
  substring, and an index past the end gives `none`.
  Indices count characters, like `len()` and `char_at`.
- Strings can't be assigned into (`s[0] = "x"` is a runtime error) — a
  String is immutable. Build a new one with `+` instead.

## Iterating a String

Strings are `Iterable` (see [Iterators & ranges](/docs/iterators-ranges)),
yielding one character at a time:

```mah
for let c, let idx in "ab" { print(idx, c) }   # 0 a, then 1 b
print("mah".map(fn(c) { c + "!" }).reduce(fn(a, b) { a + b }))   # m!a!h!
```

## What's not there yet

No `split`, `replace`, `trim`, `to_upper`/`to_lower`, or other string
methods beyond `len`/`char_at`/indexing/slicing — those are planned but
not implemented.

## Building strings in a loop

```mah
let counter = 0
let pattern = ""
while counter < 5 {
    pattern = pattern + "*"
    counter = counter + 1
}
print(pattern)   # *****
```
