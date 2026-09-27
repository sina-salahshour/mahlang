---
title: Projects
order: 13
section: Tooling
---

```sh
mah init my-app        # or `mah init` to turn the current directory into a project
cd my-app
mah run                # runs the entry point from mah-project.toml (src/main.mh)
mah build               # writes every [[target]], e.g. build/my-app.mahc
mah build --target release
```

`mah init` creates:

- **`mah-project.toml`** — package name, version, entry point, and build
  targets.
- **`src/main.mh`** — the entry point.
- **`.gitignore`**.
- **`docs/mah-language.md`** and **`AGENTS.md`** (plus a `CLAUDE.md` that
  imports it) — a complete Mah language reference and project guide
  written so a coding agent can write correct Mah without guessing from
  another language.

`mah run` and `mah build` find the manifest from any subdirectory.

## `mah-project.toml`

```toml
[package]
name = "my-app"
version = "0.1.0"        # your project's own version
entry = "src/main.mh"

[[target]]
name = "my-app"
profile = "debug"
out = "build/my-app.mahc"

[run]
# vm = "rust"             # `mah run` uses the Rust runtime instead of Python

[types]
check = "loose"           # "loose" | "strict" | "explicit": see Types

[dependencies]
# reserved for third-party packages, not supported yet
```

- `[package]`: `name`, `version` (yours to manage), `entry` (defaults to
  `src/main.mh`).
- `[[target]]`: one per build output, each with a unique `name`, a
  `profile` (`"debug"` or `"release"`), and an `out` path. Optional
  `self-contained = true` bundles the Rust runtime into the output,
  making a standalone executable (needs `mah-vm` installed to build —
  see [Tooling](/docs/tooling)).
- `[run]`: `vm = "python"` (default) or `"rust"` picks the runtime `mah
  run` uses for the project; `mah run --vm ...` overrides it for one
  invocation.
- `[types]`: `check` sets how strict the static type checker is:
  `"loose"` (default, editor warnings only), `"strict"` (type errors fail
  `mah run`/`mah build`), or `"explicit"` (strict, plus annotations
  wherever a type can't be inferred). See [Types](/docs/types).
- `[dependencies]`: reserved for third-party packages, which aren't
  supported yet — keep it empty.

## Commands

Run these from the project directory (or any subdirectory):

```sh
mah run                     # compile and run the entry point
mah run other.mh            # run a specific file instead
mah build                   # write every [[target]] from mah-project.toml
mah build --target release  # write one target
mah runc build/my-app.mahc  # run a compiled file (or just ./build/my-app.mahc)
mah dis build/my-app.mahc   # show the compiled bytecode
mah run --vm rust           # run on the native Rust runtime (if installed)
mah build --self-contained  # make every target standalone (runs without mah, same OS/CPU)
mah format                  # lay out every .mh file in the standard style
mah format --check          # list files that aren't formatted (exit 1 if any)
mah check                   # run the static type checker at the project's level
```

A compile error (syntax, undefined name, wrong struct fields) is
reported before anything runs. A runtime error stops the program and
says where it happened, `at position #LINE:COL` (or `file.mh#LINE:COL`
inside an imported file).

## Self-contained executables

```sh
make vm                                              # build the Rust runtime (needs cargo)
mah build --self-contained ./examples/structs.mh -o structs
./structs                                             # one file, runs without mah installed
```

A self-contained build is a small shell script with the `mah-vm` runtime
and the compiled bytecode appended; it runs on machines with the same OS
and CPU it was built on. See [Tooling](/docs/tooling) for how the Rust
VM and bytecode format fit together.

## Keeping generated project docs in sync

`docs/mah-language.md` and `AGENTS.md` (the ones `mah init` writes into
new projects) are templates that live in `mah/project/templates/` in the
Mah repo itself, and must be kept in sync with the language as it grows
— every language change updates them alongside the code.
