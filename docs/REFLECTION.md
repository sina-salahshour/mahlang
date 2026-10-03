# Reflection, decorators, and hooks

Status: **designed 2026-09-30; M41a, M41b and M41c landed** (type values,
metadata, spread calls, `std:reflect`, `json.decode`; bytecode 1.14;
decorators as metadata, bytecode 1.15; hook traits, function-item impls and
rest parameters, bytecode 1.16). See "M41a: what landed", "M41b: what landed"
and "M41c: what landed" below for where the implementation differs from or
adds to this design.

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
  `"Type"`. `to_string` is the type's declared name (a module-scoped type's
  `__mah_m<i>_` prefix is dropped, docs/MAHC_FORMAT.md §4.3). Types aren't Map
  keys (keys stay String/Number/Bool) and `json.stringify` rejects them
  (`json.JsonError.Shape`).
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
turns them into these types (each is `export`ed; write `reflect.Schema` etc.
outside the module). Type and trait names they carry (`Method.trait_name`,
`TypeRef.Trait`'s `name`, the `Type` values) are display names: a
module-scoped `__mah_m<i>_Named` shows as `Named`.

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
| `implements(t: Type<Unknown>, trait_name: String) -> Bool` | whether the type has any method registered under a trait whose *display* name is `trait_name` (M41s: a module's `lib.Named` is `"Named"`; two traits of that name in different modules both match) |
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
  written as a type annotation (TYPES.md). Its structs and enums are
  exported by the module and, like every module's types since M41s, reached
  as `reflect.TypeRef`, `reflect.Param`, `reflect.Signature`, `reflect.Field`,
  `reflect.Variant`, `reflect.Schema`, `reflect.Method` and
  `reflect.ReflectError` (or bare, with a flat `import "std:reflect"`).
  Before M41s they were global names, so a program that imported
  `std:reflect` **or `std:json`** (which imports it) and declared a `Field`
  of its own got "already declared"; that clash is gone.
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

### M41b: what landed

Everything above, as specified, with these choices (each unspecified or
forced):

- **Function closures are created up front** in a program that has
  decorators (the phase needs every function to exist, and top-level
  functions weren't hoisted before). Nothing can observe it: a function can't
  be named before its declaration. A `let f = fn ...` is a statement, not a
  declaration, so it counts as a top-level `let` for the phase and for the
  top-level-variable rule.
- **What "statement" means for the phase**: anything that isn't a `fn`,
  `extern fn`, `struct`, `enum`, `trait`, `impl` or `test`. Modules are
  found with the preprocessor's source map (file of the decorator, file of
  the statement); a module with no statement runs its phase before the next
  statement of any module after the end of its code. A decorator that calls a
  function reading its own module's `let` sees `none`: only the *name* of a
  `let` is rejected.
- **Forward references**: a decorator may name a top-level function or type
  declared later in the module (they're resolved once every top-level name is
  declared; the closures exist when the phase runs). A top-level `let`, early
  or late, is still the compile error. Ordinary code keeps declaration order.
- **Factories that read a `let`**: a decorator factory that reads its own
  module's top-level `let`, used on that same module's declarations, sees
  `none` (the module's `let`s haven't run); used from an importing module it
  sees the initialized value. Keep constants inside the factory, or in a
  function that returns them.
- **`decorate` takes its values as operands** (`A*`), not from a stack: this
  VM has registers. `kind` is a varuint like `loadtype`'s. The target of a
  `struct`/`enum` is told apart by name, so a struct and an enum sharing a
  name are decorated separately.
- **`reflect.signature` and `reflect.schema` carry the decorators** (one more
  element on the function/parameter/type/field/variant descriptors), so
  `reflect.mh` doesn't call `reflect.decorators` and a program that imports
  `std:reflect` without decorators stays at minor 14. The native exists (arity
  3, since 15) and returns a copy; it isn't reachable from Mah code without
  indices, which aren't public API.
- **Decorator expression shapes**: `@a` is an `Ident`, `@a.b` a
  `FieldAccess`, `@a(x)` a `Call`, `@a.b(x)` a `MethodCall`, so `@Route.make("x")`
  works like it does in an expression. A decorator on a closure parameter,
  nested `fn`, trait method or its parameters, or a variant's field is the same
  compile error as the other misplacements.
- **Docs and decorators**: a doc run above the decorator lines documents the
  declaration; a parameter/field/variant's doc is looked up above its first
  decorator.
- **`find`** is plain Mah over `type_of`/`==`.
- **Formatter** puts a decorator of a `fn`/`struct`/`enum`/method on its
  own line with no blank line before the declaration; the rest stay inline.

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
fn log(f, info: reflect.FnInfo) { ... }
impl reflect.WrapFn for log { fn wrap(self, f, info) { self(f, info) } }
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

*(Revised 2026-10-03, before implementation, from what M41a/M41b
taught: the hook machinery lives in `std:reflect`, not the prelude,
because every program compiles the prelude and would otherwise need
bytecode 1.16; and struct literals name their type, so most checks are
decided at compile time instead of inside the VM.)*

`std:reflect` exports the traits and the info structs they receive
(`Function` isn't a legal annotation, so functions are `Unknown`):

```mah
export struct FnInfo { name: String, function: Unknown }       # function: the original
export struct ParamInfo { name: String, index: Number, function: Unknown }
export struct TypeInfo { type: Type<Unknown> }
export struct FieldInfo { name: String, type: Type<Unknown> }

export trait WrapFn { fn wrap(self, f: Unknown, info: FnInfo) -> Unknown }
export trait WrapParam { fn transform(self, value: Unknown, info: ParamInfo) -> Unknown }
export trait WrapStruct { fn construct(self, value: Unknown, info: TypeInfo) -> Unknown }
export trait WrapField { fn set(self, value: Unknown, info: FieldInfo) -> Unknown }
```

Users write `impl reflect.WrapFn for Log` (or a flat import). **A program
that contains any decorator implicitly imports `std:reflect`** (the
preprocessor adds a hidden import), so the generated code below can call
its helpers; a program without decorators is unchanged.

A decorator is a hook if its runtime type (or, for a function, its item
type) has a method registered under the trait — checked by native
`hooks.has(value, trait_display_name)`, which matches display names like
`reflect.implements` does. Hooks of one target run **closest-first**:
the decorator nearest the declaration first (reverse source order), each
receiving the previous one's result.

**Setup, in the decorator phase.** M41b's phase evaluates and stores each
target's decorators in source order. M41c adds, right after each
*declaration's* own `decorate`s (so a later declaration's decorators see
earlier declarations already wrapped):

- a type `T` with decorators on itself or any field: `reflect.__setup_type(T,
  struct_decorators, [field name: field decorators, ...])`, which keeps
  the hooks (filtered, closest-first) and stores them with native
  `hooks.set_type(T, data)`; nothing is stored when there are none;
- each decorated parameter `i` of function `F`:
  `reflect.__setup_param(F, i, name, decorators)` → `hooks.set_param(F, i,
  data)` when any hook remains;
- function `F` (top-level or method): `F2 = reflect.__wrap_fn(F, name,
  decorators)`: for each WrapFn hook `f = d.wrap(f, FnInfo { name, function:
  F })`; a non-Function result is an `ArgumentError` ("WrapFn.wrap must
  return a function"); then `hooks.adopt(f, F)` gives the final wrapper
  F's **identity**. Codegen stores `F2` into F's global slot, or re-runs the
  method's `defmethod` with `F2`. Only emitted for functions with at
  least one decorator.

**Identity.** Every closure has an identity: its own function index,
unless adopted. `reflect.signature`, `reflect.decorators`, the
`paramhooks` lookup and item-type dispatch all use the identity, so a
wrapped `get_user` still reports `get_user`'s parameters and decorators.
`reflect.find(decorators, f)` matches a function decorator with the same
identity and defining frame as `f` (native `hooks.same_fn`), so finding
a function that has since been wrapped still works. `==` on functions is
unchanged (object identity).

**At run time:**

- **WrapParam** — on every call, after arguments are bound and defaults
  filled, before the body. For each *decorated* parameter (known at
  compile time) codegen emits `paramhooks i` (new opcode: the running
  closure's identity's stored data for parameter `i`, or `none`) and, when
  it isn't `none`, `p = reflect.__run_param(p, data)`. Undecorated
  parameters cost nothing; decorated ones without hooks cost one opcode.
- **WrapField / WrapStruct on construction** — a struct literal names
  its type, so codegen emits, after `newstruct` and only for a type that
  has decorators on itself or a field, `data = hooks.of(v)` and when it
  isn't `none`, `v = reflect.__run_struct(v, data)`: each hooked field, in
  declaration order, gets `set` hooks (read and written with the raw
  natives `hooks.get_field`/`hooks.set_field`, so no hook re-triggers),
  then the struct's `construct` hooks; the result is the literal's value
  (it may be a different value, or a throw rejects it).
  `reflect.construct` does the same after building the value.
- **WrapField on assignment** — `obj.f = v` doesn't know `obj`'s type, so
  in a program that contains any decorator, codegen emits before every
  field assignment `data = hooks.of(obj)` and, when not `none`, `v =
  reflect.__run_field(obj, data, "f", v)` (which runs `f`'s set hooks, or
  returns `v` unchanged). Programs without decorators compile field
  assignment as today. Hooks run in the task that triggered them and may
  `await`; a throw propagates to the call, literal or assignment, or for
  setup, out of the phase as an uncaught error at startup.

Natives (1.16): `hooks.has` (2), `hooks.adopt` (2), `hooks.same_fn` (2),
`hooks.set_type` (2), `hooks.set_param` (3), `hooks.of` (1),
`hooks.get_field` (2), `hooks.set_field` (3). Opcode: `paramhooks`.
Enums and variants have no hooks.

### Checker

WrapFn doesn't change a function's declared type (a wrapper with another
signature fails at runtime, not in the checker). Parameter and field
hooks don't change declared types either. Item types: see above.

---

### M41c: what landed

Everything in "M41c: hooks, function-item impls, rest parameters" above, as
specified, with these additions and choices (each was either unspecified or
forced):

**Rest parameters**

- **Representation**: the rest parameters are the last one or two entries of
  `FnExpr.params` (and `MethodDecl.params`), with `rest` flags on the node (1
  `...`, 2 `**`), so slots, positions, docs, decorators and `arity` need no
  special case. `FunctionDecl.rest` carries the same flags to the bytecode
  (derived from PARAMS' bits; the decoder validates them, message for message
  the same in both VMs).
- **Binding** is the `_bind_params`/`bind_params` of M16 extended in place
  (docs/MAHC_FORMAT.md §6.1): extras go to a new Vector, unmatched keywords to
  a new Map; a rest parameter's own name is not a keyword name; `Argument
  Count is invalid` is only the fast path with no keyword, default or rest.
- **A method call whose function has no ordinary parameter and a `...`
  parameter** (`fn(...args, **kw)`) binds the receiver as the first item of
  that Vector. The spec says nothing on how a wrapper written that way (the
  design's own example) would ever receive a method's `self`; the normal
  "slot 0 is the receiver" rule would put it in `kw`'s place and underflow.
- **Parse errors** (all `SyntaxError`s with positions): "a rest parameter
  ('...' or '**') must come after every ordinary parameter", "a function can
  have only one '...' rest parameter" (`'**'` likewise), "the '...' rest
  parameter must come before the '**' rest parameter", "a rest parameter can't
  have a default value", "'self' can't be a rest parameter", "an extern fn
  can't have rest parameters". The parser decides `**` by position (the start
  of a parameter), like M41a did for arguments.
- `reflect.signature` has `rest` and `kwrest` (`Option<Param>`), so a
  `Signature` literal needs two more fields (none exists outside `reflect.mh`).

**Function-item impls**

- **Resolver**: a type name wins; otherwise a top-level `fn` *declaration*
  (collected before impl headers are registered, since functions are declared
  in a later phase); otherwise "impl targets must be a type or a top-level
  function" when the name is a `let` or a nested `fn` somewhere in the
  program, and the old "Undefined type" for an unknown name. The impl table
  key is `fn#NAME`; `ImplDecl.fn_target` is the function's `FnExpr`, which
  lowering turns into `fn#<index>` (deferred like `decorate`, once every
  closure is lowered). `Self` in such an impl is an error.
- **Orphan rule** compares *source files* from the preprocessor's source map
  (the impl, the function and the trait's declaration positions), not mangled
  prefixes; the message is "an impl for the function 'NAME' must be in its own
  module or the trait's". `impl Mine for lib.f` with `Mine` declared in the
  same file is therefore *allowed* (the impl is in the trait's module); the
  case the rule rejects is a foreign trait for a foreign function
  (`impl lib.Named for lib.f`, `impl reflect.WrapFn for lib.f`). A built-in
  trait counts as declared nowhere.
- **Dispatch**: both VMs keep a flag "some `fn#` method exists" so ordinary
  method calls never build a key. A call tries the identity's table entry and
  falls back to `Function`'s; a trait-restricted call falls through the same
  way. `print` doesn't consult function-item `Printable` impls.
- **Checker**: `TFn` has an `item` marker (`fn#name`) set when a top-level fn
  *declaration*'s name is used as a value; it survives `subst` and `let g =
  f`, unification ignores it, `_method_sig` looks up `impl_methods["fn#name"]`
  for it. The `self` of such a method is unchecked (`TUnknown`), and the
  receiver-inference scan skips `fn#` keys.
- **LSP**: after the functions are declared the resolver looks up every
  fn-target impl's name, so go-to-definition and rename see it as a reference
  (cross-file rename of a function was already there); hover shows `impl fn
  NAME`. Tree-sitter needed nothing (an `impl` target was already an
  identifier).

**Hooks**

- **How codegen reaches the private helpers.** `reflect.__setup_type`,
  `__setup_param`, `__wrap_fn`, `__run_param`, `__run_struct`, `__run_field`
  are plain functions in `mah/std/reflect.mh`, **not exported**: user code
  can't name them (`reflect.__x` is "not exported", a bare `__x` undefined, a
  user's own `__setup_type` doesn't clash since it's a different mangled
  name). Every top-level name of a module is a global slot whether exported or
  not, so `Resolver._resolve_hook_helpers` finds `__mah_m{idx}___setup_type`
  and friends in the global scope (idx from `Preprocessed.module_index`),
  stores their `(0, slot)` addresses on `resolver.hook_helpers` and
  `global_frame.hook_helpers` (which `Codegen` reads, so the three
  `Codegen(global_frame, pp)` call sites keep their signature), and codegen
  calls them with `call`. Nothing was exported under a special name.
- **The hidden import** is `preprocess()` inlining `std:reflect` after the
  user's code (before the prelude, like it) when any scanned file has an `@`
  token and the module wasn't imported already. It binds no name in anyone's
  namespace, so it can't collide with `import reflect from "std:reflect"`, a
  flat import or a variable called `reflect`; the include guard makes a user
  import and the hidden one the same module. `Preprocessed.hidden_reflect` is
  its `(start, end)` range (the LSP's completion skips those symbols).
  Because it comes after the user's code, nothing in `reflect.mh` can have a
  top-level `let` (none does).
- **Minor versions: a conflict in the spec.** `reflect.mh` itself uses
  `hooks.*` natives (`find` -> `same_fn`, `construct` -> `of`, the helpers), and
  a std module's natives are in the file's NATIVES whether or not the program
  calls them. So **every program that imports `std:reflect` or `std:json`**,
  not only decorated ones, is written at 16 (it was 14); a program that
  imports neither and has no rest parameter, decorator or `fn#` impl keeps its
  old minor (`print(1)` is still 4, `std:math` 5, ...). Keeping reflect-only
  programs at 14 would have needed the hook code in another module (and `find`
  and `construct` need it), or conditional compilation of the std module.
  tests/test_decorators.py, test_reflection.py, test_stdlib.py and
  test_bytecode.py's expectations moved to 16 accordingly; test_hooks.py
  asserts what is true. A metadata-only decorator program is 16 too, as the
  task said.
- **`paramhooks function index dest`** takes the function as an operand
  (resolved at lowering like `decorate`) rather than reading "the running
  closure's identity", because frames here don't know their closure and the
  function whose prologue it sits in is known statically. It reads the data
  keyed by that function's own index, so a wrapper (which has the original's
  *identity*) running its own decorated parameters' hooks doesn't pick up the
  original's. `hooks.set_param(f, i, data)` keys by `f`'s identity, which for
  the original, at setup time, is its index.
- **Identity**: `Closure.identity` / `ClosureData.identity` (a `Cell`),
  initially the FUNCTIONS index. `hooks.adopt` mutates and returns the
  *wrapper*: a wrapper that returns a function shared elsewhere (a top-level
  one rather than a fresh closure) changes that function's identity too.
  `reflect.signature`, the decorator lookups (they are keyed by function index
  at `decorate` time, so a wrapper reads the original's), `paramhooks` data
  and `fn#` dispatch use it; `==` and everything else don't. `signature` now
  reads the parameter names and function name from the identity's FUNCTIONS
  entry, not the closure's. `hooks.same_fn` is identity + defining frame, so
  two closures of one function made by a factory differ.
- **What the setup stores** (private structs in `reflect.mh`): `ParamHooks
  { info, hooks }` for a parameter and `TypeHooks { info, struct_hooks,
  field_hooks }` for a struct, so `__run_*` don't index heterogeneous Vectors
  (the checker would reject those literals). `hooks.of` returns them
  or `none`. `FieldInfo.type` is the struct that declares the field (the spec
  gave only `Type<Unknown>`); `ParamInfo.function` and `FnInfo.function` are
  the original function.
- **`hooks.has`** looks a function decorator up under its item type only
  (`fn#<identity>`), not `Function`, per the spec's "its item type"; a closure
  that was never declared with `impl` therefore isn't a hook. It scans the
  whole method table (setup time only).
- **Order**: after a declaration's `decorate`s, codegen emits the type setup
  (struct), then each decorated parameter's `__setup_param(F, i, name,
  decorators)`, then `F2 = __wrap_fn(F, name, decorators)` stored into F's
  global slot; a method also re-emits its `defmethod`s for the slot (every
  trait impl that registered it, including a trait impl's own methods).
  The decorator values are the addresses the `decorate` just used (each
  decorator expression is evaluated once). A function whose wrap list holds
  no `WrapFn` hook is "wrapped" by itself.
- **Runtime checks** exactly as specified: `paramhooks` only for decorated
  parameters, after defaults, with a `jmpf` over the call when `none`;
  `hooks.of` after a struct literal only for types decorated on themselves or
  a field (by declared name; `Self { }` too); `hooks.of` before **every** field
  assignment in a program with any decorator -- including assignments in the
  std modules and the prelude that program contains (a few instructions each) --
  never in a program without decorators. The assigned value is copied to a
  fresh temp first, so assigning from a variable doesn't overwrite it.
  `__run_struct` reads and writes fields with `hooks.get_field`/`set_field`, so
  hooks never re-trigger, and runs each hooked field once, in declaration order.
- **`reflect.find`** with a function: `hooks.same_fn`; with a Type: by type;
  otherwise `==`, as before. `reflect.construct` runs `__run_struct` when
  `hooks.of` gives data, so it must be declared after the helpers in the file
  (functions can't be named before their declaration).
- **Errors**: a non-function `wrap` result is `RuntimeError.ArgumentError`,
  "WrapFn.wrap must return a function", raised from `__wrap_fn` in the decorator
  phase (uncaught at startup, nothing printed first, the same on both VMs). The
  `hooks.*` natives' argument errors are `TypeMismatch`/`NoSuchField` with the
  texts in docs/MAHC_FORMAT.md §4.4.
- **Not done**: hooks for enums and variants; `print` of function-item
  `Printable`; a user-facing way to reach the info structs' constructors other
  than `reflect.FnInfo { ... }` (they are exported, as specified).

**Tooling and tests**: formatter (`...name: T` tight, idempotent), tree-sitter
(`param` gains an optional `rest` marker; regenerated, zero ERROR nodes across
`examples/*.mh` and `mah/std/*.mh`), the VS Code grammar already scopes `...`
and `**` as operators, the project templates' language reference,
`examples/hooks.mh`, www (decorators and functions pages, standard-library,
traits), `mah dis` (`...r`, `**k`, `paramhooks`), `tests/test_hooks.py` (121
tests, both VMs), `mah/std/reflect.test.mh`, vm_diff (`hooks`,
`hooks_wrap_returns_a_non_function`, malformed PARAMS flags, `paramhooks`
operands and `fn#` keys) and Rust decode tests.

---

## Not in scope (later)

- Field `get` hooks / computed fields.
- Decorators on nested functions, closures, `let`s, traits.
- Naming nested functions as impl targets (`impl Tr for outer.inner`):
  decorators with arguments are factories returning structs instead.
- Modules as runtime values (`app.mount(import "./users")`).
- A runtime type test / narrowing (`x is Number`, type patterns).
