---
title: std:reflect
order: 16
section: Language & testing
summary: Inspect types, functions, docs and decorators at run time; build values and call functions dynamically; hooks.
---

# `std:reflect`

What a program can find out about its own functions, types and values
while it runs: a value's type, a function's parameters and docs, a
struct's fields, a type's methods, and the decorators on each. It can also
build values and call functions dynamically. It's what powers
[`std:json`](/std/json)'s `decode` and [decorators](/docs/decorators).

```mah
import reflect from "std:reflect"

## Adds two numbers.
fn add(a: Number, b: Number = 1) -> Number { a + b }

let sig = reflect.signature(add)
print(sig.name, sig.doc)                               # add Adds two numbers.
print(sig.params.map(fn(p) { p.name }).reduce())       # [a, b]
print(reflect.type_of(3), reflect.call(add, [1], ["b": 5]))   # Number 6
```

## Types are values

A bare type name in an expression is a value of type `Type`: `Number`,
`String`, `Vector`, `Option`, or your own `User`. `==` compares them and
`print` shows the name. `type_of(value)` gives a value's type:

```mah
import reflect from "std:reflect"

struct User { name: String }

fn describe(x) -> String {
    let t = reflect.type_of(x)
    if t == Number { "a number" } elif t == User { "a user named " + x.name } else { "a " + t.to_string() }
}
print(describe(3), describe(User { name: "ada" }), describe([1]))   # a number a user named ada a Vector
print(reflect.type_of(none), reflect.type_of(some(1)))              # None Option
```

## Doc comments

A `##` comment directly above a `fn`, `struct`, `enum`, method, field,
variant or parameter is its **documentation**, kept in the compiled
program and returned as `doc`. (A plain `#` comment isn't.) Your editor's
hover shows it too.

## Functions: `signature`

`signature(f)` gives a `reflect.Signature` with `name`, `doc`,
`type_params`, `params`, `returns`, `throws`, `decorators`, and `rest` /
`kwrest` (the `...` and `**` parameters, as Options). Each `reflect.Param`
has `name`, `type`, `doc`, `has_default` and `default` (`some(value)` when
the default is a literal):

```mah
import reflect from "std:reflect"

fn greet(
    ## Who to greet.
    who: String,
    punct: String = "!",
    ...extra
) -> String { "hi " + who + punct }

let sig = reflect.signature(greet)
for let p in sig.params {
    print(p.name, p.has_default, p.default, p.doc)
}
# who false none Who to greet.
# punct true some(!)
print(sig.rest.is_some(), sig.returns)                 # true TypeRef.Named { type: String, args: [] }
```

Types come back as a `reflect.TypeRef`: `Named { type, args }` (a struct,
enum or built-in, with type arguments), `Fn { params, returns, throws }`,
`Param { name }` (a type parameter), `SelfType`, `Never`, `Trait { name,
args }`, or `Unknown` when there's no annotation. These are the types the
source **wrote**, never what the checker inferred.

## Structs and enums: `schema`

`schema(T)` is `some(Schema.Struct { type, doc, type_params, fields,
decorators })` or `some(Schema.Enum { type, doc, type_params, variants,
decorators })`, and `none` for primitives like `Number`. Each `Field` has
`name`, `type`, `doc` and `decorators`; each `Variant` has `name`, `doc`,
`fields` and `decorators`.

```mah
import reflect from "std:reflect"

## A user.
struct User {
    ## Their name.
    name: String,
    tags: Vector<String>
}

match reflect.schema(User) {
    some(reflect.Schema.Struct { type: t, doc: doc, type_params: tps, fields: fields, decorators: ds }) => {
        print(doc, fields.len())                       # A user. 2
        for let f in fields { print(f.name, f.doc) }   # name Their name. / tags
    }
    _ => { }
}
```

## Building values and calling functions

`construct(T, fields)` builds a struct from a Map of **exactly** its
fields, and `construct_variant(T, "Variant", fields)` an enum value. A
missing or unknown field throws `reflect.ReflectError` naming it.
`call(f, args = [], kwargs = [:])` calls any function with a Vector of
positional arguments and a Map of keyword arguments:

```mah
import reflect from "std:reflect"

struct Point { x: Number, y: Number }
enum Shape { Circle { r: Number }, Empty }

let p = reflect.construct(Point, ["x": 1, "y": 2])
print(p, reflect.construct_variant(Shape, "Circle", ["r": 3]))   # Point { x: 1, y: 2 } Shape.Circle { r: 3 }
print(try { reflect.construct(Point, ["x": 1]) } catch { e: reflect.ReflectError => { e.message } })
# missing field 'y' for Point
```

## Methods and traits

`methods(T)` lists the `reflect.Method { name, function, is_method,
trait_name }` written in `impl`s for a type: inherent methods first, then
trait methods by trait and name. `implements(T, "Trait")` says whether a
type has a trait's methods, native ones included:

```mah
import reflect from "std:reflect"

struct Dog { name: String }
impl Dog {
    fn new(name: String) -> Dog { Dog { name: name } }
    fn bark(self) -> String { self.name + ": woof" }
}
impl Printable for Dog { fn to_string(self) { "Dog " + self.name } }

for let m in reflect.methods(Dog) { print(m.name, m.is_method, m.trait_name) }
# bark true none / new false none / to_string true some(Printable)
print(reflect.implements(Dog, "Printable"), reflect.implements(Number, "Printable"))   # true true
```

## Decorators and hooks

Every `Signature`, `Param`, `Schema`, `Field` and `Variant` has a
`decorators` Vector with the values of the `@decorators` written on it.
`find(decorators, target)` gives the first one whose type is `target` (a
Type), or that is the function `target`:

```mah
import reflect from "std:reflect"

struct Route { method: String, path: String }
fn get(path: String) -> Route { Route { method: "GET", path: path } }

@get("/users/{id}")
fn show_user(id: Number) -> String { "user " + id.to_string() }

let route = reflect.find(reflect.signature(show_user).decorators, Route)
print(route.unwrap().method, route.unwrap().path)      # GET /users/{id}
```

This module also defines the **hook traits** that let a decorator change
behavior: `WrapFn` (wrap a function), `WrapParam` (transform an argument
on every call), `WrapField` (transform a field on every assignment) and
`WrapStruct` (check or replace a struct when it's built), with the info
structs they receive (`FnInfo`, `ParamInfo`, `FieldInfo`, `TypeInfo`).
They're explained with examples on the [Decorators](/docs/decorators)
page.

## Reference

| Function | |
|---|---|
| `type_of(value)` | the value's `Type` |
| `signature(f)` | a `Signature` |
| `schema(T)` | `some(Schema)`, or `none` for primitives |
| `methods(T)` | the `Method`s written in `impl`s |
| `implements(T, "Trait")` | whether the type implements a trait |
| `call(f, args = [], kwargs = [:])` | `f(...args, **kwargs)` |
| `construct(T, fields)` | a struct from exactly its fields |
| `construct_variant(T, "Variant", fields)` | an enum value |
| `find(decorators, target)` | the first matching decorator, as an Option |

Types: `TypeRef`, `Param`, `Signature`, `Field`, `Variant`, `Schema`,
`Method`, `ReflectError`; hook traits `WrapFn`, `WrapParam`, `WrapField`,
`WrapStruct` and their `FnInfo`, `ParamInfo`, `FieldInfo`, `TypeInfo`. A
function wrapped by a hook reports the original's signature. Programs
that import `std:reflect` (or `std:json`, or use any decorator) need
bytecode 1.16.
