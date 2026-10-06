---
title: std:json
order: 6
section: Text & data
summary: Parse and write JSON, and decode it straight into your own structs and enums with precise error messages.
---

# `std:json`

Reading and writing JSON. `parse` gives plain Maps and Vectors;
`parse_as` checks the data against one of your types and builds it, with
errors that say exactly which field was wrong.

```mah
import json from "std:json"

let data = try json.parse("{\"name\": \"mah\", \"tags\": [1, 2], \"debug\": null}") else [:]
print(data["name"], data["tags"][1], data["debug"])    # mah 2 none
print(try json.stringify(data) else "")                # {"name":"mah","tags":[1,2],"debug":null}
```

## Parsing

`parse(text)` turns JSON into Mah values:

| JSON | Mah |
|---|---|
| object | `Map` (keys in document order) |
| array | `Vector` |
| number | `Number` (exact, 28 digits) |
| string | `String` |
| `true` / `false` | `Bool` |
| `null` | `none` |

It follows the standard strictly: no comments, no trailing commas, no
single quotes. Bad input throws `json.JsonError.Syntax` with the line and
column:

```mah
import json from "std:json"

try {
    json.parse("[1, 2,]")
} catch {
    e: json.JsonError => { print(e.message()) }   # expected a value, found ']' at line 1, column 7
}
```

## Writing

`stringify(value, indent = 0)` writes compact JSON, or with `indent`
spaces per level and one item per line:

```mah
import json from "std:json"

print(try json.stringify(["a": [1, 2], "b": none], indent: 2) else "")
# {
#   "a": [
#     1,
#     2
#   ],
#   "b": null
# }
```

It also writes your own types: a **struct** as an object of its fields,
an **enum** unit variant as its name, and any other variant as an object
with one key, the variant's name. `some(x)` is written as `x`.

```mah
import json from "std:json"

struct Point { x: Number, y: Number }
enum Shape { Circle { r: Number }, Empty }

print(try json.stringify(Point { x: 1, y: 2 }) else "")         # {"x":1,"y":2}
print(try json.stringify([Shape.Circle { r: 2 }, Shape.Empty]) else "")   # [{"Circle":{"r":2}},"Empty"]
```

A value JSON can't hold (a function, a Vector that contains itself)
throws `json.JsonError.Shape`.

## Decoding into your types

`parse_as(T, text)` parses and then checks the value against `T`'s
declaration, building a real struct. It's `decode(T, parse(text))`;
use `decode` when you already have a parsed value.

```mah
import json from "std:json"

struct User { name: String, age: Number, email: Option<String>, tags: Vector<String> }

let u = try json.parse_as(User, "{\"name\": \"ada\", \"age\": 36, \"tags\": [\"x\"]}") else none
print(u.name, u.age + 1, u.email, u.tags)     # ada 37 none [x]
```

It understands `Number`, `String`, `Bool`, `Vector<X>`,
`Map<String, X>`, `Option<X>` (a missing key or `null` is `none`), nested
structs, and enums (in the shape `stringify` writes them). A field
without a type annotation takes the value as it is.

Errors name the exact place, as the type's name followed by `.field`,
`[i]` or `["key"]`:

```mah
import json from "std:json"

struct Item { sku: String, qty: Number }
struct Order { id: Number, items: Vector<Item> }

let text = "{\"id\": 7, \"items\": [{\"sku\": \"a\", \"qty\": 1}, {\"sku\": \"b\", \"qty\": \"two\"}]}"
try {
    json.parse_as(Order, text)
} catch {
    e: json.JsonError => { print(e.message()) }   # expected a Number for Order.items[1].qty, got String
}
print(try { json.parse_as(Order, "{}") } catch { e: json.JsonError => { e.message() } })
# missing field 'id' for Order
```

Decoding uses [`std:reflect`](/std/reflect) to read the struct's
declaration, so it needs no code from you.

## Custom decoding: `FromJson`

When the JSON doesn't match your struct's shape (renamed keys, computed
fields, validation), implement `json.FromJson`. `decode` and `parse_as`
use your `from_json` whenever they reach that type, even nested inside
another struct. The helpers `field(object, name)`, `as_number`,
`as_string`, `as_bool`, `as_vector` and `as_map` check shapes as they go:

```mah
import json from "std:json"

struct Temp { celsius: Number }

impl json.FromJson for Temp {
    fn from_json(value) {
        let f = json.as_number(json.field(value, "fahrenheit"), "fahrenheit")
        Temp { celsius: (f - 32) * 5 / 9 }
    }
}

struct Reading { place: String, temp: Temp }

let r = try json.parse_as(Reading, "{\"place\": \"lab\", \"temp\": {\"fahrenheit\": 212}}") else none
print(r.place, r.temp.celsius)                # lab 100
```

## Errors

`json.JsonError` is an enum with two variants:

- `Syntax { message, line, column }`: the text isn't JSON.
- `Shape { message }`: a value doesn't have the shape asked for, from
  `decode`/`parse_as`, a `FromJson` helper, or `stringify` given something
  JSON can't hold.

`e.message()` gives a complete sentence either way.

## Reference

| Function | |
|---|---|
| `parse(text)` | the value `text` holds |
| `stringify(value, indent = 0)` | `value` as JSON text |
| `parse_as(T, text)` | `decode(T, parse(text))` |
| `decode(T, value)` | a parsed value checked and built as a `T` |
| `decode_ref(type_ref, value)` | like `decode`, for a `reflect.TypeRef` (e.g. one field's `type`) |
| `field(object, name)` | `object[name]`; throws unless `object` is a Map that has it |
| `as_number(v, what = "the value")` | `v` if it's a Number; `what` names it in the error |
| `as_string`, `as_bool`, `as_vector`, `as_map` | the same for the other types |

| Type | |
|---|---|
| `JsonError` | `Syntax { message, line, column }` or `Shape { message }` |
| `FromJson` | trait with `fn from_json(value) -> Self` |

Programs that import `std:json` need bytecode 1.16 (its `decode` is
written over `std:reflect`).
