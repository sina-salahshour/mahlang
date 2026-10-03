# {{name}}

A project written in **Mah**, a small dynamically typed language (`.mh`
files). Mah isn't a mainstream language, so don't guess its syntax from
Python, JavaScript, or Rust: **read `docs/mah-language.md` before writing or
changing any Mah code.** It's short and lists exactly what exists (including
`throw`/`try`/`catch`, the String methods and the standard library), plus
what doesn't (no tuples, no string interpolation, ...).

## Layout

| path | what |
|---|---|
| `mah-project.toml` | project manifest: name, version, entry point, build targets |
| `src/` | Mah source files |
| `src/main.mh` | entry point (the `entry` in the manifest); top-level code runs top to bottom |
| `src/*.test.mh` | tests, run by `mah test` (`src/main.test.mh` tests `src/main.mh`) |
| `docs/mah-language.md` | the complete Mah language reference |
| `build/` | compiled output from `mah build` (git-ignored) |

## Commands

Run these from the project directory (or any subdirectory):

```sh
mah run                     # compile and run the entry point
mah run other.mh            # run a specific file instead
mah run -- a "b c"          # everything after `--` is the program's arguments (std:process `args()`)
mah build                   # write every [[target]] from mah-project.toml
mah build --target release  # write one target
mah check                   # run the static type checker and print its diagnostics
mah test                    # run every test (*.test.mh); `mah test NAME` runs the matching ones
mah runc build/{{name}}.mahc   # run a compiled file (or just ./build/{{name}}.mahc; `-- ARGS` works here too)
mah dis build/{{name}}.mahc    # show the compiled bytecode
mah run --vm rust           # run on the native Rust runtime (if it's installed)
mah build --self-contained  # make every target standalone (runs without mah, same OS/CPU)
mah format                  # lay out every .mh file in the standard style
mah format --check          # list files that aren't formatted (exit 1 if any)
```

Run `mah format` after editing Mah code. It only changes whitespace and
refuses to touch a file with a syntax error.

A compile error (syntax, undefined name, wrong struct fields) is reported
before anything runs. A runtime error stops the program and says where it
happened, `at position #LINE:COL` (or `file.mh#LINE:COL` inside an imported
file; an error from inside the standard library is reported at your call to
it).

Check your changes with `mah test`. Tests live in `*.test.mh` files next to
the code they test: `test "name" { ... }` blocks using `assert`/`assert_eq`/
... from `std:test` (see "Testing" in `docs/mah-language.md`). A test file
`x.test.mh` can use `x.mh`'s non-exported functions and types too. Add or update a
test with every change, and make sure `mah test` passes.

## `mah-project.toml`

- `[package]`: `name`, `version` (yours to manage), `entry` (defaults to
  `src/main.mh`).
- `[[target]]`: one per build output, each with a unique `name`, a
  `profile` (`"debug"` or `"release"`), and an `out` path. Optional
  `self-contained = true` bundles the Rust runtime into the output, making a
  standalone executable (it needs `mah-vm` installed to build).
- `[run]`: `vm = "python"` (default) or `"rust"` picks the runtime `mah run`
  uses for the project; `mah run --vm ...` overrides it.
- `[types]`: `check = "loose"` (default) makes type mismatches editor
  warnings only; `"strict"` makes them compile errors for `mah run`/`mah
  build` too; `"explicit"` is `"strict"` plus every declaration whose type
  can't be inferred must be annotated. `mah check` reports at whichever
  level is set (`--level` overrides it for one run).
- `[dependencies]`: reserved for third-party packages, which aren't supported
  yet. Keep it empty.

## Writing Mah here

- Split code into files with `export fn` / `export let` / `export struct` /
  `export enum` / `export trait` and `import "file.mh"` (or `import name
  from "file"` for namespaced access, where a type is `name.Point`). Types
  are module-scoped like functions: only exported ones are visible, and two
  modules may each declare a `Request`. Paths are relative to the importing
  file. The standard library is imported the same
  way, as `"std:<name>"`: `std:math`, `std:path`, `std:json`, `std:csv`,
  `std:random`, `std:collections`, `std:regex`, `std:time`, `std:async`,
  `std:fs`, `std:process`, `std:reflect` (types, `##` docs and decorators at run time,
  which `json.decode` uses to read JSON into your structs) and `std:test` for
  tests (see `docs/mah-language.md`).
- Model data with `struct`/`enum` + `match`, and give behavior to types with
  `impl` blocks and traits. Implement `Printable` (`fn to_string(self)`) to
  control how a value prints.
- Keep `src/main.mh` as the entry point unless you also change `entry` in
  the manifest. Put other source files under `src/` too.
