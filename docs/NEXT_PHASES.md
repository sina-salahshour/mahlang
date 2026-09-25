# Mah: phases after M9

Status: **design notes for future work, not scheduled, not implemented.**
Companion to [`V2_DESIGN.md`](V2_DESIGN.md) (milestones M0–M9: AST pipeline,
heap frames/closures, structs, enums, pattern matching, expression-blocks,
forgiving errors, LSP rename). Nothing here blocks M0–M9 starting, but each
section below ends with what M0–M9's implementation should **not** do that
would make this harder to add later — read those "keep in mind" notes while
building M0–M9, even though the features themselves come after. Also covers
two LSP gaps M7 explicitly left open (cross-file rename, struct/enum/field
rename) — recorded here at the user's request once their scope was clear
enough to identify as real, separate pieces of future work rather than
something to bolt onto M7 itself.

## Match guards

`pattern if cond => expr` — deferred from M4 (`V2_DESIGN.md`'s pattern
matching milestone) so M4 ships without needing an extra conditional edge
in every arm's codegen, but the mechanism composes directly once M4 exists:

- M4's decision-tree codegen for `match` already needs, per arm, a "this
  arm's pattern didn't match, fall through to the next arm's checks" jump
  target (the same shape as v1's `if`/`elif` chain — see
  `docs/RUNTIME.md`'s backpatching section, and the compiler-construction
  skill's "compiling pattern matching" notes).
- A guard is just **one more check in that same chain**: after the
  pattern's structural checks all pass (and its bindings are live), compile
  the guard expression and add one more conditional jump to "next arm" on
  false, *before* falling through to the arm body. No new control-flow
  primitive, no backtracking beyond "try the next arm from the top" (Mah
  guards, like Rust's, don't get to partially-undo a match and try a
  different binding — a failed guard abandons the whole arm).
- Bindings introduced by the pattern are scoped to the arm (pattern match +
  guard + body); a failed guard means those bindings are simply never used,
  not that they need explicit cleanup (heap frames get GC'd later, same as
  anything else — see `V2_DESIGN.md`'s heap model).

**Keep in mind for M4:** structure the per-arm codegen as an explicit
"checks, then body" pair with one shared "next arm" jump target, rather
than inlining checks and body together — that shape is what makes slotting
a guard in later a small addition instead of a rewrite.

## Arrays / lists

Not designed in detail yet. Sketch, consistent with `V2_DESIGN.md`'s heap
model:

- A new heap object kind, `ArrayInstance` (wraps a Python list of element
  values — immediates or heap pointers, same as struct/enum fields).
  Nothing about the existing `Frame`/`Closure`/`StructInstance`/
  `EnumInstance` design assumes a closed set of heap kinds — adding one
  more is additive.
- New syntax needed: literal (`[1, 2, 3]`), indexing (`arr[0]`, both read
  and as an assignment target — a new `Index` AST node alongside
  `FieldAccess`), and almost certainly a `for` loop (`for x in arr { ... }`)
  since iterating v1's `while` + manual index is painful for real list use.
  As of M12 the `for` loop's direction is set: it goes through the
  `Iterable`/`Iterator` system traits and desugars into a `while` + `match`
  over `Iterator.next` (see `docs/TRAITS.md`), with arrays getting native
  impls of those traits.
- Pattern matching over arrays (fixed-length `[a, b]`, or a slice-style
  `[head, ...rest]`) is a natural extension of M4's pattern compiler but is
  explicitly not committed to yet — flagging it so M4's `Pattern` AST base
  case isn't designed in a way that's awkward to add a fifth pattern kind
  to later (it shouldn't be — `WildcardPat`/`BindPat`/`LiteralPat`/
  `StructPat`/`EnumPat` are already an open variant set, not a closed enum
  baked into codegen via, say, a hardcoded 4-way `if`).

**Keep in mind for M0–M9:** none of the array design above requires
anything different from what's already planned; just don't special-case
"there are exactly N heap object kinds" or "there are exactly N pattern
kinds" anywhere (docs, error messages, LSP hover text) in a way that reads
as exhaustive/closed.

## Generics

Not designed in detail yet, but there's a concrete likely direction worth
recording: once the type system (below) makes **types first-class runtime
values**, a generic function is plausibly just an ordinary function that
takes a `Type` value as an explicit parameter — closer to Zig's `comptime`
generics than to Rust/C++ monomorphization or Java/TS type erasure. E.g.
something in the shape of `fn identity(T, x) { ... }` where `T` is passed
a `Type` value at the call site (perhaps inferred from an argument in the
common case, so callers don't usually spell it out). This would mean
generics don't need their own syntax or a separate compile-time-only
mechanism — they'd fall out of "types are values you can pass to functions"
plus whatever staging/execution model the type system phase settles on.
**This is a direction, not a commitment** — revisit once the type system's
execution model (see below) is actually designed, since generics'
feasibility depends entirely on how that gets resolved.

**Keep in mind for M0–M9:** nothing to do differently now; this is purely
a note for whichever future phase tackles it.

## Traits / interfaces

Landed as M12 — see `docs/TRAITS.md` for the design and
`docs/V2_DESIGN.md`'s M12 entry for what changed. The earlier sketch here
proposed purely *structural* traits checked by the future type system; M12
went **nominal** instead (Rust-style `trait` / `impl Tr for T` / inherent
`impl T`, with runtime method dispatch), because method dispatch and the
orphan rule need an explicit record of who implements what. That record
(`Resolver.trait_decls`/`Resolver.impls`) is what a future type checker
would query for trait bounds. System traits (`Printable` today;
`Iterable`/`Iterator` for a future `for` loop) are how built-in operations
get per-type behavior — see `docs/TRAITS.md`'s "System traits" section for
the planned `for` desugaring.

## The type system

The headline future feature, and the one most worth designing carefully
before touching, because its execution model affects everything above it.

### The core idea

Not a separate type-language (no TypeScript-style `T extends U ? A : B`
conditional-type syntax, no distinct "type expression" grammar). Instead:

- **Types are ordinary runtime values.** There's a `Type` kind of value
  (heap object, same as everything else in `V2_DESIGN.md`'s heap model)
  with cases mirroring the value kinds that exist by then: `NumberType`,
  `StringType`, `BoolType`, a struct type (a handle to a declared struct's
  shape — name + field list), an enum type (name + variant shapes), a
  function type (param types + return type), and later an array type, a
  union of types, etc. `struct`/`enum` declarations *are* type values —
  writing `struct Point { x, y }` both declares the shape (as today) and
  makes `Point` usable as a `Type` value wherever one is expected.
- **Type-level programs are just Mah programs.** A function that computes
  or narrows a type is written with the exact same `if`, `for`,
  recursion, and pattern matching as any other Mah function — it just
  happens to take/return `Type` values instead of numbers or strings. E.g.
  (illustrative, not final syntax):

  ```
  fn element_type(t) {
      match t {
          ArrayType { element } => element,
          _ => Never,
      }
  }
  ```

  This is the concrete meaning of "ran during runtime": there is no second
  interpreter, no macro-expansion phase with different rules — type-level
  code is Mah code, executed by (a use of) the same evaluator that runs
  everything else, over `Type` values instead of ordinary ones.
- **Narrowing** is TypeScript-style: inside a branch that has established
  something about a value's shape — an `if`/`match` arm that checked an
  enum's variant, a struct's field, or (once it exists) an explicit type
  test — the checker treats that value as having the narrower type for the
  rest of the branch. Since M4's pattern matching already produces exactly
  this kind of tag/shape-checking control flow, narrowing should hook onto
  the same `Match`/`If`-with-pattern AST rather than inventing a second,
  separate condition syntax for type tests.

### The open question this phase must resolve first

*When* does type-level code actually run, relative to the program it's
checking/describing? Three shapes this could take, not decided here:

1. **A genuine separate type-checking pass** that interprets type-level
   Mah functions (over `Type` values, which stand in for "the type of a
   value" rather than the value itself) before normal codegen/execution —
   closest to a classical type checker, but implemented by reusing the
   real Mah evaluator on a different domain of values.
2. **True staged/comptime execution** — type-level code actually runs
   (for real, producing real `Type` values) as part of compiling the
   program, interleaved with compilation rather than as a wholly separate
   pass (closer to Zig `comptime`).
3. **Fully dynamic** — "type" checks/narrowing are actual runtime checks
   with no separate compile-time pass at all (closest to how v1's
   `assert_not_function` runtime check already works today, just
   generalized) — weakest guarantees, simplest to implement, and possibly
   a reasonable starting point before 1 or 2.

This needs its own design pass when scheduled — don't guess at it now.

### Keep in mind for M0–M9

These are the concrete things to *not* do while building M0–M9, so this
phase isn't blocked or requiring rework later:

- **Don't erase struct/enum declaration metadata after resolve/codegen.**
  Keep a registry (declared name → field list, or name → variant list)
  reachable at runtime by name, not just baked into fixed codegen-time
  field offsets that disappear once M2/M3's codegen runs. A future
  struct/enum `Type` value is naturally "a handle into this registry" —
  it needs the registry to still exist.
- **Keep resolve and codegen as genuinely separate passes** (already the
  M0 plan). Whichever execution model above gets picked, it almost
  certainly wants to run as its own pass between them (or interleaved with
  codegen, for the staged/comptime option) — a single fused
  parse-and-emit pass (v1's current architecture, and exactly what M0
  replaces) would make inserting this very hard.
- **Don't design the `Pattern`/heap-object-kind sets as closed** (see
  Arrays section above) — a `Type` value is just another heap object kind,
  and type tests in `if`/`match` conditions are just another thing
  narrowing can hook onto, as long as pattern/condition compilation in M4
  isn't hardcoded to only ever see number/string/bool/struct/enum shapes.

## Async: `detach` / `.await`

Landed as M10 -- see docs/V2_DESIGN.md's M10 milestone for what shipped
and docs/prototypes/async_model.py for the original validated spike this
is based on.

## TODO: `detach` on a block

Requested 2026-09-25, not scheduled yet. Allow `detach` to take a block
expression, not only a call:

```mah
let x = detach {
    2 + 3
};
"Hello, " + name + "!" + x
```

Likely design, which fits what exists today: `detach { ... }` behaves like
detaching an anonymous zero-argument closure, `detach (fn() { ... })()`.
The block runs immediately as a new task (it can `sleep_async`/`.await`
inside), captures surrounding variables by reference like any closure, and
the expression's value is a `Promise` for the block's tail value. That's
the parser-level desugaring M9's `defer` already uses for its body (a
synthesized zero-param `FnExpr`), so it needs no new resolve logic or
bytecode.

**Open question to settle before implementing:** in the example, `x` is used
directly in string concatenation. With today's semantics `x` is a `Promise`,
so that would print `Hello, …!Promise.Settled { value: 5 }`, and the user
would write `x.await`. If the intent is for a detached value to be awaited
implicitly when it's *used*, that's a much bigger change to the async model
(every operator and call site would have to await Promise operands). Confirm
which is wanted.

Keep in mind: `return`/`break`/`continue` inside a detached block (they'd
apply to the synthesized closure, not the enclosing function or loop, so
probably reject `break`/`continue` there with a clear error), the
tree-sitter/TextMate grammars (`detach_expr` currently only wraps a call),
and the project template's `docs/mah-language.md`.

## Cross-file rename

Landed as M11 -- see docs/V2_DESIGN.md's M11 milestone for what shipped
(a workspace-wide reverse-import-graph search, built on the preprocessor's
own tolerant scanner, feeding a real `Parser`/`Resolver` pass per relevant
file).

## Struct/enum/field rename

Landed as M11, see docs/V2_DESIGN.md -- struct/enum type-name, enum
variant-name, and struct/enum field-name rename in declarations/literals/
explicit patterns, always single-file (structs/enums can't be exported/
imported across files at all). Field-*access* rename (`p.x`) remains
deliberately refused, unsound without a real type system -- still waits
on `docs/NEXT_PHASES.md`'s own "The type system" section above.
