---
title: Collections
order: 7
section: Language
---

## Vectors

```mah
let v = [10, 20, 30]
v.push(40)                      # add at the end; push_start(x) adds at the start
print(v[0], v.len())            # 10 4 (zero-indexed)
v[1] = 21                       # replace an existing item
print(v[-1], v[99])             # 40 none: -1 is the last item; a missing index reads as none
print(v.pop(), v.pop_start())   # 40 10 (none when the Vector is empty)
for let x, let i in v { print(i, x) }       # 0 21, then 1 30

let w = [0, 1, 2, 3, 4, 5]
print(w[1..3], w[2..], w[..=1], w[-2..])   # [1, 2] [2, 3, 4, 5] [0, 1] [4, 5]
print(w[3..100])                # [3, 4, 5]: a range past the end just stops there
```

- **Slicing**: indexing a Vector with a range (`v[a..b]`, `v[a..=b]`,
  `v[a..]`, `v[..b]`, `v[..=b]`) returns a **new** Vector of those items.
  Like Python, negative bounds count from the end and bounds past either
  end are clamped instead of raising. Bounds must be integers. You can't
  assign to a slice (`v[0..2] = ...` is a runtime error).
- Methods: `len()`, `push(x)`, `pop()`, `push_start(x)`, `pop_start()`,
  `copy(deep = false)`. `pop`/`pop_start` return the removed item, or
  `none` when empty. Negative indices count from the end (reading and
  writing). Reading an index outside `-len..len`, or a fractional one,
  gives `none`; **writing** there is a runtime error (use `push`). A
  non-Number index is a runtime error.
- There's no `insert`/`remove`/`sort`/`contains` yet.

## Maps

```mah
let m = ["a": 1, "b": 2]
m["c"] = 3                      # add or replace
print(m["a"], m["zzz"], m.len())            # 1 none 3
print(m.has("b"), m.remove("b"), m)         # true 2 [a: 1, c: 3]
for let key in m { print(key, m[key]) }     # a 1, then c 3 (keys, insertion order)
print(m.keys(), m.values())                 # [a, c] [1, 3]
for let e in m.entries() { print(e.key, e.value) }   # MapEntry { key, value }
let empty_vector = []
let empty_map = [:]
```

- Methods: `len()`, `has(k)`, `remove(k)` (the removed value, or `none`),
  `copy(deep = false)`, and `keys()`, `values()`, `entries()`, which each
  return a new Vector (a snapshot: changing the Map afterwards doesn't
  change it). `entries()` holds `MapEntry { key, value }` structs.
  Reading a missing key gives `none`, so `m[k]` can't tell a missing key
  from a stored `none` — use `m.has(k)`.
- Map keys must be Strings, Numbers, or Bools (anything else is a
  runtime error). `1` and `1.0` are the same key; `1`, `"1"`, and `true`
  are three different keys.

## Both are references

```mah
let grid = [[0, 0], [0, 0]]
let shallow = grid.copy()
let deep = grid.copy(deep: true)
grid[0][0] = 1
print(shallow[0][0], deep[0][0])   # 1 0
```

Vectors and Maps are references: `let w = v` then `w.push(1)` changes `v`
too. `copy()` is **shallow**: a Vector of Vectors gets a new outer Vector
sharing the same inner ones. `copy(deep: true)` also copies every
Vector, Map, struct, and enum value inside, all the way down (functions
and Promises are still shared).

Printing shows `[1, 2]` and `[a: 1, b: 2]`. A `[` on a new line starts a
new Vector/Map literal, not an index into the previous line's value:
write `x[0]` with the `[` right after `x`.

## Strings can be indexed and sliced too (read-only)

```mah
let s = "héllo"
print(s[1], s[-1], s[9], s[1..3], s[..=1], s[2..])   # é o none él hé llo
```

Indices count characters (Unicode code points), like `len()` and
`char_at`. See [Strings](/docs/strings).

Both Vectors and Maps (and Strings, Bytes and ranges) are **Iterable** — `for`,
`map`, `filter`, `skip`, `take`, and `reduce` all work on them; see
[Iterators & ranges](/docs/iterators-ranges).

## Bytes

`Bytes` is a growable sequence of bytes (whole Numbers 0 to 255) for
binary data: file contents, network packets, hashes. It's mutable and
passed by reference like a Vector. There's no literal; make one with
`"text".to_bytes()` (a String's UTF-8 encoding) or the
[`std:bytes`](/std/bytes) functions.

```mah
import bytes from "std:bytes"
let b = "hi".to_bytes()
print(b, b.len(), b[0], b[-1], b[9])       # Bytes[68 69] 2 104 105 none
b.push(33)
b[0] = 72
print(b.to_text(), b.to_hex(), b.to_base64())   # some(Hi!) 486921 SGkh
print(b[1..], b == bytes.from_hex("486921"))    # Bytes[69 21] true
print(b + bytes.new(2, 0), b.index_of(bytes.from_vector([105])))   # Bytes[48 69 21 00 00] some(1)
for let x in b { print(x) }                # 72, 105, 33
print(bytes.concat([b, b]).len())          # 6
```

- **Indexing** works like a Vector's: `b[i]` is the byte as a Number
  (`none` past the end, negative counts from the end), `b[a..b]` is a
  **new** Bytes, and `b[i] = n` sets an existing byte. Writing anything
  other than a whole Number from 0 to 255 is a runtime error.
- `==` compares **contents** (unlike Vectors), so
  `"hi".to_bytes() == "hi".to_bytes()` is `true`; a Bytes never equals a
  Vector. `a + b` is a new, concatenated Bytes. A Bytes prints as
  `Bytes[68 69]`, two hex digits per byte.
- **Methods**: `len()`, `push(n)`, `pop()` (`none` when empty),
  `extend(other)`, `copy()`, `to_vector()`, `to_text()` (`some(String)` if
  the bytes are valid UTF-8, else `none`), `to_text_lossy()` (invalid
  sequences become U+FFFD), `to_hex()`, `to_base64()`, and
  `index_of(needle)` (`some(i)` of the first occurrence of another Bytes).
- `Bytes` is a type annotation and a Type value
  (`reflect.type_of(b) == Bytes`).

Binary files ([`std:fs`](/std/fs)'s `read_bytes`/
`write_bytes`) and sockets (`std:socket`'s `send`/`recv`) read and write
Bytes.

## Indexing your own types

Implement `Index`/`IndexAssign` to support `x[k]`/`x[k] = v` on a struct
— see the example on the [Structs](/docs/structs) page.
