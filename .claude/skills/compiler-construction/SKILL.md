---
name: compiler-construction
description: Reference concepts and terminology for designing/implementing language runtime features that Mah's current MVP lacks — activation records, static/dynamic chain pointers, heap allocation and layout, closures, tagged unions for enums, pattern-match compilation, and GC-aware (but GC-less) design. Use when designing or implementing Mah v2 runtime/language features, or discussing tradeoffs for them, not for routine grammar-only additions (see mah-add-feature for that).
---

# Compiler construction concepts for Mah v2

Grounded in Mah's actual gaps — see `docs/RUNTIME.md`'s "why this breaks
recursion and closures" before anything here. This is a concept/vocabulary
reference for the redesign, not a step-by-step workflow.

## Activation records (stack frames)

The fix for "every variable gets one address forever" (`docs/RUNTIME.md`):
allocate a fresh **frame** per call, addressed relative to a **frame
pointer (FP)** that changes every call, instead of Mah's current fixed
global addresses. A frame typically holds: parameters, locals, saved return
address, and (see below) a way to reach enclosing frames.

## Static and dynamic chain pointers (SCP / DCP)

The classic mechanism (Pascal/Algol-family) for nested functions and
lexically-scoped closures, and the likely backbone of Mah's new calling
convention:

- **Dynamic chain pointer (DCP)** — a.k.a. dynamic link. Saved in each
  frame, points to the **caller's** frame (whoever's `call` got us here).
  Used only to tear down the frame and return — walking it traces the
  actual call sequence at runtime, which for a recursive function is a
  chain of many frames of the *same* function.
- **Static chain pointer (SCP)** — a.k.a. static link. Saved in each frame,
  points to the frame of the **lexically enclosing** function (where this
  function was *written*, not where it was *called from*). Walking it N
  hops up resolves a reference to a variable declared N scopes out —
  this is what makes a nested function/closure able to read and, critically,
  **write** a variable in an enclosing scope, live, without copying it.
- These are independent and usually both needed: DCP unwinds the call
  stack, SCP resolves non-local names. A closure "capturing by reference"
  (what the user wants, JS-style, as opposed to capturing a value snapshot)
  is exactly "the closure carries a pointer to the enclosing frame" — i.e.
  a live static link — rather than copying the variable's value into the
  closure at creation time.
- Alternative to a chain walked at runtime: **display** (an array of
  pointers, one per lexical nesting depth, updated on call/return) —
  O(1) non-local access instead of O(depth) chain walking, at the cost of
  updating the display on every call/return. Worth considering once nesting
  depth in practice is known; SCP chain-walking is simpler to implement
  first and almost certainly fine for Mah's expected program sizes.

## Heap allocation without a GC (but GC-aware)

"No GC for now, design with one in mind" means: get the **object
representation** right even though the **reclamation** strategy is deferred.
Concretely:

- Every heap value should carry a **type tag** (which variant/shape it is)
  so a future collector (or `match`) can find it without extra bookkeeping.
- Composite values (structs, enum payloads) should be **pointers to heap
  cells containing pointers/values**, not inlined-everywhere — this is what
  makes "closed by reference" and mutation-through-aliases work, and it's
  also the layout a mark-sweep or tracing GC needs (it needs to *find* every
  pointer field to trace it — keep pointer fields distinguishable from raw
  number/string data, e.g. by a per-type field-layout descriptor next to the
  tag, not commingled).
- Without a collector, the simplest non-embarrassing story is a **bump
  allocator that never frees** (fine for short-lived scripts/examples) —
  explicitly *not* a case-by-case manual free (that's a correctness hazard
  with reference-by-closure semantics: something else may still hold a
  pointer). Don't build `free`/`drop` into the language now if the plan is
  a GC later — that's the shape of bug (use-after-free, double-free) a GC
  exists to avoid, and retrofitting ownership rules afterward is a much
  bigger redesign than adding a collector to already-correct heap layout.
- Roots for a future GC = anything reachable from: the current chain of
  activation records (via SCP/DCP) and their local/param slots, plus any
  global bindings. Keeping frames explicit (not Mah's current implicit
  fixed-address globals) is what makes root-finding possible later.

## Tagged unions for Rust-like enums

An enum value is a heap cell (or, for a `Copy`-sized unit-only encoding, an
inline tag) holding: **which variant** (a small int/tag) + **that variant's
payload**, laid out per-variant (struct variants get named fields, unit
variants get none). Pattern matching on it is: check the tag, then bind the
payload's fields into the match arm's scope. This is the same representation
whether the enum is "Option-like" (unit + 1-payload variants) or has many
struct variants — don't special-case unit variants at the representation
level, just give them a zero-field payload.

## Compiling pattern matching

A `match` compiles to a **decision tree**, not a naive if/elif chain over
the raw scrutinee (though for a first cut a linear chain of
"check tag/value, jump past on mismatch, bind, jump to end" per arm is a
completely reasonable starting point and mirrors Mah's existing
backpatching-heavy `if`/`elif`/`else` compilation almost exactly). Per
pattern kind:

- **Literal** (number/string/bool) → equality check against the scrutinee.
- **Struct pattern** → no tag check, just field-by-field recursive match
  (a struct has one shape).
- **Enum pattern** → tag check first, then (if matched) recursively match
  each bound field of that variant's payload.
- **Binding** (`x`) / **wildcard** (`_`) → always matches; bind (or don't)
  and continue — this is also how the current LL(1) parser table would see
  a bare identifier at the start of a pattern, so binding-vs-literal-name
  disambiguation needs a grammar/semantic rule (e.g. Rust's own convention:
  a lowercase bare name in pattern position is always a fresh binding,
  never a value to compare against, unless explicitly qualified).
- Exhaustiveness/reachability checking is a nice-to-have, not required for
  a correct v2 — can be deferred or done as a simple "was there a wildcard
  or every variant covered" check without full pattern algebra.

## Blocks and `if`/`match` as expressions

Rust-style "a block's value is its last expression" means `CodeBlock` can no
longer compile to pure statements — it needs a **result slot**: reserve (or
infer) a destination address before compiling the block's statements, and
have whatever compiles the final expression-statement write into it instead
of discarding it. `if`/`match` as expressions are the same idea one level up
— every arm/branch writes into the same result slot before falling through
to a shared join point. This composes directly with Mah's existing
backpatching machinery (`docs/RUNTIME.md`); the new piece is *threading a
destination address into* block/arm compilation, not the jump patching
itself.

## AST vs. direct syntax-directed translation

Mah today has no AST at all (`docs/ARCHITECTURE.md`) — actions fire and
emit code *while parsing*. Forgiving error recovery, rename, and multi-pass
analysis (e.g. "resolve all enum/struct type names before compiling
function bodies that reference them, regardless of declaration order") are
all much easier with a real AST as an intermediate representation: parse
errors become "insert an error node and keep going" instead of aborting the
whole parse, and a second pass over the tree can do scope resolution,
type/shape checking, and codegen separately — each pass gets to assume the
previous one succeeded, instead of one pass trying to do everything at once
under one token of lookahead. This is the highest-leverage structural
change for everything else on the v2 list (pattern matching, struct/enum
declarations before use, LSP rename, better diagnostics) — it's worth
treating as its own milestone rather than something added incidentally
while implementing a specific feature.

## LSP rename

Needs, at minimum: an unambiguous mapping from "the symbol under the
cursor" to **every token that refers to the same declaration**, including
across files (Mah's module system renames on import — see
`docs/ARCHITECTURE.md`'s preprocessor section — so a rename in an exported
declaration must also account for how importers reference it, not just
occurrences in the declaring file). This is naturally the same scope-tree
problem `_build_scopes` in `lsp/analysis.py` already solves for
go-to-definition — extending it to "find all references" is the
incremental step; the harder part is making sure it agrees with whatever
the v2 compiler's *own* (presumably now AST-based) scope resolution does,
rather than being a third independent reimplementation of scoping (Mah
already has two that can drift — see `docs/ARCHITECTURE.md`'s "Tooling"
section).
