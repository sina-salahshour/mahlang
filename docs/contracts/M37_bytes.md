# M37 work contract: `Bytes` (bytecode 1.17)

This contract coordinates the subagents that finish M37 in parallel. The core
(the compiler, both VMs, std modules and parity cases) has already landed in
the working tree. **Each agent edits only the files it owns (section 3).** Agents
read anything, never run `git commit`/`git push`/`git checkout`/`git stash`,
and never touch `www/`.

## 1. The design (normative; this is what landed)

**Value.** `Bytes` is a new built-in type: a growable sequence of bytes (whole
Numbers 0..255), **mutable and by reference** like a Vector. Differences from a Vector:
- `==` compares **contents** (two Bytes with equal bytes are `==`; Bytes never equals
  a Vector).
- Always truthy. Not usable as a Map key (the usual Map key error).
- `to_string`: `Bytes[` + each byte as two lowercase hex digits joined by a space + `]`
  (`Bytes[68 69]`, empty `Bytes[]`).
- `a + b` (both Bytes) is a new Bytes, the concatenation. `"x" + b` stays the
  String rule (the `to_string`).
- `Vector.copy(deep: true)` copies Bytes reached from it; a shallow copy shares them.
- No literal syntax. Primitive type code **8** (`loadtype 1, 8`; `reflect.type_of(b)` is the
  Type `Bytes`; the bare name `Bytes` is a Type value and a type annotation).

**Indexing** (system traits `Index`/`IndexAssign`, native for Bytes, same position
rules as Vector): `b[i]` is the byte as a Number, `none` when `i` names no item
(negative counts from the end); `b[a..b]` (any range form) is a new Bytes with
Vector's slice rules. `b[i] = n` sets a byte. Errors:
- byte not a Number → TypeMismatch `Bytes item must be a Number, got TYPE`
- not a whole 0..255 → ArgumentError `Bytes item must be a whole number from 0 to 255, got N`
- out of range → IndexOutOfRange `Bytes index N is out of range for Bytes of length L (use push to add items)`
- slice assign → TypeMismatch `Can't assign to a Bytes slice (b[a..b] = ...); assign items one at a time`
- non-Number index → TypeMismatch `Bytes index must be a Number, got TYPE`

**Native methods on Bytes** (all inherent):
| method | result |
|---|---|
| `len()` | Number of bytes |
| `push(n)` | appends a byte; `none`. Errors prefixed `push: the value` (`push: the value must be a whole number from 0 to 255, got -1`) |
| `pop()` | removes and returns the last byte, or `none` |
| `extend(other)` | appends `other`'s bytes (other must be Bytes: TypeMismatch `extend: other must be Bytes, got TYPE`); `b.extend(b)` doubles it; `none` |
| `copy()` | a new Bytes with the same bytes |
| `to_vector()` | a new Vector of Numbers |
| `to_text()` | `some(String)` if the bytes are valid UTF-8, else `none` |
| `to_text_lossy()` | String, each invalid UTF-8 sequence (maximal subpart) as U+FFFD |
| `to_hex()` | lowercase hex, two digits per byte |
| `to_base64()` | standard alphabet (RFC 4648 §4), `=` padded |
| `index_of(needle)` | `some(i)` of the first occurrence of the Bytes `needle` (`some(0)` for empty), else `none`; non-Bytes needle: TypeMismatch `index_of: needle must be Bytes, got TYPE` |

**`String.to_bytes()`**: the UTF-8 encoding, as Bytes. It's a 1.17 native method, so
`NATIVE_METHOD_SINCE_MINOR["to_bytes"] = 17`: a file calling any method named `to_bytes`
outside the prelude is written as 1.17.

**Iteration**: the prelude has `impl Iterable<Number> for Bytes` (it reuses
`VectorIterator`; live, like a Vector's). `for let x in b` gives Numbers.

**Natives (1.17, docs/MAHC_FORMAT.md §4.4)**:
| name | arity | behavior |
|---|---|---|
| `bytes.new` | 2 | `size, fill` → Bytes of `size` copies of `fill`. size not a Number: TypeMismatch `new: size must be a Number, got TYPE`; not whole ≥ 0: ArgumentError `new: size must be a whole number of at least 0, got N`; fill per the byte rule with prefix `new: fill` |
| `bytes.from_vector` | 1 | `items` (a Vector, else TypeMismatch `from_vector: items must be a Vector, got TYPE`) → Bytes; each item per the byte rule with prefix `from_vector: item I` (I its 0-based index) |
| `bytes.from_hex` | 1 | `text` (String, else TypeMismatch `from_hex: text must be a String, got TYPE`) → `some(Bytes)`, or `none` unless it is an even number of hex digits (either case) |
| `bytes.from_base64` | 1 | `text` → `some(Bytes)`, or `none` unless: length a multiple of 4, only the standard alphabet, `=` only as 1 or 2 trailing characters of the last group, no whitespace. The unused low bits of the last group are ignored (`Zh==` decodes like `Zg==`) |
| `fs.read_bytes` | 1 | `path` → Promise of a result (as the other fs natives): the whole file as Bytes |
| `fs.write_bytes` / `fs.append_bytes` | 2 | `path, data` (Bytes, else TypeMismatch `NAME: data must be Bytes, got TYPE`, NAME `write_bytes`/`append_bytes`) → `none`; replace (creating) / append (creating). The data is copied when the native is called |
| `fs.file_read_bytes` | 2 | `id, max` → up to `max` bytes from an open file (all the rest when `max` is `none`); fewer only at the end; empty Bytes at the end. max not a Number or none: TypeMismatch `file_read_bytes: max must be a Number or none, got TYPE`; not whole ≥ 0: ArgumentError `file_read_bytes: max must be a whole number of at least 0, got N`. A file open for writing: `other`, `the file isn't open for reading` |
| `fs.file_write_bytes` | 2 | `id, data` → `none`; data must be Bytes (TypeMismatch `file_write_bytes: data must be Bytes, got TYPE`). A file open for reading: `other`, `the file isn't open for writing` |

Text reads and binary reads can be mixed on one open file.

**Versioning**: MINOR is now 17. A file is 1.17 when it lists a 1.17 native, calls a method
named `to_bytes` (outside the prelude), or has `loadtype 1, 8`. Since std:fs now declares the
binary natives, **any program importing std:fs is 1.17** (like std:json being 1.16
through std:reflect). In META, a `Bytes` annotation is written as primitive code 8 only in a
1.17 file; otherwise as Unknown (tag 0), so 1.14-1.16 VMs still load it. Decoders accept
primitive code 8 (loadtype and META) only from minor 17.

**std:bytes** (`mah/std/bytes.mh`): `new(size = 0, fill = 0)`, `from_vector(items)`,
`from_hex(text) throws BytesError`, `from_base64(text) throws BytesError`,
`concat(parts: Vector<Bytes>)`, and `struct BytesError { kind, description }` (kind
`invalid_hex` / `invalid_base64`; description `from_hex: not valid hexadecimal: "TEXT"` /
`from_base64: not valid base64: "TEXT"`, TEXT cut to 40 characters + `...`; `message()` is the
description). Users catch it as `bytes.BytesError`.

**std:fs additions**: `read_bytes(path) -> Bytes`, `write_bytes(path, data)`,
`append_bytes(path, data)`, and on `File`: `read_bytes(max = none) -> Bytes`,
`write_bytes(data)`. All throw `FsError` like the rest (op names `read_bytes`,
`write_bytes`, `append_bytes`).

**Checker**: `Bytes` is a primitive type (`BYTES` in mah/compiler/types.py); `b[i]` is
Number, `b[a..b]` Bytes, `Bytes + Bytes` is Bytes, iteration elements are Number, and the
methods above have signatures (`pop()` is typed Number, like Vector's `pop()` is its T).

**Implementation map (already done, read-only for agents)**: `mah/runtime_values.py`
(`BytesValue`), `mah/bytes_methods.py` (reference implementation of every rule above),
`mah/code_interpreter.py`, `mah/natives.py`, `mah/fs_natives.py`, `mah/reflect_natives.py`,
`mah/bytecode/{format,lower,decode}.py`, `mah/compiler/{resolve,typecheck,types}.py`,
`mah/std/{prelude,bytes,fs}.mh`; Rust: `runtime/src/vm/bytes.rs` (new) plus `value.rs`,
`methods.rs`, `exec.rs`, `link.rs`, `natives.rs`, `fs.rs`, `reflect.rs`, `mod.rs`,
`runtime/src/decode.rs`; `runtime/tests/vm_diff.py` (cases `bytes`, `std_fs_bytes`).

## 2. Conventions (all agents)

- Read the files you touch before editing, and match their style: comment density,
  milestone tags like `M37 (1.17)`, plain wording. The project's docs are terse and
  factual; follow the neighbouring M35/M36 entries.
- Don't change behavior. If you find a bug in the landed code, **report it**; don't fix it
  outside your files.
- Run the commands listed for your task and report their exact results.

## 3. Tasks and file ownership

### Agent A: tests and example
Owns: `mah/std/bytes.test.mh` (new), `mah/std/fs.test.mh`, `tests/test_bytes.py` (new),
`examples/bytes.mh` (new), `tests/test_bytecode.py` (only the example→minor dict entry for
`bytes.mh`, if `examples/bytes.mh` needs one, which it does: 17).
- `bytes.test.mh`: `std:test` tests (see `mah/std/process.test.mh` and `docs/MAH_TEST.md` for
  the style; import with `import bytes from "./bytes"`) covering every method, indexing,
  slicing, `==`, `+`, iteration, deep copy, `bytes.*` functions, BytesError kinds and
  messages, base64/hex round trips and edge cases (`Zh==`, `Zg=`, `Z===`, `Zg==Zg==`).
- `fs.test.mh`: add tests for `read_bytes`/`write_bytes`/`append_bytes`, `File.read_bytes`
  (with and without `max`, 0, at the end), `File.write_bytes`, mixing text and binary on
  one file, and the open-mode errors. Follow that file's own temp_dir pattern.
- `tests/test_bytes.py`: unittest, both VMs via `tests/support.py`'s helpers (read how
  `tests/test_string_methods.py`/`test_stdlib.py` do it). Cover: runtime error messages and
  kinds (each message in §1), checker results (e.g. `mah check`-style diagnostics via the
  existing helpers: `b[0]` is Number, `b + 1` is an error, `"s".to_bytes()` is Bytes,
  `fs.read_bytes(p)` is Bytes), bytecode minor (`print("a".to_bytes())` → 17,
  `print(Bytes)` → 17, a program with neither → unchanged; `import fs` → 17), META
  downgrade (a non-1.17 file annotating `x: Bytes` still decodes, with the type written as
  Unknown, e.g. via `std:reflect` signature or by inspecting the decoded META), the decoder
  refusing `loadtype 1, 8` in a 1.16 file, and reflection (`reflect.type_of(b)` prints
  `Bytes`).
- `examples/bytes.mh`: a short, readable tour (like `examples/files.mh`), deterministic
  output, cleans up any temp files. `tests/test_examples.py` may need its expected output.
  Check how it works and follow it.
- Commands: `python3 -m unittest tests.test_bytes tests.test_stdlib tests.test_examples tests.test_bytecode -q`,
  the same with `MAH_TEST_VM=rust` (the Rust VM is built at runtime/target/release/mah-vm;
  don't run cargo), `python3 -m mah test --file mah/std/bytes.test.mh`, and the same with `--vm rust` if
  supported, and `python3 runtime/tests/vm_diff.py` (it runs examples too).

### Agent B: specification and design docs
Owns: `docs/MAHC_FORMAT.md`, `docs/STDLIB.md`, `docs/V2_DESIGN.md`, `docs/NEXT_PHASES.md`,
`README.md`, `docs/RUST_VM.md` (only if it lists natives/modules).
- `MAHC_FORMAT.md`: the 1.17 natives rows in §4.4 (with the error texts above), the Bytes row
  in §5 and the type-name list there, primitive code 8, `to_string` in §6.6, the Bytes
  methods + `String.to_bytes` + Index/IndexAssign in §6.7, Bytes in §6.9 (indexing, slicing,
  equality, deep copy), `add` and `eq` in §6.2, and §7's version history/minor rules (META
  writes Bytes as Unknown below 1.17; code 8 needs 1.17). Mirror how earlier minors are marked
  (`*(1.17)*`).
- `STDLIB.md`: status line (M37), a `### Bytes` / `std:bytes` section with ✅ Landed (M37)
  and its decisions (mutable, no literal, content equality, hex/base64, Option-returning
  `to_text`, fs binary I/O), update the `std:fs` "binary reads wait for Bytes" line, and
  `std:process`'s "Later" line (spawn still waits for the network work, but no longer for Bytes).
- `V2_DESIGN.md`: a milestone entry `42. **M37 — Bytes. ✅ Landed.**` after the M41c entry,
  in the style of the M35/M36 entries (decisions taken with the user: mutable, no literal
  yet, scope includes fs binary I/O and hex/base64; the files; versioning incl. the
  std:fs → 1.17 consequence and the META downgrade; intended test changes: MINOR pins
  16→17, `files.mh` and std:fs minor 12→17, unsupported-minor tests now use 18; tests added).
  Update the Status section if it lists milestones.
- `NEXT_PHASES.md`: the "Errors and the standard library" paragraph: M37 landed; next is
  sockets, the URL/HTTP client, the HTTP server, packages.
- `README.md`: "Where this is going" (Bytes done) and the milestone range ("M0 through
  M41c" → include M37), plus any std module list.
- Command: none required beyond re-reading; keep markdown tables well-formed.

### Agent C: language reference, project templates, editors
Owns: `mah/project/templates/docs/mah-language.md`, `mah/project/templates/AGENTS.md`,
`mah/project/templates/CLAUDE.md`, `syntax-highlight/queries/mah/highlights.scm`,
`syntax-highlight/grammar.js` (only if built-in type names are listed there),
`editors/vscode/syntaxes/mah.tmLanguage.json`, `editors/vscode/CHANGELOG.md` (only if it
has an "unreleased"/next section pattern for language changes; otherwise leave it),
any neovim/editor files listing built-in type names (search `editors/` and `syntax-highlight/`
for `Vector`), `mah/lsp/` only if it hard-codes built-in type names or hover text
for built-in types (search for `"Vector"`; the resolver tables already give completion).
- Document `Bytes` (methods, indexing, `+`, `==`, iteration, `to_bytes`), `std:bytes`, and
  the std:fs binary functions in the language reference wherever Vector/String methods and
  std:fs are documented; `tests/test_project.py`'s TemplateDriftTests must pass (it requires
  every built-in type name to appear in mah-language.md).
- Add `Bytes` wherever built-in type names are highlighted.
- Commands: `python3 -m unittest tests.test_project tests.test_lsp_hover_types_and_completion -q`.

## 4. Report back (every agent)
Files changed; the exact output of your commands (pass/fail counts); any judgment call
you made and why; anything in the landed code that looks wrong.
