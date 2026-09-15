# Mah: phases after M9

Status: **design notes for future work, not scheduled, not implemented.**
Companion to [`V2_DESIGN.md`](V2_DESIGN.md) (milestones M0–M9: AST pipeline,
heap frames/closures, structs, enums, pattern matching, expression-blocks,
forgiving errors, LSP rename). Nothing here blocks M0–M9 starting, but each
section below ends with what M0–M9's implementation should **not** do that
would make this harder to add later — read those "keep in mind" notes while
building M0–M9, even though the features themselves come after.

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
  `for`-loop syntax/desugaring (e.g. into a `while` over an index, or a
  dedicated iterator protocol) is an open question for whenever this phase
  is scheduled — not decided here.
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

Also not designed in detail yet, with a similar likely direction: Mah's
structs are already structurally described (a name + field list, no
inheritance) and enums are tagged unions — a "trait" most naturally becomes
a **structural** constraint ("has at least these fields" / "is one of
these enum shapes") checked by the future type system, rather than a
nominal `impl Trait for Type` declaration mechanism bolted on separately.
Concretely this would likely again piggyback on types-as-values: a trait
is a `Type` value describing a required shape, and "does this value satisfy
this trait" is just a function (possibly a builtin) over `Type` values,
using the same if/match narrowing described below. Not committed — same
caveat as generics above.

**Keep in mind for M0–M9:** nothing to do differently now.

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

Not designed in detail yet, but the user has fixed the core model, so it's
recorded precisely here even though it's scheduled well after M0–M9.

### The model

**Async-first, JS-style, single-threaded cooperative concurrency** — not
OS threads, not colored-function opt-in like Rust's `async fn`. The key
correction from an earlier draft of this section: **`detach` is not itself
a scheduling boundary.** Real scheduling (actually yielding control back to
whoever's driving the event loop) only happens at a *genuine* suspension —
a real, scheduled operation like file I/O (implemented now, see below) or
a future network request. Calling a function, detached or not, always
starts running its body **immediately and synchronously**, exactly like a
direct call, for as long as it doesn't hit one of those. So:

- An ordinary call — `foo(x)` — runs synchronously and the caller waits for
  the result, same as it looks and behaves today.
- **`detach <call>`** starts the call **right now, synchronously**, and
  keeps stepping it exactly like a direct call would — *unless and until*
  it hits a real suspension point, at which point (and only then) it stops
  and hands the caller a still-pending `Promise`, letting the caller's own
  code continue past the `detach` line while the rest of the callee runs
  later. If the callee never hits a real suspension point, `detach` has,
  by the time it returns, already run the whole thing to completion —
  the caller just gets an already-resolved `Promise` instead of the raw
  value (the only difference from calling it directly).

  Worked example (verified against the prototype below):

  ```
  fn foo() { print("hey") }

  print("1")
  detach foo()
  print("2")
  ```

  prints `1`, `hey`, `2` — **not** `1`, `2`, `hey` — because `foo` never
  touches anything that needs real scheduling, so `detach foo()` runs it
  to completion in place before the next line even starts.
- **`value.await`** — a postfix pseudo-field, not a prefix keyword like
  JS/Rust's `await value` — suspends the current execution until `value`
  (a `Promise`) resolves, then yields the resolved value. Written as field
  access deliberately: it reuses `FieldAccess`'s existing grammar (`expr
  "." ident`) rather than adding new expression syntax, resolved specially
  at codegen/runtime when the receiver is a `Promise` and the field name is
  literally `await` — the same "reuse an existing production, special-case
  it structurally" approach already used for `some`/`none` in
  `V2_DESIGN.md`.
- **Call lifecycle is JS's**: a synchronous call stack plus a microtask
  queue fed by completions of real scheduled work. This is exactly how a
  JS `async function` behaves (runs eagerly up to its first *real* `await`)
  — the difference from a naive reading of "`detach` = async" is that
  *most* calls, detached or not, never actually reach a real suspension
  point at all, so in practice most `detach`ed calls just run in place.

### Validated with a working prototype

`docs/prototypes/async_model.py` is a standalone (not wired into the real
pipeline) Python spike implementing exactly this policy: a `Promise`, a
`detach` that drives a call synchronously until a real suspension, and
`read_file_async`/`write_file_async` as the first genuinely scheduled
primitives (real file I/O on a background thread, completions drained by a
single-threaded scheduler loop — the "microtask queue"). It reproduces the
`1, hey, 2` example above exactly, and a second scenario shows a `detach`
around a call that *does* hit real file I/O returning immediately, with
the rest of that call's output only appearing once the event loop later
drains the completion. Mah function bodies are modeled as Python
generators there (a plain, never-suspending body is just a generator that
runs to completion without yielding); this is a deliberate echo of
`V2_DESIGN.md`'s calling convention — a generator's frame is heap-resident
and resumable independent of Python's own call stack, which is exactly the
property required of the real interpreter's frame/pc state (see below) —
but it's a stand-in for that spike's purposes, not a suggestion to
implement the real interpreter on top of Python generators.

### Representation

A new heap object kind, `PromiseInstance` (state: pending/fulfilled +
value, plus whatever continuation bookkeeping the scheduler needs) — same
"just another heap kind" pattern as `ArrayInstance`/`Type` elsewhere in
this document; nothing about the existing heap model needs to change to
add it. Error/rejection semantics (does a promise reject, and if so how
does that interact with pattern matching — a `some`/`none`-shaped result,
a third variant, an actual exception?) are explicitly unspecified by the
user so far and need deciding when this phase is designed.

### Why this is the single biggest constraint on M1's design, right now

Real suspensions are rare in practice — gated entirely behind genuinely
scheduled builtins (file I/O now, network later) rather than every call —
but the interpreter still has to support the rare case correctly, and that
shapes M1 regardless of how often it's hit.

A suspended call means: this call's execution needs to pause *mid-function*
and let unrelated code run, then later resume exactly where it left off —
a coroutine, in effect. This is only tractable if a Mah call's state
(where it is, its locals) lives somewhere that isn't the host Python
interpreter's own native call stack — because you cannot pause and resume
an arbitrary point in a live Python call without generators/greenlets/
threads, none of which fit "reuse the same evaluator, single-threaded,
JS-style scheduler."

`V2_DESIGN.md`'s M1 calling convention — heap-allocated `Frame`s, plus an
**explicit** return-info stack (not Python's call stack) driving a flat
`pc`-stepping interpreter loop — already has exactly the right shape for
this: a suspended call is just "stop advancing `pc` for this task, stash
its `(pc, current_frame)`, let the scheduler run something else, restore
`(pc, current_frame)` later and keep stepping." This works *only* if the
interpreter's dispatch loop stays a flat loop over an explicit frame/
return-stack (as M1 already plans) and never becomes a design where a Mah
`call` is implemented as a literal recursive Python function call (e.g. a
tree-walking evaluator that recurses per `Call` node, or a VM that uses
Python's own stack via recursive `run_code` calls) — that would tie a Mah
call's ability to be paused to a live Python stack frame, which can't be
suspended and resumed later without much heavier machinery.

**Keep in mind for M1 specifically:** implement `call`/`ret` (and later
the loop bodies driving them) as an explicit, inspectable, steppable state
machine — `(pc, current_frame, return_info_stack)` mutated by a `while`
loop, not Python call recursion — even though M1 itself has nothing to
suspend yet. This is the one M1 decision that's expensive to redo later if
gotten wrong; everything else about async (the scheduler, `Promise`,
`detach`/`.await` parsing) can be bolted on as a self-contained later
phase without touching M1 again, provided this one property holds.
