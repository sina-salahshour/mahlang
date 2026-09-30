# Reflection, decorators, and hooks

Status: **designed 2026-09-30; M41a landed** (type values, metadata, spread
calls, `std:reflect`, `json.decode`; bytecode 1.14). M41b (decorators as
metadata, bytecode 1.15) and M41c (hook traits, function-item impls, rest
parameters, bytecode 1.16) are not started. See "M41a: what landed" below for
where the implementation differs from or adds to this design.

The motivating user is a backend web framework written in Mah, in the
style of NestJS and FastAPI:

```mah
import http from "framework"

## A registered account.
@http.schema(name: "User")
struct User {
    @http.json("user_name") name: String,
    @http.format("email") email: String,
    @http.skip password_hash: String,
}

## Fetch one user.
@http.get("/users/{id}")
fn get_user(@http.path id: Number, @http.query verbose: Bool = false) -> User throws NotFound {
    ...
}
```

The framework has to read this at runtime: which functions are routes,
their parameters' names, types, defaults and decorators, their return
types and error types (to generate OpenAPI/Swagger and to bind requests
to arguments), and the fields of the structs involved (to decode JSON
into them and describe them). It also needs decorators that change
behavior, NestJS-style: wrap a function, transform an argument, validate
a struct or field.

## Principles

- **Annotations still never change behavior.** `docs/TYPES.md` said
  annotations are erased before codegen. That's relaxed to: *written*
  annotations and doc comments are also kept as metadata that programs
  can read; nothing in the VM acts on them. A program with and without
  annotations runs the same unless it calls `std:reflect`.
- **Only what's written.** Metadata records annotations as the source
  spells them, resolved to the declarations they name. It never records
  the checker's inferences, so the checker still can't affect codegen
  (TYPES.md's "never let the checker affect codegen" rule, and its
  byte-comparison test, stay as they are).
- **Decorators are values; hooks are opt-in.** A decorator is an
  ordinary Mah value attached to a declaration. It changes behavior only
  if its type implements one of four hook traits (M41c).

---

## M41a: type values, metadata, spread calls, `std:reflect`

### Type values

A new runtime value kind, **`Type`**. A bare identifier in expression
position that isn't a variable in scope and names a struct, an enum, or
one of the built-in type names `Number`, `String`, `Bool`, `Function`,
`Vector`, `Map`, `Option`, `Promise`, `RuntimeError`, `None`, `Type`
evaluates to that type. The resolver disambiguates it exactly like a
bare enum unit variant (`FieldAccess.enum_unit_type`): a variable of the
same name always wins.

```mah
let t = User
print(t)                 # User
print(t == User)         # true
print(Number)            # Number
reflect.type_of(3) == Number   # true
```

- Type values are unparameterized: `Vector`, not `Vector<User>` (no type
  arguments in expression position, as TYPES.md already rules). Type
  *descriptors* (below) carry arguments.
- `==` compares identity (same declared type). `type_name_of` a Type is
  `"Type"`. `to_string` is the type's name (demangled). Types aren't Map
  keys (keys stay String/Number/Bool) and `json.stringify` rejects them
  (`JsonError.Shape`).
- Checker: the expression `User` has type `Type<User>` (a new built-in
  generic; for a generic struct, `Type<Pair<?, ?>>` with fresh inference
  variables). This is what lets `json.decode<T>(t: Type<T>, v) -> T`
  return a typed value.
- LSP: hover, go-to-definition and rename treat these uses as references
  to the type (they're already references to its name).

Bytecode: new opcode `loadtype kind u8, index varuint` → pushes a Type.
`kind` 0: a TYPES index (built-in enums 0–2 and user types); `kind` 1: a
primitive: `0` Number, `1` String, `2` Bool, `3` Function, `4` Vector,
`5` Map, `6` None, `7` Type.

### Doc comments

A **doc comment** is a run of full-line comments starting with `##`
(not `#`), immediately above a declaration, with nothing between them
but decorator lines (M41b). Its text is each line with `##` and at most
one following space removed, joined with `\n`. Plain `#` comments are
never docs, so existing comments don't become API text.

Declarations that take docs: top-level `fn` (and `extern fn`), `struct`,
`enum`, `trait`, `impl` methods, struct fields, enum variants, and
function parameters (a `##` line inside the parameter list, above the
parameter). The formatter already keeps `#` comments; `##` ones are
comments to it.

### Metadata (the META section)

A new **optional** section, id `0x82`, written after CODE by every 1.14+
encoder (debug and release targets alike: frameworks need it in
production). A VM that sees no META section answers reflection with
names only (every type Unknown, no docs).

```
META:
  functions: count varuint (= FUNCTIONS count), count × fnmeta
  types:     count varuint (= number of user types in TYPES), count × typemeta

fnmeta   = flags u8                    bit 0: has metadata (0 → nothing else follows)
           doc str?                    (optional-str encoding, as FUNCTIONS' name)
           ntype_params varuint, ntype_params × str
           nparams varuint             (= the function's param_count)
           nparams × parammeta
           returns typeref
           throws u8 (0 = no clause; 1 = clause) [n varuint, n × typeref]

parammeta = type typeref,
            doc str?,
            default u8 (0 = none; 1 = non-constant default; 2 = constant default, then a CONSTANTS index varuint)

typemeta = doc str?, ntype_params varuint, ntype_params × str, body
  struct: nfields × (type typeref, doc str?)            (count from TYPES)
  enum:   nvariants × (doc str?, nfields × (type typeref))

typeref = tag u8, payload
  0 unknown                         (no annotation)
  1 named: kind u8, index varuint   (as `loadtype`), nargs varuint, nargs × typeref
  2 fn:    nparams varuint, nparams × typeref, returns typeref,
           throws u8 (0 | 1 then n varuint, n × typeref)
  3 param: name str                 (a type parameter, `T`)
  4 self                            (`Self`)
  5 never
  6 trait: name str, nargs varuint, nargs × typeref   (a trait used as a type)
```

A **constant default** is a parameter default that's a literal Number,
String, Bool or `none`, or `-` applied to a Number literal. Anything else
is recorded as "non-constant" (it's still evaluated per call as today).

Every function gets an entry, including the prelude's, std modules', and
anonymous closures'. Names in docs and `to_string` are demangled.

### Spread calls

`f(a, ...xs, k: v, **m)`: `...expr` expands a Vector into positional
arguments at that point; `**expr` expands a Map (String keys) into
keyword arguments. Any number of each, mixed with ordinary arguments;
positional items (plain and `...`) must come before keyword items (named
and `**`). A keyword supplied twice (by name or through a Map) is an
`ArgumentError`, as is a non-Vector after `...` or a non-Map / non-String
key after `**`. Method calls take them too: `obj.m(...xs)`.

`detach` on a call with spread arguments is a compile error for now
("spread arguments can't be detached yet").

Bytecode: `callspread` / `callmethodspread`: the callee (or receiver and
method name/trait, as `callmethod`), then a Vector of positional
arguments and a Map of keyword arguments on the stack; binding follows
§6.1 exactly as `callkw` does. Codegen builds the Vector and Map
(`...` appends a copy of the items, `**` merges entries) and uses these
opcodes only when a call contains a spread; other calls compile as today.

### `std:reflect`

The module's natives return plain Vectors/Maps; `mah/std/reflect.mh`
turns them into these types:

```mah
enum TypeRef {
    Unknown,
    Named { type: Type, args: Vector<TypeRef> },
    Fn { params: Vector<TypeRef>, returns: TypeRef, throws: Option<Vector<TypeRef>> },
    Param { name: String },
    SelfType,
    Never,
    Trait { name: String, args: Vector<TypeRef> },
}

struct Param {
    name: String,
    type: TypeRef,
    doc: String,                  # "" when none
    has_default: Bool,
    default: Option<Unknown>,     # some(value) for a constant default
    decorators: Vector<Unknown>,  # M41b; [] until then
}

struct Signature {
    name: String,                 # "" for an anonymous function
    doc: String,
    type_params: Vector<String>,
    params: Vector<Param>,
    returns: TypeRef,
    throws: Option<Vector<TypeRef>>,   # none = no `throws` clause written
    decorators: Vector<Unknown>,       # M41b
}

struct Field { name: String, type: TypeRef, doc: String, decorators: Vector<Unknown> }
struct Variant { name: String, doc: String, fields: Vector<Field>, decorators: Vector<Unknown> }
enum Schema {
    Struct { type: Type, doc: String, type_params: Vector<String>, fields: Vector<Field>, decorators: Vector<Unknown> },
    Enum { type: Type, doc: String, type_params: Vector<String>, variants: Vector<Variant>, decorators: Vector<Unknown> },
}

struct Method { name: String, function: Function, is_method: Bool, trait_name: Option<String> }

struct ReflectError { message: String }     # impl Error
```

Functions:

| function | result |
|---|---|
| `type_of(value) -> Type<Unknown>` | the value's runtime type |
| `signature(f: Function) -> Signature` | from META (names only without it) |
| `schema(t: Type<Unknown>) -> Option<Schema>` | `none` for a primitive |
| `methods(t: Type<Unknown>) -> Vector<Method>` | every method registered for the type (inherent first, then trait methods by trait name, each by method name, sorted) |
| `implements(t: Type<Unknown>, trait_name: String) -> Bool` | whether the type has any method registered under that trait |
| `call(f, args = [], kwargs = [:])` | `f(...args, **kwargs)` |
| `construct(t, fields: Map<String, Unknown>) -> Unknown throws ReflectError` | a struct from exactly its fields (a missing or unknown field is an error naming it) |
| `construct_variant(t, variant: String, fields: Map<String, Unknown>) -> Unknown throws ReflectError` | an enum value |
| `find(decorators: Vector<Unknown>, target) -> Option<Unknown>` | M41b: the first decorator whose type is `target` (a Type), or that `==` `target` (a function) |

Natives (1.14, all synchronous): `reflect.type_of` (1), `reflect.signature`
(1), `reflect.schema` (1), `reflect.methods` (1), `reflect.implements` (2),
`reflect.construct` (2), `reflect.construct_variant` (3). `call` and
`find` are plain Mah.

### `json.decode`

`std:json` gains `decode<T>(t: Type<T>, value: Unknown) -> T throws
JsonError` and `decode_ref(r: TypeRef, value: Unknown) -> Unknown throws
JsonError`, plus `parse_as<T>(t: Type<T>, text: String) -> T throws
JsonError` (`decode(t, parse(text))`). Written in Mah over `std:reflect`:

- `Number`/`String`/`Bool`: the value must have that type.
- `Vector<T>`: each item decoded as `T`. `Map<String, V>`: each value as
  `V`. Without arguments, items/values pass through.
- `Option<T>`: `none` stays `none`; anything else is `some(decode T)`.
- A type that implements `FromJson`: its `from_json` (found with
  `methods`), as today.
- A struct: the value must be a Map; each field decoded by its declared
  type. A missing key is `none` if the field's type is `Option<...>`,
  else `JsonError.Shape` "missing field 'x'". Unknown keys are ignored.
- An enum: a String names a unit variant; a one-key Map `{"Variant":
  {...}}` a variant with fields (the inverse of `stringify`).
- `Unknown`, a type parameter, `Self`, a trait, a function type: passed
  through unchanged.
- Errors say where: "expected a Number for user.age, got String".

### M41a: what landed

Everything above, as specified, with these additions and choices (each was
either unspecified or forced):

- **A helper opcode, `spread`** (`0x3C`, operands target `A`, source `A`,
  keyword `B`). The spec says codegen builds the positional Vector and the
  keyword Map but names no way to append/merge; `spread` is it (append a
  Vector's items to a Vector, merge a Map into a Map), and the run-time
  checks the spec lists (a non-Vector after `...`, a non-Map or non-String
  key after `**`, a keyword given twice) live in it. Minor 14, like the
  other two. `loadtype`'s two operands are encoded as `varuint`s (`N`), which
  for kinds 0/1 and primitive codes 0-7 are the same bytes as a `u8`.
- **The natives' shapes.** They return plain Vectors: a type descriptor is
  `[tag, ...]`; `signature` is `[name or none, doc, type_params, params,
  returns, throws or none]` with parameters `[name, type, doc, has_default,
  is_constant, constant]`; `construct`/`construct_variant` return `[true,
  value]` or `[false, message]` (like std:fs's results) and reflect.mh
  throws `ReflectError`. docs/MAHC_FORMAT.md §4.4 has them all.
- **`reflect.mh` uses `Unknown` where the design says `Function`**
  (`signature(f)`, `Method.function`, `call(f, ...)`): `Function` can't be
  written as a type annotation (TYPES.md). Its structs and enums are global
  names like every struct: `TypeRef`, `Param`, `Signature`, `Field`,
  `Variant`, `Schema`, `Method` and `ReflectError` exist in any program that
  imports `std:reflect` **or `std:json`**, which imports it for `decode`, so
  a program that also declares one of those names, and imports either module,
  gets the usual "already declared" error.
- **`std:json` now needs 1.14**: `json.mh` imports `std:reflect`, whose
  natives are 1.14, so every program that imports `std:json` is written at
  minor 14 (it used to be 7).
- **`reflect.methods` lists only Mah-code methods**: a native target (the
  built-in types' `Printable`, `len`, ...) is not a Function value, so
  `methods(Number)` is empty while `implements(Number, "Printable")` is true.
  A trait implemented with no methods registers nothing, so `implements` is
  false for such a marker trait.
- **`Type` joins `BUILTIN_TYPE_NAMES`**: a program can't declare a struct,
  enum or trait called `Type` (it could before, if nothing else used it).
  `Type<T>` needs exactly one argument in an annotation.
- **`Self` in expression position** inside an impl is that impl's type (a
  Type value), like `Self` in `Self { ... }`.
- **Docs need the declaration to be first on its line**: a `##` run above
  `struct S { a, b }` documents `S`, not `a`. A blank line or a plain `#`
  comment ends the run. `##` comments are kept in a lexer side table
  (`Lexer.doc_comments`, `doc_above`); the parser reads them, and the token
  stream is unchanged. The LSP's hover text drops the `##` marker.
- **The checker** gives a bare type name `Type<T>` and skips arity checks for
  a call with spread arguments (it still checks each spread expression, and
  the call's errors). `SpreadArg` (in `Call.args`, or `(None, SpreadArg,
  pos)` in `kwargs`) is the AST node; the formatter prints it tight.
- **`std:reflect` has no `find`**: it's the M41b decorator lookup.
- **`type_name_of`**, which the M41a test list mentions, isn't a Mah
  function; the tests use `reflect.type_of(x)` printing `Type`.
- **`annotations compile to identical bytecode`** (tests/test_type_syntax.py)
  became "identical code, functions, types, natives, handlers and minor,
  ignoring META and the tables it adds to": the bytes differ by META now.

---

## M41b: decorators

### Syntax

```
decorator := "@" NAME { "." NAME } [ "(" call_args ")" ]
```

Not an arbitrary expression, so `@a b: Number` can only mean decorator
`a` on parameter `b`. `call_args` are ordinary call arguments (keywords
and, from M41a, spreads allowed).

Where they go: before a top-level `fn`/`extern fn` (and before `export`
when there is one: `@get("/") export fn f`), a top-level `struct`/`enum`,
an `impl` method, a struct field, an enum variant, and a parameter of any
of those functions. Several may be stacked; each decorator of a `fn`/
`struct`/`enum`/method on its own line by convention (the formatter puts
them there), parameter/field/variant decorators inline.

**Not allowed** (compile errors): on nested `fn`s, anonymous closures,
`let`s, `trait` declarations or their methods, `impl` blocks themselves.

### Evaluation

Decorator expressions are evaluated **eagerly, once**, in a *decorator
phase* per module: at the start of that module's top-level code, after
every function is defined and every method registered, before the
module's first statement, in source order. Modules are inlined
dependency-first, so a decorator can call anything from the modules it
imports, including functions that use their top-level `let`s. Within its
own module, a decorator may use functions, types and literals but not the
module's own top-level `let` variables, which haven't run yet: that's a
compile error ("a decorator can't use the top-level variable 'x': it runs
before it; use a function or a literal").

The values are stored per target, in source order (top to bottom), and
never change. `reflect.signature(f).decorators`, `.params[i].decorators`,
`reflect.schema(T)`'s `decorators` on the schema, fields and variants
return them.

Bytecode (1.15): opcode `decorate kind u8, a varuint, b varuint, count
varuint` pops `count` values (pushed in source order) and stores them for
the target: kind 0 function `a`; 1 parameter `b` of function `a`; 2 type
`a` (TYPES index); 3 field `b` of struct `a`; 4 variant `b` of enum `a`.
A target is decorated at most once per run. New native
`reflect.decorators(kind, a, b)` (3) returns a copy of the stored Vector;
`reflect.mh` fills the `decorators` fields from it.

### Tooling

Parser/AST (decorator lists on FnExpr/params/StructDecl/fields/EnumDecl/
variants/MethodDecl), resolver (the names are ordinary references;
the top-level-`let` rule above), checker (each decorator is type-checked
as an expression; no constraint on its type), formatter, tree-sitter
grammar and highlights, LSP hover/go-to-definition/rename inside
decorators, the project templates' language reference.

---

## M41c: hooks, function-item impls, rest parameters

### Rest parameters

```mah
fn log_all(prefix: String, ...items: Vector<Unknown>, **options: Map<String, Unknown>) { ... }
```

`...name` collects the extra positional arguments into a new Vector;
`**name` collects keyword arguments that match no parameter into a new
Map, in the order given. At most one of each, `...` before `**`, both
after every ordinary parameter; no defaults on them. Without `**`, an
unknown keyword is still an `ArgumentError`; without `...`, too many
positional arguments still are. The annotation is the collection's type
(`Vector<T>` / `Map<String, T>`), defaulting to Unknown items.

PARAMS flags (1.16): bit 1 = collects positional rest, bit 2 = collects
keyword rest (on the last one or two parameters). `Function.arity()`
counts them. `Signature` gains `rest: Option<Param>` and
`kwrest: Option<Param>` (not listed in `params`).

### Function-item types: `impl Tr for somefn`

Every **top-level** named `fn` has its own nominal type, the *item
type* of that function, written by its name in `impl` position only:

```mah
fn log(f, info: FnInfo) { ... }
impl WrapFn for log { fn wrap(self, f, info) { self(f, info) } }
impl log { fn describe(self) -> String { "logs calls" } }
```

- `impl` target lookup: a type name first; else a top-level `fn` in
  scope (after the preprocessor's renaming — the preprocessor must now
  rewrite a function name in `impl` target position like any reference).
- Runtime: method-table entries for an item type are keyed by the type
  name `fn#<function index>` (the FUNCTIONS index; `#` can't occur in a
  Mah name, so no clash). Dispatch on a Function value tries
  `fn#<its function's index>` first (its *identity* index, see wrapping
  below), then `Function`. `type_name_of` stays `"Function"`.
- Checker: a top-level fn's name has its item type, assignable to its
  ordinary `fn(...) -> R` type anywhere; method lookup on the item type
  finds these impls. Nested `fn`s and closures have no item type.
- Orphan rule: the `impl` must be in the module that declares the
  function or the one that declares the trait.

### Hook traits

Declared in the prelude (so no import is needed to implement them), with
the info structs they receive:

```mah
struct FnInfo { name: String, function: Function }           # function: the original
struct ParamInfo { name: String, index: Number, function: Function }
struct TypeInfo { type: Type<Unknown> }
struct FieldInfo { name: String, type: Type<Unknown> }

trait WrapFn { fn wrap(self, f: Function, info: FnInfo) -> Function }
trait WrapParam { fn transform(self, value: Unknown, info: ParamInfo) -> Unknown }
trait WrapStruct { fn construct(self, value: Unknown, info: TypeInfo) -> Unknown }
trait WrapField { fn set(self, value: Unknown, info: FieldInfo) -> Unknown }
```

A decorator is a hook if its runtime type (or item type) implements the
trait. Hooks of one target run **closest-first**: the decorator nearest
the declaration first (reverse source order), each receiving the
previous one's result. `std:reflect`'s `Signature` etc. give richer
information from `info.function`/`info.type`.

- **WrapFn** — functions and `impl` methods. At the end of the
  decorator phase for that function: `f = d.wrap(f, info)` for each hook;
  the result replaces the function's global slot (or re-registers the
  method with `defmethod`). The runtime then marks the final wrapper
  closure with the original's function index as its *identity*:
  `reflect.signature`, `reflect.decorators`, and item-type dispatch all
  use the identity, so a wrapped `get_user` still reports `get_user`'s
  parameters and decorators. Calls that happened before the phase (only
  possible from earlier-module decorators) saw the unwrapped function.
  A wrapper returning a non-Function is an `ArgumentError`.
- **WrapParam** — on **every call**, after arguments are bound and
  defaults filled, before the body: `value = d.transform(value, info)`
  per hook, stored back into the parameter. Only parameters with at
  least one hook pay anything: codegen emits, at the function's entry,
  for each *decorated* parameter, a check (opcode `hasparamhooks fn,
  i` → Bool) and, when true, a call to the prelude helper that runs them.
- **WrapField** — on struct construction (literals and
  `reflect.construct`) for each field that has hooks, in declaration
  order, and on every `obj.field = v` assignment to it: `v =
  d.set(v, info)` per hook, then stored.
- **WrapStruct** — after WrapField hooks on construction: `value =
  d.construct(value, info)` per hook; the final value is the literal's
  result (it may be a different instance, or a throw to reject it).

Construction and assignment are compiled without knowing the type, so
the VM decides: types with field or struct hooks get a *hooked* flag
(set by the decorator phase through a native); `newstruct` and
`setfield` on a hooked type call the prelude helpers
(`__run_struct_hooks(value)`, `__run_field_hooks(obj, field, value)`,
which the prelude registers once with native `hooks.install`) instead of
finishing directly, and the opcode's result is the helper's return
value. Unhooked types pay one flag check. The helpers use raw natives
(`reflect.__raw_set`) so they don't re-trigger hooks.

Hooks run in whatever task triggered them and may `await`. A hook that
throws propagates to the declaration's use (the call, the literal, the
assignment) — or, for WrapFn, out of the decorator phase as an uncaught
error at program start.

### Checker

WrapFn doesn't change a function's declared type (a wrapper with another
signature fails at runtime, not in the checker). Parameter and field
hooks don't change declared types either.

---

## Not in scope (later)

- Field `get` hooks / computed fields.
- Decorators on nested functions, closures, `let`s, traits.
- Naming nested functions as impl targets (`impl Tr for outer.inner`):
  decorators with arguments are factories returning structs instead.
- Modules as runtime values (`app.mount(import "./users")`).
- A runtime type test / narrowing (`x is Number`, type patterns).
