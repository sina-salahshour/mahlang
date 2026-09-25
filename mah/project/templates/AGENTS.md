# {{name}}

A project written in **Mah**, a small dynamically typed language (`.mh`
files). Mah isn't a mainstream language, so don't guess its syntax from
Python, JavaScript, or Rust: **read `docs/mah-language.md` before writing or
changing any Mah code.** It's short and lists exactly what exists, plus what
doesn't (no arrays, no `for` loop, no `<=`/`>=`, no string methods, ...).

## Layout

| path | what |
|---|---|
| `mah-project.toml` | project manifest: name, version, entry point, build targets |
| `src/` | Mah source files |
| `src/main.mh` | entry point (the `entry` in the manifest); top-level code runs top to bottom |
| `docs/mah-language.md` | the complete Mah language reference |
| `build/` | compiled output from `mah build` (git-ignored) |

## Commands

Run these from the project directory (or any subdirectory):

```sh
mah run                     # compile and run the entry point
mah run other.mh            # run a specific file instead
mah build                   # write every [[target]] from mah-project.toml
mah build --target release  # write one target
mah runc build/{{name}}.mahc   # run a compiled file
mah dis build/{{name}}.mahc    # show the compiled bytecode
```

A compile error (syntax, undefined name, wrong struct fields) is reported
before anything runs. A runtime error stops the program and says where it
happened, `at position #LINE:COL` (or `file.mh#LINE:COL` inside an imported
file). There's no test framework yet: check your changes by running the
program and reading its output.

## `mah-project.toml`

- `[package]`: `name`, `version` (yours to manage), `entry` (defaults to
  `src/main.mh`).
- `[[target]]`: one per build output, each with a unique `name`, a
  `profile` (`"debug"` or `"release"`), and an `out` path.
- `[dependencies]`: reserved for third-party packages, which aren't supported
  yet. Keep it empty.

## Writing Mah here

- Split code into files with `export fn` / `export let` and `import
  "file.mh"` (or `import name from "file"` for namespaced access). Paths are
  relative to the importing file.
- Model data with `struct`/`enum` + `match`, and give behavior to types with
  `impl` blocks and traits. Implement `Printable` (`fn to_string(self)`) to
  control how a value prints.
- Keep `src/main.mh` as the entry point unless you also change `entry` in
  the manifest. Put other source files under `src/` too.
