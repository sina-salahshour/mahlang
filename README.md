# The Piaz Language

## how to run it?

```sh
# first generate language by running python ./generate_parser.py <input_file>

python ./generate_parser.py ./piaz.lang

# import parser, lexer, and ir_generator from compiler module and implement
# required actions

# then get the result how ever you want

python ./piaz.py ./examples/input.pz run

# or you can see the generated code with
python ./piaz.py ./examples/input.pz build

```
