---
name: mah-add-feature
description: Add or change a Mah language construct (syntax, operator, statement, builtin) across the whole pipeline — grammar, parser-generator, IR actions, VM, tree-sitter grammar, and LSP. Use whenever the user asks to add/change Mah syntax or semantics, not just discuss the language.
---

# Adding a Mah language feature

Mah has no AST — see `docs/ARCHITECTURE.md`, `docs/GRAMMAR_DSL.md`,
`docs/RUNTIME.md` first if you haven't already; this skill assumes that
context. A single feature touches up to five places. Do them **in this
order** — each step depends on the previous one existing.

## 1. Grammar: `mah.lang`

- New token? Add `Name: "regex";` **before** `--`. Put it before `ID` in
  file order if it's a keyword (order = lexer match priority).
- New syntax? Add/extend a rule after `--`. Remember the LL(1) constraint
  (`docs/GRAMMAR_DSL.md`): no two alternatives of one rule may share a
  first token, no left recursion, only one `#e` alternative per rule.
- Decide semantic-action hook points (`@name`) — where in the production
  does code need to fire relative to the tokens around it (e.g. `@save`
  *before* a block so you have the placeholder address; the jump-patch
  action *after*, once the block's end address is known).

Regenerate and confirm no LL(1) conflict:

```sh
make lang
```

If it raises `"the grammar is not LL(1)"`, the rule needs restructuring
(left-factoring, an extra nonterminal layer for precedence, etc.) — this is
the DSL forcing you to resolve the ambiguity, not a bug to work around.

## 2. Semantic actions: `actions.py`

Every `@name` used in `mah.lang` must have a matching
`@ir.action("name")` callback here (missing one is a runtime
`SystemError: Action not found`, not a build-time error — `make lang` does
not check this). Study an existing action of the shape you need first:

- **Binary op** → copy the `operator()` helper pattern (pop rhs, pop lhs,
  emit one instruction, push a temp).
- **New control-flow construct** → copy the `if`/`while` backpatching
  pattern in `docs/RUNTIME.md`'s "Control flow" section: `@save` a
  placeholder, compile the body, patch the placeholder once you know where
  execution continues.
- **New builtin call** → copy `builtin_single_arg_function` or the
  `print`/`sin` action shape (pop args off `ir.stack` until the
  `["function_arg_stack_base", ...]` marker).

Use `ir.get_temp_address()` for intermediates, `ir.declare_variable`/
`ir.get_variable_address` for named values, `ir.write_code` to emit (pass
`address=` only when patching a placeholder). Remember: whatever this
action pushes onto `ir.stack` is what "this construct's value" means to
whoever consumes it next in the grammar.

## 3. VM opcode: `code_interpreter.py`

If you introduced a new `op` string, add a `case (op, ...):` arm to the
`match operation` in `run_code`. Keep the existing convention: read operands
via `stack[addr]`, write the result via `stack[dest]`. If the op needs to be
printed nicely by `mah.py build`'s dump, it needs nothing extra — the
printer just renders the raw tuple.

## 4. Try it

```sh
python mah.py build examples/your_test.mh   # dump generated IR, sanity-check by eye
python mah.py run examples/your_test.mh     # or just: python mah.py examples/your_test.mh
```

Add a small example under `examples/` exercising the new construct — this
project uses runnable examples, not a test framework.

## 5. Editor support (do this before calling the feature done)

- **`syntax-highlight/grammar.js`** — mirror the new syntax by hand (this is
  a hand-maintained tree-sitter grammar, *not* generated from `mah.lang`).
  Update `syntax-highlight/queries/mah/highlights.scm` if new syntax needs
  new highlight groups. Regenerating `src/parser.c` requires the
  `tree-sitter` CLI (not part of this repo's own build).
- **`lsp/analysis.py`** — if the feature introduces a new kind of
  declaration or scope, update `_build_scopes` (go-to-definition/rename
  target resolution) and `collect_symbols`/`get_hover`/`get_completions` as
  relevant. This is a second, independent scope model re-derived from
  tokens — it does not share code with `IRGenerator`'s scope stack, so it
  will not "just work" from the compiler change alone.

## Common mistakes

- Forgetting step 2 and getting a confusing `SystemError` at parse time
  instead of a grammar error at build time.
- Writing a grammar rule that's ambiguous in a way LL(1) can't express
  (e.g. two statement forms starting with the same token) — restructure
  with a shared prefix nonterminal rather than trying to add lookahead.
- Changing `mah.lang` and forgetting `make lang` — the top-level `compiler/`
  package is checked-in generated output, it does not regenerate itself.
- Updating the tree-sitter grammar or LSP scope logic and assuming the other
  one is automatically consistent — they are three separate descriptions of
  the same syntax (`mah.lang`, `grammar.js`, `analysis.py`'s token-level
  scope builder) with no shared source of truth today.
