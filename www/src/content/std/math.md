---
title: std:math
order: 1
section: Basics
summary: Constants, rounding, min/max/clamp, square roots and powers, trigonometry, exponentials and logarithms.
---

# `std:math`

Numeric functions beyond the operators: rounding, roots and powers,
trigonometry, exponentials and logarithms, plus the constants `pi` and
`e`.

```mah
import math from "std:math"

print(math.sqrt(2))                                    # 1.414213562373095048801688724
print(math.round(math.pi, 4), math.floor(-2.5), math.ceil(2.1))   # 3.1416 -3 3
print(math.clamp(15, 0, 10), math.max(3, 7))           # 10 7
```

## Importing

Import it namespaced (`math.sqrt`) or flat, which is handy for formula-heavy
code:

```mah
import "std:math"

fn hypot(a: Number, b: Number) -> Number { sqrt(a ** 2 + b ** 2) }
print(hypot(3, 4), round(pi * 2, 3))                   # 5 6.283
```

`sin(x)` and `cos(x)` also work with no import at all. They're ordinary
names, though, so a function of your own called `sin`, or a flat
`import "std:math"`, takes over.

## Precision: two kinds of function

Mah's `Number` is a decimal with 28 significant digits, so `0.1 + 0.2 == 0.3`.
`std:math` keeps that exactness wherever it can:

- **Exact** (28 digits, written in Mah): `sqrt`, `pow`, `abs`, `floor`,
  `ceil`, `round`, `min`, `max`, `clamp`, and the constants.
- **Double precision** (about 16 digits, computed by a native): `sin`,
  `cos`, `tan`, `asin`, `acos`, `atan`, `atan2`, `exp`, `log`, `log10`.

```mah
import math from "std:math"

print(math.pow(1.1, 2), math.sqrt(1.21))       # 1.21 1.1
print(math.round(math.sin(math.pi / 6), 10))   # 0.5
```

Round a double-precision result (`round(x, 10)`) before comparing it
with `==`.

## Rounding

`round(x, digits = 0)` rounds halves **away from zero**, so `round(2.5)` is
3 and `round(-2.5)` is -3 (not banker's rounding). Positive `digits` keep
that many decimal places; negative ones round to tens, hundreds, ...

```mah
import math from "std:math"

print(math.round(2.5), math.round(-2.5), math.round(1234.5678, 2))   # 3 -3 1234.57
print(math.round(1234, -2), math.floor(-0.5), math.ceil(-0.5))        # 1200 -1 0
```

`floor` and `ceil` round toward negative and positive infinity. For
"drop the fractional part" use `//` (integer division) or
`x - x % 1`.

## Angles

Trigonometric functions work in **radians**. Convert degrees with
`pi / 180`. `atan2(y, x)` gives the angle of the point `(x, y)`, correct in
every quadrant and when `x` is 0, which `atan(y / x)` isn't:

```mah
import math from "std:math"

fn degrees(rad: Number) -> Number { math.round(rad * 180 / math.pi, 6) }
print(degrees(math.atan2(1, 1)), degrees(math.atan2(1, -1)))   # 45 135
print(degrees(math.atan2(-1, 0)))                               # -90
```

## Errors

Input outside a function's domain (`sqrt(-1)`, `log(0)`, `asin(2)`) throws
a `RuntimeError.ArgumentError`, which you can catch like any error:

```mah
import math from "std:math"

print(try math.log(0) else "log(0) is undefined")
print(try { math.sqrt(-1) } catch { e: RuntimeError => { e.message } })   # sqrt: argument out of range
```

## Reference

| Name | |
|---|---|
| `pi` | 3.141592653589793238462643383 |
| `e` | 2.718281828459045235360287471, the base of `exp` and `log` |
| `sqrt(x)` | the square root; `x` must not be negative |
| `pow(x, y)` | `x ** y` |
| `abs(x)` | `x` without its sign |
| `floor(x)` | the largest whole number not greater than `x` |
| `ceil(x)` | the smallest whole number not less than `x` |
| `round(x, digits = 0)` | rounded to `digits` places, halves away from zero |
| `min(a, b)`, `max(a, b)` | the smaller / larger |
| `clamp(x, lo, hi)` | `x` limited to `lo..=hi` |
| `sin(x)`, `cos(x)`, `tan(x)` | of `x` radians |
| `asin(x)`, `acos(x)` | in radians; `x` must be in `-1..=1` |
| `atan(x)` | in radians |
| `atan2(y, x)` | the angle of `(x, y)` from the positive x axis |
| `exp(x)` | `e ** x` |
| `log(x)`, `log10(x)` | natural and base-10 logarithms; `x` must be positive |

For the minimum or maximum of a whole Vector, use `reduce`:
`xs.reduce(fn(a, b) { math.max(a, b) })` (see
[Iterators & ranges](/docs/iterators-ranges)).
