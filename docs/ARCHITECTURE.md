# Mah Architecture

Mah is a from-scratch language: a hand-rolled grammar-description language
generates the lexer/parser/IR-generator, a flat-memory interpreter executes
the IR, and a pure-stdlib LSP + a tree-sitter grammar sit on top for editor
support. There is currently **no AST** — parsing and code generation happen
in a single pass (classic syntax-directed translation), and there is
**no heap and no call stack** — every variable gets one fixed address for
the whole run of the program. Read this alongside [GRAMMAR_DSL.md](GRAMMAR_DSL.md)
(the `.lang` meta-language) and [RUNTIME.md](RUNTIME.md) (the IR + VM).

## The two-tier bootstrap

There are two independent copies of a `compiler/` package in this repo, and
which one is "active" depends entirely on which directory a script runs from
(Python resolves `import compiler` against `sys.path[0]`, the running
script's own directory):

1. **`compiler-generator/compiler/`** — the *meta-compiler*. It was itself
   generated (by hand-bootstrapping once, then regenerating itself) from
   [`compiler-generator/compiler.lang`](../compiler-generator/compiler.lang),
   the grammar that describes the `.lang` file format. `generate.py` lives
   next to it, so `import compiler` inside `generate.py` picks up this copy.
   Its job: parse a `.lang` grammar file (e.g. `mah.lang`) into a token list
   and a grammar dict.

2. **top-level `compiler/`** — the *Mah language compiler*. It's what
   `make lang` (`python compiler-generator/generate.py mah.lang`) writes out:
   a lexer, a table-driven LL(1) parser, and an `IRGenerator` shell, all
   parameterized with Mah's actual tokens and grammar rules. `mah.py`,
   `actions.py`, and the LSP all run from the repo root, so their
   `import compiler` picks up *this* copy.

```
mah.lang  ──generate.py (using compiler-generator/compiler/)──▶  compiler/{lexer,parser,ir_generator}.py
                                                                          │
actions.py (semantic actions) ───────────────────────────────────────────┤
                                                                          ▼
mah.py ──▶ preprocessor.py (imports) ──▶ Lexer/Parser/IRGenerator ──▶ code_interpreter.py
```

Regenerating the language (`make lang`) only rewrites the top-level
`compiler/`. `compiler-generator/compiler/` changes only if someone
hand-edits `compiler.lang` and re-bootstraps — you will essentially never
need to touch it for a Mah language feature.

## `generate.py`: grammar → parsing table

`compiler-generator/generate.py` parses `mah.lang` by registering semantic
actions (`@token_name`, `@rule_variable`, `@save_rule`, ...) on the
meta-compiler's `IRGenerator`, so parsing `mah.lang` incidentally *builds*
two Python structures: `tokens` (name, regex, ignored?) and `grammar` (rule
name → list of alternative symbol sequences, tokens prefixed `TokenType.`,
epsilon alternatives as `[]`).

From `grammar` it computes, from scratch, the classic LL(1) tables:

- `find_first` — FIRST sets, recursively, memoized per call via a `checked`
  set to avoid infinite recursion on left-recursive-looking cycles.
- `find_follows` — FOLLOW sets, same recursive style.
- `parsing_table[nonterminal][lookahead-token] = which alternative to use`,
  built by unioning FIRST sets per alternative and using FOLLOW when an
  alternative can derive empty. **If two alternatives claim the same
  lookahead token, `generate.py` raises `"the grammar is not LL(1)"`** —
  this is the hard constraint on every grammar change (see GRAMMAR_DSL.md).

The three `.pyt` templates in `compiler-generator/templates/` are then
string-substituted (`{% TOKEN_TYPES %}`, `{% GRAMMAR_RULES %}`,
`{% PARSING_TABLE %}`, ...) and written to `compiler/lexer.py`,
`compiler/parser.py`, `compiler/ir_generator.py`.

## Lexer

Generated `compiler/lexer.py` is a linear scanner: at each position it tries
every `TOKEN_RULES` regex **in declaration order** (`mah.lang`'s token list)
and takes the first match anchored at the current position — so token order
in `mah.lang` is significant for keywords vs. identifiers (keywords must be
declared before `ID`, since `ID`'s pattern would otherwise swallow them; this
already works today because `mah.lang` lists keywords first). Tokens marked
`!Name: "..."` (e.g. `!Comment`) are lexed but filtered out by
`get_next_token` before the parser ever sees them.

## Parser

Generated `compiler/parser.py` is a **table-driven predictive (LL(1))
parser**, not a recursive-descent one written by hand. `parse()` keeps an
explicit stack seeded with `[START_SYMBOL, EOF]` and a single lookahead
token:

- pop a nonterminal → look up `table[nonterminal][lookahead]` → splice the
  chosen alternative's symbols onto the front of the stack.
- pop a terminal → must equal the current token, or it's a `SyntaxError`;
  advance the lexer.
- pop an `@action` string → look it up in `self.actions` (registered by
  `actions.py` via `ir.action(name)`) and call it with the *current* token.

Because it's table-driven with one token of lookahead and no backtracking,
**every grammar ambiguity is a build-time error**, not a runtime one — this
is both the parser's biggest safety net and its biggest expressiveness
limit (see GRAMMAR_DSL.md for what this rules out).

## IR generation: no AST, direct syntax-directed translation

`actions.py` registers one callback per `@action` name used in `mah.lang`.
Because actions fire *during* parsing, at exactly the point they're written
in a grammar rule, `IRGenerator` effectively **is** the AST: it has no tree,
only two pieces of state threaded through action calls:

- `ir.stack` — an operand stack of "which memory address holds this
  expression's value" (plus a few sentinel list markers like
  `["function_def", name, return_addr, code_addr]` and
  `["function_arg_stack_base", fn]` used to delimit variable-length
  constructs — function bodies, call argument lists — on the same stack).
- `ir.sstack[0:400]` — the emitted code: a flat array of 4-tuples
  `(op, arg1, arg2, dest)`, written sequentially via `write_code`, with
  in-place patching (`write_code(code, address=...)`) used for forward jump
  targets (if/while) once they become known — classic **backpatching**.

There is no separate semantic-analysis or codegen pass. Emitting code *is*
parsing. See [RUNTIME.md](RUNTIME.md) for the instruction set and memory
layout, including why this makes recursion and closures impossible today.

## Modules: a textual preprocessor, not a compiler feature

The compiler itself has no concept of files, `import`, or `export` — no
token exists for `.` even. `preprocessor.py` runs *before* the lexer:

- tolerant hand-written scanner (its own regex, separate from the generated
  lexer, because it needs to recognize `.` and `import`/`export` which
  aren't real tokens) walks the entry file and recursively inlines imported
  files (include-guarded by absolute path, so cycles/diamonds are safe).
- every inlined module's top-level names are alpha-renamed to
  `__mah_m{index}_{name}`; only `export`ed names get a rewrite rule applied
  at the *use* site in the importing file. A reference to a non-exported
  member is rewritten to `__mah_noexport_{name}`, a name that provably
  doesn't exist, so the ordinary "undefined variable" error fires.
- the output is one combined source string plus a `Segment` list mapping
  combined-text ranges back to `(original file, original offset)`, which is
  how `mah.py` and the LSP report errors at the right file/line/column and
  how go-to-definition crosses file boundaries.

This means the real compiler always sees one flat, single-file, singly
alpha-renamed program — multi-file support is 100% a source-to-source
transform layered in front of it.

## Runtime data types

Only four Python types ever land in a memory slot: `decimal.Decimal`
(numbers — chosen over `float` for exactness), `str` (strings), and `int`
`0`/`1` (booleans — comparison/`and`/`or` opcodes produce these; there is no
distinct boolean type). `+` overloads to string concatenation if either
operand is a string; `*` overloads to string repetition if one operand is a
string and the other numeric. There are no arrays, objects, enums, or `null`.
A function with no explicit `return` gets one injected that yields `0`
(`@fn_ret_inject_zero`).

## Tooling: two more hand-maintained views of the same grammar

- **`syntax-highlight/`** — a [tree-sitter](https://tree-sitter.github.io/)
  grammar (`grammar.js`) that re-describes Mah's syntax independently, plus
  `queries/mah/highlights.scm`. It is **not generated from `mah.lang`** —
  every grammar change must be mirrored here by hand, including the
  generated `src/parser.c` (via `tree-sitter generate`, not part of this
  repo's build).
- **`lsp/`** — a dependency-free LSP server (`server.py`, stdio JSON-RPC) on
  top of `analysis.py`. Diagnostics genuinely re-run the real compiler
  pipeline (`preprocess` → `Lexer`/`Parser`/`IRGenerator`) and catch its
  exceptions, so they're always accurate. But hover/definition/completion
  need per-token positions and a symbol table the compiler doesn't expose
  (it emits code, not a queryable structure), so `analysis.py` **re-derives
  its own lightweight scope tree from the token stream**
  (`_build_scopes`/`_resolve_declaration`) that mirrors — but does not
  share code with — the real `@scopestart`/`@scopeend`/`declare_variable`
  logic in `IRGenerator`/`actions.py`. Keeping these two scope models in
  sync by hand is the main maintenance cost of the LSP today. There is no
  rename support yet.

## Current hard limits (why a v2 is needed)

- **No recursion.** Every `let`/parameter gets one fixed global address
  forever (`ir.variable_pointer`, monotonically increasing, never reused per
  call). A recursive call reuses the *same* addresses as its caller, so
  a second in-flight call clobbers the first call's locals.
- **No closures / no first-class functions.** Functions are looked up by
  name at compile time into a fixed code address; there's no runtime
  function value, so nothing can be captured, stored in a variable, or
  passed around.
- **No heap, no composite data.** `variable_pointer`/`tmp_pointer` are
  simple bump counters into a *fixed 1500-slot array*; there's no allocator,
  no reference type, no structs/objects/enums/arrays.
- **No AST.** Error recovery is "stop at the first `SyntaxError`"; there's
  no tree to run a second, more forgiving pass over, and no structure for
  an LSP rename to rewrite safely (today's LSP literally re-tokenizes and
  re-derives scopes ad hoc).
