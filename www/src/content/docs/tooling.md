---
title: Tooling
order: 15
section: Tooling
---

## The language server

`mah lsp` is a dependency-free (standard library only) implementation of
the Language Server Protocol, built directly on the same resolver the
compiler uses — not a second, independent analysis of the source. It
currently provides:

- live diagnostics as you type, including syntax and resolve errors
  inside imported files
- hover, with docs for keywords and builtins, the declaration a
  variable/function/parameter resolved to (with its doc comment),
  struct/enum shapes, and trait and method signatures
- completion: keywords, builtins, names in scope, imported and
  namespaced names, methods/fields after a `.` based on the
  receiver's type, and — inside an `import "..."` string — `.mh` files and
  the standard library's `std:` modules
- go to definition — scope-aware, and it follows imports: jumping from a
  namespaced call, the namespace name itself, or an `import` path
  string, into the file it points at
- rename: variables and functions across every file that imports them,
  and struct/enum/variant/field names (including their uses in type
  annotations)
- document formatting, the same as `mah format` (see
  [Formatter](/docs/formatter))

Any LSP client can run it directly over stdio:

```sh
mah lsp
```

## VS Code

A VS Code extension lives in `editors/vscode/` — a TextMate grammar for
syntax highlighting, `mah lsp` wired in as the LSP client, and a custom
file icon for `.mh` files.

```sh
make build-vscode
code --install-extension editors/vscode/mah-language-*.vsix
```

## Neovim

```sh
make install-nvim      # tree-sitter syntax highlighting + the LSP ftplugin
make uninstall-nvim
```

This builds the `syntax-highlight/` tree-sitter grammar into your Neovim
config and drops in an ftplugin that starts `mah lsp` for every `.mh`
buffer via `vim.lsp.start`.

## Installing the `mah` command

```sh
make install-mah      # installs into ~/.local/lib/mah, links ~/.local/bin/mah
                       # (run `make vm` first to install the Rust runtime too)
make uninstall-mah
```

`make install` runs both `install-mah` and `install-nvim`, in order.
`PREFIX` defaults to `~/.local` — make sure `~/.local/bin` is on your
`PATH`.

## Portable bytecode: `.mahc`

```sh
mah build ./examples/structs.mh                              # -> structs.mahc
mah build ./examples/structs.mh --target release -o out.mahc # no debug info
mah runc ./examples/structs.mahc                              # run compiled bytecode
mah dis ./examples/structs.mahc                                # show it as readable instructions
```

`mah build` output starts with a `#!/usr/bin/env -S mah runc` line and is
marked executable, so with `mah` on your `PATH` you can run it directly:
`./structs.mahc`. The `.mahc` format is a normative, documented binary
format (`docs/MAHC_FORMAT.md` in the repo) — detailed enough to write a
VM for it in any language, which is exactly what the Rust runtime below
is.

## The Rust runtime (`mah-vm`)

`runtime/` is a second implementation of the `.mahc` machine, written in
Rust with **only the standard library** (no crates). The Python VM stays
the reference implementation: the Rust one prints the same output, fails
with the same messages and exit codes, and rejects the same malformed
files.

```sh
make vm                                   # cargo build --release -> mah-vm
mah run --vm rust prog.mh                 # compile with Python, run on mah-vm
mah runc --vm rust prog.mahc              # run compiled bytecode on mah-vm
mah build --self-contained prog.mh        # one executable file with mah-vm inside
```

In a project, `mah-project.toml` can set this per project (`[run] vm =
"rust"`, or `self-contained = true` on a `[[target]]`) — see
[Projects](/docs/projects).

### Self-contained executables

`mah build --self-contained prog.mh` writes one file that runs on a
machine without `mah` (or Python) installed, as long as it has the same
OS and CPU as the `mah-vm` it was built with. It's a `/bin/sh` script
with the runtime and the bytecode appended: on first run it checks the
platform, caches the runtime under `~/.cache/mah/vm/`, then runs itself
through it. Later runs, and other programs built with the same runtime,
reuse the cached copy. `mah dis` on a self-contained file prints which
runtime it carries before the usual disassembly.

### Numbers, precisely

A Mah `Number` is a base-10 decimal (28 significant digits,
round-half-even, matching Python's `decimal.Decimal`), and the Rust
runtime implements the same arithmetic from scratch on its own big
integers — including correctly-rounded `exp`/`ln`, so `**` with a
fractional exponent gives identical digits on both VMs. A change to the
language or the VM has to land in both implementations, with the Python
one as the reference.
