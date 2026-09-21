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

Landed as M10 -- see docs/V2_DESIGN.md's M10 milestone for what shipped
and docs/prototypes/async_model.py for the original validated spike this
is based on.

## Cross-file rename

M7 landed single-file rename (variables/parameters/functions), built on a
real symbol table (`compiler/resolve.py`'s `Symbol`/`position_index`) —
and deliberately **refuses outright** the moment a symbol's declaration or
any reference falls outside the entry file's own text segment, rather than
attempting a partial cross-file rename (see `docs/V2_DESIGN.md`'s M7
entry). This section is what closing that gap actually requires — recorded
now because it's a real, identifiable piece of future work, not because
it's scheduled.

### Why it's a genuinely separate feature, not a small extension

Mah's module system is a **textual preprocessing step**, not a compiler
concept: `preprocessor.py` inlines every imported file into one combined
text *before* the real lexer/parser/resolver ever run, alpha-renaming each
inlined module's top-level names to `__mah_m{idx}_{name}` (see
`preprocessor.py`'s own module docstring and `docs/ARCHITECTURE.md`). By
the time `Resolver` builds its symbol table, an imported declaration is
just an ordinary `let`/`fn` binding with a mangled name in the combined
text — there is no marker anywhere saying "this really lives in a
different file, and other files reference it under their own unmangled
spelling." Two consequences follow directly:

1. **Reversing the mangling.** A symbol's *declared* name, as far as
   `Resolver`/`Symbol.name` know it, is the mangled combined-text spelling
   for anything not in the entry file. Renaming it correctly means
   demangling back to the real name the declaring file actually wrote
   (`preprocessor.py`'s own `_MANGLED_RE`/`demangle_message` already do
   this for error messages — the same regex is directly reusable here) and
   editing the *declaring file's own source text* at the real,
   un-mangled name's position, not the combined text's mangled spelling.

2. **Finding every importer, not just the currently-open file's own import
   closure.** The current LSP only ever analyzes the import closure of
   *one* entry file at a time — whatever `path` was handed to
   `preprocess()` for the document currently open in the editor. If that
   document happens to import the file the symbol is declared in, M7's
   existing cross-file *detection* correctly notices this. But a
   *different* file elsewhere in the project that also imports the same
   declaring file is invisible to this single-document analysis entirely —
   there is no workspace-wide index of "which files import which" at all
   today. Correctly renaming a symbol used by three sibling files that all
   import the same library file requires knowing about all three, not just
   whichever one happens to be open when the rename is invoked.

### What it would take

- **A workspace file index**: enumerate `.mh` files in the project (via
  the LSP `workspace/` capabilities, or a simple directory walk from the
  workspace root) and, for each, run just enough of `preprocessor.py`'s
  own directive-scanning (`_match_import`/`analyze_module`, already
  written, no new parsing needed) to know its own import list without
  fully inlining anything — cheap, since this doesn't need the real
  lexer/parser/resolver at all, just the preprocessor's existing
  tolerant scanner.
- **A reverse import graph** built from that index, so "which open-or-
  closed files transitively import file X" is a graph query, not a full
  re-scan per rename request.
- **Per-importer reference search**: for each file that imports the
  declaring file, search *that file's own source text* (via the same
  tolerant scanner, which already understands `.` for namespace access —
  see `preprocessor.py`'s `_SCAN_RE`) for occurrences of the exported name,
  either bare (flat `import "path"`) or as `namespace.member` (namespaced
  `import ns from "path"`) — this is a text-level search per file, not a
  full resolve, since only the *declaring* file's own resolve pass is
  needed to build the initial `Symbol`; an importing file just needs its
  import form and where the name is spelled.
- **A true multi-file `WorkspaceEdit`**: LSP's `changes` field already
  supports `{uri1: [edits], uri2: [edits], ...}` — `server.py`'s rename
  handler doesn't need protocol changes, just to be handed a dict with
  more than one key instead of always one.
- **Careful scope-awareness in each importer**: a flat `import "path"`
  brings the name into the importing file's own scope directly, which
  means that file could *also* have an unrelated local binding with the
  same bare name (shadowing the import, or simply coincidental) — a naive
  text search for the bare name would produce false positives. The
  importing file's own resolve pass (already run for its own diagnostics)
  is what correctly disambiguates "this occurrence resolved to the
  imported symbol" from "this occurrence is an unrelated local" — so this
  isn't just a text substitution, it needs each importer's own `Resolver`
  run too, checked against the declaring file's re-derived original name.

### Keep in mind while this isn't built

Nothing about M7's current design forecloses this — the cross-file
*detection* check (`pp.map_to_source(position).path != pp.entry_path`)
already correctly identifies exactly the boundary this future work needs
to cross, it just currently means "give up" instead of "go find everyone
else." Don't build ad hoc, partial cross-file support (e.g. "also rename
occurrences in files already open in the editor") as a shortcut — that's
the "silently incomplete rename" failure mode M7 explicitly chose to avoid
by refusing outright; a workspace index is what actually closes the gap
correctly, not a bigger heuristic.

## Struct/enum/field rename

Also out of scope for M7 (see its entry in `docs/V2_DESIGN.md`): renaming
a `struct`/`enum` **type name**, or a struct/enum **field name**. These
are genuinely two different problems, with two different levels of
difficulty — worth separating clearly rather than treating as one lump of
future work.

### Type-name rename: tractable without any new infrastructure

A `struct`/`enum` type name (`Point` in `struct Point { x, y }`, `Point {
x: 1, y: 2 }`, a `Point { ... }` pattern) is **syntactically unambiguous**
everywhere it's written — every occurrence is a direct, explicit reference
to the type by name, looked up by exact string match in
`Resolver.struct_decls`/`enum_decls`. This is the same shape of problem
M7 already solved for variables, just against a different (currently
un-instrumented) registry. Concretely: `struct_decls`/`enum_decls` don't
currently record a declaration *position* or a list of *referencing*
positions at all — they're pure `name -> shape` lookup tables, with no
`Symbol`-equivalent. Extending them to also carry a `Symbol`-shaped record
(declaration position + reference list, populated everywhere a
`StructLit`/`EnumLit`/`StructPat`/`EnumPat`/`StructDecl`/`EnumDecl` names a
type) would give type-name rename "for free" via the exact same mechanism
`get_rename_edits` already implements for variables. **This is the
straightforward half of this future work** — no blocking dependency,
just not built yet because M7 scoped itself to the variable/function case
first.

### Field-name rename: blocked on the future type system, not just more code

A field name is a different, harder problem, because of a decision already
made deliberately in M2 (see `docs/V2_DESIGN.md`'s M2 entry): **field
*access* (`p.x`) is validated at runtime, not statically**, precisely
because there is no type system yet to know what struct type an arbitrary
expression's value holds at compile time. This isn't a small gap for
rename specifically — it's the same reason M2 couldn't validate `p.x`
statically at all. Concretely: given `p.x` in isolation, resolving `p`
tells you *an address*, not *a struct type* — a function parameter,
in particular, has no static type annotation anywhere, so `p` could hold
any struct (or enum, or anything) shape depending on what's passed in at
each call site. A field named `x` could simultaneously belong to `Point`,
`Vector`, and a dozen unrelated structs in the same program; without
static types, a rename tool has no sound way to tell, for a given `p.x`,
*which* struct's `x` field is meant — the same field name is not
necessarily the same field.

Two shapes *are* tractable without waiting for the type system, since they
name their type explicitly, exactly like the type-name case above:

- A field name in a **declaration** (`struct Point { x, y }`) or a
  **literal**/**pattern** (`Point { x: 1 }`, `Point { x, y } => ...`) is
  unambiguous, since the struct/enum type is spelled right there.
- Only **field *access*** (`p.x`, and the corresponding assignment
  `p.x = v`) is the ambiguous case — the exact same construct M2 already
  declined to validate statically, for the exact same reason.

So a *partial* field rename (declarations, literals, patterns — but not
plain `p.x` access) is buildable now, using the same `Symbol`-extension
idea as type names. Whether that partial version is worth shipping ahead
of the type system, versus waiting to do all field references correctly
at once, is a real product decision for whenever this is picked up — not
answered here. **Full, sound field-access rename should wait for
`docs/NEXT_PHASES.md`'s own "The type system" section above** to land,
since that's precisely what would let `p.x` resolve to a known struct
shape (or safely refuse when it can't be determined) instead of guessing.

### Keep in mind while this isn't built

- When `struct_decls`/`enum_decls` do eventually gain `Symbol`-style
  tracking, keep it a **separate namespace** from the variable/function
  symbol table (`Resolver.position_index`), exactly as `struct_decls`/
  `enum_decls` already are separate, non-scoped registries from
  `self.scopes` — a struct named `Point` and a variable named `Point`
  coexist today (M2's design) and renaming one must never touch the other.
- Don't attempt an unsound heuristic for field-access rename (e.g. "rename
  every `.x` in the file regardless of the receiver's type") as a stopgap
  — that's the same "confidently wrong" failure mode M7's cross-file
  refusal and M2's runtime-validation choice both deliberately avoided
  elsewhere in this codebase; a field rename that's sometimes silently
  wrong is worse than no field rename.
