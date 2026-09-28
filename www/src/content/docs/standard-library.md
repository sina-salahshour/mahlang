---
title: Standard library
order: 17
section: Language
---

The standard library is a set of modules that ship with Mah. You import
them like any other file, with a `std:` path, either namespaced or flat:

```mah
import math from "std:math"      # math.sqrt(2), math.pi
```

```mah
import "std:math"                # sqrt(2), pi
print(sqrt(16), round(pi, 2))    # 4 3.14
```

`std:` names are reserved: they never refer to a file of yours, and an
unknown one (`import "std:nope"`) is a compile error. Errors inside a
standard library module are located as `std:math#20:9`, and in your
editor, hover and go-to-definition work on its functions like on your own.

So far there is one module, `std:math`. The rest of the plan (json, csv,
fs, process, random, time, async, regex, collections, sockets, http) is
in `docs/STDLIB.md` in the repository.

## `std:math`

```mah
import math from "std:math"

print(math.sqrt(2))                          # 1.414213562373095048801688724
print(math.round(math.pi, 4), math.floor(0 - 2.5), math.ceil(2.1))   # 3.1416 -3 3
print(math.round(math.atan2(1, 1) * 180 / math.pi, 6))               # 45
print(math.log10(1000), math.min(3, 7), math.clamp(15, 0, 10))       # 3 3 10
```

| Function | |
|---|---|
| `pi`, `e` | constants, to 28 digits |
| `sqrt(x)`, `pow(x, y)`, `abs(x)` | exact on Numbers |
| `floor(x)`, `ceil(x)`, `round(x, digits = 0)` | `round` rounds halves away from zero; negative `digits` round to tens, hundreds, ... |
| `min(a, b)`, `max(a, b)`, `clamp(x, lo, hi)` | |
| `sin`, `cos`, `tan`, `asin`, `acos`, `atan`, `atan2(y, x)` | radians |
| `exp(x)`, `log(x)`, `log10(x)` | `log` is the natural logarithm |

`sqrt`, `pow` and the rounding functions work on Mah's own Numbers, so
they're exact to 28 significant digits. The trigonometric, exponential
and logarithmic functions are computed in double precision (about 16
digits).

Bad input throws a `RuntimeError.ArgumentError`, which you can catch like
any error (see [Errors](/docs/errors)):

```mah
import math from "std:math"
print(try math.log(0) else "log(0) is undefined")
print(try { math.sqrt(0 - 1) } catch { e: RuntimeError => { e.message } })   # sqrt: argument out of range
```

`sin(x)` and `cos(x)` also work with no import at all, as they always have.
They're ordinary names, though, so a function of your own called `sin`, or
a flat `import "std:math"`, takes over.

## How it's built

Each module is a Mah file inside the `mah` package (`mah/std/math.mh`).
Most of `std:math` is plain Mah. The functions that need the machine,
such as `tan` and `log`, are declared with `extern fn`, which binds a Mah
function to a native the VM provides:

```text
export extern fn tan(x: Number) -> Number = "math.tan"
```

Only standard library modules may use `extern fn`. New natives come with
a new bytecode minor version, and a compiled program is marked with the
lowest version it needs. An older runtime keeps running programs that
don't use new natives. It refuses one that does, and names the natives it
lacks.
