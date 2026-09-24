# Traits, `impl`, and system traits

Status: **landed as M12, extended in M13** (see `docs/V2_DESIGN.md`'s M12/M13 entries for what
changed where). This file is the design reference: the language rules, how
dispatch works at runtime, and how future system traits (`Iterable` for a
`for` loop, and friends) plug into the same machinery.

## The language

```mah
trait Shape {
    fn area(self)                       # required method (no body)
    fn name(self) { "shape" }           # default method, inherited unless overridden
    fn unit()                           # static function: first param isn't `self`
}

struct Rect { w, h }

impl Shape for Rect {
    fn area(self) { self.w * self.h }
    fn unit() { Rect { w: 1, h: 1 } }
}

impl Rect {                             # inherent impl: Rect's own methods/functions
    fn new(w, h) { Self { w: w, h: h } }
}

let r = Rect.new(2, 3)                  # static call:          Type.fn(args)
print(r.area())                         # method call:          dynamic dispatch
print(Shape.area(r))                    # trait-qualified call: receiver is first arg
print(Rect.area(r))                     # type-qualified call:  explicit self
```

- **Method vs static function**: a trait/impl `fn` whose first parameter is
  literally `self` is a method (`value.m()`); otherwise it's a static
  function (`Type.f()`). `self` is only legal as that first parameter; it
  can't be `let`-bound or pattern-bound. `Self` is only legal inside `impl`
  method bodies (including closures nested in them) and means the impl's
  target type — in struct/enum literals, unit variants, patterns, and
  static calls (`Self.new(1)`). It's rejected in trait default bodies,
  since those don't know the concrete type.
- **Top-level, order-independent items.** `trait` and `impl` are only
  allowed at the top level (imported modules are inlined at top level, so
  they count). Top-level `struct`/`enum`/`trait`/`impl` are hoisted — usable
  before their declaration. Method bodies are resolved after every other
  top-level statement, so they can reference any top-level name; at runtime
  every method closure is created and registered before the first
  top-level statement runs.
- **Built-in types** have names so they can be `impl` targets and appear in
  error messages: `Number`, `String`, `Bool`, `Function`, `Option`,
  `Promise` (`runtime_values.BUILTIN_TYPE_NAMES`). User types can't reuse
  these names or `Self`, and a trait can't share a name with any type.
- **Orphan rule** (who may write which `impl`):
  - `impl T { }` — T must be a user-declared struct/enum. `impl Promise { }`
    is an error.
  - `impl Tr for T { }` — the trait or the type must be user-defined.
    `impl MyTrait for Promise` ✓, `impl Printable for Point` ✓,
    `impl Printable for Number` ✗.
- **Impl checking** (compile time): every required trait method present, no
  extras, same parameter count, same method-vs-static shape; no duplicate
  `impl Tr for T`; no duplicate inherent method names across `impl T` blocks.

## Dispatch

`x.m(args)` compiles to a `callmethod` opcode; the interpreter looks up
`(type_name_of(x), "m")` in a runtime method table populated by `defmethod`
opcodes at program start:

1. an inherent method of that type wins;
2. otherwise, if exactly one implemented trait provides `m`, use it;
3. if two or more traits provide it → runtime error asking for
   `Trait.m(x, ...)`, which dispatches restricted to that trait;
4. a static function found this way → error pointing at `Type.m(...)`.

Dispatch is dynamic because Mah has no static types yet: nothing at compile
time knows what type `x` holds. `Type.f(args)` *is* resolved at compile
time — each impl fn lives in a hidden global slot, so a static call is an
ordinary `call` of that slot. A real variable of the same name always wins
over a type name (`let Point = 3; Point.new()` calls a method on a Number),
the same disambiguation rule as enum unit variants.

## System traits

A system trait is declared by the runtime, not by user code
(`runtime_values.SYSTEM_TRAITS`). Every built-in type implements every
system trait **natively** (Python functions in `code_interpreter.py`'s
`NATIVE_TRAIT_METHODS`), and user types opt in with a normal `impl`.
Runtime features that need behavior from a value go through the trait
rather than hard-coding per-type logic.

### `Printable { fn to_string(self) }` (M12)

`print(v)` and string concatenation (`"x" + v`) format values via
`Printable`: a user impl's `to_string` is called (and must return a
String); otherwise the built-in formatting is used, which recurses through
`Printable` for nested values (`some(p)`, a struct field holding `p`). A user
struct without an impl still prints structurally (`Point { x: 1, y: 2 }`),
but `p.to_string()` is a "no method" error — the Display-vs-Debug split
Rust uses.

Calling Mah code from *inside* an opcode (e.g. `print` needing a user
`to_string`) uses `invoke_sync`: it runs the closure on a fresh `Task` via
the already-re-entrant `step_task` and returns its value. A callee that
tries to suspend (awaits a pending Promise) is a runtime error there, since
the opcode that asked for the value can't be suspended half-way.

### Planned: `Iterable` / `Iterator` and `for` (not implemented)

The shape this is designed to take, so nothing above has to change:

```mah
trait Iterator { fn next(self) }        # some(item) / none when exhausted
trait Iterable { fn iter(self) }        # returns an Iterator

for x in expr { body }
```

`for` desugars at parse or codegen time into the existing pieces — no new
dispatch mechanism:

```mah
{
    let __it = Iterable.iter(expr)      # (or expr itself, if it's already an Iterator)
    while true {
        match Iterator.next(__it) {
            some(x) => { body }
            none => { break }
        }
    }
}
```

- Both calls are ordinary `callmethod`s in the compiled code, so they can
  suspend like any other call — `invoke_sync` is only for runtime-internal
  calls like `print`'s.
- `break`/`continue` inside `body` reuse the `while` loop's existing
  machinery (and M9's defer unwinding).
- Built-in iterables (arrays, once they exist, a `range(a, b)`, maybe
  String) would get native `Iterable`/`Iterator` impls, added to
  `SYSTEM_TRAITS` + `NATIVE_TRAIT_METHODS` the same way `Printable` is.
- `for` is already a reserved keyword (it's also used in `impl Tr for T`).

Other system traits likely to follow the same pattern: `Eq` (for `==` on
structs), `Ord` (`<`/`>`), `Awaitable`.

## Relationship to the future type system

`docs/NEXT_PHASES.md` had sketched traits as purely *structural*
constraints. M12 made them **nominal** (`impl Tr for T` declarations,
Rust-style) instead, because method dispatch and the orphan rule need an
explicit record of who implements what. The resolver keeps that record
(`Resolver.trait_decls`, `Resolver.impls`) and it's exactly what a future
type checker needs to answer "does `T` implement `Tr`" — trait bounds on
generic functions would be predicates over that registry, still in keeping
with the "types as values" direction. Nothing about dispatch has to change
when static types arrive; a checker can later reject calls that
would fail at runtime today, or resolve some of them statically.

## Field closures, `detach` on methods, and editor support (M13)

- **Calling a function stored in a field**: `p.f(args)` first does normal
  method lookup; if the type has no method `f` (or only a *static* `f`) and
  the struct/enum value has a field `f`, the field's function is called
  with exactly `args` (no `self`). Methods win over same-named fields, and
  an ambiguity error still wins over the field. `Trait.f(x)` never falls
  back to a field.
- **`detach` takes any call chain** and detaches its *last* call:
  `detach obj.m(1)`, `detach Type.make()`, `detach Trait.m(x)`,
  `detach s.field_fn()`, and `detach a.b().c().await` meaning
  `(detach a.b().c()).await` (so `detach work().await` keeps its meaning).
  Dynamic ones compile to a `detachmethod` opcode sharing `callmethod`'s
  lookup (`find_method` in the interpreter).
- **LSP**: hover, go-to-definition and completion work on method names.
  The resolver records a best-effort *type hint* per expression — `self`
  inside an impl, literals, and variables bound from those or from a static
  fn whose last expression is `Self { .. }`/a literal, dropped if the
  variable is reassigned to anything of another type. With a hint, results
  are exact; without one, hover lists every implementation, go-to-definition
  returns all of them (a list of locations), and `x.` completes every known
  method name. Hints are editor-only and never affect compilation.

## Known limitations (deliberate, for now)

- Type hints are local and syntactic: parameters (other than `self`) have
  no hint, and a static fn's return type is only known when its body ends
  in a literal.
- No method rename, `textDocument/implementation`, or signature help.
- `Tr.f()` for a *static* trait function can't pick an implementation
  (there's no receiver); call it on the concrete type. Two traits providing
  the same static fn name on one type make `Type.f()` ambiguous, with no way
  to qualify it.
- Traits, like structs and enums, are global across imported files and
  need no `export`.
- No bound-method values (`let f = p.area` is a field access, and fails).
