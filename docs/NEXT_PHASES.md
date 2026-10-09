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

Landed as M20 -- see docs/V2_DESIGN.md's M20 entry. As sketched here, a
guard is one more failable check after the pattern's own, jumping to the
next arm when it's falsy.

## Arrays / lists

Landed as M19's `Vector` (and `Map`) -- see `docs/V2_DESIGN.md`'s M19 entry
and `docs/MAHC_FORMAT.md` §6.9. What's still open from this sketch is
pattern matching over them (the last bullet). The original sketch:

- A new heap object kind, `ArrayInstance` (wraps a Python list of element
  values — immediates or heap pointers, same as struct/enum fields).
  Nothing about the existing `Frame`/`Closure`/`StructInstance`/
  `EnumInstance` design assumes a closed set of heap kinds — adding one
  more is additive.
- New syntax needed: literal (`[1, 2, 3]`), indexing (`arr[0]`, both read
  and as an assignment target — a new `Index` AST node alongside
  `FieldAccess`), and almost certainly a `for` loop (`for x in arr { ... }`)
  since iterating v1's `while` + manual index is painful for real list use.
  The `for` loop landed in M18 (`for let x in arr { ... }`), going through
  the `Iterable`/`Iterator` system traits (see `docs/TRAITS.md`), so
  arrays only need impls of those traits.
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

**Superseded (2026-09-25) by [`TYPES.md`](TYPES.md)**, a static design:
optional annotations, inference, generics, and three strictness levels.
The runtime-types-as-values idea below is kept for a later narrowing/
`match`-on-type phase.

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

## `detach` on any expression

Landed as M20 -- see docs/V2_DESIGN.md's M20 entry. The open question was
settled as "stays a `Promise`": `detach { ... }` gives a Promise you
`.await`, with no implicit awaiting when a Promise is used.

## Cross-file rename

Landed as M11 -- see docs/V2_DESIGN.md's M11 milestone for what shipped
(a workspace-wide reverse-import-graph search, built on the preprocessor's
own tolerant scanner, feeding a real `Parser`/`Resolver` pass per relevant
file).

## Struct/enum/field rename

Landed as M11, see docs/V2_DESIGN.md -- struct/enum type-name, enum
variant-name, and struct/enum field-name rename in declarations/literals/
explicit patterns. Type-name rename was single-file until M41s made types
exportable; it is now cross-file the way function rename is (see
docs/V2_DESIGN.md's M41s milestone), while variant and field rename stay
single-file. Field-*access* rename (`p.x`) remains
deliberately refused, unsound without a real type system -- still waits
on `docs/NEXT_PHASES.md`'s own "The type system" section above.

## Errors and the standard library

Designed 2026-09-28: typed, checked, inferred errors (`throw` /
`try ... catch`) in [`ERRORS.md`](ERRORS.md), then the `std:` standard
library (json, csv, fs, process, random, math, path, time, async, regex,
collections, socket, http) in [`STDLIB.md`](STDLIB.md). **M25 landed the
errors design's syntax + runtime** (both VMs), and **M26 the static
checker's error sets** (inference, `throws` checking, unhandled-error
diagnostics; `ERRORS.md`'s "M26: what landed" lists what's still open).
**M27 started the standard library**: `std:` imports, `extern fn`,
native table versioning, and `std:math`; **M28 added `std:test` and
`mah test`** ([`MAH_TEST.md`](MAH_TEST.md)), and **M29 the String
methods**, **M30 `std:path`, `std:json` and `std:csv`**, and **M31
`std:random` (on a PRNG shared by both VMs) and `std:collections`**, and
**M32 `std:regex`**, which completes Phase 1, and **M33 an async `input`**
(with I/O in the scheduler), and **M34 `std:time`/`std:async`** (with
cancellable timers), and **M35 `std:fs`** (with handles), and **M36
`std:process`** (arguments, environment, running programs, `exit`), and
**M41a type values, `##` docs, spread calls, `std:reflect` and
`json.decode`**, and **M41b decorators as metadata**, and **M41c hooks,
function-item impls and rest parameters** ([`REFLECTION.md`](REFLECTION.md)),
and **M37 the `Bytes` type** (with `std:bytes` and binary `std:fs`), and
**M38 `std:socket`** (TCP), and **M39 TLS, `std:url` and the `std:http`
client**. The roadmap after it, in order (recorded at the user's request),
is the next four sections.
`std:test` and the `mah test` runner ([`MAH_TEST.md`](MAH_TEST.md)) come
right after errors and `std:` resolution.

## The HTTP server

✅ Landed as M42 -- see docs/V2_DESIGN.md's M42 entry and
docs/contracts/M42_http_server.md. Deferred: HTTP/2, `Upgrade`/WebSockets/1xx
replies, streaming request bodies (they are read whole), response
compression, Range requests, static files, cookie helpers, per-connection
request caps, write timeouts, client certificates, SNI/several certificates,
ALPN and certificate reloading.

## A NestJS/Hono-style web framework

It will live in a separate repository, built on `http.serve`'s
`fn(Request) -> Reply` handlers. Built on the HTTP server and M41b/M41c's decorators and hooks: controllers
and routes declared with decorators, **parameter decorators** binding path,
query, header and body values to typed parameters, **validation** from the
declared types and validator decorators, **pipes** and **transforms** that
convert and check values before the handler runs (and interceptors/guards
around it), middleware in a Hono-like style, and OpenAPI generated from the
same metadata through `std:reflect`. Likely gaps to close first: decorators
on parameters reaching hooks with enough information, and async hooks.

## Packages from GitHub repositories

Landed as M43 -- see docs/V2_DESIGN.md's M43 entry and docs/PACKAGES.md.
Deferred: `mah add`/`mah remove`, hosts other than GitHub and `git = "url"`
sources, semver ranges and version solving, a download cache shared between
projects, package registries, `mah init --lib`, several versions of one name
in a project, packages that bind natives, locking concurrent `mah install`
runs against each other, and verifying commit signatures.

`[dependencies]` in `mah-project.toml` (reserved since projects landed)
becomes real: each entry names a GitHub repository, with an optional
branch, tag or commit hash to install, and an optional subdirectory of the
repository to use as the installed library's root. `mah install` fetches
them, and a **lock file** records the exact commit (and a content hash) of
each, so installs are reproducible; imports then resolve a package name to
its installed root.

## Optional multithreading for `detach`ed expressions

✅ Landed as M44 (M44a threads and shared variables, M44b channels, bytecode
1.21) and M45 (`atomic { }` transactions replacing M44's `lock`, in the same
unreleased 1.21) -- see docs/V2_DESIGN.md's M44a/M44b/M45 entries, the
contracts docs/contracts/M44_threads.md and docs/contracts/M45_atomic.md, and
the discussion paper docs/contracts/M44_threads_options.md. A `std:thread` Thread is a queue of
jobs; `detach(t) expr` and `t.run(f, ...args)` run work on it, in an isolated
VM, on a copy of every global and of what it captures, taken when the job is
queued. `shared let` variables live outside every VM and change in place only
inside `atomic { }` transactions (which rerun on conflict, wait with `retry`,
and go exclusive after 8 failed attempts); `Semaphore` and `Channel` are
process-wide.

Deferred (follow-ups):

- First-class shared cells, `thread.ref(value)`: a shared value made at run
  time and passed around (to functions, into jobs, inside structs), read and
  changed only inside `atomic { }` like a `shared let`. Today shared state is
  top-level `shared let` only, because a top-level name is the one identity
  every thread (each running its own copy of the program) agrees on and the
  compiler can see every use; a ref's identity would travel with the value
  instead, so copying a ref into a job must keep it pointing at the same
  cell.
- Copying only the globals a job uses (free-variable slicing), instead of the
  whole main frame per job.
- Acquire timeouts; `select` over several channels; `WaitGroup`.
- From M45 (docs/contracts/M45_atomic.md §16): `or_else` (it needs nested
  rollback of the write set) and nested rollback of an inner `atomic` left
  by a throw; `retry` inside implicit runtime calls (`to_string`,
  `Error.message`); making `ch.len()`/`s.available()` transactional (or
  waking `retry` on them) and transactional channels/semaphores; a
  statistics/introspection API (attempt counts, exclusive runs) and
  Rust-side debug counters like Python's `TX_STATS`; contention management
  smarter than "8 then exclusive" (backoff, priorities); detecting
  outer-object mutation inside `atomic` (`outer.push(x)`) and outer
  assignments from called functions/closures; detecting a busy-wait loop
  inside `atomic`; highlighting `retry` outside statement position in the
  tree-sitter and VS Code grammars; cheaper transaction reads (copy-on-write
  working copies).
- Deadlock detection through semaphores, channels and not-yet-started jobs,
  and through cycles of plain same-VM awaits (today those are left to the
  quiescence rule, which fires only once every thread is waiting), and
  reporting *which* wait is hopeless while some other thread is still busy.
- Interrupting a running job; thread-local storage; priorities.
- Transferring a socket to a thread (handles are already shared).
- A free-threaded CPython or subinterpreter backend for the Python VM, so jobs
  run in parallel there too; a `MAH_THREADS=inline` debugging mode.
- Giving back the semaphore permits of tasks a job abandons (permits are
  owner-less; the docs say to release with `defer`).
- Reporting several unobserved job failures in a deterministic order (today:
  the order the replies arrived).
- Cheaper shared reads (copy-on-write or immutable sharing of shared values);
  every read outside `atomic` copies the whole value.
- A runtime test for a job Promise settled by hand (no exported std function
  can settle another Promise today, so that path is reachable only from std
  code).
