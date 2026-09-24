"""The `.mahc` portable bytecode format -- see docs/MAHC_FORMAT.md (the
normative spec) and this package's own module docstrings:

  format.py  -- constants: magic/version, section ids, opcode table,
                constant tags, built-in types, native arities.
  leb128.py  -- varuint/varint read/write helpers.
  program.py -- `Program`, a plain data model mirroring the file 1:1.
  lower.py   -- compiler IR (codegen.py's tuples) -> `Program`.
  encode.py  -- `Program` -> bytes.
  decode.py  -- bytes -> `Program`, with full load-time validation.
  disasm.py  -- `Program` -> human-readable disassembly text.

Only `format.py`/`program.py` (plus the stdlib) may be imported by
`mah/code_interpreter.py` (the VM) -- see that module's docstring for why.
"""
