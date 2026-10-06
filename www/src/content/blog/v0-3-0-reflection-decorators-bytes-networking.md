---
title: "v0.3.0: reflection, decorators, Bytes, processes and networking"
date: 2026-10-06
description: "Types as values, ## doc comments and std:reflect; decorators and hooks that wrap functions, parameters and fields; rest parameters and spread calls; module-scoped type names; Bytes and binary files; std:process; and TCP, TLS, URLs and an HTTP client."
tags: [changelog]
version: "0.3.0"
---

This release (milestones M36 through M41) lets a Mah program look at
itself: types are values, functions and fields carry their `##` docs and
annotations at run time, and decorators attach data to declarations or,
through hooks, change what they do. It also adds binary data, running
other programs, and the network: TCP sockets, TLS, URLs and an HTTP client.
`mah-vm --version`, `mah lsp --version` and the bytecode all move forward
together; programs using the new features need bytecode 1.14 to 1.19,
depending on which ones (`mah build` marks each program with the lowest
version it needs, as before).

**One breaking change:** struct, enum and trait names declared in an
imported module are now private to it unless exported. See
[module-scoped type names](#module-scoped-type-names) below.

## Types are values, and `std:reflect`

A bare type name in an expression is a `Type`: `Number`, `String`, `User`,
`Vector`. `==` compares them and `print` shows the name. A `##` comment
directly above a `fn`, `struct`, `enum`, method, field, variant or
parameter is its documentation, and the new `std:reflect` module reads it
back along with the annotations and defaults the source wrote.

```mah
import reflect from "std:reflect"

## A user.
struct User { name: String, age: Number }

fn greet(who: String, punct: String = "!") -> String { "hi " + who + punct }

let t = User
print(t, t == User, reflect.type_of(3))                  # User true Number
let sig = reflect.signature(greet)
print(sig.name, sig.params.len(), sig.params[1].default) # greet 2 some(!)
let u = reflect.construct(User, ["name": "ada", "age": 36])
print(u.name, reflect.call(greet, ["ada"], ["punct": "?"]))   # ada hi ada?
```

Also `schema(T)` (a struct's fields or an enum's variants), `methods(T)`
and `implements(T, "Trait")`. Annotations are what the source *wrote*,
never what the checker inferred, and they still cost nothing unless you
ask: the metadata lives in an optional section of the `.mahc` file. See
the [standard library reference](/docs/standard-library#stdreflect).

`std:json` uses it to decode straight into your types, with errors that
say exactly which field was wrong:

```mah
import json from "std:json"
struct Point { x: Number, y: Number }
try {
    let p = json.parse_as(Point, "{\"x\": 1, \"y\": 2}")
    print(p.x + p.y)                       # 3
    json.parse_as(Point, "{\"x\": 1}")
} catch {
    e: json.JsonError => { print(e.message()) }   # missing field 'y' for Point
}
```

## Rest parameters and spread calls

`...name` collects extra positional arguments into a Vector, `**name`
unknown keyword arguments into a Map. In a call, `...xs` spreads a Vector
into positional arguments and `**m` a Map into keyword arguments, which
is what lets `reflect.call` (and the hooks below) be written in Mah.

```mah
fn sum(first: Number, ...rest, **opts) -> Number {
    let total = first
    for let n in rest { total = total + n }
    if opts["double"] == true { total * 2 } else { total }
}
let xs = [2, 3]
print(sum(1, ...xs), sum(1, ...xs, double: true), sum(1, **["double": true]))   # 6 12 2
```

## Decorators and hooks

`@name` or `@name(args)` above a top-level `fn`, `struct` or `enum`, an
`impl` method, or before a parameter, field or variant attaches a value to
it. Decorators are ordinary expressions, evaluated once at startup, and
`std:reflect` hands them back (`sig.decorators`, `reflect.find`), so a
router or a serializer can read `@get("/users/{id}")` or
`@tag("json:user_name")` without any special support in the language.

A decorator whose type implements a **hook** trait (`reflect.WrapFn`,
`WrapParam`, `WrapField` or `WrapStruct`) changes behavior. A function can
implement a trait too (`impl Tr for somefn`), so a plain function works as
a decorator:

```mah
import reflect from "std:reflect"

fn log(f, info: reflect.FnInfo) {
    fn(...args, **kw) {
        print("call", info.name)
        f(...args, **kw)
    }
}
impl reflect.WrapFn for log { fn wrap(self, f, info) { self(f, info) } }

fn trim(value, info: reflect.ParamInfo) { value }
impl reflect.WrapParam for trim { fn transform(self, value, info) { value.trim() } }

@log
fn shout(@trim text: String) -> String { text.to_upper() }
print(shout("  hi "))      # call shout, then HI
```

A wrapped function keeps its identity: reflection, its decorators and
trait dispatch still see the original. The full rules (when decorators
run, where they may go, what each hook receives) are on the new
[Decorators](/docs/decorators) page.

## Module-scoped type names

Before 0.3.0 every `struct`, `enum` and `trait` name was global to the
program, so two modules couldn't each declare a `Request`, and importing
`std:json` reserved names like `Field`. Now a module's types are private
to it, like its functions, unless it writes `export struct` (or `export
enum`, `export trait`). Importers name them through the module:

```mah
import lib from "./lib.mh"     # lib.mh: export struct Point { x: Number, y: Number }
struct Point { x: Number }     # a different Point, no clash
let a = lib.Point { x: 1, y: 2 }
print(a, Point { x: 3 }, lib.Point == Point)   # Point { x: 1, y: 2 } Point { x: 3 } false
```

After a flat import (`import "./lib.mh"`) they're written bare, as before.
The standard library's types are reached the same way: `json.JsonError`,
`fs.FsError`, `process.Output`. **If your program used a type from another
file, export it there and qualify it here** (or switch to a flat import).

## `Bytes` and `std:bytes`

Binary data gets its own type: a growable, mutable sequence of bytes,
indexed and sliced like a Vector but compared by content. Make one with
`"text".to_bytes()` or `std:bytes`; `std:fs` reads and writes binary files
with it.

```mah
import bytes from "std:bytes"
let b = "hi".to_bytes()
b.push(33)
b[0] = 72
print(b.to_text(), b.to_hex(), b.to_base64())   # some(Hi!) 486921 SGkh
print(b[1..], b == bytes.from_hex("486921"))    # Bytes[69 21] true
```

See [Bytes](/docs/collections#bytes) and
[`std:bytes`](/docs/standard-library#stdbytes).

## `std:process`

The program's arguments (`mah run app.mh -- a b`), its environment, and
running other programs, off the main thread like `std:fs`:

```mah
import process from "std:process"

let name = process.env_get("USER").unwrap_or("friend")
try {
    let out = process.run("echo", ["hello", name])     # no shell, found through PATH
    print(out.ok(), out.stdout.trim().starts_with("hello"))   # true true
    print(process.shell("echo a b c | wc -w").stdout.trim())  # 3
} catch {
    e: process.ProcessError => { print(e.kind) }
}
```

## Networking: sockets, TLS, URLs and HTTP

`std:socket` has TCP clients and servers (with TLS on top), `std:url`
parses, builds and encodes URLs, and `std:http` is an HTTP/1.1 client
written in Mah over sockets, so both runtimes run the same code. It
follows redirects and honors `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY`. Only
TLS itself is native (Python's `ssl`, Rust's `rustls`).

```mah
import http from "std:http"
import url from "std:url"
fn show() throws http.HttpError {
    let r = http.get("https://example.com/?" + url.encode_query(["q": "mah"]))
    print(r.status, r.header("content-type"))
}
```

## Editors and tooling

The tree-sitter grammar, the VS Code grammar, the formatter and the
language server all understand decorators, `##` doc comments, rest
parameters, spread calls and `export struct`/`enum`/`trait`. Hover shows a
qualified type's real name (`lib.Point`, not an internal one), and rename
and go-to-definition work across files on exported types and inside
decorators.

## The website

[mahlang.dev](/) works properly on phones now: no more sideways scrolling
on the home page, the docs page list folds into a bar under the header,
wide tables scroll inside themselves, and headings have links you can
share (`/docs/standard-library#stdfs`).
