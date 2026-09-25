# `mah format`

Status: **implemented** (milestone M21b, see `docs/V2_DESIGN.md`). Code in
`mah/format/`.

`mah format` rewrites Mah source into one consistent layout. It uses the
lexer to find tokens and the parser to learn the structure (where each
statement starts, which `{` opens a block and which opens a struct
literal, which `<` is a generic bracket, which `-` is unary). The layout
rules are built-in defaults for now. They live in one options object, so
a `[format]` section in `mah-project.toml` can set them later without
touching the formatter.

## The guarantee: only whitespace changes

The formatter **never adds, removes, or reorders a token**. It only
changes the whitespace between tokens: spaces, newlines, indentation,
blank lines. Comments are kept, word for word.

That makes it safe by construction. Mah's parser ignores newlines except
in a few places, all of which need two tokens on the same line: a postfix
`[` (indexing) and what it indexes (a line starting with `[` is a new
Vector literal), a range's `..` and its end, and `break`/`return` and
their value. So the formatter:

- only ever breaks lines between statements, after an opening bracket or
  comma, and before a closing bracket, never inside those pairs;
- keeps a newline between statements, so a `[` that started a new
  statement still does.

It does **not** add or remove `;`, parentheses, or trailing commas.
Those are tokens. (A future `--fix` mode could, separately.)

Every run verifies the guarantee before writing anything. The formatted
text must re-lex to the same token sequence (type and literal, positions
ignored), contain the same comments in the same order, and re-parse to
the same AST (compared with positions stripped). If any check fails,
that's a formatter bug: the file is left untouched and `mah format`
reports "internal formatter error, file left unchanged" with the file
name, and exits nonzero.

## Inputs the parser can't read directly

- `import ...` statements and the `export` keyword are handled by the
  preprocessor, not the parser. The formatter finds them among the
  lexer's tokens (the same shapes the preprocessor accepts) and replaces
  them with spaces of the same length before parsing, so every other token
  keeps its position.
  Then it prints them back normalized: `import "lib.mh"`, `import m from
  "lib"`, `export fn f...`, `export name`.
- A file with a syntax error isn't formatted: "can't format FILE: <the
  parse error>", exit 1, other files still processed.

## Comments and blank lines

The lexer skips comments, but tokens keep their positions, so the text
between two consecutive tokens (the "gap") holds exactly the comments and
newlines there. From each gap the formatter keeps:

- **comments**, each classified as **trailing** (on the same line as the
  previous token) or **own-line**;
- **blank lines**: whether the gap had at least one empty line. That
  becomes at most `max_blank_lines` (default 1) in the output. Blank lines
  right after a `{` or right before a `}` are removed. So are any at the
  start or end of the file. The file ends with exactly one newline.

Own-line comments are re-indented to the depth of the code that follows
them (or, before a `}`, to the depth inside the block). Trailing comments
stay at the end of their line with one space before the `#`. A run of
consecutive lines that all end in a trailing comment gets the comments
aligned one space after the longest line's code, like gofmt. A comment's
text is kept exactly, except for trailing whitespace. A trailing comment
after a closing bracket doesn't force that bracket's group to break.

A gap that contains a comment always keeps a line break where the comment
ends (a trailing comment ends its line, by definition).

## Layout rules (defaults)

`FormatOptions`: `line_width = 100`, `indent = 4` (spaces),
`max_blank_lines = 1`.

### Statements and blocks

- One statement per line. Statements that shared a line (`let a = 1; let b
  = 2`) are split. The `;` stays at the end of the first line, because
  it's a token.
- Statement starts come from the AST: the first token of each statement in
  a block or at top level. A statement that the user spread over several
  lines is laid out by the expression rules below, not split.
- A block `{ ... }` (function body, `if`/`else`/`while`/`for`/`match`
  body, block expression, match arm body) is **inline** (`{ x + 1 }`, `{
  }`) only if it was on one line in the source, holds at most one
  statement or tail, contains no comment, and the whole line fits in
  `line_width`. Otherwise it's broken: `{` ends the line, each statement
  on its own line one indent deeper, `}` on its own line at the outer
  depth. `} else {` / `} elif ... {` stay joined. The blocks of one
  `if`/`elif`/`else` chain are inline or broken **together**: if the whole
  chain doesn't fit on one line, every block in it is broken.
- `match` arms: one arm per line, one indent inside the `match { }`.
- `struct`/`enum`/`trait`/`impl` bodies follow the bracket-group rule
  below for their field/variant lists, and the block rule for method
  lists (each method on its own line, one blank line allowed between).

### Bracket groups `( )`, `[ ]`, and brace-delimited lists

Call and parameter lists, Vector/Map literals, struct literals, struct/
enum field lists, and `<...>` type parameter/argument lists are **groups**
of comma-separated items. A group is laid out:

- **flat**, `f(a, b, c)`, if it fits on the current line and **either** it's
  a `( )`/`< >` group **or** the source had no newline right after its
  opening bracket. That's prettier's rule for object literals: a
  Vector/Map/struct literal you deliberately broke stays broken.
- otherwise **broken**: opening bracket ends the line, one item per line
  one indent deeper, each item keeping its comma, closing bracket on its
  own line at the outer depth. Items are laid out recursively with the
  same rules. No trailing comma is added, and an existing one is kept.
- An item that contains a comment forces its group to break.
- **Hugging**: when only the last argument of a call spans lines (a
  closure, a block, a broken literal), the call's parentheses stay put and
  only that argument breaks: `v.map(fn(x) {` ... `})`, not one argument per
  line.

Lines that are still too long after breaking every group on them (a
long binary expression or method chain with no groups left) are left as
they are. The formatter never breaks before `.`, `[`, `(` or a binary
operator.

### Spacing within a line

| Where | Rule | Example |
|---|---|---|
| binary operators `+ - * / // % ** == != < > <= >= & \| = => ->` | one space each side | `a + b`, `x => {` |
| range operators `..` `..=` | no space either side | `1..n`, `a..`, `..=b` |
| unary `-` and `!` | no space after | `-x`, `!done` |
| `,` | no space before, one after (or line end) | `f(a, b)` |
| `;` | no space before; newline after, unless inside an inline block | `x = 1;` |
| `:` (type annotation, kwarg, struct field, map pair, bound) | no space before, one after | `a: Number`, `f(x: 1)` |
| `.` | no space either side | `p.x`, `v.len()` |
| `(` `[` after a callee/indexed value | no space before | `f(x)`, `v[0]` |
| `(` `[` after an opener, or `)` `]` before a closer | no space inside | `(a + b)`, `[1, 2]` |
| `{` `}` | one space inside when inline: `{ x }`; empty: `{ }` | `P { x: 1 }` |
| `{` after a head (`if c`, `fn f()`, `struct P`, `match x`) | one space before | `if x {` |
| generic `<` `>` | no space inside or before `<` | `Vector<Number>` |
| keywords | one space after (`let`, `fn`, `if`, `return`, `detach`, ...); `fn(` in a type or anonymous fn has none | `fn(x) { }` |
| `#` trailing comment | see Comments | |
| any two tokens that would lex as something else when touching | one space | `100.. =>` (not `100..=>`) |

The AST settles every ambiguous token: which `-` is unary, which `<` is a
generic bracket (the ones following a type NAME that has arguments, or
opening a type parameter list), which `{` opens a block and which opens a
struct literal or a declaration body, and which `..` is missing a side.

## CLI

```
mah format [PATH ...] [--check]
```

- No paths: format every `*.mh` under the project root (found as `mah run`
  finds it, via `mah-project.toml`), skipping `build/`. Outside a project,
  the current directory, recursively.
- A directory path means every `*.mh` under it. A file path means that
  file. `-` means stdin to stdout.
- Rewrites files in place, printing each changed file's name. `--check`
  writes nothing, prints the files that would change, and exits 1 if any
  would.
- Exit codes: 0 fine, 1 `--check` found changes or a file couldn't be
  formatted (syntax error, or an internal formatter error).

## Editor

The LSP gets `textDocument/formatting` (advertised as
`documentFormattingProvider`), returning one whole-document edit, or no
edits when the file has a syntax error. The VS Code extension gets
format-on-save for free through that.

## Implementation notes

- `mah/format/`: `doc.py` (a small Wadler/prettier-style document model
  and printer: text, lines, groups, indent, line suffixes for trailing
  comments), `formatter.py` (tokens and their gaps, the facts taken from
  the AST, the bracket tree, the layout rules, and the verification),
  `cli.py` (`mah format`).
- The document model's `group` is "flat if it fits, else break", decided
  outermost first. That's the standard algorithm, and it makes the bracket
  rules above fall out directly.
- The parser also builds brace-less blocks (around a `defer` statement or
  a `detach` operand); only blocks whose `{` is in the source have
  statements of their own for layout purposes.
- AST comparison drops positions, including the one stored inside each
  keyword argument's tuple.
- Tests: every `examples/*.mh` and every template doc code block must be
  **idempotent** (formatting twice gives the same text as once) and pass
  the safety verification. Plus golden tests: small inputs with their
  exact expected outputs, one per rule above.
