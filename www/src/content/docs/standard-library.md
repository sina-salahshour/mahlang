---
title: Standard library
order: 18
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

A module's types are exported and reached like its functions: with a
namespaced import, `json.JsonError`, `fs.FsError`, `process.Output`,
`regex.Regex`; the ones in a flat import (`import "std:collections"`) are
written bare (`Set`). They are module-scoped, so your own `struct Field` or
`struct Match` never clashes with a module's.

So far there are `std:math`, `std:path`, `std:json`, `std:csv`,
`std:random`, `std:collections`, `std:regex`, `std:time`, `std:async`,
`std:fs`, `std:process` and `std:reflect` (and `std:test`, see
[Testing](/docs/testing)).
The rest of the plan (sockets, http) is in `docs/STDLIB.md` in the
repository.

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

Both throw `json.JsonError`: `json.JsonError.Syntax { message, line, column }` when
the text isn't JSON, and `json.JsonError.Shape { message }` for a value JSON
can't hold (a function, or a Vector that contains itself).

```mah
import json from "std:json"

print(try { json.parse("[1, 2,]") } catch { e: json.JsonError => { e.message() } })
# expected a value, found ']' at line 1, column 7
```

To read JSON into your own types, implement `json.FromJson`. The helpers
`field(object, name)`, `as_number`, `as_string`, `as_bool`, `as_vector`
and `as_map` check the shape as they go, throwing `json.JsonError.Shape`:

```mah
import json from "std:json"

struct Point { x: Number, y: Number }

impl json.FromJson for Point {
    fn from_json(value) {
        Point { x: json.as_number(json.field(value, "x"), "x"), y: json.as_number(json.field(value, "y"), "y") }
    }
}

let p = try Point.from_json(json.parse("{\"x\": 1, \"y\": 2}")) else Point { x: 0, y: 0 }
print(p.x + p.y)                                               # 3
print(try { Point.from_json(json.parse("{\"x\": 1}")) } catch { e: json.JsonError => { e.message() } })
# missing field 'y'
```

Or let `decode` read it from the struct's declaration. `decode(T, value)`
(and `parse_as(T, text)`, which is `decode(T, parse(text))`) checks a
parsed value against the annotations and builds the struct, through
`std:reflect` (below). It handles `Number`, `String`, `Bool`,
`Vector<X>`, `Map<String, X>`, `Option<X>` (a missing key is `none`), other
structs, and enums (the way `stringify` writes them). A field without an
annotation takes the value as it is, and a type that implements
`json.FromJson` is read by its own `from_json`. Errors say where:

```mah
import json from "std:json"

struct User { name: String, age: Number, email: Option<String>, tags: Vector<String> }

let u = try json.parse_as(User, "{\"name\": \"ada\", \"age\": 36, \"tags\": [\"x\"]}") else none
print(u.name, u.age + 1, u.email, u.tags)     # ada 37 none [x]

try {
    json.parse_as(User, "{\"name\": \"a\", \"age\": \"old\", \"tags\": []}")
} catch {
    e: json.JsonError => { print(e.message()) }    # expected a Number for User.age, got String
}
print(try { json.parse_as(User, "{}") } catch { e: json.JsonError => { e.message() } })
# missing field 'name' for User
```

The path in a message is the type's name, then `.field`, `[i]` or
`["key"]`. `json.decode(Vector, [1, "a"])` (no type arguments) passes the
items through untouched.

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
| `column(row, name)` | `row[name]`, for `csv.FromCsvRow` impls |

Every field is read as a String (convert with `to_number()`). Lines end
with `\n` or `\r\n`, and blank lines are skipped. Errors are a `csv.CsvError
{ message, line }`, such as a quote that's never closed or a record with
the wrong number of fields. `csv.FromCsvRow` works like `json.FromJson`, with
`from_csv_row(row)` taking a record from `parse_records`.

## `std:random`

```mah
import random from "std:random"

random.seed(42)                     # leave out for different numbers each run
print(random.random())              # 0.08386297105988216316063699196
print(random.randint(1, 6), random.choice(["heads", "tails"]))
let deck = ["A", "K", "Q", "J"]
random.shuffle(deck)
print(deck, random.sample(deck, 2))
```

| Function | |
|---|---|
| `random()` | a Number in [0, 1) |
| `uniform(lo, hi)` | a Number in [lo, hi) |
| `randint(lo, hi)` | a whole Number from `lo` to `hi`, both included |
| `choice(v)` | one item of a non-empty Vector |
| `shuffle(v)`, `shuffled(v)` | in place, or a shuffled copy |
| `sample(v, k)` | `k` items from different positions |
| `seed(n)` | makes everything after it repeatable |

Every VM uses the same generator (xoshiro256**), so a seeded program
prints the same numbers on the Python and the Rust runtime. Without a
seed, it starts from the operating system's randomness. It isn't meant
for cryptography. `random.Rng.new(seed)` makes an independent generator with the
same methods, which is handy when one part of a program needs repeatable
numbers and the rest doesn't:

```mah
import "std:random"

let a = random.Rng.new(7)
let b = random.Rng.new(7)
print(a.randint(1, 100) == b.randint(1, 100))    # true
```

Bad arguments, like `choice([])` or `randint(5, 1)`, throw a
`RuntimeError.ArgumentError`.

## `std:collections`

`Set`, `Deque` and `PriorityQueue`, usually imported flat. Each works
with `for` and `map`/`filter`/`reduce`, and prints with its name.

```mah
import "std:collections"

let tags = Set.of(["red", "blue", "red"])
tags.add("green")
print(tags, tags.has("blue"), tags.len())             # Set[red, blue, green] true 3
print(tags.intersection(Set.of(["blue", "pink"])))    # Set[blue]

let line = Deque.of(["ana", "bo"])
line.push_front("vip")
print(line.pop_front(), line.pop_back(), line)        # vip bo Deque[ana]

let tasks = PriorityQueue.new(fn(task) { task.len() })
for let t in ["write docs", "fix", "test it"] {
    tasks.push(t)
}
print(tasks.pop(), tasks.pop(), tasks.pop())          # fix test it write docs
```

- **`Set`**: distinct values in the order they were first added. The
  values must be usable as Map keys (Strings, Numbers, Bools). `add`,
  `remove`, `has`, `len`, `union`, `intersection`, `difference`,
  `is_subset`, and `equals` (`==` on two Sets compares identity).
- **`Deque`**: push and pop at both ends in constant time: `push_front`,
  `push_back`, `pop_front`, `pop_back`, `front`, `back`, `get(i)`.
  Popping an empty one gives `none`, like `Vector.pop`.
- **`PriorityQueue`**: `pop` gives the smallest item (by `key(item)` if
  you pass a key function), and equal ones in the order they were pushed.
  For largest first, use a key that negates: `fn(x) { 0 - x }`.

## `std:regex`

```mah
import regex from "std:regex"

let date = regex.must_compile("(?<y>\\d{4})-(?<m>\\d\\d)")
let m = date.find("due 2026-09, paid 2026-10").unwrap()
print(m.text, m.start, m.group("y"))                         # 2026-09 4 2026
print(date.find_all("due 2026-09, paid 2026-10").len())      # 2
print(date.replace_all("2026-09 2027-01", "$m/$y"))          # 09/2026 01/2027
print(regex.must_compile("\\s*,\\s*").split("a , b,c"))        # [a, b, c]
print(regex.must_compile("cat", "i").replace_all("Cat CAT", fn(m) { m.text.to_lower() }))  # cat cat
```

Patterns use the familiar syntax, restricted to what every runtime
matches identically:

| | |
|---|---|
| `.` `[abc]` `[^a-z]` | any character but a newline / a class |
| `\d` `\w` `\s` (and `\D` `\W` `\S`) | ASCII digit, word character (`[0-9A-Za-z_]`), whitespace |
| `^` `$` `\A` `\z` `\b` `\B` | start, end, word boundary |
| `*` `+` `?` `{n}` `{n,}` `{n,m}` | repeats (add `?` for lazy); counts up to 1000 |
| `(x)` `(?:x)` `(?<name>x)` `a\|b` | groups and alternatives |
| `\.` `\\` `\n` `\t` | escapes: any punctuation after `\` is literal |

Flags go in `compile`'s second argument: `"i"` (ASCII letters match
either case), `"m"` (`^` and `$` at every line), `"s"` (`.` matches a
newline too). `$` without `m` is the very end of the text. Lookaround,
backreferences, and inline flags like `(?i)` aren't supported, and a
pattern that uses them is an error rather than something that behaves
differently depending on where it runs. Remember that a Mah String has
its own escapes, so the pattern `\d+` is written `"\\d+"`.

A `regex.Regex` has `is_match`, `find` (an `Option<regex.Match>`), `find_all`,
`replace` and `replace_all` (with `$1`, `$name`, `${name}`, `$$`, or a
function of the Match), and `split(text, limit = none)`. A `regex.Match` has
`text`, `start`, `end` (positions count characters), and `group(n)` or
`group(name)`, which gives `none` for a group that didn't take part.
`regex.escape(text)` makes a pattern that matches `text` literally.

`regex.compile` throws a `regex.RegexError` saying what's wrong and where, so
use it for patterns that come from outside the program. `must_compile`
is for patterns written into the program: a mistake there is a bug, so
it throws a `RuntimeError` the checker doesn't ask you to handle.

```mah
import regex from "std:regex"

let pattern = "(\\d+"
print(try { regex.compile(pattern) } catch { e: regex.RegexError => { e.message() } })
# missing ) at position 0 in "(\d+"
```

## `std:time`

```mah
import time from "std:time"

let d = time.utc(1790597925)                           # seconds since 1970, UTC
print(d)                                               # 2026-09-28T12:18:45Z
print(time.format(d, "%A %d %B %Y, %H:%M"))            # Monday 28 September 2026, 12:18
let later = time.utc(d.timestamp() + 36 * 3600)        # times are Numbers of seconds
print(time.format(later, "%a %H:%M"), time.duration_text(later.timestamp() - d.timestamp()))   # Wed 00:18 1d 12h 0m 0s
print(try time.parse("2026-02-30", "%Y-%m-%d") else "no such date")                             # no such date
```

`time.now()` is the current time and `time.monotonic()` the seconds since
the program started (use it to measure how long something takes). Both,
and every duration, are Numbers of seconds with milliseconds, so ordinary
arithmetic works on them.

A `time.DateTime` has `year`, `month`, `day`, `hour`, `minute`, `second` and
`millisecond`, plus `timestamp()`, `weekday()` (Monday is 1) and
`day_of_year()`. Make one with `time.utc(seconds)` or `time.date(year,
month, day, hour = 0, ...)`. It prints in ISO 8601, which `time.iso` and
`time.parse_iso` write and read.

| Code | | Code | |
|---|---|---|---|
| `%Y` | year, 4 digits | `%M` | minute |
| `%m` | month, 01–12 | `%S` | second |
| `%d` | day, 01–31 | `%f` | millisecond, 000–999 |
| `%H` | hour, 00–23 | `%j` | day of the year |
| `%B` `%b` | month name, full / 3 letters | `%A` `%a` | weekday name, full / 3 letters |

`parse` and `date` throw `time.TimeError` for text that doesn't match or an
impossible date (February 30). Everything is UTC for now: time zones
aren't supported yet.

## `std:async`

A Promise comes from `detach`. `std:async` combines them, and runs
timers:

```mah
import async from "std:async"

fn job(ms: Number, name: String) -> String {
    sleep_async(ms)
    name
}

print(async.all([detach job(30, "a"), detach job(10, "b")]))           # [a, b]
print(async.race([detach job(30, "slow"), detach job(5, "fast")]))     # fast
print(try { async.timeout(detach job(500, "x"), 20) } catch { e: async.TimeoutError => { e.message() } })   # timed out after 20 ms

let ticks = [0]
let ticker = async.set_interval(fn() { ticks[0] = ticks[0] + 1 }, 10)
async.set_timeout(fn() { async.clear_interval(ticker) }, 55)
```

- `all(promises)` gives every value, in order, once all are done, and
  throws as soon as any fails. `race(promises)` gives whichever settles
  first. `timeout(promise, ms)` gives the value if it comes in time and
  throws `async.TimeoutError` otherwise. The type checker knows each one's
  result type and what it can throw.
- These wait like any call; `detach` them to keep going meanwhile.
- `set_timeout(f, ms)` calls `f` once, and `set_interval(f, ms)` calls it
  repeatedly until `clear_interval`. An active interval keeps the program
  running; a cleared timer doesn't. Callbacks can't throw (their type is
  `fn() throws never`): handle errors inside them.

## `std:fs`

Files and directories. Every function waits like any call, but the work
happens off your program's thread, so `detach fs.read_text(path)` lets
other tasks and timers run meanwhile.

```mah
import fs from "std:fs"

try {
    let dir = fs.temp_dir()                     # a fresh temporary directory
    defer fs.remove(dir, recursive: true)
    fs.write_text(dir + "/todo.txt", "buy milk\nwalk dog\n")
    fs.append_text(dir + "/todo.txt", "call mom\n")
    let f = fs.open(dir + "/todo.txt")
    defer f.close()
    for let line, let i in f.lines() {
        print(i + 1, line)                      # 1 buy milk, 2 walk dog, 3 call mom
    }
    print(fs.list_dir(dir), fs.info(dir + "/todo.txt").size)   # [todo.txt] 27
    fs.read_text(dir + "/missing.txt")
} catch {
    e: fs.FsError => { print(e.kind, "-", e.message()) }       # not_found - read_text: no such file ...
}
```

| Function | |
|---|---|
| `read_text(path)`, `write_text(path, text)`, `append_text(path, text)` | a whole file at once |
| `exists`, `is_file`, `is_dir(path)` | Bools, never throwing |
| `info(path)` | `fs.FileInfo { kind, size, modified }`: "file", "dir" or "other"; bytes; seconds since 1970 |
| `list_dir(path)` | the names in a directory, sorted |
| `mkdir(path, parents = false)` | `parents` also makes missing directories above it |
| `remove(path, recursive = false)` | a file or an empty directory, or with `recursive` a whole tree |
| `rename(from, to)`, `copy(from, to)` | replacing a file at `to` |
| `glob(pattern)` | matching paths, sorted: `*`, `?`, `[abc]` in a name, `**` across directories |
| `temp_dir()` | a new, empty temporary directory |
| `open(path, mode = "r")` | a `fs.File` (mode "r", "w" or "a") with `read_line()` (`none` at the end), `lines()`, `read_all()`, `write(text)` and `close()` |

Errors are an `fs.FsError` whose `kind` is `"not_found"`,
`"permission_denied"`, `"already_exists"`, `"is_a_directory"`,
`"not_a_directory"`, `"directory_not_empty"`, `"invalid_utf8"`,
`"closed"` (using a closed File) or `"other"`, plus the `op`, `path`, and
a `description`. Text is UTF-8, read and written exactly: no newline
conversion. Binary data waits for a future `Bytes` type.

## `std:process`

The program's arguments and environment, and running other programs.
`run` waits for the program like any call, but the work happens off your
program's thread, so `detach process.run(...)` lets other tasks and timers
run meanwhile.

```mah
import process from "std:process"

print(process.args())                        # what follows `--`: mah run app.mh -- a "b c"
let port = process.env_get("PORT").unwrap_or("8080")

try {
    let out = process.run("git", ["status", "--short"], cwd: some("."))
    if out.ok() { print(out.stdout) } else { print("git said:", out.stderr) }
    print(process.shell("ls | wc -l").stdout.trim())
    process.run("no-such-program")
} catch {
    e: process.ProcessError => { print(e.kind, "-", e.message()) }   # not_found - no-such-program: no such file ...
}
```

| Function | |
|---|---|
| `args()` | the program's arguments: everything after `--` in `mah run FILE -- ARGS...` (also `mah runc`) |
| `exit(code = 0)` | ends the program at once (0 to 255); output is flushed, but `defer`s don't run and no `try`/`catch` can stop it |
| `env_get(name)` | `some(value)` or `none` |
| `env_set(name, value)`, `env_remove(name)` | change the program's own copy of the environment |
| `env()` | a Map of every variable, sorted by name |
| `cwd()`, `pid()`, `platform()` | the current directory; the process id; "linux", "macos" or "windows" |
| `run(program, args = [], cwd = none, env = none, stdin = "")` | starts the program directly (no shell, found through `PATH`) and returns a `process.Output { code, stdout, stderr }` |
| `shell(command, cwd = none, env = none, stdin = "")` | `run` of the command line through `/bin/sh -c` (`cmd /C` on Windows) |

`process.Output.ok()` is `code == 0`. A non-zero exit is not an error, and a
program killed by signal N has the code `128 + N`. Output is decoded as
UTF-8, with invalid bytes replaced by U+FFFD. Only a program that can't be
started throws a `process.ProcessError` whose `kind` is `"not_found"`,
`"permission_denied"` or `"other"`, plus the `command` and a `description`.

The environment functions work on a copy taken when the program starts:
`env_set` never changes the real environment, but the programs `run`
starts get the copy (plus the call's `env` entries). Names must be
non-empty and contain no `=`; names and values can't contain a NUL
character.

## `std:reflect`

What a program can find out about its own functions, types and values while
it runs. A bare type name is a value: `Number`, `String`, `User`, `Vector`
(a `Type`; `==` compares them and `print` shows the name). A `##` comment
directly above a `fn`, `struct`, `enum`, method, field, variant or parameter
is its documentation, and `std:reflect` reads it together with the
annotations, defaults and `throws` clause the source *wrote* (never what the
checker inferred, so an unannotated parameter's type is `reflect.TypeRef.Unknown`).

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

let sig = reflect.signature(add)
print(sig.name, sig.doc)                                  # add Adds two numbers.
print(sig.params[1].has_default, sig.params[1].default)   # true some(1)

match reflect.schema(User) {
    some(reflect.Schema.Struct { type: t, doc: doc, type_params: tps, fields: fields, decorators: ds }) => {
        print(doc, fields[0].name, fields[0].doc)         # A user. name Their name.
    }
    _ => { }
}
let u = reflect.construct(User, ["name": "ada", "tags": []])
print(reflect.type_of(u) == User, reflect.type_of(3))     # true Number
print(reflect.call(add, [1], ["b": 5]))                   # 6
```

| Function | |
|---|---|
| `type_of(value)` | the value's type (`type_of(none)` is `None`) |
| `signature(f)` | a `reflect.Signature { name, doc, type_params, params, returns, throws, decorators, rest, kwrest }`; each `reflect.Param` has `name`, `type`, `doc`, `has_default` and `default` (`some(value)` when it's a literal). `rest` and `kwrest` are `Option`s of the `...` and `**` parameters, which `params` doesn't list. A function wrapped by a hook reports the original's signature |
| `schema(T)` | `some(Schema.Struct { ... fields })` or `some(Schema.Enum { ... variants })`; `none` for `Number`, `String` and the other primitives |
| `methods(T)` | the `reflect.Method { name, function, is_method, trait_name }` written in `impl`s: inherent first, then trait methods by trait and name |
| `implements(T, "Trait")` | whether the type has methods for that trait, native ones included |
| `call(f, args = [], kwargs = [:])` | `f(...args, **kwargs)` |
| `construct(T, fields)`, `construct_variant(T, "Variant", fields)` | a new struct or enum value from exactly its fields; a missing or unknown field throws `reflect.ReflectError` naming it |
| `find(decorators, target)` | the first of a `decorators` Vector (see [Decorators](/docs/decorators)) whose type is `target` (a Type), that is the same function as `target` (a function, even if it was wrapped since), or that `==` it; `none` if there is none |

A type annotation comes back as a `reflect.TypeRef`: `Unknown`, `Named { type,
args }`, `Fn { params, returns, throws }`, `Param { name }`, `SelfType`,
`Never` or `Trait { name, args }`.

`...xs` and `**m` in a call's argument list (a Vector into positional
arguments, a Map into keyword arguments: `f(...args, k: 1, **more)`) exist so
`call` can be written in Mah; see
[Functions & closures](/docs/functions-closures).

The natives behind it read the `.mahc` file's structure and run the
decorator hooks, so they need bytecode 1.16 (and so does any program that
imports `std:json`, whose `decode` is written over it, or has a decorator,
which imports `std:reflect` implicitly). `std:reflect` exports its types (`TypeRef`, `Param`, `Signature`, `Field`,
`Variant`, `Schema`, `Method`, `ReflectError`) and the hook traits and their
info structs (`WrapFn`, `WrapParam`, `WrapField`, `WrapStruct`, `FnInfo`,
`ParamInfo`, `FieldInfo`, `TypeInfo`, see [Decorators](/docs/decorators)),
reached as `reflect.TypeRef` and so on; like every module's types they are module-scoped, so your own
`Field` or `Method` struct never clashes with them.

## How it's built

Each module is a Mah file inside the `mah` package (`mah/std/math.mh`).
Most of `std:math` is plain Mah. The functions that need the machine,
such as `tan` and `log`, are declared with `extern fn`, which binds a Mah
function to a native the VM provides:

```text
export extern fn tan(x: Number) -> Number = "math.tan"
```

`std:path` and `std:collections` need no natives, and `std:json` and
`std:csv` are Mah too, over a handful of natives for reading a value's
type and fields and a String's characters. `std:random`'s generator is a
small native, specified bit for bit so every runtime computes the same
numbers. `std:fs` and `std:process`'s `run` hand their work to a thread and settle
a Promise when it's done; an open file is an id in a table, wrapped in a `fs.File`
struct. `std:regex` parses each pattern in Mah and hands the runtime a
form that Python's `re` and Rust's `regex` crate read the same way.

Only standard library modules may use `extern fn`. New natives come with
a new bytecode minor version, and a compiled program is marked with the
lowest version it needs. An older runtime keeps running programs that
don't use new natives. It refuses one that does, and names the natives it
lacks.
