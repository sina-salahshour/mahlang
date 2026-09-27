---
title: Getting started
order: 1
section: Start
---

Mah (ماه — "moon" in Persian) is a small, dynamically typed language with
closures, structs, enums, pattern matching, Rust-style traits,
block-scoped `defer`, and cooperative async. Source files end in `.mh`.

The whole toolchain — lexer, parser, resolver, bytecode compiler, VM,
formatter, and language server — is hand-written, pure Python, standard
library only. There's also a from-scratch Rust runtime (`mah-vm`) that
runs the exact same bytecode. Nothing to `pip install`.

## Running a file directly

From a repo checkout:

```sh
python -m mah run ./examples/prime_numbers.mh        # compile and run
python -m mah build ./examples/structs.mh             # -> structs.mahc
python -m mah runc ./examples/structs.mahc            # run compiled bytecode
python -m mah dis ./examples/structs.mahc              # show it disassembled
python -m mah format ./examples                        # rewrite .mh files
```

Once `mah` is installed on your `PATH` (see below), the same commands drop
the `python -m` prefix, and `mah <file>` (no subcommand) is shorthand for
`mah run <file>` (or `mah runc` for a `.mahc` file):

```sh
mah path/to/program.mh
mah build path/to/program.mh
```

## Installing the `mah` command

```sh
make install-mah      # installs into ~/.local/lib/mah, links ~/.local/bin/mah
                       # (run `make vm` first to install the Rust runtime too)
make uninstall-mah
```

`PREFIX` defaults to `~/.local` — make sure `~/.local/bin` is on your
`PATH`.

## Your first program

```mah
let x = 1            # declare
x = x + 1             # assign (the variable must already exist)
let y = {
    let t = x * 2
    t + 1              # tail: the block's value is 5
}
print(y)               # 5
```

A program is a sequence of statements run top to bottom — there's no
`main` function. Comments start with `#`. Newlines separate statements;
`;` is optional except in one case (see [Syntax basics](/docs/syntax-basics)).

## A project, not just a file

```sh
mah init my-app        # or `mah init` to turn the current directory into a project
cd my-app
mah run                # runs the entry point from mah-project.toml (src/main.mh)
mah build               # writes every [[target]], e.g. build/my-app.mahc
```

`mah init` also writes `docs/mah-language.md` (this same language
reference) and `AGENTS.md`/`CLAUDE.md`, so a coding agent working in the
project has an authoritative source for Mah's syntax without guessing
from Python, JavaScript, or Rust. See [Projects](/docs/projects).

## Where to go next

- [Syntax basics](/docs/syntax-basics) — values, operators, control flow
- [Functions & closures](/docs/functions-closures)
- [Structs](/docs/structs) and [Enums & pattern matching](/docs/enums-pattern-matching)
- [Projects](/docs/projects) — `mah-project.toml`, `mah init`/`run`/`build`
- [Tooling](/docs/tooling) — the LSP, VS Code, and Neovim

Or jump straight to `examples/*.mh` in the repo for runnable programs
covering structs, enums, traits, iterators, async, `defer`, closures,
recursion, imports, and number-base conversions.
