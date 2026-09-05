# The Mah Language

> A beautiful language

## Getting Started

```sh
# first generate language by running python ./compiler-generator/generate.py <input_file>

python ./compiler-generator/generate.py ./mah.lang

# or

make lang

# import parser, lexer, and ir_generator from compiler module and implement
# required actions

# then get the result how ever you want

python ./mah.py ./examples/input.mh

# or you can see the generated code with
python ./mah.py build ./examples/input.mh

# or to read from input.txt and write to output.txt, simply run:
python mah.py


```

## Examples:

To see the language in action, you can check out the `examples` folder.

```sh
# Prime numbers calculation
python mah.py examples/new_prime_numbers.mh

# String operations, concatenation, comparisons, and functions
python mah.py examples/strings.mh

# Importing another file
python mah.py examples/import_demo.mh
```

## Imports and exports

Modules are scoped: a file only shares the names it marks with `export`, and
another file brings them in with `import`. The `.mh` extension is optional in
import paths, which are resolved relative to the importing file.

Export declarations (or a bare name declared elsewhere):

```mah
# mathlib.mh
export def square(n) { return n ** 2 }
export let answer = 42

def helper() { return 1 }   # private: not visible to importers
export helper               # ...unless explicitly exported
```

Import into a namespace and access members with `.`:

```mah
import math from "mathlib"   # ".mh" optional

print(math.square(4))
print(math.answer)
```

Or import a module's exports directly into scope:

```mah
import "mathlib"

print(square(4))
```

Only `export`ed names are reachable; referencing a private or non-exported
member is a compile error. Each file is inlined at most once, so diamond
imports and cycles are safe, and errors inside an imported file are reported
with their originating `file:line:column`.



## Installing the `mah` command

Install the interpreter locally and expose a `mah` executable on your `PATH`:

```sh
# Installs into ~/.local/lib/mah and links ~/.local/bin/mah
make install-cli

# Remove it
make uninstall-cli
```

The install copies the interpreter into `$(PREFIX)/lib/mah` and symlinks
`$(PREFIX)/bin/mah` to it (`PREFIX` defaults to `~/.local`; make sure
`~/.local/bin` is on your `PATH`). Afterwards you can run programs from
anywhere:

```sh
mah path/to/program.mh
mah build path/to/program.mh
```

`make install` installs everything: the `mah` CLI, Neovim syntax highlighting,
and the language server.


## Syntax highlighting in neovim

You can automatically install or remove the syntax highlighting for Neovim via Makefile:

```sh
# Install syntax highlighting, parser, and filetype detection to Neovim
make install-nvim

# Remove syntax highlighting from Neovim
make uninstall-nvim
```

Alternatively, you can install the `syntax-highlight` folder as a Neovim plugin, then copy the `queries` folder inside of it into your Neovim config root.

![syntax highlight showcase](./examples/example.png)

## Language server (LSP)

Mah ships with a language server (`lsp/`) written in pure Python (standard
library only, no extra dependencies). It reuses the compiler pipeline to
provide:

- live diagnostics (compile errors) as you type, including errors inside
  imported files
- hover docs for keywords, builtins, functions and variables, showing any
  `#` doc comment written directly above the declaration
- go to definition (scope-aware: resolves parameters, locals, then globals),
  working across imports -- including namespace members (`math.square`), the
  namespace name itself, and the import path
- autocomplete-on-type for keywords, builtins, in-scope symbols, imported
  names and namespaces; typing `namespace.` lists that module's exports
- document symbols (functions and variables)
- a comment / uncomment code action for the selected lines

### Neovim

Install the server and the filetype hook that starts it for `*.mh` files:

```sh
# Install the language server + Neovim integration
make install-lsp

# Remove it
make uninstall-lsp
```

`make install-lsp` copies the server (and the compiler modules it needs) to
`<nvim-config>/mah-lsp/` and installs `<nvim-config>/ftplugin/mah.lua`, which
launches the server via `vim.lsp.start` for every Mah buffer. It uses
`python3` by default; set `$MAH_LSP_PYTHON` to choose a different interpreter.

With the server running, Neovim's built-in `vim.lsp.buf.definition` (mapped to
`grd`, or use `gd` in older configs) jumps to the declaration of the symbol
under the cursor, even when it lives in an imported file. Completion pops up
automatically as you type (autotrigger), comment toggling is offered as a code
action via `vim.lsp.buf.code_action`, and the buffer's `commentstring` is set
so built-in commenting (`gcc`) works too.

`make install` installs everything: the `mah` CLI, syntax highlighting, and
the language server.

### Other editors

Any LSP client can run the server directly over stdio:

```sh
python3 ./lsp/server.py
```

Point your editor's LSP client at that command for the `mah` filetype
(`.mh` files).
