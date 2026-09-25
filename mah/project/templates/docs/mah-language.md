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
| `String` | `"hi\n"` | escapes `\n \t \" \\`; immutable; **no methods or indexing** |
| `Bool` | `true`, `false` | |
| `Option` | `none`, `some(x)` | Mah's null. `none` is falsy; `some(x)` is always truthy |
| `Function` | `fn(a) { a }` | first-class closures, captured by reference |
| struct / enum | user-declared | reference semantics (assigning copies the reference) |
| `Promise` | from `detach` / `sleep_async` | see Async |

**Truthiness**: `false`, `none`, `0`, and `""` are falsy; everything else is
truthy.

## Operators

Precedence, loosest to tightest. Note the two unusual rules: `&`/`|` share
one level, and `%` shares a level with `+`/`-`.

| level | operators | associativity |
|---|---|---|
| 1 | `&` (and), `\|` (or) | left, same level: `a \| b & c` is `(a \| b) & c` |
| 2 | `==` `!=` `<` `>` | left |
| 3 | `+` `-` `%` | left: `1 + 6 % 4` is `(1 + 6) % 4` = `3` |
| 4 | `*` `/` `//` | left |
| 5 | unary `-` | |
| 6 | `**` | right: `-2 ** 2` is `-(2 ** 2)` = `-4` |

- There is **no `<=`, `>=`, `!` (not), `&&`, or `||`**. Write `!(a > b)` as
  `(a > b) == false` and `a <= b` as `(a < b) | (a == b)`.
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

- Every arm body is a `{ }` block. Arms are tried top to bottom; if none
  matches it's a runtime error. There are no guards (`if` in arms).
- Struct/enum patterns must list **every** field of that struct/variant.

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

- Arrays/lists/maps, indexing (`a[0]`), `for` loops, ranges.
- String methods (`len`, `split`, `replace`, ...), string indexing.
- `<=`, `>=`, `!`, `&&`, `||`, `+=`, `++`, ternary `?:`.
- Type annotations, generics, classes/inheritance, exceptions/`try`.
- Variadic parameters (`*args`, `**kwargs`); only `print` takes any number of arguments.
- File, network, or OS access (planned as future built-ins).
- `null`/`nil`/`undefined`: use `none`.
