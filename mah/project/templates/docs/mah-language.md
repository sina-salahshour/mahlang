# Mah language reference (for humans and LLMs)

Mah is a small, dynamically typed language with closures, structs, enums,
pattern matching, Rust-style traits, block-scoped `defer`, and cooperative
async. Source files end in `.mh`. This file is a complete reference to
**what exists today** — if a feature isn't described here, assume Mah
doesn't have it (see "Not available" at the end) rather than guessing from
another language.

## Program structure

- A program is a sequence of statements, run top to bottom. There's no
  `main` function: the entry file's top-level code *is* the program.
- Comments start with `#` and run to end of line. A run of full-line comments
  starting with `##` directly above a `fn`, `struct`, `enum`, `trait`, method,
  field, variant or parameter is its **documentation** (see "Type values and
  reflection"); it's still just a comment to the program.
- Newlines separate statements; `;` is optional between most statements and
  **required** in one case: an expression statement that isn't a call, a
  block (`if`/`match`/`{}`), or an assignment must be followed by `;` when
  more code follows on the same block. When in doubt, add `;`.
- Blocks are `{ ... }`. The **last expression without a trailing `;`** is the
  block's value (its "tail"); a block with no tail has the value `none`.
- `let` may reuse a name in the same scope: it declares a **new** variable
  (shadowing), and its value can still use the old one: `let x = 1` then
  `let x = "one: " + x`. Closures that captured the old `x` keep it. `fn`
  names, parameters, and pattern bindings can't be declared twice in one
  scope.

```mah
let x = 1            # declare
x = x + 1            # assign (the variable must already exist)
let y = {
    let t = x * 2
    t + 1            # tail: the block's value is 5
}
```

## Values and types

| type | literals / how you get one | notes |
|---|---|---|
| `Number` | `42`, `3.14`, `-7` | decimal numbers (28 significant digits); `10 / 4` is `2.5` |
| `String` | `"hi\n"` | escapes `\n \t \" \\`; immutable; `s.len()`, `s[i]`, `s[a..b]` (see Vectors and Maps), `split`/`trim`/`replace`/... (see String methods); iterable, see Iterators |
| `Bool` | `true`, `false` | |
| `Option` | `none`, `some(x)` | Mah's null. `none` is falsy; `some(x)` is always truthy |
| `Function` | `fn(a) { a }` | first-class closures, captured by reference |
| struct / enum | user-declared | reference semantics (assigning copies the reference) |
| `Promise` | from `detach` / `sleep_async` | see Async |
| `Vector` | `[1, 2, 3]`, `[]` | growable zero-indexed list, by reference; see Vectors and Maps |
| `Map` | `["a": 1, "b": 2]`, `[:]` | String/Number/Bool keys, insertion-ordered, by reference; see Vectors and Maps |
| `Bytes` | `"hi".to_bytes()`, `bytes.new(n)` (no literal) | a growable sequence of bytes (Numbers 0 to 255), mutable and by reference; see Bytes |
| `Range`, `FromRange`, `ToRange` | `5..10`, `1..`, `..10` | built-in structs, see Ranges |
| `Type` | `Number`, `User`, `Vector`, `Bytes` (a bare type name) | a type as a value; `==` compares them, `print` shows the name; see Type values and reflection |

**Truthiness**: `false`, `none`, `0`, and `""` are falsy; everything else is
truthy.

## Operators

Precedence, loosest to tightest. Note the unusual rules: ranges bind
loosest of all, `&`/`|` share one level, and `%` shares a level with
`+`/`-`.

| level | operators | associativity |
|---|---|---|
| 0 | `..` `..=` (ranges) | not chainable: `1..n + 1` is `1..(n + 1)` |
| 1 | `&` (and), `\|` (or) | left, same level: `a \| b & c` is `(a \| b) & c` |
| 2 | `==` `!=` `<` `>` `<=` `>=` | left |
| 3 | `+` `-` `%` | left: `1 + 6 % 4` is `(1 + 6) % 4` = `3` |
| 4 | `*` `/` `//` | left |
| 5 | unary `-`, `!` (not) | `!a == b` is `(!a) == b` |
| 6 | `**` | right: `-2 ** 2` is `-(2 ** 2)` = `-4` |
| 7 | `x.field`, `x.method()`, `x[key]` | postfix, left to right: `m["k"].len()` |

- `!x` is `true` when `x` is falsy (see Truthiness) and `false` otherwise.
  There is no `&&` or `||`: use `&` and `|`.
- `<`, `>`, `<=`, `>=` compare two Numbers or two Strings; anything else is
  a runtime error.
- `&`/`|` evaluate **both** sides (no short-circuit) and return a Bool.
- `+` with a String on either side concatenates, converting the other side
  to text: `"n = " + 5` is `"n = 5"`. `"ab" * 3` is `"ababab"`.
- `//` truncates toward zero; `%` takes the sign of the left operand.
- `==` compares Numbers/Strings/Bools/`none` by value and everything else
  (structs, enums, `some(..)`, functions, Vectors, Maps) by identity:
  `[1] == [1]` is `false`. **Bytes** are the exception: they compare by
  contents. Different types are never equal (`true == 1` is `false`).
- `a + b` on two Bytes is a new Bytes, the concatenation (see Bytes).
- Parenthesize whenever you're unsure.

## Built-ins

| call | meaning |
|---|---|
| `print(a, b, ..., sep: " ", end: "\n")` | prints the arguments separated by `sep` (default: a space), then `end` (default: a newline). `print()` prints just a newline |
| `input(prompt = "")` | prints `prompt` (no newline), reads one line from stdin and returns it as a String without the newline; throws `EndOfInput` at the end of the input. Use `.to_number()` for a Number. `detach input()` gives a Promise instead (see Async) |
| `sin(x)`, `cos(x)` | radians (ordinary names: a `fn sin` of your own takes over) |
| `sleep_async(ms)` | pauses (see Async) |

## Control flow

```mah
let n = 7
if n > 10 { print("big") } elif n > 5 { print("medium") } else { print("small") }

let i = 0
while i < 3 {
    i = i + 1
    if i == 2 { continue }
    print(i)
}

for let v in 1..4 { print(v) }              # 1 2 3 (one per line)
for let c, let idx in "ab" { print(idx, c) } # 0 a, then 1 b

let x = while true { break 10 }             # loops are expressions: 10
let big = for let n in 1.. { if n * n > 50 { break n } }   # 8
let none_found = for let n in 1..3 { if n > 5 { break n } }  # none
```

- `if`/`elif`/`else`, `match`, `while`, and `for` are all **expressions**:
  `let s = if ok { "y" } else { "n" }`. An `if` with no `else` whose
  condition is false yields `none`.
- `for let value in iterable { }` loops over any Iterable (ranges, Strings,
  `map`/`filter`/... results, your own types -- see "Ranges and
  iterators"). `for let value, let index in ... { }` also binds the
  0-based index. Both need `let`; `for x in ...` is a syntax error. The
  bindings only exist inside the loop body.
- `break`/`continue` apply to the innermost `while`/`for`. `break value`
  ends the loop and makes `value` the loop's value; a loop that ends any
  other way (condition false, iterable exhausted, bare `break`) has the
  value `none`. The value must start on the same line as `break`.
- Conditions need no parentheses. A struct literal can't appear bare in an
  `if`/`while`/`match` head or after `for ... in`; wrap it in parentheses:
  `if (Point { x: 1, y: 2 }.x > 0) { }`, `for let v in (Countdown { from: 3 }) { }`.

## Functions and closures

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
- A call's `(` must be on the same line as what it calls (like an index's
  `[`): a line starting with `(` is a new statement. A statement that
  *starts* with an anonymous `fn` is a function value, not a call, so write
  `(fn() { ... })()` to call one immediately.

### Default values and keyword arguments

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
- A default is evaluated **on every call** that doesn't pass that argument,
  so `fn f(b = B { n: 0 })` gets a fresh struct each time.
- At a call site, positional arguments come first, then `name: value`
  pairs. Passing an unknown keyword, the same parameter twice, too many
  arguments, or leaving out a parameter without a default is a runtime
  error.
- Works the same for methods (`r.scaled(k: 3)`, `Rect.new(w: 2)`) and
  detached calls (`detach fetch(url: u)`). `self` can't have a default, and
  a trait's required (bodyless) methods can't declare defaults (put them on
  the impl).
- A top-level `fn` can only call functions **declared above it** (except
  inside `impl` blocks, see Traits).

### Spread calls

`...xs` in an argument list expands a Vector into positional arguments, and
`**m` expands a Map with String keys into keyword arguments:

```mah
fn f(a, b = 2, c = 3) { a + b + c }
let args = [1, 10]
print(f(...args))                    # 14
print(f(1, **["c": 100]))            # 103
print(f(...[1], b: 5, **["c": 0]))   # 6    any number of each, mixed with ordinary arguments
print("a-b".split(...["-"]))         # [a, b]   method calls take them too
```

- Positional arguments (plain or `...`) come before keyword ones (`name: v`
  or `**`). Items are evaluated left to right.
- A keyword given twice (by name, or through a Map), a non-Vector after
  `...`, and a non-Map (or a non-String key) after `**` are
  `RuntimeError.ArgumentError`s.
- `**` is still the exponent operator anywhere else (`2 ** 3`). `print` and
  `sleep_async` take no spread arguments, and `detach f(...xs)` isn't allowed
  yet. In `Trait.m(x, ...)`/`Type.m(x, ...)` the receiver `x` can't be a spread.

### Rest parameters

`...name` collects the extra positional arguments into a new Vector and
`**name` the keyword arguments that match no parameter into a new Map (in the
order given). They go last, `...` before `**`, with no default; either or
both may be there:

```mah
fn show(label, ...items, **options) {
    print(label, items, options)
}
show("a")                          # a [] [:]
show("b", 1, 2, 3, color: "red")   # b [1, 2, 3] [color: red]
show("c", ...[4], **["size": 9])   # c [4] [size: 9]   spread calls bind them too

fn log_all(prefix: String, ...items: Vector<Number>, **opts: Map<String, String>) { }
```

- The annotation is the collection's type (`Vector<T>`, `Map<String, T>`);
  without one they're `Vector<Unknown>` and `Map<String, Unknown>`.
- Without `**`, an unknown keyword is still an `ArgumentError`; without `...`,
  too many positional arguments still is. A keyword that names an ordinary
  parameter binds it as usual and never lands in the Map.
- They work on top-level and nested `fn`s, closures and methods (not
  `extern fn`). `f.arity()` counts them, and `reflect.signature(f)` lists
  them as `rest`/`kwrest` (an `Option` of a `Param`), apart from `params`.
- `**` is still the exponent operator anywhere but at the start of a
  parameter. In a method call the receiver binds to the first parameter, so
  in `fn(...args, **kw)` it is `args[0]`.

## Structs and enums

```mah
struct Point { x, y }                       # fields have no types
let p = Point { x: 1, y: 2 }                # every field, exactly once
                                            # (no trailing commas in field
                                            # or variant lists)
p.x = 10                                    # fields are mutable
print(p)                                    # Point { x: 10, y: 2 }

enum Shape {
    Circle { r },
    Rect { w, h },
    Empty                                   # unit variant (no trailing comma!)
}
let s = Shape.Circle { r: 2 }
let e = Shape.Empty
print(s.r)                                  # 2 (variant fields are fields)
```

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

- **Range patterns** match a Number (or String) in a range; bounds must be
  literals (negative numbers are fine). A value of another type just
  doesn't match.

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

- **Guards**: `pattern if cond => { ... }` only matches when the pattern
  does and `cond` (which can use the pattern's bindings) is truthy;
  otherwise the next arm is tried.

```mah
fn sign(n) {
    match n {
        0 => { "zero" }
        x if x < 0 => { "negative" }
        _ => { "positive" }
    }
}
```

- Every arm body is a `{ }` block. Arms are tried top to bottom; if none
  matches it's a runtime error.
- Struct/enum patterns must list **every** field of that struct/variant.

## Ranges and iterators

```mah
let r = 5..10                   # Range: 5, 6, 7, 8, 9
let i = 5..=10                  # Range including 10
let from = 1..                  # FromRange: 1, 2, 3, ... forever
let upto = ..10                 # ToRange: only for `match` (it has no start)
print(r)                        # 5..10
print(r.start, r.end)           # 5 10

let total = (1..=5).reduce(fn(acc, x) { acc + x })            # 15
let squares = (1..).map(fn(x) { x * x }).take(3)               # lazy: nothing runs yet
print(squares.reduce(fn(acc, x) { acc + ", " + x }))           # 1, 4, 9
let odd_positions = (1..10).filter(fn(v, i) { i % 2 == 0 })    # the index is optional
print(odd_positions.skip(1).reduce(fn(acc, x) { acc + x }, 0)) # 3 + 5 + 7 + 9 = 24
print("mah".map(fn(c) { c + "!" }).reduce(fn(a, b) { a + b }))   # m!a!h!
```

- **Parenthesize a range before calling a method on it**: `(1..10).map(f)`.
  Without the parentheses, `1..10.map(f)` means `1..(10.map(f))`.
- Only ranges of Numbers can be iterated (they count up by 1). String
  ranges like `"a"..="z"` work in `match` patterns, but iterating one is a
  runtime error.
- Programs that use ranges or iterators can't declare their own types or
  traits named `Range`, `FromRange`, `ToRange`, `Iterable`, `Iterator`, or
  the adapter types (`Mapped`, `Filtered`, `Skipped`, `Taken`, and their
  `...Iterator` types), `VectorIterator`, or `MapEntry`.
- A range's end must be on the same line as its `..`: `let f = 1..` at the
  end of a line is an open-ended `FromRange`, and the next line is a new
  statement.
- Every **`Iterable`** (ranges except `ToRange`, Strings, Vectors, Maps, Bytes, and your own types
  that implement it) has these methods, all lazy except `reduce`:
  - `map(f)`: `f(value)` or `f(value, index)` gives each new item.
  - `filter(f)`: keeps items where `f(value)` or `f(value, index)` is truthy.
  - `skip(n)`, `take(n)`: skip the first `n` items, or stop after `n`.
  - `reduce(f, initial)`: like JavaScript: `f(acc, value)` or `f(acc,
    value, index)`. Without `initial` the first item starts the
    accumulator, and an empty Iterable gives `none`.
  - `reduce()` with no arguments collects the items into a new Vector:
    `(1..4).reduce()` is `[1, 2, 3]`, `"ab".reduce()` is `[a, b]`. (There's
    no separate `collect`.)
- An Iterable can be iterated again (each pass starts over). Nothing
  consumes an infinite `1..` except `take`, so `(1..).reduce(f)` never ends.
- **Your own types**: implement `Iterable` (an `iter` method returning an
  iterator) and `Iterator` (a `next` method returning `some(item)` or
  `none`), and every method above comes for free:

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

- Loop over any Iterable with `for` (see "Control flow"):

```mah
for let n, let i in (1..=2).map(fn(x) { x * 10 }) {
    print(i, n)                 # 0 10, then 1 20
}
```

## Vectors and Maps

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

- **Slicing**: indexing a Vector with a range (`v[a..b]`, `v[a..=b]`,
  `v[a..]`, `v[..b]`, `v[..=b]`) returns a **new** Vector of those items.
  Like Python, negative bounds count from the end and bounds past either
  end are clamped instead of raising (`v[..40]` is the whole Vector,
  `v[10..]` of a short one is `[]`). Bounds must be integers. You can't
  assign to a slice (`v[0..2] = ...` is a runtime error).
- **Vector** methods: `len()`, `push(x)`, `pop()`, `push_start(x)`,
  `pop_start()`, `copy(deep = false)`. `pop`/`pop_start` return the removed item, or `none` when
  empty. Negative indices count from the end, like Python: `v[-1]` is the
  last item, `v[-2]` the one before it (reading and writing). Reading an
  index outside `-len..len`, or a fractional one, gives `none`;
  **writing** there is a runtime error (use `push`). A non-Number index is
  a runtime error.
- **Map** methods: `len()`, `has(k)`, `remove(k)` (the removed value, or
  `none`), `copy(deep = false)`, and `keys()`, `values()`, `entries()`, which each return a new
  Vector (a snapshot: changing the Map afterwards doesn't change it).
  `entries()` holds `MapEntry { key, value }` structs. Reading a missing
  key gives `none`, so `m[k]` can't tell a missing key from a stored `none`:
  use `m.has(k)`.
- Map keys must be Strings, Numbers, or Bools (anything else is a runtime
  error). `1` and `1.0` are the same key; `1`, `"1"`, and `true` are three
  different keys.
- Both are **Iterable**, so `for`, `map`, `filter`, `skip`, `take`, and
  `reduce` work on them. A Vector iterates its items (live: items pushed
  during the loop are reached too); a Map iterates its keys (a snapshot,
  so removing keys while looping is safe).
- Both are references: `let w = v` then `w.push(1)` changes `v` too. Use
  `let w = v.copy()` for an independent one. `copy()` is **shallow**: a
  Vector of Vectors gets a new outer Vector sharing the same inner ones.
  `copy(deep: true)` also copies every Vector, Map, struct, and enum value
  inside, all the way down (functions and Promises are still shared):

```mah
let grid = [[0, 0], [0, 0]]
let shallow = grid.copy()
let deep = grid.copy(deep: true)
grid[0][0] = 1
print(shallow[0][0], deep[0][0])   # 1 0
```
- Printing shows `[1, 2]` and `[a: 1, b: 2]` (Strings inside are printed
  without quotes, like everywhere else).
- A `[` on a new line starts a new Vector/Map literal, not an index into
  the previous line's value: write `x[0]` with the `[` right after `x`.
- **Strings** can be indexed and sliced the same way, read-only: `s[0]`
  is the first character (a one-character String), `s[-1]` the last,
  `s[1..3]` a substring, and an index past the end gives `none`. Indices
  count characters (Unicode code points), like `len()` and `char_at`:

```mah
let s = "héllo"
print(s[1], s[-1], s[9], s[1..3], s[..=1], s[2..])   # é o none él hé llo
```

- **String methods** (all return new Strings; positions count code points,
  and "whitespace" is any Unicode whitespace):

```mah
print("a,b,,c".split(","), "  one  two ".split(), "k=v=w".split("=", 1))   # [a, b, , c] [one, two] [k, v=w]
print("|" + "  hi ".trim() + "|", "7".pad_start(3, "0"), "ab".repeat(2))  # |hi| 007 abab
print("a-b-c".replace("-", "+"), "a-b-c".replace_all("-", "+"))           # a+b-c a+b+c
print("hello".starts_with("he"), "hello".contains("ll"), "hello".index_of("l"))  # true true some(2)
print("Straße".to_upper(), "A\nB\n".lines(), ["x", 1].join(", "))           # STRASSE [A, B] x, 1
print("42".to_number() + 1, " 7 ".parse_number(), "x".parse_number())     # 43 7 none
```

| method | |
|---|---|
| `split(sep = none, limit = none)` | a Vector of pieces; no `sep`: split on runs of whitespace; `limit` = at most that many splits |
| `trim()`, `trim_start()`, `trim_end()` | without surrounding whitespace |
| `pad_start(width, fill = " ")`, `pad_end(...)` | padded with `fill` to `width` characters |
| `replace(from, to)`, `replace_all(from, to)` | the first / every occurrence replaced |
| `starts_with(s)`, `ends_with(s)`, `contains(s)` | Bool |
| `index_of(s)` | `some(position)` or `none` |
| `repeat(n)`, `to_upper()`, `to_lower()` | |
| `lines()` | a Vector of lines (`\n` or `\r\n` endings) |
| `to_number()` | the Number it spells (whitespace allowed); throws `NumberParseError` (`e.text` is the String) |
| `parse_number()` | the same, but `none` instead of throwing |
| `char_at(i)` | the character at `i` (an error when out of range; `s[i]` gives `none`) |

Also `v.join(sep = "")` on a Vector (each item's `to_string`), and on
an Option value such as `index_of`'s result: `unwrap()` (the value; throws
on `none`), `unwrap_or(default)`, `is_some()`, `is_none()`, and
`to_bytes()` on a String (its UTF-8 encoding, as Bytes; see Bytes).

### Bytes

`Bytes` is a growable sequence of bytes (whole Numbers 0 to 255) for binary
data. It's mutable and by reference like a Vector, and there's no literal:
make one with `"text".to_bytes()` or the `std:bytes` functions.

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

- **Indexing** works like a Vector's: `b[i]` is the byte as a Number (`none`
  when `i` names no item; negative counts from the end), `b[a..b]` (any
  range form) is a **new** Bytes with a Vector's slice rules, and `b[i] = n`
  sets a byte. Writing needs an existing index (use `push` to add) and a
  whole Number from 0 to 255; anything else is a runtime error, as is
  assigning to a slice or a non-Number index.
- `==` compares **contents**, so `"hi".to_bytes() == "hi".to_bytes()` is
  `true`; a Bytes never equals a Vector. A Bytes is always truthy, isn't a
  Map key, and prints as `Bytes[68 69]` (two lowercase hex digits per byte,
  `Bytes[]` when empty). `a + b` on two Bytes is a new Bytes.
- **Methods**: `len()`, `push(n)` (appends a byte), `pop()` (the removed
  byte, or `none`), `extend(other)` (appends another Bytes' bytes),
  `copy()`, `to_vector()` (a Vector of Numbers), `to_text()` (`some(String)`
  if the bytes are valid UTF-8, else `none`), `to_text_lossy()` (a String,
  each invalid sequence replaced by U+FFFD), `to_hex()` (lowercase),
  `to_base64()` (standard alphabet, `=` padded), `index_of(needle)` (`some(i)`
  of the first occurrence of the Bytes `needle`, `none` if absent).
- A Bytes is **Iterable** over its Numbers (live, like a Vector). A Vector's
  `copy(deep: true)` copies the Bytes inside it; a shallow `copy()` shares them.
- `Bytes` is also a type annotation and a Type value
  (`reflect.type_of(b)` is `Bytes`).

- **Your own types** can support `x[k]` by implementing the `Index` trait
  (`fn index(self, key)`) and `x[k] = v` with `IndexAssign`
  (`fn index_assign(self, key, value)`):

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

## Traits and methods

```mah
trait Shape {
    fn area(self)                           # required method
    fn describe(self) { "area " + self.area() }   # default method
    fn unit()                               # static (no `self`)
}

struct Rect { w, h }

impl Rect {                                 # the type's own methods
    fn new(w, h) { Self { w: w, h: h } }    # `Self` = the impl's type
    fn scale(self, k) { self.w = self.w * k; self }
}

impl Shape for Rect {
    fn area(self) { self.w * self.h }
    fn unit() { Rect { w: 1, h: 1 } }
}

let r = Rect.new(2, 3)          # static function: Type.name(...)
print(r.area())                 # method call
print(r.describe())             # inherited default method
print(Shape.area(r))            # trait-qualified call
```

- A trait/impl `fn` whose first parameter is `self` is a method; otherwise
  it's a static function called as `Type.name(...)`. `self` can't appear
  anywhere else.
- `trait` and `impl` must be at the top level. They (and top-level
  `struct`/`enum`) can be used before they're declared, and method bodies
  can use any top-level name.
- `impl Type { }` only works for your own structs/enums. `impl Trait for
  Type { }` works if the trait or the type is yours, so `impl MyTrait for
  Number` is fine. Built-in type names: `Number`, `String`, `Bool`,
  `Function`, `Option`, `Promise`, `Vector`, `Map`, `Bytes`. Built-in traits:
  `Printable`, `Index`, `IndexAssign`.
- The impl must define every required method, with the same parameters.
- A top-level `fn` can be an impl target too: `impl Tr for somefn { ... }`
  and `impl somefn { ... }` give *that function* methods
  (`somefn.describe()`); other functions and closures don't have them. Only a
  top-level `fn` declaration works (not a `let f = fn ...`, a nested `fn` or
  a closure: "impl targets must be a type or a top-level function"), and the
  impl must be in the module that declares the function or the one that
  declares the trait. `reflect.type_of(somefn)` is still `Function`. This is
  how a function acts as a decorator with hooks (see Hooks).
- If two traits give one type the same method name, call it as
  `Trait.name(value)`.
- `p.f()` calls a function stored in field `f` if there's no method `f`.

### `Printable`: custom printing

```mah
struct Point { x, y }
let p = Point { x: 10, y: 2 }
impl Printable for Point {
    fn to_string(self) { "(" + self.x + ", " + self.y + ")" }
}
print(p)            # (10, 2), also used by "text " + p
```

Without an impl, values print structurally (`Point { x: 1, y: 2 }`).
`to_string` must return a String.

## `defer`

```mah
fn work() {
    print("open")
    defer print("close")        # runs when the enclosing block exits
    print("working")
}                               # prints open, working, close
```

Deferred statements run when their **block** exits (normally or via
`return`/`break`/`continue`), last-deferred first.

## Async

Single-threaded and cooperative. Calls are synchronous unless you `detach`
them.

```mah
fn fetch(n) { sleep_async(100); n * 2 }   # a bare sleep_async just waits

let p = detach fetch(21)        # starts it; you get a Promise back
print("meanwhile")
print(p.await)                  # waits for the result: 42
let q = detach obj.method(1)    # methods can be detached too
let r = detach {                # so can any expression: a block, a loop,
    sleep_async(10);            # an `if`, a `match`, `(a + b)`, ...
    2 + 3
};
print(r.await)                  # 5
```

- Detaching a call evaluates its arguments right away, in the caller; only
  the call itself runs as the new task. Any other expression runs entirely
  in the new task and sees surrounding variables by reference (it reads
  their values when it runs, not when you detach it).
- `detach` binds tightly: `detach a + b` is `(detach a) + b`; write
  `detach (a + b)`. `return`, and a `break`/`continue` for a loop outside
  the detached expression, are compile errors inside it.

- `value.await` waits for a Promise. A Promise is an enum:
  `Promise.Pending` or `Promise.Settled { value }`.
- The program exits once the main code is done **and** no detached work
  (a timer, or a detached `input` waiting for its line) is still pending.
- `input(...)` waits for a line like any call; detached, it lets timers and
  other tasks run meanwhile:

```mah
let answer = detach input("number: ")
for let i in 0..3 {
    sleep_async(500)
    print("still waiting...")           # prints while the user types
}
let n = try answer.await.to_number() else 0   # .await throws EndOfInput / to_number NumberParseError
print(n + 1)
```

## Type annotations

Declarations can carry optional types. An unknown type name or a wrong
number of `<...>` arguments is always a compile error. Annotations never
change how the program runs (the program can read the ones it *wrote*, and
`##` docs, through `std:reflect`, but nothing acts on them), and they
are checked, by `mah check` and by the editor: whether a mismatch is just
a warning or a compile error, and whether an inferred type is allowed to
stay unannotated, depends on `[types] check` in `mah-project.toml`:
`"loose"` (the default) reports mismatches as warnings only, and `mah
run`/`mah build` don't check at all; `"strict"` makes a mismatch a compile
error, blocking `run`/`build`; `"explicit"` is like `strict`, and also
requires an annotation anywhere a type can't be inferred. Method calls
(`p.area()`, `Rect.new(1, 2)`, `Shape.area(p)`) are checked against the
method's signature, and `self` has the impl's type; a parameter whose
only clue is a method call gets the one type that has that method.
Trait-typed values, bounds, and the prelude's iterator methods (`map`,
`filter`, ...) aren't checked yet.

The checker is stricter than the runtime in a few places, so code that
should pass `strict` follows these rules:

- A variable keeps one type: `let x = 1` then `x = "s"` is a type error
  (use a new `let x = ...` to shadow it instead).
- `[...]` and `[k: v]` literals hold one element type (`[1, "a"]` is an
  error), and range bounds are Numbers.
- `none` fits any type, and `let x = none` takes its type from the first
  non-`none` value assigned later.
- Unannotated parameters are inferred from how they're used. `+`, `*`, and
  comparisons on values of unknown type default to `Number`: `fn add(a, b)
  { a + b }` takes Numbers, so annotate `a: String` to concatenate.
- An `if`/`match` whose branches have different types is fine as a
  statement, but its value has type `Unknown`.

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

- Types: `Number`, `String`, `Bool`, `Bytes`, `Vector<T>`, `Map<K, V>`, `Option<T>`,
  `Promise<T>`, `Type<T>` (the type of the value `T`, see Type values and
  reflection), your structs/enums/traits (with their `<...>` arguments),
  a type parameter, `fn(A, B) -> R` (no `->` means it returns `none`),
  `Self` (inside `trait`/`impl`), `None` (the type of `none`), `Never`,
  and `Unknown` (anything).
- `self` is never annotated. Type arguments are never written at a call
  (`first(v)`, not `first<Number>(v)`).
- `Function` is not a type in annotations: write `fn(...) -> ...`.
- In `impl` headers the `<...>` can be left off (`impl Iterable for P`).

## Modules

```mah
# mathlib.mh
export fn square(n) { n * n }
export let answer = 42

# main.mh
import "mathlib.mh"                  # bring exported names in directly
import m from "mathlib"              # or namespaced (the .mh is optional)
print(square(3), m.answer)
```

Only names marked `export` are visible to importers. Paths are relative to
the importing file. `struct`, `enum` and `trait` names are module-scoped just
like `fn` and `let` names: an imported module's types are private unless
exported (`export struct Point { ... }`, `export enum`, `export trait`, or
`export Point` for one declared elsewhere), so two modules (or a module and
your program) can each declare their own `Request` without clashing. A
namespaced import reaches a type as `lib.Point` wherever a type name is
written; a flat import brings the bare name:

```mah
# geometry.mh
export struct Point { x: Number, y: Number }
export enum Shape { Circle { r: Number }, Empty }
export trait Named { fn name(self) -> String }
export fn origin() -> Point { Point { x: 0, y: 0 } }

# main.mh
import geo from "geometry"
struct Mine { a: Number }                          # your own names never clash
impl geo.Named for Mine { fn name(self) -> String { "mine" } }
let p: geo.Point = geo.Point { x: 1, y: 2 }        # annotation and literal
let s = geo.Shape.Circle { r: 2 }
match s {
    geo.Shape.Circle { r } => { print(r) }         # pattern (variants: geo.Shape.Empty)
    _ => { }
}
print(geo.Point, p, geo.origin())                  # Point Point { x: 1, y: 2 } Point { x: 0, y: 0 }
```

Types print (and appear in error messages) under the name they were
declared with, never the module-private one. A type or trait *name* is only
looked up by string in `reflect.implements(T, "Trait")`, which compares the
declared name. The prelude's types and traits (`Range`, `Iterator`,
`Iterable`, `Printable`, `Error`, ...) stay global; a module may declare its
own type with such a name.

## Standard library

Standard library modules are imported as `"std:<name>"`, the same two ways
as a file: `std:math`, `std:path`, `std:json`, `std:csv`, `std:random`,
`std:collections`, `std:regex`, `std:time`, `std:async`, `std:bytes`, `std:fs`,
`std:process`, `std:socket`, `std:url`, `std:http`, `std:reflect` (and `std:test`, below).

```mah
import math from "std:math"
print(math.sqrt(2), math.round(math.pi, 2))     # 1.414213562373095048801688724 3.14
print(math.floor(0 - 2.5), math.max(3, 7))      # -3 7
print(try math.log(0) else "undefined")         # undefined
```

| `std:math` | |
|---|---|
| `pi`, `e` | constants |
| `sqrt(x)`, `pow(x, y)`, `abs(x)` | exact on Numbers (28 digits) |
| `floor(x)`, `ceil(x)`, `round(x, digits = 0)` | `round` rounds halves away from zero |
| `min(a, b)`, `max(a, b)`, `clamp(x, lo, hi)` | two arguments each (plus the range for `clamp`) |
| `sin`, `cos`, `tan`, `asin`, `acos`, `atan`, `atan2(y, x)` | radians, double precision |
| `exp(x)`, `log(x)`, `log10(x)` | natural log and base 10, double precision |

A domain error (`log(0)`, `sqrt(-1)`, `asin(2)`) throws
`RuntimeError.ArgumentError`. Any other `std:` name is a compile error.

`std:path` is string logic on paths (`/` or `\`, answers use `/`):

```mah
import path from "std:path"
print(path.join("src", "main.mh"), path.dirname("a/b/c.txt"))    # src/main.mh a/b
print(path.basename("a/b.tar.gz"), path.extension("b.tar.gz"))   # b.tar.gz .gz
print(path.stem("b.tar.gz"), path.normalize("a/./b/../c"))       # b.tar a/c
print(path.relative("x/a", "x/b"), path.is_absolute("/x"))       # ../b true
print(path.join_all(["a", "b", "c"]))                            # a/b/c (no variadics)
```

`std:json`: `parse(text)` gives Maps, Vectors, Numbers, Strings, Bools and
`none` (typed `Unknown`); `stringify(value, indent = 0)` also writes
structs (as objects) and enums (`"Unit"` or `{"Variant": {fields}}`). Both
throw `json.JsonError` (`.Syntax { message, line, column }` or `.Shape {
message }`). Read into a struct by implementing `json.FromJson` with the
helpers `field`, `as_number`, `as_string`, `as_bool`, `as_vector`, `as_map`
-- or let `decode` do it from the struct's declaration (see below):

```mah
import json from "std:json"
struct Point { x: Number, y: Number }
impl json.FromJson for Point {
    fn from_json(value) {
        Point { x: json.as_number(json.field(value, "x")), y: json.as_number(json.field(value, "y")) }
    }
}
let data = try json.parse("{\"x\": 1, \"y\": [true, null]}") else [:]
print(data["y"][0], try json.stringify(data) else "")         # true {"x":1,"y":[true,null]}
let p = try Point.from_json(json.parse("{\"x\": 1, \"y\": 2}")) else Point { x: 0, y: 0 }
let pretty = try json.stringify(p, indent: 2) else ""
print(p.x + p.y, pretty.lines().len())                          # 3 4
```

`json.decode(T, value)` (and `json.parse_as(T, text)`, `decode(T, parse(text))`)
reads a parsed value as a declared type `T`, checking it against the
annotations: `Number`, `String`, `Bool`, `Vector<X>`, `Map<String, X>`,
`Option<X>` (a missing key is `none`), other structs and enums (an enum reads
what `stringify` writes), and takes an unannotated field, `Unknown` or a type
parameter as it is. A type with a `json.FromJson` impl is read by that. It throws
`json.JsonError.Shape` saying where: `expected a Number for User.age, got String`,
`missing field 'name' for User`, `expected a String for User.tags[0], got Number`.

```mah
import json from "std:json"
struct User { name: String, age: Number, email: Option<String>, tags: Vector<String> }
let u = json.parse_as(User, "{\"name\": \"ada\", \"age\": 36, \"tags\": [\"x\"]}")
print(u.name, u.age + 1, u.email, u.tags)                     # ada 37 none [x]
print(try json.parse_as(User, "{\"name\": 1}") else "shape")   # shape
try {
    json.parse_as(User, "{\"name\": \"a\", \"age\": \"old\", \"tags\": []}")
} catch {
    e: json.JsonError => { print(e.message()) }    # expected a Number for User.age, got String
}
```

`std:csv` (RFC 4180; every field is a String; throws `csv.CsvError { message,
line }`):

```mah
import csv from "std:csv"
let rows = try csv.parse("a,b\n1,\"x, y\"\n") else []           # Vector<Vector<String>>
print(rows[1][1])                                               # x, y
let people = try csv.parse_records("name,age\nal,3\n") else []   # Vector<Map<String, String>>
let age = try people[0]["age"].to_number() else 0
print(age + 1)                                                  # 4
print(try csv.stringify([["a", "b,c"], ["1", ""]]) else "")     # a,"b,c" then 1,
```

`csv.stringify_records(records, columns = none)` writes Maps with a header
row, and `csv.FromCsvRow` / `csv.column(row, name)` work like `FromJson`.

`std:random` (the same generator on every VM, so a seeded run is
repeatable; not for cryptography). Bad arguments throw
`RuntimeError.ArgumentError`:

```mah
import random from "std:random"
random.seed(42)                                  # optional: repeatable from here on
print(random.randint(1, 6), random.random() < 1)  # 1 true (randint includes both ends)
let deck = ["A", "K", "Q"]
random.shuffle(deck)                             # in place; shuffled(v) returns a copy
print(random.choice(deck) != "", random.sample(deck, 2).len())   # true 2
let rng = random.Rng.new(7)                             # an independent generator
print(rng.uniform(0, 10) < 10)                   # true
```

`std:collections` exports `Set`, `Deque` and `PriorityQueue` (a flat
import is idiomatic, so they are written bare). All three are Iterable and
print like `Set[1, 2]`:

```mah
import "std:collections"
let seen = Set.of([3, 1, 3])                     # Set[3, 1]; values must be Map keys
seen.add(2)
print(seen.has(1), seen.len(), seen.union(Set.of([9])))   # true 3 Set[3, 1, 2, 9]
let q = Deque.new()                              # O(1) at both ends
q.push_back(1)
q.push_front(0)
print(q.pop_front(), q.pop_back(), q.pop_back())  # 0 1 none (none when empty)
let pq = PriorityQueue.new(fn(word) { word.len() })   # smallest key first; ties in push order
pq.push("pear")
pq.push("fig")
print(pq.pop(), pq.peek(), pq.len())             # fig pear 1
```

Also: Set `remove`/`intersection`/`difference`/`is_subset`/`equals`
(`==` compares identity), Deque `front`/`back`/`get(i)`, and
`PriorityQueue.of(values, key = none)`; for largest first use a negating
key, `fn(x) { 0 - x }`.

`std:regex`: the same matching on every VM, over a checked subset of the
usual syntax -- `.` `[a-z]` `[^...]` `\d \w \s` (ASCII) `^ $ \A \z \b`
`* + ? {n,m}` (lazy with `?`) `(x) (?:x) (?<name>x) a|b`, flags `"i"`
(ASCII case), `"m"`, `"s"`. No lookaround, backreferences or inline
flags. Positions count characters. Remember that `\` in a Mah String is
itself escaped: write `"\\d+"`.

```mah
import regex from "std:regex"
let date = regex.must_compile("(?<y>\\d{4})-(?<m>\\d\\d)")   # a fixed pattern
print(date.is_match("due 2026-09"))                             # true
let m = date.find("due 2026-09").unwrap()                       # find gives an Option<Match>
print(m.text, m.start, m.group("y"), m.group(2))                # 2026-09 4 2026 09
print(date.replace_all("2026-09 2027-01", "$m/$y"))             # 09/2026 01/2027
print(regex.must_compile("\\s*,\\s*").split("a , b,c"))           # [a, b, c]
print(date.find_all("2026-09 2027-01").len())                   # 2
let user_pattern = "(oops"
print(try { regex.compile(user_pattern) } catch { e: regex.RegexError => { e.message() } })
# missing ) at position 0 in "(oops" -- compile throws regex.RegexError; use it for patterns from input
```

Also `replace` (first match only), `replace_all(text, fn(m) { ... })`,
`split(text, limit)`, `regex.escape(text)` for a literal, and
`regex.Match`'s `groups`/`end`. A group the pattern lacks (`m.group(9)`) throws
`RuntimeError.ArgumentError`.

`std:time`: times and durations are Numbers of **seconds** (so `t + 90`
and `b - a` work); dates are UTC only (no time zones yet).

```mah
import time from "std:time"
let start = time.monotonic()                     # seconds since the program started
let d = time.utc(1790597925)                     # a DateTime from seconds since 1970 (time.now() is now)
print(d, d.year, d.weekday())                    # 2026-09-28T12:18:45Z 2026 1 (Monday = 1)
print(time.format(d, "%a %d %b %Y %H:%M"))       # Mon 28 Sep 2026 12:18
let due = try time.parse("2026-10-05", "%Y-%m-%d") else d   # parse/date throw time.TimeError
print(time.duration_text(due.timestamp() - d.timestamp()))  # 6d 11h 41m 15s
print(time.duration_text(time.monotonic() - start) != "")   # true
```

Codes: `%Y %m %d %H %M %S %f`(ms) `%j %B %b %A %a %%`. Also `time.date(y, m,
d, h = 0, ...)`, `.timestamp()`, `.day_of_year()`, `time.iso(d)` /
`time.parse_iso(text)`.

`std:async` combines Promises (from `detach`) and runs timers. Each
function waits like a call; its value and errors are those of the
Promises:

```mah
import async from "std:async"
fn job(ms: Number, name: String) -> String { sleep_async(ms); name }
print(async.all([detach job(30, "a"), detach job(10, "b")]))   # [a, b] (in order)
print(async.race([detach job(30, "slow"), detach job(5, "fast")]))  # fast
print(try async.timeout(detach job(500, "x"), 20) else "too slow")  # too slow (async.TimeoutError)
let count = [0]
let ticker = async.set_interval(fn() { count[0] = count[0] + 1 }, 10)
sleep_async(55)
async.clear_interval(ticker)                     # cleared timers don't keep the program alive
print(count[0] > 0)                              # true
```

`set_timeout(f, ms)` / `clear_timeout(id)` likewise. Timer callbacks are
`fn() throws never`: handle errors inside them.

`std:bytes`: binary data (see Bytes). `new(size = 0, fill = 0)`,
`from_vector(items)`, `concat(parts)` (a Vector of Bytes joined into one),
and `from_hex(text)` / `from_base64(text)`, which throw `bytes.BytesError {
kind, description }` (`kind` is `"invalid_hex"` or `"invalid_base64"`) unless
the text is an even number of hex digits (either case) or valid padded standard
base64 (no whitespace). A bad byte or size in `new`/`from_vector`/`push`/`b[i] = n`
is a runtime error, not a `BytesError`.

`std:fs`: files and directories. Every function waits like a call (`detach`
one to run it alongside other work) and throws `fs.FsError { kind, op, path,
description }`, `kind` being `"not_found"`, `"permission_denied"`,
`"already_exists"`, `"is_a_directory"`, `"not_a_directory"`,
`"directory_not_empty"`, `"invalid_utf8"`, `"closed"` or `"other"`. Text
is UTF-8, exactly as written (no newline changes):

```mah
import fs from "std:fs"
try {
    let dir = fs.temp_dir()                      # a fresh temporary directory
    defer fs.remove(dir, recursive: true)
    let p = dir + "/notes.txt"
    fs.write_text(p, "one\ntwo\n")               # also append_text; read_text gives it all back
    print(fs.exists(p), fs.is_file(p), fs.is_dir(dir))   # true true true
    let f = fs.open(p)                           # mode "r" (default), "w" or "a"
    defer f.close()
    for let line in f.lines() { print(line) }   # one, then two (lines() reads lazily)
    print(fs.list_dir(dir))                      # [notes.txt] (sorted names)
    print(fs.glob(dir + "/*.txt").len())         # 1 (* ? [abc], and ** across directories)
    fs.read_text(dir + "/nope")
} catch {
    e: fs.FsError => { print(e.kind) }              # not_found
}
```

Also `info(path)` (`fs.FileInfo { kind, size, modified }`), `mkdir(path,
parents = false)`, `remove(path, recursive = false)`, `rename(from, to)`,
`copy(from, to)`, and a File's `read_line()` (`none` at the end),
`read_all()` and `write(text)`.

Binary I/O uses Bytes: `read_bytes(path)`, `write_bytes(path, data)` (replace
or create), `append_bytes(path, data)`, and a File's `read_bytes(max = none)`
(up to `max` bytes, all the rest without it; fewer only at the end, empty
Bytes at the end) and `write_bytes(data)`. They throw `FsError` like the rest.
Text and binary reads can be mixed on one open file. A program importing
`std:fs` needs a 1.17 VM.

`std:process`: the program's arguments and environment, and running other
programs. `args()` is what follows `--` in `mah run FILE -- ARGS...` (also
`mah runc`); `exit(code = 0)` ends the program at once (0 to 255): output is
flushed, but pending `defer`s don't run and no `try`/`catch` can stop it.
`env_get(name)` is an `Option<String>`; `env_set`, `env_remove` and `env()`
(a Map sorted by name) work on the program's own copy of the environment,
taken at start, which the programs it runs inherit. `run(program, args = [],
cwd = none, env = none, stdin = "")` starts a program directly (no shell) and
waits for it; `shell(command, ...)` runs a command line through `/bin/sh -c`.
Both give a `process.Output { code, stdout, stderr }` (`ok()` is `code == 0`; a
signal N gives `128 + N`) and throw `process.ProcessError { kind, command,
description }` (`"not_found"`, `"permission_denied"` or `"other"`) only when
the program can't be started; a non-zero exit is not an error. Also `cwd()`,
`pid()` and `platform()` (`"linux"`, `"macos"`, `"windows"`).

```mah
import process from "std:process"
print(process.args())                            # [] (or the words after `--`)
let port = process.env_get("PORT").unwrap_or("8080")
print("port", port)                              # port 8080 (unless PORT is set)
try {
    let out = process.run("sh", ["-c", "printf hi; exit 3"], env: some(["X": "1"]))
    print(out.ok(), out.code, out.stdout)        # false 3 hi
    print(process.shell("echo a | tr a b").stdout.trim())   # b
    process.run("no-such-program")
} catch {
    e: process.ProcessError => { print(e.kind) }         # not_found
}
```

`std:socket`: TCP. `connect(host, port, timeout = none)` gives a `socket.Socket`;
`listen(port, host = "127.0.0.1", backlog = 128)` gives a `socket.Listener`
(port 0 picks a free one; `.port` is the real one) whose `accept(timeout =
none)` gives Sockets and `close()` stops it. A Socket has `send(data)` (Bytes,
all of it), `send_text(text)`, `recv(max = 65536, timeout = none)` (Bytes, up to
`max`; **empty once the peer has closed its side**), `recv_exactly(n, timeout =
none)`, `read_line(timeout = none)` (a String without its `\n` or `\r\n`, or
`none` at the end; extra bytes wait in `buffer`, which `recv` returns first),
`shutdown()` (stop sending; the peer's `recv` then ends) and `close()` (twice is
fine), plus `peer_host`, `peer_port` and `local_port`. Timeouts are in
milliseconds (`none` waits forever, 0 only what is there). Everything throws
`socket.SocketError { kind, op, address, description }`; `kind` is
`"connection_refused"`, `"connection_reset"`, `"timed_out"`, `"address_in_use"`,
`"address_not_available"`, `"host_not_found"`, `"permission_denied"`,
`"closed"`, `"closed_early"` (`recv_exactly` hit the end), `"invalid_utf8"` (in
`read_line`) or `"other"`. Calls wait like any async call, and `detach` runs one
in the background; a waiting `accept` or `recv` keeps the program running, and
closing its socket or listener makes it fail with `"closed"`. **TLS**:
`socket.connect_tls(host, port, timeout = none)` opens an encrypted connection,
and `s.start_tls(server_name, timeout = none)` upgrades a connected Socket (its
`buffer` must be empty); the server's certificate must be valid for the name
and trusted (the PEM file in the `SSL_CERT_FILE` environment variable, else
the system's roots). TLS failures are kinds `"tls_certificate"` and `"tls"`. A
program importing `std:socket` needs a 1.19 VM.

```mah
import socket from "std:socket"
let server = socket.listen(0)                  # a free port on 127.0.0.1
let incoming = detach server.accept()
let client = socket.connect("127.0.0.1", server.port)
let conn = incoming.await
client.send_text("hello\n")
print(conn.read_line())                        # hello
conn.send("hi".to_bytes())
print(client.recv())                           # Bytes[68 69]
client.close()
conn.close()
server.close()
try {
    socket.connect("127.0.0.1", server.port, 500)
} catch {
    e: socket.SocketError => { print(e.kind) } # connection_refused
}
```

`std:url`: `url.parse(text)` gives a `url.Url { scheme, username, password,
host, port, path, query, fragment }` (scheme and host lowercased; `port`,
`query`, `fragment` are `none` when absent; `path`/`query` stay
percent-encoded) or throws `url.UrlError { kind, text, description }`.
`u.effective_port()` (the scheme's default when `port` is none),
`u.origin()`, `u.request_target()`, `u.query_pairs()`, `u.resolve(relative)`
(RFC 3986) and `u.with_query(params)`; it prints as the URL. `url.encode(text,
safe = "")` / `url.decode(text)` percent-encode, `url.encode_query(params)`
takes a Map or `[key, value]` pairs (form style, space as `+`) and
`url.parse_query(q)` returns `[key, value]` pairs. `url.is_valid(text)`.

```mah
import url from "std:url"
let u = url.parse("https://example.com:8443/docs/a?x=1#top")
print(u.host, u.port, u.path, u.query, u.fragment)   # example.com 8443 /docs/a x=1 top
print(u.resolve("../b?y=2"))                         # https://example.com:8443/b?y=2
print(url.encode("a b/c"), url.encode_query(["q": "mah lang", "n": 2]))   # a%20b%2Fc q=mah+lang&n=2
```

`std:http`: an HTTP/1.1 client (http and https) and server (below). `http.get(url, headers = [:],
timeout = 30000, max_redirects = 10)`, `http.post(url, body = none, json = none,
form = none, headers = [:], ...)`, likewise `put`, `patch`, `delete`, `head`,
and `http.request(method, url, ...)`. `body` is a String or Bytes; `json:`
sends a value as JSON, `form:` a Map as a form; `headers` is a Map or `[name,
value]` pairs. The result is an `http.Response { status, reason, headers, body,
url, method }` with `r.text()`, `r.json()`, `r.header(name)` (any case, `none` if
missing), `r.header_all(name)`, `r.is_success()` and `r.check_status()`. **A 404
or 500 is still a Response**; `http.HttpError { kind, method, url, description }`
is for requests that couldn't complete (`kind` is a SocketError kind like
`"connection_refused"`, `"timed_out"`, `"tls_certificate"`, or
`"invalid_url"`, `"unsupported_scheme"`, `"invalid_response"`,
`"too_many_redirects"`, `"proxy"`, `"status"` from `check_status`). Redirects
are followed; `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` are honored; each request
uses one connection, and `timeout` (ms) applies to each wait.

```mah
import http from "std:http"
fn fetch_title() -> String throws http.HttpError {
    let r = http.get("https://example.com/", headers: ["Accept": "text/html"])
    if !r.is_success() { return "status " + r.status }
    r.text().split("<title>")[1].split("</title>")[0]
}
let created = try { http.post("https://api.example.com/items", json: ["name": "mah"]) } catch {
    e: http.HttpError => { print(e.kind, e.message()); none }
}
```

**The server**: `http.serve(port, handler, host = "127.0.0.1", ...)` listens
(port 0 picks a free one), returns an `http.Server` at once and serves in the
background. A handler is a plain function `fn(http.Request) -> http.Reply`.
A Request has `method`, `target`, `path`, `query` (`none` without one),
`version`, `headers` (pairs), `body` (Bytes, read whole), `peer_host`,
`peer_port`, `tls`, and `header(name)`, `content_type()`, `text()`, `json()`,
`query_param(name)`, `query_pairs()`, `form()` (`[name, value]` pairs) and
`multipart()` (a Vector of `http.Part { name, filename, content_type,
headers, body }`). Answer with `http.Reply.text(s)`, `html`, `json(value)`,
`bytes(b, content_type)`, `redirect(location)`, `empty()` (204),
`Reply.new(status, body, headers)` or `Reply.stream(fn(w) { w.write(...) })`
(each takes `status:` and `headers:`), and `set_header`/`add_header`.
A bad JSON, form or query in `req.json()` & co. throws `http.HttpError` kind
`"bad_request"`, answered with 400; any other error a handler throws is a 500
(`on_error: fn(e, req) { ... }` may return the Reply to send instead).
Keep-alive, chunked bodies and `Expect: 100-continue` are handled; options
`max_head`, `max_body`, `read_timeout`, `idle_timeout`, `max_connections`
set the limits (431/414, 413, 408, 503). Connections run as tasks, so slow
handlers that wait don't block each other. `server.shutdown()` stops
gracefully and returns at once, `server.wait()` waits until it has stopped,
`server.close()` does both; a server keeps the program running until then.
`tls: socket.tls_server_config("cert.pem", "key.pem")` serves HTTPS.

```mah
import http from "std:http"
fn handle(req: http.Request) -> http.Reply {
    if req.path == "/" { return http.Reply.text("hello") }
    if req.path == "/add" & req.method == "POST" {
        let data = req.json()                    # bad JSON: a 400 for the client
        return http.Reply.json(["sum": data["a"] + data["b"]])
    }
    http.Reply.text("not found", status: 404)
}
let server = http.serve(0, handle)               # a free port; serve(8080, handle).wait() runs forever
print(http.get(server.url("/")).text())          # hello
print(http.post(server.url("/add"), json: ["a": 1, "b": 2]).text())   # {"sum":3}
server.close()
```

### Type values and reflection

A bare type name in an expression is a **`Type` value**: `Number`, `String`,
`Bool`, `Function`, `Vector`, `Map`, `Bytes`, `Option`, `Promise`, `RuntimeError`,
`None`, `Type`, or any struct or enum. A variable in scope with that name
wins, exactly like an enum's unit variant. Types have no type arguments in a
value (`Vector`, not `Vector<User>`), compare with `==` (same declared type),
print as their name, and aren't Map keys or JSON. The checker gives `User`
the type `Type<User>`.

A `##` comment above a declaration is its **documentation**; `std:reflect`
reads it, and the written annotations, defaults and `throws` clause, at run
time. It reports what the source *wrote*, never what was inferred, so an
unannotated parameter's type is `reflect.TypeRef.Unknown`:

```mah
import reflect from "std:reflect"

## Adds two numbers.
fn add(a: Number, b: Number = 1) -> Number { a + b }

## A user.
struct User {
    ## Their name.
    name: String,
    tags: Vector<String>
}

impl User {
    fn greet(self) -> String { "hi " + self.name }
}

let sig = reflect.signature(add)
print(sig.name, sig.doc)                     # add Adds two numbers.
print(sig.params[1].has_default, sig.params[1].default)   # true some(1)
match sig.params[0].type {
    reflect.TypeRef.Named { type: t, args: args } => { print(t == Number) }     # true
    _ => { }
}
match reflect.schema(User) {
    some(reflect.Schema.Struct { type: t, doc: doc, type_params: tps, fields: fields, decorators: ds }) => {
        print(doc, fields.len(), fields[0].doc)         # A user. 2 Their name.
    }
    _ => { }
}
let u = reflect.construct(User, ["name": "ada", "tags": []])
print(reflect.type_of(u) == User, reflect.type_of(3), reflect.methods(User)[0].name)   # true Number greet
print(reflect.implements(User, "Printable"), reflect.call(add, [1], ["b": 5]))         # false 6
```

- `type_of(v)` (`type_of(none)` is `None`), `signature(f)` (`name`, `doc`,
  `type_params`, `params` -- each with `name`, `type`, `doc`, `has_default`,
  and `default`, `some(value)` when the default is a literal -- `returns`,
  `throws`, an `Option` that is `none` when there's no `throws` clause),
  `schema(T)` (`reflect.Schema.Struct { type, doc, type_params, fields, decorators }`
  or `reflect.Schema.Enum { ..., variants }`; `none` for a primitive), `methods(T)`
  (`reflect.Method { name, function, is_method, trait_name }`, inherent ones first;
  the built-in types' native methods aren't listed), `implements(T,
  "Trait")` (matching the trait's declared name, whichever module declares
  it), `call(f, args = [], kwargs = [:])` (`f(...args, **kwargs)`),
  `construct(T, fields)` and `construct_variant(T, "Variant", fields)`
  (throw `reflect.ReflectError` for a missing or unknown field).
- A `reflect.TypeRef` is `Unknown`, `Named { type, args }`, `Fn { params, returns,
  throws }`, `Param { name }`, `SelfType`, `Never` or `Trait { name, args }`.
  Every `decorators` field is the Vector of that declaration's decorators (see Decorators).
- `Signature`, `Param`, `Field`, `Variant`, `Schema`, `Method`, `TypeRef` and
  `ReflectError` are exported by `std:reflect`: write `reflect.TypeRef`,
  `reflect.Schema.Struct { ... }`, `reflect.ReflectError`, ...

## Decorators

`@name` or `@name(args)` (also `@lib.name(...)`) written above a top-level
`fn`/`struct`/`enum` (and above `export`), an `impl` method, or before a
parameter, struct field or enum variant attaches a value to it. The
expression is evaluated **once, at startup**, and kept in source order;
nothing acts on it unless its type implements a hook trait (see Hooks); `std:reflect` returns them as the
`decorators` Vector of a `Signature`, `Param`, `Schema`, `Field` or
`Variant`. `reflect.find(decorators, Route)` is the first one whose type is
`Route`; `reflect.find(decorators, my_fn)` the first that is the same function.

```mah
import reflect from "std:reflect"

struct Route { method: String, path: String }

fn get(path: String) -> Route { Route { method: "GET", path: path } }
fn tag(name: String) -> String { "tag:" + name }

## Fetch one user.
@get("/users/{id}")
fn get_user(@tag("path") id: Number, verbose: Bool = false) -> Number { id }

@tag("model")
struct User { @tag("json:user_name") name: String, age: Number }

let sig = reflect.signature(get_user)
print(reflect.find(sig.decorators, Route))   # some(Route { method: GET, path: /users/{id} })
print(sig.params[0].decorators)              # [tag:path]
```

- Each module's decorators run in one phase before that module's first
  statement (other than declarations), after every function exists; a
  module's imports have already run. A decorator may call any function or
  use a type or literal, but may not name a top-level `let` of **its own**
  module (compile error: it hasn't run yet); another module's `let`s are fine.
- A factory that reads its own module's top-level `let` sees `none` when used
  on that same module's declarations (the `let`s haven't run yet); an importing
  module sees the initialized value. Keep constants inside the factory, or in a
  function that returns them. A decorator may name a function or type declared
  later in the module.
- Put the `##` doc comment above the decorator lines. Decorators of a
  `fn`/`struct`/`enum`/method go one per line above it; a parameter's, field's
  or variant's stay inline (`@a @b(1) name: T`).
- A decorator anywhere else is a compile error.

## Hooks

A decorator **changes behavior** when its type implements a hook trait from
`std:reflect`: `reflect.WrapFn`, `reflect.WrapParam`, `reflect.WrapField` or
`reflect.WrapStruct`. A decorator is either a value of a struct/enum type (a
factory like `@retry(3)` returns one) or a function that has the impl
(`impl reflect.WrapFn for log`). A program with any decorator imports
`std:reflect` implicitly; you only write the import to name the traits.

```mah
import reflect from "std:reflect"

# WrapFn: replace the function (`f` is the function so far, `info.name` its name)
fn log(f, info: reflect.FnInfo) {
    fn(...args, **kw) {
        print("call", info.name)
        f(...args, **kw)
    }
}
impl reflect.WrapFn for log { fn wrap(self, f, info) { self(f, info) } }

@log
fn greet(name: String, punct: String = "!") { "hi " + name + punct }
print(greet("ann"))                          # call greet / hi ann!
print(reflect.signature(greet).params.len()) # 2   the wrapper reports greet's own signature

# WrapParam: transform an argument on every call, before the body runs
fn trim(value, info: reflect.ParamInfo) { value }
impl reflect.WrapParam for trim { fn transform(self, value, info) { value.trim() } }
fn shout(@trim text: String) { text.to_upper() }
print(shout("  hi "))                         # HI

# WrapField / WrapStruct: on a literal, `reflect.construct`, and `obj.field = v`
fn lower(value, info: reflect.FieldInfo) { value }
impl reflect.WrapField for lower { fn set(self, value, info) { value.to_lower() } }
struct Adult {}
impl reflect.WrapStruct for Adult {
    fn construct(self, value, info) {
        if value.age < 18 { throw RuntimeError.ArgumentError { message: "too young" } }
        value
    }
}
fn adult() -> Adult { Adult {} }

@adult()
struct Member { @lower email: String, age: Number }
let m = Member { email: "ANN@X.COM", age: 30 }
m.email = "BOB@X.COM"
print(m.email)                               # bob@x.com
```

- `wrap(self, f, info)` gets the function (already wrapped by the decorators
  closer to it) and `reflect.FnInfo { name, function }` (`function` is the
  original) and returns the function that takes its place, which must be a
  function (`ArgumentError`: "WrapFn.wrap must return a function", at startup).
  A wrapper for a method receives the object as its first argument
  (`fn(...args, **kw)` is `args[0]`).
- `transform(self, value, info)` gets the bound argument (a Vector or Map for
  a rest parameter) and `reflect.ParamInfo { name, index, function }`.
  `set(self, value, info)` gets `reflect.FieldInfo { name, type }` (`type` is
  the struct) and `construct(self, value, info)` `reflect.TypeInfo { type }`;
  both return the value to use (`construct` may return a different value, or
  throw to reject it). Field hooks run before struct hooks, a throw
  propagates to the call, literal or assignment, and hooks may `await`.
- Several hooks on one target run **closest to the declaration first**, each
  receiving the previous result. A decorator that implements no hook trait
  only attaches its value, as before.
- The wrapped function keeps the original's identity: `reflect.signature`,
  its decorators and its parameter hooks, and `impl Tr for greet` methods
  all still work; `reflect.find(decorators, greet)` finds `greet` even after
  it was wrapped. Hooks never change what the type checker thinks.
- Not available: hooks on enums, variants or nested functions, and field *read*
  hooks.

## Errors

`throw` raises a value -- any struct/enum that `impl`s the built-in `Error`
trait -- as an error; it unwinds to the nearest enclosing `try` (or up to the
program itself). `try { ... } catch { arms }`'s arms are `match` arms, plus
one new pattern kind, `name: Type`/`_: Type` ("type-test"), which matches
any instance of that type. An arm that doesn't match re-throws the error to
whatever encloses this `try`. `try EXPR else FALLBACK` catches everything
and evaluates to `FALLBACK`.

```mah
enum ParseError { Empty, BadDigit }
impl Error for ParseError {
    fn message(self) {
        match self {
            ParseError.Empty => { "empty input" }
            ParseError.BadDigit => { "not a digit" }
        }
    }
}

fn first_digit(s) {
    if s.len() == 0 { throw ParseError.Empty }
    let c = s.char_at(0)
    if c < "0" | c > "9" { throw ParseError.BadDigit }
    c
}

fn safe_first_digit(s) {
    try { first_digit(s) } catch { ParseError.Empty => { "was empty" } }   # BadDigit: re-thrown
}

let a = try { safe_first_digit("x") } catch { e: ParseError => { "other: " + e.message() } }
let b = try first_digit("x") else "?"          # try/else shorthand
print(a, b)                                    # other: not a digit ?
print(try { 1 / 0 } catch { e: RuntimeError => { e.message } })   # Division by zero

fn load(s: String) -> String throws ParseError { first_digit(s) }   # checked: must cover the body
let run: fn(String) -> String throws never = fn(s) { s }   # fn types take `throws` too
```

Every VM-raised failure (division by zero, a bad method call, an
out-of-range index, a `match` with no matching arm, ...) is a value of the
built-in `RuntimeError` enum -- catchable the same way as any other error
(`RuntimeError.DivisionByZero { message } =>`, or a type-test `e:
RuntimeError =>`). `Error`'s default `message()` is the value's own
`to_string()`; override it (as above) for a nicer message. `defer`red
blocks still run, in order, while an error unwinds past them. An error
nothing catches stops the program with a message and source location.

The type checker infers what every function can throw (its **error set**),
with no annotation: a function throws what its body `throw`s plus whatever
the functions it calls throw, minus what a `try` around them fully handles
(`e: T`, `_`/`e`, or arms for every variant of an enum; a single variant or
a guarded arm handles only part of a type, so the type stays). Hover shows
it (`fn first_digit(s: String) -> String throws ParseError`). An error that
can reach the top of the program is reported as `Unhandled error: T` (a
warning in `loose` mode, an error in `strict`/`explicit`), and a `catch`
arm naming a type the body never throws is a warning. `RuntimeError`s are
catchable but never tracked. A function or function type may declare its
error set with `throws A | B` (or `throws never`); leaving it out means
"inferred", and a written one must cover everything the body can throw.
Only values whose type implements `Error` can be thrown or listed after
`throws`.
Compile errors (syntax, undefined names, wrong struct fields) are reported
before anything runs.

## Testing

Tests live in `*.test.mh` files and run with `mah test`. A test file holds
only declarations (`import`, `fn`, `let`, `struct`, `enum`, `trait`,
`impl`) and `test "name" { ... }` blocks; `std:test` has the assertions:

```mah
# src/calc.test.mh
import "std:test"

fn add(a, b) { a + b }

test "adds two numbers" {
    assert_eq(add(2, 3), 5)
    assert(add(1, 1) > 1, "should grow")
}

test "division by zero throws" {
    let e = assert_throws(fn() { 1 / 0 })
    assert_eq(e.message, "Division by zero")
}

test "not ready yet" {
    skip("needs real data")
}
```

| `std:test` | |
|---|---|
| `assert(cond, message = "")` | fails unless `cond` is truthy |
| `assert_eq(actual, expected, message = "")` | fails unless `actual == expected`; the report shows both |
| `assert_ne(a, b, message = "")` | fails if `a == b` |
| `assert_throws(f)` | calls `f`, returns what it threw; fails if it returned normally |
| `fail(message)` / `skip(reason = "")` | fail now / report the test as skipped |

- `mah test` finds every `*.test.mh` under the project (not `build/` or
  hidden directories) and runs each test in a fresh VM, so tests can't see
  each other's state. `mah test NAME` runs the tests whose name (or
  `file::name`) contains NAME; `--file`, `--vm` and `--timeout MS` also work.
- A failure is reported at the line of your failing assertion (or, for
  any other error, with a stack trace), plus whatever the test printed.
- `x.test.mh` importing `x.mh` from the same directory sees all of
  `x.mh`'s top-level names, exported or not. Nothing else does.
- A test body may throw anything and may `.await`; timers still pending
  when it finishes are cancelled, with a warning.
- `==` compares Vectors, Maps, structs and enums **by identity**, so
  `assert_eq([1], [1])` fails; compare their parts, or `len()`s.
- `test` is only a keyword at the top level of a test file, right before a
  string. `mah run`/`mah build` refuse a test file, and nothing can import one.

## Not available (don't use these)

- Tuples, built-in sets (use `std:collections`' `Set`), assigning to a slice (`v[1..3] = ...`),
  `collect` (use `reduce()`), `for_each`/`count`, Vector `insert`/`remove`/`sort`/`contains`,
  assigning into a String (`s[0] = "x"`: Strings are immutable).
- `for x in ...` without `let`, C-style `for (i = 0; ...)`, destructuring in
  `for` bindings, labeled `break`/`continue`.
- String formatting/interpolation (use `+`,
  `pad_start`, and friends).
- `&&`, `||`, `+=`, `++`, ternary `?:`.
- Type-checking trait-typed values, bounds, and the prelude's iterator methods (the checker skips these for now); classes/inheritance.
- `*args` (use `...args`, see Rest parameters), rest parameters on an `extern fn`
  or with a default; only `print` takes any number of arguments without one.
- Hooks for enums or variants, field *get* hooks, and `impl` for a nested
  `fn` or closure (only top-level `fn`s and types are impl targets).
- Decorators on `let`s, traits, `impl` blocks, nested functions or closures.
- UDP, HTTP/2, WebSockets, spawning a process to stream from, and every other planned
  `std:` module besides `std:math`, `std:path`, `std:json`, `std:csv`,
  `std:random`, `std:collections`, `std:regex`, `std:time`, `std:async`,
  `std:bytes`, `std:fs`, `std:process`, `std:socket`, `std:reflect` and `std:test`. A Bytes literal. Time zones (`std:time` is UTC only).
- `null`/`nil`/`undefined`: use `none`.
