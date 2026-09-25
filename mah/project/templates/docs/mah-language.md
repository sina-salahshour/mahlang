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
- Comments start with `#` and run to end of line.
- Newlines separate statements; `;` is optional between most statements and
  **required** in one case: an expression statement that isn't a call, a
  block (`if`/`match`/`{}`), or an assignment must be followed by `;` when
  more code follows on the same block. When in doubt, add `;`.
- Blocks are `{ ... }`. The **last expression without a trailing `;`** is the
  block's value (its "tail"); a block with no tail has the value `none`.

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
| `String` | `"hi\n"` | escapes `\n \t \" \\`; immutable; only `s.len()` and `s.char_at(i)` (no indexing); iterable, see Iterators |
| `Bool` | `true`, `false` | |
| `Option` | `none`, `some(x)` | Mah's null. `none` is falsy; `some(x)` is always truthy |
| `Function` | `fn(a) { a }` | first-class closures, captured by reference |
| struct / enum | user-declared | reference semantics (assigning copies the reference) |
| `Promise` | from `detach` / `sleep_async` | see Async |
| `Range`, `FromRange`, `ToRange` | `5..10`, `1..`, `..10` | built-in structs, see Ranges |

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

- `!x` is `true` when `x` is falsy (see Truthiness) and `false` otherwise.
  There is no `&&` or `||`: use `&` and `|`.
- `<`, `>`, `<=`, `>=` compare two Numbers or two Strings; anything else is
  a runtime error.
- `&`/`|` evaluate **both** sides (no short-circuit) and return a Bool.
- `+` with a String on either side concatenates, converting the other side
  to text: `"n = " + 5` is `"n = 5"`. `"ab" * 3` is `"ababab"`.
- `//` truncates toward zero; `%` takes the sign of the left operand.
- `==` compares Numbers/Strings/Bools/`none` by value and everything else
  (structs, enums, `some(..)`, functions) by identity. Different types are
  never equal (`true == 1` is `false`).
- Parenthesize whenever you're unsure.

## Built-ins

| call | meaning |
|---|---|
| `print(a, b, ..., sep: " ", end: "\n")` | prints the arguments separated by `sep` (default: a space), then `end` (default: a newline). `print()` prints just a newline |
| `input()` | reads an integer from stdin (skips non-digits until one) |
| `sin(x)`, `cos(x)` | radians |
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
```

- `if`/`elif`/`else` and `match` are **expressions**: `let s = if ok { "y" }
  else { "n" }`. An `if` with no `else` whose condition is false yields `none`.
- `while cond { }` is the only loop. `break`/`continue` apply to the
  innermost `while`. **There is no `for` loop.**
- Conditions need no parentheses. A struct literal can't appear bare in an
  `if`/`while`/`match` head; wrap it in parentheses:
  `if (Point { x: 1, y: 2 }.x > 0) { }`.

## Functions and closures

```mah
fn add(a, b) { a + b }             # tail expression is the return value
fn early(n) {
    if n < 0 { return "negative" }
    return                          # bare return gives none
}
let twice = fn(f, x) { f(f(x)) }   # anonymous function value
print(twice(fn(v) { v * 2 }, 3))   # 12

fn counter() {
    let n = 0
    fn() { n = n + 1; n }           # captures n by reference
}
let c = counter()
c()
print(c())                          # 2
```

- Recursion works.

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

- Every arm body is a `{ }` block. Arms are tried top to bottom; if none
  matches it's a runtime error. There are no guards (`if` in arms).
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
  `...Iterator` types).
- A range's end must be on the same line as its `..`: `let f = 1..` at the
  end of a line is an open-ended `FromRange`, and the next line is a new
  statement.
- Every **`Iterable`** (ranges except `ToRange`, Strings, and your own types
  that implement it) has these methods, all lazy except `reduce`:
  - `map(f)`: `f(value)` or `f(value, index)` gives each new item.
  - `filter(f)`: keeps items where `f(value)` or `f(value, index)` is truthy.
  - `skip(n)`, `take(n)`: skip the first `n` items, or stop after `n`.
  - `reduce(f, initial)`: like JavaScript: `f(acc, value)` or `f(acc,
    value, index)`. Without `initial` the first item starts the
    accumulator, and an empty Iterable gives `none`.
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

- There's no `for` loop yet: consume iterables with `reduce`, or with
  `while` and `next()`.

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
  `Function`, `Option`, `Promise`.
- The impl must define every required method, with the same parameters.
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
```

- `value.await` waits for a Promise. A Promise is an enum:
  `Promise.Pending` or `Promise.Settled { value }`.
- The program exits once the main code is done **and** no detached work
  is still pending.

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

Only `fn`/`let` names marked `export` are visible to importers. Paths are
relative to the importing file. `struct`, `enum`, and `trait` declarations
are global across all imported files and need no `export`.

## Errors

There's no exception handling yet. A runtime error (calling a missing
method, a type mismatch like `1 + true`, division by zero, a `match` with
no matching arm) stops the program with a message and source location.
Compile errors (syntax, undefined names, wrong struct fields) are reported
before anything runs.

## Not available (don't use these)

- Arrays/lists/maps, indexing (`a[0]`), `for` loops, `collect`/`for_each`/`count`.
- String methods other than `len`/`char_at` (no `split`, `replace`, ...), string indexing.
- `&&`, `||`, `+=`, `++`, ternary `?:`.
- Type annotations, generics, classes/inheritance, exceptions/`try`.
- Variadic parameters (`*args`, `**kwargs`); only `print` takes any number of arguments.
- File, network, or OS access (planned as future built-ins).
- `null`/`nil`/`undefined`: use `none`.
