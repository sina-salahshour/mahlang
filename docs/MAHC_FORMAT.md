# The `.mahc` bytecode format (version 1.3)

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
minor      u16       = 2          (0 or 1 for older files; see §7)
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
  one it implements (it may use opcodes/natives the VM doesn't know). An
  encoder writes the lowest minor version whose features it uses.
- **Required sections**, each present exactly once and in increasing id
  order: STRINGS (`0x01`), CONSTANTS (`0x02`), TYPES (`0x03`), NATIVES
  (`0x04`), FUNCTIONS (`0x05`), CODE (`0x06`), and — in files with minor ≥ 1
  only — PARAMS (`0x07`). A 1.0 file **must not** contain PARAMS; a 1.1 or
  later file **must**.
- Ids `0x08`–`0x7F` are reserved for future *required* sections: a VM
  **must** reject a file containing one it doesn't know.
- Ids `0x80`–`0xFF` are *optional* sections, allowed after CODE in any
  order: a VM **must** skip ones it doesn't know (using `length`). `0x80` is
  DEBUG (§4.7).
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

User-declared structs and enums. Type **indices** `0` and `1` are built in
and implicit (never written in the section); user types are numbered from
`2` in section order.

| index | type | variants (index: name {fields}) |
|---|---|---|
| `0` | `Option` | `0: none`, `1: some { value }` |
| `1` | `Promise` | `0: Pending`, `1: Settled { value }` |

```
count varuint
count × type
type   = kind u8, name str, body
  kind 0 (struct): nfields varuint, nfields × str          (field names, declaration order)
  kind 1 (enum):   nvariants varuint, nvariants × variant
variant = name str, nfields varuint, nfields × str
```

A struct and an enum may share a name; they're still distinct types.

### 4.4 NATIVES (`0x04`)

The host functions this program uses — the extension point for everything
a VM provides beyond pure computation.

```
count varuint
count × (name str, arity varuint)
```

Instructions refer to natives by their index in this table. A VM **must**
refuse to load a file listing a native it doesn't implement, or one whose
arity doesn't match its own. Native names are dotted, `module.function`.
Version 1.0 defines:

| name | arity | behavior |
|---|---|---|
| `io.print` | 1 | writes `to_string(v)` (§6.6) followed by `\n` to standard output; returns `none` |
| `io.write` | 1 | *(1.1)* writes `to_string(v)` (§6.6) to standard output with **no** newline; returns `none` |
| `io.input` | 0 | reads characters from standard input: skips characters until the first ASCII digit, then consumes digits up to and including the first non-digit (or end of input); returns that Number. End of input before any digit is a runtime error. |
| `math.sin` | 1 | sine of a Number (radians); the result is computed in IEEE-754 double precision and converted to Number via its shortest round-trip decimal text |
| `math.cos` | 1 | cosine, same rules |
| `time.sleep_async` | 1 | returns a new pending Promise that the scheduler settles with `none` after the argument's number of milliseconds (§6.4) |

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
                                   all other bits must be 0
```
Parameter names are needed to bind keyword arguments, and default flags to
know which parameters may be left unbound (§6.1). Function 0 has 0
parameters. In a 1.0 file (no PARAMS), parameters are unnamed and have no
defaults.

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
| `0x40` | `deferpush` | — |
| `0x41` | `deferadd` | closure `A` |
| `0x42` | `deferpeek` | dest `A` |
| `0x43` | `deferpop` | dest `A` |
| `0x44` | `deferscopepop` | — |
| `0x50` | `native` | fn `X`, args `A*`, dest `A?` |

All other opcode values are reserved. Opcodes, operand kinds, and natives
marked *(1.1)* **must not** appear in a file whose minor version is 0,
those marked *(1.2)* not in one whose minor version is below 2, and those
marked *(1.3)* not in one whose minor version is below 3.

In the `*kw` opcodes, `kwnames` names the **last** `len(kwnames)` entries
of `args`, in order; the entries before them are positional. `len(kwnames)
≤ len(args)`, and the names are distinct (validated at load). `native`'s `args` count **must**
equal the native's declared arity (validated at load). `struct`/`enum`'s
`values` count **must** equal the type's/variant's field count. `map`'s
`pairs` count **must** be even (validated at load).

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

## 5. Values

| type name | values |
|---|---|
| `Number` | decimal numbers. VMs **should** use base-10 arithmetic with at least 28 significant digits (the reference VM uses exactly 28, rounding beyond that); integer-valued results must be exact within that precision |
| `String` | immutable Unicode text |
| `Bool` | `true`, `false` |
| `Option` | enum type 0; `none` is a single shared value |
| `Promise` | enum type 1, plus scheduler state (§6.4) |
| `Function` | a closure: (function index, defining frame) |
| `Vector` *(1.3)* | an ordered, growable list of values, indexed from `0`; **mutable, by reference** |
| `Map` *(1.3)* | an insertion-ordered table from keys (Strings, Numbers, Bools) to values; **mutable, by reference** (§6.9) |
| user struct | (type, field values in declaration order), **mutable, by reference** |
| user enum | (type, variant, field values), mutable, by reference |

The **type name** of a value (used by method dispatch): `Number`,
`String`, `Bool`, `Function`, `Option`, `Promise`, `Vector`, `Map`, or the
user type's name.

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
- `jmpset param L`: jump to `L` if `param` (always a slot of the current
  frame) holds a bound value, i.e. anything but the absent marker.
- `ret value`: return register ← value. If the task's return stack is empty,
  the task finishes with that value; otherwise pop `(pc, frame)` and
  continue there.
- `retval dest`: `dest ← return register`. Encoders emit it immediately
  after every `call`/`callkw`/`callmethod`/`callmethodkw`.
- `halt`: the task finishes with `none` (ends the main program's top-level
  code).

### 6.2 Operators
- `move src dest`: copy (values are references for structs/enums/functions/
  promises).
- `loadk k dest`: load the constant.
- `jmp L`; `jmpf cond L`: jump if `cond` is falsy.
- `add`: if either operand is a String, the result is
  `to_string(a) ++ to_string(b)` (§6.6); if both are Numbers, their sum;
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
  (identity). Values of different types are never equal. Result: Bool.
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
- `matchfail`: runtime error "No pattern in 'match' matched the value".
- `matchrange v lo hi inclusive dest` *(1.2)*: `dest ←` a Bool, true iff
  `v` and every present bound are all Numbers or all Strings, and (`lo`
  absent or `lo ≤ v`) and (`hi` absent, or `v < hi`, or `v ≤ hi` when
  `inclusive` is 1). It never raises: a value of another type simply
  doesn't match. At least one of `lo`/`hi` is present (validated at load).
  Strings compare as `lt` does.

### 6.4 Tasks, promises, and the scheduler
Single-threaded cooperative scheduling:
- A **Promise** is an enum instance of type 1: `Pending`, or `Settled
  { value }` once resolved; it also holds an internal list of waiting
  continuations. Resolving an already-settled promise does nothing;
  resolving a pending one sets it to `Settled { value }` and then runs its
  waiting continuations **synchronously, in registration order**.
- `detach callee args dest` / `detachkw callee args kwnames dest`: bind
  arguments like `call`/`callkw` (errors happen here, in the calling task);
  create a new pending Promise `p` and a new task whose frame is set up
  like `call` (empty
  return stack), with `p` as the promise it resolves when finished. **Run
  the new task immediately**, until it finishes (then resolve `p` with its
  result) or suspends. Then `dest ← p` and the calling task continues.
- `detachmethod recv name args trait dest` / `detachmethodkw ...`: method
  lookup exactly like `callmethod` (§6.7), then like `detach`/`detachkw`
  with the resolved function and the argument list including `recv` when
  it's a method call. If the target is a native method, call it and `dest ←`
  a Promise already settled with its result (keyword arguments to a native
  method → runtime error, unexpected keyword argument).
- `await p dest`: `p` must be a Promise (else runtime error). If settled,
  `dest ← value`. Otherwise the current task **suspends**: register a
  continuation on `p` that writes the value to `dest` and resumes the task
  at the next instruction; control returns to whatever was running the task
  (the `detach` that started it, or the scheduler loop).
- `time.sleep_async(ms)` returns a pending promise and registers a **timer**
  at `now + ms`.
- **Scheduler loop**: after task 0 is first run (it runs until it halts or
  suspends), repeat: if task 0 has finished and no timers remain, the
  program ends; otherwise take the earliest timer (ties: registration
  order), wait until it's due, and resolve its promise with `none`. So the
  program ends only once the main code has finished *and* no scheduled work
  remains.
- When a detached task finishes, its promise is resolved with the task's
  result (running the promise's continuations).

### 6.5 Defer
Each task has a defer stack of scopes; a scope is a stack of closures.
- `deferpush`: push an empty scope.
- `deferadd c`: push closure `c` onto the top scope.
- `deferpeek dest`: `dest ← (top scope is non-empty)` as a Bool.
- `deferpop dest`: pop the top scope's most recent closure into `dest`.
- `deferscopepop`: discard the (empty) top scope.
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
   - Vector *(1.3)*: `[` + the items' `to_string`s joined by `, ` + `]`
     (`[]` when empty).
   - Map *(1.3)*: `[` + `to_string(k) + ": " + to_string(v)` for each entry
     in insertion order, joined by `, `, + `]`; `[:]` when empty.

### 6.7 Methods
(Terminology: a *native target* here is the VM's own built-in
implementation of a system-trait method. It is unrelated to the NATIVES
section (§4.4) and is never listed there.)

The VM keeps a **method table** keyed by `(type name, method name)`; each
entry holds at most one *inherent* target and at most one target per trait
name. A target is (function value or native, `is_method`).
- Initially: for every built-in type name (`Number`, `String`, `Bool`,
  `Function`, `Option`, `Promise`, and *(1.3)* `Vector`, `Map`), a native
  target for trait `Printable`, method `to_string`, `is_method` true,
  computing §6.6 step 2.
- Also initially *(1.3)*, native targets for `Vector`, `Map`, and
  `String` for trait `Index`, method `index` (one argument, `key`), and for
  `Vector` and `Map` (not `String`: Strings are immutable) for trait
  `IndexAssign`, method `index_assign` (two arguments, `key`, `value`),
  `is_method` true, behaving as §6.9 describes. Encoders compile `x[k]` to `callmethod x
  "index" [k] "Index"` and `x[k] = v` to `callmethod x "index_assign" [k,
  v] "IndexAssign"` (each followed by `retval`), so a user type that
  `defmethod`s those traits is indexable the same way.
- Also initially *(1.2)*, native **inherent** methods (all `is_method`
  true; arguments after the receiver are positional, and keyword arguments
  are a runtime error, except that *(1.3)* a parameter shown below as
  `name = default` is optional and may also be passed by keyword; binding
  then follows §6.1 as for a function with that defaulted parameter):
  | type | method | behavior |
  |---|---|---|
  | `String` | `len()` | the number of Unicode code points, as a Number |
  | `String` | `char_at(i)` | the code point at index `i` (an integer Number, `0 ≤ i < len`) as a one-character String; anything else is a runtime error |
  | `Function` | `arity()` | the function's `param_count` (including defaulted parameters) |
  | `Vector` *(1.3)* | `len()` | the number of items |
  | `Vector` *(1.3)* | `push(x)` / `push_start(x)` | append `x` at the end / insert it at index 0; returns `none` |
  | `Vector` *(1.3)* | `pop()` / `pop_start()` | remove and return the last / first item, or `none` if empty |
  | `Vector` *(1.3)* | `copy(deep = false)` | a new Vector with the same items. Shallow unless `deep` is truthy; deep copies are described in §6.9 |
  | `Map` *(1.3)* | `len()` | the number of entries |
  | `Map` *(1.3)* | `keys()` / `values()` | a new Vector of the keys / values, in insertion order |
  | `Map` *(1.3)* | `has(k)` | Bool: whether `k` is present (a non-key `k` is a runtime error, §6.9) |
  | `Map` *(1.3)* | `remove(k)` | remove `k`'s entry and return its value, or `none` if absent |
  | `Map` *(1.3)* | `copy(deep = false)` | a new Map with the same entries in the same order; shallow unless `deep` is truthy (§6.9) |
  Wrong argument counts give the usual `Argument Count is invalid.
  method 'NAME' accepts N arguments but M was given`.
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

### 6.8 Runtime errors
Version 1.0 has no error values and no way to catch an error: a runtime
error stops the whole program with a message. With DEBUG present, VMs
should add the failing instruction's location: the reference CLI appends
`at position #LINE:COL` for the entry file and `at position FILE#LINE:COL`
for others. Without DEBUG (a `--target release` build), the message is
reported alone, with no location or instruction index.

Structured errors (error values, a way to catch or propagate them) are
planned. They'll arrive as a minor version: new opcodes, and probably a
built-in error type alongside `Option`/`Promise`. Nothing in 1.0 has to
change for that.

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
- **Deep copy** (`copy(deep: true)`): copies every Vector, Map, and struct
  and enum instance reachable from the receiver; every other value
  (Numbers, Strings, Bools, Functions, `none`, Promises) is shared, not
  copied. An object reachable twice is copied once, so sharing (and cycles)
  inside the original is reproduced in the copy. `index_assign(v, i, x)` replaces
  the item and returns `none`; when `i` names no item it's a runtime error.
- **Map keys** must be Strings, Numbers, or Bools; any other key given to
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

## 7. Versioning and extension rules

- **Minor version** (backwards compatible for readers): new natives, new
  opcodes (from the reserved values), new constant tags, new optional
  sections, new built-in types at the next free type indices. A 1.x VM runs any 1.y file with y ≤ x.
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
- Planned growth, for orientation: string utilities, filesystem,
  networking, and process natives, structured errors.
