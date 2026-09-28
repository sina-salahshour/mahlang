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
unknown one (`import "std:nope"`) is a compile error. An uncaught error
from inside a standard library function is reported at your call to it,
and in your editor, hover and go-to-definition work on its functions like
on your own (showing locations like `std:math#20:9`).

So far there are `std:math`, `std:path`, `std:json` and `std:csv` (and
`std:test`, see [Testing](/docs/testing)). The rest of the plan (fs,
process, random, time, async, regex, collections, sockets, http) is in
`docs/STDLIB.md` in the repository.

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

## `std:path`

Paths as Strings: nothing here touches the file system. Input may use `/`
or `\` and start with a drive (`C:`); `normalize` and `relative` always
answer with `/`.

```mah
import path from "std:path"

print(path.join("src", "main.mh"), path.join_all(["a", "b", "c"]))   # src/main.mh a/b/c
print(path.dirname("a/b/c.txt"), path.basename("a/b/c.txt"))        # a/b c.txt
print(path.extension("a.tar.gz"), path.stem("a.tar.gz"))            # .gz a.tar
print(path.normalize("a/./b/../c//d/"), path.normalize("/../a"))    # a/c/d /a
print(path.relative("/a/b", "/a/c/d"), path.is_absolute("C:\\x"))   # ../c/d true
```

| Function | |
|---|---|
| `join(a, b)`, `join_all(parts)` | an absolute `b` replaces `a`; no normalizing |
| `dirname(p)`, `basename(p)` | `"."` / `""` when there's nothing to give |
| `extension(p)`, `stem(p)` | from the last `.`; `.bashrc` has no extension |
| `normalize(p)` | removes `.`, repeated separators and `x/..`; never climbs above a root |
| `is_absolute(p)` | starts with a separator, or a drive and a separator |
| `relative(from, to)` | the path from directory `from` to `to` |

## `std:json`

```mah
import json from "std:json"

let config = try json.parse("{\"name\": \"mah\", \"tags\": [1, 2], \"debug\": null}") else [:]
print(config["name"], config["tags"][1], config["debug"])    # mah 2 none
print(try json.stringify(config) else "")                    # {"name":"mah","tags":[1,2],"debug":null}
print(try json.stringify(["a": [1]], indent: 2) else "")
# {
#   "a": [
#     1
#   ]
# }
```

`parse(text)` turns objects into Maps (keys in document order), arrays
into Vectors, and `null` into `none`. It follows the JSON standard
strictly: no comments or trailing commas. `stringify(value, indent = 0)`
writes compact JSON, or with `indent` spaces per level. It also writes
structs, as objects of their fields, and enum values: a unit variant is
its name (`"Empty"`), any other an object holding one key, the variant's
name (`{"Circle": {"r": 2}}`). `some(x)` is written as `x`.

Both throw `JsonError`: `JsonError.Syntax { message, line, column }` when
the text isn't JSON, and `JsonError.Shape { message }` for a value JSON
can't hold (a function, or a Vector that contains itself).

```mah
import json from "std:json"

print(try { json.parse("[1, 2,]") } catch { e: JsonError => { e.message() } })
# expected a value, found ']' at line 1, column 7
```

To read JSON into your own types, implement `FromJson`. The helpers
`field(object, name)`, `as_number`, `as_string`, `as_bool`, `as_vector`
and `as_map` check the shape as they go, throwing `JsonError.Shape`:

```mah
import json from "std:json"

struct Point { x: Number, y: Number }

impl FromJson for Point {
    fn from_json(value) {
        Point { x: json.as_number(json.field(value, "x"), "x"), y: json.as_number(json.field(value, "y"), "y") }
    }
}

let p = try Point.from_json(json.parse("{\"x\": 1, \"y\": 2}")) else Point { x: 0, y: 0 }
print(p.x + p.y)                                               # 3
print(try { Point.from_json(json.parse("{\"x\": 1}")) } catch { e: JsonError => { e.message() } })
# missing field 'y'
```

## `std:csv`

Comma-separated values, following RFC 4180: a field in double quotes can
hold the delimiter, line breaks, and `""` for a quote.

```mah
import csv from "std:csv"

let rows = try csv.parse("name,city\nal,\"Paris, FR\"\n") else []
print(rows[1][1])                                     # Paris, FR
let people = try csv.parse_records("name,age\nal,3\nbo,4\n") else []
print(people[1]["name"], people.len())                # bo 2
print(try csv.stringify([["a", "b,c"], ["1", "say \"hi\""]]) else "")
# a,"b,c"
# 1,"say ""hi"""
```

| Function | |
|---|---|
| `parse(text, delimiter = ",")` | `Vector<Vector<String>>`, one Vector per row |
| `parse_records(text, delimiter = ",")` | `Vector<Map<String, String>>`, keyed by the first row |
| `stringify(rows, delimiter = ",")` | quotes only the fields that need it; `none` is an empty field |
| `stringify_records(records, delimiter = ",", columns = none)` | a header row (`columns`, or the first record's keys), then one row per Map |
| `column(row, name)` | `row[name]`, for `FromCsvRow` impls |

Every field is read as a String (convert with `to_number()`). Lines end
with `\n` or `\r\n`, and blank lines are skipped. Errors are a `CsvError
{ message, line }`, such as a quote that's never closed or a record with
the wrong number of fields. `FromCsvRow` works like `FromJson`, with
`from_csv_row(row)` taking a record from `parse_records`.

## How it's built

Each module is a Mah file inside the `mah` package (`mah/std/math.mh`).
Most of `std:math` is plain Mah. The functions that need the machine,
such as `tan` and `log`, are declared with `extern fn`, which binds a Mah
function to a native the VM provides:

```text
export extern fn tan(x: Number) -> Number = "math.tan"
```

`std:path` needs no natives, and `std:json` and `std:csv` are Mah too,
over a handful of natives for reading a value's type and fields and a
String's characters.

Only standard library modules may use `extern fn`. New natives come with
a new bytecode minor version, and a compiled program is marked with the
lowest version it needs. An older runtime keeps running programs that
don't use new natives. It refuses one that does, and names the natives it
lacks.
