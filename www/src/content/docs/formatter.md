---
title: Formatter
order: 14
section: Tooling
---

`mah format` rewrites Mah source into one consistent layout. It uses the
lexer to find tokens and the parser to learn the structure (where each
statement starts, which `{` opens a block versus a struct literal, which
`<` is a generic bracket, which `-` is unary).

## The guarantee: only whitespace changes

The formatter **never adds, removes, or reorders a token**. It only
changes the whitespace between tokens: spaces, newlines, indentation,
blank lines. Comments are kept, word for word. It does **not** add or
remove `;`, parentheses, or trailing commas — those are tokens.

Every run verifies the guarantee before writing anything: the formatted
text must re-lex to the same token sequence, contain the same comments
in the same order, and re-parse to the same AST (positions ignored). If
any check fails, that's treated as a formatter bug — the file is left
untouched and `mah format` reports an internal error and exits nonzero,
rather than risk silently changing meaning.

## What it does

- **One statement per line.** Statements that shared a line are split.
- **Blocks** (`{ }`) stay inline only if they were on one line in the
  source, hold at most one statement or tail, contain no comment, and
  fit within the line width. Otherwise each statement goes on its own
  line. An `if`/`elif`/`else` chain's blocks are inline or broken
  **together**.
- **`match` arms**: one per line.
- **Bracket groups** (call/parameter lists, Vector/Map literals, struct
  literals, `<...>` type argument lists) go **flat** if they fit on the
  line, or **broken** (one item per line) if they don't — or if you
  deliberately wrote a Vector/Map/struct literal across multiple lines
  to begin with (a broken literal stays broken, prettier's rule for
  object literals).
- **Hugging**: when only the last argument of a call spans lines (a
  closure, a block, a broken literal), only that argument breaks —
  `v.map(fn(x) {` ... `})`, not one argument per line.
- **Comments**: own-line comments are re-indented to the code that
  follows them; trailing comments stay at the end of their line, aligned
  with other trailing comments on consecutive lines (gofmt-style).
- **Blank lines**: collapsed to at most one in a row; stripped right
  after `{` and right before `}`, and at the start/end of the file.
- Spacing rules for every operator, `,`/`;`/`:`, brackets, and keywords
  are built in (see `docs/FORMAT.md` in the repo for the full table).

`import`/`export` lines are handled specially, since they're preprocessor
syntax rather than part of the grammar the parser sees — the formatter
finds them among the lexer's tokens and prints them back normalized:
`import "lib.mh"`, `import m from "lib"`, `export fn f...`, `export
name`.

## CLI

```sh
mah format [PATH ...] [--check]
```

- No paths: format every `*.mh` under the project root, skipping
  `build/`. Outside a project, the current directory, recursively.
- A directory path means every `*.mh` under it. A file path means that
  file. `-` means stdin to stdout.
- Rewrites files in place, printing each changed file's name. `--check`
  writes nothing, prints the files that would change, and exits 1 if any
  would.
- A file with a syntax error isn't formatted: `mah format` reports the
  parse error for that file, exit 1, and keeps processing the rest.

## Editor integration

The LSP advertises `textDocument/formatting`, returning one
whole-document edit (or no edit when the file has a syntax error). The
VS Code extension gets format-on-save for free through that — see
[Tooling](/docs/tooling).
