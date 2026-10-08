# Mah errors: typed, checked, inferred

Status: **M25 landed 2026-09-28: syntax + runtime, both VMs** (`throw`/
`try`/`catch`/`throws`, the `Error` trait, the built-in `RuntimeError`
enum, `Promise.Failed`, bytecode 1.4 — see `docs/MAHC_FORMAT.md`).
**M26 landed 2026-09-28: the static checker's error sets** — inference
(fixpoint over recursion, generic in callbacks' error sets), `throws`
clauses on functions and function types (inferred when left out), `try`
filtering, "unhandled error" / "never thrown here" diagnostics, Promise
error sets, hover. See "M26: what landed" at the end for the exact scope
and what's still open. This work lands before the standard library (see
[`STDLIB.md`](STDLIB.md)), whose I/O and parsing functions will report
failures by throwing the errors described here.

## Goals

1. **Errors are thrown, not returned.** No `Result` type; a failing call
   throws a value and the caller's code reads straight through.
2. **Errors are type safe.** The checker knows which error types every
   block and function can throw.
3. **Errors infect upward.** A block that can throw makes its enclosing
   block, function, and every caller throw the same types, with no marker
   at call sites, until some scope catches them.
4. **Catches are selective.** A `catch` handles only the error types (or
   variants) its arms name; everything else is re-thrown automatically.

## Error types

Any struct or enum that implements the built-in `Error` trait (declared in
the prelude, so it counts as a system trait for the orphan rule):

```mah
trait Error {
    fn message(self) -> String { ... }   # default: the type/variant name
}

enum FsError { NotFound { path: String }, PermissionDenied { path: String } }
impl Error for FsError {
    fn message(self) {
        match self {
            FsError.NotFound { path } => { "no such file: " + path }
            FsError.PermissionDenied { path } => { "permission denied: " + path }
        }
    }
}

struct InvalidAge { value: Number }
impl Error for InvalidAge {}
```

`throw` of a value whose type doesn't implement `Error` is a compile error
at every strictness level.

## Syntax

New keywords: `throw`, `try` (reserved). `catch`, `throws`, and `never`
stay **contextual** (ordinary `ID` tokens, matched by their literal text)
so they remain usable as identifiers everywhere else (`let catch = 1`
works).

### Throwing

```mah
throw InvalidAge { value: n }
```

`throw` is an expression of type `Never` (it doesn't produce a value), so
it fits anywhere: `let n = if ok { x } else { throw E {} }`.

### Catching

`try { ... } catch { arms }` is an expression. Its arms are `match` arms
(patterns plus optional guards), with one new pattern kind:

```mah
let text = try {
    fs.read_text(path).await
} catch {
    FsError.NotFound { path } => { "" }            # one variant
    e: ParseError => { log(e.message()); "?" }     # a whole type (type-test pattern)
    e: FsError if retries > 0 => { retry() }       # guards work as in match
    # anything not matched is re-thrown, unchanged
}
```

- **Type-test pattern** `name: Type` (and `_: Type`): matches when the
  error's runtime type is `Type`, binding it. New pattern kind, usable
  only in `catch` arms for now (a later `match`-on-type can reuse it).
- `_ =>` or `e =>` with no type catches everything.
- The value of the `try` expression is the body's value or the matching
  arm's value; they unify like `match` arms.
- An arm naming a type the body can't throw is a warning ("`X` is never
  thrown here").

### Shorthand

```mah
let n = try s.to_number() else 0
```

`try EXPR else FALLBACK` catches **every** error from `EXPR` (tracked and
runtime errors alike) and evaluates to `FALLBACK`. It's sugar for
`try { EXPR } catch { _ => { FALLBACK } }`.

### Declaring

Annotating a function's error set is optional, like any other annotation:

```mah
fn load(path: String) -> Config throws FsError | JsonError { ... }
fn pure(x: Number) -> Number throws never { ... }
```

The declared set must cover the inferred one (otherwise: "`load` can throw
`ParseError`, which isn't in its `throws` list"). Under `explicit`
strictness, every **exported** function whose inferred set is non-empty
must declare it.

## Inference

The checker computes an **error set** (a set of error types) for every
expression:

| Construct | Error set |
|---|---|
| `throw e` | `{typeof e}` |
| call `f(...)` | `f`'s error set, plus the arguments' sets |
| method call | the resolved method's set |
| `p.await` | the error set carried by `p: Promise<T, E>`, i.e. `E` |
| block, `if`, `match`, loop | union of their parts |
| `try B catch { arms }` | `B`'s set minus what the arms fully handle, plus the arms' own sets |
| `try E else F` | `F`'s set only |

"Fully handles" means an unguarded arm covering the whole type: `e: T`,
`_`, or an exhaustive set of variant patterns for an enum `T`. A variant
pattern or a guarded arm handles part of a type, so the type stays in the
set.

A function's error set is its body's set. Recursive and mutually recursive
functions are solved as a fixpoint over the call graph (the checker
already orders items, see `TYPES.md` "Items and order").

**Error sets are not general union types.** `TYPES.md` keeps union types
as a non-goal; error sets are a separate, flat set attached to function
types and Promises, and only ever appear after `throws`.

### Function types and callbacks

Function types carry an error set: `fn(T) -> U throws E`. Calling a value
of that type throws `E`. Generic code is polymorphic in it:

```mah
fn apply<T, U, E>(f: fn(T) -> U throws E, x: T) -> U throws E { f(x) }
```

Inferred callers never write `E`; it's inferred like any type parameter.
The prelude's lazy adapters carry it as a type parameter (`Mapped<T, U,
E>`, `Filtered<T, E>`), so a throwing `map` callback's errors surface at
whatever pulls items (`reduce`, a `for` loop), which is where they really
happen at runtime.

When the checker can't tell what a call throws (a callee of type
`Unknown`), the set contains `Unknown`: fine in `loose`, "can't infer what
this throws; annotate it" in `explicit`.

## Runtime errors (catchable, not tracked)

Errors the VM itself raises are values of one built-in enum (M25 landed;
see `docs/MAHC_FORMAT.md` #4.1/#4.5 for the exact classification of every
VM error site into one of these variants):

```mah
enum RuntimeError {
    DivisionByZero { message },
    TypeMismatch { message },
    NoSuchField { message },
    NoSuchMethod { message },
    ArgumentError { message },
    IndexOutOfRange { message },
    MatchFailed { message },
    InputError { message },
    Internal { message },
}
impl Error for RuntimeError {
    fn message(self) { self.message }
}
```

Every variant carries just `message` (the exact text the pre-M25 runtime
error would have printed) — no structured fields like an index or a
type-name pair; a future minor version could add those without changing
this design.

They're **catchable** like any error (`RuntimeError.DivisionByZero =>`,
`e: RuntimeError =>`, `_ =>`), but **not tracked**: they never enter an
inferred error set and never trigger "unhandled error" diagnostics.
Almost every function does arithmetic or indexing, so tracking them would
make every set non-empty and the sets would stop meaning anything (Java's
checked/unchecked split, for the same reason). A catch arm naming
`RuntimeError` is never warned about as unreachable.

## Uncaught errors

At the top of the program (a module's top-level code, or `main`):

- **Checker (M26, landed)**: a non-empty error set reaching the top
  level is an `Unhandled error: FsError, ParseError` diagnostic at the
  statement/call/`try` it comes out of: a **warning** in `loose`, an
  **error** in `strict` and `explicit`.
- **Runtime (M25, landed)**: the program stops and reports `Uncaught T:
  <message()>` (or a `RuntimeError`'s own `message` field, unwrapped, for
  a VM-raised error), located at the `throw` site exactly like an
  ordinary runtime error already is (`at position #LINE:COL`, or
  `FILE#LINE:COL` outside the entry file; nothing in a release build) —
  see `docs/MAHC_FORMAT.md` #6.8 for the exact algorithm, including a
  detached task's uncaught error failing its Promise instead of stopping
  the program. **No Mah stack trace** (out of scope for M25; only the
  final throw site's own location is reported).

So a quick script still runs in `loose` mode, and fails cleanly on bad
input:

```mah
let x = input("decimal: ").to_number()   # warning: unhandled NumberParseError, EndOfInput
```

## Interactions

- **`defer`**: an error unwinding through a scope runs that scope's
  deferred blocks, in the usual order, before continuing up (M25 landed:
  `docs/MAHC_FORMAT.md` #5.4/#5.5's `deferdepth`/`deferabove`-based
  drain). An error thrown *inside* a deferred block while unwinding
  **replaces** the one in flight, and draining continues with the
  remaining deferred blocks — M25 does not keep the replaced one as a
  `cause` anywhere (no stack traces at all yet, see above); that's a
  possible M26+ refinement, not decided.
- **`detach` / `.await`**: a detached task that throws settles its
  Promise as `Failed { error }` instead of stopping the program (M25
  landed: `Promise` gained this third variant at the *value* level);
  `.await` re-throws `error` in the awaiting task. At the type level
  (M26) a Promise carries the detached expression's error set, and
  `.await` throws it; there's no written `Promise<T, E>` syntax yet (a
  written `Promise<T>` has an inferred error set). A detached Promise that's never awaited and failed is
  reported as uncaught when the program finishes (the first one, in fail
  order, if there are several).
- **Timers** (`std:async`'s `set_timeout`/`set_interval`): the callback's
  type is `fn() -> Unknown throws never`. Nothing could catch an error
  thrown from a timer, so the checker requires the callback to handle its
  own errors. At runtime, one escaping anyway (a `RuntimeError`, or
  `loose` mode) is treated as uncaught.
- **`break`/`continue`/`return` inside `try`**: allowed; they leave the
  `try` normally (running `defer`s) and aren't errors.
- **Natives**: `extern fn` declarations in std modules state what they
  throw (`extern fn read_text(path: String) -> Promise<String, FsError>
  = "fs.read_text"`); a native throws by returning an error value to the
  VM, which unwinds exactly as for `throw`.

## Threads (M44, landed)

`std:thread`, `detach(t)`, `shared let`/`lock` and channels
(docs/contracts/M44_threads.md, docs/STDLIB.md) add one error type,
`ThreadError`, declared in the **prelude** (so it needs no import, and its
name is global like `EndOfInput`'s):

```mah
struct ThreadError { kind: String, message: String }
impl Error for ThreadError {
    fn message(self) { self.message }
}
```

| kind | thrown by |
|---|---|
| `closed` | queueing a job on a closed Thread (`thread 'NAME' is closed`); `send` on a closed channel, `recv` on a closed empty one, a waiting send/recv when the channel closes (`the channel is closed`) |
| `full` | queueing beyond `workers + capacity` jobs (`thread 'NAME' is full (N jobs queued or running)`) |
| `cancelled` | a queued job dropped by `close(cancel: true)` (`thread 'NAME' was closed before this job started`) |
| `deadlock` | a `lock` wait (or the lock an assignment takes) that would close a cycle of waits (`deadlock: waiting for 'VAR' would never end`); an `.await` or `t.join()` that would wait, through locks, jobs or joins, for itself (`deadlock: this await would never end (it waits, through locks or threads, for itself)`); `t.join()` from one of `t`'s own jobs (`deadlock: a thread can't join itself`) |
| `stuck` | a lock, semaphore, channel, join or job-reply wait while every thread of the run is waiting (`the wait can never finish: every thread is waiting`); a job whose root waits on a Promise nothing will settle (`the job never finished: it waits on a Promise nothing will settle`) |
| `not_sendable` | a Promise inside `t.run`'s arguments, a job's result or error, or a channel message (`a Promise can't be sent to another thread`); a shared variable written with a Promise inside (`shared variable 'VAR' can't hold a Promise`) |
| `foreign_promise` | `.await`, in a job, of a Promise that was still pending when it was copied into the job |
| `over_release` | `Semaphore.release` with every permit free |

- **Tracked where declared**: `std:thread`'s functions that can throw it
  declare `throws ThreadError` (like `std:socket`'s `SocketError`), so in a
  `strict` project a top-level call of one needs a `try`. The `ThreadError`s
  the VM raises by itself — a `lock` that would deadlock, a write-back
  refused with `not_sendable`, a failed job Promise's `.await` — are
  **untracked**, like `RuntimeError`.
- **Jobs**: a job's error comes back through its Promise as a **copy** (same
  type, fields, throw site), so `try { p.await } catch { e: MyError => ... }`
  works across threads. If the error value itself contains a Promise, the
  job fails with `not_sendable` instead. An uncaught job error reported by
  the main thread is located at its original `throw`.
- **Unobserved failed job Promises** are reported when the program ends,
  exactly like an unobserved failed detached task. With **several**, which
  one is reported can differ from run to run (it is the order the replies
  arrived).
- **`lock` and throws**: a `lock` block left by a throw still writes its
  changes back and releases. If that write-back is refused (the value holds a
  Promise), the throw keeps unwinding unchanged — a failed write-back never
  replaces the error in flight (unlike an error thrown inside a `defer`,
  above); leaving the block normally instead throws `not_sendable` at its
  end.
- **Compile errors** (resolver, exact texts in the contract §5.3): a method
  call on a shared variable outside `lock` on it (`Method call on shared
  variable 'xs' outside 'lock xs { }': it would act on a copy; write 'lock xs
  { ... }'`), an assignment into one (`Assignment into shared variable ...`),
  `x = ... x ...` outside `lock x` (`'x = ...' reads shared variable 'x'
  outside 'lock x { }': ...`), `'lock' takes shared variables, and 'T' is not
  one`, `'x' is locked twice in one 'lock'`, `'shared let' is only allowed at
  the top level of a file`, `'x' is a shared variable and can't be declared
  again in the same scope`. Two `detach(t)` parse errors: `detach(t) needs an
  expression; write 'detach(t) { ... }'` (a statement after `detach(t)` on
  the same line) and `detach(t) and its operand must be on the same line;
  write 'detach(t) {' on one line` (`detach (name)` then `{` on the next
  line). The checker reports `detach(...) needs a thread.Thread, got T` when
  it knows the type, and three warnings: `detach (...)` that isn't the
  thread form although its parenthesized expression is a Thread, a shared
  variable passed as a copy to a call, and assigning into the loop variable
  of `for let item in shared_vec`.

**M44 compatibility notes.** Three things that compiled before mean
something else or stop compiling:

- `detach (x) expr` with another expression **on the same line** after the
  `)` is now the thread form (it used to be two statements, `detach (x)`
  then `expr`).
- The two new `detach(t)` compile errors above: a statement on the same line
  after `detach (x)`, and `detach (name)` followed by a block on the next
  line.
- `ThreadError` is a new prelude name: a program that declares its own
  `ThreadError` type in a file that includes the prelude now gets the
  existing "built-in name" error. Rename it.

## Runtime and bytecode (M25, landed)

Both runtimes (`mah/code_interpreter.py` and `runtime/src/vm/`) implement
the same behavior, checked identically by the normal test suite (every
`tests/test_*.py` program runs on both VMs — `make test-rust` reruns the
whole thing with `MAH_TEST_VM=rust`).

- A `throw` opcode (throws the value it names; substitutes a
  `RuntimeError.TypeMismatch` if the value doesn't `impl Error`).
- A single, program-wide **HANDLERS section**: `(start_pc, end_pc,
  handler_pc, slot)` per `try`/implicit-`defer`-guard region, in
  innermost-first order (`docs/MAHC_FORMAT.md` #4.8). Unwinding scans it
  for the first entry covering the throwing pc; not found in the current
  frame pops to the caller's frame (`pc = call_site - 1`) and repeats
  until a handler is found or the task's return stack is empty.
- A handler entry's target runs the compiled `catch` arms (or `DRAIN`,
  for an implicit `defer`-guard) as a `match` on the thrown value in the
  named frame slot; the "no arm matched" fallthrough re-throws
  automatically.
- Every existing VM runtime-error site throws a `RuntimeError` value
  (classified by kind, `docs/MAHC_FORMAT.md` #4.5) instead of aborting.
- Uncaught at a task root: settle the task's Promise as `Failed` (a
  detached task) or stop the program with the uncaught-error report
  (the main task) — `docs/MAHC_FORMAT.md` #4.6/#6.8 has the exact rules,
  including a synchronous sub-task (`to_string`) re-throwing in its
  caller.
- **No stack traces** (explicitly out of scope for M25 — only the throw
  site's own location is in the uncaught report).
- `.mahc` format version bump to 1.4: the `throw`/`matchtype`/
  `deferdepth`/`deferabove` opcodes, the HANDLERS section, the built-in
  `RuntimeError` enum, `Promise.Failed` (`docs/MAHC_FORMAT.md`).

The checker's (M26) error sets are erased before codegen, like all
types; the runtime needs only the handler table and type tests
(`tests/test_typecheck.py`'s codegen-unchanged test covers it).

## Implementation plan

1. **Syntax. ✅ Landed (M25).** Lexer keywords, parser for `throw`,
   `try/catch`, `try/else`, `throws` annotations, type-test patterns.
   AST, resolver, formatter, LSP (via the `mah-add-feature` skill).
   Tree-sitter/TextMate grammars followed with M26 (`throw_expr`,
   `try_expr`, `catch_arm`, `type_test_pattern`, `throws_clause`).
2. **Runtime. ✅ Landed (M25).** `Error` trait and `RuntimeError` in the
   prelude, `throw` opcode, the HANDLERS table, unwinding with `defer`,
   runtime-error sites converted, uncaught-error reporting (no stack
   traces yet), Promise error state. Both VMs.
3. **Checker. ✅ Landed (M26)**, except as listed below. Error sets,
   fixpoint inference, function types with `throws`, Promise error sets,
   unhandled/unreachable diagnostics at each strictness level, hover
   showing a function's `throws`.
4. **Docs and templates. ✅ Landed (M25, website with M26).**
   `mah/project/templates/` (language reference, AGENTS.md),
   `examples/errors.mh`, the website's `/docs/errors` page and the
   v0.2.0 changelog post.

Tests for each step per `docs/TESTING.md`: parser/formatter round-trips,
runtime unwinding (nested `try`, re-throw, `defer` order, errors across
`await`, uncaught in detached tasks), checker inference and diagnostics
per strictness level.

## M26: what landed

`mah/compiler/types.py` (`ESet`, `solve`) and `mah/compiler/typecheck.py`
(`_raise`, `_check_throw`, `_check_try`, `_generalize_esets`,
`_report_errors`); tests in `tests/test_typecheck.py`'s
`M26ErrorSetTests`.

- **Representation.** Error sets are nodes in one program-wide graph
  (`ESet`): a node's value is its own error names plus every node it
  links to, minus that link's exclusions (a `try`'s fully-handled types,
  or everything for a catch-all). Values are the least fixpoint
  (`types.solve`), computed once at the end of checking, so recursion and
  mutual recursion need no special handling. `TFn.throws` is a function
  type's node; `TCon.throws` a Promise's.
- **Sources.** `throw e` (e's type name; `RuntimeError` is never added),
  every call and method call (the callee type's node), `.await` (the
  Promise's node), and a `defer`red block (its closure's node) raise into
  the innermost accumulator: the function body's, or a `try` body's /
  `detach` expression's own. A catch-all arm's binding re-thrown with
  `throw e` re-throws the `try` body's set. A function value passed where
  the checker can't follow it (an `Unknown` parameter, an unchecked
  prelude method such as `map`) counts as throwing its errors at that call.
- **`throws` clauses.** Written on a function (`fn f() throws A | B`), a
  method, or a function type (`fn(T) -> U throws E`): the node is
  *sealed* — callers see exactly the written set, and what flows in (the
  body, or a closure assigned/passed there) is checked against it. With
  no clause the set is inferred. A clause naming a type parameter
  (`throws E` in `fn apply<T, U, E>`) is inferred too. Every listed name
  must implement `Error`.
- **Generics.** Like type variables, the error-set variables in a
  function's signature (a callback parameter's) are quantified at
  generalization and copied per instance, so `apply(good)` doesn't throw
  what `apply(bad)` does. The function's own set is flattened in place at
  that point (down to names plus links to outer sets and those variables).
- **Diagnostics.** `Unhandled error: A, B` (kind `unhandled`: warning in
  `loose`, error otherwise) at each top-level site; `'f' can throw X,
  which isn't in its throws list` / `This function can throw X, but its
  expected type only allows throws Y` (same kind); `X is never thrown
  here` for a catch arm (kind `warning`: a warning at every level, never
  fails a build); `X doesn't implement Error, so it can't be thrown`
  (mismatch); `Can't infer what this throws; annotate it` at a call to an
  `Unknown` callee (explicit level only, unless a catch-all encloses it).
  `typecheck.is_warning` decides the severity for the CLI, driver and LSP.
- **Hover** shows `throws A | B` after a function's signature.

Still open (not in M26):

- The `explicit`-level rule that every **exported** function with a
  non-empty set must declare it.
- The prelude adapters' own error parameters (`Mapped<T, U, E>`): the
  prelude is still unchecked, so a throwing `map` callback is charged to
  the `map` call rather than to whatever pulls items.
- User `Index`/`Iterable`/`Printable` impls that throw (indexing, `for`,
  `print`'s `to_string`) aren't tracked, and neither are errors thrown
  from a struct field's closure stored before the field's type is known.
- Written `Promise<T, E>` syntax, and timers (`std:async` doesn't exist yet).
- Hover doesn't name error-set variables (`fn apply(f: fn(T) -> U, x: T)
  -> U`, not `... throws E`).
