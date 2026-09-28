---
title: Syntax basics
order: 2
section: Language
---

## Program structure

- A program is a sequence of statements, run top to bottom. There's no
  `main` function: the entry file's top-level code *is* the program.
- Comments start with `#` and run to end of line.
- Newlines separate statements; `;` is optional between most statements
  and **required** in one case: an expression statement that isn't a
  call, a block (`if`/`match`/`{}`), or an assignment must be followed by
  `;` when more code follows on the same block. When in doubt, add `;`.
- Blocks are `{ ... }`. The **last expression without a trailing `;`** is
  the block's value (its "tail"); a block with no tail has the value
  `none`.
- `let` may reuse a name in the same scope: it declares a **new**
  variable (shadowing), and its value can still use the old one:
  `let x = 1` then `let x = "one: " + x`. Closures that captured the old
  `x` keep it. `fn` names, parameters, and pattern bindings can't be
  declared twice in one scope.

```mah
let x = 1            # declare
x = x + 1             # assign (the variable must already exist)
let y = {
    let t = x * 2
    t + 1              # tail: the block's value is 5
}
```

## Values and types

| type | literals / how you get one | notes |
|---|---|---|
| `Number` | `42`, `3.14`, `-7` | decimal numbers (28 significant digits); `10 / 4` is `2.5` |
| `String` | `"hi\n"` | escapes `\n \t \" \\`; immutable; `s.len()`, `s.char_at(i)`, `s[i]`, `s[a..b]` |
| `Bool` | `true`, `false` | |
| `Option` | `none`, `some(x)` | Mah's null. `none` is falsy; `some(x)` is always truthy |
| `Function` | `fn(a) { a }` | first-class closures, captured by reference |
| struct / enum | user-declared | reference semantics (assigning copies the reference) |
| `Promise` | from `detach` / `sleep_async` | see [Async](/docs/async) |
| `Vector` | `[1, 2, 3]`, `[]` | growable zero-indexed list, by reference |
| `Map` | `["a": 1, "b": 2]`, `[:]` | String/Number/Bool keys, insertion-ordered, by reference |
| `Range`, `FromRange`, `ToRange` | `5..10`, `1..`, `..10` | built-in structs, see [Iterators & ranges](/docs/iterators-ranges) |

**Truthiness**: `false`, `none`, `0`, and `""` are falsy; everything else
is truthy.

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

- `!x` is `true` when `x` is falsy and `false` otherwise. There is no
  `&&` or `||`: use `&` and `|`.
- `<`, `>`, `<=`, `>=` compare two Numbers or two Strings; anything else
  is a runtime error.
- `&`/`|` evaluate **both** sides (no short-circuit) and return a Bool.
- `+` with a String on either side concatenates, converting the other
  side to text: `"n = " + 5` is `"n = 5"`. `"ab" * 3` is `"ababab"`.
- `//` truncates toward zero; `%` takes the sign of the left operand.
- `==` compares Numbers/Strings/Bools/`none` by value and everything
  else (structs, enums, `some(..)`, functions, Vectors, Maps) by
  identity: `[1] == [1]` is `false`. Different types are never equal
  (`true == 1` is `false`).
- Parenthesize whenever you're unsure.

## Built-ins

| call | meaning |
|---|---|
| `print(a, b, ..., sep: " ", end: "\n")` | prints the arguments separated by `sep` (default: a space), then `end` (default: a newline). `print()` prints just a newline |
| `input()` | reads an integer from stdin (skips non-digits until one) |
| `sin(x)`, `cos(x)` | radians; the rest of the math is in [`std:math`](/docs/standard-library) |
| `sleep_async(ms)` | pauses (see [Async](/docs/async)) |

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
- `for let value in iterable { }` loops over any Iterable (ranges,
  Strings, `map`/`filter`/... results, your own types). `for let value,
  let index in ... { }` also binds the 0-based index. Both need `let`;
  `for x in ...` is a syntax error. The bindings only exist inside the
  loop body.
- `break`/`continue` apply to the innermost `while`/`for`. `break value`
  ends the loop and makes `value` the loop's value; a loop that ends any
  other way (condition false, iterable exhausted, bare `break`) has the
  value `none`. The value must start on the same line as `break`.
- Conditions need no parentheses. A struct literal can't appear bare in
  an `if`/`while`/`match` head or after `for ... in`; wrap it in
  parentheses: `if (Point { x: 1, y: 2 }.x > 0) { }`.

## What's not there (on purpose)

Tuples, sets, `&&`/`||`, `+=`/`++`, ternary `?:`, C-style `for`,
labeled `break`/`continue`, and exceptions/`try` don't exist. See each
topic page for what a construct is replaced with (e.g. `&`/`|` instead
of `&&`/`||`, `match` instead of `try`).
