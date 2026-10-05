---
title: "v0.2.0: checked errors, the type checker, a standard library, and mah test"
date: 2026-09-28
description: "throw/try/catch with a built-in Error trait and RuntimeError enum on both VMs, a checker that infers what every function throws, the first static type checker with mah check, a standard library with math, path, JSON, CSV, random, collections and regex modules, and a built-in test runner."
tags: [changelog]
version: "0.2.0"
---

This release (milestones M22 through M35) adds a static type checker,
errors you can throw, catch, and have checked, the first modules of a
standard library (math, paths, JSON, CSV, random numbers, collections,
regular expressions), a test runner, and String methods. Types and error sets cost nothing at run
time: they're erased before codegen. The bytecode gains 1.4's handler
table for `try`, 1.5's math natives, 1.7's natives behind JSON and CSV,
1.8's random number generator, 1.9's regex matcher, 1.10's
asynchronous `input`, 1.11's clocks and hand-settled Promises, and 1.12's
file system access.

## Errors: `throw`, `try`, `catch`

Anything implementing the built-in `Error` trait can be thrown. `try {
... } catch { arms }` takes `match` arms plus a type-test pattern (`e:
Type`). An error no arm matches is re-thrown automatically, and
`try EXPR else FALLBACK` catches everything.

```mah
enum ParseError { Empty, BadDigit }
impl Error for ParseError {}

fn first_digit(s) {
    if s.len() == 0 { throw ParseError.Empty }
    let c = s.char_at(0)
    if c < "0" | c > "9" { throw ParseError.BadDigit }
    c
}

let a = try { first_digit("") } catch {
    ParseError.Empty => { "was empty" }
    e: ParseError => { "bad: " + e.to_string() }
}
let b = try first_digit("x") else "?"
let c = try { 1 / 0 } catch { e: RuntimeError => { e.message } }
print(a, b, c)     # was empty ? Division by zero
```

Everything the VM itself used to abort on (division by zero, a missing
method, a bad index, a `match` with no arm for a value, ...) is now a
value of the built-in `RuntimeError` enum, catchable like any other error.
An uncaught one still prints exactly the message and location it always
did. `defer`red blocks run while an error unwinds past them. A detached
task that throws fails its Promise (the new `Promise.Failed { error }`)
instead of stopping the program, and `.await` re-throws the error. Both
VMs implement all of this identically, with bytecode format 1.4.

## Checked errors

The checker infers what every function can throw, with no annotations:
its body's `throw`s, plus what it calls and awaits, minus what a `try`
around them fully handles. An error that can reach the top of the program
is reported, and a `throws` clause (on a function, or on a function type
such as `fn(String) -> String throws never`) is checked against the body.
Leaving `throws` off means "inferred".

```mah
fn load(s) { first_digit(s) }                  # fn load(s: String) -> String throws ParseError
fn safe(s) { try load(s) else "0" }            # fn safe(s: String) -> String
fn strict(s: String) -> String throws never { load(s) }
load("")
```

```text
$ mah check        # both samples above, in one src/main.mh
warning: 'strict' can throw ParseError, which isn't in its throws list at position #20:1
warning: Unhandled error: ParseError at position #21:1
2 warnings
```

Those are warnings in `loose` mode and errors in `strict`/`explicit`. A
`catch` arm for an error the body can never throw is always just a
warning. Callbacks are tracked per call, `RuntimeError`s are catchable but
never tracked, and hover shows each function's `throws`. See
[Errors](/docs/errors).

## The static type checker

Optional annotations everywhere a name is declared, with the rest
inferred, generics included: `fn id(x) { x }` is `fn<T>(T) -> T`, and `fn
add(a, b) { a + b }` is `fn(Number, Number) -> Number`. Method calls on
your own types and on `String`/`Vector`/`Map` are typed too. How strict it
is comes from `mah-project.toml`:

```toml
[types]
check = "loose"      # "loose" | "strict" | "explicit"
```

`mah check` prints every diagnostic, and the editor shows them as you type,
along with inferred types on hover. See [Types](/docs/types).

## The standard library begins: `std:math`

Standard library modules are imported with a `std:` path, namespaced or
flat, and the first one is `std:math`:

```mah
import math from "std:math"
print(math.sqrt(2))                                       # 1.414213562373095048801688724
print(math.round(math.pi, 4), math.floor(0 - 2.5))        # 3.1416 -3
print(math.log10(1000), try math.log(0) else "undefined") # 3 undefined
```

`sqrt`, `pow` and the rounding functions are exact on Mah's 28-digit
Numbers. `tan`, `asin`, `acos`, `atan`, `atan2`, `exp`, `log` and `log10`
are new natives, computed in double precision like `sin`/`cos`, and bad
input throws `RuntimeError.ArgumentError`. `sin` and `cos` are no longer
keywords: they still work with no import, but a function of your own
named `sin` now takes over. A compiled program is marked with the lowest
bytecode version it needs, 1.4 unless it uses the new natives. So a
1.4 runtime still runs programs that don't use them, and a runtime that's
too old names the natives it's missing. See
[Standard library](/docs/standard-library).

## String methods

Strings finally have the methods you'd expect, the same on both VMs:

```mah
print("a,b,,c".split(","), "  one  two ".split(), "|" + "  hi ".trim() + "|")  # [a, b, , c] [one, two] |hi|
print("7".pad_start(3, "0"), "a-b".replace_all("-", "+"), "hello".index_of("l"))  # 007 a+b some(2)
print("Straße".to_upper(), "A\nB\n".lines(), ["x", 1].join(", "))              # STRASSE [A, B] x, 1
print("42".to_number() + 1, "abc".parse_number())                               # 43 none
```

Also `trim_start`/`trim_end`, `pad_end`, `replace`, `starts_with`/
`ends_with`/`contains`, `repeat`, and `to_lower`. `to_number()` throws
a `NumberParseError` that the checker tracks, and `index_of`'s Option
has `unwrap()`, `unwrap_or()`, `is_some()` and `is_none()`. An uncaught
error from inside the prelude, such as `"x".to_number()`, is now reported
at your line, not at the prelude's. See [Strings](/docs/strings).

## `std:path`, `std:json` and `std:csv`

Three more modules, written in Mah:

```mah
import path from "std:path"
import json from "std:json"
import csv from "std:csv"

print(path.join("src", "main.mh"), path.extension("a.tar.gz"), path.relative("x/a", "x/b"))  # src/main.mh .gz ../b
let doc = try json.parse("{\"tags\": [1, 2], \"ok\": true}") else [:]
print(doc["tags"][1], try json.stringify(doc) else "")      # 2 {"tags":[1,2],"ok":true}
let rows = try csv.parse_records("name,age\nal,3\n") else []
print(rows[0]["name"], rows[0]["age"])                      # al 3
```

- `std:path` is string logic over `/` and `\` paths: `join`, `dirname`,
  `basename`, `extension`, `stem`, `normalize`, `is_absolute` and
  `relative`.
- `std:json` parses strictly and throws `JsonError.Syntax` with the line
  and column. `stringify` can indent, and it writes structs and enums too.
- `std:csv` follows RFC 4180. `parse` gives rows of Strings, and
  `parse_records` gives Maps keyed by the header.
- `FromJson` and `FromCsvRow` read into your own structs, with helpers
  (`json.field`, `json.as_number`, `csv.column`, ...) that throw on a
  shape mismatch.
- An uncaught error from inside a standard library module is now reported
  at your call to it, as prelude errors already were.

See [Standard library](/docs/standard-library).

## `std:random` and `std:collections`

```mah
import random from "std:random"
import "std:collections"

random.seed(42)
print(random.random(), random.randint(1, 6))       # 0.08386297105988216316063699196 1
let seen = Set.of(["a", "b", "a"])
let queue = PriorityQueue.of([5, 1, 3])
print(seen, queue.pop(), Deque.of([1, 2]).pop_front())   # Set[a, b] 1 1
```

- `std:random` has `random`, `uniform`, `randint`, `choice`, `shuffle`,
  `shuffled`, `sample`, `seed`, and independent `Rng.new(seed)`
  generators. Both VMs use the same generator (xoshiro256**), so a seeded
  program prints the same numbers on either one.
- `std:collections` has `Set`, `Deque` (O(1) at both ends) and
  `PriorityQueue` (smallest first, optional key function). All three are
  Iterable and generic, so the checker knows a `Set<String>` from a
  `Set<Number>`.

## `std:regex`

```mah
import regex from "std:regex"

let date = regex.must_compile("(?<y>\\d{4})-(?<m>\\d\\d)")
print(date.find("due 2026-09").unwrap().group("y"))         # 2026
print(date.replace_all("2026-09 2027-01", "$m/$y"))         # 09/2026 01/2027
```

Regular expressions with the usual syntax, matched the same way on both
VMs: the module checks each pattern against a common subset of Python's
`re` and Rust's `regex` crate (ASCII `\d`/`\w`/`\s`, no lookaround or
backreferences), and a pattern outside it is a `RegexError` with its
position. `find` gives an `Option<Match>`, and `replace_all` takes `$1`
/ `$name` templates or a function. `must_compile` is for fixed patterns.
The Rust runtime gains its first dependency, the `regex` crate.

## `std:fs`

```mah
import fs from "std:fs"

try {
    let dir = fs.temp_dir()
    defer fs.remove(dir, recursive: true)
    fs.write_text(dir + "/a.txt", "one\ntwo\n")
    for let line in fs.open(dir + "/a.txt").lines() {
        print(line)                             # one, then two
    }
    print(fs.glob(dir + "/*.txt").len())        # 1
} catch {
    e: FsError => { print(e.message()) }
}
```

Files and directories: whole-file `read_text`/`write_text`/`append_text`,
`open` for a line at a time, `list_dir`, `mkdir`, `remove`, `rename`,
`copy`, `info`, `glob` with `**`, and `temp_dir`. The work runs off the
program's thread, so a `detach`ed read doesn't hold anything up. Failures
are an `FsError` with a `kind` (`"not_found"`, `"permission_denied"`,
...) that's the same on both runtimes. Text is read and written exactly
as UTF-8.

## `std:time` and `std:async`

```mah
import time from "std:time"
import async from "std:async"

fn job(ms: Number, name: String) -> String {
    sleep_async(ms)
    name
}

print(async.all([detach job(30, "a"), detach job(10, "b")]))        # [a, b]
print(try async.timeout(detach job(500, "x"), 20) else "too slow")  # too slow
print(time.format(time.utc(1790597925), "%a %d %b %Y %H:%M"))       # Mon 28 Sep 2026 12:18
```

- `std:async` has `all`, `race` and `timeout` (which throws
  `TimeoutError`), and `set_timeout`/`set_interval` with their `clear_`
  functions. A cleared timer doesn't keep the program running. The type
  checker sees through them: `all` of `Promise<String>`s is a
  `Vector<String>`, and it throws what the Promises throw. Timer
  callbacks must not throw.
- `std:time` has `now()`, `monotonic()`, UTC `DateTime`s with
  `format`/`parse` (`%Y-%m-%d %H:%M`...), ISO 8601, and
  `duration_text`. Times and durations are plain Numbers of seconds.
  There are no time zones yet.

## `input` reads a line, and can wait in the background

**Breaking:** `input()` used to scan standard input for digits and return
a Number. Now `input(prompt = "")` prints the prompt, reads one line, and
returns it as a String; add `.to_number()` where you need a Number. At
the end of the input it throws `EndOfInput`, which the checker tracks.

`input` is asynchronous underneath: called plainly it waits for the line,
and `detach input()` gives a Promise, so timers and other tasks keep
running while the user types:

```mah
let answer = detach input("number: ")
for let i in 0..3 {
    sleep_async(500)
    print("still waiting...")
}
print(try answer.await.to_number() else 0)
```

Both runtimes read standard input on a reader thread and settle the
Promise from their scheduler, which now waits for whichever comes first,
a timer or a finished read. `input` is also no longer a keyword: a
function or variable of your own named `input` takes over. Files compiled
before this change keep their old behavior.

## `mah test`

Tests live in `*.test.mh` files: `test "name" { ... }` blocks, with
assertions from `std:test`. `mah test` finds them all and runs each test
in a fresh VM:

```mah
# src/calc.test.mh
import "std:test"

fn add(a, b) { a + b }

test "adds two numbers" {
    assert_eq(add(2, 2), 5)
}
```

```text
$ mah test
running 1 test
test src/calc.test.mh::adds two numbers ... FAILED

failures:

---- src/calc.test.mh::adds two numbers ----
assert_eq failed at src/calc.test.mh:7
  actual:   4
  expected: 5

test result: FAILED. 0 passed; 1 failed; 0 skipped; finished in 0.03s
```

- A failure points at your failing line, even from inside a helper
  function: thrown values now carry a backtrace, and other errors print a
  Mah stack trace.
- `x.test.mh` can test `x.mh`'s private functions.
- `mah test NAME` filters, `--vm rust` runs on `mah-vm`, and `--timeout`
  stops runaway tests.
- New projects start with `src/main.test.mh`. See [Testing](/docs/testing).

## Editors

The tree-sitter grammar (Neovim) and the VS Code TextMate grammar now
highlight `throw`, `try`, `catch`, `throws`, `never`, `extern` and
`test`. The last five only count as keywords where they can be one, so
`let catch = 1` still works. The editor lists a test file's tests as
symbols. Hover and go-to-definition reach into standard library
modules. Completion inside an `import "..."` string offers the `std:`
modules.

## Self-contained builds

`mah-vm --version` now also prints the newest bytecode version it runs
(`mah-vm 0.2.0 (x86_64-linux) bytecode 1.7`). `mah build
--self-contained` refuses to bundle a program with a `mah-vm` that's too
old to run it, and says to rebuild it (`make vm`). Previously you got an
executable that failed with "unsupported minor version" when you ran it.

## Networking: sockets, TLS, URLs and HTTP

Since this post first went up, `std:socket` (M38) added TCP clients and
servers, and M39 added TLS, `std:url` and an HTTP client, `std:http`,
written in Mah over sockets so both runtimes run the same code. Programs
that use them need bytecode 1.19.

```mah
import http from "std:http"
import url from "std:url"
fn show() throws http.HttpError {
    let r = http.get("https://example.com/?" + url.encode_query(["q": "mah"]))
    print(r.status, r.header("content-type"))
}
```

