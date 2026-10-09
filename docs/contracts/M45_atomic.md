# M45 contract: `atomic { }` (software transactional memory) replaces `lock` — bytecode 1.21, redefined

Written by the thinker (`docs/DEVELOPMENT_WORKFLOW.md`) from the user's binding decision: **remove the
`lock` block entirely and add `atomic { ... }`**, modeled on Haskell's STM and Clojure's `dosync`,
adapted to Mah. It is the only spec the coders work from. Nothing here is open: where a choice was
made by judgment it says so, and §15 lists every such choice for the user.

M44 (`docs/contracts/M44_threads.md`) stays the reference for everything this file does not change:
threads, jobs, `detach(t)`, copy semantics (§4 there), `shared let` storage and declaration rules,
semaphores, channels, the shared stdout/stdin/handle tables, `process.exit`, quiescence (§6.10 there).
**The M44 state (with `lock`) is preserved on the git branch `m44-lock-block`; nobody touches that
branch.** Part 3 adds a note to the top of the M44 contract (§11.6).

The work is split in three parts, like M44 (§13 assigns every file to exactly one):

- **Part 1 "core"** (first): the compiler (lexer helper, parser, AST, resolver, codegen, checker,
  preprocessor, format tables), `mah/std/thread.mh`'s header comment, the prelude's `ThreadError`
  comment, the whole Python VM, Python-side tests, `mah/std/thread.test.mh`, `examples/threads.mh`.
- **Part 2 "rust"** (after Part 1, in parallel with Part 3): the Rust VM to parity, `vm_diff` cases,
  `make test-rust` green.
- **Part 3 "tooling+docs"** (after Part 1, in parallel with Part 2): tree-sitter grammar and
  queries, VS Code grammar, LSP, formatter tests, the www highlighter, every doc and www page.

Every coder reads §1–§7 (what the language does and how the runtime does it), §14 (Mah gotchas) and
their own part's sections. Python/Rust parity is mandatory: every observable behaviour (stdout,
stderr, exit codes, error kinds and messages) is defined once here and both VMs implement it.

---

## 1. Decisions (summary)

1. **`lock` is gone.** No `lock` syntax remains; `lock` is an ordinary identifier everywhere again.
   The opcodes `sharedlock`/`sharedunlock` (0x72/0x73), lock deadlock detection, the lock wait-for
   edges, the lock error messages and the M44 rules E4/E6 about lock targets are removed.
2. **`atomic { body }` is an expression** whose value is the body's value. It runs the body as one
   **transaction**: reads of shared variables come from a consistent snapshot; writes and in-place
   changes go to private working copies; at the end the runtime atomically checks that nothing the
   transaction read has changed since and publishes every change at once (each published variable
   gets a new version). Otherwise it discards the working copies and **runs the body again from
   the start**. Algorithm: TL2-style (a global version clock, per-variable versions, read-time
   snapshot checks, commit-time validation), all under the existing one runtime mutex (§6).
3. **Progress guarantee:** after **8** failed attempts of one transaction, the 9th attempt runs
   **exclusive** ("irrevocable"): it first takes the runtime's single exclusivity token, and while
   it holds it every other commit and every plain assignment to a shared variable waits. An
   exclusive attempt can't fail validation, so it commits on that attempt. Only one transaction is
   exclusive at a time and it never waits for anything while exclusive, so this can't deadlock
   (§6.9).
4. **`retry`** (only lexically inside `atomic { }`) abandons the attempt and suspends the task — a
   pending Promise, other tasks keep running — until a shared variable the attempt read changes;
   then the transaction runs again. A `retry` that can never be woken becomes `ThreadError stuck`
   through M44's quiescence rule; a `retry` whose attempt read no shared variable fails at once
   with `stuck`.
5. **Nesting is flat**: an `atomic` inside an `atomic` (lexically or through calls, including a
   `to_string` the runtime calls) joins the outer transaction. Only the outermost one commits,
   validates, restarts or publishes. **`or_else` is deferred** (it needs nested rollback; §16).
6. **No side effects in a transaction** (the body may run more than once):
   compile errors for lexically visible `.await`, `sleep_async`, `print`, `input`, `detach`, for
   `retry` outside `atomic`, for `return`/`break`/`continue` leaving an `atomic`, and for assigning a
   non-shared variable declared outside the `atomic`; at run time, `.await`, `detach` and 53 named
   side-effecting natives throw the new `ThreadError` kind **`in_atomic`** while the task is in a
   transaction (§5.2).
7. **A throw that leaves the outermost `atomic` discards the attempt** (nothing is published) and
   the error propagates unchanged (Haskell/Clojure semantics). A throw caught inside the body is
   ordinary control flow; the transaction goes on.
8. **Outside `atomic`**: a read of a shared variable never waits and gives a copy; `x = e` is an
   atomic single-variable write (a one-variable transaction that can't conflict — it waits only
   while an exclusive transaction runs). In-place mutation and read-modify-write outside `atomic`
   are compile errors telling the user to wrap the code in `atomic { }` (E1–E3).
9. **Deadlock detection** keeps only the job/join await cycles of M44 (§6.4 there, minus lock
   edges) and the self-join check; quiescence (`stuck`) is unchanged and now also covers `retry`
   waits. Semaphores and channels are unchanged.
10. **Bytecode 1.21 is redefined in place** (it was never released: no git tag exists): `sharedget`
    (0x70) and `sharedset` (0x71) keep their encodings with new semantics; 0x72/0x73 become
    unassigned; four operand-less opcodes are added: `atomicbegin` 0x74, `atomicend` 0x75,
    `atomicabort` 0x76, `retry` 0x77. MINOR stays **21** (§7).
11. **`atomic` and `retry` are contextual** (no new lexer keywords; §2). No existing program changes
    meaning: `atomic` is the keyword only where `atomic {` couldn't have been a valid struct literal
    or condition head, `retry` only inside an `atomic` body.

---

## 2. Syntax and parsing

### 2.1 Grammar

```
atomic_expr := "atomic" block          -- contextual, rule in §2.2
retry_expr  := "retry"                 -- contextual, only inside an atomic body, rule in §2.3
```

`atomic { }` is block-shaped (as a statement it needs no `;`), may be followed by a postfix chain
like `if`/`match` (`atomic { xs }.len()`), and is an ordinary expression everywhere a struct literal
is allowed (`let v = atomic { ... }`, `print(atomic { n })`, `f(atomic { a + b })`). `retry` is an
expression of type `Never` (like `throw`), allowed as a statement or anywhere an expression may
appear inside the atomic body (`if q.len() == 0 { retry }`, `none => retry,`).

### 2.2 The exact `atomic` rule

`_parse_primary`, ID branch, before any other handling of an `ID`: the current token `tok` is the
keyword `atomic` iff **all** hold:

1. `tok.literal == "atomic"`;
2. `self._struct_literal_allowed` is true (so never in an `if`/`elif`/`while`/`for`/`match` head —
   there `atomic {` keeps meaning "the variable `atomic`, then the block");
3. the next token `t1` is `BRACE_OPEN` and starts on the same line as `tok` ends
   (`self._next_on_same_line(tok, t1)`);
4. the token `t2` after `t1` is **not** `BRACE_CLOSE` (an empty `atomic {}` / `atomic { }` stays a
   struct literal of a struct named `atomic` — an empty atomic block would be useless anyway);
5. `t2` and the token `t3` after it are **not** `ID` followed by `COLON` (`atomic { x: 1 }` stays a
   struct literal).

Two-token lookahead: add `Lexer.peek_tokens(self, n: int) -> list[Token]` to
`mah/compiler/lexer.py`, implemented exactly like `peek_token` (save `self.position`, call
`get_next_token` `n` times, restore the position, return the list). The parser calls
`self.lexer.peek_tokens(3)` (tokens after the current one).

Then `_parse_atomic(tok)`: `self.advance()` (consume `atomic`); save `self._atomic_depth`, set it to
the saved value + 1, `body = self.parse_block()`, restore it in a `finally`; build

```python
closure = FnExpr(name=None, params=[], body=body, position=tok.position, name_position=None,
                 param_positions=[], atomic=True)
return self._parse_postfix_from(AtomicExpr(closure=closure, position=tok.position))
```

`Parser.__init__` gains `self._atomic_depth = 0`. **Every function-body parse path** saves
`self._atomic_depth`, sets it to 0 while parsing the body block and restores it in a `finally` (a
`fn` written inside an atomic body is not inside it): `_parse_fn_expr` (lambdas and `fn`
declarations), `_parse_method_decl` (impl and trait methods, including default bodies),
`_parse_test_decl`, and any other parser function that parses a function's body block (the coder
greps `parse_block()` call sites in parser.py and checks each: a body that becomes a `FnExpr`,
`MethodDecl` or `TestDecl` resets; a statement block does not). Nothing else resets it (a `defer`
body, a nested `atomic`, `if`/`for` blocks and `try` blocks inside the body stay inside).

`_parse_block_items`: add `AtomicExpr` and `RetryExpr` to the tuple of node types that need no `;`
when more code follows on a later line (next to `DetachExpr`).

### 2.3 The exact `retry` rule

`_parse_primary`, ID branch, right after the `atomic` check: the current token `tok` is the keyword
`retry` iff `tok.literal == "retry"`, `self._atomic_depth > 0`, and the next token `nxt`
(`self.lexer.peek_token()`) either has type `BRACE_CLOSE`, `SEMICOLON`, `COMMA`, `PAREN_CLOSE`,
`BRACKET_CLOSE` or `EOF`, or starts on a later line (`not self._next_on_same_line(tok, nxt)`). Then
`self.advance()` and return `RetryExpr(position=tok.position)` (no postfix chain). Otherwise `retry`
is an ordinary identifier (`retry + 1`, `retry = 2`, `retry.len()`). Note that inside an atomic body
`f(retry)`, `f(retry, x)`, `[retry]`, `S { a: retry }` and `let y = retry` ⏎ contain the keyword (a
`)`, `,`, `]`, `}` or a line break follows). So that this can never silently change what such code
means when a variable `retry` is in scope, the resolver rejects a `RetryExpr` while any binding named
`retry` is visible (**E6b**, §4), and the preprocessor does the same for module-level names it would
mangle (§8.1). Code that wants the variable writes it where the keyword can't be (`(retry)` is still
the keyword — `)` follows — so rename the variable).

Outside an atomic body `retry` is always an identifier; the resolver turns an **undefined** `retry`
into E6 (§4).

### 2.4 What parses how

| source | parses as |
|---|---|
| `atomic { n = n + 1 }` | `AtomicExpr` |
| `let v = atomic {` ⏎ `...` ⏎ `}` | `AtomicExpr` (the `{` is on the `atomic` line) |
| `atomic` ⏎ `{ x: 1 }` | `StructLit` of a struct named `atomic` (today's meaning: the ID branch builds a struct literal whenever `{` follows and struct literals are allowed, with no same-line check) |
| `atomic` ⏎ `{ 1 }` | a syntax error, exactly as today (`Invalid syntax '<NUMBER,'1'>'`; it is the struct-literal path) |
| `if atomic { f() }` | `IfStmt` whose condition is the variable `atomic` (today's meaning) |
| `if (atomic { ok }) { f() }` | `IfStmt` whose condition is an `AtomicExpr` |
| `atomic { x: 1 }`, `atomic { }`, `atomic {}` | `StructLit` of a struct named `atomic` (today's meaning) |
| `let atomic = 2`, `atomic + 1`, `fn f(atomic) { atomic }` | identifiers (today's meaning) |
| `atomic { if c { retry } }` | `RetryExpr` in the `if` block |
| `atomic { retry + 1 }`, `atomic { retry = 1 }` | the identifier `retry` |
| `let retry = 3` ⏎ `atomic { f(retry) }` | `RetryExpr` → E6b (a variable `retry` is visible) |
| `atomic { let f = fn() { retry } }` | the identifier `retry` (inside a nested `fn`) → E6 if undefined |
| `fn g() { let retry = 5; retry }` | the identifier `retry` (outside atomic: today's meaning) |
| `retry` at top level, no variable `retry` | the identifier → E6 |

Compatibility: no program that compiles today changes meaning or stops compiling. (A program that
used an *undefined* `retry` gets E6's message instead of "Undefined variable".)

---

## 3. Semantics (the language)

### 3.1 Transactions

- **Begin.** Evaluating `atomic { body }` when the task is not in a transaction starts one: an
  *attempt* with a snapshot time `rv` (the global clock's value), an empty read set and an empty
  set of working entries. The body then runs (it is a zero-parameter closure called at once; its
  locals are fresh in every attempt).
- **Reads inside a transaction** (any read of a shared variable while the task is in a
  transaction — lexically inside the `atomic`, or in any function it calls):
  - the first access of `x` in the attempt takes the store's current `(value, version)`; if
    `version > rv`, `x` changed after the attempt started: the attempt **conflicts** and restarts
    at once (so every attempt only ever sees a consistent snapshot, as of `rv`); else `x` enters
    the read set with that version and gets a working entry whose working value is a fresh local
    copy of the stored value;
  - a read **written lexically inside** the `atomic` (same function; a `defer` closure inside it
    counts; a nested `fn` or a nested function's body does not) returns the **working value
    itself**, so `xs.push(1)`, `xs[i] = v`, `let a = xs; a.push(1)` change the working copy;
  - **every other read** (a helper called from the body) returns a **local copy** of the working
    value: a helper sees the transaction's uncommitted changes but can never change them through a
    read (M44's T20 rule, unchanged).
- **Writes inside a transaction**: `x = e` (lexically inside, or in a called function) replaces the
  working value of `x` with a **fresh local copy** of the value of `e` (M44 §4.2 local mode:
  Promises are kept as the same object, they are refused only when published; Rust: the same local
  copy), creating `x`'s entry if `x` wasn't accessed yet (a *blind write*, which does not enter the
  read set). The copy is what keeps the working value private to the transaction: without it,
  `atomic { ys = xs; ys.push(1) }` would make `ys`'s working value the very object `xs`'s working
  value is (and change `xs` too), and `let v = [1]` ⏎ `atomic { xs = v; xs.push(3) }` would change
  the non-shared `v` — once per attempt, so what is published would depend on how many reruns
  happened. With the copy, a working value is only ever reachable through its own shared variable
  (or a local alias made by a lexical read inside this attempt, which the attempt's rerun discards),
  so an attempt can always be thrown away and run again from the start. This is exactly the
  assignment semantics outside a transaction (M44: `ys = xs` copies).
- **Commit.** When the outermost `atomic`'s body finishes normally, the runtime decides which
  entries to publish: every entry that was assigned, and every entry whose working value was handed
  out by a lexical read and now differs from the stored value it was copied from (§6.6 `same_copy`).
  If nothing is to be published (a read-only transaction) it simply ends (no validation needed: it
  is serialized at `rv`). Otherwise, atomically: if any variable in the read set has a version
  different from the one recorded, the attempt **conflicts** and restarts; else the global clock
  advances by one and every published variable gets the new value (a strict copy) and the new
  clock value as its version, and every task waiting in `retry` on one of them is woken.
- **Restart** (a conflict): the attempt's working entries and read set are discarded, the task's
  state is put back exactly as it was when the outermost `atomic` began (the frame, the call stack
  depth, the defer stack depth; nothing in between runs — no `defer` of the abandoned attempt runs),
  and the body runs again with a new `rv`. The count of failed attempts carries over (§3.3).
- **Value.** The `atomic` expression's value is the body's value (it is the task's own object; a
  working value returned out of the block stays a private object of the task after the commit).

### 3.2 Nesting

An `atomic` evaluated while the task is already in a transaction **joins** it: only a depth count
changes. The inner block's normal end does nothing else; a throw leaving the inner block only
decrements the depth and keeps unwinding — the inner block's changes are **not** undone separately
(flat nesting, like Clojure); if the outer body catches the error and finishes, those changes are
published with the rest. A conflict or `retry` anywhere restarts the **outermost** transaction.

Implicit runtime calls (`Printable.to_string` for `print`/`+`/string conversion, `Error.message`)
run as their caller's task (M44 §5.2): if the caller is in a transaction, an `atomic` inside the
`to_string` joins it and a read in it is a read of that transaction. If the caller is not in a
transaction, an `atomic` inside the `to_string` starts its own transaction, owned by that implicit
call (§6.3).

### 3.3 Exclusive mode (progress)

`ATOMIC_ATTEMPTS = 8`. A transaction that has had 8 failed attempts (conflicts) runs its next attempt
**exclusive**: before taking `rv` it acquires the runtime's single exclusivity token, waiting (the
whole VM thread blocks; §6.9) while another transaction holds it or while commits that were already
waiting for the token are still in progress. While it holds the token, every other transaction's
commit that has something to publish, and every plain assignment `x = e` outside a transaction,
waits (the VM thread blocks) until the token is released. The exclusive attempt therefore can't
conflict and commits on that attempt. The token is released when the attempt commits, throws out of
the transaction, runs `retry`, fails inside the runtime (§6.3 "Guards"), or its VM is torn down.
This holds for every transaction, including one owned by an implicit runtime call (§3.2): its
exclusive wait is an OS-level wait of the VM thread inside the caller's step, exactly like the
commit wait every transaction already does, not a task suspension, so it needs no Promise. (Only
`retry` is refused for implicit owners, §3.4.) After a `retry` the failure count starts again from
0.

### 3.4 `retry`

`retry` inside a transaction:
- if the transaction's owner is an implicit runtime call: throws `RuntimeError` (kind `Internal`,
  as the existing "cannot suspend" error) with message
  `'LABEL' cannot suspend (it used 'retry') when called implicitly by the runtime` (LABEL =
  `to_string` or `message`); it is an ordinary error inside the transaction (§3.5);
- else if the attempt's read set is empty: throws `ThreadError` kind `stuck`, message
  `retry can never wake up: this transaction read no shared variable` (an ordinary error inside the
  transaction, §3.5);
- else the attempt is abandoned like a restart (state put back, entries discarded, the exclusivity
  token released if held), the transaction ends, and the task waits until any variable of the read
  set has a version different from the one recorded (if one already has, it runs again at once).
  The wait is a pending Promise (other tasks and timers keep running), counted as an internal
  runtime wait for M44's quiescence rule. When woken, the task evaluates the outermost `atomic`
  again from scratch (a new transaction). If the wait fails (`ThreadError stuck` from the
  quiescence rule), the error is thrown **at the outermost `atomic` expression**, outside the
  transaction.

### 3.5 Errors inside a transaction

- An error thrown in the body and caught in the body: ordinary control flow.
- An error leaving a joined (inner) `atomic`: depth − 1, keeps unwinding (§3.2).
- An error leaving the outermost `atomic`: the attempt's entries are discarded (nothing is
  published), the exclusivity token is released if held, the transaction ends, and the error keeps
  unwinding unchanged. No validation happens first: the attempt saw a consistent snapshot (§3.1), so
  the error is one the program could really produce.
- A commit whose published value contains a Promise (strict copy refused, M44 §4.2): nothing is
  published, the transaction ends (token released), and `ThreadError not_sendable`
  (`shared variable 'NAME' can't hold a Promise`, NAME = the first refused variable in first-access
  order) is thrown at the end of the `atomic` expression, outside the transaction.
- Errors thrown by the runtime inside a transaction (`in_atomic`, a native's ArgumentError, ...)
  are ordinary errors and follow the same rules.

### 3.6 Outside a transaction

- A read gives a fresh copy of the stored value (absent: `none`), never waits (M44).
- `x = e`: `e` is evaluated, strict-copied (refusal → `ThreadError not_sendable`
  `shared variable 'NAME' can't hold a Promise`, nothing stored), then atomically stored with a new
  version (waiting only while another task's transaction is exclusive), waking `retry` waiters of
  `x`. `shared let x = e` is such an assignment when the top-level code reaches it.
- The read point is unchanged from M44 (a shared read happens at its place in left-to-right order).
- Reads outside `atomic` happen one variable at a time, so `print(a, b)` or `let total = acct1 +
  acct2` can see `a` from before another thread's transaction and `b` from after it (a torn
  state). To read several shared variables consistently, read them together in one transaction:
  `let [a, b] = atomic { [x, y] }` (a read-only transaction: it never waits and never bumps a
  version).

### 3.7 What a rerun does not undo (documented, partly compile errors)

The body may run more than once. Only shared variables are transactional. Not undone:
- assigning a non-shared variable declared outside the `atomic` — **compile error E9** for the
  lexically visible cases (`count = count + 1`, `outer[k] = v`, `outer.f = v`, `self.n = ...`);
- changing an object reached from such a variable through a method (`outer.push(x)`) — not
  detected (Mah can't know which methods mutate);
- assigning a non-shared outer or global variable **from a function the block calls**
  (`fn inc() { count = count + 1 }` called inside `atomic`) or **from a closure written in the
  body** (`atomic { let f = fn() { g = 1 }; f() }`) — not detected (E9 sees only assignments
  lexically in the body's own function); both repeat on every rerun;
- `std:random` draws (a rerun draws again), `time.now_ms`/`time.monotonic_ms` reads;
- runtime objects that aren't shared variables: `channel_new`/`semaphore_new` inside a transaction
  create a new object on every run; `ch.len()`, `s.available()`, `t.pending()` read live,
  non-transactional state and are **not** part of the snapshot (a `retry` is not woken by them).

### 3.8 Side effects are refused

While the task is in a transaction (lexically or through calls), these throw `ThreadError` kind
`in_atomic` with message `'NAME' can't run inside 'atomic { }': its body may run more than once`
before doing anything:
- the `.await` opcode, whatever the Promise's state (NAME = `.await`);
- the `detach`, `detachkw`, `detachmethod`, `detachmethodkw` opcodes (NAME = `detach`) — these are
  what `detach f()` / `detach { }` compile to;
- `detach(t) f()` / `detach(t) { }` compiles to the native `thread.submit` (codegen), as does
  `t.run(...)`, so in a called function it reports NAME = **`thread.submit`** (the native's name,
  like every other refused native; the message is not specialized). Lexically inside `atomic` it is
  E4 (`'detach'`) at compile time;
- a call of any native in the list of §5.2 (NAME = the native's dotted name, e.g. `io.write` for
  `print`, `time.sleep_async`, `thread.channel_send`).

---

## 4. Compile-time rules (resolver/codegen errors; exact messages)

"**Lexically inside `atomic`**" means: inside an `atomic` body, in the same function — a nested
`atomic` inside it counts, a `defer` closure inside it counts (it runs before the body's function
returns, so inside the transaction), a nested `fn`/closure and the closure of a `detach` do not.

`NAME` is the variable's demangled source name; `P` is the integer source position, formatted as the
existing resolver messages do (`at position {P}`).

| # | rule | where | message |
|---|---|---|---|
| E1 | a method call whose receiver is a shared `x` or an index/field chain rooted at it, **not** lexically inside `atomic`, unless `x` is a handle variable (M44 §5.3, unchanged) and the receiver is `x` itself | resolver | `Method call on shared variable 'NAME' outside 'atomic { }': it would act on a copy; wrap it in 'atomic { ... }' at position P` (P = the root Ident) |
| E2 | an assignment into a shared `x` (`x[k] = v`, `x.f = v`, `x.f[k] = v`) not lexically inside `atomic` | resolver | `Assignment into shared variable 'NAME' outside 'atomic { }': it would change a copy; wrap it in 'atomic { ... }' at position P` (P = the assignment) |
| E3 | `x = e` (x shared) where `e` mentions `x` anywhere (nested closures included), not lexically inside `atomic` | resolver | `'NAME = ...' reads shared variable 'NAME' outside 'atomic { }': another thread can change it in between; wrap it in 'atomic { ... }' at position P` (P = the assignment) |
| E4 | `.await`, `sleep_async(...)`, `print(...)`, `input(...)` (the builtin `Call` with `builtin == "input"`, which lowers to `io.read_line` plus an await) or `detach` (any form) lexically inside `atomic` | resolver | `'WHAT' can't be used inside 'atomic { }': its body may run more than once at position P` (WHAT = `.await`, `sleep_async`, `print`, `input`, `detach`; P = the node's position) |
| E5 | `shared let` anywhere but the top level of a file (unchanged) | resolver | `'shared let' is only allowed at the top level of a file at position P` |
| E6 | `retry` not lexically inside `atomic`: an **undefined** identifier named `retry` anywhere, or a `RetryExpr` the resolver meets while not inside | resolver | `'retry' is only allowed inside 'atomic { }' at position P` (raised as the same exception class as "Undefined variable") |
| E6b | a `RetryExpr` (the keyword, §2.3) resolved while a binding named `retry` is visible (local, parameter, pattern/`for` binding, global, flat import — whatever `lookup("retry")` finds); for module-level names of a non-entry module or flat imports, which the preprocessor would have mangled, the preprocessor raises it instead (§8.1) | resolver / preprocessor | `'retry' here is the keyword (it ends this 'atomic { }' run); rename the variable 'retry' at position P` (P = the `retry` token) |
| E7 | a `let`/`shared let`/`fn` redeclaring a shared variable's name in the same scope (unchanged) | resolver | `'NAME' is a shared variable and can't be declared again in the same scope at position P` |
| E8 | `return` in an `atomic` body (outside any nested `fn`), or `break`/`continue` whose loop is outside the `atomic` | codegen (next to `_reject_in_detached`) | `'KEYWORD' can't leave an 'atomic { }' block at position P (use the block's value instead)` |
| E9 | an assignment lexically inside `atomic` whose target is, or is an index/field chain rooted at, a **non-shared** variable declared outside the outermost enclosing `atomic` of the same function chain | resolver | `'NAME' is declared outside 'atomic { }', and changing it there isn't undone when the block runs again; return what you need as the block's value at position P` (P = the assignment) |

Notes:
- Reads are always allowed. Method calls and assignments into shared variables lexically inside
  `atomic` are allowed (they act on the working value).
- E9 counts a variable as declared outside when its declaring frame is shallower than the body
  frame of the **outermost** lexically enclosing `atomic` (so a variable declared in an outer
  `atomic` body may be assigned in an inner one: a rerun re-declares it). Shared variables are exempt
  (they are the point). Pattern bindings and `for` variables declared inside the body are inside.
- A `defer` inside `atomic` keeps "inside" (E1 doesn't fire for `defer v.push(1)`; E9 does fire for
  `defer outer = 1`).
- **Handle variables** (M44 §5.3) are unchanged.
- The old M44 E4 ("'lock' takes shared variables") and E6 ("locked twice") no longer exist.

What the rules can't see, documented in §11.7 and the language reference: passing a shared
variable to a mutating function outside `atomic` (checker warning W2), mutating a loop variable bound
from a shared iterable outside `atomic` (W3), mutating a local copy (`let s = xs; s.push(1)`), a
read-compute-write split across a helper (`x = g()` where `g` reads `x`) outside `atomic`, and the
non-undone effects of §3.7. The rule users are given: **to change a shared value based on itself,
or in place, do it inside `atomic { }`.**

---

## 5. `ThreadError` and the natives refused in a transaction

### 5.1 Every kind and message (complete table after M45)

| kind | raised by | message (exact) |
|---|---|---|
| `closed` | unchanged (M44) | unchanged |
| `full` | unchanged | unchanged |
| `cancelled` | unchanged | unchanged |
| `deadlock` | an `.await` (or `t.join()`) that would close a cycle through at least one job or join edge | `deadlock: this await would never end (it waits, through threads, for itself)` (**changed text**) |
| `deadlock` | `t.join()` from one of `t`'s own jobs | `deadlock: a thread can't join itself` (unchanged) |
| `stuck` | quiescence (M44 §6.10), now also failing `retry` waits | `the wait can never finish: every thread is waiting` (unchanged) |
| `stuck` | a job whose root waits on a Promise nothing will settle | unchanged |
| `stuck` | `retry` in an attempt that read no shared variable | `retry can never wake up: this transaction read no shared variable` (new) |
| `not_sendable` | unchanged sources (args, results, messages) | `a Promise can't be sent to another thread` |
| `not_sendable` | a plain assignment or a commit whose value contains a Promise | `shared variable 'NAME' can't hold a Promise` |
| `foreign_promise` | unchanged | unchanged |
| `over_release` | unchanged | unchanged |
| `in_atomic` | §3.8 | `'NAME' can't run inside 'atomic { }': its body may run more than once` (new) |

Removed: `deadlock: waiting for 'VAR' would never end`.

### 5.2 The natives refused in a transaction (exactly these 53; both VMs; one shared list)

| module | refused | why |
|---|---|---|
| `io` | `io.print`, `io.write`, `io.input`, `io.read_line` | output/input happen once per run |
| `time` | `time.sleep_async`, `time.cancel` | timers (and a wait) |
| `promise` | `promise.resolve`, `promise.fail` | settling a Promise runs other tasks' continuations |
| `fs` | all 20: `fs.read_text`, `fs.write_text`, `fs.append_text`, `fs.info`, `fs.list_dir`, `fs.mkdir`, `fs.remove`, `fs.rename`, `fs.copy`, `fs.temp_dir`, `fs.open`, `fs.read_line`, `fs.read_all`, `fs.write`, `fs.close`, `fs.read_bytes`, `fs.write_bytes`, `fs.append_bytes`, `fs.file_read_bytes`, `fs.file_write_bytes` | file I/O (all of it is queued I/O whose result needs an await anyway) |
| `process` | `process.exit`, `process.run`, `process.env_set`, `process.env_remove` | leave the process, run programs, change the environment |
| `socket` | all 10: `socket.connect`, `socket.listen`, `socket.accept`, `socket.send`, `socket.recv`, `socket.shutdown`, `socket.close`, `socket.start_tls`, `socket.tls_server_config`, `socket.start_tls_server` | network I/O |
| `thread` | `thread.spawn`, `thread.submit`, `thread.close`, `thread.join`, `thread.semaphore_acquire`, `thread.semaphore_try_acquire`, `thread.semaphore_release`, `thread.channel_send`, `thread.channel_recv`, `thread.channel_try_recv`, `thread.channel_close` | other threads would see them at once and a rerun would repeat them |

**Allowed** (and why): `math.*`, `string.*`, `regex.*`, `value.*`, `bytes.*`, `reflect.*` — pure
functions of their arguments (std:json is pure Mah); `random.*` — they only change the generator's
own state (a Mah value of this VM; a rerun draws again, documented §3.7); `hooks.*` — they change
only this VM's own hook tables or one object, like assigning a non-shared variable (documented as
not undone); `time.now_ms`, `time.monotonic_ms`, `process.args`, `process.env_get`,
`process.env_all`, `process.cwd`, `process.pid`, `process.platform` — reads that change nothing;
`promise.new` — a fresh Promise changes nothing (it can't be stored in a shared variable or awaited
in the transaction anyway); `thread.pending`, `thread.current`, `thread.cores`,
`thread.semaphore_available`, `thread.channel_len`, `thread.channel_closed` — reads of live state
(documented as not part of the snapshot); `thread.semaphore_new`, `thread.channel_new` — a new
object nobody else can see until the transaction publishes it (a rerun makes another; documented).

The list lives once per VM: Python `ATOMIC_REFUSED_NATIVES: frozenset[str]` in
`mah/thread_runtime.py`; Rust `pub const ATOMIC_REFUSED_NATIVES: &[&str]` in
`runtime/src/vm/thread.rs`. Both contain exactly the 53 names above. A parity test (§12.1
`RefusedNativesParityTests`) keeps them in sync: the frozenset has exactly 53 entries, all in
`mah.natives.NATIVES`, and it equals the set of string literals parsed out of the Rust const (a
regex over `thread.rs` between `ATOMIC_REFUSED_NATIVES` and the closing `];`, the way the existing
opcode-table parity tests read Rust sources).

---

## 6. The runtime model (both VMs)

### 6.1 Runtime state (replaces M44's `shared`/`locks`/`waiting_on`)

In the process-wide runtime, all under the one runtime mutex (M44 §6.1):
- `shared: map index → (stored value, version)` — the stored value is a strict copy (Python: a
  copied object graph that is never mutated or handed out; Rust: `Arc<Payload>`); absent =
  `(none, 0)`.
- `clock: integer` from 0 (the version of the last commit or plain write).
- `watchers: map index → set of retry-wait keys`; `retry_waits: map key → (VM, promise/pending id,
  list of indices)`. Python key = `id(promise)`; Rust key = `(vm id, pending id)`. The watcher
  sets are ordered by when each wait began (Python: a dict used as an ordered set; Rust: a
  `BTreeSet` of `(vm id, pending id)`), so one write wakes a VM's waiters in the order they
  waited, on both VMs.
- Exclusivity: `excl_owner` = none or `(tx serial, vm id)`; `excl_queue` = FIFO of tx serials
  waiting to become exclusive; `commits_waiting` = how many commits/plain writes are blocked on
  the token; a condition variable `excl_cv` on the runtime mutex (Python
  `threading.Condition(self.lock)`; Rust `Condvar` used with the `Mutex<RtState>` guard).
- Removed: `locks`, `waiting_on`, `_LockState`/`LockState`, `acquire`/`release`/`grant_next`/`mark`,
  the lock edge in the wait-for graph, the `lock` completion (`Completion::Lock`), `Wait::Lock`,
  `Task.held`/`Held`. `tracking` (await edges) is now set only by `thread.spawn`.

### 6.2 Per-task state

`Task` gains `tx` (none or a shared reference to a `Tx`) and `implicit` (none, or the label
`"to_string"`/`"message"` of an implicit runtime call's sub-task). `invoke_sync` sets, on the
sub-task: `id` = the stepping top's id (M44), `tx` = the stepping top's `tx` (the same object, by
reference; none if the top has none or there is no top), `implicit` = its `label` argument.
Every other task (`detach`, job roots, the main task, test tasks) starts with `tx` none, `implicit`
none.

`Tx` (one per transaction; Python class in `mah/runtime_values.py`, Rust struct in
`runtime/src/vm/value.rs`):

| field | meaning |
|---|---|
| `serial` | process-unique, from a counter starting at 1 (Python `itertools.count(1)`; Rust `static NEXT_TX: AtomicU64`) |
| `owner` | the task that began it (Python: the `Task` object, compared with `is`; Rust: `Rc::as_ptr(task) as usize`) |
| `implicit` | the owner's `implicit` label (none for a normal task) |
| `depth` | nesting depth; 0 = between a conflict and the restarted `atomicbegin` |
| `rv` | the attempt's snapshot time |
| `reads` | index → version, in first-read order |
| `entries` | index → entry, in first-access order; an entry is `{name, base, working, assigned, exposed}`: `name` the source name (from the opcode), `base` the stored value it was copied from (none for a blind write), `working` the working value, `assigned` (a `sharedset` replaced it), `exposed` (a working-mode `sharedget` handed it out) |
| `attempts` | failed attempts (conflicts) so far |
| `irrevocable` | this attempt is exclusive |
| `restart` | `(pc of the outermost atomicbegin, the frame current there, return-stack length, defer-stack length)` |

### 6.3 The opcodes (exact algorithms; "lock" below = the runtime mutex)

**`atomicbegin`** (at pc `B`):
1. `tx = task.tx`. If none: create `Tx(owner = task, implicit = task.implicit, restart = (B,
   task.current_frame, len(return_stack), len(defer_stack)))` with depth 1, attempts 0; `task.tx =
   tx`; `tx_start(vt, tx)` (guarded).
2. Else if `tx.depth > 0`: `tx.depth += 1` (join). Nothing else.
3. Else (depth 0, a restart): `tx.depth = 1`; if `tx.attempts >= ATOMIC_ATTEMPTS`:
   `tx.irrevocable = True` (for every owner, implicit ones included, §3.3); `tx_start(vt, tx)`
   (guarded).

`tx_start(vt, tx)`: under the lock: if `tx.irrevocable`, `acquire_excl(vt, tx.serial)` (§6.9); then
`tx.rv = clock`.

**`sharedget k, name, mode`** (mode 1 "working" = lexically inside `atomic`; 0 "copy"):
1. `tx = task.tx`. If none: mode 1 → `RuntimeError.Internal` `sharedget in working mode outside a
   transaction` (codegen never emits that); mode 0 → `(v, _) = read_shared(k)` (under the lock),
   return a **local copy** of `v` (M44 §4.2 local mode; copy outside the lock).
2. If `k` has no entry: `(v, ver) = read_shared(k)`; if `ver > tx.rv` → **restart (conflict)**;
   else `tx.reads[k] = ver`; entry = `{name, base: v, working: local copy of v, assigned: false,
   exposed: false}`.
3. mode 1: `entry.exposed = True`; return `entry.working` itself. mode 0: return a local copy of
   `entry.working`.

**`sharedset k, name, src`** (value `v`):
1. `tx = task.tx`. If not none: `w = local copy of v` (M44 §4.2 local mode, Promises kept as the
   same object; Rust: the same local copy — never `v` itself, §3.1); no entry → create `{name,
   base: none, working: w, assigned: true, exposed: false}` (no read-set record); else
   `entry.working = w; entry.assigned = True`. Done.
2. Else (a plain write): strict-copy `v` (refusal → throw `ThreadError not_sendable` `shared variable
   'NAME' can't hold a Promise`, NAME = `name`); `write_shared(vt, k, copy)`:
   under the lock: `wait_no_excl(vt, None)`; `clock += 1`; `shared[k] = (copy, clock)`;
   `wake_watchers([k])`.

**`atomicend`** (after the body's call returned; the body's value is already in a temp):
1. `tx = task.tx` (none → `RuntimeError.Internal` `atomicend outside a transaction`). If
   `tx.depth > 1`: `tx.depth -= 1`; done.
2. Build the publish list (outside the lock), in entry order: skip an entry unless `assigned` or
   `exposed`; strict-copy its `working` (refusal → remember the entry's `name` and **stop**);
   if not `assigned` and `same_copy(copy, entry.base)` → skip; else append `(k, copy)`.
3. A refusal: `end_attempt(tx)`; `task.tx = None`; throw `ThreadError not_sendable`
   `shared variable 'NAME' can't hold a Promise`.
4. `tx_commit(vt, tx, publish)`; false → **restart (conflict)**; true → `task.tx = None`.

Steps 2–4 run guarded (below).

`tx_commit(vt, tx, publish)`, under the lock:
```
if publish:
    wait_no_excl(vt, tx.serial)
    for k, ver in tx.reads: if version_of(k) != ver: return False     # (version_of absent = 0)
    clock += 1
    for k, copy in publish: shared[k] = (copy, clock)
    wake_watchers([k for k, _ in publish])
release_excl(tx.serial)
return True
```
(A read-only transaction neither waits nor validates.)

**`atomicabort`** (in the handler, §8.5; the error is in a temp the next `throw` re-throws):
1. `tx = task.tx` (none → `RuntimeError.Internal` `atomicabort outside a transaction`). If
   `tx.depth > 1`: `tx.depth -= 1`; done.
2. `end_attempt(tx)`; `task.tx = None`.

`end_attempt(tx)`: under the lock, `release_excl(tx.serial)`.

**`retry`**:
1. `tx = task.tx` (none → `RuntimeError.Internal` `retry outside a transaction`).
2. `tx.implicit` not none → throw `RuntimeError` (kind `Internal`) `'LABEL' cannot suspend (it used
   'retry') when called implicitly by the runtime`.
3. `tx.reads` empty → throw `ThreadError stuck` `retry can never wake up: this transaction read no
   shared variable`.
4. Else **restart (retry)**.

**Guards (no leaked transaction).** `tx_start` in `atomicbegin` steps 1 and 3, and `atomicend`
steps 2–4, run inside a guard: on **any** error other than a restart signal (a Python
`RecursionError`/`MemoryError` from a copy or `same_copy`, `_Timeout`, `ProgramExit` or `Abandoned`
out of `acquire_excl`/`wait_no_excl`, an Internal error; in Rust any `Err` from `payload_of`, a
copy or a wait whose `restart` is `None`), the guard runs `end_attempt(tx)` (releases the token if
held) and sets `task.tx = None` — these steps only ever run for the outermost `atomic` (a join
neither starts nor commits), so the guard always ends the whole transaction — and then re-raises
the error unchanged. The error then unwinds from the `atomicbegin`/`atomicend` instruction, which
is outside the body's handler region (§8.6), to the enclosing handlers like any runtime error.
Python: `try: ... except TxRestart: raise` / `except BaseException: <cleanup>; raise`. Rust: a
`match` on the `Err`, cleaning up unless `e.restart.is_some()`. As a safety net, when a task ends
(done or failed, in `drive`/the step loop's finish path) with `task.tx` not none and the task is
`tx.owner`, the VM calls `end_attempt(tx)` and sets `task.tx = None` before reporting the result.
After such an error, the task is outside any transaction: a later `atomic` begins a new one, plain
assignments write directly, natives are not refused.

**Restart** is a non-local transfer to the transaction's owner (§6.4). Restart (conflict) and
restart (retry) are raised from `sharedget`, `atomicend` and `retry`, possibly inside an implicit
call's sub-task stepped synchronously inside the owner's step.

### 6.4 Restarting (both VMs)

- Python: `class TxRestart(BaseException)` in `mah/thread_runtime.py` with `kind` (`"conflict"` or
  `"retry"`); raised as `raise TxRestart("conflict")`. It is a `BaseException`, so no
  `except Exception` (the step loop's own host-error wrapper, `invoke_sync`, natives) swallows it.
- Rust: `RuntimeError` gains `pub restart: Option<TxSignal>` (`pub enum TxSignal { Conflict, Retry
  }` in `runtime/src/vm/error.rs`; every existing constructor sets `None`;
  `RuntimeError::restart(sig)` builds one). Every `?` propagates it; no code path may turn such an
  error into another error or swallow it (the only `.ok()`-style fallbacks on `invoke_sync`/`to_str`,
  in `uncaught_message`, run outside any transaction).

The step loop (`_step_task` / `step_task_inner`) handles it **before** any other error handling:
if `task.tx` is not none and `task.tx.owner` is this very task → `restart_tx(task, kind)`; its
result is either "continue stepping" or "suspended" (return that from the step loop). Otherwise
(this task is an implicit-call sub-task that joined someone else's transaction) re-raise / return
the error unchanged, so it propagates out of `invoke_sync` into the owner's step loop.

`restart_tx(task, kind)`:
```
tx = task.tx
(pc, frame, rs, ds) = tx.restart
task.pc = pc; task.current_frame = frame
truncate task.return_stack to rs; truncate task.defer_stack to ds
end_attempt(tx)                                   # releases the token if held
if kind == conflict:
    tx.attempts += 1; tx.depth = 0; tx.reads = {}; tx.entries = {}; tx.irrevocable = False
    return CONTINUE                                # the next instruction is atomicbegin at pc
# retry
reads = tx.reads
task.tx = None
promise = retry_wait(reads)                        # §6.5
if promise is None: return CONTINUE                # something already changed: run again now
register a continuation on promise:
    settled → task.pc = pc; drive(task)            # atomicbegin runs again: a new transaction
    failed(e) → drive(task, pending=(e, pc))       # thrown at the atomic expression
return SUSPENDED
```
Python: the continuation is a closure appended to `promise.callbacks` (shape of the `await` case's
`_resume(ok, value)`). Rust: `Continuation` gains `pub restart: bool` (all existing constructions
pass `false`); for retry push `Continuation { task, dest: (0, 0), resume_pc: pc + 1, restart: true }`;
`resolve_promise` for a `restart` continuation sets `pc = resume_pc - 1` and writes nothing;
`fail_promise` already throws at `resume_pc - 1`.

### 6.5 `retry` waits and wake-ups

`retry_wait(vt, reads) -> Promise | None`, under the lock: if any `k` in `reads` has
`version_of(k) != reads[k]` → return none. Else create a pending Promise, register it as an
**internal** wait of this VM (Python `vt.add_wait(promise, "retry", keys)`; Rust `let id =
vm.alloc_id(); vm.add_wait(id, promise.clone(), Wait::Retry)`), record `retry_waits[key] = (vm,
promise or pending id, keys)` and add `key` to `watchers[k]` for every `k`; return the Promise.

`wake_watchers(keys)` (lock held; called by every commit that publishes and every plain write):
for each `k` in `keys`, for each `key` in `watchers.pop(k)`: if `retry_waits.pop(key)` exists, remove
`key` from the watcher sets of its other indices and `post` to its VM a **settle ok `none`**
completion for its Promise (Python `(promise, "settle", (True, NONE_VALUE))`; Rust
`Completion::Settle(Ok(Payload::none()))` with the pending id). The VM's existing `settle` handling
resolves it (the wait's pending entry is removed as for any internal wait).

Removal of a retry waiter (stuck handling `_remove_waiter`/`remove_waiter`, teardown): pop it from
`retry_waits` and discard its key from every watcher set. A completion whose entry is gone is
dropped (M44).

Quiescence: unchanged rule; a VM whose only pending entries are retry waits is blocked like any VM
with only runtime waits. The main VM's `stuck` handling fails them like any internal wait (§3.4:
the error is thrown at the `atomic` expression).

### 6.6 `same_copy(a, b)` (both VMs; decides whether an exposed, unassigned entry changed)

Both arguments are strict copies (no Promise). A parallel, iterative walk over pairs with two memos
(Python `id(x) → id(y)` and `id(y) → id(x)`; Rust node indices of the two graphs): the pair is
"same" iff every pair visited is same:
- `none`/`Bool`/`String`: both the same kind and equal; `Number`: both Numbers and numerically
  equal; `Type`: both Types with equal kind and index (the same internal identity `==` uses); the absent marker: both absent.
- Heap pairs: different kinds → not same. A pair whose left was already paired with a different
  right (or right with a different left) → not same; already paired with each other → same (skip).
  Else memoize and compare: Vector — equal lengths, items pairwise; Map — equal lengths, the same
  map keys in the same order (by `map_key`), original keys same by the scalar rule, values
  pairwise; Bytes — equal contents; Struct — equal **internal type identity** (the VM's own
  `type_name`, the mangled module-scoped name that `==` and `matchstruct` compare — never a
  source or demangled name: `a.R { v: 1 }` and a local `R { v: 1 }` are not same), the same field
  names in the same order, fields pairwise; Enum — equal internal type identity and variant, then
  as Struct.
- A Closure or Frame anywhere → **not same** (conservative: such entries are always published).

The result only decides whether a version is bumped (and retry waiters woken); it never changes
what a program prints.

### 6.7 Where `in_atomic` is checked

Before anything else happens in: the `await` opcode; the four `detach*` opcodes; the `native`
opcode for a native in `ATOMIC_REFUSED_NATIVES` — when `task.tx` is not none. Linking precomputes it:
Python's linked native instruction becomes `("native", impl, arg_addrs, dest, atomic_name)`
(`atomic_name` = the native's name if it's in the list, else `None`); Rust's
`LinkedInstr::Native` gains `atomic: Option<&'static str>` (the matching element of the list).
The throw is a thrown Mah value (`MahThrow` / `RuntimeError::thrown_value`), catchable.

### 6.8 Interactions

- **Copies (M44 §4).** Working values are local copies (M44 local mode, Promises kept as the same
  object); publishing strict-copies (Promise → `not_sendable`); outside-transaction reads are local
  copies of the stored value. Shared variables are never in a job's snapshot. A job's tasks start
  with no transaction.
- **Same-VM tasks.** A transaction never suspends while an attempt runs (await, detach and waiting
  natives are refused; `retry` ends the attempt first), so two transactions of one VM never
  interleave; conflicts come only from other threads.
- **Job teardown (`forget_vm`, M44 §6.6)**, under the lock, in addition to M44's steps (minus the
  lock steps): remove every `retry_waits` entry of this VM (and its watcher keys); if `excl_owner`
  names this VM, `release_excl` it. A transaction that was running in the VM is simply gone (nothing
  of it was published). A thread blocked in `acquire_excl`/`wait_no_excl` has already left the wait
  (it raised `Abandoned`, removing itself, §6.9) before teardown runs.
- **`process.exit`.** Refused inside a transaction (`in_atomic`). An exit requested by another
  thread reaches a Python VM blocked in an exclusivity wait through its wait check (§6.9); Rust's
  process exit ends every thread.
- **Wait-for graph.** Lock edges are gone; `retry` waits are not edges. The await check (job/join
  edges) is unchanged except its message (§5.1) and `tracking` being set only by `thread.spawn`.
- **Program end / quiescence:** unchanged; retry waits are internal waits.

### 6.9 Exclusivity: algorithms and why they can't deadlock

All three are called with the runtime mutex held; a wait releases it (condition variable). Python
waits are `excl_cv.wait(0.05)` followed by `vt.wait_check()` (§9.4: raises `_Timeout` past a test
deadline, `ProgramExit` in the main VM after a job's `process.exit`, `Abandoned` in a job VM after
shutdown); Rust waits are `excl_cv.wait_timeout(guard, 50 ms)` with no check.

```
acquire_excl(vt, serial):     # vt = the calling VM's thread state; its id goes into excl_owner
    excl_queue.push_back(serial)
    while not (excl_owner is none and excl_queue.front == serial and commits_waiting == 0):
        wait            # Python: on any exception, remove serial from excl_queue, notify_all, re-raise
    excl_queue.pop_front(); excl_owner = (serial, vt.vm_id)

wait_no_excl(vt, serial):   # serial = the committing transaction's, or none for a plain write
    if excl_owner is none or excl_owner.serial == serial: return
    commits_waiting += 1
    try: while excl_owner is not none and excl_owner.serial != serial: wait
    finally: commits_waiting -= 1; if commits_waiting == 0: notify_all

release_excl(serial):
    if excl_owner is not none and excl_owner.serial == serial: excl_owner = none; notify_all
```

Properties:
- **At most one exclusive transaction**: `excl_owner` is a single slot, changed only under the
  mutex.
- **It can't conflict**: it takes `rv` after acquiring; from then on every commit that publishes and
  every plain write waits in `wait_no_excl` (a read-only commit publishes nothing), so no version
  exceeds `rv` until it releases; its reads never conflict and its validation passes.
- **No deadlock**: the holder never waits for anything while holding the token — inside an attempt
  `.await`, `detach`, every waiting native and `process.exit` are refused (`in_atomic`), its own
  commit doesn't wait (`excl_owner.serial == serial`), nested `atomic`s join (no second
  `acquire_excl`), an implicit call's `atomic` joins its transaction, and every way an attempt
  can end releases the token: a commit, a conflict restart, `retry`, a throw out of the
  transaction (`atomicabort`), a `not_sendable` refusal at commit, **an error inside
  `atomicbegin`/`atomicend` itself (the guards of §6.3)**, the end of the owning task with a
  transaction still set (the safety net of §6.3), and a VM teardown. An implicit-call owner that
  is exclusive (§3.3) is no different: its attempt runs to its end inside one step of the caller's
  VM (inside `invoke_sync`) and waits for nothing. A waiter waits only for the holder
  (commits/plain writes) or for the holder plus the commits already waiting when it was released
  (`commits_waiting == 0`), and those never wait for a begin-waiter. So the waits-for relation
  between OS threads has no cycle. A waiter is never in the holder's VM: the holder's attempt runs
  to its end within one step of its task, during which its VM thread runs nothing else, and an
  implicit call inside that step joins the holder's transaction instead of committing on its own.
- **Bounded waiting for the exclusive transaction**: begin-waiters are served in FIFO order of
  `excl_queue`; a released token goes to the front waiter as soon as the commits that were already
  waiting have gone through (new commits arriving while the token is free don't wait and don't
  count).
- The blocking is OS-level (the VM thread waits; its other tasks and timers pause): it lasts only as
  long as one exclusive attempt (documented: a long exclusive transaction holds up every other
  thread's commits).

---

## 7. Bytecode 1.21 (redefined; MINOR stays 21)

### 7.1 Why no bump

1.21 was introduced by M44 and has never been released: there is no git tag, the www post v0.4.0 is
unpublished, and `mah-vm` 0.4.0 has not shipped — checked by the thinker on 2026-10-08:
`cargo search mah-vm` prints nothing and the crates.io API (`GET /api/v1/crates/mah-vm`) answers
`crate 'mah-vm' does not exist`, so no published binary of any version accepts 1.21. HEAD's "Bump
mah-vm to 0.4.0" only changed the version in the unreleased tree. A `mah-vm` built by hand from the
M44 tree (`cargo install --path`/from `main` before M45) is a development build; it refuses an M45
file with `unknown opcode 0x74 ...` instead of a version error, which is accepted (development
builds of unreleased formats are not supported). **Part 2 re-runs the check before landing; if
`mah-vm` 0.4.0 (or any version accepting 1.21) has been published by then, it stops and the thinker
bumps MINOR to 22 instead.** Redefining 1.21 in place keeps one MINOR for the whole
threads feature. To make a stale M44-era 1.21 file fail cleanly instead of misbehaving, the two
removed opcodes' codes are **left unassigned** (a file using 0x72/0x73 is refused with the existing
`unknown opcode 0x72 at instruction I` error) rather than reused.

### 7.2 Opcodes

| name | code | operands | meaning |
|---|---|---|---|
| `sharedget` | `0x70` | `N` index, `S` name, `N` mode, `A` dest | §6.3; mode 0 = copy, 1 = working (lexically inside `atomic`) |
| `sharedset` | `0x71` | `N` index, `S` name, `A` src | §6.3 (in a transaction: the working value; else a plain atomic write) |
| — | `0x72`, `0x73` | — | unassigned (were M44's `sharedlock`/`sharedunlock` in unreleased builds) |
| `atomicbegin` | `0x74` | — | §6.3 |
| `atomicend` | `0x75` | — | §6.3 |
| `atomicabort` | `0x76` | — | §6.3 |
| `retry` | `0x77` | — | §6.3 |

All six have since-minor 21 (`OPCODE_SINCE_MINOR`, Rust `opcode_info`); a file below 21 using one
is refused with the existing `opcode 'NAME' at instruction I requires minor version >= 21, but this
file's minor version is M`. Linking refuses a `sharedget` mode other than 0/1 with
`RuntimeError.Internal` `bad mode M for 'sharedget'` (both VMs; the `sharedunlock` case is gone).

IR (codegen → `lower.py`, 4-slot tuples):

| IR | lowered |
|---|---|
| `("sharedget", (index, mode), name, dest)` | `Instr("sharedget", (index, intern(name), mode, dest))` (unchanged) |
| `("sharedset", index, name, src)` | `Instr("sharedset", (index, intern(name), src))` (unchanged) |
| `("atomicbegin", None, None, None)` | `Instr("atomicbegin", ())` |
| `("atomicend", None, None, None)` | `Instr("atomicend", ())` |
| `("atomicabort", None, None, None)` | `Instr("atomicabort", ())` |
| `("retry", None, None, None)` | `Instr("retry", ())` |

Python linked forms: `("sharedget", k, name, mode, dest)`, `("sharedset", k, name, src)`,
`("atomicbegin",)`, `("atomicend",)`, `("atomicabort",)`, `("retry",)`, and
`("native", impl, arg_addrs, dest, atomic_name)`. `mah dis` prints `sharedget`'s mode as
`copy`/`working`, and the four new opcodes with no operands.

### 7.3 Pins

- `mah/bytecode/format.py`: `MINOR` stays 21; the docstring paragraph becomes "M44 bumps MINOR to
  21: the shared-variable opcodes `sharedget`/`sharedset`, the transaction opcodes `atomicbegin`/
  `atomicend`/`atomicabort`/`retry` (M45 redefined the unreleased 1.21, replacing `lock`'s
  `sharedlock`/`sharedunlock`) and the `thread.*` natives behind std:thread (docs/MAHC_FORMAT.md
  #4.4/#4.6/#6.11)"; `OPCODES`: remove `sharedlock`/`sharedunlock`, add the four rows;
  `OPCODE_SINCE_MINOR`: remove the two, add the four (= 21). No native changes.
- `runtime/src/decode.rs`: `MINOR` stays 21; `opcode_info` rows changed the same way.
- No test's MINOR pin changes. `_file_minor` is table driven: a program using `atomic` (even
  without `shared let`) is written as 1.21.

---

## 8. Part 1 — the compiler

### 8.1 `mah/preprocessor.py`

`_uses_prelude`: replace the M44 pair trigger `... or a.value == "lock"` by: an `id` token `atomic`
immediately followed by a `punct` token `{` (a program using `atomic` can get an `in_atomic`,
`not_sendable` or `stuck` ThreadError). Keep the `shared` `let` trigger. Update the comment.
(`export shared let` handling stays.)

**Name mangling must not touch the contextual keywords.** In a non-entry module `process` rewrites
every `id` that spells one of the module's top-level names or a flat import (`name_rewrite`) into
its mangled name. Without care, a library with a top-level `fn retry`/`let retry` would turn its
keyword `retry` into a reference to that binding (the transaction would just go on), and one with
`struct atomic`/`fn atomic`/`let atomic` would turn `atomic {` into a struct literal of
`__mN_atomic` (a syntax error) — the same source would behave differently as the entry file. So the
rewrite loop tracks the parser's §2.2/§2.3 decisions with a token-level mirror:

- **`atomic` keyword token.** An `id` `atomic` is the keyword (never rewritten) iff: the previous
  token is not a `dot`; it is not in a condition head at that head's own paren/bracket depth (a head
  starts at an `id` `if`/`elif`/`while`/`for`/`match` and ends at the first `punct` `{` at the same
  paren/bracket depth as that id; `if (atomic { ok }) { ... }` is deeper, so the keyword — matching
  the parser, which allows struct literals again inside parentheses, §2.4); the next token is
  `punct` `{` with no newline between them; and the token after the `{` is neither `punct` `}` nor
  an `id` followed by `punct` `:` (§2.2 rules 2–5).
- **Atomic regions.** A stack of booleans parallel to brace depth: every `{` pushes a flag — `True`
  for the `{` of a keyword `atomic`; `False` for the `{` that opens a function body (the first `{`
  after an `id` `fn` or a `test` block's string, at the same paren/bracket depth as the `fn`;
  methods of `impl`/`trait` bodies are `fn`s too); otherwise a copy of the top (`False` when empty);
  every `}` pops. "Inside an atomic body" = the top is `True`. This is exactly the parser's
  `_atomic_depth > 0` with its resets in every function-body path.
- **`retry` keyword token.** An `id` `retry` inside an atomic body whose next token is `punct`
  `}` `;` `,` `)` `]`, or EOF, or starts on a later line (§2.3), is the keyword: it is **never
  rewritten**. If `retry` is in `name_rewrite` (the module has a top-level `retry`, or a flat
  import brought one in), the preprocessor also records **E6b** (§4) at that token, like its other
  errors (`errors.append(...)`; for a non-entry module reported at the import site with `(in
  'FILE')`, as the existing package errors are), because the resolver can no longer see that
  binding under the name `retry`.
- Everything else is unchanged (`retry(1)`, `retry + 1`, `atomic + 1`, `atomic { v: 1 }` inside a
  module are still rewritten references).

The mirror and the parser must agree on every token; the §12.1 module tests (T13b) check it.

### 8.2 `mah/compiler/lexer.py`

`peek_tokens(n)` (§2.2). Nothing else.

### 8.3 `mah/compiler/ast_nodes.py`

- Remove `LockExpr`, `LockAcquire`, `LockRelease`.
- `FnExpr`: `atomic: bool = field(default=False, repr=False)` next to `detached`.
- New: `AtomicExpr(closure: FnExpr, position: int)`; `RetryExpr(position: int)`.
- `Ident`: rename `shared_locked` to `shared_atomic` (`field(default=False, repr=False)`): set by
  the resolver on every shared Ident written lexically inside `atomic` (§4's definition).
- Module docstring: a short M45 paragraph.

### 8.4 `mah/compiler/parser.py`

- Remove `_at_lock`, `_parse_lock` and their call; remove the `LockExpr` import/usage; `LockExpr`
  out of the block-shaped tuple, `AtomicExpr` and `RetryExpr` in.
- `_atomic_depth` (§2.2), `_at_atomic`/`_parse_atomic` (§2.2), the `retry` rule (§2.3), the resets in
  every function-body path (`_parse_fn_expr`, `_parse_method_decl`, `_parse_test_decl`, ...; §2.2).
- Update the M44 section comment (`shared let` / `atomic` / `retry` contextual words).

### 8.5 `mah/compiler/resolve.py`

- Remove `_held_locks`, `_resolve_lock`, `_resolve_lock_step`, `_lookup_shared_quietly`, the
  `LockExpr`/`LockAcquire`/`LockRelease` cases and imports.
- New stacks: `self._in_atomic: list[bool] = [False]`; `self._atomic_frames: list = [None]` (the
  frame depth of the outermost enclosing atomic body in the current function chain, or None).
- `_resolve_fn_expr(fn, ..., keep_atomic: bool = False)` (rename of `keep_locks`; the `DeferStmt`
  case passes `keep_atomic=True`): around the body push
  - `_in_atomic`: `True` if `fn.atomic`; the current top if `keep_atomic`; else `False`;
  - `_atomic_frames`: if `fn.atomic`: the current top if it is not None, else
    `self.frame_stack[-1].depth + 1` (the body's frame depth); if `keep_atomic`: the current top;
    else `None`;
  and pop both in the `finally`.
- `AtomicExpr`: `self._resolve_fn_expr(expr.closure)`. `RetryExpr`: if not `_in_atomic[-1]` → E6;
  else if a lookup of the name `retry` (the ordinary scope chain: locals, params, pattern and `for`
  bindings, globals, imports — without marking anything as used or captured) finds a binding → E6b
  with the `RetryExpr`'s position.
- `_bind_ident`: `ident.shared_atomic = self._in_atomic[-1]` for a shared symbol (replacing
  `shared_locked`). When the lookup of a name `retry` fails, raise E6's message instead of the
  undefined-variable message (same exception class).
- `_shared_unheld(node)` → `_shared_outside_atomic(node)`: a shared `Ident` and not
  `_in_atomic[-1]`. E1 (`_check_shared_method_call`), E2, E3: same conditions as M44 with "not held"
  replaced by "not inside atomic", and the §4 messages.
- E4: in `resolve_expr`, before resolving the node's parts, when `_in_atomic[-1]`: `FieldAccess`
  with `field == "await"` → `'.await'`; `SleepAsyncExpr` → `'sleep_async'`; `DetachExpr` →
  `'detach'`; a `Call` with `builtin == "input"` (whatever `_BUILTIN_FNS` resolution marks as the
  builtin `input`, not a user function named `input`) → `'input'`; in `resolve_stmt`, `PrintStmt` →
  `'print'`. Position: the node's `position`.
- E9: in `AssignStmt`, after the existing checks: `root = self._chain_root(stmt.target)`; if `root`
  is an `Ident` with `shared_index is None` and an `address`, and `af = self._atomic_frames[-1]` is
  not None and `self.frame_stack[-1].depth - root.address[0] < af` → E9 with
  `display_name(root.name)`.
- Everything else of M44's shared handling (E5, E7, handle variables, `shared_names`, symbol kind
  `"shared"`) is unchanged.

### 8.6 `mah/compiler/codegen.py`

- Remove `_gen_shared_store`, `_gen_lock`, the `LockExpr`/`LockAcquire`/`LockRelease` cases.
- Shared read (`Ident` with `shared_index`): `("sharedget", (idx, 1 if ident.shared_atomic else 0),
  name, dest)` (unchanged shape).
- `_gen_store` for a shared `Ident` and `shared let`: always `("sharedset", idx, name, src)`.
- `AtomicExpr` → `_gen_atomic(expr)` returning `dest`:
  ```
  dest = temp
  c = gen_expr(expr.closure)                       # the closure instruction
  emit ("atomicbegin", None, None, None)
  err = temp
  R = _open_region()
  emit ("call", c, (), None)
  v = temp; emit ("retval", None, None, v)
  segments = _close_region(R)
  emit ("atomicend", None, None, None)
  emit ("=", v, None, dest)
  skip = placeholder
  HANDLER: _emit_handler_entries(segments, HANDLER, err[1])
  emit ("atomicabort", None, None, None)
  emit ("throw", err, None, None)
  patch skip → ("jmp", None, None, END)
  END:
  ```
  The region covers only the call and `retval` (so `atomicbegin`, `atomicend` and the handler are
  outside it; a `not_sendable` from `atomicend` and a retry-wait failure thrown at `atomicbegin`
  propagate to the enclosing handlers). Because they are outside the region, `atomicbegin`'s
  `tx_start` and `atomicend`'s steps 2–4 clean up after themselves on any error (§6.3 "Guards").
- **The synthesized closure is not a user function.** `FnExpr.atomic` marks a block, and every
  pass other than the resolver's scope handling and codegen treats it as one:
  - codegen emits it like any closure (function table / META entry included; no name, so the
    entry is the existing anonymous-closure entry — both VMs read the same table, nothing new);
  - **backtraces** (M28 test failures, uncaught-error reports): the body's call is a real call, so a
    backtrace through an `atomic` contains one extra entry, the `call` instruction's pc — the line
    of the `atomic` keyword — between the throw site and the enclosing function's caller. This is
    kept (it names where the transaction is) and is identical in both VMs because both build
    backtraces from the same return stack; a `vm_diff` case (§12.2 `threads_atomic_uncaught`)
    checks the output of an uncaught error thrown inside a helper called from an `atomic`;
  - the checker (§8.7) checks the body inline, never as a lambda; `mah check --level explicit`'s
    "every declaration whose type can't be inferred must be annotated" scan skips `FnExpr.atomic`
    (it declares nothing) and descends into its body as a block;
  - LSP walkers (§11.3) — document symbols, inlay hints (parameter/return hints for lambdas),
    signature help, semantic tokens, folding, "unused" and closure-capture analysis — skip the
    `FnExpr` node itself when `atomic` is true and visit its body as a plain block.
- `RetryExpr`: emit `("retry", None, None, None)`; return a fresh temp (like `ThrowExpr`).
- `_reject_in_detached(keyword, position)` additionally raises E8 when `self._fn_stack[-1].atomic`
  (same call sites: `_gen_return` always; `_gen_break`/`_gen_continue` when `_while_stack` is empty).

### 8.7 `mah/compiler/typecheck.py`

- Remove the `LockExpr`/`LockAcquire`/`LockRelease` cases and imports.
- `AtomicExpr` → `self._check_block(expr.closure.body, hint)` — the body is checked **inline** in the
  current function context (its thrown errors join the current accumulator, exactly as if it were a
  plain block); `expr.closure` is never passed to `_check_fn`.
- `RetryExpr` → `NEVER`.
- W2/W3: `shared_locked` → `shared_atomic`; messages:
  W2 `shared variable 'NAME' is passed as a copy: changes the callee makes to it are lost; to change it, call inside 'atomic { ... }'`;
  W3 `'ITEM' is a copy of an element of shared variable 'NAME': assigning into it changes nothing shared; loop inside 'atomic { ... }'`.
  W1 unchanged.
- `ThreadError`s raised by the VM in transactions are untracked (like M44's).
  `examples/threads.mh` and `mah/std/thread.mh` must have no strict diagnostics.

### 8.8 `mah/bytecode/format.py`, `lower.py`, `disasm.py` (encode/decode only if needed)

§7. `lower.py`: remove the `sharedlock`/`sharedunlock` branches; `if op in ("atomicbegin",
"atomicend", "atomicabort", "retry"): return Instr(op, ())`. `disasm.py`: remove the two
branches; `sharedget` mode names `{0: "copy", 1: "working"}`; the new opcodes need no branch
(empty operand list). `encode.py`/`decode.py` are table driven: change them only if the §12.1
bytecode tests fail.

### 8.9 `mah/std/thread.mh`, `mah/std/prelude.mh`

`thread.mh`: the header comment's sentence becomes "Share state with `shared let` variables changed
inside `atomic { }`, with a Semaphore, or with channels." Nothing else.

`prelude.mh`: the comment above `ThreadError` becomes
```mah
# M44/M45: what threads, shared variables, `atomic` blocks, semaphores and
# channels throw. `kind` is one of "closed", "full", "cancelled", "deadlock",
# "not_sendable", "foreign_promise", "over_release", "stuck", "in_atomic".
```
(both files must still satisfy `format_source(text) == text`).

---

## 9. Part 1 — the Python VM

### 9.1 `mah/runtime_values.py`

- `Task.__slots__`: remove `"held"`, add `"tx"`, `"implicit"` (both `None` in `__init__`); update
  the M44 comment.
- `_TX_SERIALS = itertools.count(1)`; `class Tx` with `__slots__ = ("serial", "owner", "implicit",
  "depth", "rv", "reads", "entries", "attempts", "irrevocable", "restart")`,
  `__init__(self, owner, implicit, restart)`: `serial = next(_TX_SERIALS)`, `depth = 1`, `rv = 0`,
  `reads = {}`, `entries = {}`, `attempts = 0`, `irrevocable = False`.
- `class TxEntry` with `__slots__ = ("name", "base", "working", "assigned", "exposed")`.

### 9.2 `mah/thread_runtime.py`

- Remove `_LockState`, `locks`, `waiting_on`, `acquire`, `release`, `grant_next`, `cycle` (the lock
  one), the lock edge in `_out_edges`, the lock parts of `_remove_waiter`/`forget_vm`,
  `VmThreads.get/set/lock/unlock` (replaced below). `tracking = True` only in `spawn`.
- `AWAIT_DEADLOCK_MESSAGE` = `"deadlock: this await would never end (it waits, through threads, for itself)"`.
- New constants: `ATOMIC_ATTEMPTS = 8`; `ATOMIC_REFUSED_NATIVES` (§5.2);
  `RETRY_NO_READS_MESSAGE = "retry can never wake up: this transaction read no shared variable"`;
  `def in_atomic(name) -> MahThrow` (the §5.1 message).
- `class TxRestart(BaseException)` (`kind`).
- `TX_STATS = {"conflicts": 0, "exclusive": 0}` (module level; debug counters for tests, never
  visible to Mah programs, no Rust counterpart): `restart_tx` adds 1 to `"conflicts"` for a
  conflict, `tx_start` adds 1 to `"exclusive"` after `acquire_excl` returns; both under `self.lock`.
- `def same_copy(a, b) -> bool` (§6.6).
- `ThreadRuntime.__init__`: `shared = {}` (k → (value, version)), `clock = 0`, `watchers = {}`,
  `retry_waits = {}`, `excl_owner = None`, `excl_queue = collections.deque()`,
  `commits_waiting = 0`, `excl_cv = threading.Condition(self.lock)`.
- Public methods (each takes `self.lock` itself): `read_shared(k) -> (value, version)`;
  `write_shared(vt, k, copy)`; `tx_start(vt, tx)`; `tx_commit(vt, tx, publish) -> bool`;
  `end_attempt(tx)`; `retry_wait(vt, reads) -> PromiseInstance | None`. Private (lock held):
  `_acquire_excl(vt, serial)`, `_wait_no_excl(vt, serial)`, `_release_excl(serial)`,
  `_reads_valid(reads)`, `_wake_watchers(keys)`. Algorithms: §6.3, §6.5, §6.9.
- `_remove_waiter(promise)` and `forget_vm(vt)`: §6.5, §6.8.
- `VmThreads`: `wait_check = lambda: None` attribute (set by `_execute_with`, §9.4); methods
  implementing §6.3: `get(task, k, name, mode)`, `set(task, k, name, value)`, `begin(task, pc)`,
  `end(task)`, `abort(task)`, `retry(task)`; the waits dict gains kind `"retry"`.

### 9.3 `mah/thread_natives.py`, `mah/natives.py`

No change expected (change only if needed to make the above work).

### 9.4 `mah/code_interpreter.py`

- Import `ATOMIC_REFUSED_NATIVES`, `TxRestart`, `in_atomic`, `ATOMIC_ATTEMPTS` (module level is fine:
  `thread_runtime` doesn't import `code_interpreter` at module level).
- `_link_instr`: remove `sharedlock`/`sharedunlock`; add the four no-operand opcodes; `native` gets
  `atomic_name`; `_check_mode` only for `sharedget`.
- `_execute_with`:
  - `threads.wait_check = wait_check` where `def wait_check(): if deadline is not None and
    time.monotonic() > deadline: raise _Timeout(); if rt.active: threads.poll()`.
  - `invoke_sync`: §6.2 (`sub_task.tx`, `sub_task.implicit = label`).
  - `restart_tx(task, kind)` (§6.4) as a nested function (it needs `drive`); returns `None`
    (continue) or `("suspended", None)`.
  - Guards (§6.3): `VmThreads.begin`/`end` wrap `tx_start` and `end`'s steps 2–4 in
    `try: ... except TxRestart: raise except BaseException: rt.end_attempt(tx); task.tx = None;
    raise`. Safety net: where `_step_task`/`drive` finishes a task (`("done", ...)` or
    `("failed", ...)`), if `task.tx is not None and task.tx.owner is task`: `rt.end_attempt(task.tx);
    task.tx = None`.
  - `_step_task`: `except TxRestart as signal:` first in the chain: `if task.tx is None or
    task.tx.owner is not task: raise`; `outcome = restart_tx(task, signal.kind)`; `if outcome is not
    None: return outcome`; `continue`.
  - `_exec`: `("sharedget", k, name, mode, dest)` → `threads.get`; `("sharedset", k, name, src)` →
    `threads.set`; `("atomicbegin",)` → `threads.begin(task, task.pc - 1)`; `("atomicend",)`,
    `("atomicabort",)`, `("retry",)` → the matching `threads` methods; `await` and the four `detach*`
    cases start with `if task.tx is not None: raise in_atomic(".await")` / `in_atomic("detach")`;
    `("native", impl, arg_addrs, dest, atomic_name)`: `if atomic_name is not None and task.tx is not
    None: raise in_atomic(atomic_name)` first.
  - `settle_io`: remove the `"lock"` branch (a `"retry"` wait is settled by the existing `"settle"`
    branch).

---

## 10. Part 2 — the Rust VM (parity)

Read §2–§7 and §9 (the Python design is the reference); implement the same observable behaviour.

- **`runtime/src/decode.rs`**: `opcode_info`: remove 0x72/0x73; add `0x74 => ("atomicbegin",
  Some(21))`, `0x75 atomicend`, `0x76 atomicabort`, `0x77 retry`; `RawInstr`: remove `SharedLock`,
  `SharedUnlock`; add `AtomicBegin`, `AtomicEnd`, `AtomicAbort`, `Retry` (no operands). Test
  `decodes_the_shared_variable_opcodes` becomes: code `[7, 0x70, 3, 0, 1, 0, 0, 0x71, 3, 0, 0, 0,
  0x74, 0x75, 0x76, 0x77, 0x00]` decodes to `SharedGet { index: 3, name: 0, mode: 1, dest: (0, 0) }`,
  `SharedSet { index: 3, name: 0, src: (0, 0) }`, `AtomicBegin`, `AtomicEnd`, `AtomicAbort`,
  `Retry`, `Halt`; at minor 20 it fails with `opcode 'sharedget' at instruction 0 requires minor
  version >= 21, but this file's minor version is 20`; and `[2, 0x72, 3, 0, 0, 1, 0x00]` at minor 21
  fails with `unknown opcode 0x72 at instruction 0`.
- **`runtime/src/vm/error.rs`**: `TxSignal`, `RuntimeError.restart` (§6.4).
- **`runtime/src/vm/value.rs`**: `Task`: remove `held`, add `tx: Option<Rc<RefCell<Tx>>>`,
  `implicit: Option<Rc<str>>`; remove `Held`; add `Tx` (§6.2: `serial: u64`, `owner: usize`,
  `implicit: Option<Rc<str>>`, `depth: u32`, `rv: u64`, `reads: Vec<(u64, u64)>`, `entries:
  Vec<TxEntry>`, `attempts: u32`, `irrevocable: bool`, `restart: (usize, FrameRef, usize, usize)`)
  and `TxEntry { index: u64, name: Rc<str>, base: Option<Arc<Payload>>, working: Value, assigned:
  bool, exposed: bool }`; `Continuation.restart: bool` (§6.4).
- **`runtime/src/vm/link.rs`**: `LinkedInstr::SharedGet { index, name, working: bool, dest }`,
  `SharedSet { index, name, src }` (unchanged), `AtomicBegin`, `AtomicEnd`, `AtomicAbort`, `Retry`;
  remove `SharedLock`/`SharedUnlock` and their mode check; `Native` gains `atomic: Option<&'static
  str>` (§6.7).
- **`runtime/src/vm/thread.rs`**: remove `LockState`, `locks`, `waiting_on`, `acquire`, `release`,
  `mark`, `grant_next`, the lock edge in `out_edges`, `cycle` and the lock parts of
  `forget_vm`/`remove_waiter`; `AWAIT_DEADLOCK_MESSAGE` new text; `tracking` set only by `spawn`.
  Add the §6.1 state (`shared: HashMap<u64, (Arc<Payload>, u64)>`, `clock: u64`, `watchers:
  HashMap<u64, BTreeSet<(u64, u64)>>`, `retry_waits: HashMap<(u64, u64), Vec<u64>>`,
  `excl_owner: Option<(u64, u64)>`, `excl_queue: VecDeque<u64>`, `commits_waiting: usize` in
  `RtState`; `excl_cv: Condvar` in `ThreadRuntime`), `ATOMIC_ATTEMPTS`, `ATOMIC_REFUSED_NATIVES`,
  `RETRY_NO_READS_MESSAGE`, `same_copy(a: &Payload, b: &Payload) -> bool` (§6.6 over the two
  graphs), `ThreadRuntime::{read_shared, write_shared, tx_start, tx_commit, end_attempt,
  register_retry}` (`register_retry(vm_id, pid, reads) -> bool`: false = something changed already)
  and the opcode functions `get(vm, task, k, name, working)`, `set(vm, task, k, name, v)`,
  `begin(vm, task, pc)`, `end(vm, task)`, `abort(vm, task)`, `retry(vm, task)`, `in_atomic<T>(name)
  -> RResult<T>`. Never hold a `RefCell` borrow across a condvar wait. Unit tests: the wait-for-graph
  tests of §12.1 (`WaitForGraphTests`) as `#[test]`s, and `AtomicRuntimeTests` 1–3 and 6 against
  `ThreadRuntime` with a registered VM channel (check what was posted with `try_recv`; for 6, a
  `Tx` with `attempts = ATOMIC_ATTEMPTS` and depth 0 through `begin`).
- **`runtime/src/vm/exec.rs`**: `Completion::Lock` and `Wait::Lock` removed; `Wait::Retry` added;
  `invoke_sync` per §6.2; the §6.3 guards in `thread.rs`'s `begin`/`end` (`match` on the `Err`;
  clean up unless `e.restart.is_some()`) and the end-of-task safety net in `exec.rs` where a task
  finishes; `step_task_inner` handles `e.restart` first (§6.4) through
  `restart_tx(&mut self, task, sig) -> RResult<Option<StepControl>>`; `resolve_promise` handles
  `restart` continuations; the `Await`, `Detach*` and `Native` arms check `in_atomic` (§6.7); the
  shared/atomic arms call the `thread.rs` functions; `settle_io`'s lock branch removed.
- **`runtime/src/vm/natives.rs`, `mod.rs`**: only what the above needs to compile.
- **`runtime/tests/vm_diff.py`**: §12.2.

---

## 11. Part 3 — tooling and docs

### 11.1 `syntax-highlight/grammar.js` (+ regenerated `src/`, `queries/mah/highlights.scm`)

- Remove `lock_expr` and `_lock_target` (and `$.lock_expr` from `expr`'s choice).
- Add `atomic_expr: ($) => seq("atomic", field("body", $.block))` in `expr`'s choice where
  `lock_expr` was, with a comment like M44's for `shared`: `atomic` is contextual in the real parser;
  here it is a keyword token, which only matters where an identifier named `atomic` starts an
  expression.
- No grammar rule for `retry` (an identifier). `highlights.scm`: replace `"lock" @keyword` with
  `"atomic" @keyword`; add, **at the end of the file next to the `test_block` capture** (the file is
  last-pattern-wins: a capture placed before the generic identifier captures would lose to
  `@variable`), with a comment like the M28 one:
  `((expr_stmt (expr (identifier) @keyword)) (#eq? @keyword "retry"))`.
  Only statement-position `retry` (alone as a statement, including a block's last statement:
  `if c { retry }`) is highlighted; `retry` as an argument, list item or field value is not (those
  are E6b-or-variable cases anyway). tree-sitter's `match_arm` body is always a `block`, so
  `none => { retry }` is covered and no extra pattern is needed; this limitation is documented in a
  comment next to the capture.
- Regenerate (`cd syntax-highlight && pnpm install && pnpm exec tree-sitter generate`), commit `src/`.
  `tree-sitter parse ../examples/threads.mh` shows no `ERROR`; `let atomic = 1` and
  `let retry = 2` parse as lets.

### 11.2 `editors/vscode/syntaxes/mah.tmLanguage.json`

Replace the `lock` pattern with `\batomic(?=\s*\{)` and add `\bretry\b(?=\s*(;|\}|$))`, both scoped
like `shared`; update the comment.

### 11.3 `mah/lsp/analysis.py` (+ `tests/test_lsp_threads.py`)

- `KEYWORD_DOCS`: remove `"lock"`; rewrite `"shared"` with §11.7's wording (it must contain
  `atomic { ... }`); add `"atomic"` (§11.7 wording + example
  ```mah
  atomic { hits = hits + 1 }
  let next = atomic {
      hits = hits + 1
      hits
  }
  ```
  ) and `"retry"` (§11.7 wording + the queue example of §11.7).
- `_is_thread_contextual_keyword(token, tokens, text)`: `shared` before `let` on the same line
  (unchanged); `atomic` when the next token is `{` on the same line and the token after it is
  neither `}` nor an `ID` followed by `:` (and not in a condition head, as §8.1); `retry` when it
  is inside an atomic body (the brace-flag stack of §8.1, over the LSP's tokens) and the next token
  is `}`, `;`, `,`, `)`, `]` or EOF, or on a later line. Hover follows the compiler's reading
  exactly: such a `retry` hovers as `**keyword** \`retry\`` **whether or not** a variable `retry`
  is in scope (the compiler reads it as the keyword and reports E6b, which the diagnostics show); the
  old "`_symbol_at_position(...) is None`" condition is dropped. A `retry` not in that position
  hovers as the variable it resolves to.
- Completion offers `shared`, `atomic`, `retry` as keywords (where `lock` was offered).
- Tests (replace the lock ones): hover on `atomic` in `shared let n = 0\natomic { n = n + 1 }\nprint(n)\n`
  → starts with `**keyword** \`atomic\``; hover on `retry` in
  `shared let n = 0\natomic {\n    if n == 0 { retry }\n}\n` → `**keyword** \`retry\``; hover on the
  second `atomic` in `let atomic = 2\nprint(atomic)\n` and on the second `retry` in
  `let retry = 2\nprint(retry)\n` → `**variable**`; in `let retry = 1\natomic {\n    let y = retry\n}\n`
  hover on the second `retry` → `**keyword** \`retry\`` and the diagnostics contain E6b's message;
  document symbols of `shared let n = 0\nfn f() { atomic { n = n + 1 } }\n` list `n` and `f` only
  (no anonymous function for the `atomic`); `shared`'s hover contains `atomic { ... }`;
  rename of `n` in the first source → `shared let hits = 0\natomic { hits = hits + 1 }\nprint(hits)\n`;
  completion offers `shared`, `atomic`, `retry`; `shared let xs = []\nxs.push(1)\n` shows the new
  E1 message at `xs.push`; `let total = 0\natomic { total = total + 1 }\n` shows E9;
  `shared let n = 0\nfn bump() { atomic { n = n + 1 } }\nbump()\nprint(n)\n` has no errors; the
  detach W1 test stays.

### 11.4 `mah/format/formatter.py` (+ `tests/test_format.py`)

No formatter change is expected; change it only if a golden below fails. Goldens (replace the lock
ones): `atomic{n=n+1}` → `atomic { n = n + 1 }`; `let v = atomic { n = n + 1; n }` →
`let v = atomic {\n    n = n + 1;\n    n\n}`; `fn f() {\natomic {\nxs.push(1)\nif xs.len()==0{retry}\n}\n}`
→ `fn f() {\n    atomic {\n        xs.push(1)\n        if xs.len() == 0 { retry }\n    }\n}`;
`let atomic=1\nlet retry=2\nprint(atomic+retry)` → `let atomic = 1\nlet retry = 2\nprint(atomic + retry)`.
(Each golden also asserts that formatting the expected text is a no-op, like the existing
`ThreadSyntaxTests.check`.)

### 11.5 `www/src/lib/markdown/highlight-mah.ts`

The contextual keyword list: `'shared'`, `'atomic'`, `'retry'` (remove `'lock'`).

### 11.6 Docs and www (describe the behaviour defined here, not Part 1's implementation)

| file | change |
|---|---|
| `docs/contracts/M44_threads.md` | at the very top (after the title), the note: "> **Superseded in part by M45.** The `lock` construct, its opcodes `sharedlock`/`sharedunlock`, lock deadlock detection and the lock rules of §5 were replaced, before 0.4.0 shipped, by M45's `atomic { }` blocks (software transactional memory): see `docs/contracts/M45_atomic.md`. The M44 state is preserved on the git branch `m44-lock-block`. Everything else here (threads, jobs, copies, `shared let`, semaphores, channels, quiescence) still holds." Nothing else in that file changes. |
| `docs/MAHC_FORMAT.md` | §3's minor rule names `sharedget`/`sharedset`/`atomicbegin`/`atomicend`/`atomicabort`/`retry`; §4.4's native table notes which natives are refused in a transaction (or points to §6.11's list); §4.6: remove the 0x72/0x73 rows, add 0x74–0x77, mode names copy/working; §6.4 "pending" includes retry waits, and its job-lifetime rule (a job ends when its root has finished and nothing is pending) lists `retry` waits among the pending work — internal waits that keep the job and the program alive until woken or ended by quiescence (`stuck`), replacing "lock waits"; **§6.11** rewritten for transactions: §3, §5, §6 of this contract condensed but normative (store and versions, Tx fields, every opcode's algorithm including the local copy `sharedset` makes in a transaction, the §6.3 guards and end-of-task safety net, restart, retry waits and that they keep a job alive, `same_copy` with internal type identity, exclusivity, the refused natives, the `ThreadError` table); remove the lock and lock-deadlock text; §7's 1.21 entry describes the final 1.21 (no mention of lock except "0x72/0x73 are unassigned"). |
| `docs/STDLIB.md` | "Phase 5: threads": `atomic`/`retry` replace `lock`; the ThreadError table of §5.1; the "acts on a copy" and "not undone" lists; the torn-reads and "wait with `retry`, not a loop" notes; the job-lifetime rule with `retry` waits among the pending work (replacing lock waits); deadlocks paragraph (§11.7). |
| `docs/V2_DESIGN.md` | new entry **M45 — `atomic` blocks replace `lock`. ✅ Landed.** after M44b: the decisions of §1, the redefined 1.21, files, tests, §15's judgment calls, a pointer to this contract and to the branch `m44-lock-block`; the M44a entry gets one line "(`lock` was replaced by M45's `atomic` before release)"; Status: "... M43, M44a/M44b and M45 ..." and the threads sentence says `shared`/`atomic`. |
| `docs/NEXT_PHASES.md` | the threads item mentions M45; §16's deferred items listed as follow-ups. |
| `docs/ERRORS.md` | "Threads" section: kinds per §5.1 (`in_atomic`, the new `stuck` message, the changed `deadlock` text, no lock deadlock); "a throw out of `atomic` publishes nothing; a commit that would store a Promise publishes nothing and throws `not_sendable`" (replacing the failed-write-back paragraph). |
| `docs/RUST_VM.md` | Threads section: "the shared-variable store and its versions, the exclusivity token" instead of "its locks"; transactions, `TxSignal` restarts. |
| `docs/FORMAT.md` | the `lock` row becomes `atomic { }` (`atomic { n = n + 1 }`), `retry` formats as a plain word. |
| `mah/project/templates/docs/mah-language.md` | the Threads section: `atomic`/`retry` replace `lock` (compiling examples: counter, bank transfer, a `retry` queue); the "acts on a copy" and "not undone" lists; the "reading several variables" and "waiting" notes and the `retry`-variable clash (E6b) (§11.7); "Not available": replace the lock items by "locks and mutexes (use `atomic { }`), `or_else`, I/O or waiting inside `atomic`, killing a running job, `select` over channels". |
| `mah/project/templates/AGENTS.md` | "`shared let` variables, `atomic { }` blocks, semaphores and channels". |
| `www/src/content/std/thread.md` | "Shared variables and `lock`" → "Shared variables and `atomic`" (+ `retry`, nesting, exclusive mode, refused natives); "Deadlocks" → "Waits that can never end" (join cycles → `deadlock`, quiescence and hopeless `retry` → `stuck`); the job-lifetime sentence lists `retry` waits among what keeps a job alive (instead of lock waits); the torn-reads and "wait with `retry`" notes (§11.7); Errors table per §5.1. |
| `www/src/content/docs/async.md`, `standard-library.md`, `errors.md` | `lock` → `atomic` (the standard-library example becomes `atomic { total = total + n }`; errors.md's kinds per §5.1, the write-back sentence replaced as in ERRORS.md). |
| `www/src/content/blog/v0-4-0-threads.md` | front-matter description: "shared let variables and atomic blocks share state safely" (no "lock"); intro paragraph: `atomic` block instead of `lock`; "## Shared variables and `lock`" → "## Shared variables and `atomic`" (rewritten: transactions, rerun, nesting, `retry` with the queue example, exclusive mode after 8 failed runs, no side effects); "## Deadlocks are errors, not hangs" → "## Waits that can never end are errors" (join cycles, quiescence, hopeless `retry`); version stays 0.4.0; no compatibility note about `lock` (it never shipped). |

`tests/test_project.py` must stay green (every `mah` block in the template reference compiles; the
reference must mention `atomic` and `retry`).

### 11.7 Wording to reuse in docs and LSP

- **shared**: "`shared let NAME = value` (top level only) declares a variable every thread shares.
  Reading it gives a copy and never waits; assigning it (`NAME = value`) is atomic; to change it in
  place (`push`, `x[k] = v`) or to read and write it together, do it inside `atomic { ... }`."
- **atomic**: "`atomic { body }` runs `body` as one transaction: it reads a consistent snapshot of
  the shared variables, and its changes are published all at once when it ends. If another thread
  changed what it read in the meantime, it runs again from the start, so the body can't do I/O,
  wait or start tasks (ThreadError `in_atomic`). A throw out of it publishes nothing. Its value is
  the body's value; an `atomic` inside another joins it."
- **retry**: "`retry` (only inside `atomic { }`) gives up this run of the transaction and waits
  until a shared variable it read changes, then runs it again — the way to wait for a condition."
  Example:
  ```mah
  shared let queue = []
  fn take() {
      atomic {
          if queue.len() == 0 { retry }
          queue.pop_start()
      }
  }
  ```
- **Not undone when the block runs again**: anything that isn't a shared variable — assigning a
  variable declared outside the block (a compile error), changing an object reached from one
  (`outer.push(x)`, not detected), assigning an outer variable from a function or closure the
  block calls (not detected), random draws; reading `ch.len()`/`s.available()` isn't part of
  the snapshot.
- **Exclusive mode**: "a transaction that had to run again 8 times runs its next attempt alone:
  while it does, every other thread's commits and shared assignments wait for it."
- **Reading several variables**: "reads outside `atomic` happen one variable at a time; to read
  several shared variables consistently, read them together: `let [a, b] = atomic { [x, y] }`."
- **Waiting**: "inside `atomic`, wait for a condition with `retry`, not a loop: the body reads a
  snapshot, so `atomic { while !ready { } }` never sees `ready` change (and, if the transaction is
  running exclusively, it holds up every other thread's commits)."
- **Job lifetime**: a job (and the program) stays alive while one of its tasks waits in `retry`;
  the wait ends when a variable it read changes, or with `stuck` when nothing can change it.
- **Acts on a copy / read point and cost / random numbers in jobs / semaphore permits /
  unobserved failures**: M44 §11.5's bullets, with `lock` replaced by `atomic`.
- **Waits that can never end** fail with `ThreadError` `stuck` once every thread is waiting
  (including a `retry` nobody can wake); `.await`/`join` cycles through threads fail with
  `deadlock`; a wait that only *another running thread* could end keeps waiting.

---

## 12. Tests

Outputs are exact (stdout). All are deterministic: the main VM prints, jobs print only where noted,
and every cross-thread order is forced by awaits, channels, `retry` or serializability.

### 12.1 Part 1 (Python side; `make test` green)

**`tests/test_parser.py`** — in `ThreadSyntaxTests`, replace `test_lock`/`test_lock_as_a_name` with:
- `atomic { 1 }` → `AtomicExpr` whose `closure` is a `FnExpr` with `atomic=True`, `params == []`,
  body tail `NumberLit(1)`.
- `let atomic = 2\nprint(atomic)` → `LetStmt`, `PrintStmt`; `atomic + 1` → `Binary` over
  `Ident('atomic')`; `atomic { x: 1 }` and `atomic { }` → `StructLit` with `type_name == "atomic"`;
  `if atomic { 1 }` → `IfStmt` whose condition is `Ident('atomic')`; `"atomic\n{ x: 1 }"` →
  `StructLit(type_name='atomic')` (today's struct-literal-across-lines behaviour, unchanged);
  `"let atomic = 1\natomic\n{ 1 }"` raises the same `SyntaxError` as today (`Invalid syntax`).
- `atomic { if c { retry } }` → the `if` block's tail is a `RetryExpr`; `atomic {\n    retry\n    1\n}`
  → body stmts `[ExprStmt(RetryExpr)]`, tail `NumberLit(1)` (no `;` needed); `atomic { retry + 1 }`
  → tail `Binary` over `Ident('retry')`; `atomic { let f = fn() { retry } }` → the inner tail is
  `Ident('retry')`; `fn g() { retry }` → `Ident('retry')`.
- `atomic { xs }.len()` → `MethodCall` on an `AtomicExpr`.
- Depth resets in every function-body path: `atomic { let f = fn() {\n    retry\n} }` → the
  lambda's body tail is `Ident('retry')`; `struct S { n: Number }\nimpl S {\n    fn m(self) {\n        retry\n    }\n}`
  and `test "t" {\n    retry\n}` → `Ident('retry')` (outside any atomic; a regression guard that
  `_parse_method_decl`/`_parse_test_decl` don't inherit a stale depth: each is parsed right after
  a top-level `atomic { 1 }` statement in the same source).

**`tests/test_threads.py`** — update the module docstring (`atomic` instead of `lock`). Keep T1–T6,
T11, T12, T15, T16–T18, T24, T25, T26 and `QuiescenceWindowTests` unchanged. Remove T9, T21, T22, T23,
T27, T28 and the lock wait-for-graph tests. Change/add (every program via `run_source` unless noted):

T7 (`test_t7_shared_variables_and_atomic`):
```mah
import thread from "std:thread"
shared let n = 0
shared let xs = []
fn bump() {
    atomic {
        n = n + 1
        n
    }
}
print(atomic {
    bump()
    bump()
})
print(n)
atomic { xs.push("a") }
let mine = xs
atomic { xs.push("b") }
print(xs, mine)
let t = thread.spawn()
print(detach(t) {
    atomic { xs.push("c") }
    atomic { xs.len() }
}.await)
print(xs)
xs = ["reset"]
print(t.run(fn() { xs }).await)
shared let slot = none
try { slot = [detach { 1 }] } catch {
    e: ThreadError => { print(e.kind, e.message, slot) }
}
let p2 = detach { 2 }
try { atomic { slot = [p2] } } catch {
    e: ThreadError => { print(e.kind, e.message, slot) }
}
t.join()
```
→ `2\n2\n[a, b] [a]\n3\n[a, b, c]\n[reset]\nnot_sendable shared variable 'slot' can't hold a Promise none\nnot_sendable shared variable 'slot' can't hold a Promise none\n`

T8 (`test_t8_pool_and_shared_counters`; the lost-update test): M44's T8 with
`lock total { total = total + 1 }` → `atomic { total = total + 1 }` and `lock log { log.push(n) }` →
`atomic { log.push(n) }` → `800 8 28 56\n`.

T10 (semaphores): M44's T10 with the two lock blocks replaced by
`atomic {\n        inside = inside + 1\n        if inside > most { most = inside }\n    }` and
`atomic { inside = inside - 1 }` → unchanged output.

T13 (modules): `lib.mh` = `export shared let hits = 0\nexport fn hit() {\n    atomic { hits = hits + 1 }\n}\n`;
`main.mh` = M44's with the last-but-one line `atomic { lib.hits = lib.hits + 1 }` → `3\n`.

T13b (`test_t13b_modules_keep_contextual_keywords`; `run_file` on temp dirs like T13), three
programs:
- `lib.mh` = `struct atomic { v: Number }\nexport fn retry(n) { n + 1 }\nexport shared let k = 0\nexport fn both() {\n    let s = atomic { v: 1 }\n    atomic {\n        k = retry(k)\n        k + s.v\n    }\n}\n`;
  `main.mh` = `import lib from "lib.mh"\nprint(lib.both(), lib.both(), lib.k)\n` → `2 3 2\n` (the
  module's `atomic {` is the keyword although the module declares `struct atomic`; `atomic { v: 1 }`
  stays its struct literal; `retry(k)` calls the function).
- `lib.mh` = `export shared let k = 0\nexport fn wait_k() {\n    atomic {\n        if k == 0 { retry }\n        k\n    }\n}\n`;
  `main.mh` = `import lib from "lib.mh"\nlet p = detach { lib.wait_k() }\nlib.k = 4\nprint(p.await)\n`
  → `4\n` (`retry` in a module really retries).
- `lib.mh` = `export fn retry(n) { n }\nexport shared let k = 0\nexport fn f() {\n    atomic {\n        if k == 0 { retry }\n        k\n    }\n}\n`
  → compiling `main.mh` = `import "lib.mh"\nprint(f())\n` fails with E6b's message (the keyword
  is not rewritten into a reference to `lib`'s `retry`, and the clash is reported as it would be in
  the entry file).

T14 (`test_t14_contextual_words_stay_identifiers`; no import), four programs:
- `let shared = [1]\nlet atomic = 2\nlet retry = 3\nfn f(atomic) { atomic + 1 }\nfn g() {\n    let retry = 5\n    retry\n}\nprint(shared, atomic, retry, f(atomic), g())` → `[1] 2 3 3 5\n`
- `struct atomic { v: Number }\nlet a = atomic { v: 1 }\nprint(a.v)` → `1\n`
- `let atomic = true\nif atomic { print("cond") }` → `cond\n`
- `shared let n = 1\nlet retry = 1\nprint(atomic { retry + n })` → `2\n`
- and `print(detach (1 + 2).await)` → `3\n` (kept).

T19 (`test_t19_implicit_calls_share_their_callers_transaction`; no import):
```mah
shared let n = 0
struct P { x: Number }
impl Printable for P {
    fn to_string(self) {
        atomic { n = n + 1 }
        "P(" + self.x + ", n=" + n + ")"
    }
}
let s = atomic {
    n = 10
    "" + P { x: 1 }
}
print(s, n)
print(P { x: 2 })
```
→ `P(1, n=11) 11\nP(2, n=12)\n`

T20 (`test_t20_only_lexical_reads_alias`; no import):
```mah
shared let xs = [1]
fn count_with(v) {
    let s = xs
    s.push(v)
    s.len()
}
print(count_with(2), xs)
print(atomic {
    xs.push(5)
    [count_with(9), xs.len()]
}, xs)
```
→ `2 [1]\n[3, 2] [1, 5]\n`

A1 (`test_a1_nested_atomic_composes`; no import):
```mah
shared let a = 0
shared let b = 0
fn move(n) {
    atomic {
        a = a - n
        b = b + n
    }
}
fn move_twice(n) {
    atomic {
        move(n)
        move(n)
        [a, b]
    }
}
print(move_twice(5), a, b)
let r = atomic {
    a = 100
    try {
        atomic {
            b = 100
            throw RuntimeError.ArgumentError { message: "inner" }
        }
    } catch {
        e => { "caught " + e.message() }
    }
}
print(r, a, b)
```
→ `[-10, 10] -10 10\ncaught inner 100 100\n`

A2 (`test_a2_a_throw_publishes_nothing`; no import):
```mah
shared let xs = [1]
shared let n = 0
try {
    atomic {
        xs.push(2)
        n = 5
        throw RuntimeError.ArgumentError { message: "stop" }
    }
} catch {
    e => { print("caught", e.message()) }
}
print(xs, n)
shared let slot = []
let p = detach { 1 }
try {
    atomic {
        n = 7
        slot.push(p)
    }
} catch {
    e: ThreadError => { print(e.kind, "|", e.message) }
}
print(slot, n)
```
→ `caught stop\n[1] 0\nnot_sendable | shared variable 'slot' can't hold a Promise\n[] 0\n`

A3 (`test_a3_retry_waits_for_a_change`; no import):
```mah
shared let box = ""
let waiter = detach {
    atomic {
        if box == "" { retry }
        box
    }
}
box = "filled"
print(waiter.await)
```
→ `filled\n`

A4 (`test_a4_retry_as_a_blocking_queue`):
```mah
import thread from "std:thread"
shared let queue = []
fn take() {
    atomic {
        if queue.len() == 0 { retry }
        queue.pop_start()
    }
}
fn put(v) {
    atomic { queue.push(v) }
}
let t = thread.spawn(name: "consumer")
let got = t.run(fn() {
    let out = []
    for let i in 0..5 { out.push(take()) }
    out
})
for let i in 1..=5 { put(i * 10) }
print(got.await)
t.join()
```
→ `[10, 20, 30, 40, 50]\n`

A5 (`test_a5_hopeless_retry_is_stuck`; no import):
```mah
shared let flag = false
try {
    atomic {
        if !flag { retry }
        1
    }
} catch {
    e: ThreadError => { print(e.kind, "|", e.message) }
}
try { atomic { retry } } catch {
    e: ThreadError => { print(e.kind, "|", e.message) }
}
print("end")
```
→ `stuck | the wait can never finish: every thread is waiting\nstuck | retry can never wake up: this transaction read no shared variable\nend\n`

A6 (`test_a6_side_effects_throw_in_atomic`):
```mah
import thread from "std:thread"
fn say(s) { print(s) }
fn nap() { sleep_async(1) }
fn wait_for(p) { p.await }
fn spawn_one() { detach { 1 } }
let ch = thread.channel()
fn post(v) { ch.send(v) }
let done = detach { 1 }
let tries = [fn() { say("hi") }, fn() { nap() }, fn() { wait_for(done) }, fn() { spawn_one() }, fn() { post(1) }]
for let f in tries {
    try {
        atomic { f() }
    } catch {
        e: ThreadError => { print(e.kind, "|", e.message) }
    }
}
print(ch.len())
```
→ five lines `in_atomic | 'X' can't run inside 'atomic { }': its body may run more than once` with X =
`io.write`, `time.sleep_async`, `.await`, `detach`, `thread.channel_send` in that order, then `0\n`.

A8 (`test_a8_bank_transfers_keep_the_total`):
```mah
import thread from "std:thread"
shared let accounts = [100, 100, 100, 100]
fn transfer(from, to, amount) {
    atomic {
        accounts[from] = accounts[from] - amount
        accounts[to] = accounts[to] + amount
    }
}
fn worker(seed) {
    let bad = 0
    for let i in 0..200 {
        transfer((seed + i) % 4, (seed + i * 3 + 1) % 4, 1 + i % 7)
        let total = atomic {
            let s = 0
            for let a in accounts { s = s + a }
            s
        }
        if total != 400 { bad = bad + 1 }
    }
    bad
}
let pool = thread.spawn(workers: 4)
let jobs = []
for let s in 0..4 { jobs.push(pool.run(worker, s)) }
let bad = 0
for let j in jobs { bad = bad + j.await }
let final = accounts
let sum = 0
for let a in final { sum = sum + a }
print(bad, sum, final.len())
pool.join()
```
→ `0 400 4\n`

A9 (`test_a9_exclusive_mode_ends_starvation`):
```mah
import thread from "std:thread"
shared let hot = 0
shared let stop = false
fn hammer() {
    let n = 0
    while !stop {
        atomic { hot = hot + 1 }
        n = n + 1
    }
    n
}
fn slow() {
    while hot < 50 { }
    atomic {
        let seen = hot
        let s = 0
        for let i in 0..20000 { s = s + i }
        hot = seen + 1000000
        s
    }
}
let pool = thread.spawn(name: "hammers", workers: 3)
let hs = []
for let i in 0..3 { hs.push(pool.run(hammer)) }
let t = thread.spawn(name: "slow")
let s = t.run(slow).await
stop = true
let total = 0
for let h in hs { total = total + h.await }
print(s, hot - total)
pool.join()
t.join()
```
→ `199990000 1000000\n` (terminates; the slow transaction conflicts with the hammers until its
exclusive attempt). On the Python VM the test also asserts `TX_STATS["exclusive"] >= 1` (reset
before the run): slow's body reads `hot` first and then spins far longer than the GIL switch
interval while three hammers commit to `hot`. The deterministic check of the exclusive path is
`AtomicRuntimeTests` 6; A9 is the end-to-end one.

A10 (`test_a10_a_join_cycle_is_a_deadlock`):
```mah
import thread from "std:thread"
let t1 = thread.spawn(name: "a")
let t2 = thread.spawn(name: "b")
let go = thread.channel()
fn join_other(other) {
    go.recv()
    try {
        other.join()
        "joined"
    } catch {
        e: ThreadError => { e.message }
    }
}
let pa = t1.run(join_other, t2)
let pb = t2.run(join_other, t1)
go.send(1)
go.send(2)
let ra = pa.await
let rb = pb.await
let msg = "deadlock: this await would never end (it waits, through threads, for itself)"
print(ra == "joined" | rb == "joined", ra == msg | rb == msg)
```
→ `true true\n` (whichever join comes second detects the cycle).

A11 (`test_a11_a_failed_job_drops_its_retry_waits`):
```mah
import thread from "std:thread"
shared let k = 0
let t = thread.spawn()
let p = detach(t) {
    detach {
        atomic {
            if k == 0 { retry }
            k
        }
    }
    sleep_async(10)
    throw RuntimeError.ArgumentError { message: "boom" }
}
try { p.await } catch {
    e => { print("failed") }
}
k = 1
print(k)
t.join()
```
→ `failed\n1\n`

A12 (`test_a12_assigning_a_working_value_copies`; no import):
```mah
shared let xs = [1]
shared let ys = []
print(atomic {
    ys = xs
    ys.push(2)
    xs.len()
}, xs, ys)
let v = [1]
atomic {
    xs = v
    xs.push(3)
}
print(v, xs)
```
→ `1 [1] [1, 2]\n[1] [1, 3]\n`

A13 (`test_a13_a_rerun_never_changes_what_it_assigned_from`):
```mah
import thread from "std:thread"
shared let xs = [0]
shared let ys = []
shared let stop = false
fn hammer() {
    let n = 0
    while !stop {
        n = n + 1
        xs = [n]
    }
    n
}
let t = thread.spawn(name: "hammer")
let h = t.run(hammer)
while xs[0] == 0 { }
let v = [1]
let r = atomic {
    let seen = xs
    ys = v
    ys.push(0)
    let i = 0
    while i < 20000 { i = i + 1 }
    seen.len()
}
stop = true
h.await
print(r, v, ys)
t.join()
```
→ `1 [1] [1, 0]\n`. On the Python VM the test also asserts `TX_STATS["conflicts"] >= 1` (reset to
zeros before the run; §9.2): the body's 20000-iteration loop outlasts the GIL's 5 ms switch
interval, so the hammer writes `xs` during the attempt and the attempt reruns (at worst it goes
exclusive after 8 conflicts and commits).

**Compile errors** (class `SharedCompileErrorTests`; each asserts the exception text contains the §4
message with the right name, without the position): E1 `shared let v = []\nv.push(1)`;
`shared let v = [[1]]\nv[0].push(2)`; `shared let c = none\nc.send(1)`; handle variables (M44's test,
messages updated); E2 `shared let v = [1]\nv[0] = 2` and the struct field case; E3
`shared let n = 0\nn = n + 1` and `shared let n = 0\nn = (fn() { n })()`; E1 inside a nested fn in
atomic `shared let v = []\natomic { let f = fn() { v.push(1) } }`; `shared let v = []\natomic { defer v.push(1) }`
compiles; E4 `let p = detach { 1 }\natomic { p.await }` (`'.await'`), `atomic { sleep_async(1) }`
(`'sleep_async'`), `atomic { print(1) }` (`'print'`), `atomic { detach { 1 } }` (`'detach'`),
`let t = 1\natomic { detach(t) { 1 } }` (`'detach'`), `atomic { input("? ") }` (`'input'`);
`atomic { let f = fn(p) { p.await } }` compiles; `fn input(x) { x }\natomic { input(1) }` compiles
(a user function, not the builtin);
E5 unchanged; E6 `retry`, `fn f() {\n    retry\n}`, `atomic { let f = fn() { retry } }`; `let retry =
1\nprint(retry)` compiles; E6b `let retry = 1\natomic {\n    let y = retry\n}`,
`fn f(retry) {\n    atomic { g(retry) }\n}\nfn g(x) { x }`, `fn retry() { 1 }\natomic { [retry] }`;
`let retry = 1\nshared let n = 0\natomic { n = retry + n }` compiles (not keyword position); E7 unchanged; E8 `fn f() {\n    atomic { return 1 }\n}` (`'return'`),
`while true {\n    atomic { break }\n}` (`'break'`), `for let i in 0..3 {\n    atomic { continue }\n}`
(`'continue'`); `atomic {\n    for let i in 0..3 { break }\n}` compiles; E9
`let count = 0\natomic { count = count + 1 }` (`'count'`), `let v = [1]\natomic { v[0] = 2 }` (`'v'`),
`fn f(x) {\n    atomic { x = 1 }\n}` (`'x'`), `struct S { n: Number }\nimpl S {\n    fn bump(self) {\n        atomic { self.n = 1 }\n    }\n}`
(`'self'`); these compile: `atomic {\n    let c = 0\n    c = c + 1\n    c\n}`,
`atomic {\n    let c = 0\n    atomic { c = 1 }\n    c\n}`, `let g = 0\natomic { let f = fn() { g = 1 } }`;
`return` inside `detach(t) { }` keeps its existing error.

**`WaitForGraphTests`** (Python only; Part 2 mirrors them): with `ThreadRuntime(None)` and pools set
by hand:
- join cycle: pool 1 running job 7 (root 40), pool 2 running job 8 (root 50), `awaiting[40] = ("join",
  2)` → `cycle_from(producer_edges(("join", 1)), 50, True)` is true;
- a job not started yet: `awaiting[30] = ("job", 6)`, `job_roots[6] = 10` → for `("job", 5)`:
  false; after `job_roots[5] = 30`: `cycle_from(producer_edges(("job", 5)), 10, True)` is true;
- plain await cycle not reported (kept).

**`AtomicRuntimeTests`** (Python only; Part 2 mirrors 1–3): a `ThreadRuntime(None)`, a main
`VmThreads(rt, 0, _Io(), None)` registered as `rt.vms[0]` and a second `vt2 = VmThreads(rt, 1,
_Io(), None)` registered as `rt.vms[1]` (the `_Io` fake of `QuiescenceWindowTests`),
`Tx(None, None, None)` records; every helper is called with the signatures of §9.2
(`write_shared(vt, k, copy)`, `tx_start(vt, tx)`, `tx_commit(vt, tx, publish)`,
`end_attempt(tx)`, `retry_wait(vt, reads)`); positive "the thread finished" checks use
`thread.join(timeout=5)` then `assertFalse(thread.is_alive())`:
1. *validation*: `write_shared(vt, 0, Decimal(1))`; `tx_start` → `tx.rv == 1`; `tx.reads[0] = 1`;
   `write_shared(vt, 0, Decimal(2))`; `tx_commit(vt, tx, [(0, Decimal(5))])` is false and
   `read_shared(0) == (Decimal(2), 2)`; a fresh tx with `reads[0] = 2` commits and
   `read_shared(0) == (Decimal(5), 3)`.
2. *read-only*: a tx with `reads[0] = 1` after `0` changed: `tx_commit(vt, tx, [])` is true and the
   clock is unchanged.
3. *retry wake-ups*: keys 0 and 1 written once each; `p = retry_wait(vt, {0: version of 0})` is a
   pending Promise; `write_shared(vt, 1, ...)` posts nothing (`vt.done` empty); `write_shared(vt, 0,
   ...)` posts `(p, "settle", (True, NONE_VALUE))`; `retry_wait` with an outdated version returns
   `None`.
4. *exclusivity blocks writers*: an irrevocable tx started by `tx_start` (holds the token); a Python
   thread calls `write_shared(vt2, 0, Decimal(9))`; after 0.2 s the stored value is unchanged and the
   thread is alive; `end_attempt(tx)`; the thread finishes (join timeout 5 s) and the value is 9.
5. *one exclusive at a time*: tx1 irrevocable started; a thread runs `tx_start(vt2, tx2)` with tx2
   irrevocable; after 0.2 s it is still waiting and `rt.excl_owner[0] == tx1.serial`;
   `end_attempt(tx1)` → the thread returns (join timeout 5 s) and `rt.excl_owner[0] == tx2.serial`.
6. *the 9th attempt is exclusive (deterministic)*: a `Task` `task` with `task.tx = tx`, `tx.owner =
   task`, `tx.depth = 0`, `tx.attempts = ATOMIC_ATTEMPTS`; `vt.begin(task, 0)` → `tx.irrevocable`
   is true, `tx.depth == 1` and `rt.excl_owner == (tx.serial, 0)`; then `vt.end(task)` with an
   empty entry set → `rt.excl_owner is None` and `task.tx is None`. With `tx.attempts =
   ATOMIC_ATTEMPTS - 1` instead, `begin` leaves `irrevocable` false and `excl_owner` none. Also with
   `tx.implicit = "to_string"` and `attempts = ATOMIC_ATTEMPTS`: exclusive too (§3.3).
7. *guards release on error*: `vt2` holds the token (an irrevocable tx2 started); a task whose tx
   (depth 0, `attempts = ATOMIC_ATTEMPTS`) is begun by `vt.begin` (same thread), with
   `vt.wait_check` set to raise `_Timeout` → the call re-raises `_Timeout`, `task.tx is None`,
   `rt.excl_queue` is empty, and after `end_attempt(tx2)` `rt.excl_owner is None` (no leaked queue
   entry or token).

**`RefusedNativesParityTests`** (Python only): `len(ATOMIC_REFUSED_NATIVES) == 53`;
`ATOMIC_REFUSED_NATIVES <= set(mah.natives.NATIVES)`; the set of `"..."` literals in
`runtime/src/vm/thread.rs` between `pub const ATOMIC_REFUSED_NATIVES` and the next `];` equals the
frozenset (skipped with a message if the Rust tree is absent).

**`tests/test_bytecode.py`** (`SharedOpcodeTests`): `shared let x = 1\natomic { x = x + 1 }\nprint(x)`
is minor 21 and `mah dis` shows `sharedset`, `sharedget`, `atomicbegin`, `atomicend`,
`atomicabort` and `name="x"`; `shared let x = 0\natomic {\n    if x == 0 { retry }\n}` shows
`retry`; flipping the minor byte to 20 fails with `opcode 'NAME' at instruction I requires minor
version >= 21, ...` for the first `shared*`/`atomic*` instruction; a decoded program whose first
instruction's code byte is replaced by `0x72` fails with `unknown opcode 0x72 at instruction 0` (build
the bytes like the existing hand-built-file tests); std:thread stays minor 21.

**`tests/test_typecheck.py`** (`ThreadTypeTests`): `shared let n = 0\nlet v = atomic { n + 1 }` →
`v: Number`; `shared let n = 0\nlet w = atomic {\n    if n > 0 { n } else { retry }\n}` → `w: Number`;
W2 for `shared let xs = [1]\nfn f(v) { v.push(2) }\nf(xs)` (new text) and none for
`...\natomic { f(xs) }`; W3 (new text) and none inside `atomic { ... }`; W1 and the rest unchanged.

**`mah/std/thread.test.mh`**: `bump` uses `atomic { counter = counter + 1 }`; `add_twice` becomes
```mah
fn add_twice(v: Unknown) {
    atomic {
        items.push(v)
        atomic { items.push(v) }
    }
}
```
test "lock is re-entrant" → test "atomic blocks nest" (same body); test "write-back on throw" →
test "a throw inside atomic publishes nothing":
```mah
test "a throw inside atomic publishes nothing" {
    try {
        atomic {
            items.push("lost")
            throw Oops { why: "stop" }
        }
    } catch {
        _ => { }
    }
    let now = items
    assert_eq(now.len(), 0)
}
```
and two new tests:
```mah
fn emit(s: String) { print(s) }

test "retry waits for a change" {
    let t = thread.spawn()
    let p = detach(t) {
        atomic {
            if counter == 0 { retry }
            counter
        }
    }
    counter = 7
    assert_eq(p.await, 7)
    t.join()
}

test "side effects throw in_atomic" {
    let e = assert_throws(fn() { atomic { emit("x") } })
    assert_eq(e.kind, "in_atomic")
}
```
(`emit` goes next to the other helpers at the top of the file.)

**`examples/threads.mh`** — M44's file with: line 1 `# M44/M45: threads -- jobs on other threads, shared variables, atomic blocks and channels.`;
the comment above the shared lets `# Shared variables live outside every thread; \`atomic\` changes them safely.`;
a third shared let `shared let ready = ""`; `visit`'s blocks `atomic { hits = hits + 1 }` and
`atomic { seen.push(n) }`; and, between section 4 and the failing-job section (renumbered 6):
```mah
    # 5. `retry` waits until a shared variable it read changes.
    let waiter = detach(worker) {
        atomic {
            if ready == "" { retry }
            ready
        }
    }
    ready = "go"
    print("waited for:", waiter.await)
```
Golden (`tests/test_examples.py::test_threads`):
`fib(18) = 2584\n144 on worker\nhello from worker / hello\nhits: 400 seen: 8 sum: 28\nsum of squares: 55\nwaited for: go\ncaught: bad input\ndone\n`.

### 12.2 Part 2 (`make test-rust`, `cargo test`, `vm_diff.py` green)

- Everything in §12.1 that runs programs passes with `MAH_TEST_VM=rust`.
- `runtime/tests/vm_diff.py` inline cases (empty stdin): keep `threads_basic`, `threads_errors`,
  `threads_channels`, `threads_close`, `threads_exit`, `threads_keepalive`, `threads_uncaught`,
  `threads_stuck`, `threads_handles`; replace `threads_shared` = T7, `threads_pool` = T8,
  `threads_semaphore` = T10, `threads_implicit` = T19, `threads_reads` = T20; remove
  `threads_deadlock`, `threads_writeback`, `threads_await_deadlock`, `threads_await_deadlock_job`,
  `threads_teardown_lock`; add `threads_nested` = A1, `threads_atomic_throw` = A2,
  `threads_retry_local` = A3, `threads_retry_queue` = A4, `threads_retry_stuck` = A5,
  `threads_in_atomic` = A6, `threads_bank` = A8, `threads_exclusive` = A9, `threads_join_cycle` = A10,
  `threads_teardown_retry` = A11, `threads_alias` = A12, `threads_rerun_alias` = A13 (output only),
  `threads_modules` = T13b's first two programs (as multi-file cases, the way existing module cases
  are written; skip if vm_diff has no multi-file support and say so in the report), and
  `threads_atomic_uncaught`: `shared let n = 0\nfn bad() { throw RuntimeError.ArgumentError {
  message: "inside" } }\natomic {\n    n = 1\n    bad()\n}\n` (uncaught: stderr and exit code must
  match, including the backtrace/position lines; `n` is never published).
- `cargo test`: §10's decode test and the thread.rs unit tests.

### 12.3 Part 3

`tests/test_lsp_threads.py` (§11.3), `tests/test_format.py` (§11.4), `tests/test_project.py` green;
the tree-sitter parse check of §11.1.

---

## 13. Work split — every file, exactly one part

| file | part | change |
|---|---|---|
| `mah/preprocessor.py` | 1 | §8.1 |
| `mah/compiler/lexer.py` | 1 | §8.2 |
| `mah/compiler/ast_nodes.py` | 1 | §8.3 |
| `mah/compiler/parser.py` | 1 | §8.4 |
| `mah/compiler/resolve.py` | 1 | §8.5 |
| `mah/compiler/codegen.py` | 1 | §8.6 |
| `mah/compiler/typecheck.py` | 1 | §8.7 |
| `mah/bytecode/format.py`, `lower.py`, `disasm.py` | 1 | §7, §8.8 |
| `mah/bytecode/encode.py`, `decode.py` | 1 | only if §12.1's bytecode tests fail |
| `mah/runtime_values.py` | 1 | §9.1 |
| `mah/thread_runtime.py` | 1 | §9.2 |
| `mah/thread_natives.py`, `mah/natives.py` | 1 | §9.3 (only if needed) |
| `mah/code_interpreter.py` | 1 | §9.4 |
| `mah/std/thread.mh`, `mah/std/prelude.mh` | 1 | §8.9 |
| `mah/std/thread.test.mh` | 1 | §12.1 |
| `examples/threads.mh` | 1 | §12.1 |
| `tests/test_threads.py`, `tests/test_parser.py`, `tests/test_typecheck.py`, `tests/test_examples.py`, `tests/test_bytecode.py` | 1 | §12.1 |
| `runtime/src/decode.rs` | 2 | §10 |
| `runtime/src/vm/error.rs`, `value.rs`, `link.rs`, `exec.rs`, `thread.rs`, `natives.rs`, `mod.rs` | 2 | §10 |
| `runtime/tests/vm_diff.py` | 2 | §12.2 |
| `syntax-highlight/grammar.js`, `syntax-highlight/src/**`, `syntax-highlight/queries/mah/highlights.scm` | 3 | §11.1 |
| `editors/vscode/syntaxes/mah.tmLanguage.json` | 3 | §11.2 |
| `mah/lsp/analysis.py`, `tests/test_lsp_threads.py` | 3 | §11.3 |
| `mah/format/formatter.py`, `tests/test_format.py` | 3 | §11.4 |
| `www/src/lib/markdown/highlight-mah.ts` | 3 | §11.5 |
| `docs/contracts/M44_threads.md` (top note only), `docs/MAHC_FORMAT.md`, `docs/STDLIB.md`, `docs/V2_DESIGN.md`, `docs/NEXT_PHASES.md`, `docs/ERRORS.md`, `docs/RUST_VM.md`, `docs/FORMAT.md` | 3 | §11.6 |
| `mah/project/templates/docs/mah-language.md`, `mah/project/templates/AGENTS.md` | 3 | §11.6 |
| `www/src/content/std/thread.md`, `www/src/content/docs/async.md`, `www/src/content/docs/standard-library.md`, `www/src/content/docs/errors.md`, `www/src/content/blog/v0-4-0-threads.md` | 3 | §11.6 |

Not touched: the branch `m44-lock-block`, `docs/contracts/M44_threads_options.md`, the web
framework, the `mah-todo-demo` repository, thread.agent, semaphore timeouts.

Order: Part 1 lands (`make test` green; `make test-rust` may fail only on programs using the changed
1.21 opcodes). Parts 2 and 3 branch from it. Where Part 1 reports a forced deviation, the thinker
updates this contract first.

---

## 14. Mah gotchas for anyone writing Mah here

- M44 §14's list still applies (`&`/`|` evaluate both sides; `print(a, b)` separates with a space;
  `;` rules; `for let x in v`).
- `atomic {` must have its `{` on the same line, and in an `if`/`while`/`for`/`match` head it must be
  parenthesized: `if (atomic { ready }) { ... }`. A plain read needs no `atomic`: `while !stop { }`.
- Inside `atomic`: no `print`, `.await`, `sleep_async`, `detach` (compile errors), and no calls that
  do them (`in_atomic` at run time); return values instead of assigning outer variables
  (`let v = atomic { ... }`); `retry` only directly in the body (not in a nested `fn`).
- A helper that changes a shared variable in place must itself use `atomic { }` (it joins the
  caller's transaction when there is one).
- `retry` alone on its line (or before `}`/`;`/`,`/`)`/`]`) inside `atomic` is the keyword;
  `retry + 1` is a variable. Don't name a variable or function `retry` where an `atomic` body would
  use it in keyword position: that's E6b.
- Inside `atomic`, assigning a shared variable copies (`ys = xs` then `ys.push(1)` leaves `xs`
  alone), exactly as outside.
- Don't busy-wait inside `atomic` (`while !ready { }` reads a snapshot forever); use `retry`.

---

## 15. Judgment decisions (for the user)

1. **TL2 algorithm** (global clock, per-variable versions, snapshot check on every first read,
   validation at commit under the one runtime mutex). Every attempt sees a consistent snapshot, so
   read-only transactions commit without validation and a throw/`not_sendable` propagates without
   validation (unlike GHC, which validates because its attempts can see inconsistent states).
2. **The body is a closure** called by the `atomic` instruction sequence, so a rerun gets fresh
   locals; restart restores (pc, frame, call-stack depth, defer-stack depth) of the outermost
   `atomicbegin`; abandoned attempts run no `defer`s.
3. **Flat nesting** (joins; a throw out of an inner block undoes nothing by itself). **`or_else`
   deferred** — it needs nested rollback.
4. **`retry` only lexically inside `atomic`** (helpers compose by wrapping their own code in
   `atomic`, which joins). A `retry` with an empty read set fails at once with `stuck`; in an
   implicit runtime call it throws the existing "cannot suspend" error.
5. **8 failed attempts → exclusive attempt**; the token blocks every committing transaction and
   every plain shared assignment (whole VM thread, OS-level wait), FIFO among exclusive waiters,
   commits already waiting go first. Transactions owned by an implicit runtime call go exclusive
   too (their wait is an OS-level wait like a commit's, not a suspension), so the progress
   guarantee has no exception. Every way an attempt ends — including an internal error inside
   `atomicbegin`/`atomicend` (guards) and a task ending with a transaction set (safety net) —
   releases the token.
6. **Publishing an exposed-but-unassigned entry only if it changed** (`same_copy`), so read-only use
   of a container inside `atomic` doesn't bump versions; closures/frames always count as changed.
7. **Compile errors** E4 (`.await`, `sleep_async`, `print`, `detach` inside), E6 (`retry` outside),
   E8 (`return`/`break`/`continue` leaving), **E9** (assigning a non-shared variable declared outside
   — added because a rerun would repeat it); method calls on outer objects are not detectable and are
   documented instead.
8. **The refused-native list** of §5.2 (53 natives: all of io, fs, socket; timers; Promise settling;
   process exit/run/env changes; every thread/semaphore/channel operation that acts on other threads).
   Pure natives, `random`, `hooks`, clock reads, environment reads, `promise.new`, new
   semaphores/channels and live reads of counts are allowed. `.await` is refused whatever the
   Promise's state (deterministic).
9. **`atomic`/`retry` contextual with zero breakage**: `atomic {}`/`atomic { }`/`atomic { x: ... }`
   stay struct literals; in condition heads `atomic` stays a variable; `retry` is a keyword only
   inside an atomic body.
10. **Bytecode 1.21 redefined in place** (no bump: never released); the removed opcodes' codes stay
    unassigned so stale files fail cleanly.
11. **Await-deadlock message** loses "locks": `deadlock: this await would never end (it waits,
    through threads, for itself)`.
12. **`atomic` takes a postfix chain** (`atomic { xs }.len()`), like `if`/`match` (M44's `lock` did
    not).
13. **Assigning a shared variable inside a transaction stores a local copy** (as M44's held
    assignments did), so working values never alias each other or non-shared objects and an attempt
    can always be discarded and rerun.
14. **E6b**: the keyword `retry` with a visible binding named `retry` is a compile error rather
    than a silent reinterpretation; the preprocessor keeps `atomic {`/`retry` unmangled in modules
    (mirroring the parser's rules) and reports E6b for module-level `retry`s it would have mangled.
15. **Backtraces keep the `atomic` line** as one extra entry (the body is a real call); LSP and
    checker walkers treat the synthesized closure as a block.
16. **`detach(t)` in a helper reports `thread.submit`** in its `in_atomic` message (it is that
    native); lexically inside `atomic` it is E4 `'detach'`.
17. **Bytecode 1.21 redefinition confirmed safe**: crates.io has no `mah-vm` crate (checked
    2026-10-08); Part 2 re-checks before landing and the thinker bumps to 22 if that changed.

## 16. Deferred (not in M45)

`or_else` (needs nested rollback of the write set); nested rollback of an inner `atomic` left by a
throw; `retry` inside implicit runtime calls; making `ch.len()`/`s.available()` transactional (or
waking `retry` on them); transactional channels/semaphores; a statistics/introspection API (attempt
counts, exclusive runs); contention management smarter than "8 then exclusive" (backoff, priorities);
detecting outer-object mutation inside `atomic` (`outer.push(x)`) and outer assignments from called
functions/closures; highlighting `retry` outside statement position (tree-sitter/VS Code);
detecting a busy-wait loop inside `atomic` (documented instead); Rust-side debug counters like
Python's `TX_STATS` (the Rust exclusive path is covered by its unit test 6); cheaper reads (copy-on-write
working copies); everything M44 §16 deferred that is still open (`select`, thread-local storage,
interrupting a job, ...). Semaphore timeouts and thread.agent are out of scope.
