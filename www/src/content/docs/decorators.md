---
title: Decorators
order: 17
section: Language
---

A decorator attaches a value to a declaration. `@name` or `@name(args)`
(also `@lib.name(...)`) goes above a top-level `fn`, `struct` or `enum` (and
above `export`), above an `impl` method, or before a parameter, a struct
field or an enum variant. It is an ordinary expression, evaluated **once, at
startup**, and kept in source order. By itself nothing acts on it:
[`std:reflect`](/docs/standard-library) hands the values back, and a
decorator whose type implements one of the hook traits (below) *changes
behavior*.

```mah
import reflect from "std:reflect"

struct Route { method: String, path: String }

fn get(path: String) -> Route { Route { method: "GET", path: path } }
fn tag(name: String) -> String { "tag:" + name }

## Fetch one user.
@get("/users/{id}")
fn get_user(@tag("path") id: Number, verbose: Bool = false) -> Number { id }

@tag("model")
struct User {
    @tag("json:user_name") name: String,
    age: Number
}

enum Shape { @tag("round") Circle { r: Number }, Empty }

impl User {
    @tag("greeting") fn hello(self) -> String { "hi" }
}

let sig = reflect.signature(get_user)
print(sig.doc)                       # Fetch one user.
print(sig.decorators[0].path)        # /users/{id}
print(sig.params[0].decorators)      # [tag:path]
```

The values are in the `decorators` Vector of a `Signature`, a `Param`, a
`Schema`, a `Field` and a `Variant`. `reflect.find(decorators, Route)` is the
first decorator whose type is `Route`; `reflect.find(decorators, my_fn)` is
the first that is the same function (it still matches after `my_fn` was
wrapped by a hook); both give `none` when nothing matches.

## When they run

- Each module's decorators run in one phase, in source order, right before
  that module's first statement (anything that isn't a `fn`, `struct`,
  `enum`, `trait`, `impl` or `test`), or at the end of its code if it has
  none. Every function already exists by then, and the modules it imports
  have already run.
- A decorator may call any function and use types and literals. It may
  **not** name a top-level `let` of its own module, which hasn't run yet:
  "a decorator can't use the top-level variable 'x': it runs before it; use a
  function or a literal". Another module's `let`s are fine.
- A decorator factory that reads its own module's top-level `let` sees `none`
  when used on that same module's declarations (the `let`s haven't run yet),
  while a module that imports it sees the initialized value. Keep constants
  inside the factory, or in a function that returns them.
- A decorator may name a function or type declared later in the module.
- A decorator that throws is an uncaught error at startup.

## Where they may go

Only on top-level functions, structs and enums, `impl` methods, their
parameters, fields and variants. Anywhere else (a nested function, a
closure, a `let`, a `trait` or its methods, an `impl` block) is a compile
error. Put a `##` doc comment above the decorator lines; the formatter keeps
the decorators of a function, struct, enum or method on their own lines and
those of a parameter, field or variant inline.

## Hooks

A decorator is a hook when its type implements `reflect.WrapFn`,
`reflect.WrapParam`, `reflect.WrapField` or `reflect.WrapStruct`. A function
can implement a trait too (`impl Tr for somefn`), so a plain function is a
decorator with `@log`; a factory like `@retry(3)` returns a struct. A
program with any decorator imports `std:reflect` implicitly: write the
import only to name the traits.

```mah
import reflect from "std:reflect"

fn log(f, info: reflect.FnInfo) {
    fn(...args, **kw) {
        print("call", info.name)
        f(...args, **kw)
    }
}
impl reflect.WrapFn for log { fn wrap(self, f, info) { self(f, info) } }

@log
fn greet(name: String, punct: String = "!") { "hi " + name + punct }
print(greet("ann"))                          # call greet / hi ann!
print(reflect.signature(greet).params.len()) # 2

fn trim(value, info: reflect.ParamInfo) { value }
impl reflect.WrapParam for trim { fn transform(self, value, info) { value.trim() } }
fn shout(@trim text: String) { text.to_upper() }
print(shout("  hi "))                         # HI

fn lower(value, info: reflect.FieldInfo) { value }
impl reflect.WrapField for lower { fn set(self, value, info) { value.to_lower() } }
struct Adult {}
impl reflect.WrapStruct for Adult {
    fn construct(self, value, info) {
        if value.age < 18 { throw RuntimeError.ArgumentError { message: "too young" } }
        value
    }
}
fn adult() -> Adult { Adult {} }

@adult()
struct Member { @lower email: String, age: Number }
let m = Member { email: "ANN@X.COM", age: 30 }
m.email = "BOB@X.COM"
print(m.email)                               # bob@x.com
```

| trait | on | method | runs |
|---|---|---|---|
| `WrapFn` | a function or method | `wrap(self, f, info) -> function` | once at startup: the result replaces the function |
| `WrapParam` | a parameter | `transform(self, value, info) -> value` | on every call, after binding (and defaults), before the body |
| `WrapField` | a struct field | `set(self, value, info) -> value` | on a literal, `reflect.construct` and every `obj.field = v` |
| `WrapStruct` | a struct | `construct(self, value, info) -> value` | on a literal and `reflect.construct`, after the field hooks |

- Several hooks on one target run **closest to the declaration first**, each
  receiving the previous result. A decorator that implements no hook trait
  just attaches its value.
- `wrap` must return a function (`ArgumentError`, "WrapFn.wrap must return a
  function", at startup). The wrapper takes the original's *identity*:
  `reflect.signature`, the decorators, the parameter hooks and any
  `impl Tr for greet` methods still belong to the original. A wrapper for a
  method gets the object as `args[0]`.
- A hook that throws rejects the call, literal or assignment; hooks may
  `await`. A parameter hook of a rest parameter gets its Vector or Map.
- `impl Tr for somefn` and `impl somefn { ... }` target a top-level `fn`
  declaration, in its own module or the trait's; any other target is the
  compile error "impl targets must be a type or a top-level function".
  `reflect.type_of(somefn)` is still `Function`.
- Hooks for enums and field *read* hooks don't exist.
