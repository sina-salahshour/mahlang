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
# json5 = { github = "owner/repo", tag = "v1.0.0" }   # fetched by `mah install`
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
- `[dependencies]`: packages from GitHub repositories, fetched by `mah
  install` — see [Packages](#packages) below.

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
mah install                 # fetch [dependencies] into .mah/ and write mah-lock.toml
```

A compile error (syntax, undefined name, wrong struct fields) is
reported before anything runs. A runtime error stops the program and
says where it happened, `at position #LINE:COL` (or `file.mh#LINE:COL`
inside an imported file).

## Packages

A project can use Mah code from GitHub repositories. Declare each package
under `[dependencies]` with a local name:

```toml
[dependencies]
json5  = { github = "acme/mah-json5", tag = "v1.2.0" }
utils  = { github = "acme/monorepo", branch = "main", path = "packages/utils" }
pinned = { github = "acme/thing", rev = "0123456789abcdef0123456789abcdef01234567" }
latest = { github = "acme/other" }            # the default branch
```

`tag`, `branch` or a full 40-character `rev` choose the version (at most
one); `path` uses a subdirectory of the repository as the package. Then run
`mah install`: it fetches the packages (and the packages *they* declare)
into `.mah/packages/`, and writes `mah-lock.toml`, which pins the exact
commit and a content hash of each. Commit the lock; `.mah/` is git-ignored.

Import a package with the reserved `pkg:` prefix:

```
import json5 from "pkg:json5"          # the package's library file
import "pkg:json5/src/extra.mh"        # any file in it (.mh optional)
```

The library file is the package's `[package] lib` (default `src/lib.mh`), or
`lib.mh` for a directory without a `mah-project.toml`. Your code may import
only the packages your `[dependencies]` declare. Nothing is installed
automatically: a missing or out-of-date package is a compile error at the
import telling you to run `mah install`. A built `.mahc` contains the
package code, so it runs without `.mah/`.

- `mah install` keeps what the lock pins (offline, without git, when
  nothing changed), resolves only new or changed entries, reinstalls files
  edited by hand, and removes packages no longer needed.
- `mah install --update` moves every branch and tag to its current commit;
  `mah install --update NAME` just that package.
- `mah install --frozen` installs exactly the lock and fails if it's missing
  or out of date: use it in CI.

Fetching uses the `git` command. `GITHUB_TOKEN` is sent to github.com for
private repositories (your git credentials work too), and
`MAH_GITHUB_URL_BASE` points at a mirror or GitHub Enterprise. The full
reference, with every error message, is `docs/PACKAGES.md` in the Mah
repository.

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
