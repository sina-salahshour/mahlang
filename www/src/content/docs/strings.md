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

## String methods

Every method returns a new String (or Vector, Bool, ...): Strings never
change. Positions count characters (Unicode code points), and
"whitespace" means any Unicode whitespace.

```mah
print("a,b,,c".split(","), "  one  two ".split(), "k=v=w".split("=", 1))   # [a, b, , c] [one, two] [k, v=w]
print("|" + "  hi ".trim() + "|", "7".pad_start(3, "0"), "ab".repeat(2))  # |hi| 007 abab
print("a-b-c".replace("-", "+"), "a-b-c".replace_all("-", "+"))           # a+b-c a+b+c
print("hello".starts_with("he"), "hello".contains("ll"), "hello".index_of("l"))  # true true some(2)
print("Straße".to_upper(), "A\nB\n".lines(), ["x", 1].join(", "))           # STRASSE [A, B] x, 1
```

| Method | |
|---|---|
| `split(sep = none, limit = none)` | the pieces between each `sep`; with no `sep`, the runs of non-whitespace. `limit` caps the number of splits |
| `trim()`, `trim_start()`, `trim_end()` | without leading and/or trailing whitespace |
| `pad_start(width, fill = " ")`, `pad_end(...)` | padded with `fill` to `width` characters |
| `replace(from, to)`, `replace_all(from, to)` | the first / every occurrence of `from` replaced |
| `starts_with(s)`, `ends_with(s)`, `contains(s)` | `true` or `false` |
| `index_of(s)` | `some(position)` of the first occurrence, or `none` |
| `repeat(n)` | the String `n` times |
| `to_upper()`, `to_lower()` | full Unicode case mapping |
| `lines()` | the lines, for `\n` or `\r\n` endings |
| `to_number()`, `parse_number()` | the Number the String spells (see below) |
| `len()`, `char_at(i)` | the length, and one character |

A Vector joins its items into a String with `join(sep = "")`, using each
item's `to_string`.

## Numbers from text

`to_number()` reads a number out of a String, surrounding whitespace
allowed. Anything else throws a `NumberParseError`, whose `text` is the
String. The [checker](/docs/errors) knows that, and warns when nothing
catches it. `parse_number()` is the same but returns `none` instead of
throwing.

```mah
print("42".to_number() + 1, " -1.5e2 ".to_number())   # 43 -150
print(try "abc".to_number() else 0)                    # 0
print("abc".parse_number())                            # none
```

`index_of` returns an Option, and Options have a few helpers:
`unwrap()` (the value, and an error for `none`), `unwrap_or(default)`,
`is_some()` and `is_none()`.

```mah
let at = "hello".index_of("l")
print(at.unwrap(), "hello".index_of("z").unwrap_or(0 - 1))   # 2 -1
```

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
