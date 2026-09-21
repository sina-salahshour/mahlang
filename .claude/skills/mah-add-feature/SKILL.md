---
name: mah-add-feature
description: Add or change a Mah language construct (syntax, operator, statement, builtin) across the whole pipeline — lexer, parser, AST, resolver, codegen, VM, tests, tree-sitter grammar, and LSP. Use whenever the user asks to add/change Mah syntax or semantics, not just discuss the language.
---

# Adding a Mah language feature

Read `docs/V2_DESIGN.md` (current design + landed-milestone writeups),
`docs/NEXT_PHASES.md` (deferred work, and constraints already-landed code
must respect for it), and `docs/TESTING.md` first if you haven't already.
**Note:** `docs/ARCHITECTURE.md`/`GRAMMAR_DSL.md`/`RUNTIME.md` describe the
*pre-M0* grammar-generator pipeline (`mah.lang`, `actions.py`) — that
pipeline is retired; don't follow their workflow, they're historical
reference only (each has a banner saying so).

If this feature is large enough to delegate implementation to a subagent
(a full milestone like M1's heap-frame rewrite, say), read
`docs/DEVELOPMENT_WORKFLOW.md` first — it covers when that's a good idea,
how to write a spec a cheaper coding agent can execute without having to
make its own design decisions, and why the verification step afterward
shouldn't itself be delegated to a cheap model.

The current pipeline is hand-written: `preprocessor.py` (imports/exports,
rarely needs touching) → `compiler/lexer.py` → `compiler/parser.py`
(builds the AST in `compiler/ast_nodes.py`) → `compiler/resolve.py`
(scope/address resolution) → `compiler/codegen.py` (emits IR tuples) →
`code_interpreter.py`'s `run_code` (executes them), with `runtime_values.py`
holding shared heap-object types (`Frame`, `Closure`, `NONE_VALUE`, and
whatever M2+ adds — `StructInstance`, `EnumInstance`, etc.). A feature
typically touches several of these **in this order**:

## 1. Lexer: `compiler/lexer.py`

New keyword or symbol → add it to `KEYWORDS` (put multi-char keywords
before anything that could prefix-match them as an identifier — not an
issue currently since identifiers and keywords are disambiguated by exact
string match, but check `TokenType`/`_SINGLE_CHAR`/`_TWO_CHAR` for the
existing pattern). New literal *shape* (not just a keyword) → extend
`get_next_token`'s character-class dispatch (see how `NUMBER`/`STRING`
work) and add a `TokenType`.

## 2. AST: `compiler/ast_nodes.py`

Add a `@dataclass` node for the new construct. Look at how `FnExpr` is
shaped (M1) for the pattern of "resolver fills in extra fields after
construction" (e.g. `address`, `frame_level`) — declare those fields with
`field(default=None, repr=False)` so the parser doesn't need to know about
them.

## 3. Parser: `compiler/parser.py`

Hand-written recursive-descent — no grammar-conflict machinery to satisfy,
but keep the precedence chain (`_parse_or_and` → `_parse_compare` →
`_parse_additive` → `_parse_multiplicative` → `_parse_unary` → `_parse_pow`
→ `_parse_primary`) in mind: a new binary operator goes into whichever
level it belongs at (or a new level between two existing ones — see that
chain's comment for v1's non-standard precedence choices, preserved
deliberately). A new statement form gets a branch in `parse_stmt`; a new
expression form gets one in `_parse_primary`.

## 4. Resolve: `compiler/resolve.py`

This is where scoping/addressing happens — read its module docstring
first, it explains frame levels vs. lexical scopes and the `(depth, slot)`
addressing scheme in detail (M1). If your feature introduces a new kind of
declaration, decide: does it need its own frame level (only `fn` bodies do
today), or does it just need a slot in the current one (`let`-like)? If it
introduces a new kind of name lookup, extend `_lookup`/`_declare` rather
than duplicating scope-walking logic.

## 5. Codegen: `compiler/codegen.py`

Emit `(op, arg1, arg2, dest)` tuples via `self.buf.emit(...)`; addresses
are always `(depth, slot)` tuples (`self._temp()` for a fresh scratch
value). For control flow, copy the `_gen_if`/`_gen_while` backpatch
pattern (emit a placeholder, remember its address, patch it once the
target is known) — don't invent a different mechanism. If the feature
needs a new heap object kind (like M1's `Closure`), add the class to
`runtime_values.py`, not here.

## 6. VM opcode: `code_interpreter.py`

New `op` string → add a `case (op, ...):` arm in `run_code`'s `match`. Use
the `_read`/`_write` helpers for any `(depth, slot)` operand — never index
`frame.slots` directly outside those two functions.

## 7. Test it — do this before calling the feature done, not after

**Read `docs/TESTING.md` — every feature needs tests, not just an example
file.** Concretely:

```sh
make test                                       # must stay green
python -m mah build examples/your_test.mh       # eyeball the generated IR
python -m mah run examples/your_test.mh         # run it
```

- Add test cases to `tests/test_language.py` (happy path + the feature's
  error cases — undefined-name-shaped mistakes, arity/shape mismatches,
  wrong-context misuse). A big new feature (structs, enums, pattern
  matching) probably deserves its own `tests/test_<feature>.py`.
- If the change is about parsing/precedence/AST shape, also add a narrow
  test to `tests/test_parser.py` (see `FnDesugaringTests` for the pattern)
  — it isolates a broken layer far faster than an end-to-end failure.
- Add a small example under `examples/` too, for a human to read — but it
  is *not* a substitute for the automated test, since nothing runs it
  automatically.
- If the change is an intentional behavior change (not a bug fix), update
  whichever existing test asserted the old behavior in the same change,
  and say why — never just delete an inconvenient assertion.

## 8. Editor support (do this before calling the feature done)

The LSP is live and working (`mah/lsp/`, rebuilt on the real resolver
across M6-M9 and a later hover/completion follow-up — see
`docs/V2_DESIGN.md`) — a new keyword or builtin needs to show up there,
not just in the compiler:

- **`syntax-highlight/grammar.js`** — mirror the new syntax by hand (a
  hand-maintained tree-sitter grammar, not generated from anything).
  Update `syntax-highlight/queries/mah/highlights.scm` if new syntax needs
  new highlight groups.
- **`editors/vscode/syntaxes/mah.tmLanguage.json`** — the VS Code
  extension's own TextMate grammar, hand-maintained separately from the
  tree-sitter one above (VS Code has no public API for a third-party
  extension to register a tree-sitter-based grammar — TextMate is the only
  option). A new keyword needs a pattern here too, or it won't highlight
  in VS Code even though it does in Neovim.
- **`mah/lsp/analysis.py`** — a new reserved keyword needs an entry in
  `KEYWORD_TOKENS`/`KEYWORD_DOCS` (or `BUILTIN_TOKENS`/`BUILTIN_DOCS` for a
  builtin-function-shaped addition like `sin`/`cos`) so hover and
  completion pick it up automatically — both dispatch off these same
  tables, no separate registration needed. If the feature adds a new kind
  of *declaration* (like M2's structs, M3's enums) rather than just a
  keyword, check whether it needs its own position-tracking in
  `compiler/resolve.py` for hover/go-to-definition to describe it (see
  `type_position_index` for the struct/enum precedent) — variables/
  functions already get this for free via `position_index`.
- Add or extend a test in `tests/test_lsp_*.py` covering hover on the new
  keyword/construct, matching the pattern already there for existing
  keywords/builtins.

## Common mistakes

- Skipping step 7 — an example file that "looks right" when you eyeball
  its output is not a regression test; the next change can silently break
  it with nothing catching it.
- Forgetting that only `fn` bodies create a new frame level (`resolve.py`)
  — a new block-like construct (e.g. a future `match` arm) almost
  certainly shares the enclosing function's frame level, it doesn't get
  its own.
- Introducing a new heap value kind and forgetting `_to_str` in
  `code_interpreter.py` needs to know how to print it (see how `bool` and
  `NONE_VALUE` are special-cased there, checked *before* the generic
  numeric branch since `bool` is a Python `int` subclass).
- Assuming `mah.lang`/`compiler-generator/`/`actions.py` are relevant —
  they're retired (see `docs/V2_DESIGN.md`'s M0 entry). If you find
  yourself editing `mah.lang`, stop — you're following stale docs.
