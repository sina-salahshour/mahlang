---
title: Decorators
order: 17
section: Language
---

A decorator attaches a value to a declaration. `@name` or `@name(args)`
(also `@lib.name(...)`) goes above a top-level `fn`, `struct` or `enum` (and
above `export`), above an `impl` method, or before a parameter, a struct
field or an enum variant. It is an ordinary expression, evaluated **once, at
startup**, and kept in source order. Nothing acts on it except
[`std:reflect`](/docs/standard-library), which hands the values back.

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
the first that is `==` the function; both give `none` when nothing matches.

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
