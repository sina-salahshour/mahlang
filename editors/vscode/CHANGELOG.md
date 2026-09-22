# Changelog

## 0.2.0

All changes below are entirely in the `mah lsp` server this extension
already talked to in 0.1.0 — the extension client itself (`src/extension.ts`)
didn't need to change to pick any of this up, beyond one startup bug fix.

- **Fixed a startup bug**: the client was passing `transport:
  TransportKind.stdio` explicitly, which `vscode-languageclient` reads as
  "append a `--stdio` flag to the server command" (the convention for
  servers with multiple transport modes) rather than "use stdio" — `mah
  lsp` has no such flag and always speaks stdio unconditionally, so this
  made the server crash on launch. Fixed by omitting `transport` entirely
  (an `Executable` server already defaults to stdio).
- **Fixed cross-file go-to-definition**: jumping to a definition in a
  *different* file previously produced a malformed URI
  (`file://///path/...`, extra slashes) on current Python versions, so
  the jump silently failed or misbehaved. Same-file jumps were unaffected,
  which is why this was easy to miss initially.
- **Hover** now shows doc comments (the `#` comment written directly above
  a declaration) for both local and imported symbols, with the real,
  demangled name — no more seeing an imported symbol's internal
  compiler-mangled name. Hovering a `struct`/`enum` name or an enum
  variant now shows its shape (fields / variant list) instead of nothing;
  hovering `defer`/`detach`/`sleep_async`/`.await` now shows real
  documentation too.
- **Completion** is enabled for the first time — it was previously
  advertised but silently broken. Now offers keywords, builtins, in-file
  variables/functions, struct/enum type names, and imported/namespaced
  symbols (typing `namespace.` completes just that namespace's exports).
  Typing inside an `import "..."` string — even before closing the quote
  — completes real `.mh` files found on disk.
- **Go-to-definition** now also works on struct/enum type names and enum
  variant names, jumping to their declaration.
- **Rename** now works across files: renaming a function or variable
  finds every file in the workspace that imports it (not just the
  currently open one) and updates all of them together, or refuses
  outright if any relevant file has an error — never a silent partial
  rename. Also new: renaming a `struct`/`enum` type name, an enum variant
  name, or a struct/enum field name (in its declaration, in a literal, or
  in an explicit pattern) — always single-file, since structs/enums can't
  be exported across files. Renaming through plain field access (`p.x`)
  is deliberately still refused rather than guessed at — there's no type
  system yet to know which struct's `x` a given `p.x` actually means.
- **Syntax highlighting** gained `defer`, `detach`, `sleep_async`, and
  `.await` (Mah's new async support), plus a `defer` grammar gap that
  turned out to predate this extension entirely.

## 0.1.0

- Initial release: TextMate syntax highlighting for `.mh` files, an LSP
  client wired to `mah lsp` (diagnostics, hover, go-to-definition, rename),
  and a custom file icon for VS Code's built-in icon themes.
