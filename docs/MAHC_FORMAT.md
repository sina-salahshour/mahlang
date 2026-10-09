# The `.mahc` bytecode format (version 1.21)

`mah build prog.mh` compiles a program (its entry file plus everything it
imports) into a single `.mahc` file; `mah runc prog.mahc` runs one. This
document is the **normative** description of that file and of the machine
that executes it. It's written so a VM can be implemented in any language
from this document alone — nothing in it depends on the reference
implementation's language (Python) or its types.

Key words: **must** = required for a conforming VM/encoder; **should** =
recommended.

## 1. Design goals

- **Portable**: only Mah values and plain integers/UTF-8 strings appear in
  the file.
- **Compact**: variable-length integers everywhere, one shared string table,
  deduplicated constants, types referenced by index.
- **Extendable**:
  - host capabilities (I/O, files, sockets, string utilities, processes...)
    are **natives** referenced *by name*, so adding one never changes the
    format;
  - new opcodes, constant tags, and sections are added in **minor** versions;
  - optional sections can be skipped by VMs that don't understand them.
- **Fail fast**: a VM validates the whole file at load time and refuses to
  run a program that needs something it doesn't support (an unknown opcode or
  native, a newer major version), rather than failing half-way through.

## 2. Encoding primitives

| name | encoding |
|---|---|
| `u8` | one byte |
| `u16` | two bytes, little-endian |
| `varuint` | unsigned LEB128 (7 bits per byte, low groups first, high bit = "more bytes follow"); arbitrary size |
| `varint` | signed integer, zigzag-mapped (`n >= 0 → 2n`, `n < 0 → -2n-1`) then `varuint`; arbitrary size |
| `str` | `varuint` index into the string table (§4.1) |
| `str?` | `varuint`: `0` = absent, otherwise string index + 1 |
| `bytes(n)` | `n` raw bytes |

## 3. File layout

```
magic      bytes(4)  = 0x4D 0x41 0x48 0x43   ("MAHC")
major      u16       = 1
minor      u16       = 6          (0-5 for older files, up to 14; see §7)
sections   (id u8, length varuint, payload bytes(length))*   until end of file
```

- A file **may** start with a shebang line: bytes `#!` up to and
  including the first `\n`, placed before `magic`. It isn't part of the
  format; a VM **must** skip it. The reference `mah build` writes
  `#!/usr/bin/env -S mah runc\n` and marks the file executable, so a built
  program runs as `./prog.mahc`. A self-contained executable
  (`mah build --self-contained`) wraps a `.mahc` file together with a
  runtime; that container is described in docs/RUST_VM.md.
- A VM **must** reject a file whose `major` differs from the one it
  implements, and **should** reject one whose `minor` is greater than the
  one it implements (it may use opcodes/natives the VM doesn't know). When
  it does, it **should** still read that file's STRINGS and NATIVES
  sections (their layout is the same in every 1.x file) and name any
  natives it doesn't implement in its error, since those are the usual
  reason a newer file won't run. An encoder writes the lowest minor
  version whose features it uses. *(1.5)* The reference encoder writes 4
  (every file it produces has 1.4's TYPES layout and HANDLERS section),
  or the highest `(1.x)` marker among the natives the file lists (5 for
  the `std:math` natives, 7 for the ones behind `std:json`/`std:csv`, 8 for `std:random`'s, 9 for `std:regex`'s, 10 for `io.read_line`, 11 for `std:time`/`std:async`'s, 12 for `std:fs`'s, 13 for `std:process`'s, 14 for `std:reflect`'s, 17 for the `bytes.*` natives and the binary `fs.*` ones, 18 for `std:socket`'s, 19 for `socket.start_tls`, 20 for `socket.tls_server_config`/`socket.start_tls_server`), so a program that doesn't call newer natives
  still runs on an older VM. *(1.6)* Native methods (§6.7) are called by
  name, so it also writes the minor that added any native method whose
  *name* a `callmethod`/`callmethodkw`/`detachmethod`/`detachmethodkw` in
  the program's own code (outside the prelude) uses, whatever the
  receiver turns out to be; the prelude's `to_number`, which relies on 1.6
  methods, counts as one. *(1.14)* It also writes 14 when the code uses
  `loadtype`, `callspread`, `callmethodspread` or `spread`. *(1.15)* It
  writes 15 only when the code contains `decorate`. The META
  section (§4.10) doesn't count: it's optional, so a file whose only 1.14
  feature is META keeps its lower minor (older VMs skip the section).
  (`std:reflect`'s own functions never use `decorate`: `signature` and `schema`
  carry the decorators.) *(1.16)* It writes 16 when the code uses
  `paramhooks` or a `hooks.*` native, when any function's PARAMS has a rest
  flag (§4.5a), or when a `defmethod` names a function-item type
  `fn#<index>` (§6.7). `std:reflect` lists the `hooks.*` natives (its `find`,
  `construct` and hook helpers use them), so every program that imports it
  or `std:json`, and every program with a decorator (which imports
  `std:reflect` implicitly, docs/REFLECTION.md), is 16; one with none of
  these keeps its lower minor. *(1.17)* It writes 17 when the file lists a
  1.17 native (§4.4), when the program's own code (outside the prelude)
  calls a method named `to_bytes` (§6.7), or when the code has
  `loadtype 1, 8` (§5). `std:fs` declares the binary natives, so every
  program that imports it is 17, whether or not it uses them (as 1.16 is for
  `std:json` via `std:reflect`); a program with none of these keeps its lower
  minor. A `Bytes` annotation in META (§4.10) is written as primitive code 8
  only in a 1.17 file; in a lower-minor file it is written as `unknown`
  (tag 0), so that 1.14–1.16 VMs, which refuse code 8, still load it.
  *(1.18)* It writes 18 when the file lists a 1.18 native (§4.4), the seven
  `socket.*` ones; no opcode or method needs it. Only a program that imports
  `std:socket` lists them, so every other program keeps its lower minor
  (`std:fs` and `std:bytes` programs stay 17).
  *(1.19)* It writes 19 when the file lists `socket.start_tls`. std:socket
  declares it, so every program importing `std:socket` (or `std:http`, which
  imports it) is 1.19; `std:url` alone needs no newer VM.
  *(1.20)* It writes 20 when the file lists `socket.tls_server_config` or
  `socket.start_tls_server`. std:socket declares them, so every program
  importing `std:socket` (or `std:http`) is 1.20; `std:url` alone still
  needs no newer VM.
  *(1.21)* It writes 21 when the file uses a shared-variable or transaction
  opcode (`sharedget`, `sharedset`, `atomicbegin`, `atomicend`,
  `atomicabort`, `retry`: a program that declares a `shared let` or uses
  `atomic { }`, even without a `shared let`) or lists a `thread.*` native
  (every program importing `std:thread`).
- **Required sections**, each present exactly once and in increasing id
  order: STRINGS (`0x01`), CONSTANTS (`0x02`), TYPES (`0x03`), NATIVES
  (`0x04`), FUNCTIONS (`0x05`), CODE (`0x06`), — in files with minor ≥ 1 —
  PARAMS (`0x07`), and — in files with minor ≥ 4 — HANDLERS (`0x08`,
  §4.8). A 1.0 file **must not** contain PARAMS; a 1.1–1.3 file **must**
  contain it but **must not** contain HANDLERS; a 1.4 file **must**
  contain both.
- Ids `0x09`–`0x7F` are reserved for future *required* sections: a VM
  **must** reject a file containing one it doesn't know.
- Ids `0x80`–`0xFF` are *optional* sections, allowed after CODE in any
  order: a VM **must** skip ones it doesn't know (using `length`). `0x80` is
  DEBUG (§4.7); `0x81` is TESTS (§4.9), written only for `mah test`; `0x82`
  is META (§4.10, *(1.14)*), written after the other optional sections by
  every 1.14 encoder.
- A section's payload **must** be fully consumed by its own contents
  (trailing garbage inside a section is an error), and every index anywhere
  in the file **must** be in range; VMs validate this at load time.

## 4. Sections

### 4.1 STRINGS (`0x01`)
```
count varuint
count × (len varuint, bytes(len) UTF-8)
```
Every name (type, field, variant, method, trait, native, function) and
every string constant is an index into this table. Encoders should
deduplicate.

### 4.2 CONSTANTS (`0x02`)
```
count varuint
count × (tag u8, payload)
```

| tag | value | payload |
|---|---|---|
| `0` | `none` | — |
| `1` | `false` | — |
| `2` | `true` | — |
| `3` | integer Number | `varint` |
| `4` | non-integer Number | `str` — plain decimal text, `-?[0-9]+\.[0-9]+` (no exponent) |
| `5` | String | `str` |

Tags `6`–`255` are reserved for future versions (e.g. array/map literals).

### 4.3 TYPES (`0x03`)

User-declared structs and enums. Type **indices** at the start are built in
and implicit (never written in the section); user types are numbered from
the next free index in section order.

In a file with **minor < 4**, the built-ins are:

| index | type | variants (index: name {fields}) |
|---|---|---|
| `0` | `Option` | `0: none`, `1: some { value }` |
| `1` | `Promise` | `0: Pending`, `1: Settled { value }` |

and user types are numbered from `2`, as in 1.0–1.3.

In a file with **minor ≥ 4** *(1.4)*, `Promise` gains a third variant and
a new built-in enum `RuntimeError` (docs/ERRORS.md) is added at index 2:

| index | type | variants (index: name {fields}) |
|---|---|---|
| `0` | `Option` | `0: none`, `1: some { value }` |
| `1` | `Promise` | `0: Pending`, `1: Settled { value }`, `2: Failed { error }` |
| `2` | `RuntimeError` | `0: DivisionByZero { message }`, `1: TypeMismatch { message }`, `2: NoSuchField { message }`, `3: NoSuchMethod { message }`, `4: ArgumentError { message }`, `5: IndexOutOfRange { message }`, `6: MatchFailed { message }`, `7: InputError { message }`, `8: Internal { message }` |

and user types are numbered from `3`. (§7's "new built-in types at the
next free type indices" is exactly this: both VMs' decode/link honor the
file's own minor to know where user types start.)

```
count varuint
count × type
type   = kind u8, name str, body
  kind 0 (struct): nfields varuint, nfields × str          (field names, declaration order)
  kind 1 (enum):   nvariants varuint, nvariants × variant
variant = name str, nfields varuint, nfields × str
```

A struct and an enum may share a name; they're still distinct types.

**Display names** *(M41s, no format change)*. The compiler renames an
imported module's types and traits so that two modules can each declare a
`Request` (`__mah_m<index>_Request`, docs/V2_DESIGN.md's M41s milestone), so
a type name in this section (and a trait name in a `defmethod`, §6.7) may be
such a mangled name. **Rule: a type or trait name of the form
`__mah_m<digits>_<rest>` *displays* as `<rest>`.** Both VMs apply it
wherever a type or trait name reaches the user or Mah code: `to_string` of a
struct, enum or Type value (§5, §6.6), runtime error messages (§6.8) and
uncaught-error reports, `value.type_name`, what `std:reflect` returns (a
`Method`'s `trait_name`, a `TypeRef.Trait`'s `name`, `construct`'s
failures), `mah test` output, and `reflect.implements(T, "Name")`, which
compares a trait's display name. **Dispatch never uses it**: the method table
(§6.7), struct/enum equality of types, `matchstruct`/`matchenum` and
`throw`'s `Error` lookup all use the full name, so two modules' `Request`
types stay distinct. The rule only strips a prefix, so a file written before
M41s still runs on a newer VM, and an older VM merely shows the mangled name;
the version is unchanged. A disassembler shows the full name (it is a
debugging tool). The prelude's types and traits, and the built-ins, are never
renamed.

### 4.4 NATIVES (`0x04`)

The host functions this program uses — the extension point for everything
a VM provides beyond pure computation.

```
count varuint
count × (name str, arity varuint)
```

Instructions refer to natives by their index in this table. *(1.21)* The
natives listed in §6.11 "Refused natives" (all of `io`, `fs` and `socket`,
timers, Promise settling, `process.exit`/`run`/`env_set`/`env_remove` and the
`thread.*` operations that act on other threads) throw `ThreadError`
`in_atomic` when called while the task is in a transaction. A VM **must**
refuse to load a file listing a native it doesn't implement, or one whose
arity doesn't match its own. Native names are dotted, `module.function`.
Version 1.0 defines:

| name | arity | behavior |
|---|---|---|
| `io.print` | 1 | writes `to_string(v)` (§6.6) followed by `\n` to standard output; returns `none` |
| `io.write` | 1 | *(1.1)* writes `to_string(v)` (§6.6) to standard output with **no** newline; returns `none` |
| `io.input` | 0 | reads characters from standard input: skips characters until the first ASCII digit, then consumes digits up to and including the first non-digit (or end of input); returns that Number. End of input before any digit is a runtime error. *(1.10: no longer emitted, since `input` compiles to `io.read_line`; VMs keep it for older files.)* |
| `io.read_line` | 1 | *(1.10)* the async `input(prompt)`: writes the prompt (a String, else `RuntimeError.TypeMismatch`, `input: the prompt must be a String, got TYPE`) to standard output with no newline and flushes it, then returns a pending Promise that the scheduler (§6.4) settles with the next line of standard input as a String, without its `\n` or `\r\n`, or fails with an `EndOfInput` struct value (no fields; the prelude's type) when there are no more lines. Lines go to calls in call order. |
| `time.now_ms` | 0 | *(1.11)* the wall-clock time: whole milliseconds since 1970-01-01 00:00:00 UTC |
| `time.monotonic_ms` | 0 | *(1.11)* whole milliseconds since the VM started, from a clock that never goes backwards |
| `time.cancel` | 1 | *(1.11)* removes the pending timer (§6.4) that would settle the given Promise; the Promise then never settles, and the timer no longer keeps the program running. `true` if there was one |
| `promise.new` | 0 | *(1.11)* a new pending Promise, settled only by the two natives below |
| `promise.resolve` | 2 | *(1.11)* `resolve(p, value)`: if `p` is pending, settles it with `value`, running its continuations synchronously (§6.4), and gives `true`; otherwise does nothing and gives `false` |
| `promise.fail` | 2 | *(1.11)* `fail(p, error)`: likewise, failing `p` with `error` (each awaiting task throws it) |
| `fs.read_text` | 1 | *(1.12)* `path` → the whole file as text |
| `fs.write_text` / `fs.append_text` | 2 | *(1.12)* `path, text` → `none`; write replaces (creating), append adds (creating) |
| `fs.info` | 1 | *(1.12)* `path` → `[kind, size, modified]`: kind `"file"`, `"dir"` or `"other"` (following symlinks), size in bytes, modified in whole ms since 1970 (0 before it) |
| `fs.list_dir` | 1 | *(1.12)* `path` → the entry names, sorted by code point |
| `fs.mkdir` | 2 | *(1.12)* `path, parents` → `none`; with `parents` (`true`), missing ancestors too, and an existing directory is fine |
| `fs.remove` | 2 | *(1.12)* `path, recursive` → `none`: a file or symlink; a directory only if empty, or with `recursive` (`true`) the whole tree |
| `fs.rename` / `fs.copy` | 2 | *(1.12)* `from, to` → `none`, replacing a file at `to`; `copy` copies a file's bytes and refuses a directory (`is_a_directory`) |
| `fs.temp_dir` | 0 | *(1.12)* → the path of a new, empty directory in the system's temporary directory |
| `fs.open` | 2 | *(1.12)* `path, mode` → a file id (below); mode `"r"`, `"w"` (replacing) or `"a"` (appending), else `RuntimeError.ArgumentError` at once; `"r"` of a directory is `is_a_directory` |
| `fs.read_line` / `fs.read_all` | 1 | *(1.12)* `id` → the next line without its `\n` or `\r\n` (`none` at the end) / the rest of the file |
| `fs.write` | 2 | *(1.12)* `id, text` → `none` |
| `fs.close` | 1 | *(1.12)* `id` → `none`; closing a closed file does nothing |
| `process.args` | 0 | *(1.13)* → a new Vector of Strings: the program's arguments (`[]` when none) |
| `process.exit` | 1 | *(1.13)* `code` → never returns: ends the program at once with that exit code (below) |
| `process.env_get` | 1 | *(1.13)* `name` → the variable's value in the VM's environment table, or `none` |
| `process.env_set` | 2 | *(1.13)* `name, value` → `none`, setting it in the table |
| `process.env_remove` | 1 | *(1.13)* `name` → `none`, removing it (no error if absent) |
| `process.env_all` | 0 | *(1.13)* → a new Map String → String of the whole table, keys in sorted (code point) order |
| `process.cwd` | 0 | *(1.13)* → the current directory, as an absolute path |
| `process.pid` | 0 | *(1.13)* → this process's id |
| `process.platform` | 0 | *(1.13)* → `"linux"`, `"macos"`, `"windows"`, or else the OS's own name |
| `process.run` | 5 | *(1.13)* `program, args, cwd, env, stdin` → a Promise of a result (below): `[true, [code, stdout, stderr]]` |
| `reflect.type_of` | 1 | *(1.14)* `value` → the value's type as a `Type` value (§5): `none` is the primitive `None`, `some(x)` is `Option`, a struct or enum value its declared type, everything else its built-in type |
| `reflect.signature` | 1 | *(1.14)* `f` (a Function, else `TypeMismatch`, `reflect.signature: expected a Function, got TYPE`) → `[name or none, doc, type_params, params, returns, throws or none, decorators, rest, kwrest]`, each parameter `[name, type, doc, has_default, is_constant, constant, decorators]` (below; `decorators` *(1.15)* is a copy of what `decorate` stored for the function / parameter, `[]` if nothing). *(1.16)* It describes `f`'s *identity* (§6.7): a function a hook wrapped reports the function it wraps. `rest` and `kwrest` are the descriptors of the `...` and `**` parameters, or `none`; `params` doesn't list them |
| `reflect.schema` | 1 | *(1.14)* `t` (a Type, else `TypeMismatch`, `NAME: expected a Type, got TYPE`) → `none` for a primitive, else `["struct", t, doc, type_params, fields, decorators]` (a field is `[name, type, doc, decorators]`) or `["enum", t, doc, type_params, variants, decorators]` (a variant is `[name, doc, fields, decorators]`, its fields `[name, type, doc, []]`); `decorators` *(1.15)* are copies of what `decorate` stored for the type, field or variant |
| `reflect.methods` | 1 | *(1.14)* `t` → a new Vector of `[name, function, is_method, trait or none]`, one per Mah-code target in the method table (§6.7) under the type's name: the inherent ones sorted by name, then the trait ones sorted by trait name, then method name. Native targets (§6.7) aren't functions, so aren't listed |
| `reflect.implements` | 2 | *(1.14)* `t, trait_name` → Bool: whether the method table has any target, native or not, under that trait name for the type's name |
| `reflect.construct` | 2 | *(1.14)* `t, fields` (a Map with String keys) → `[true, value]` with a new struct value of type `t` whose fields, in declaration order, are `fields`' entries, or `[false, message]`: `can't construct T: it isn't a struct`, `T has no field 'f'` (checked first, in the Map's order), `missing field 'f' for T` |
| `reflect.decorators` | 3 | *(1.15)* `kind, a, b` (whole Numbers, else `TypeMismatch`, `reflect.decorators: NAME must be a whole Number, got TYPE`) → a new Vector holding a copy of the decorators `decorate` (§4.6) stored this run for that target, or `[]`. `b` only matters for kinds 1, 3, 4. A negative index has none. `signature`/`schema` don't call it: they append the same Vectors themselves |
| `reflect.construct_variant` | 3 | *(1.14)* `t, variant, fields` → likewise a new enum value (`none` for `Option`'s `none`): failures `can't construct a variant of T: it isn't an enum`, `can't construct a Promise`, `T has no variant 'v'`, `T.v has no field 'f'`, `missing field 'f' for T.v` |
| `hooks.has` | 2 | *(1.16)* `value, trait` (a String, else `TypeMismatch`, `hooks.has: the trait name must be a String, got TYPE`) → Bool: whether the type of `value` has a target registered under a trait whose *display* name (§4.3) is `trait` in the method table (§6.7). The type is `value`'s type name, or for a Function the item type `fn#<its identity>` only (not `Function`) |
| `hooks.adopt` | 2 | *(1.16)* `wrapper, original` (both Functions, else `TypeMismatch`, `hooks.adopt: expected two Functions, got TYPE and TYPE`) → `wrapper`, which now has `original`'s identity (§6.7); adopting an adopted closure takes the identity the original has |
| `hooks.same_fn` | 2 | *(1.16)* `a, b` → Bool: both are Functions with the same identity and the same defining frame (anything else is `false`, never an error) |
| `hooks.set_type` | 2 | *(1.16)* `t, data` (a Type naming a struct, else `TypeMismatch`, `NAME: expected a Type, got TYPE` / `hooks.set_type: T isn't a struct`) → `none`, storing `data` for that struct type (by its type name) |
| `hooks.set_param` | 3 | *(1.16)* `f, index, data` (a Function, else `TypeMismatch`, `hooks.set_param: expected a Function, got TYPE`; a whole Number ≥ 0, else `hooks.set_param: the parameter index must be a whole Number`) → `none`, storing `data` for parameter `index` of `f`'s identity, what `paramhooks` reads |
| `hooks.of` | 1 | *(1.16)* `value` → the data `hooks.set_type` stored for `value`'s type if `value` is a struct instance, else `none` |
| `hooks.get_field` | 2 | *(1.16)* `value, name` → the field's current value, read raw. `value` a struct or enum instance (else `TypeMismatch`, `hooks.get_field: expected a struct, got TYPE`), `name` a String naming one of its fields (`NoSuchField`, `hooks.get_field: 'T' has no field 'f'`) |
| `hooks.set_field` | 3 | *(1.16)* `value, name, new` → `none`, writing the field raw; the same checks (`hooks.set_field: ...`) |
| `bytes.new` | 2 | *(1.17)* `size, fill` → a new Bytes of `size` copies of the byte `fill`. `size` not a Number: `TypeMismatch`, `new: size must be a Number, got TYPE`; not a whole Number ≥ 0: `ArgumentError`, `new: size must be a whole number of at least 0, got N`; `fill` follows the byte rule (§6.9) with the prefix `new: fill` |
| `bytes.from_vector` | 1 | *(1.17)* `items` (a Vector, else `TypeMismatch`, `from_vector: items must be a Vector, got TYPE`) → a new Bytes of its items; each item follows the byte rule (§6.9) with the prefix `from_vector: item I`, `I` its 0-based index |
| `bytes.from_hex` | 1 | *(1.17)* `text` (a String, else `TypeMismatch`, `from_hex: text must be a String, got TYPE`) → `some(Bytes)`, two hex digits (either case) per byte; `none` unless `text` is an even number of hex digits (`""` gives `some` of empty Bytes) |
| `bytes.from_base64` | 1 | *(1.17)* `text` (a String, else `TypeMismatch`, `from_base64: text must be a String, got TYPE`) → `some(Bytes)` decoded with the standard alphabet (RFC 4648 §4), or `none` unless the length is a multiple of 4, every character is in the alphabet or is `=`, and `=` occurs only as the last 1 or 2 characters of the last group (`""` gives `some` of empty Bytes; whitespace is not allowed). The unused low bits of the last group are ignored (`Zh==` decodes like `Zg==`) |
| `fs.read_bytes` | 1 | *(1.17)* `path` → the whole file as a new Bytes, exactly as stored (a Promise of a result, like the other `fs.*` natives) |
| `fs.write_bytes` / `fs.append_bytes` | 2 | *(1.17)* `path, data` (Bytes, else `TypeMismatch`, `NAME: data must be Bytes, got TYPE`, with `NAME` `write_bytes` / `append_bytes`) → `none`; write replaces (creating), append adds (creating). The bytes are copied when the native is called, so later changes to `data` don't affect the write |
| `fs.file_read_bytes` | 2 | *(1.17)* `id, max` → a new Bytes of up to `max` bytes from the open file's current position, or all the rest when `max` is `none`; fewer than `max` only at the end of the file, and empty Bytes once it is reached. `max` neither a Number nor `none`: `TypeMismatch`, `file_read_bytes: max must be a Number or none, got TYPE`; not a whole Number ≥ 0: `ArgumentError`, `file_read_bytes: max must be a whole number of at least 0, got N`. A file opened for writing: failure kind `other`, `the file isn't open for reading` |
| `fs.file_write_bytes` | 2 | *(1.17)* `id, data` → `none`, writing the bytes as `fs.write` writes text (unbuffered); `data` not Bytes: `TypeMismatch`, `file_write_bytes: data must be Bytes, got TYPE`. A file opened for reading: failure kind `other`, `the file isn't open for writing` |
| `socket.connect` | 3 | *(1.18)* `host, port, timeout` → `[id, peer_host, peer_port, local_port]`: resolves `host` (DNS; IPv4 or IPv6), tries each address in order until one connects, the timeout covering the whole attempt; `peer_host` is the numeric address it connected to (`127.0.0.1`) and `local_port` its local port. `port` 0 is refused (`connect: port must be a whole number from 1 to 65535, got 0`) |
| `socket.listen` | 3 | *(1.18)* `host, port, backlog` → `[id, port]`, `port` the bound port (the real one when 0 was asked): binds `host:port` with `SO_REUSEADDR` on Unix, then listens with `backlog` |
| `socket.accept` | 2 | *(1.18)* `id, timeout` → `[id, peer_host, peer_port, local_port]` of the new socket, once a connection arrives; `id` must be a listener |
| `socket.send` | 2 | *(1.18)* `id, data` (Bytes, else `TypeMismatch`, `send: data must be Bytes, got TYPE`) → `none` once every byte is written. The bytes are copied when the native is called |
| `socket.recv` | 3 | *(1.18)* `id, max, timeout` → a new Bytes of 1 to `max` bytes (whatever is available, at most `max`), or empty Bytes once the peer has closed its side |
| `socket.shutdown` | 1 | *(1.18)* `id` → `none`, shutting down the write side (the peer's `recv` then reaches the end); reading still works |
| `socket.close` | 1 | *(1.18)* `id` → `none`; an unknown or already closed id is fine (`[true, none]`) |
| `socket.start_tls` | 3 | *(1.19)* `id, server_name, timeout` → `none` once a TLS client handshake on the open socket has finished; from then on `send`/`recv` on that id carry plaintext through TLS. See below |
| `socket.tls_server_config` | 2 | *(1.20)* `cert_path, key_path` → `id` (a socket-table id of kind TLS config) once the PEM certificate chain and private key have been read, parsed and checked to match. See below |
| `socket.start_tls_server` | 3 | *(1.20)* `id, config, timeout` → `none` once a TLS **server** handshake on the open socket `id`, with config `config`, has finished; from then on `send`/`recv`/`shutdown`/`close` behave as after `start_tls`. See below |
| `thread.spawn` | 3 | *(1.21)* `name` (String or `none`), `workers`, `capacity` (Number or `none`) → `[id, name]`: starts a thread of `workers` OS threads sharing one FIFO job queue (§6.11); `name` `none` becomes `thread-ID` |
| `thread.submit` | 3 | *(1.21)* a `Thread` struct, a function, a Vector of positional arguments → a pending Promise of the job (§6.11 "Queueing") |
| `thread.close` | 2 | *(1.21)* `id, cancel` (Bool) → `none`: the thread takes no more jobs; with `cancel` true, every queued job fails with `cancelled`, in queue order. Idempotent |
| `thread.join` | 1 | *(1.21)* `id` → a Promise of `none`, settled once every worker of the thread has exited (closes it first); throws `deadlock` when called from one of that thread's own jobs |
| `thread.pending` | 1 | *(1.21)* `id` → queued + running jobs, now |
| `thread.current` | 0 | *(1.21)* → `[0, "main"]` in the main VM, `[id, name]` of the job's thread in a job VM |
| `thread.cores` | 0 | *(1.21)* → how many threads the machine runs at once (at least 1); not reproducible across machines |
| `thread.semaphore_new` | 1 | *(1.21)* `permits` → a new semaphore id |
| `thread.semaphore_acquire` | 1 | *(1.21)* `id` → a Promise of `none`, settled when a permit is taken (FIFO) |
| `thread.semaphore_try_acquire` | 1 | *(1.21)* `id` → `true` (a permit taken) if one is free and nobody waits, else `false` |
| `thread.semaphore_release` | 1 | *(1.21)* `id` → `none`; hands the permit to the first waiter, or frees it; throws `over_release` when every permit is free |
| `thread.semaphore_available` | 1 | *(1.21)* `id` → free permits |
| `thread.channel_new` | 1 | *(1.21)* `capacity` (Number or `none`) → a new channel id |
| `thread.channel_send` | 2 | *(1.21)* `id, value` → a Promise of `none`; `value` is copied (strict, §6.11) first (throws `not_sendable`), then `closed` is thrown if the channel is closed |
| `thread.channel_recv` | 1 | *(1.21)* `id` → a Promise of the next message (fails with `closed` once the channel is closed and empty) |
| `thread.channel_try_recv` | 1 | *(1.21)* `id` → `some(message)` if one is queued or a sender waits, else `none` |
| `thread.channel_close` | 1 | *(1.21)* `id` → `none`; idempotent |
| `thread.channel_len` | 1 | *(1.21)* `id` → queued messages |
| `thread.channel_closed` | 1 | *(1.21)* `id` → whether `close` was called |
| `math.sin` | 1 | sine of a Number (radians); the result is computed in IEEE-754 double precision and converted to Number via its shortest round-trip decimal text |
| `math.cos` | 1 | cosine, same rules |
| `time.sleep_async` | 1 | returns a new pending Promise that the scheduler settles with `none` after the argument's number of milliseconds (§6.4) |
| `math.tan` | 1 | *(1.5)* tangent (radians) |
| `math.asin` | 1 | *(1.5)* arc sine, in radians |
| `math.acos` | 1 | *(1.5)* arc cosine, in radians |
| `math.atan` | 1 | *(1.5)* arc tangent, in radians |
| `math.atan2` | 2 | *(1.5)* `atan2(y, x)`: the angle of the point (x, y), in radians, correct in every quadrant |
| `math.exp` | 1 | *(1.5)* `e` raised to the argument |
| `math.log` | 1 | *(1.5)* natural logarithm |
| `math.log10` | 1 | *(1.5)* base-10 logarithm |
| `value.type_name` | 1 | *(1.7)* the argument's runtime type name, the one method dispatch uses: `Number`, `String`, `Bool`, `Vector`, `Map`, `Function`, or a struct's or enum's name (`Option` for `some(x)`, `Promise`) -- except `"None"` for `none` |
| `value.fields` | 1 | *(1.7)* a struct's or enum value's fields, as a new Map from field name to value in declaration order (`{value: x}` for `some(x)`, empty for a unit variant); `none` for anything else, `none` and Promises included |
| `value.variant` | 1 | *(1.7)* an enum value's variant name (`"some"` for `some(x)`); `none` for anything else, `none` itself included |
| `string.chars` | 1 | *(1.7)* a new Vector of the String's code points, each a one-code-point String |
| `string.code_point` | 1 | *(1.7)* the code point of a one-code-point String, as a Number |
| `string.from_code_point` | 1 | *(1.7)* the one-code-point String for a Unicode scalar value (a whole Number in 0–0x10FFFF, not 0xD800–0xDFFF) |
| `random.seed` | 1 | *(1.8)* a new generator state (below) from a whole Number seed with \|seed\| < 2^64, a negative one taken modulo 2^64 |
| `random.fresh` | 0 | *(1.8)* a new generator state seeded from 64 bits of the operating system's randomness |
| `random.next` | 1 | *(1.8)* advances the state and returns its next 64-bit output, a whole Number in [0, 2^64) |
| `random.below` | 2 | *(1.8)* `below(state, n)`: a uniformly distributed whole Number in [0, n), for a whole `n` from 1 to 2^64 |
| `regex.find` | 3 | *(1.9)* `find(source, text, start)`: the first match of canonical pattern `source` (below) in `text` starting at or after code point `start`, as its spans, or `none` |
| `regex.find_all` | 2 | *(1.9)* `find_all(source, text)`: every match, left to right, as a Vector of spans |

The `(1.5)` `math.*` natives follow `math.sin`'s rules: each argument
must be a Number (otherwise a `RuntimeError.TypeMismatch`, message
`NAME: expected a Number, got TYPE`, where NAME is the part after
`math.`), is converted to IEEE-754 double precision, and the result is
converted back via its shortest round-trip decimal text. A result that
isn't finite (a domain error such as `log(0)` or `asin(2)`, or an overflow
such as `exp(100000)`) is a `RuntimeError.ArgumentError` with the message
`NAME: argument out of range`. Programs reach them through the standard
library's `std:math` module (`extern fn`, docs/STDLIB.md).

The `(1.7)` natives are reached through `std:json` and `std:csv`. A
`string.*` argument of the wrong type is a `RuntimeError.TypeMismatch`,
`NAME: expected a String, got TYPE` (`from_code_point`: `expected a
Number`), with NAME the part after `string.`. `code_point` of a String
that isn't exactly one code point is an `ArgumentError`, `code_point:
expected one character, got N`; `from_code_point` of anything but a
Unicode scalar value is an `ArgumentError`, `from_code_point: not a
Unicode scalar value`. The `value.*` natives accept any value.

The `(1.8)` natives are `std:random`'s generator, the same on every VM so
a seeded sequence is too. A **generator state** is a Vector of four whole
Numbers in [0, 2^64), not all zero: the four 64-bit words `s0..s3` of
xoshiro256** (Blackman and Vigna). `random.next` and `random.below`
replace the Vector's items with the advanced state in place.
- Seeding: `x` = the seed modulo 2^64; each word in turn is the next
  splitmix64 output: `x += 0x9E3779B97F4A7C15; z = x; z = (z ^ (z >> 30))
  * 0xBF58476D1CE4E5B9; z = (z ^ (z >> 27)) * 0x94D049BB133111EB; word =
  z ^ (z >> 31)` (all arithmetic modulo 2^64).
- One step: `out = rotl(s1 * 5, 7) * 9; t = s1 << 17; s2 ^= s0; s3 ^= s1;
  s1 ^= s2; s0 ^= s3; s2 ^= t; s3 = rotl(s3, 45)`.
- `below(state, n)`: steps until `out < 2^64 - (2^64 mod n)`, then returns
  `out mod n` (so every value is equally likely).

Errors: a state that isn't one is an `ArgumentError`, `NAME: not a
generator state` (NAME `next` or `below`); a non-Number seed or bound is
a `TypeMismatch`, `seed: expected a Number, got TYPE` (likewise `below`);
a seed that isn't whole or is 2^64 or more in size is an `ArgumentError`,
`seed: expected a whole number smaller than 2^64 in size`; a bound out of
range, `below: expected a whole number from 1 to 2^64`.

The `(1.13)` natives are `std:process`'s. Each VM run has an **environment
table**, a snapshot of the process's environment taken when it starts
(entries whose name or value isn't valid Unicode are left out);
`process.env_*` read and write only that table, never the real environment.
A name must be a String that is non-empty and contains neither `=` nor NUL,
a value a String without NUL, else `RuntimeError.ArgumentError` at once
(`NAME: name must not be empty`, `NAME: name must not contain "="`, `NAME:
name must not contain a NUL character`, `NAME: value must not contain a NUL
character`); a non-String is a `TypeMismatch`, `NAME: name must be a String,
got TYPE`. **`process.exit`** takes a whole Number from 0 to 255, else
`ArgumentError`, `exit code must be a whole number from 0 to 255`; it flushes
standard output and ends the program with that exit status without running
pending `defer`s, and abandons tasks, timers and I/O; a `try`/`catch` can't
intercept it. In a test run (§6.10) the test ends as `failed` with the
message `the test called exit(N)` instead. **`process.run`** validates its
arguments at once (`program` a non-empty String, `args` a Vector of Strings,
`cwd` a String or `none`, `env` a Map of Strings or `none`, `stdin` a
String; none may contain NUL, and `env` names follow the rule above), then
returns a pending Promise settled off the VM's thread (§6.4). The program
is started directly (no shell; found through `PATH`) with the environment
table plus the `env` entries as its whole environment, `cwd` as its working
directory (`none`: this process's), and `stdin` written to a pipe that is
then closed (always a pipe, even for `""`); its stdout and stderr are
captured, decoded as UTF-8 with invalid bytes replaced by U+FFFD. The result
is `[true, [code, stdout, stderr]]`, `code` being the exit code, or 128 + N
if signal N killed it; a non-zero exit is not a failure. A program that
can't be started gives `[false, kind, description]` with kind `not_found`
(`no such file or directory`), `permission_denied` (`permission denied`) or
`other` (the OS's text). Program arguments come from the command line:
`mah run FILE -- ARGS...`, `mah runc FILE -- ARGS...` and `mah-vm run PATH
ARGS...`.

The `(1.14)` natives are `std:reflect`'s (docs/REFLECTION.md); they read
the file's own structure, so they run synchronously and are the only natives
that need the loaded program: its TYPES, FUNCTIONS, CONSTANTS, META (§4.10)
and the method table. A **type descriptor** crosses as a Vector `[tag, ...]`
mirroring the META `typeref`: `[0]` unknown, `[1, type, args]` named (a
`Type` value, and a Vector of descriptors), `[2, params, returns, throws]`
function (`throws` is `none` or a Vector), `[3, name]` type parameter, `[4]`
`Self`, `[5]` `Never`, `[6, name, args]` trait. In a `signature` parameter,
`is_constant` says `constant` holds the default's value (else it's `none`);
`has_default` comes from PARAMS. **Without META**, or for a function or type
it has no information on (`has_meta` 0), every descriptor is `[0]`, every
doc `""`, `type_params` `[]`, `throws` `none`, and no default is constant;
names, `has_default` and the shape of structs and enums (from TYPES) are
still reported. `reflect.construct*` don't run hooks or validate types; the
built-in enums `Option` and `RuntimeError` may be constructed too.

The `(1.12)` natives are `std:fs`'s. Each returns a pending Promise at once
and does its blocking work off the VM's thread of execution, as an I/O
operation (§6.4), settling it with a **result**: `[true, value]` (the
value the table lists), or `[false, kind, description]`, which std:fs
turns into an `FsError`. `kind` is `not_found`, `permission_denied`,
`already_exists`, `is_a_directory`, `not_a_directory`,
`directory_not_empty`, `invalid_utf8` or `closed`, each with a fixed
description (`no such file or directory`, `permission denied`, `already
exists`, `is a directory`, `not a directory`, `directory not empty`, `not
valid UTF-8 text`, `the file is closed`), or `other`, whose description
is the OS's own error text (strerror). Reading a file opened for writing,
or writing one opened for reading, is `other` with `the file isn't open
for reading` / `... for writing`. Text is UTF-8, strictly decoded and
written byte for byte (no newline translation); writes to an open file
are unbuffered. **Open files** live in the VM's handle table: `fs.open`
gives a positive whole-Number id, and the file natives take it back; an
id that isn't open (closed, or never opened) settles with `[false,
"closed", ...]`. A non-String path or text, or a non-Number id, is a
`RuntimeError.TypeMismatch` at once (`NAME: path must be a String, got
TYPE`; `NAME: expected a file id, got TYPE`).

*(1.17)* The binary `fs.*` natives use the same results, failure kinds and
argument checks, but move bytes as they are: no UTF-8 decoding (so no `not
valid UTF-8 text` failure) and no newline translation. `fs.file_write_bytes`
writes at the end of what has been written, like `fs.write`. A file has one
position, shared by all reads, so text reads (`fs.read_line`,
`fs.read_all`) and `fs.file_read_bytes` may be mixed on one open file, each
continuing where the last stopped. The Bytes a native returns is new, and
one it is given isn't kept.

The `(1.18)` natives are `std:socket`'s (TCP only). Each returns a pending
Promise at once and does its work off the VM's thread of execution, as an I/O
operation (§6.4), settling it with a **result** exactly like the `(1.12)`
natives: `[true, value]` (the value the table lists) or `[false, kind,
description]`, which std:socket turns into a `SocketError`. **Sockets and
listeners** are positive whole-Number ids in a per-VM **socket table**,
numbered from 1 and separate from the file table; the VM closes every open
one when it finishes. Sends and receives on one socket may run at the same
time from different tasks. The VM's thread checks the arguments **before**
anything else and raises these as RuntimeErrors at once
(`NAME` is the part after `socket.`; numbers are formatted as `print` shows
them):

- `host` not a String: `TypeMismatch`, `NAME: host must be a String, got TYPE`.
- `port` not a Number: `TypeMismatch`, `NAME: port must be a Number, got
  TYPE`; not a whole Number from 0 to 65535: `ArgumentError`, `NAME: port must
  be a whole number from 0 to 65535, got N`. `connect` refuses 0 the same way,
  with `from 1 to 65535`.
- `timeout` neither `none` nor a Number: `TypeMismatch`, `NAME: timeout must
  be a Number or none, got TYPE`; not a whole Number ≥ 0: `ArgumentError`,
  `NAME: timeout must be a whole number of at least 0, got N`. A timeout is in
  milliseconds; `none` waits forever and 0 means only what is already there.
- `backlog` not a Number: `TypeMismatch`, `listen: backlog must be a Number,
  got TYPE`; not a whole Number ≥ 1: `ArgumentError`, `listen: backlog must be
  a whole number of at least 1, got N`.
- `id` not a Number: `TypeMismatch`, `NAME: expected a socket id, got TYPE`.
- `data` not Bytes: `TypeMismatch`, `send: data must be Bytes, got TYPE`.
- `max` not a Number: `TypeMismatch`, `recv: max must be a Number, got TYPE`;
  not a whole Number ≥ 1: `ArgumentError`, `recv: max must be a whole number
  of at least 1, got N`.

An id that isn't open (closed, never opened, or of the wrong kind: a
listener id given to `recv`, a socket id given to `accept`) settles at once
with `[false, "closed", "the socket is closed"]`. The failure kinds, each with
a fixed description: `connection_refused` (`connection refused`),
`connection_reset` (`connection reset by peer`; also a broken pipe or an
aborted connection), `timed_out` (`timed out`), `address_in_use` (`address
already in use`), `address_not_available` (`address not available`),
`host_not_found` (`host not found`: the name didn't resolve),
`permission_denied` (`permission denied`), `closed` (`the socket is closed`),
or `other`, whose description is the OS's own error text without a trailing
` (os error N)`. **Waiting and closing**: a worker waiting in `accept`, `recv`
or `connect` polls in slices of at most 50 ms, and between slices checks its
deadline (`timed_out`) and whether its id was closed. `socket.close` removes
the id from the table at once, on the VM's thread, and then closes the OS
socket off it, so a call already waiting on that id fails with `closed` within
about 50 ms; a `send` in progress may fail with `closed` or `connection_reset`.
A pending `accept` or `recv` is an I/O operation, so it keeps the program
running (§6.4) until it settles.

The `(1.19)` native `socket.start_tls` makes an open socket a **TLS client**
(TLS 1.2 or 1.3, no client certificate, no ALPN). The server's certificate
must be valid now, name `server_name` (a DNS name or an IP address), and chain
to a trusted root: the roots in the PEM file named by the `SSL_CERT_FILE`
environment variable when it is set (read at each call), else the VM's
built-in set (the Python VM uses the platform's, the Rust VM Mozilla's). A
self-signed CA certificate used as the server's own isn't accepted
everywhere; a server certificate signed by a trusted CA is. Arguments are
checked like the other socket natives' (`server_name` not a String:
`TypeMismatch`, `start_tls: server name must be a String, got TYPE`; the
`timeout` rules above, covering the whole handshake). An id that isn't an
open socket settles with `closed`. Two more failure kinds:
`tls_certificate` (`the server's certificate isn't trusted`: verification
failed, including a name that doesn't match) and `tls` (`the TLS handshake or
connection failed`: anything else, including a peer that doesn't speak TLS
and calling `start_tls` again on a TLS socket). The peer closing during the
handshake is `connection_reset`. After the handshake, `recv` returns
decrypted bytes, and a peer that closes **without** TLS's close notice is a
normal end (empty Bytes), as is one that sends it; `shutdown` ends the
sending side of the TCP connection without sending a close notice, and
`close` closes it. A send and a recv on one TLS socket may still run at the
same time (they take turns at the TLS state, holding it at most one 50 ms
slice).

The `(1.20)` natives make TLS **servers**. `socket.tls_server_config` reads
the PEM certificate chain at `cert_path` (the server's own certificate first)
and the unencrypted PEM private key at `key_path` (PKCS#8, PKCS#1 RSA or SEC1
EC), checks that they match, and adds them to the socket table as a **TLS
config** id. That id is "not open" for every other socket native (they settle
with `closed`, as a listener id does for `recv`); `socket.close` on it removes
it (`none`, and closing twice is fine). Argument checks, on the VM's thread:
`TypeMismatch`, `tls_server_config: certificate path must be a String, got
TYPE` and `tls_server_config: key path must be a String, got TYPE`. Its
failures are kind `tls_config`, whose description, unlike other kinds' (but
`other`), is one of a fixed set, the first that applies in this order:
`can't read the certificate file` (opening or reading `cert_path` fails), `can't
read the private key file` (likewise `key_path`), `no certificate in the
certificate file` (the bytes lack `-----BEGIN CERTIFICATE-----`), `the private
key is encrypted` (the key bytes contain `-----BEGIN ENCRYPTED PRIVATE
KEY-----` or `Proc-Type: 4,ENCRYPTED`), `no private key in the key file`
(they contain none of `-----BEGIN PRIVATE KEY-----`, `-----BEGIN RSA PRIVATE
KEY-----`, `-----BEGIN EC PRIVATE KEY-----`), `the private key doesn't match
the certificate`, and `the certificate or private key isn't usable` (any other
parse or load failure). The third to fifth are a plain byte search, done the
same way by every VM before the TLS library sees the data. RSA (2048 bits and
up) and ECDSA P-256 keys are supported; other key types may load on one VM
only.

`socket.start_tls_server` runs a TLS server handshake (TLS 1.2 or 1.3, one
certificate: no client certificates, no SNI handling, no ALPN) on the open
socket `id` with the TLS config `config`. Checks: `id` not a Number is
`TypeMismatch`, `start_tls_server: expected a socket id, got TYPE`; `config`
not a Number, `start_tls_server: expected a TLS config id, got TYPE`; then the
`timeout` rules above, covering the whole handshake. It settles at once with
`closed` when `id` isn't an open socket or `config` isn't an open TLS config
(fractional and negative ids are never open). Its failures are `start_tls`'s:
`tls` (a client that doesn't speak TLS, a handshake alert such as a client
that doesn't trust the certificate, or calling it on a socket that already
uses TLS), `connection_reset` (the client closed during the handshake),
`timed_out` and `closed`. Afterwards the socket is exactly like a `start_tls`
one: a client closing without a close notice is a normal end, and `shutdown`
sends none.

The `(1.21)` natives are `std:thread`'s (docs/STDLIB.md, M44); §6.11 is
their model. Thread, semaphore and channel ids are three separate counters,
each from 1 per run; the main thread's id is 0. A bad id (only reachable by a
hand-built struct) is `RuntimeError.ArgumentError`, `thread: no such thread
N` / `thread: no such semaphore N` / `thread: no such channel N`.
`thread.submit` checks its first argument is a struct whose display type name
is `Thread` with a Number field `id` (else `RuntimeError.TypeMismatch`,
`detach(...) needs a thread.Thread to run on, got T`) and its second is a
function (else `TypeMismatch`, `thread.run: f must be a function, got T`).
Natives **throw** `ThreadError` values (the prelude struct `ThreadError {
kind, message }`, §6.11) as ordinary thrown values, catchable at the call.

- **Semaphores**: `acquire` takes a permit at once when one is free and
  nobody waits (a settled Promise), else queues a waiter (a pending
  Promise). `try_acquire` never waits. `release` passes the permit to the
  first waiter if there is one, else frees it; with every permit free it
  throws `over_release` (`release without a matching acquire (all N permits
  are free)`). Permits have no owner.
- **Channels**: `send` copies the value first (refusal: throw
  `not_sendable` synchronously), then: closed → throw `closed` (`the channel
  is closed`); a receiver waits → hand it the message (settled `none`); the
  channel has room (`capacity` `none`, or fewer than `capacity` queued) →
  queue it (settled `none`); else wait as a sender holding its message (a
  pending Promise, settled with `none` when the message is queued or taken,
  failed with `closed` if the channel closes first). `recv`: a queued
  message → take the oldest (and move the first waiting sender's message
  into the queue, settling that sender); else a waiting sender (capacity 0)
  → take its message and settle it; else closed → a Promise failed with
  `closed`; else wait as a receiver. `try_recv` is `recv`'s first two
  branches without waiting, else `none`. `close` is idempotent: every
  waiting receiver and sender fails with `closed` (a waiting sender's
  message is dropped); queued messages stay receivable.

The `(1.11)` natives are `std:time`'s clocks and the building blocks of
`std:async`. A non-Promise argument to `time.cancel`, `promise.resolve`
or `promise.fail` is a `RuntimeError.TypeMismatch`, `NAME: expected a
Promise, got TYPE` (NAME `cancel`, `resolve` or `fail`). Settling a
Promise from inside a running task resumes the tasks waiting on it right
then, before the settling native returns, exactly as a `detach` runs its
new task at once.

The `(1.9)` natives are `std:regex`'s matcher. `std:regex` parses every
pattern itself (in Mah, so its syntax errors are the same everywhere) and
hands these natives a **canonical pattern** whose every construct means
the same thing to the two engines behind the reference VMs, Python's `re`
and Rust's `regex` crate. Its syntax is the `regex` crate's, restricted
to:
- literal characters, with `\ . + * ? ( ) | [ ] { } ^ $ # & - ~` written
  as `\` plus the character (and no other escapes of literals);
- `.` (any character but `\n`) and `(?s:.)` (any character);
- classes `[...]` / `[^...]` of literal characters and ranges `a-b`
  between Unicode scalar values, with `\ ] [ ^ - & ~` escaped;
- anchors `^`, `\A`, `\z` (end of text only), `(?m:^)`, `(?m:$)`, and
  the ASCII word boundaries `(?-u:\b)` and `(?-u:\B)`;
- groups `( )`, `(?: )` and `(?P<name> )`, alternation `|`, and repeats
  `* + ? {n} {n,} {n,m}` (n, m at most 1000), each optionally lazy (`?`),
  never applied twice in a row, nor to a capturing group that can match
  the empty string when the repeat's maximum is above 1 (the engines
  capture different things there).

So `\d`, `\w`, `\s`, case-insensitivity and the like are already spelled
out as classes. A VM matches it with leftmost-first (Perl-style)
semantics. Python's `re` needs three spellings changed, with the pattern
compiled under `re.ASCII`: `(?-u:\b)` becomes `\b`, `(?-u:\B)` becomes
`(?:(?<=\w)(?=\w)|(?<!\w)(?!\w))` (`re`'s own `\B` never matches in an
empty string), and `\z` becomes `\Z`.
- **Spans**: a Vector `[start0, end0, start1, end1, ...]`, the whole
  match then each capture group in order of its `(`, as code-point
  positions; `none, none` for a group that took no part.
- **`find_all`'s iteration**: search from position 0; after a match
  ending at `e`, search again from `e`, or from `e + 1` after an empty
  match, until no match is found or the position passes the end. A search
  from a later position still sees the text before it (`^`, `\b`).
- **Errors**: `source` or `text` not a String is a `TypeMismatch`
  (`NAME: expected a String, got TYPE`); a `source` the engine can't
  compile, an `ArgumentError`, `NAME: not a canonical pattern`; a `start`
  that isn't a whole Number from 0 to the text's length, an
  `ArgumentError`, `find: start must be a position in the text`. VMs may
  cache compiled patterns.

**Adding natives** (files, sockets, string utilities, processes, ...) is
the intended way to grow the platform, e.g. `fs.read`, `fs.write`,
`fs.delete`, `fs.list_dir`, `net.connect`, `string.split`,
`string.replace`, `os.run`. A new native is added in a minor version: it
gets a name, an arity, and a behavior written in the table above; no opcode
or section changes. A host **may** deliberately leave natives out (a
sandboxed VM without `fs.*`), and then refuses such programs at load time.

### 4.5 FUNCTIONS (`0x05`)
```
count varuint              (≥ 1)
count × (entry varuint, slot_count varuint, param_count varuint, name str?)
```

`entry` is an instruction index into CODE. Function `0` is the **main
program**: entry `0`, `param_count` `0`, and its `slot_count` is the size of
the global frame. `name` is used only to print function values and in error
messages.

### 4.5a PARAMS (`0x07`, required from 1.1)
```
for each function, in FUNCTIONS order:
  nparams varuint                  (must equal that function's param_count)
  nparams × (name str, flags u8)   flags bit 0 = the parameter has a default;
                                   bit 1 = (1.16) the `...` rest parameter;
                                   bit 2 = (1.16) the `**` rest parameter;
                                   all other bits must be 0
```
Parameter names are needed to bind keyword arguments, and default flags to
know which parameters may be left unbound (§6.1). Function 0 has 0
parameters. In a 1.0 file (no PARAMS), parameters are unnamed and have no
defaults.

*(1.16)* A **rest parameter** collects the arguments no ordinary parameter
takes (§6.1): bit 1 marks the positional one (a Vector of the extra
positional arguments), bit 2 the keyword one (a Map of the unmatched
keyword arguments). A parameter has at most one of the two bits and no
default; bit 2 is only allowed on the last parameter, and bit 1 on the last
parameter or the one before a bit-2 parameter, so the rest parameters are
always the last one or two, `...` before `**`. They are ordinary slots: they
count in `param_count` and in `Function.arity()`. In a file whose minor is
below 16 only bit 0 is defined. The load-time messages (the same on both VMs,
`F` the function's FUNCTIONS index, `P` the parameter's index): `PARAMS:
invalid flags byte N (only bit 0 is defined)` (below 1.16) or `(only bits
0-2 are defined)`; `PARAMS: function F parameter P is flagged as both a '...'
and a '**' rest parameter`; `PARAMS: function F parameter P is a rest
parameter and can't have a default`; `PARAMS: function F: the '**' rest
parameter must be the last parameter`; `PARAMS: function F: the '...' rest
parameter must be the last parameter or the one before the '**' rest
parameter`.

### 4.6 CODE (`0x06`)
```
count varuint
count × (opcode u8, operands...)
```

Instructions are numbered from `0`; jump targets and function entries are
these indices. Operand kinds:

| kind | encoding | meaning |
|---|---|---|
| `A` | `depth varuint, slot varuint` | a frame slot: start at the current frame, follow `static_parent` `depth` times, take slot `slot` |
| `A?` | `varuint d`; if `d > 0`, then `slot varuint` (depth = `d-1`) | optional address |
| `A*` | `count varuint, count × A` | address list |
| `S*` | `count varuint, count × str` | *(1.1)* string list (keyword-argument names) |
| `K` | `varuint` | constant index |
| `S` / `S?` | `str` / `str?` | string index |
| `L` | `varuint` | instruction index (jump target) |
| `F` | `varuint` | function index |
| `T` | `varuint` | type index |
| `N` | `varuint` | small unsigned integer (e.g. a variant index) |
| `X` | `varuint` | native index |
| `B` | `u8` | boolean flag, `0` or `1` |

Opcodes (semantics in §6):

| op | name | operands |
|---|---|---|
| `0x00` | `halt` | — |
| `0x01` | `move` | src `A`, dest `A` |
| `0x02` | `loadk` | const `K`, dest `A` |
| `0x03` | `jmp` | target `L` |
| `0x04` | `jmpf` | cond `A`, target `L` |
| `0x05` | `jmpset` *(1.1)* | param `A`, target `L` |
| `0x10` | `add` | a `A`, b `A`, dest `A` |
| `0x11` | `sub` | a, b, dest |
| `0x12` | `mul` | a, b, dest |
| `0x13` | `div` | a, b, dest |
| `0x14` | `idiv` | a, b, dest |
| `0x15` | `mod` | a, b, dest |
| `0x16` | `pow` | a, b, dest |
| `0x17` | `eq` | a, b, dest |
| `0x18` | `neq` | a, b, dest |
| `0x19` | `lt` | a, b, dest |
| `0x1A` | `gt` | a, b, dest |
| `0x1B` | `and` | a, b, dest |
| `0x1C` | `or` | a, b, dest |
| `0x1D` | `neg` | a `A`, dest `A` |
| `0x1E` | `le` *(1.2)* | a, b, dest |
| `0x1F` | `ge` *(1.2)* | a, b, dest |
| `0x0F` | `not` *(1.2)* | a `A`, dest `A` |
| `0x20` | `closure` | fn `F`, dest `A` |
| `0x21` | `call` | callee `A`, args `A*` |
| `0x22` | `ret` | value `A` |
| `0x23` | `retval` | dest `A` |
| `0x24` | `callmethod` | recv `A`, name `S`, args `A*`, trait `S?` |
| `0x25` | `defmethod` | closure `A`, type_name `S`, trait `S?`, name `S`, is_method `B` |
| `0x26` | `callkw` *(1.1)* | callee `A`, args `A*`, kwnames `S*` |
| `0x27` | `callmethodkw` *(1.1)* | recv `A`, name `S`, args `A*`, kwnames `S*`, trait `S?` |
| `0x28` | `detach` | callee `A`, args `A*`, dest `A` |
| `0x29` | `detachmethod` | recv `A`, name `S`, args `A*`, trait `S?`, dest `A` |
| `0x2A` | `await` | promise `A`, dest `A` |
| `0x2B` | `detachkw` *(1.1)* | callee `A`, args `A*`, kwnames `S*`, dest `A` |
| `0x2C` | `detachmethodkw` *(1.1)* | recv `A`, name `S`, args `A*`, kwnames `S*`, trait `S?`, dest `A` |
| `0x2D` | `callspread` *(1.14)* | callee `A`, args `A`, kwargs `A` |
| `0x2E` | `callmethodspread` *(1.14)* | recv `A`, name `S`, args `A`, kwargs `A`, trait `S?` |
| `0x30` | `struct` | type `T`, values `A*` (declaration order), dest `A` |
| `0x31` | `enum` | type `T`, variant `N`, values `A*` (declaration order), dest `A` |
| `0x32` | `getfield` | obj `A`, field `S`, dest `A` |
| `0x33` | `setfield` | obj `A`, field `S`, src `A` |
| `0x34` | `matchstruct` | value `A`, type `T`, dest `A` |
| `0x35` | `matchenum` | value `A`, type `T`, variant `N`, dest `A` |
| `0x36` | `matchfail` | — |
| `0x37` | `matchrange` *(1.2)* | value `A`, lo `A?`, hi `A?`, inclusive `B`, dest `A` |
| `0x38` | `vector` *(1.3)* | items `A*`, dest `A` |
| `0x39` | `map` *(1.3)* | pairs `A*` (`k1 v1 k2 v2 …`), dest `A` |
| `0x3A` | `matchtype` *(1.4)* | value `A`, type `T`, dest `A` |
| `0x3B` | `loadtype` *(1.14)* | kind `N`, index `N`, dest `A` |
| `0x3C` | `spread` *(1.14)* | target `A`, source `A`, keyword `B` |
| `0x3D` | `decorate` *(1.15)* | kind `N`, a `N`, b `N`, values `A*` |
| `0x3E` | `paramhooks` *(1.16)* | function `N`, param `N`, dest `A` |
| `0x40` | `deferpush` | — |
| `0x41` | `deferadd` | closure `A` |
| `0x42` | `deferpeek` | dest `A` |
| `0x43` | `deferpop` | dest `A` |
| `0x44` | `deferscopepop` | — |
| `0x45` | `deferdepth` *(1.4)* | dest `A` |
| `0x46` | `deferabove` *(1.4)* | depth `A`, dest `A` |
| `0x50` | `native` | fn `X`, args `A*`, dest `A?` |
| `0x60` | `throw` *(1.4)* | value `A` |
| `0x70` | `sharedget` *(1.21)* | index `N`, name `S`, mode `N`, dest `A` |
| `0x71` | `sharedset` *(1.21)* | index `N`, name `S`, src `A` |
| `0x74` | `atomicbegin` *(1.21)* | — |
| `0x75` | `atomicend` *(1.21)* | — |
| `0x76` | `atomicabort` *(1.21)* | — |
| `0x77` | `retry` *(1.21)* | — |

All other opcode values are reserved. Opcodes, operand kinds, and natives
marked *(1.1)* **must not** appear in a file whose minor version is 0,
those marked *(1.2)* not in one whose minor version is below 2, those
marked *(1.3)* not in one whose minor version is below 3, and those
marked *(1.4)* not in one whose minor version is below 4, and those marked
*(1.14)* not in one whose minor version is below 14, and those marked
*(1.15)* not in one whose minor version is below 15, and those marked
*(1.16)* not in one whose minor version is below 16, and those marked
*(1.21)* not in one whose minor version is below 21 (`opcode 'NAME' at
instruction I requires minor version >= 21, but this file's minor version is
M`).

The `shared*` opcodes *(1.21)* implement shared variables and the
`atomic*`/`retry` opcodes implement transactions (§6.11). `index` is the
shared variable's number (any varuint; a program numbers its shared
variables from 0); `name` is its source name, used in error messages and
transaction entries. `sharedget`'s mode is 0 (**copy**) or 1 (**working**:
the read is written lexically inside `atomic`, so it returns the
transaction's working value itself). Any other mode is refused at link time
with `RuntimeError.Internal`, `bad mode M for 'sharedget'`. `mah dis` prints
the mode as `copy`/`working` and the four transaction opcodes with no
operands. The codes `0x72` and `0x73` are unassigned (they were `sharedlock`/
`sharedunlock` in unreleased development builds of 1.21); a file using them
is refused like any unknown opcode (`unknown opcode 0x72 at instruction I`).

`decorate kind a b values` *(1.15)* stores the decorators of one target
(docs/REFLECTION.md, M41b). `values` are the addresses holding the decorator
values, **in source order** (the design says "pops `count` values"; this VM is
register-based, so the values are operands, `A*`, and `count` is the operand
list's own length). `kind` is a varuint (the same byte as a `u8` for 0–4):
0 function `a` (a FUNCTIONS index), 1 parameter `b` of function `a`, 2 type
`a` (a TYPES index), 3 field `b` of struct `a`, 4 variant `b` of enum `a`. `b`
is 0 for kinds 0 and 2. The VM keeps a Vector of the values in a per-run
table keyed by the target; a target decorated a second time in one run is a
runtime `Internal` error (`decorate: the target (kind K, A, B) is decorated
twice`; the encoder never emits that). The load-time checks (the same message
on both VMs, `'decorate' at instruction I: ...`): `unknown kind K` (> 4);
`function index A out of range`; `parameter index B out of range for function
A`; for kinds 2–4 `type index A is not a user type` (a built-in or out of
range), for 3 `type index A is not a struct` and `field index B out of range
for type A`, for 4 `type index A is not an enum` and `variant index B out of
range for type A`. The encoder puts all of a module's `decorate`s in one run
before that module's first statement (docs/REFLECTION.md).

`paramhooks fn index dest` *(1.16)*: `dest ←` the hooks the VM stored for
parameter `index` of the function with FUNCTIONS index `fn` (`hooks.set_param`,
§4.4), or `none`. It sits in the prologue of function `fn` itself, after the
arguments are bound and the defaults filled, for each decorated parameter
(docs/REFLECTION.md, "WrapParam"); the stored data is keyed by the function's
*identity* (§6.7), which for the original function is its own index, so a
wrapper that took the original's identity doesn't read the original's
parameters' hooks. (The design says the opcode reads the running closure's
identity; frames here don't know their closure, and the function the code
belongs to is known statically, so the index is an operand.) Load-time checks
(`'paramhooks' at instruction I: ...`): `function index F out of range`;
`parameter index P out of range for function F`.

In the `*kw` opcodes, `kwnames` names the **last** `len(kwnames)` entries
of `args`, in order; the entries before them are positional. `len(kwnames)
≤ len(args)`, and the names are distinct (validated at load). `native`'s `args` count **must**
equal the native's declared arity (validated at load). `struct`/`enum`'s
`values` count **must** equal the type's/variant's field count. `map`'s
`pairs` count **must** be even (validated at load). `loadtype`'s `kind` is
`0` (`index` a type index `T`, in range) or `1` (`index` a primitive code,
0-7, *(1.17)* 0-8 when the file's minor is ≥ 17, §5); encoded as a `varuint` each, which for these values is the same
single byte a `u8` would be.

### 4.7 DEBUG (`0x80`, optional)
```
nfiles varuint, nfiles × str      source paths; file 0 is the entry file,
                                   others relative to its directory
nruns varuint, nruns × (pc_delta varuint, file varuint, line varuint, col varuint)
```
A run says "instructions from this pc on come from `file:line:col`" (1-based
line and column), until the next run; `pc_delta` is relative to the
previous run's pc (the first is relative to `0`). Instructions before
the first run have no source position. The reference encoder always starts
with a run at pc `0`. A `line` of `0` means
"no source position". Used only for error messages. `mah build --target
debug` (the default) writes it; `--target release` omits it.

### 4.8 HANDLERS (`0x08`, required iff minor ≥ 4) *(1.4)*
```
count varuint
count × (start L, end L, handler L, slot varuint)
```
One entry per `try`/`catch` region (and every implicit one `defer`
introduces, §6.8) compiled anywhere in the program, in **section order**
(the encoder writes an inner region's entries before the entries of any
region enclosing it -- "innermost first"). A handler entry covers
instructions `start ≤ pc < end`; on a throw at such a `pc`, execution
continues at `handler` in the **same frame** (not a fresh one -- a jump
target, not a call), with the thrown value written into slot `slot` of
that frame, at depth `0`. Unwinding scans this table in section order and
takes the **first** entry whose range contains the throwing `pc` (see
§6.8 for the full algorithm across frames).

Validated at load: `start < end ≤` the CODE section's own instruction
count, and `handler <` that count. A 1.4 file with no `try`/`defer`
anywhere still has this section, with `count` `0`. A minor < 4 file
**must not** contain this section at all (it's simply not on that minor's
required-section list, so one present there is rejected as an unknown
required section, like any other). `mah dis` prints the table.

Nested functions' code sits inline inside their enclosing function's own
code (jumped over by a `closure` instruction's own preceding jump), so an
encoder **must not** let a handler range cover a nested function's body --
ranges are split around them. This is what makes "pc range → same frame"
sound without a frame needing to know which function it's running.

### 4.9 TESTS (`0x81`, optional)

The test table of a file built for `mah test` (docs/MAH_TEST.md); ordinary
`mah build` output never has one. Being optional, it needs no new minor
version: a VM that doesn't run tests skips it like any unknown optional
section.

```
count × (name str, slot varuint, line varuint)
```

One entry per `test "name" { ... }` block, in source order. `slot` is a
slot of function 0's frame (the main frame; it **must** be below that
function's `slot_count`) into which the file's top-level code stores the
test's closure, a function of no parameters. `line` is the line of the
`test` keyword in the entry file (0 if unknown), for tools. How a VM runs
one entry is §6.10.

### 4.10 META (`0x82`, optional) *(1.14)*

What the program's source *wrote* about its functions and types: type
annotations, doc comments and constant parameter defaults, for
`std:reflect` (docs/REFLECTION.md). Being optional, it needs no minor
version by itself: older VMs skip it, and a VM without it answers
reflection with names only (§4.4, the `(1.14)` natives). Every 1.14 encoder
writes it, after the other optional sections, in debug and release builds
alike. It never records what a checker inferred: an unannotated parameter's
type is `unknown`.

```
functions: count varuint (= FUNCTIONS count), count × fnmeta
types:     count varuint (= number of user types in TYPES), count × typemeta

fnmeta   = flags u8                    bit 0: has metadata (0 → nothing else follows;
                                       all other bits must be 0)
           doc str?
           ntype_params varuint, ntype_params × str
           nparams varuint             (= the function's param_count)
           nparams × parammeta
           returns typeref
           throws u8 (0 = no clause; 1 = clause) [n varuint, n × typeref]

parammeta = type typeref,
            doc str?,
            default u8 (0 = none; 1 = non-constant default; 2 = constant default, then a CONSTANTS index varuint)

typemeta = doc str?, ntype_params varuint, ntype_params × str, body
  struct: nfields × (type typeref, doc str?)            (count from TYPES)
  enum:   nvariants × (doc str?, nfields × (type typeref))

typeref = tag u8, payload
  0 unknown                         (no annotation, or `Unknown`, or a name that resolves to nothing)
  1 named: kind u8, index varuint, nargs varuint, nargs × typeref
           kind 0: a type index T (the built-in enums 0-2, then user types); kind 1: a
           primitive code -- `0` Number, `1` String, `2` Bool, `3` Function, `4` Vector,
           `5` Map, `6` None, `7` Type, *(1.17)* `8` Bytes (the codes of `loadtype`).
           A 1.14–1.16 file never uses code 8: its encoder writes `Bytes` as `unknown`
           (tag 0) there, and a decoder takes code 8 only when the file's minor is ≥ 17
  2 fn:    nparams varuint, nparams × typeref, returns typeref,
           throws u8 (0 | 1 then n varuint, n × typeref)
  3 param: name str                 (a type parameter, `T`)
  4 self                            (`Self`)
  5 never
  6 trait: name str, nargs varuint, nargs × typeref   (a trait used as a type)
```

A **constant default** is a parameter default that's a literal Number,
String, Bool or `none`, or `-` applied to a Number literal; anything else is
"non-constant" (it's still evaluated per call, §6.1). A function whose source
wrote nothing (no doc, no annotation, no type parameter, no `throws`, no
default) gets `flags` 0, as do function 0 and closures the compiler makes
itself. A `fn(A) -> B` type without `-> B` returns the primitive `None`.
Validation at load: every string, type and constant index in range, both
counts and each `nparams` equal what they describe, `flags` and `default`
values as listed, the payload fully consumed. A doc is the text of the `##`
comment lines directly above the declaration, each without its `##` and at
most one space after it, joined by `\n` (docs/REFLECTION.md).

## 5. Values

| type name | values |
|---|---|
| `Number` | decimal numbers. VMs **should** use base-10 arithmetic with at least 28 significant digits (the reference VM uses exactly 28, rounding beyond that); integer-valued results must be exact within that precision |
| `String` | immutable Unicode text |
| `Bool` | `true`, `false` |
| `Option` | enum type 0; `none` is a single shared value |
| `Promise` | enum type 1, plus scheduler state (§6.4); *(1.4)* also a hidden `observed` flag (§6.4) |
| `RuntimeError` *(1.4)* | enum type 2 (§4.3); every VM-raised failure, docs/ERRORS.md |
| `Function` | a closure: (function index, defining frame) |
| `Vector` *(1.3)* | an ordered, growable list of values, indexed from `0`; **mutable, by reference** |
| `Map` *(1.3)* | an insertion-ordered table from keys (Strings, Numbers, Bools) to values; **mutable, by reference** (§6.9) |
| `Bytes` *(1.17)* | an ordered, growable sequence of bytes, each a whole Number 0–255, indexed from `0`; **mutable, by reference**. Unlike a Vector, two Bytes are `==` when their contents are equal (§6.2); always truthy; not usable as a Map key (§6.9). It has no literal and no opcode: it comes from the 1.17 natives (§4.4) and `String.to_bytes` (§6.7). `to_string` is `Bytes[68 69]` (§6.6) |
| `Type` *(1.14)* | a type: `(kind, index)` with kind 0 a type index `T` (§4.3: the built-in enums, structs, user enums) or kind 1 a primitive code (`0` Number, `1` String, `2` Bool, `3` Function, `4` Vector, `5` Map, `6` None, `7` Type, *(1.17)* `8` Bytes). Immutable; two Types are `==` when kind and index match; not usable as a Map key (§6.9); `to_string` is the type's name (§6.6) |
| user struct | (type, field values in declaration order), **mutable, by reference**; *(1.4)* also a hidden `thrown_at` slot (§6.8), never visible to Mah code |
| user enum | (type, variant, field values), mutable, by reference; *(1.4)* also a hidden `thrown_at` slot (§6.8) |

The **type name** of a value (used by method dispatch): `Number`,
`String`, `Bool`, `Function`, `Option`, `Promise`, `Vector`, `Map`,
`RuntimeError` *(1.4)*, `Type` *(1.14)*, `Bytes` *(1.17)*, or the user type's name.

**Truthiness** (`jmpf`, `and`, `or`): `false`, `none`, the Number `0`, and
the empty String are falsy; every other value is truthy.

## 6. Execution

### 6.1 Frames, calls, returns
- A **frame** is an array of `slot_count` slots (initially empty) plus a
  `static_parent` frame pointer. Slot reads/writes go through `A` operands
  as described in §4.6. Reading a never-written slot yields `none`.
- A **task** has a program counter, a current frame, a return stack of
  `(return pc, frame)` pairs, and a defer stack (§6.5). The VM also has one
  shared **return register**.
- Start: create the global frame (`functions[0].slot_count` slots, no
  parent) and task 0 at pc `0`. Each step fetches the instruction at `pc`,
  increments `pc`, then executes.
- `closure fn dest`: `dest ← Function(fn, current frame)`.
- `call callee args` / `callkw callee args kwnames`: `callee` must hold a
  Function (else runtime error). Create a frame of the function's
  `slot_count` with `static_parent` = the closure's defining frame,
  **bind the arguments** into its parameter slots `0..param_count-1` (below),
  push `(pc, current frame)`, make the new frame current, and jump to the
  function's `entry`.
- **Argument binding.** Given the function's parameters `p0..p(n-1)` (names
  and default flags from PARAMS), the positional values `v0..v(m-1)` and the
  keyword pairs `(k, w)`:
  1. `m > n` → runtime error (too many positional arguments).
  2. Slot `i ← v_i` for every `i < m`.
  3. For each keyword pair in order: find the parameter named `k`; none →
     runtime error (unexpected keyword argument); already bound (by position
     or an earlier keyword) → runtime error (multiple values); otherwise bind
     it to `w`.
  4. Every parameter still unbound: if it has a default, its slot holds the
     **absent** marker; otherwise → runtime error (missing required
     argument).

  *(1.16)* **Rest parameters** (PARAMS flags, §4.5a). Let `n'` be the
  number of ordinary parameters, `n` minus the rest ones (they are always the
  last one or two). The same four steps apply to the ordinary parameters
  `p0..p(n'-1)` only, with these changes: in step 1, `m > n'` is an error
  only when there is no `...` parameter, and the extra values `v(n')..v(m-1)`
  go, in order, into a **new Vector** bound to it; in step 3 a keyword
  naming an ordinary parameter binds it as before (or is "multiple
  values"), and any other keyword goes, in the order given, into a **new Map**
  (String keys) bound to the `**` parameter, or is "unexpected keyword
  argument" when there is none. A keyword given twice that way is "got
  multiple values". The rest parameters' own names are not parameter names for
  keywords (`f(r: 1)` with `...r` is an unmatched keyword). The "Argument
  Count is invalid" text is only used when there are no keywords, no
  defaulted parameters and no rest parameter; with a rest parameter the "at
  most N positional arguments" text counts `n'`. A method call (`callmethod`
  and friends) binds the receiver to the first parameter; when that is the
  `...` parameter (a function that has no ordinary parameter, such as a
  wrapper `fn(...args, **kw)`), the receiver is the first item of its Vector
  and every following argument binds as above.
  Encoders emit, at the start of the function body, one `jmpset` per
  defaulted parameter that skips that parameter's default computation when
  the parameter was bound, so the absent marker is never observed by
  anything else. A default is therefore evaluated at call time, inside the
  callee's frame, in parameter order. Reference error messages: with no
  keyword arguments involved and no defaulted parameters,
  `Argument Count is invalid. 'f' accepts N arguments but M was given`
  (`function` for an anonymous one); otherwise `'f' takes at most N
  positional arguments but M were given`, `'f' got an unexpected keyword
  argument 'k'`, `'f' got multiple values for argument 'k'`, and
  `'f' is missing required argument 'p'`. For method calls the counts
  exclude the receiver and the label is `method 'f'`.
- `callspread callee args kwargs` *(1.14)*: `callee` must hold a Function,
  `args` a Vector and `kwargs` a Map with String keys (both built by the
  encoder's code just before, with `spread` below); the Vector's items are
  the positional values and the Map's entries, in insertion order, the
  keyword pairs of the binding above, which then proceeds exactly as for
  `callkw`. `callmethodspread recv name args kwargs trait` likewise follows
  `callmethodkw` (§6.7, receiver bound first). Both are followed by
  `retval`. Codegen uses them only for calls with `...xs`/`**m` arguments;
  other calls compile as before.
- `spread target source keyword` *(1.14)*: with `keyword` false, `target`
  (a Vector) gets a copy of `source`'s items appended, and `source` must be
  a Vector; with `keyword` true, `source` must be a Map whose keys are all
  Strings and its entries are added to `target` (a Map), which must not
  already have that key. Each violation is `RuntimeError.ArgumentError` with
  the reference text `'...' needs a Vector, got TYPE`, `'**' needs a Map, got
  TYPE`, `'**' needs String keys, got a TYPE key`, `keyword argument 'k'
  given more than once`. The encoder builds a spread call's Vector from its
  plain arguments (`vector`) and each `...xs` (`spread`), its Map from its
  `name: v` arguments (`map`, `spread`) and each `**m`, evaluating
  everything left to right; a keyword given by name and again through a Map
  is therefore the last error above.
- `loadtype kind index dest` *(1.14)*: `dest ←` the Type value (§5).
- `jmpset param L`: jump to `L` if `param` (always a slot of the current
  frame) holds a bound value, i.e. anything but the absent marker.
- `ret value`: return register ← value. If the task's return stack is empty,
  the task finishes with that value; otherwise pop `(pc, frame)` and
  continue there.
- `retval dest`: `dest ← return register`. Encoders emit it immediately
  after every `call`/`callkw`/`callmethod`/`callmethodkw` (and *(1.14)* `callspread`/`callmethodspread`).
- `halt`: the task finishes with `none` (ends the main program's top-level
  code).

### 6.2 Operators
- `move src dest`: copy (values are references for structs/enums/functions/
  promises).
- `loadk k dest`: load the constant.
- `jmp L`; `jmpf cond L`: jump if `cond` is falsy.
- `add`: if either operand is a String, the result is
  `to_string(a) ++ to_string(b)` (§6.6); if both are Numbers, their sum;
  *(1.17)* if both are Bytes, a new Bytes: the bytes of `a` followed by
  those of `b` (neither operand changes; with a String on the other side
  the String rule above applies, so `"x" + b` is `"x" ++ to_string(b)`);
  otherwise a runtime error.
- `sub`, `div`, `pow`: Numbers only. `div` by zero is a runtime error.
- `mul`: Numbers; or a String and an integer Number in either order,
  giving the String repeated that many times (≤ 0 → empty).
- `idiv`: quotient truncated toward zero. `mod`: remainder with the sign of
  the dividend (`a - b × idiv(a, b)`). Division by zero is a runtime error.
- `neg`: Number negation.
- `eq`/`neq`: Numbers compare numerically, Strings by content, Bools by
  value, `none` equals only `none`; every other value (structs, enums
  including `some(..)`, functions, promises, Vectors, Maps) is equal only to itself
  (identity); *(1.17)* two Bytes are equal when they have the same length and
  the same bytes (by content, unlike Vectors), and a Bytes never equals a
  Vector or any other type. Values of different types are never equal. Result: Bool.
- `lt`/`gt`, and *(1.2)* `le`/`ge` (≤ / ≥): two Numbers, or two Strings
  (by Unicode code point sequence); anything else is a runtime error.
  Result: Bool.
- `not a dest` *(1.2)*: `dest ←` the Bool `!truthy(a)`.
- `and`/`or`: both operands are already evaluated (no short-circuit);
  result is the Bool of `truthy(a) && truthy(b)` / `truthy(a) || truthy(b)`.

### 6.3 Structs, enums, patterns
- `struct T values dest`: new instance of struct `T`, fields in declaration
  order. `enum T v values dest`: new instance of variant `v` of enum `T`;
  `enum 0 0 [] dest` (`Option.none`) **must** produce the shared `none`.
- `getfield obj f dest` / `setfield obj f src`: `obj` must be a struct or
  enum instance having a field named `f` (by name, since the type isn't
  known statically); otherwise a runtime error. `setfield` mutates in place.
- `matchstruct v T dest`: `dest ← (v is an instance of struct T)`.
  `matchenum v T k dest`: `dest ← (v is an instance of enum T, variant k)`.
- `matchfail`: runtime error "No pattern in 'match' matched the value"
  (`RuntimeError.MatchFailed`, §6.8).
- `matchrange v lo hi inclusive dest` *(1.2)*: `dest ←` a Bool, true iff
  `v` and every present bound are all Numbers or all Strings, and (`lo`
  absent or `lo ≤ v`) and (`hi` absent, or `v < hi`, or `v ≤ hi` when
  `inclusive` is 1). It never raises: a value of another type simply
  doesn't match. At least one of `lo`/`hi` is present (validated at load).
  Strings compare as `lt` does.
- `matchtype v T dest` *(1.4)*: `dest ←` a Bool, true iff `v` is an
  instance of `T` -- for a struct `T`, exactly like `matchstruct`; for an
  enum `T`, any variant matches (unlike `matchenum`, which checks one
  specific variant). `none`/`some(..)` (type 0) and every Promise (type 1,
  any state) count as instances of `Option`/`Promise` respectively. Never
  raises. Used to compile a catch arm's type-test pattern (`name: Type`,
  docs/ERRORS.md).

### 6.4 Tasks, promises, and the scheduler
Single-threaded cooperative scheduling:
- A **Promise** is an enum instance of type 1: `Pending`, `Settled
  { value }` once resolved, or *(1.4)* `Failed { error }` once a detached
  task throws past its own top (§6.8); it also holds an internal list of
  waiting continuations and, *(1.4)*, a hidden `observed` flag (§6.8),
  never visible to Mah code. Resolving/failing an already-settled-or-
  failed promise does nothing; resolving a pending one sets it to
  `Settled { value }`, and *(1.4)* failing one sets it to `Failed { error
  }`, then either way runs its waiting continuations **synchronously, in
  registration order**.
- `detach callee args dest` / `detachkw callee args kwnames dest`: bind
  arguments like `call`/`callkw` (errors happen here, in the calling task);
  create a new pending Promise `p` and a new task whose frame is set up
  like `call` (empty
  return stack), with `p` as the promise it resolves when finished. **Run
  the new task immediately**, until it finishes (then resolve `p` with its
  result), suspends, or *(1.4)* fails past its own top (then fail `p`
  with the error, §6.8). Then `dest ← p` and the calling task continues.
- `detachmethod recv name args trait dest` / `detachmethodkw ...`: method
  lookup exactly like `callmethod` (§6.7), then like `detach`/`detachkw`
  with the resolved function and the argument list including `recv` when
  it's a method call. If the target is a native method, call it and `dest ←`
  a Promise already settled with its result (keyword arguments to a native
  method → runtime error, unexpected keyword argument).
- `await p dest`: `p` must be a Promise (else runtime error:
  `RuntimeError.TypeMismatch`). *(1.4)* Sets `p`'s `observed` flag
  regardless of what follows. If settled, `dest ← value`. *(1.4)* If
  failed, throw `error` at this `await` instruction (§6.8) -- caught by an
  enclosing `try` exactly like any other throw. Otherwise (pending) the
  current task **suspends**: register a continuation on `p` that, once it
  settles, writes the value to `dest` and resumes the task at the next
  instruction, or, *(1.4)* once it fails, resumes the task by throwing
  `error` at this same `await` instruction; control returns to whatever
  was running the task (the `detach` that started it, or the scheduler
  loop) either way.
- `time.sleep_async(ms)` returns a pending promise and registers a **timer**
  at `now + ms`.
- *(1.10)* `io.read_line` starts a pending **I/O operation**: the VM does
  the blocking read off its own thread of execution (the reference VMs use
  one standard-input reader thread, so lines are handed out in the order
  they were asked for) and only ever settles the operation's Promise from
  the scheduler loop, never concurrently with a running task.
- **Scheduler loop**: after task 0 is first run (it runs until it halts,
  suspends, or -- fatally, §6.8 -- fails), repeat: if task 0 has finished
  and no timers *(1.10)* or I/O operations remain, the program ends;
  otherwise settle the next event: *(1.10)* an I/O operation that has
  already completed, if any; else wait until the earliest timer is due
  (ties: registration order) or, *(1.10)* if one completes sooner, an I/O
  operation does, whichever comes first, and settle it -- a timer's promise
  resolves with `none`. So the program ends only once the main code has
  finished *and* no scheduled work remains: *(1.10)* a detached `input`
  nobody awaits still keeps the program running until its line arrives. *(1.18)* So does
  a pending `socket.accept`, `socket.recv` or `socket.connect`, until it settles
  (data, a connection, an error, its timeout, or its socket being closed). *(1.21)* So
  does every runtime wait of §6.11: a queued or running job this VM submitted,
  a semaphore or channel wait, a join and a `retry` wait (idle threads don't
  count); a run in which those waits can never be satisfied ends them with `stuck`
  instead of waiting forever (§6.11, "Quiescence"). *(1.4)* Once it does, if any
  detached task's Promise failed and was never observed, the program
  still stops with the uncaught-error report (§6.8) for the **first**
  such Promise, in fail order.
- When a detached task finishes, its promise is resolved with the task's
  result (running the promise's continuations); *(1.4)* if it fails past
  its own top instead, its promise is failed with the error the same way.

### 6.5 Defer
Each task has a defer stack of scopes; a scope is a stack of closures.
- `deferpush`: push an empty scope.
- `deferadd c`: push closure `c` onto the top scope.
- `deferpeek dest`: `dest ← (top scope is non-empty)` as a Bool.
- `deferpop dest`: pop the top scope's most recent closure into `dest`.
- `deferscopepop`: discard the (empty) top scope.
- `deferdepth dest` *(1.4)*: `dest ←` a Number, how many scopes the
  task's defer stack currently holds.
- `deferabove depth dest` *(1.4)*: `depth` holds a Number `d` (from an
  earlier `deferdepth`); `dest ←` a Bool, whether the defer stack
  currently holds more than `d` scopes. Used, with `deferdepth`, to drain
  a task's defer stack back down to a saved depth while unwinding a throw
  (§6.8) -- `deferpop`/`deferscopepop` alone drain by a fixed *count* of
  scopes (known at compile time for `return`/`break`/`continue`); a throw
  needs to drain down to a saved *depth* instead, since the same handler
  code runs regardless of how many scopes happened to be open when the
  throw occurred.
Encoders drain scopes with ordinary `call`/`retval` instructions.

### 6.6 `to_string` (the `Printable` system trait)
`to_string(v)`, used by `io.print` and by `add` with a String operand:
1. If the method table (§6.7) has a **Mah-code** (non-native)
   implementation of trait `Printable`, method `to_string`, for `v`'s type
   name: call it **synchronously** with `v` as its only argument. It runs as
   a fresh task started from the current step, and it's a runtime error if
   it suspends or returns a non-String. Its result is the answer.
2. Otherwise, built-in formatting:
   - Number: an integer value prints with no fractional part (`42`, `-3`);
     otherwise plain decimal notation with no exponent and no trailing zeros
     (`2.5`, `0.001`).
   - String: its text, unquoted. Bool: `true`/`false`. `none`: `none`.
   - `some(x)`: `some(` + `to_string(x)` + `)`.
   - other enum values: `Type.Variant`, or `Type.Variant { f1: v1, f2: v2 }`
     with each field value formatted by `to_string` (this includes Promises:
     `Promise.Pending`, `Promise.Settled { value: 1 }`).
   - struct: `Type { f1: v1, f2: v2 }`, fields in declaration order.
   - Function: `<fn NAME>`, or `<fn>` for an anonymous function.
   - Type *(1.14)*: the type's name (`Number`, `User`; a struct or enum
     is shown by the name it was declared with).
   - Vector *(1.3)*: `[` + the items' `to_string`s joined by `, ` + `]`
     (`[]` when empty).
   - Map *(1.3)*: `[` + `to_string(k) + ": " + to_string(v)` for each entry
     in insertion order, joined by `, `, + `]`; `[:]` when empty.
   - Bytes *(1.17)*: `Bytes[` + each byte as two lowercase hex digits,
     joined by one space, + `]` (`Bytes[68 69]`; `Bytes[]` when empty).

### 6.7 Methods
(Terminology: a *native target* here is the VM's own built-in
implementation of a system-trait method. It is unrelated to the NATIVES
section (§4.4) and is never listed there.)

The VM keeps a **method table** keyed by `(type name, method name)`; each
entry holds at most one *inherent* target and at most one target per trait
name. A target is (function value or native, `is_method`).
- Initially: for every built-in type name (`Number`, `String`, `Bool`,
  `Function`, `Option`, `Promise`, and *(1.3)* `Vector`, `Map`, *(1.4)*
  `RuntimeError`, *(1.14)* `Type`, *(1.17)* `Bytes`), a native target for trait `Printable`,
  method `to_string`, `is_method` true, computing §6.6 step 2.
- Also initially *(1.3)*, native targets for `Vector`, `Map`, and
  `String` for trait `Index`, method `index` (one argument, `key`), and for
  `Vector` and `Map` (not `String`: Strings are immutable) for trait
  `IndexAssign`, method `index_assign` (two arguments, `key`, `value`),
  `is_method` true, behaving as §6.9 describes. Encoders compile `x[k]` to `callmethod x
  "index" [k] "Index"` and `x[k] = v` to `callmethod x "index_assign" [k,
  v] "IndexAssign"` (each followed by `retval`), so a user type that
  `defmethod`s those traits is indexable the same way. *(1.17)* `Bytes` has
  native targets for both traits too (§6.9).
- Also initially *(1.2)*, native **inherent** methods (all `is_method`
  true; arguments after the receiver are positional, and keyword arguments
  are a runtime error, except that *(1.3)* a parameter shown below as
  `name = default` is optional and may also be passed by keyword; binding
  then follows §6.1 as for a function with that defaulted parameter):
  | type | method | behavior |
  |---|---|---|
  | `String` | `len()` | the number of Unicode code points, as a Number |
  | `String` | `char_at(i)` | the code point at index `i` (an integer Number, `0 ≤ i < len`) as a one-character String; anything else is a runtime error |
  | `Function` | `arity()` | the function's `param_count` (including defaulted parameters and *(1.16)* rest parameters) |
  | `Vector` *(1.3)* | `len()` | the number of items |
  | `Vector` *(1.3)* | `push(x)` / `push_start(x)` | append `x` at the end / insert it at index 0; returns `none` |
  | `Vector` *(1.3)* | `pop()` / `pop_start()` | remove and return the last / first item, or `none` if empty |
  | `Vector` *(1.3)* | `copy(deep = false)` | a new Vector with the same items. Shallow unless `deep` is truthy; deep copies are described in §6.9 |
  | `Map` *(1.3)* | `len()` | the number of entries |
  | `Map` *(1.3)* | `keys()` / `values()` | a new Vector of the keys / values, in insertion order |
  | `Map` *(1.3)* | `has(k)` | Bool: whether `k` is present (a non-key `k` is a runtime error, §6.9) |
  | `Map` *(1.3)* | `remove(k)` | remove `k`'s entry and return its value, or `none` if absent |
  | `Map` *(1.3)* | `copy(deep = false)` | a new Map with the same entries in the same order; shallow unless `deep` is truthy (§6.9) |
  | `String` *(1.6)* | `split(sep = none, limit = none)` | a Vector of Strings. With `sep` `none`: the maximal runs of non-whitespace. Otherwise the pieces between non-overlapping occurrences of `sep` (a non-empty String), left to right. With `limit` (a whole Number), at most that many splits: the last piece is then the rest of the String, unchanged (in whitespace mode, starting at its next non-whitespace code point) |
  | `String` *(1.6)* | `trim()` / `trim_start()` / `trim_end()` | without leading and trailing / leading / trailing whitespace |
  | `String` *(1.6)* | `pad_start(width, fill = " ")` / `pad_end(...)` | if shorter than `width` code points: `fill` (non-empty) repeated before / after it, the last repetition cut to fit exactly; otherwise unchanged |
  | `String` *(1.6)* | `replace(from, to)` / `replace_all(from, to)` | the first / every non-overlapping occurrence of `from` replaced by `to`. An empty `from` matches at the start / before every code point and at the end |
  | `String` *(1.6)* | `starts_with(s)` / `ends_with(s)` / `contains(s)` | Bool |
  | `String` *(1.6)* | `index_of(s)` | `some(i)`, the code-point position of the first occurrence of `s` (`some(0)` for `""`), or `none` |
  | `String` *(1.6)* | `repeat(n)` | the String `n` times (`n` a whole Number ≥ 0) |
  | `String` *(1.6)* | `to_upper()` / `to_lower()` | Unicode full case mapping (`"Straße"` → `"STRASSE"`), final-sigma rule included |
  | `String` *(1.6)* | `lines()` | a Vector of the lines: split at each `\n`, dropping a `\r` immediately before it (a `\r` not followed by `\n` stays); a final `\n` doesn't start another line; `""` has none |
  | `String` *(1.6)* | `parse_number()` | the Number the String spells, or `none`: after trimming whitespace it must match `[+-]?(D+(.D*)? \| .D+)([eE][+-]?D{1,5})?` (D an ASCII digit), converted exactly as a CONSTANTS decimal (§4.2) |
  | `Vector` *(1.6)* | `join(sep = "")` | every item's `to_string` (§6.6), with `sep` between |
  | `String` *(1.17)* | `to_bytes()` | a new Bytes: the String's UTF-8 encoding |
  | `Bytes` *(1.17)* | `len()` | the number of bytes |
  | `Bytes` *(1.17)* | `push(n)` | append the byte `n`; returns `none`. A bad byte is an error with the prefix `push: the value` (§6.9), e.g. `push: the value must be a whole number from 0 to 255, got -1` |
  | `Bytes` *(1.17)* | `pop()` | remove and return the last byte as a Number, or `none` if empty |
  | `Bytes` *(1.17)* | `extend(other)` | append the bytes of `other`, which must be Bytes (else `TypeMismatch`, `extend: other must be Bytes, got TYPE`); `b.extend(b)` doubles `b`; returns `none` |
  | `Bytes` *(1.17)* | `copy()` | a new Bytes with the same bytes (there is no `deep` parameter: the items are Numbers) |
  | `Bytes` *(1.17)* | `to_vector()` | a new Vector of the bytes as Numbers |
  | `Bytes` *(1.17)* | `to_text()` | `some(String)` if the bytes are valid UTF-8 (no surrogates, no overlong forms, nothing above U+10FFFF), else `none` |
  | `Bytes` *(1.17)* | `to_text_lossy()` | the String decoded as UTF-8, with each invalid sequence (its maximal invalid subpart, as in the Unicode recommendation) replaced by one U+FFFD |
  | `Bytes` *(1.17)* | `to_hex()` | the bytes as lowercase hex, two digits per byte (`""` when empty) |
  | `Bytes` *(1.17)* | `to_base64()` | standard alphabet (RFC 4648 §4), padded with `=` to a multiple of 4 characters (`""` when empty) |
  | `Bytes` *(1.17)* | `index_of(needle)` | `some(i)`, the byte position of the first occurrence of the Bytes `needle` (`some(0)` for an empty one), or `none`; a non-Bytes `needle` is a `TypeMismatch`, `index_of: needle must be Bytes, got TYPE` |

  *(1.6)* Positions and lengths count Unicode code points, and
  "whitespace" is exactly the Unicode White_Space property. An argument
  that should be a String and isn't is a `RuntimeError.TypeMismatch`
  (`NAME: WHAT must be a String, got TYPE`); a count that isn't a whole
  Number ≥ 0 is an `ArgumentError` (`NAME: WHAT must be a whole number of
  at least 0, got N`, or `must be a Number, got TYPE` for a non-Number);
  an empty `sep`/`fill` is an `ArgumentError` (`split: sep can't be
  empty`, `NAME: fill can't be empty`). `mah/string_methods.py` is the
  reference implementation. *(1.17)* The `Bytes` methods follow the same
  conventions (positional arguments only, the usual wrong-count error), and
  `mah/bytes_methods.py` is their reference implementation; the Bytes
  methods need no minor of their own, since a Bytes value exists only in a
  1.17 file (§3), but `to_bytes` does (§3, §7).
  Wrong argument counts give the usual `Argument Count is invalid.
  method 'NAME' accepts N arguments but M was given`.
- *(1.16)* **Function identity and item types.** Every Function value has an
  *identity*: its FUNCTIONS index, unless `hooks.adopt` (§4.4) gave it
  another's. A `defmethod` whose `type` is spelled `fn#<N>` (`N` a FUNCTIONS
  index in canonical decimal, validated at load: `'defmethod' at instruction
  I: 'KEY' is not a valid function-item key`) registers the method on the
  *item type* of function `N` (docs/REFLECTION.md, "Function-item types"):
  the compiler writes `impl Tr for somefn` / `impl somefn { ... }` that way,
  `N` being `somefn`'s index. In the lookup of a method call on a Function
  value below, the entry for `fn#<identity>` is tried first (with the same
  rules as the entry for the type name, and a miss falls through, including
  for a trait-restricted call), then the one for `Function`. The type name
  of a Function is still `Function` (messages, `reflect.type_of`).
- `defmethod c type trait name is_method`: set the inherent target
  (`trait` absent) or that trait's target to (`c`, `is_method`). Encoders
  emit all `defmethod`s before any user code runs.
- `callmethod recv name args trait` (and `callmethodkw recv name args
  kwnames trait`, identical except that the arguments carry keywords) —
  lookup, with `T` = type name of `recv`:
  1. `trait` present: the target for that trait, or a runtime error
     "'T' does not implement trait ...".
  2. otherwise the inherent target if any; else the single trait target if
     exactly one trait provides `name`; if two or more do → runtime error
     (ambiguous).
  3. if (no target, or the target isn't a method) and `trait` is absent and
     `recv` is a struct/enum instance with a field `name`: the field's value
     must be a Function; call it with exactly `args` (no receiver),
     binding them like `callkw`.
  4. no target → runtime error "'T' has no method 'name'"; target isn't a
     method → runtime error (static function called as a method).
  5. otherwise call the target with `recv` as the first positional argument,
     followed by `args`. A function target is invoked like `call`/`callkw`
     (binding counts `recv`); a native target sets the return register
     directly (keyword arguments → runtime error, except for a native's
     optional parameters, see above). Either way, `retval`
     follows.

### 6.8 Throwing, unwinding, and runtime errors *(rewritten for 1.4)*

Versions 1.0–1.3 had no error values and no way to catch an error: a
runtime error stopped the whole program with a message. 1.4 (docs/
ERRORS.md) adds a `throw` opcode, the HANDLERS table (§4.8), and turns
every VM-raised failure into a catchable value instead of an immediate
abort.

**`throw value`**: throw `value` at this instruction. If `value`'s type
has no `Error`-trait target for method `message` in the method table
(§6.7) -- i.e. it doesn't `impl Error` -- throw `RuntimeError.TypeMismatch
{ message: "Cannot throw a value of type 'T': it does not implement
Error" }` instead (`T` = `value`'s type name).

**Throwing** value `v` at instruction `pc` in task `t` (this is also what
happens to every runtime error below, and to a `throw` opcode's own
value):
1. If `v` is a struct/enum instance whose hidden `thrown_at` slot is
   unset, set it to `pc` (VM-internal, never visible to Mah code; used
   only to locate an uncaught error; re-throwing -- including
   automatically, when no catch arm matches -- keeps the original).
2. Loop: find the first HANDLERS entry (in section order) with
   `start ≤ pc < end`.
   - Found: write `v` into slot `entry.slot` of `t`'s current frame (at
     depth `0`), set `t`'s pc to `entry.handler`, and continue running
     `t` from there.
   - Not found and `t`'s return stack is non-empty: pop `(ret_pc,
     frame)`, make `frame` `t`'s current frame, set `pc = ret_pc - 1`
     (the call instruction that's still unwinding), and repeat.
   - Not found and `t`'s return stack is empty: the error is **uncaught
     in `t`** (below).

**Every existing runtime-error site** (division by zero, a bad method
call, argument binding, an out-of-range index, a `match` with no
matching arm, a wrapped host-language exception, ...), in both VMs,
throws a `RuntimeError` value at the failing instruction instead of
aborting, instead of the pre-1.4 immediate stop: variant chosen by kind
(below), field `message` = **exactly the pre-1.4 message text** (no
location baked in). This is why every pre-1.4 error-message test stays
green: the *uncaught*-error report (below) reproduces that exact text
plus its location, unless something now catches it.

**Which `RuntimeError` variant each site becomes** (docs/ERRORS.md's
motivation: these are deliberately **not** tracked by the static checker,
unlike a user `throw` -- almost every function does arithmetic or
indexing, so tracking them would make every inferred error set
non-empty and meaningless):
- `DivisionByZero`: "Division by zero" (`div`, `idiv`, `mod`, `0 ** negative`).
- `TypeMismatch`: operator type errors ("Cannot apply ...", "Cannot
  compare ...", "Cannot negate ..."), calling/detaching a non-function,
  `.await` on a non-Promise, field access on a non-struct value, an
  invalid Map key, a non-Number or non-integer index/slice bound,
  `index_assign` with a range, `to_string` returning a non-String,
  throwing a non-`Error` value.
- `NoSuchField`: "'T' has no field 'f'".
- `NoSuchMethod`: "has no method", "does not implement trait", an
  ambiguous method, a static function called as a method, "Field 'f' of
  'T' is not a function".
- `ArgumentError`: every argument-binding error (§6.1, including
  native-method keyword errors).
- `IndexOutOfRange`: `char_at` out of range, `index_assign` on a Vector
  index that names no item.
- `MatchFailed`: `matchfail`.
- `InputError`: `io.input` errors (pre-1.10 files; `io.read_line` fails
  its Promise with `EndOfInput` instead).
- `Internal`: everything else, including "cannot suspend ... when called
  implicitly by the runtime", "program counter out of range", and any
  wrapped host-language exception. Both VMs must classify every site
  identically; a unit test in each VM's own test suite pins at least one
  message per variant.

**Uncaught at a task root:**
- **Main task** (task 0): the program stops immediately (pending timers
  are abandoned, exactly as a pre-1.4 runtime error already did) with
  the uncaught-error report below.
- **Detached task**: its Promise **fails** with the error (§6.4) instead
  of stopping the program. The VM also remembers every failed Promise,
  in fail order. Once the program would otherwise end normally (main
  task finished, no timers left), if any failed Promise was never
  `.await`ed (its hidden `observed` flag is unset), the program stops
  with the uncaught-error report for the **first** such Promise.
- **A synchronous sub-task** (the Mah `to_string` a VM invokes for
  `print`/`add` with a String operand, and anything else run the way
  `.await` is forbidden to -- i.e. `invoke_sync` in both reference
  implementations): the error is thrown in the **calling** task, at the
  instruction that invoked it, so it can be caught there.

**Uncaught-error report** (the fatal message text; the CLI prints it
exactly as a pre-1.4 runtime error, `at position ...` included):
- A `RuntimeError` value: its own `message` field, located at
  `thrown_at` (`at position #L:C` etc., exactly as a runtime error was
  already reported; no location in a release build). This is what keeps
  every pre-1.4 error-message test's text unchanged.
- Any other value, of type `T`: `Uncaught T: <m>`, located the same way,
  where `<m>` is the result of calling its `Error.message` method
  synchronously; if that throws, suspends, or returns a non-String, `<m>`
  is `to_string(value)` instead; if that fails too, the report is just
  `Uncaught T` (no `: <m>` at all).
- Either way, when `thrown_at` is in library code -- its DEBUG file path
  is `<prelude>` (M29) or starts with `std:` (M30) -- the location is
  instead the first pc of the error's recorded backtrace (§6.10: the
  throw site, then each return address, innermost first) that isn't; if
  every one is, `thrown_at` after all. So `"x".to_number()` or a failing
  `json.parse(text)` is reported at the program's own call.

With DEBUG present, VMs should add the failing instruction's location:
the reference CLI appends `at position #LINE:COL` for the entry file and
`at position FILE#LINE:COL` for others. Without DEBUG (a `--target
release` build), the message is reported alone, with no location or
instruction index -- unchanged from 1.0–1.3.

### 6.9 Vectors and Maps *(1.3)*
- `vector items dest`: `dest ←` a new Vector holding the values at `items`,
  in order.
- `map pairs dest`: `dest ←` a new, empty Map, then for each `(k, v)` pair
  in order, `index_assign(map, k, v)` as below (so a later duplicate key
  replaces the value but keeps the first key's position).
- **Vector indices**: an index must be a Number, otherwise a runtime error.
  An integer index `i` with `0 ≤ i < length` names the item at `i`; one
  with `-length ≤ i < 0` names the item at `length + i` (so `-1` is the
  last item). `index(v, i)` returns the named item, or `none` when `i`
  names no item (fractional, or outside `-length ≤ i < length`).
- **Vector slices**: when the index given to a Vector's `index` is a struct
  instance whose type name is `Range` (fields `start`, `end`, `inclusive`),
  `FromRange` (`start`), or `ToRange` (`end`, `inclusive`) — the shapes the
  prelude's range syntax builds — `index` returns a **new** Vector holding
  a slice instead. Each present bound must be an integer Number (else a
  runtime error). `start` defaults to `0` and `end` to the length; a
  negative bound has the length added to it; an inclusive `end` then has
  `1` added; finally both are clamped into `0 … length`, and the result is
  the items from `start` up to (not including) `end` — empty if `start ≥
  end`. Out-of-range bounds are never an error. `index_assign` with a
  range index is a runtime error.
- **String indices** follow the Vector rules above with code points as
  the items (the same code points `len`/`char_at` count): `index(s, i)`
  returns the one-character String at `i` (negative from the end), or
  `none` when `i` names none, and a range index returns the substring with
  the Vector slice rules (clamped, never an error). A non-Number index, or
  a non-integer slice bound, is a runtime error.
- **Bytes** *(1.17)* index like Vectors, with bytes as the items.
  `index(b, i)` returns the byte at `i` as a Number (negative from the end),
  or `none` when `i` names no item; a non-Number `i` is a `TypeMismatch`,
  `Bytes index must be a Number, got TYPE`. A range index (`Range`,
  `FromRange`, `ToRange`) returns a **new** Bytes with exactly the Vector
  slice rules above (clamped, never an error; a non-integer bound is the
  Vector slice's runtime error). `index_assign(b, i, n)` sets the byte at
  `i` and returns `none`. The **byte rule**, for every place a byte is given
  (`index_assign`, `push`, the `bytes.*` natives): a value that isn't a
  Number is a `TypeMismatch`, `P must be a Number, got TYPE`; a Number that
  isn't a whole number from 0 to 255 is an `ArgumentError`, `P must be a
  whole number from 0 to 255, got N` (`N` formatted as by `to_string`);
  `P` is the prefix the caller gives, `Bytes item` for `index_assign`. The
  other `index_assign` errors: `i` naming no item is an `IndexOutOfRange`,
  `Bytes index N is out of range for Bytes of length L (use push to add
  items)`; a range `i` is a `TypeMismatch`, `Can't assign to a Bytes slice
  (b[a..b] = ...); assign items one at a time`; a non-Number `i` as above.
  The checks run in that order: index (or range) first, then the byte.
  `==` compares contents (§6.2), and Bytes isn't a Map key (below).
  Iterating Bytes is prelude code (`impl Iterable<Number> for Bytes`,
  reusing the Vector's iterator, which needs only `len()` and `[i]`), so it
  is live like a Vector's and gives Numbers.
- **Deep copy** (`copy(deep: true)`): copies every Vector, Map, and struct
  and enum instance reachable from the receiver, and *(1.17)* every Bytes
  (a copy with the same bytes, made once per original like the others;
  a shallow copy shares them); every other value
  (Numbers, Strings, Bools, Functions, `none`, Promises) is shared, not
  copied. An object reachable twice is copied once, so sharing (and cycles)
  inside the original is reproduced in the copy. `index_assign(v, i, x)` replaces
  the item and returns `none`; when `i` names no item it's a runtime error.
- **Map keys** must be Strings, Numbers, or Bools (so not a Bytes); any other key given to
  `index`, `index_assign`, `has`, `remove`, or `map` is a runtime error.
  Two keys are the same key when they're equal by `eq` (§6.2): so `1` and
  `1.0` are one key, while `1`, `"1"`, and `true` are three.
  `index(m, k)` returns `k`'s value, or `none` if absent.
  `index_assign(m, k, v)` sets it and returns `none`: a new key goes at the
  end of the insertion order, an existing key keeps its position and its
  original key value (after `m[1] = a` then `m[1.0] = b`, the key is still
  `1`).
- Iteration, `map`/`filter`/… and `Map.entries()` aren't VM features: the
  prelude implements them in Mah on top of the methods above (§7).

### 6.10 Running one test *(TESTS files)*

A VM that runs tests takes a file with a TESTS section (§4.9) and an entry
index, in a fresh VM each time:

1. Run the program exactly as for an ordinary run (the main task, then
   timers, then the check for unobserved failed Promises, §6.4/§6.8). A
   test file's top level holds only declarations, so this defines the
   functions and constants and stores every test closure in its slot. If
   this fails, the test **failed**, with that error's usual report as its
   message.
2. Call the closure in the entry's `slot` as a new detached task (§6.4)
   and run the scheduler (timers) until that task's Promise settles or
   fails. Timers still pending then are dropped, not run: the outcome
   notes that (`leftover`).
3. The outcome: **ok** if the Promise settled; **skipped** if it failed
   with a `SkipTest` struct (message: its `reason` field); **failed** for
   any other error, with message: an `AssertionError`'s `Error.message()`,
   a `RuntimeError`'s `message` field, or `Uncaught T: <m>` (§6.8). If the
   task can never finish (it awaits a Promise nothing will settle), the
   test **failed**.

For a failed test the VM also reports a **backtrace**: when a value is
thrown for the first time (the moment `thrown_at` is recorded, §6.8), the
VM records that pc, then the call instruction of every frame still on the
throwing task's return stack, innermost first. Each is mapped through
DEBUG (§4.7) to `(file, line)`, dropping pcs with no line; file 0, the
test file itself, is reported without a name.

The reference VMs report an outcome in this text form (`mah-vm test FILE
INDEX` writes it to standard error and exits 0; the Python VM returns it
as `mah/test_outcome.py`'s `TestOutcome`):

```
status ok|skipped|failed
leftover 0|1
frame LINE FILE          (zero or more, innermost first; FILE "-" = the test file)
message
...the message, to the end...
```

### 6.11 Threads, jobs, shared variables and transactions *(1.21)*

The normative design is `docs/contracts/M44_threads.md` (§2–§6), with
transactions from `docs/contracts/M45_atomic.md` (§3, §5, §6) replacing its
locks; this section condenses what a VM must do. Everything observable (output, exit
codes, error kinds and messages) is the same on every VM.

**The runtime.** One process-wide runtime per program run, shared by the
main VM and every **job VM**, guarded by one mutex: the shared-variable
store and its versions, the exclusivity token, the `retry` watchers, the
wait-for graph, the threads (pools), semaphores,
channels, the live VMs (for quiescence), the shared file and socket tables,
the single stdin reader and the shared stdout. A VM never touches another
VM's heap: waking a waiter means posting a completion to that VM's done
queue; the VM settles its own Promise on its own thread. Task ids are
process-unique.

**`ThreadError`.** A prelude struct `ThreadError { kind: String, message:
String }` (with `impl Error`), built by the VM directly like `EndOfInput`;
an uncaught one reports `Uncaught ThreadError: <message>`. Kinds and exact
messages (`NAME`, `VAR`, `N` substituted):

| kind | message |
|---|---|
| `closed` | `thread 'NAME' is closed` (queueing on a closed thread); `the channel is closed` |
| `full` | `thread 'NAME' is full (N jobs queued or running)` (N = workers + capacity) |
| `cancelled` | `thread 'NAME' was closed before this job started` |
| `deadlock` | `deadlock: this await would never end (it waits, through threads, for itself)` (an await or join); `deadlock: a thread can't join itself` |
| `stuck` | `the wait can never finish: every thread is waiting`; `the job never finished: it waits on a Promise nothing will settle`; `retry can never wake up: this transaction read no shared variable` |
| `not_sendable` | `a Promise can't be sent to another thread`; `shared variable 'VAR' can't hold a Promise` (a plain write or a commit) |
| `foreign_promise` | `a Promise from another thread can't be awaited here (it was still pending when it was copied)` |
| `over_release` | `release without a matching acquire (all N permits are free)` |
| `in_atomic` | `'NAME' can't run inside 'atomic { }': its body may run more than once` |

**Copies.** Values cross VMs only as copies, made by an iterative walk that
preserves identity and cycles within one copy (one memo for the whole
snapshot). `none`, Bool, Number, String and Type values are immutable and
shared; Vector, Map (same insertion order), Bytes, struct and enum values
(same type, variant, `thrown_at` and backtrace) are new objects with copied
contents; a closure is a new closure over the same FUNCTIONS index and
identity whose defining frame is a copy; a frame copies its slots and its
static parent. Three modes differ only for Promises: **strict** (arguments
of `thread.submit`, job results and errors, channel messages, shared
values) refuses any Promise (`not_sendable`), checked before the memo;
**environment** (everything reached through a frame or closure) copies a
Promise by state — Settled and Failed are copied, Pending becomes a Failed
Promise whose error is `ThreadError foreign_promise`; every copied Promise
is observed and has no waiters; **local** (a read of a shared variable, and a working value
or an assignment inside a transaction, all of which stay in the same VM)
keeps every Promise as the same object.

**Queueing a job** (`thread.submit`; `detach(t) expr` compiles to it with
the operand wrapped in a zero-parameter closure and no arguments), in
order: validate the Thread and the function; bind the arguments to the
function's parameters in the submitting VM (the same rules and errors as
`call`); take the **snapshot** — the strict roots (the bound argument
values) first, then the environment roots (the callee closure, so its
frames and the **main frame: every global**; every user method-table entry
whose target is a Mah closure; the decorator and hook tables; the
function-item flag) and a copy of the process environment table; under the
runtime lock, a closed thread throws `closed`, a thread with `capacity` not
`none` and queued + running ≥ workers + capacity throws `full`, else the
job joins the FIFO queue; return a pending Promise counted as this VM's
pending work. Shared variables, other tasks, timers, I/O and defer stacks
are not part of a snapshot.

**Running a job.** Each worker takes the oldest queued job (with one
worker, the next starts only after the previous finished) and runs it in a
fresh job VM: the same program, a fresh heap with the snapshot restored,
its own timers, I/O, done queue, pending map and failed-promise list. The
root task calls the callee with the bound arguments; the job ends like a
program: once the root has finished and nothing (timer, I/O, job,
semaphore, channel wait, join, `retry` wait) is pending. Its outcome: the root failed →
at once, the error (strict copy; a Promise inside it → `not_sendable`); the
root never finished with nothing pending → `stuck`; a detached task failed
unobserved → that error; else the root's value (strict copy; refusal →
`not_sendable`). An unexpected host failure → `RuntimeError.Internal`. The
reply settles or fails the job's Promise in the submitting VM through its
done queue; a failed one is reported at that VM's end like an unobserved
failed detached task when nobody observed it (with several, the order is
the order the replies arrived). A job Promise already settled by hand drops
the reply. **Teardown**: the job VM's line buffer is handed over; its waiters
are removed from every semaphore, channel and join list and its `retry`
waits from every watcher set; if it owns the exclusivity token, the token
is released (a transaction running in it is simply gone: nothing of it was
published); its wait-for edges are removed; a permit or message delivered to it too late is given
back (a message to the front of its channel).

**Shared variables.** The store maps a shared variable's index to
`(stored value, version)`: the stored value is a strict copy that is never
mutated or handed out; absent = `(none, 0)`. A global `clock` (from 0) is the
version of the last commit or plain write. Outside a transaction:
- `sharedget k name 0 dest`: a **local** copy of the stored value. Never
  waits. (Mode 1 outside a transaction is `RuntimeError.Internal`
  `sharedget in working mode outside a transaction`; the compiler never
  emits it.)
- `sharedset k name src` (a **plain write**; `shared let x = e` and `x = e`
  outside `atomic` compile to it): strict-copy the value (a Promise inside →
  `ThreadError not_sendable` `shared variable 'NAME' can't hold a Promise`,
  nothing stored); then, under the runtime lock, wait while another task's
  transaction is exclusive (below), `clock += 1`, store `(copy, clock)` and
  wake the `retry` waiters of `k`.

**Transactions.** `atomic { body }` compiles to: the body as a
zero-parameter closure (so every attempt gets fresh locals), `atomicbegin`,
a handler region covering only `call closure` and `retval v`, then
`atomicend`, `dest ← v`, a jump over the handler; the handler runs
`atomicabort` and re-throws the error. `atomicbegin`, `atomicend` and the
handler are outside the region, so an error they raise unwinds to the
enclosing handlers. `retry` compiles to the `retry` opcode.

Each task has `tx` (none or a transaction) and `implicit` (none, or
`to_string`/`message` for the sub-task of an implicit runtime call, which
shares its caller's id and `tx`). A transaction has: `serial`
(process-unique, from 1), `owner` (the task that began it), `implicit` (the
owner's label), `depth` (nesting; 0 between a conflict and the restarted
`atomicbegin`), `rv` (the snapshot time), `reads` (index → version, in
first-read order), `entries` (index → `{name, base, working, assigned,
exposed}`, in first-access order), `attempts` (failed attempts so far),
`irrevocable` (this attempt is exclusive), `restart` (the pc of the
outermost `atomicbegin`, the frame current there, the return-stack and the
defer-stack lengths). `ATOMIC_ATTEMPTS` = 8.

- `atomicbegin` (at pc `B`): with no `tx`, create one (depth 1, attempts 0,
  `restart` = (B, frame, stack lengths)) and **start** it; with `depth > 0`,
  `depth += 1` (an inner `atomic` **joins**; nothing else); with depth 0 (a
  restart), `depth = 1`, mark it `irrevocable` if `attempts >= 8`, and start
  it. **Start**, under the runtime lock: an irrevocable attempt first
  acquires the exclusivity token; then `rv = clock`.
- `sharedget k name mode dest` in a transaction: on the first access of `k`,
  read `(v, ver)` from the store; `ver > rv` → **restart (conflict)**; else
  `reads[k] = ver` and a new entry `{name, base: v, working: a local copy of
  v, assigned: false, exposed: false}`. Mode 1 (**working**, written
  lexically inside `atomic`): `exposed = true`, return the working value
  itself (so `xs.push(1)` changes it). Mode 0 (a helper's read): return a
  local copy of the working value.
- `sharedset k name src` in a transaction: `w` = a **local copy** of the
  value (Promises kept as the same object; never the value itself, so a
  working value is reachable only through its own variable); no entry →
  a new one `{name, base: none, working: w, assigned: true, exposed: false}`
  (a blind write: no read-set record); else `working = w`, `assigned = true`.
- `atomicend` (no `tx` → `Internal` `atomicend outside a transaction`):
  `depth > 1` → `depth -= 1`, done. Else build the publish list, in entry
  order: skip an entry that is neither assigned nor exposed; strict-copy its
  working value (a Promise inside → stop: end the attempt, clear `tx`, throw
  `ThreadError not_sendable` `shared variable 'NAME' can't hold a Promise`
  for that entry); skip an unassigned entry whose copy is **same** as its
  `base` (below); else publish it. Then **commit**, under the runtime lock:
  with nothing to publish, just end (a read-only transaction neither waits
  nor validates); else wait while another transaction is exclusive,
  **validate** (every `reads[k]` still equals `k`'s version, absent = 0;
  else **restart (conflict)**), `clock += 1`, store every published
  `(copy, clock)`, wake their `retry` waiters. Release the token if held;
  clear `tx`.
- `atomicabort` (the handler; no `tx` → `Internal` `atomicabort outside a
  transaction`): `depth > 1` → `depth -= 1` (flat nesting: the inner block's
  changes are not undone). Else end the attempt (release the token if held)
  and clear `tx`: **nothing is published**, and the handler re-throws the
  error unchanged.
- `retry` (no `tx` → `Internal` `retry outside a transaction`): an implicit
  owner → `RuntimeError` (`Internal`) `'LABEL' cannot suspend (it used
  'retry') when called implicitly by the runtime`; empty `reads` →
  `ThreadError stuck` `retry can never wake up: this transaction read no
  shared variable`; else **restart (retry)**.

**Guards and the safety net.** Starting a transaction in `atomicbegin`, and
the publish/commit steps of `atomicend`, end the whole transaction (release
the token, clear `tx`) on any error other than a restart, then let the error
unwind from that instruction. When a task ends with a `tx` it owns, the VM
ends that transaction first. After such an error the task is outside any
transaction.

**Restart** is a non-local transfer to the owner: the step loop handles it
before any other error handling, when the task owns its `tx` (an implicit
sub-task that joined someone else's transaction passes it up to the
owner's step). It restores the pc, frame and stack lengths of `restart`
(no `defer` of the abandoned attempt runs) and releases the token if held.
A conflict: `attempts += 1`, `depth = 0`, `reads`/`entries` emptied,
`irrevocable = false`, and execution continues at `atomicbegin`. A retry:
`tx` is cleared and the task waits for the read set (below); when woken it
runs `atomicbegin` again, as a new transaction (the failure count starts
again from 0); a failed wait is thrown at the `atomic` expression, outside
the transaction.

**`retry` waits.** Under the runtime lock: if a variable of the read set
already has another version, run again at once. Else create a pending
Promise, register it as an internal wait of this VM (pending work: it keeps
the VM — a job, or the program — alive), and record it as a watcher of every
index of the read set. Every commit that publishes and every plain write
wakes (settles with `none`) and unregisters every watcher of the indices it
wrote. Quiescence fails `retry` waits like any internal wait (`stuck`).
Removing a waiter (quiescence, teardown) also unregisters it.

**Same.** Whether an exposed, unassigned entry changed: a parallel,
iterative walk over two strict copies with two memos (left → right and
right → left). `none`/Bool/String: same kind and equal; Number: numerically
equal; Type: equal kind and index; heap pairs of different kinds, or a left
already paired with another right (or the reverse) → not same; Vector:
equal lengths, items pairwise; Map: equal lengths, the same keys in the
same order, values pairwise; Bytes: equal contents; struct: equal
**internal type identity** (the module-scoped type name that `==` and
`matchstruct` compare, never a source name), the same field names in
order, fields pairwise; enum: also the same variant. A closure or frame
anywhere → not same (always published). It only decides whether a version
is bumped; it never changes what a program prints.

**Exclusivity.** One token: `excl_owner` (none or a transaction serial and
its VM), a FIFO `excl_queue` of serials waiting for it, and
`commits_waiting`. Acquire (an irrevocable start): join the queue; wait
until the token is free, this serial is at the front and
`commits_waiting == 0`; take it. A commit with something to publish, or a
plain write, while another transaction owns the token: `commits_waiting +=
1`, wait until it is released, `commits_waiting -= 1`. Release: when the
owning transaction's attempt ends in any way — commit, conflict, `retry`,
abort, a `not_sendable` refusal, a guarded error, the end of the owning
task, or its VM's teardown. Waits are OS-level (the VM thread blocks;
Python VMs poll every 50 ms for test deadlines and exits). At most one
transaction is exclusive; it can't conflict (nothing publishes after its
`rv` until it releases), so it commits on that attempt; and it never waits
for anything while it holds the token (`.await`, `detach`, every waiting
native and `process.exit` are refused in a transaction, its own commit
doesn't wait, nested `atomic`s join, an implicit call's `atomic` joins), so
the waits-for relation between threads has no cycle: no deadlock.
Exclusive waiters are served FIFO once the commits already waiting have
gone through.

**Refused natives.** While the task has a `tx` (lexically inside or in a
called function), these throw `ThreadError in_atomic` `'NAME' can't run
inside 'atomic { }': its body may run more than once` before doing
anything: the `await` opcode (NAME `.await`, whatever the Promise's state),
the four `detach*` opcodes (NAME `detach`), and a `native` call of one of
these 53 (NAME = the native's name): `io.print`, `io.write`, `io.input`,
`io.read_line`; `time.sleep_async`, `time.cancel`; `promise.resolve`,
`promise.fail`; every `fs.*` native (`fs.read_text`, `fs.write_text`,
`fs.append_text`, `fs.info`, `fs.list_dir`, `fs.mkdir`, `fs.remove`,
`fs.rename`, `fs.copy`, `fs.temp_dir`, `fs.open`, `fs.read_line`,
`fs.read_all`, `fs.write`, `fs.close`, `fs.read_bytes`, `fs.write_bytes`,
`fs.append_bytes`, `fs.file_read_bytes`, `fs.file_write_bytes`);
`process.exit`, `process.run`, `process.env_set`, `process.env_remove`;
every `socket.*` native (`socket.connect`, `socket.listen`, `socket.accept`,
`socket.send`, `socket.recv`, `socket.shutdown`, `socket.close`,
`socket.start_tls`, `socket.tls_server_config`, `socket.start_tls_server`);
`thread.spawn`, `thread.submit`, `thread.close`, `thread.join`,
`thread.semaphore_acquire`, `thread.semaphore_try_acquire`,
`thread.semaphore_release`, `thread.channel_send`, `thread.channel_recv`,
`thread.channel_try_recv`, `thread.channel_close`. Every other native is
allowed (pure ones, `random.*`, `hooks.*`, clock and environment reads,
`promise.new`, new semaphores/channels, live counts). The throw is an
ordinary, catchable error.

**Deadlocks.** The wait-for graph's nodes are tasks; edges: a task
suspended on `await` of a Promise whose producer is known → that producer:
a same-VM `detach` task (plain), a job's root task once it has started
(strong), or every running job's root task of a joined thread (strong).
Before an `await`/join suspension adds an edge, the VM checks (under the
runtime lock) whether the graph leads back to the waiting task along a path
that, with the new edge, contains a strong edge; if so the wait fails at
once with `deadlock`. Await edges are tracked only once the run has spawned
a thread. Semaphores, channels, `retry` waits, queued jobs and Promises
without a producer are not edges.

**Quiescence.** A VM is blocked when it waits for a completion with no
timeout, has no timers, an empty done queue, and only runtime waits pending
(no file, socket, process or stdin operation). When every live VM is
blocked and no thread has a queued job a free worker is about to start
(nor a closed pool whose idle workers are about to exit), the main VM is
posted a `stuck` completion: every runtime wait it has fails, in creation
order, with `ThreadError stuck` (`the wait can never finish: every thread
is waiting`), and it continues. A wait only another running thread could
end keeps waiting.

**Shared process resources.** stdout is shared, and each VM keeps a line
buffer: a write hands everything up to the last `\n` to the shared writer
in one piece, and the rest is handed over at every flush point (before
blocking or sleeping, before reading stdin, at a job's end before its
reply, at program end, before an error report, in `process.exit`), so lines
from different threads never tear. The file and socket tables are shared
(only the main VM closes them at the end); stdin has one reader, lines
handed out in request order; `time.monotonic_ms` counts from the run's
start in every VM; `process.args()` is the same everywhere; `process.exit`
in a job exits the whole process (the first exit wins).

**Program end.** Unchanged (§6.4), with the wider pending rule; then a VM
abandons any job VM still blocked and the process exits.

## 7. Versioning and extension rules

- **Minor version** (backwards compatible for readers): new natives, new
  opcodes (from the reserved values), new constant tags, new optional
  sections, new built-in types at the next free type indices, and
  (required only from that minor on, exactly like PARAMS was for 1.1) new
  *required* sections. A 1.x VM runs any 1.y file with y ≤ x.
- **Major version**: anything that changes the meaning or encoding of
  existing items.
- **1.1** added parameter names and defaults (the PARAMS
  section), keyword-argument calls (`callkw`, `callmethodkw`, `detachkw`,
  `detachmethodkw`, operand kind `S*`), `jmpset`, and the `io.write`
  native. A 1.1 VM still runs 1.0 files: without PARAMS, every parameter is
  unnamed and required, which is exactly 1.0's arity rule.
- **1.2** added `matchrange` (range patterns), the `le`/`ge`/`not`
  operators, and the native inherent
  methods `String.len`, `String.char_at`, and `Function.arity` (§6.7).
  Iterators, ranges, and their adapters (`map`, `filter`, `skip`, `take`,
  `reduce`) aren't VM features at all: they're ordinary Mah code (the
  compiler's *prelude*, `mah/std/prelude.mh`) compiled into the file like
  the user's own code, as ordinary TYPES entries, functions, and
  `defmethod`s. A VM needs nothing beyond the list above to run them.
- **1.3** added the `Vector` and `Map` value types, the `vector`/`map`
  opcodes that build them, their native inherent methods (§6.7), and the
  native `Index`/`IndexAssign` targets behind `x[k]`/`x[k] = v` (§6.9).
  Iterating them and `Map.entries()` are prelude code again (`impl
  Iterable for Vector`/`Map`, the `MapEntry` struct), needing nothing more
  from a VM.
- **1.4** added typed, catchable errors (docs/ERRORS.md): the `throw`,
  `matchtype`, `deferdepth`, and `deferabove` opcodes; the HANDLERS
  section (§4.8, required from 1.4); the built-in `RuntimeError` enum
  (type index 2, user types now numbered from 3); and `Promise`'s third
  variant, `Failed { error }`. Every VM-raised failure became a catchable
  `RuntimeError` value instead of an immediate abort (§6.8) -- an
  *uncaught* one still produces exactly the same message and location a
  1.0–1.3 VM already gave, so no existing error-message test needed to
  change. A 1.4 VM still runs 1.0–1.3 files unchanged (no HANDLERS
  section, no new opcodes, `Promise` only ever `Pending`/`Settled`, user
  types numbered from 2 as before).
- **1.5** added natives only: `math.tan`, `math.asin`, `math.acos`,
  `math.atan`, `math.atan2`, `math.exp`, `math.log`, and `math.log10`
  (§4.4), behind the standard library's `std:math`. From 1.5 on, the
  reference encoder writes the lowest minor its natives need (§3), so a
  program that doesn't use them is still a 1.4 file.
- The optional TESTS section (§4.9, for `mah test`) came without a minor
  version: older VMs skip it, and a program with one still runs normally.
- **1.6** added native methods only: the String methods `split`, `trim`,
  `trim_start`, `trim_end`, `pad_start`, `pad_end`, `replace`,
  `replace_all`, `starts_with`, `ends_with`, `contains`, `index_of`,
  `repeat`, `to_upper`, `to_lower`, `lines`, `parse_number`, and
  `Vector.join` (§6.7). The encoder writes 1.6 only for a program that
  calls a method by one of those names (§3).
- **1.7** added natives only: `value.type_name`, `value.fields`,
  `value.variant`, `string.chars`, `string.code_point`, and
  `string.from_code_point` (§4.4), behind the standard library's
  `std:json` and `std:csv`.
- **1.8** added natives only: `random.seed`, `random.fresh`,
  `random.next`, and `random.below` (§4.4), the shared generator behind
  `std:random`.
- **1.9** added natives only: `regex.find` and `regex.find_all` (§4.4),
  over `std:regex`'s canonical patterns.
- **1.10** added `io.read_line` (§4.4), the async `input`, and I/O
  operations in the scheduler loop (§6.4). The compiler stops emitting
  `io.input` for `input()`, but VMs keep implementing it, so older files
  run unchanged.
- **1.11** added natives only: `time.now_ms`, `time.monotonic_ms`,
  `time.cancel`, `promise.new`, `promise.resolve`, and `promise.fail`
  (§4.4), behind `std:time` and `std:async`.
- **1.12** added natives only: the fifteen `fs.*` natives (§4.4) behind
  `std:fs`, with open files as ids in a per-VM handle table.
- **1.13** added natives only: the ten `process.*` natives (§4.4) behind
  `std:process`, with a per-VM environment table.
- **1.14** added type values and reflection (docs/REFLECTION.md, M41a):
  the `Type` value and `loadtype` (§5, §6.1), spread calls
  (`callspread`, `callmethodspread`, and `spread`, which builds their
  argument Vector and Map), the seven `reflect.*` natives (§4.4) behind
  `std:reflect`, `json.decode` and `json.parse_as`, and the optional META
  section (§4.10). Older VMs run a file that only has META (it's skipped);
  the encoder writes 14 only for a file that uses one of the opcodes or
  natives above (§3).
- **1.15** added decorators as metadata (docs/REFLECTION.md, M41b): the
  `decorate` opcode (§4.6) and the `reflect.decorators` native (§4.4);
  `reflect.signature`/`reflect.schema` results gained their `decorators`
  elements. The encoder writes 15 only for a file that contains `decorate`.
- **1.16** added rest parameters, function-item impls and hooks
  (docs/REFLECTION.md, M41c): the PARAMS rest flags (§4.5a, with the
  binding of §6.1), `fn#<index>` method-table types and Function identity
  (§6.7), the `paramhooks` opcode (§4.6), and the eight `hooks.*` natives
  (§4.4); `reflect.signature`'s result gained `rest` and `kwrest`. The
  encoder writes 16 for a file with a rest flag, `paramhooks`, a `hooks.*`
  native or a `fn#` key (§3) -- which includes every file that imports
  `std:reflect`, `std:json` or has a decorator.
- **1.17** added `Bytes` (docs/STDLIB.md, M37): the value type (§5), its
  `+`, `==` and `to_string` (§6.2, §6.6), its native methods, `Index`/
  `IndexAssign` targets and `String.to_bytes` (§6.7, §6.9), the primitive
  type code 8 (`loadtype 1, 8`, §5, and in META, §4.10), the four `bytes.*`
  natives behind `std:bytes` and the five binary `fs.*` ones behind
  `std:fs` (§4.4). The encoder writes 17 for a file that lists one of those
  natives (so every file that imports `std:fs`), that calls a method named
  `to_bytes` outside the prelude, or that has `loadtype 1, 8` (§3). Since
  1.14–1.16 decoders refuse code 8, a file below 17 that annotates a type
  `Bytes` has the META `typeref` written as `unknown` (tag 0), not as code 8;
  a decoder accepts code 8 (in `loadtype` and META) only from minor 17, and
  otherwise reports `'loadtype' at instruction I: primitive type code 8 out of range` / `META: primitive type code 8 out of range`.
- **1.18** added `std:socket` (docs/STDLIB.md, M38): seven `socket.*`
  natives (§4.4) with a per-VM socket table, and nothing else (no opcode, type
  or method). The encoder writes 18 for a file that lists one of them, which
  only a program importing `std:socket` does. A 1.17 or older VM names them
  in its unsupported-minor error (§3).
- **1.19** added `socket.start_tls` (§4.4, M39), TLS on an open socket, behind
  std:socket's `start_tls`/`connect_tls` and `std:http`; and nothing else.
  The encoder writes 19 for a file that lists it, which every program
  importing `std:socket` does.
- **1.20** added `socket.tls_server_config` and `socket.start_tls_server`
  (§4.4, M42), TLS servers, behind std:socket's `tls_server_config`/
  `start_tls_server` and `std:http`'s `serve(tls:)`; and nothing else. The
  encoder writes 20 for a file that lists them, which every program importing
  std:socket does.
- **1.21** added threads (M44, docs/contracts/M44_threads.md) and
  transactions (M45, docs/contracts/M45_atomic.md): the shared-variable
  opcodes `sharedget`/`sharedset` (`0x70`/`0x71`), the transaction opcodes
  `atomicbegin`/`atomicend`/`atomicabort`/`retry` (`0x74`–`0x77`, §4.6) and
  the 19 `thread.*` natives behind `std:thread` (§4.4), with the model of
  §6.11 (job VMs, copies, the versioned shared-variable store, transactions,
  the exclusivity token, `retry` waits, join-cycle and quiescence detection,
  the natives refused in a transaction, `ThreadError`). `0x72`/`0x73` are
  unassigned. No new section, type or constant tag; the prelude gains the
  `ThreadError` struct. The encoder writes 21 for a file that uses one of
  those opcodes or lists a `thread.*` native: a program that declares a
  `shared let`, uses `atomic { }` or imports `std:thread`.
- Planned growth, for orientation: error-set checking (the static
  checker's `throws` inference and "unhandled error" diagnostics -- M26),
  string utilities, filesystem, networking, and process natives.
