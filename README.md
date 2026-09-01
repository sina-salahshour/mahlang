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
```

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
