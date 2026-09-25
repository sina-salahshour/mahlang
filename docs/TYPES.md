# Mah static types

Status: **design. M21 (syntax) landed; the checker (M22+) isn't implemented yet.** This replaces `docs/NEXT_PHASES.md`'s
"The type system" sketch for now. That sketch treated types as runtime
values. This design is purely static, so nothing about it runs. Types can
still become runtime values later (for `match`-on-type narrowing), and
nothing here gets in the way of that.

## Goals

1. **Optional annotations everywhere a name is declared**: parameters,
   return types, `let`s, struct and enum fields. Anything left out is
   inferred.
2. **Infer as much as possible.** That includes parameter types inferred
   from how the body uses them: `fn g(x, y) { add(x, y) }` with `add(a:
   Number, b: Number)` gives `g(x: Number, y: Number)`. It also includes
   return types, local variables, closure parameters from the expected
   function type (`"abc".map(fn(c) { ... })` gives `c: String`), and
   struct fields from how they're constructed.
3. **Generics** on functions, structs, enums, traits, and impls, with the
   prelude and built-in types fully typed with them.
4. **Three strictness levels**, set per project. Types are advisory by
   default. They can be made compile errors in both `mah run`/`mah build`
   and the LSP.
5. **Zero runtime cost, zero runtime change.** Annotations are erased
   before codegen. The `.mahc` format, the VM, and every program's
   behavior stay exactly as they are. A program that runs today still
   runs, whatever the checker says about it.

Non-goals for now: narrowing (`if x is Number`), union types, type
aliases, explicit type arguments at call sites, nullability checking
(see "none" below), and types as runtime values.

## Syntax

```mah
fn add(a: Number, b: Number = 1) -> Number { a + b }
let name: String = "mah"
let f: fn(Number) -> String = fn(n) { "" + n }

struct Point { x: Number, y: Number }
struct Pair<A, B> { left: A, right: B }
enum Shape { Circle { r: Number }, Empty }
enum Tree<T> { Leaf, Node { value: T, left: Tree<T>, right: Tree<T> } }

fn first<T>(v: Vector<T>) -> T { v[0] }
fn show<T: Printable>(x: T) -> String { x.to_string() }

trait Container<T> {
    fn get(self, i: Number) -> T
    fn size(self) -> Number { 0 }
}
impl<T> Container<T> for Vector<T> {
    fn get(self, i) { self[i] }            # types come from the trait
}
impl Point {
    fn new(x: Number, y: Number) -> Self { Self { x: x, y: y } }
}
```

Grammar additions (`type` is a new nonterminal used only in these places):

```
type        := "fn" "(" [type {"," type}] ")" ["->" type]
             | NAME ["<" type {"," type} ">"]
             | "(" type ")"
type_params := "<" type_param {"," type_param} ">"
type_param  := NAME [":" bound {"+" bound}] ["=" type]   # bound: a trait type, e.g. Iterable<T>
param       := NAME [":" type] ["=" expr]
fn_expr     := "fn" [NAME] [type_params] "(" params ")" ["->" type] block
let_stmt    := "let" NAME [":" type] "=" expr
field_decl  := NAME [":" type]                    # struct fields, enum variant fields
struct_decl := "struct" NAME [type_params] "{" ... "}"
enum_decl   := "enum" NAME [type_params] "{" ... "}"
trait_decl  := "trait" NAME [type_params] "{" ... "}"
impl_decl   := "impl" [type_params] type ["for" type] "{" ... "}"
method_decl := "fn" NAME [type_params] "(" params ")" ["->" type] [block]
for_binding := "let" NAME [":" type]
```

- `->` is a new two-char token. `<`/`>` only mean brackets in type
  position, which is always introduced by `:`, `->`, `fn NAME`, `struct
  NAME`, `enum NAME`, `trait NAME` or `impl`, so expressions never see the
  ambiguity. `>>` isn't a token today, so `Vector<Vector<Number>>` lexes
  as two `>`s. If `>>` is ever added, the type parser must split it.
- `Self` is a type inside `trait`/`impl` bodies.
- Type arguments are never written at a call site. `first(v)`, never
  `first<Number>(v)`: they're always inferred. A type argument that
  nothing determines stays an inference variable until usage settles it.
  `let v = []` is `Vector<?>` until the first `v.push(1)`.
- A `self` parameter can't be annotated: it's always `Self`.
- `impl` with generics: `impl<T> Iterable<T> for Vector<T>`. `impl<T>
  Pair<T, T> { ... }` (an inherent impl on a partly concrete target) is
  **not** supported. Inherent impls must name the type with exactly its
  own parameters, `impl<A, B> Pair<A, B>`, or none, `impl Pair`, which
  means the same.

### Type names

| Type | Meaning |
|---|---|
| `Number`, `String`, `Bool` | as today |
| `Vector<T>`, `Map<K, V>`, `Option<T>`, `Promise<T>` | built-in generic types |
| `Range`, `FromRange`, `ToRange` | the prelude's range structs (Number bounds) |
| `fn(A, B) -> R` | function type. `fn(A)` with no `->` returns `None` |
| `None` | the type of `none` (see below) |
| `Unknown` | anything; turns checking off for that value (see below) |
| `Never` | no value: a `return`/`break`/`continue`, or a block that always does one |
| `Self` | the impl target, inside `trait`/`impl` |
| a struct/enum name, with `<args>` if generic | that type |
| a trait name, with `<args>` if generic | any value whose type implements that trait (see Traits) |
| a type parameter `T` | inside the generic item that declares it |

`Function` stays a runtime type name (`type_name_of`) but isn't a static
type: write `fn(...) -> ...`.

## The type model

### Unknown

`Unknown` is the gradual escape hatch, like TypeScript's `any`: a value of
type `Unknown` can be used as anything, and anything can be passed where
`Unknown` is expected. Calling it, indexing it, or reading a field or
method on it gives `Unknown`, with no errors. It's written explicitly, or
it's inferred when there isn't enough information (the checker "gives
up"). Strictness level `explicit` rejects the inferred kind (see
Strictness). Each `Unknown` remembers which kind it is and where it came
from, so that error can point at the declaration that needs an annotation.

### none

`none` is Mah's null: `v[99]`, `v.pop()` on an empty Vector, a missing
Map key, and a function with no tail value all produce it at runtime,
without wrapping anything in `some(...)`. The checker models that honestly
but leniently:

- The literal `none` has type `None`, and `None` is **assignable to every
  type** (like `null` in Java, or TypeScript without `strictNullChecks`).
  So `let best: Number = none` and `return none` from a `-> Number`
  function are fine.
- `let x = none` with no annotation leaves `x`'s type open. The first
  non-`none` value assigned to it decides it (`x = 5` makes it `Number`).
  If nothing ever does, it's `None`.
- `some(x)` has type `Option<T>`. `Option<T>` is a normal generic enum
  whose `none` variant is the same `none`. `match` on `some(v)`/`none`
  narrows exactly as you'd expect, since that's just enum patterns.
- `v[i]` on a `Vector<T>` is `T`, not `Option<T>`, even though it can be
  `none` at runtime. The same goes for `Map` lookups and `pop`.

The cost: the checker won't catch "this might be `none`" bugs. A later
`strict-none` option can add nullable types (`T?`) without changing
anything else here.

### Assignability

Checking uses one relation, "`S` is assignable to `T`", and one
operation, unification, for inference variables:

1. Either side `Unknown`: yes.
2. `S` is `Never` or `None`: yes.
3. Either side an unbound inference variable: bind it to the other side
   (a lower bound from `None` doesn't bind; see `let x = none`), then yes.
4. `T` is a trait type `Tr<args>`: yes if `S` implements `Tr` with
   arguments unifying with `args` (see Traits). A `S` that's itself the
   trait type `Tr<args'>` or a type parameter bounded by it also counts.
5. Both the same nominal type (`Name<a..>`, `Name<b..>`): compare the
   arguments pairwise. `Option`, `Promise`, and function return types are
   covariant (assignability). Everything else is invariant (unification):
   `Vector`, `Map`, user structs/enums, since they're mutable, and
   function parameters, since contravariance can wait until someone needs
   it.
6. Both function types: `S` may have **fewer** parameters than `T` (a
   callback that ignores the index is fine where `fn(T, Number)` is
   expected, matching `__Iter.call`'s arity check), each of `S`'s
   parameters unifies with `T`'s, and `S`'s return is assignable to `T`'s.
   An `S` parameter with a default can be missing from `T`.
7. Otherwise, no. That's a type error.

Rule 3 makes inference **first-constraint-wins**. It's order dependent,
but it's deterministic, and it's what makes the "no annotation, just
infer" style work without union types.

## Inference

### Items and order

The checker runs as its own pass after `resolve` and before `codegen`,
over the same AST, using the resolver's symbol table for name binding. It
never changes the AST in a way codegen can see.

1. **Declarations first.** Every struct, enum, trait, and impl header is
   registered with its type parameters, field types, and method
   signatures, including for a named `fn` item. An unannotated part gets a
   fresh inference variable. Unannotated struct/enum fields get one
   variable each, **shared across the whole program**: every literal and
   field assignment unifies with it, so `struct P { x }` plus `P { x: 1 }`
   makes `x: Number`. If two sites disagree, the field becomes `Unknown`
   (implicit), with a warning at the second site.
2. **Function bodies, on demand.** Top-level functions and impl methods
   are checked in source order. A call to a function whose body hasn't
   been checked yet checks that function first, depth-first. Recursion
   (the callee is already in progress) uses the in-progress signature as
   is, so mutual recursion is monomorphic, like ML's.
3. **Generalization.** When a function's body is done, its signature's
   remaining free inference variables that don't appear in the
   environment (globals, shared struct-field variables, any in-progress
   function's signature) become its own type parameters. `fn id(x) { x }`
   is `fn id<T>(x: T) -> T`. `let f = fn(x) { x }` generalizes the same
   way (a syntactic function value), and a later `f = ...` reassignment
   is checked against an instance of it.
4. **Defaulting.** Before generalizing, pending operator constraints (see
   below) on still-unbound variables are defaulted to `Number`. So `fn
   add(a, b) { a + b }` is `fn add(a: Number, b: Number) -> Number`.
   Calling it as `add("x", "y")` is then a type error, so annotate it if
   string concatenation is what you meant.
5. **Top-level statements** (the "main program") are checked last, in
   order, as one body. Top-level `let`s are monomorphic globals.

### Expressions

| Construct | Type |
|---|---|
| literals | `Number` / `String` / `Bool`; `none` is `None` |
| `[a, b]` | `Vector<T>`, elements unified; `[]` is `Vector<?>` |
| `[k: v]` | `Map<K, V>`; `[:]` is `Map<?, ?>` |
| `a..b`, `a..=b`, `a..`, `..b` | `Range`/`FromRange`/`ToRange`, bounds checked as `Number` |
| `-x` | `x: Number`, gives `Number` |
| `!x`, `a & b`, `a \| b`, `a == b`, `a != b` | any operands, `Bool` |
| `a - b`, `/`, `//`, `%`, `**` | both `Number`, gives `Number` |
| `a + b` | either side `String`: `String` (the other side is anything); both `Number`: `Number`; otherwise pending |
| `a * b` | `Number * Number`: `Number`; `String * Number` or `Number * String`: `String`; otherwise pending |
| `a < b` etc. | both `Number` or both `String`, gives `Bool`; otherwise pending |
| `x[k]`, `x[k] = v` | through the `Index<K, V>`/`IndexAssign<K, V>` impls of `x`'s type; the one whose `K` fits `k` |
| `f(args)` | instantiate `f`'s signature, match args by position and keyword, check each, result is the return type |
| `obj.m(args)` | look up `m` on `obj`'s type (below), then as a call with `self` = `obj` |
| `obj.field` | the field's type, with the struct's type arguments substituted |
| `Type.f(args)`, `Trait.f(recv, ...)` | the static path's signature |
| `fn(...) { }` | a function type; unannotated params come from the **expected type** when there is one (below), else fresh variables |
| `if`/`match`/block | the unified type of every branch's value. If branches disagree the result is `Unknown` (implicit), **without** an error, since the value may never be used. An `if` without `else` includes `None` |
| `while`/`for` | the unified type of their `break` values, or `None` |
| `detach e` | `Promise<type of e>` |
| `p.await` | `T` for `p: Promise<T>` |
| `sleep_async(ms)` | `ms: Number`, gives `None` |
| `print(...)`, `sin`, `cos`, `input()` | `None`, `Number`, `Number`, `Number` |

**Pending operator constraints**: when an operand's type is still an
unbound variable, the operator records a constraint and re-checks it once
either side becomes known. Anything still pending when the enclosing
function finishes is defaulted (see Inference step 4).

**Expected types (bidirectional checking)**: when an expression is checked
against a known type (an argument to an annotated parameter, the right-
hand side of an annotated `let`, a `return` in an annotated function, a
struct field), that type flows inward before inference runs. The case
that matters most: a closure literal against `fn(T) -> U` gets `T` as its
unannotated parameter types before its body is checked. That's how
`"abc".map(fn(c) { c.len() })` knows `c: String`. Arguments are checked
left to right, and closure arguments are checked last, so the other
arguments get to bind the type variables first (`reduce(fn(acc, v) {
... }, initial: 0)` knows `acc: Number`).

### Statements and patterns

- `let x = e`: `x` gets `e`'s type. With `let x: T = e`, `e` is checked
  against `T` and `x` is `T`.
- `x = e`: `e` must be assignable to `x`'s type. So `let x = 1; x = "s"` is
  a type error, even though it runs.
- **Shadowing**: a variable can't change type, but `let` may declare a new
  variable with the same name in the same scope, Rust-style: `let x = 1;
  let x = "s" + x` is fine, and each `x` has its own type. That's a
  language change (M21). Today a same-scope redeclaration is a compile
  error. Rules: the new `let`'s value is evaluated before the name is
  rebound, so it sees the old `x`. Closures that captured the old `x` keep
  seeing the old variable. `fn x` still can't redeclare a name already
  declared in the same scope (like duplicate items in Rust), and neither
  can parameters or pattern bindings. Only `let` shadows.
- `return e` / `break e`: assignable to the function's return / the
  loop's value type.
- `for let x in e`: `e`'s type must implement `Iterable<T>`, and `x` is
  `T`. `for let x, let i in e` adds `i: Number`.
- `match s { ... }`: each pattern is checked against `s`'s type. Struct/
  enum patterns must name that type (or `s` is `Unknown`/a variable,
  which binds it). Literal and range patterns must fit it. Bindings get
  the field types. A guard is checked as any expression, not necessarily
  `Bool`, since truthiness applies.
- `if`/`while` conditions and guards: any type (truthiness).

### Inferring a parameter from its uses

Beyond "passed to something typed" (which is just rule 3 plus the call
rule), two lookups bind an unbound variable from the name alone:

- `p.name(...)` where exactly one trait declares a method `name`, and no
  inherent impl has one: `p` becomes that trait type. When exactly one
  type has `name` (inherent, or among all traits' implementers only one
  provides it), `p` becomes that type.
- `p.name` (a field read) where exactly one struct has a field `name`: `p`
  becomes that struct.

Otherwise the result is `Unknown` (implicit), and the variable stays open
for later constraints. Both lookups use the resolver's existing method/
field indexes (`_candidates_for_type` and friends).

## Traits and generics

- `trait Tr<P..> { fn m<Q..>(self, ...) -> R ... }`: `self` is `Self`,
  and `Self` and `P..` are in scope in the whole trait. Default method
  bodies are checked once, generically, with `Self` a rigid type that
  implements `Tr<P..>`.
- `impl<Q..> Tr<A..> for Target<..>`: each method's signature is the
  trait's, with `Self := Target<..>` and `P.. := A..` substituted. An
  annotation on an impl method must match that. Unannotated impl method
  parameters just take it.
- **Method lookup** on a receiver type `R`: the inherent impl first (as at
  runtime), then every trait impl whose target unifies with `R`. More than
  one trait providing the name is ambiguous. That's an error today
  already (M13) when the receiver's type is known.
- **Trait types**: a trait name used as a type means "some value that
  implements it", with method calls dispatched dynamically, exactly what
  the runtime does anyway. `fn describe(s: Printable) -> String {
  s.to_string() }`. Struct fields of a trait type are how the prelude's
  adapters are typed (`struct Mapped<T, U> { source: Iterable<T>, f:
  fn(T) -> U }`).
- **Bounds**: `fn f<T: Tr>(x: T)`. Inside `f`, `x` has `Tr`'s methods. At
  the call, `T`'s binding must implement `Tr`. `T: A + B` for several.
  Without a bound, calling a method on a `T` is an error.
- The same trait may be implemented more than once for one type with
  different arguments **only in the built-in declarations** (`Vector<T>`
  is `Index<Number, T>` and `Index<Range, Vector<T>>`), since the runtime
  keeps one impl per (type, trait). The checker picks by the arguments.
  User code keeps the runtime's one-impl rule.

### Typing the built-ins and the prelude

- **Native methods and built-in types** get a declaration file,
  `mah/std/builtins.d.mh`: ordinary Mah trait/impl syntax, parsed with
  bodies optional, never compiled, read only by the checker:

  ```mah
  trait Printable { fn to_string(self) -> String }
  trait Index<K, V> { fn index(self, key: K) -> V }
  trait IndexAssign<K, V> { fn index_assign(self, key: K, value: V) }

  impl String {
      fn len(self) -> Number
      fn char_at(self, i: Number) -> String
  }
  impl Index<Number, String> for String
  impl Index<Range, String> for String      # plus FromRange, ToRange
  impl<T> Vector<T> {
      fn len(self) -> Number
      fn push(self, value: T)
      fn pop(self) -> T
      fn push_start(self, value: T)
      fn pop_start(self) -> T
      fn copy(self, deep: Bool = false) -> Vector<T>
  }
  impl<T> Index<Number, T> for Vector<T>
  impl<T> IndexAssign<Number, T> for Vector<T>
  impl<K, V> Map<K, V> {
      fn len(self) -> Number
      fn keys(self) -> Vector<K>
      fn values(self) -> Vector<V>
      fn has(self, key: K) -> Bool
      fn remove(self, key: K) -> V
      fn copy(self, deep: Bool = false) -> Map<K, V>
  }
  enum Option<T> { some { value: T }, none }
  enum Promise<T> { Pending, Settled { value: T } }
  ```

  The resolver's hand-written native tables (`self.impls[...]["inherent"]`,
  `return_hint`) stay for dispatch and the orphan rule. The LSP's types
  move to the checker. A test cross-checks that every native method in
  those tables has a declaration here, and nothing more.

- **The prelude** gets real annotations in place, which cost nothing at
  runtime:

  ```mah
  trait Iterator<T> { fn next(self) -> Option<T> }
  trait Iterable<T> {
      fn iter(self) -> Iterator<T>
      fn map<U>(self, f: fn(T, Number) -> U) -> Mapped<T, U> { ... }
      fn filter(self, f: fn(T, Number) -> Bool) -> Filtered<T> { ... }
      fn skip(self, n: Number) -> Skipped<T> { ... }
      fn take(self, n: Number) -> Taken<T> { ... }
      fn reduce<A = Vector<T>>(self, f: fn(A, T, Number) -> A = none, initial: A = ...) -> A { ... }
  }
  impl Iterable<Number> for Range { ... }
  impl Iterable<String> for String { ... }
  impl<T> Iterable<T> for Vector<T> { ... }
  impl<K, V> Iterable<K> for Map<K, V> { ... }
  impl<T, U> Iterable<U> for Mapped<T, U> { ... }
  ```

  By rule 6, a callback with fewer parameters than `fn(T, Number) -> U`
  is fine, so `map(fn(c) { ... })` works. `reduce` needs one extra
  feature, a **default for a type parameter** (`<A = Vector<T>>`), used
  when nothing else binds `A`. That's the "no arguments collects into a
  Vector" case. Its `initial` default is an internal sentinel. The
  prelude writes that as a call to an `-> Unknown` helper, so the
  default's own type doesn't clash with `A`. The prelude's internal
  helpers (`__Iter.call`, `require_number`) are typed `Unknown` where
  they're deliberately dynamic.
- The checker always loads the prelude's declarations, even when the
  preprocessor didn't append the prelude to this program, so that
  `Iterable` etc. are known everywhere.

## Strictness

`mah-project.toml`:

```toml
[types]
check = "loose"      # "loose" (default) | "strict" | "explicit"
```

| Level | Type mismatch | Implicit `Unknown` (couldn't infer, no annotation) |
|---|---|---|
| `loose` | LSP **warning**; `mah run`/`build` don't check at all | nothing |
| `strict` | **error** in the LSP and in `mah run`/`build` (compile fails) | nothing |
| `explicit` | **error**, as `strict` | **error** at the declaration (parameter, return, `let`, field) that ended up implicit `Unknown`: "can't infer the type of `x`; annotate it" |

- `explicit` reports only declarations, never every expression that
  touched an `Unknown`, so a single missing annotation gives a single
  error. A generalized type parameter (`fn id(x) { x }`) is a real type,
  not an implicit `Unknown`.
- Explicitly written `Unknown` is allowed at every level.
- Files run outside a project (`mah run foo.mh` with no manifest) are
  `loose`.
- `mah check` (new CLI command) runs the checker and prints every
  diagnostic at the project's level. In `loose` mode it prints them as
  warnings and exits 0.
- The prelude and `builtins.d.mh` are always checked at `explicit`, in
  tests, so the standard library stays fully typed.

Diagnostics use the existing location format (`file#line:col`). A type
error never stops the checker: it reports and carries on with `Unknown`
for the offending expression, so one mistake doesn't cascade.

## Editor support

- **Hover** shows inferred types: `let x: Number`, `fn add(a: Number, b:
  Number) -> Number`, a parameter's type, a field's type, a method's
  instantiated signature at the call site (`map(f: fn(String, Number) ->
  U) -> Mapped<String, U>`).
- **Completion** after `.` uses the checker's receiver type, which
  replaces the resolver's `type_hint`/`return_hint` guesses.
- **Diagnostics**: warnings in `loose` mode, errors otherwise, alongside
  today's compile errors.
- **Inlay hints** (inferred types after `let` names and parameters) are a
  natural follow-up, not in the first cut.
- The tree-sitter and TextMate grammars get type annotations, type
  parameter lists, and `->`, with type names highlighted as types.

## Implementation plan

Each phase lands green with tests, like the M-milestones. Numbered as
milestones, continuing after M20.

- **M21, syntax only. ✅ Landed** (see `docs/V2_DESIGN.md`'s M21 entry). Same-scope `let` shadowing (above). Lexer `->`. Parser: annotations, type parameter
  lists, `impl<...>`, bounds, generic defaults, all stored on the AST
  (`TypeExpr` nodes: `NamedType(name, args)`, `FnType(params, ret)`) and
  **ignored by everything else**. Resolver: type names in annotations
  resolve to known types or declared type parameters, so an unknown type
  name is a compile error at every level. Codegen unchanged. Tree-sitter,
  TextMate, templates, preprocessor scanner (type annotations must not be
  mistaken for references to renamed module names). Tests: parsing, and
  every existing example still runs byte-identical.
- **M21b, `mah format`.** Not type work, but it lands right after M21 so it
  prints the new syntax. A new CLI command that reprints source from the
  parser's output with built-in defaults (line width, indent). It needs
  the lexer to keep comments as trivia attached to tokens (today they're
  dropped). Its options are one object, so a `[format]` section in
  `mah-project.toml` can set them later. Designed separately.
- **M22, the core checker.** `mah/compiler/types.py` (type
  representation, unification, assignability) and
  `mah/compiler/typecheck.py` (the pass). Primitives, functions,
  closures, structs/enums (with generics), operators, `let`/assignment,
  control flow, patterns, `none`, `Unknown`, inference and generalization,
  expected-type propagation. No traits yet: method calls give `Unknown`.
  The `[types] check` manifest key, `mah check`, compile-time errors in
  `strict`/`explicit`, LSP diagnostics.
- **M23, traits and the standard library.** Trait/impl typing, method
  lookup, trait types, bounds, generic defaults, `for` loops, indexing,
  `builtins.d.mh`, the annotated prelude. This is where `"abc".map(fn(c)
  {...})` gets `c: String`.
- **M24, LSP.** Hover types, completion from checker types (retiring
  `type_hint`/`return_hint`), and use-site parameter inference polish.

## Things to keep in mind

- **Positions**: the checker reports against the combined preprocessed
  source, so reuse `driver._location_label`/the LSP's existing position
  mapping. Don't invent a second mapping.
- **Mangled names**: the preprocessor renames module top-level names
  (`__mah_m1_square`). Messages must go through `demangle_message`, and so
  must hover text.
- **Performance**: the LSP re-checks on every keystroke. The checker has
  to be linear-ish in program size. Unification with path-compressed
  variables is. Re-checking the prelude and `builtins.d.mh` isn't: cache
  their declarations once per process.
- **Never let the checker affect codegen**: no AST field codegen reads
  may be written by the checker. A test compiles every example with the
  checker on and off and compares the bytes.
