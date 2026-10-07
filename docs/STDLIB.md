# Mah standard library

Status: **M27 landed 2026-09-28: Phase 0 steps 1, 2 and 5 (`std:`
resolution, `extern fn`, native table versioning) and `std:math`**, the
first module; **M29 landed the String methods** (Phase 1's first item);
**M30 landed `std:path`, `std:json` and `std:csv`**; **M31 landed
`std:random` (with Phase 0 step 6, the shared PRNG) and
`std:collections`**, and **M32 `std:regex`**, completing Phase 1;
**M33 made `input` async** (Phase 0 step 7, and step 4's I/O half);
**M34 landed `std:time` and `std:async`** (Phase 2, with step 4's
cancellable timers); **M35 landed `std:fs`** (with Phase 0 step 3,
handles); **M36 landed `std:process`**; **M37 landed `Bytes`, `std:bytes` and `std:fs`'s binary I/O**; **M38 landed `std:socket`** (TCP, Phase 4's first module); **M41a landed `std:reflect`** and `json.decode`
(see [`REFLECTION.md`](REFLECTION.md)). The rest is design. Agreed 2026-09-28. Depends on
[`ERRORS.md`](ERRORS.md) (every failure below is a thrown, typed error)
and on the static checker in [`TYPES.md`](TYPES.md).

```mah
import json from "std:json"
import "std:math"            # flat import works too
```

## Decisions

- **`std:` modules.** Every standard library module is imported as
  `"std:<name>"`. The prefix is reserved.
- **Errors are thrown**, never returned (see `ERRORS.md`). Each module
  has its own error type (`FsError`, `JsonError`, ...).
- **Module types are module-scoped and exported** (M41s). A standard
  library module's public structs, enums and traits are `export`ed and
  reached like its functions: `json.JsonError`, `fs.FsError`,
  `process.Output`, `reflect.TypeRef`, `regex.Regex`, `time.DateTime`,
  `random.Rng`, `async.TimeoutError`, `csv.FromCsvRow`, `json.FromJson`
  (bare, with a flat `import "std:collections"`: `Set`, `Deque`,
  `PriorityQueue`; `import "std:test"`: `AssertionError`, `SkipTest`). Helper
  types (`__Reader`, ...) are private. They still print and appear in
  messages under their plain names. This prose names each type by its
  declared name; prefix it with the module's namespace in code.
- **All I/O is async.** fs, process, socket, http and `input` return
  Promises. A bare call auto-awaits, like `sleep_async`, and `detach`
  runs it in the background. Pure modules (json, csv, regex, math, path,
  random, collections) are synchronous.
- **The Rust runtime may take a small, vetted set of dependencies**:
  `regex`, and `rustls` (ring provider) with `webpki-roots` for TLS. (The
  original plan named `ureq` for HTTP; M39 wrote HTTP in Mah over
  `std:socket` instead, so only TLS is native.) Anything small (glob
  matching, the PRNG, base encodings) stays hand-written. Each dependency
  gets a one-line reason in `runtime/Cargo.toml`, replacing the
  "standard library only" note.
- **Deserializing into structs** goes through traits (`FromJson`,
  `FromCsvRow`) that users implement by hand for now. They can be
  generated automatically once types are runtime values.
- **String helpers are methods on `String`**, not a `std:string` module.

## Phase 0: foundations

1. **`std:` resolution. ✅ Landed (M27).** `_resolve_import` in
   `mah/preprocessor.py` maps `std:<name>` to `mah/std/<name>.mh`, and
   never to a user file. An unknown std module (`std:prelude` included) is
   a compile error: `unknown standard library module 'std:nope'`. The LSP
   resolves go-to-definition and hover into std files. Bundles (`.mahc`)
   compile std code in like any other import. Locations inside a std file
   read `std:math#20:9` (compile errors, hover), never the install path. An
   uncaught runtime error from inside a std module is located at the
   program's own call into it (M30, like the prelude's since M29).
   `pkg:` is the other reserved prefix: it imports an installed package
   from GitHub (M43, `docs/PACKAGES.md`).
2. **`extern fn`. ✅ Landed (M27).** Std modules reach natives through
   declarations that only `std:` files (and the prelude) may use; anywhere
   else it's a compile error. `extern` is contextual (`let extern = 1`
   still works). The parser desugars it to an ordinary function whose body
   calls the native, so it's first-class and typed by its annotations; the
   resolver checks the native exists with that arity. No `Promise`-typed
   natives exist yet, so no async extern has been written:
   ```mah
   extern fn read_text(path: String) -> Promise<String, FsError> = "fs.read_text"
   ```
   The declaration is the native's type for the checker, so each std
   module is a thin Mah wrapper over natives plus pure-Mah helpers. Today
   builtins like `print` and `sleep_async` are special-cased in codegen;
   `extern fn` replaces that for everything new.
3. **Handle values.** A new opaque value kind for OS resources (`File`,
   `Socket`, `Process`, `Timer`): a type name plus a native index, with
   `close()`. Needed in both `mah/runtime_values.py` and the Rust `Value`
   enum (`runtime/src/vm/value.rs`). ✅ **Landed (M35), more simply**: a
   handle is a whole-Number id in a per-VM table (open files so far),
   wrapped by its module in an ordinary struct (`File { id, path, mode }`)
   with the methods. That gives the checker a real type, a closed or
   unknown id is a clear error (`FsError` kind `closed`), and it needed no
   new value kind in either VM or in the bytecode. (`Timer` never needed
   one: std:async's `TimerId` wraps a Promise.)
4. **Async scheduler.** Today the scheduler only handles `sleep_async`
   timers. It gains:
   - worker threads that run blocking I/O and settle a Promise (with a
     value, or an error per `ERRORS.md`); ✅ **landed (M33)** for standard
     input: one reader thread per VM, whose results the scheduler settles
     on the VM's own thread (docs/MAHC_FORMAT.md §6.4);
   - callback timers with cancellation, for `std:async`; ✅ **landed
     (M34)** as `time.cancel` (drop a `sleep_async` timer) plus
     `set_timeout`/`set_interval` in Mah on top of it;
   - "the program is done when the main task has finished and no timer
     or I/O is pending", as in Node. ✅ **Landed (M33).**
5. **Native table versioning. ✅ Landed (M27).** New natives bump the
   minor version (`docs/MAHC_FORMAT.md` §4.4/§7: the `std:math` ones are
   1.5). The encoder writes the lowest minor a file needs (1.4 unless it
   lists a 1.5 native), so old VMs keep running programs that don't need
   the new natives. A VM refusing a newer file reads its NATIVES section
   and names the natives it's missing: `unsupported minor version 6 (...):
   it uses natives this VM doesn't have ('fs.read_text'); upgrade mah to
   run it`.
6. **A shared PRNG. ✅ Landed (M31).** The same algorithm (PCG or
   xoshiro256**) in both runtimes, not Python's `random`, so seeded output
   is identical and `runtime/tests/vm_diff.py` keeps working. It's
   xoshiro256** seeded through splitmix64, as four 1.8 natives over a
   state Vector (docs/MAHC_FORMAT.md §4.4 specifies them bit for bit).
7. **`input` becomes an ordinary async function. ✅ Landed (M33).** See
   below.

### `input`

`input(prompt = "") -> String`, like Python's: prints `prompt` with no
newline, reads one line, returns it without the trailing `\n`/`\r\n`.
Throws `EndOfInput` at end of input. A stdin reader thread settles its
Promise, so `detach input()` lets other tasks and timers run while
waiting:

```mah
let answer = detach input("number: ")
let ticker = set_interval(fn() { print("waiting...") }, 1000)
let n = answer.await.to_number()
clear_interval(ticker)
```

This replaces today's digit-scanning `input()` that returns a Number, so
it breaks existing code. Updated in the same change:

- `input` stops being a lexer keyword (`mah/compiler/lexer.py`) and
  becomes a prelude/extern function taking a prompt.
- Examples:
  ```diff
  # examples/decimal_to_binary.mh, examples/new_decimal_to_binary.mh
  -let x = input()
  +let x = input("decimal: ").to_number()
  # examples/binary_to_decimal.mh
  -let x = input()
  +let x = input("binary: ").to_number()
  ```
- The language reference row in
  `mah/project/templates/docs/mah-language.md` and
  `www/src/content/docs/syntax-basics.md`:
  `input(prompt = "")`: prints `prompt`, reads one line from stdin,
  returns it as a String without the newline (use `.to_number()` to
  parse).
- Both runtimes' `io.input` (the Rust `DIGIT_BLOCK_ZEROES` table goes),
  `docs/MAHC_FORMAT.md` §4.4, the checker/LSP signature
  (`mah/lsp/analysis.py`), `docs/TYPES.md`, `docs/RUST_VM.md`,
  `docs/V2_DESIGN.md`, and the stdin fixtures in `tests/test_bytecode.py`,
  `tests/test_typecheck.py`, `runtime/tests/vm_diff.py`.

✅ **Landed (M33)**, with these decisions:

- `input` is an unbound-name built-in like `sin`/`cos` (a binding of your
  own named `input` wins), not a prelude function: the prelude can't hold
  top-level functions, and this way a bare `input()` compiles to the new
  1.10 native `io.read_line` plus an `await`, while `detach input()`
  compiles to the native alone and so hands back its pending Promise
  directly (like `sleep_async`, no task is needed). The checker types it
  `input(prompt: String = "") -> String throws EndOfInput`.
- `EndOfInput` is a prelude struct with no fields (`message()` is "end of
  input"), tracked by the checker like any error. The examples use `try
  input("decimal: ").to_number() else 0`, which the `strict` check of
  every example requires.
- `io.input` stays in both VMs, only no longer emitted, so `.mahc` files
  built before 1.10 keep running (the Rust `DIGIT_BLOCK_ZEROES` table
  stays with it).
- A detached `input` that's never awaited still keeps the program running
  until its line arrives (the "no I/O pending" rule); `mah test` gives
  tests an empty standard input, so `input()` there throws `EndOfInput`.
- `set_interval` in the example above waits for `std:async` (Phase 2):
  `sleep_async` in a loop does the same job today.

## Phase 1: pure libraries

### String methods

Native inherent methods (resolver tables, both runtimes,
`mah/std/builtins.d.mh`):

| Method | Notes |
|---|---|
| `split(sep = none, limit = none)` | no `sep`: split on runs of whitespace |
| `trim()`, `trim_start()`, `trim_end()` | |
| `pad_start(width, fill = " ")`, `pad_end(width, fill = " ")` | |
| `replace(from, to)`, `replace_all(from, to)` | `replace` does the first match only |
| `starts_with(s)`, `ends_with(s)`, `contains(s)` | |
| `index_of(s)` | `Option<Number>` |
| `repeat(n)` | |
| `to_upper()`, `to_lower()` | |
| `lines()` | splits on `\n` / `\r\n` |
| `to_number()` | throws `NumberParseError` (M29; see below); allows surrounding whitespace |

Plus `Vector.join(sep)`, and on `Option`: `unwrap()` (throws
`RuntimeError.UnwrapNone`), `unwrap_or(default)`, `is_some()`,
`is_none()`.

✅ **Landed (M29)**, with these decisions (docs/MAHC_FORMAT.md §6.7 has
the exact rules; `mah/string_methods.py` is the reference):

- Native methods on both VMs (bytecode 1.6, gated by method name, §3).
  Positions count code points and whitespace is Unicode White_Space,
  defined explicitly so the two VMs can't drift apart on edge cases.
  `split`'s `limit` counts splits (Python's `maxsplit`); `pad_*` cut the
  last repetition of `fill` to fit; `lines` drops a `\r` only before a
  `\n`; `Vector.join`'s `sep` defaults to `""` and uses each item's
  `to_string`.
- `to_number()` throws **`NumberParseError { text }`**, not `ParseError`:
  programs commonly declare their own `ParseError` (`examples/errors.mh`
  does), and a prelude type of that name would clash with it. It's a
  prelude trait method (`ToNumber`) over a new native method,
  **`parse_number()`**, which returns the Number or `none` and is public
  too. The checker tracks `NumberParseError` like any error.
- `unwrap()` on `none` throws `RuntimeError.ArgumentError` ("unwrap: the
  value is none") rather than a new `UnwrapNone` variant, which would
  change the built-in `RuntimeError` layout in the bytecode. The Option
  helpers are an `impl<T> Option<T>` in the prelude.
- Fully annotated prelude methods are now typed from their annotations
  (bodies unchecked), which is how `to_number`'s `throws` reaches the
  checker.
- An uncaught error thrown inside the prelude is now located at the
  innermost call outside it (the thrown value's backtrace), not at a
  prelude line.

### `std:math`

`sqrt`, `pow`, `abs`, `floor`, `ceil`, `round(digits = 0)`, `min`, `max`,
`clamp`, `sin`, `cos`, `tan`, `asin`, `acos`, `atan`, `atan2`, `log`,
`log10`, `exp`, and the constants `pi`, `e`.

✅ **Landed (M27)**, in `mah/std/math.mh`, with these decisions:

- `sqrt`, `pow`, `abs`, `floor`, `ceil`, `round`, `min`, `max` and `clamp`
  are plain Mah on Numbers (`sqrt(x)` is `x ** 0.5`), so they're exact to
  28 digits and identical on both VMs. `round` rounds halves away from
  zero; `min`/`max` take two arguments.
- The trigonometric, exponential and logarithmic functions are 1.5
  natives computed in double precision, like the old `sin`/`cos`. A
  domain error or overflow (`log(0)`, `asin(2)`, `sqrt(-1)`) throws
  `RuntimeError.ArgumentError`.
- `sin`/`cos` stopped being lexer keywords, so `std:math` can export them
  and `math.sin(x)` parses. An unbound `sin(x)`/`cos(x)` is still the
  built-in, with the same bytecode as before, and any binding of the name
  (a user's `fn sin`, a flat `import "std:math"`) wins. Removing the
  built-ins entirely would break existing programs for no gain.

### `std:random`

| Function | |
|---|---|
| `random()` | a Number in [0, 1) |
| `randint(lo, hi)` | inclusive at both ends |
| `choice(vec)` | one random element; throws on an empty Vector |
| `shuffle(vec)` / `shuffled(vec)` | in place / a shuffled copy |
| `sample(vec, k)` | `k` distinct elements |
| `seed(n)` | seeds the module's default generator |
| `Rng.new(seed)` | an independent generator with the same methods |

✅ **Landed (M31)**, in `mah/std/random.mh`, with these decisions:

- The generator is xoshiro256** seeded through splitmix64 (Phase 0 step
  6), not Python's Mersenne Twister, so a seeded run prints the same
  numbers on both VMs. `Rng { state }` holds its four 64-bit words in a
  Vector of Numbers, since there are no handle values yet.
- `random()` is a multiple of 2^-53 (the top 53 bits of an output over
  2^53), like a double, then divided with Mah's own 28-digit Numbers.
  `uniform(lo, hi)` is added: `lo + (hi - lo) * random()`.
- `randint` and `choice` use `random.below`, which rejects outputs past
  the largest multiple of the range, so they're unbiased. `randint`
  accepts any whole bounds up to a range of 2^64.
- Without `seed`, the module's generator (and `Rng.new()` with no seed)
  starts from the operating system's randomness. `seed(n)` takes any
  whole Number smaller than 2^64 in size.
- Bad arguments (an empty `choice`, `lo > hi`, `k` out of range) throw
  `RuntimeError.ArgumentError`, like `std:math`'s domain errors, rather
  than a `RandomError`: they're programming mistakes, which the checker
  doesn't track.

### `std:json`

- `parse(text)` returns Map, Vector, Number, String, Bool or `none`.
  Throws `JsonError`.
- `stringify(value, indent = 0)` serializes Map, Vector, Number, String,
  Bool, `none`, and structs/enums by field name.
- `trait FromJson { fn from_json(value) -> Self }`, implemented per
  struct and called as `Point.from_json(json.parse(text))`. It throws
  `JsonError` on a shape mismatch.
- `decode(t, value)`, `decode_ref(r, value)` and `parse_as(t, text)`
  read a value into a declared type (below, M41a).

✅ **Landed (M30)**, in `mah/std/json.mh`, with these decisions:

- **`JsonError` is an enum**: `Syntax { message, line, column }` for text
  that isn't JSON (the message ends "at line L, column C", counting code
  points from 1), and `Shape { message }` for a value of the wrong shape
  (`stringify` of a function or of a value that contains itself, or a
  `FromJson` helper given the wrong type).
- `parse` is strict RFC 8259: no comments, trailing commas, leading zeros
  or unescaped control characters; `\u` escapes must pair surrogates. A
  duplicate key keeps the last value. Objects become Maps in document
  order. Its type is `Unknown` (the checker has no union types).
- `stringify` writes compact JSON (`{"a":1}`), or with `indent` spaces per
  level, one item per line and `": "` after keys. Numbers are written as
  `print` shows them, which is always plain decimal, so valid JSON. Map
  keys are written as their text. A struct is an object of its fields;
  an enum value is its variant's name for a unit variant (`"Empty"`), else
  `{"Circle": {"r": 2}}` (externally tagged); `some(x)` is `x`.
- For `FromJson` impls: `field(object, name)` and `as_number`,
  `as_string`, `as_bool`, `as_vector`, `as_map(value, what = "the
  value")`, each throwing `JsonError.Shape` ("expected a Number for x, got
  String", "missing field 'y'").
- Built on six new 1.7 natives (docs/MAHC_FORMAT.md §4.4): reflection
  (`value.type_name`, `value.fields`, `value.variant`) and characters
  (`string.chars`, `string.code_point`, `string.from_code_point`). They're
  private to std modules (`extern fn`).

✅ **`decode` landed (M41a)**, in `mah/std/json.mh` over `std:reflect`, with
these decisions:

- `decode<T>(t: Type<T>, value: Unknown) -> T throws JsonError` reads a
  parsed value as the type `t` (a bare type name is a value now,
  docs/REFLECTION.md), `parse_as(t, text)` is `decode(t, parse(text))`, and
  `decode_ref(r, value)` reads a `TypeRef` from `std:reflect` (a field's
  `type`, say).
- By annotation: `Number`, `String`, `Bool` (checked), `Vector<T>` and
  `Map<String, V>` (each item/value read as `T`/`V`; without arguments the
  items pass through), `Option<T>` (`none` stays `none`, anything else is
  `some(read T)`), a struct (a Map; each declared field read by its
  annotation), an enum (a String for a unit variant, or a one-key Map
  `{"Circle": {"r": 2}}`, the inverse of `stringify`). `Unknown`, an
  unannotated field, a type parameter, `Self`, a trait or a function type
  take the value as it is. A type that implements `FromJson` is read by its
  own `from_json`, found through `reflect.methods`, so a hand-written
  decoder still wins.
- A missing key is `none` for an `Option` field and `JsonError.Shape`
  `missing field 'x' for P` otherwise; keys the struct doesn't declare are
  ignored.
- **Errors say where**, with the root type's name then `.field`, `[i]` or
  `["key"]`: `expected a Number for P.x, got String`, `expected a String
  for Q.tags[0], got Number`, `expected a Number for Q.m["k"], got String`,
  `missing field 'x' for P.p`, `unknown variant 'Nope' for Shape`.
- `std:json` now imports `std:reflect`, so a program that imports it is
  written at bytecode 1.14 (it was 1.7; 1.16 since M41c, whose hook natives
  `std:reflect` lists). Since M41s `std:reflect`'s types
  (`TypeRef`, `Param`, `Signature`, `Field`, `Variant`, `Schema`, `Method`,
  `ReflectError`) are module-scoped, so a program of its own with a `Field`
  struct can import `std:json` freely.

### `std:csv`

- `parse(text, delimiter = ",", header = false)` gives
  `Vector<Vector<String>>`, or `Vector<Map<String, String>>` with a header
  row. RFC 4180 quoting. Throws `CsvError`.
- `stringify(rows, delimiter = ",", header = none)`.
- `trait FromCsvRow { fn from_csv_row(row) -> Self }`, used the same way.

✅ **Landed (M30)**, in `mah/std/csv.mh`, with these decisions:

- **Two functions instead of a `header` flag**, so each has one type:
  `parse(text, delimiter = ",") -> Vector<Vector<String>>` and
  `parse_records(text, delimiter = ",") -> Vector<Map<String, String>>`
  (the first row names the columns; every row must have as many fields,
  and a name can't repeat). Likewise `stringify(rows, delimiter = ",")`
  and `stringify_records(records, delimiter = ",", columns = none)`, whose
  header is `columns` or the first record's keys; a missing column is an
  empty field, an unknown key an error.
- **`CsvError { message, line }`**, `line` counting from 1 (0 when it isn't
  about a line, like a bad delimiter).
- Lines end with `\n` or `\r\n` (kept as is inside quotes). A blank line
  is skipped rather than read as a row of one empty field, so a trailing
  blank line doesn't add a row; `stringify` writes such a row as `""` to
  keep it. A quote inside an unquoted field, or anything but the delimiter
  or a line break after a closing quote, is an error rather than guessed
  at.
- `stringify` ends every row with `\n`, writes `none` as an empty field and
  other non-Strings as they print, and quotes only fields holding the
  delimiter, a quote or a line break.
- `FromCsvRow.from_csv_row(row: Map<String, String>)`, with a
  `column(row, name)` helper that throws `CsvError` for a missing column.

### `std:path`

`join(...)`, `dirname`, `basename`, `extension`, `stem`, `normalize`,
`is_absolute`, `relative(from, to)`. Pure string logic, POSIX and Windows
separators.

✅ **Landed (M30)**, in `mah/std/path.mh` (plain Mah over the String
methods, so bytecode 1.6), with these decisions:

- No variadics yet, so `join(a, b)` takes two parts and `join_all(parts)`
  a Vector. An absolute second part replaces the first, like Python's
  `os.path.join`.
- Input may use `/` or `\` and start with a drive (`C:`); `normalize`
  and `relative` always answer with `/`. `normalize` never climbs above a
  root (`/../a` is `/a`) but keeps leading `..` in a relative path.
- `extension` is from the last `.` of the last segment (`.gz`), and a
  name that only starts with one (`.bashrc`) has none. `relative` answers
  `to` itself, normalized, when the two have different roots.

### `std:collections`

`Set`, `Deque` and `PriorityQueue`, written in Mah on top of `Map` and
`Vector` (no natives). Each implements `Iterable`, so `for` and
`map`/`filter`/`reduce` work on them.

✅ **Landed (M31)**, in `mah/std/collections.mh`, with these decisions:

- **`Set<T>`** is a Map from value to `true`, so values must be Map keys
  (Strings, Numbers, Bools), and it keeps first-added order. `Set.new()`,
  `Set.of(values)`, `add`, `remove` (whether it was there), `has`, `len`,
  `is_empty`, `clear`, `copy`, `to_vector`, `union`, `intersection`,
  `difference`, `is_subset`, and `equals` (same values in any order,
  since `==` on structs compares identity).
- **`Deque<T>`** keeps items at consecutive Number keys of a Map between
  a head and a tail index, so both ends are O(1): `push_front`,
  `push_back`, `pop_front`, `pop_back`, `front`, `back`, `get(i)`
  (negative from the back), `len`, `is_empty`, `clear`, `to_vector`. Like
  `Vector.pop`, popping or peeking an empty one gives `none` rather than
  throwing.
- **`PriorityQueue<T>`** is a binary min-heap. `PriorityQueue.new(key =
  none)` orders by `key(item)`, or the items themselves, with `<`; equal
  priorities come out in push order. For largest first, pass a negating
  key. `push`, `pop`, `peek` (`none` when empty), `len`, `is_empty`,
  `clear`, `to_vector` (in order, leaving the queue alone), and `of(values,
  key = none)`. Iterating goes in priority order without consuming.
- Each prints as its name then its items (`Set[1, 2]`). The types are
  exported by the module (a flat import writes them bare); helper types
  start with `__` and stay private.

### `std:regex`

`compile(pattern)` returns a `Regex` (throws `RegexError` on a bad
pattern) with `is_match(s)`, `find(s)`, `find_all(s)`, `captures(s)`,
`replace(s, with)`, `replace_all(s, with)`, `split(s)`. Rust uses the
`regex` crate; Python uses `re`, restricted to the syntax both engines
share, and parity tests cover that subset.

✅ **Landed (M32)**, in `mah/std/regex.mh`, with these decisions:

- **The subset is enforced, not just documented.** `std:regex` parses
  every pattern in Mah, so a pattern outside the subset is the same
  `RegexError` (message and code-point position) on every VM, and writes
  it back out in a canonical form that means the same thing to both
  engines (docs/MAHC_FORMAT.md §4.4). Two 1.9 natives, `regex.find` and
  `regex.find_all`, match canonical patterns; compiled patterns are
  cached per VM, as there are no handle values yet.
- **ASCII classes.** `\d`, `\w`, `\s` and `\b` are ASCII (`[0-9]`,
  `[0-9A-Za-z_]`, ...), and the `i` flag folds ASCII letters only: the
  engines' Unicode tables and case folding differ in places. Literal
  characters and `.` work on any code point. Flags are `i`, `m`, `s`,
  passed to `compile`; `$` is the very end of the text (not before a final
  newline, as in Python).
- **Left out**: lookaround, backreferences, inline flags, possessive
  repeats, and repeating (`*`, `+`, `{2}`, ...) a capture group that can
  match nothing, like `(a|)*`, where the engines capture different things.
  Each is a compile error naming the construct. Repeat counts are at most
  1000.
- **API**: `compile(pattern, flags = "")` throws `RegexError { message,
  pattern, position }`; `must_compile` is for patterns written into the
  program and throws `RuntimeError.ArgumentError` instead, which the
  checker doesn't track (like Go's `MustCompile`). A `Regex` has
  `is_match`, `find` (`Option<Match>`), `find_all`, `replace`,
  `replace_all` and `split(s, limit = none)`, and `escape(s)` quotes a
  literal. There is no separate `captures`: a `Match { text, start, end,
  groups }` carries its groups, read with `group(n)` or `group(name)`.
- **Replacements** are expanded in Mah: `$n`, `$name`, `${...}` and `$$`,
  or a function of the Match. `find_all` steps past an empty match by one
  character, so `a*` on `"baa"` finds `""`, `"aa"`, `""` on both VMs.
  Positions count code points.
- The Rust runtime's first dependency is the `regex` crate, with no
  default features (Unicode tables only).

## Phase 2: time and async

### `std:time`

`now()` (wall-clock time), `monotonic()`, a `Duration` type with
arithmetic, `format(time, pattern)` and `parse(text, pattern)`.

✅ **Landed (M34)**, in `mah/std/time.mh`, with these decisions:

- **Times and durations are Numbers of seconds** (to the millisecond):
  `now()` counts from 1970-01-01 UTC, `monotonic()` from the program's
  start. Mah has no operator overloading, so a `Duration` type couldn't
  have arithmetic; plain Numbers get `t + 90` and `b - a` for free.
  `duration_text(seconds)` writes one as "250ms", "1.5s", "2m 5s", ...
- **`DateTime { year, month, day, hour, minute, second, millisecond }`**
  is a UTC calendar reading (`utc(t)`, `date(y, m, d, h = 0, ...)`, and
  `.timestamp()`, `.weekday()` (ISO, Monday = 1), `.day_of_year()`), using
  H. Hinnant's days-from-civil algorithms in plain Mah, proleptic
  Gregorian from year 1 to 9999. It prints as ISO 8601 (`iso` /
  `parse_iso`). **No time zones yet**: the Rust runtime would need the
  OS's time zone database, which is left for later.
- `format(dt, pattern)` and `parse(text, pattern)` use strftime-style
  codes: `%Y %m %d %H %M %S %f %j %B %b %A %a %%`. `parse` throws
  `TimeError` for text that doesn't match or names an impossible date (as
  does `date`); a bad code in `format`'s pattern is a
  `RuntimeError.ArgumentError`, since it's a mistake in the program.
- Two 1.11 natives: `time.now_ms` and `time.monotonic_ms`.

### `std:async`

- `all(promises)`, `race(promises)`.
- `timeout(promise, ms)`: throws `TimeoutError` if `promise` isn't
  settled in time.
- `set_timeout(f, ms)` and `set_interval(f, ms)` return a `TimerId`;
  `clear_timeout(id)` and `clear_interval(id)` cancel it. Callbacks must
  throw nothing (`ERRORS.md`, "Interactions"). An active interval keeps
  the program running.

✅ **Landed (M34)**, in `mah/std/async.mh`, with these decisions:

- **Written in Mah** over four 1.11 natives: `promise.new`,
  `promise.resolve` and `promise.fail` (a Promise settled by hand), and
  `time.cancel` (drop the timer behind a `detach sleep_async(ms)`).
- **`all`, `race` and `timeout` wait like any call** and return the value
  (detach them to keep going). Watcher tasks await the inputs and report
  which one decided the outcome; the function then awaits that Promise
  itself, already settled. So `all` fails as soon as any input fails,
  `race` settles with the first to settle (value or error), and the
  checker sees each return exactly the inputs' type and throw exactly
  their errors (plus `TimeoutError { ms }` for `timeout`). They're generic
  over `Vector<Promise<T>>`.
- **Timers**: `set_timeout`/`set_interval` return a `TimerId`; an
  interval's next wait starts when the callback returns. Clearing cancels
  the pending timer, so a cleared timer never keeps the program running.
  Callbacks are typed `fn() throws never`, so the checker enforces
  "callbacks must throw nothing".

## Phase 3: OS access

### Bytes and `std:bytes`

✅ **Landed (M37)**: the built-in `Bytes` type (docs/MAHC_FORMAT.md §5, §6.7,
§6.9), the module `mah/std/bytes.mh`, and `std:fs`'s binary functions below.
Decisions:

- **A growable sequence of bytes**, each a whole Number 0 to 255, **mutable
  and by reference** like a Vector (`push`, `pop`, `extend`, `b[i] = n`,
  and a copy is explicit: `copy()`).
- **No literal syntax yet.** Bytes come from `"text".to_bytes()` (UTF-8),
  `bytes.new`, `bytes.from_vector`, `bytes.from_hex`, `bytes.from_base64`,
  `fs.read_bytes` and slicing. `Bytes` is also a type name and a Type value
  (`reflect.type_of(b)` is `Bytes`).
- **Equality is by content**: two Bytes with the same bytes are `==`, and a
  Bytes never equals a Vector. `a + b` (both Bytes) is a new Bytes, the
  concatenation; `"x" + b` stays the String rule. A Bytes is always truthy
  and isn't a Map key. `to_string` is `Bytes[68 69]` (lowercase hex, one
  space between bytes), `Bytes[]` when empty.
- **Indexing follows Vector's rules**: `b[i]` is a Number or `none` (negative
  counts from the end), `b[a..b]` a new Bytes, `for let x in b` gives
  Numbers (live, like a Vector's). A byte that isn't a whole 0..255 Number
  is an error (`TypeMismatch` or `ArgumentError`), never wrapped or
  clamped. `Vector.copy(deep: true)` copies the Bytes it reaches.
- **Methods**: `len`, `push`, `pop`, `extend`, `copy`, `to_vector`,
  `to_text`, `to_text_lossy`, `to_hex`, `to_base64`, `index_of(needle)`.
  `to_text()` returns an `Option<String>` (`none` for invalid UTF-8, no
  error), `to_text_lossy()` always a String with U+FFFD for each invalid
  sequence. Hex is lowercase when written and accepts either case; base64 is
  the standard alphabet with `=` padding, required when decoding.
- **`std:bytes`**: `new(size = 0, fill = 0)`, `from_vector(items)`,
  `from_hex(text)`, `from_base64(text)` (both throw `BytesError` for text
  that isn't valid; whitespace isn't ignored) and `concat(parts)`. `BytesError
  { kind, description }` has kind `invalid_hex` or `invalid_base64`; the
  description quotes the text, cut to 40 characters plus `...`.
- **Binary file I/O lives in `std:fs`**: `read_bytes(path)`,
  `write_bytes(path, data)`, `append_bytes(path, data)`, and on `File`
  `read_bytes(max = none)` and `write_bytes(data)`; all throw `FsError`
  (ops `read_bytes`, `write_bytes`, `append_bytes`). Text and binary reads
  can be mixed on one open file.
- **Bytecode 1.17**: four `bytes.*` and five `fs.*` natives, the primitive
  type code 8 and the method `to_bytes`. Since `std:fs` declares the binary
  natives, every program that imports it is 1.17.
- **Later**: a literal syntax. (Sockets landed in M38 and read and write Bytes.)

### `std:fs`

- Whole files: `read_text`, `write_text`, `append_text`.
- Paths: `exists`, `is_file`, `is_dir`, `remove`, `rename`, `copy`,
  `mkdir(path, parents = false)`, `list_dir`, `glob(pattern)`.
- Handles: `open(path, mode)` returns a `File` with `read_line()`,
  `lines()` (Iterable), `write(s)` and `close()`.
- Throws `FsError`. *(M37)* Binary I/O is `read_bytes`, `write_bytes`,
  `append_bytes` and `File.read_bytes(max = none)` / `File.write_bytes(data)`,
  on `Bytes` (see "Bytes and `std:bytes`" above).

✅ **Landed (M35)**, in `mah/std/fs.mh`, with these decisions:

- **Async underneath, waiting like a call.** Each of the fifteen 1.12
  `fs.*` natives returns a Promise and does the work on a worker thread;
  the std:fs functions await it. So `fs.read_text(p)` waits, while
  `detach fs.read_text(p)` gives a Promise and lets other tasks and
  timers run. Both VMs' I/O hub (M33) runs the jobs and settles their
  Promises from the scheduler.
- **`FsError { kind, op, path, description }`**, a struct rather than an
  enum so a `catch` can test `e.kind` without listing every variant's
  fields. The kinds are `not_found`, `permission_denied`,
  `already_exists`, `is_a_directory`, `not_a_directory`,
  `directory_not_empty`, `invalid_utf8`, `closed` and `other` (with the
  OS's own text), mapped identically from Python's exceptions and Rust's
  `io::ErrorKind`. Natives never build it: they settle `[false, kind,
  description]` and std:fs throws.
- **Text is exact**: strict UTF-8, no newline translation, lines split
  at `\n` only (dropping a `\r` before it, like `input`); writes to an
  open file are unbuffered. `list_dir` is sorted.
- `remove(path, recursive = false)` takes files and empty directories,
  or a whole tree with `recursive`. `copy` copies files only. `info(path)`
  gives `FileInfo { kind, size, modified }` (modified in seconds, like
  std:time). `temp_dir()` makes a fresh temporary directory, which the
  tests and examples use.
- **`glob`** is written in Mah over `list_dir`: `*`, `?`, `[abc]`,
  `[!abc]`, `[a-z]` within a name, `**` across directories; hidden names
  match only a pattern part that starts with "."; results sorted.

### `std:process`

✅ **Landed (M36)**, in `mah/std/process.mh`, with these decisions:

- **Surface**: `args()`, `exit(code = 0)`, `env_get(name)` (an
  `Option<String>`), `env_set`, `env_remove`, `env()` (a Map, sorted by
  name), `cwd()`, `pid()`, `platform()`, `run(program, args = [], cwd =
  none, env = none, stdin = "")` and `shell(command, cwd =, env =, stdin
  =)`, both returning `Output { code, stdout, stderr }` with `ok()`.
  Ten 1.13 `process.*` natives; `run` is async like std:fs's (a worker
  thread, awaited by the std module), the rest are synchronous.
- **The environment is a table per VM run**: a snapshot of the process
  environment taken at start (entries that aren't valid Unicode are
  skipped). `env_get`/`env_set`/`env_remove`/`env` touch only that table,
  never the real environment, and the programs `run` starts get exactly
  the table plus the call's `env` overrides. Names must be non-empty
  Strings without `=` or NUL, values Strings without NUL (else an
  ArgumentError, with the same text on both VMs).
- **`run` starts a program directly** (no shell, found through `PATH`)
  with its input piped in (always, even for `""`) and its output
  captured. Failing to start throws `ProcessError { kind, command,
  description }` (`not_found`, `permission_denied` or `other`); a non-zero
  exit is just a `code`. A child killed by signal N has code `128 + N`.
  stdout and stderr are decoded as UTF-8 leniently (invalid bytes become
  U+FFFD).
- **`shell(command)`** is `run("/bin/sh", ["-c", command])`, or `cmd /C`
  on Windows, and a failure to start names the command line. It is a
  separate function so shell use is explicit; a command the shell can't
  find is just exit code 127.
- **`exit(code)`** takes a whole number from 0 to 255. It ends the
  program at once: stdout is flushed, but pending `defer`s don't run,
  detached tasks, timers and pending I/O are abandoned, and no Mah
  `try`/`catch` can intercept it. In a `mah test` run it fails the test
  ("the test called exit(N)") instead of ending the run.
- **Program arguments** are what follows the first `--` of `mah run [FILE]
  -- ARGS...` or `mah runc FILE -- ARGS...` (and `mah-vm run PATH
  [ARGS...]`, and a self-contained executable's own command line); `mah
  test` passes none.
- **Later**: `spawn(...)` (a `Process` handle for streaming), signals and
  a way to change the working directory wait for the network work (`Bytes`
  landed in M37, so a spawned process's streams can use it).

### `std:reflect`

✅ **Landed (M41a)**, in `mah/std/reflect.mh`. The design, the API and every
decision are in [`REFLECTION.md`](REFLECTION.md) (its "M41a: what landed"
section lists where the implementation adds to it); the byte-level side is
docs/MAHC_FORMAT.md §4.4 (seven 1.14 `reflect.*` natives), §4.10 (the META
section) and §5 (the `Type` value). In short:

- `type_of(value)`, `signature(f)` (name, `##` doc, type parameters,
  parameters with their written types, docs and constant defaults, return
  type, `throws`), `schema(t)` (a struct's fields or an enum's variants,
  with types and docs; `none` for a primitive), `methods(t)`, `implements(t,
  trait_name)`, `call(f, args = [], kwargs = [:])`, `construct(t, fields)`
  and `construct_variant(t, variant, fields)` (throwing `ReflectError`).
  M41b: every descriptor has a `decorators` Vector, and `find(decorators,
  target)` picks one by type or `==` (docs/REFLECTION.md). M41c: a `Signature`
  also has `rest` and `kwrest`; `find` matches a function with the same
  identity even after it was wrapped; and the module exports the hook traits
  `WrapFn`, `WrapParam`, `WrapField`, `WrapStruct` with their info structs
  `FnInfo`, `ParamInfo`, `FieldInfo`, `TypeInfo`. It also holds the private
  helpers (`__wrap_fn`, `__setup_*`, `__run_*`, not exported) that the code
  generated for a decorated program calls, which is why a program with a
  decorator imports it implicitly; its `hooks.*` natives make every program
  that imports it (or `std:json`) bytecode 1.16.
- It reports what the source *wrote*, never what the checker inferred; an
  unannotated parameter's type is `TypeRef.Unknown`.
- Bare type names are values (`User`, `Number`), spread calls
  (`f(...xs, **m)`) exist so `call` can be written in Mah, and `##` comments
  above a declaration are its documentation. The standard library's own
  exported functions carry `##` docs, which the editor's hover shows too.

## Phase 4: network

### `std:socket`

✅ **Landed (M38)**, in `mah/std/socket.mh`, with these decisions:

- **Surface**: `connect(host, port, timeout = none)` returns a `Socket`;
  `listen(port, host = "127.0.0.1", backlog = 128)` returns a `Listener`
  (port 0 picks a free one; `Listener.port` is the real one) whose
  `accept(timeout = none)` gives Sockets and whose `close()` stops it. A
  `Socket` has `send(data: Bytes)` (sends all of it), `send_text(text)`,
  `recv(max = 65536, timeout = none)` (up to `max` bytes, **empty Bytes once
  the peer has closed its side**), `recv_exactly(n, timeout = none)`,
  `read_line(timeout = none)`, `shutdown()` (stops sending, so the peer's
  `recv` sees the end) and `close()` (closing twice does nothing). Both
  print as `Socket(127.0.0.1:5000)` / `Listener(127.0.0.1:5000)` (the peer's
  address, the listen address).
- **TCP only**, plus TLS clients since M39 (`start_tls`, `connect_tls`)
  and TLS servers since M42 (`tls_server_config`, `start_tls_server`; both
  below). UDP comes later.
- **Timeouts are in milliseconds**, like `std:async`'s; `none` waits
  forever, 0 means "only what is already there", and running out throws kind
  `timed_out`.
- **Ids in a socket table, wrapped by structs**: the seven 1.18 `socket.*`
  natives take positive whole-Number ids from a per-VM table (separate from
  the file table); `Socket { id, peer_host, peer_port, local_port, buffer }`
  and `Listener { id, host, port }` wrap them, like `File`.
- **Buffered `read_line`**: it reads from the socket into the Socket's
  `buffer` (Bytes) until a `\n`, and returns the line as a String without
  `\n` or `\r\n`; bytes after the line stay in `buffer`. A final line with no
  `\n` is still returned, then `none`. Text that isn't valid UTF-8 throws
  `invalid_utf8`. `recv` returns buffered bytes first, without waiting;
  `recv_exactly` throws `closed_early` ("the connection closed before N bytes
  arrived") if the peer closes first.
- **Errors**: `SocketError { kind, op, address, description }` with
  `message()` = `op + ": " + description + ": " + address`. Kinds:
  `connection_refused`, `connection_reset`, `timed_out`, `address_in_use`,
  `address_not_available`, `host_not_found`, `permission_denied`, `closed`,
  `closed_early`, `invalid_utf8` and `other` (the OS's text). `op` is the
  function's name; `address` is `host:port` (the peer's for Socket methods,
  the listen address for Listener methods and `listen`, the target for
  `connect`).
- **Async like std:fs**: every function waits like a call and the work runs
  on a worker thread, so `detach server.accept()` runs in the background. A
  pending `accept` or `recv` keeps the program running (the scheduler's
  pending-I/O rule). **Closing the socket or listener ends the wait**: the
  waiting call fails with kind `closed` (within about 50 ms: waiting workers
  poll in slices of at most 50 ms, checking the deadline and the table).
- `listen` sets `SO_REUSEADDR` on Unix so a restarted server can rebind at
  once; `connect` tries every address the name resolves to (IPv4 or IPv6).
  When the VM finishes, every open socket and listener is closed.
- Bytecode 1.18 (docs/MAHC_FORMAT.md §4.4): only programs importing
  `std:socket` are 1.18 (1.19 since M39, which added `socket.start_tls`;
  1.20 since M42, which added the two TLS server natives).
- **TLS (M39)**: `sock.start_tls(server_name, timeout = none)` turns a
  connected Socket into a TLS client (its `buffer` must be empty, else
  `RuntimeError.ArgumentError`), and `connect_tls(host, port, timeout =
  none)` is `connect` plus `start_tls(host)`, closing the socket if the
  handshake fails. The certificate must chain to a trusted root: the PEM
  file in `SSL_CERT_FILE` when set, else the platform's (Python) or
  Mozilla's (Rust) set. Two more kinds: `tls_certificate` (verification
  failed, including the wrong name) and `tls` (any other TLS failure). A
  peer closing without TLS's close notice is a normal end; `shutdown` on a
  TLS socket sends no close notice. Python uses `ssl`, Rust `rustls`.
- **TLS servers (M42)**: `tls_server_config(cert_path, key_path)` loads a
  PEM certificate chain (the server's certificate first) and an unencrypted
  PEM private key (RSA 2048+ or ECDSA P-256 tested), checks that they match,
  and returns a `TlsServerConfig { id, cert_path, key_path }` (printed as
  `TlsServerConfig(cert_path)`; `close()` frees it, twice is fine). It is
  loaded **once**, so a wrong path fails at startup, not at the first
  connection. `sock.start_tls_server(config, timeout = none)` then runs a
  server handshake on an accepted Socket (its `buffer` must be empty, else
  `RuntimeError.ArgumentError`); afterwards the Socket works as after
  `start_tls`. One config serves any number of connections (and servers):
  `std:http`'s `serve(tls: config)` uses it per connection. Loading failures
  are kind `tls_config`, with `address` the certificate path and a
  description from a fixed list, checked in this order: `can't read the
  certificate file`, `can't read the private key file`, `no certificate in
  the certificate file`, `the private key is encrypted`, `no private key in
  the key file` (those three are a plain search for the PEM markers, the
  same on both VMs), `the private key doesn't match the certificate`, `the
  certificate or private key isn't usable`. Handshake failures are
  `start_tls`'s kinds: `tls` (a client that doesn't speak TLS or rejects
  the certificate, or a socket already using TLS), `connection_reset`,
  `timed_out`, `closed`. TLS 1.2 and 1.3; no client certificates, SNI
  (one certificate per config) or ALPN. A config id is "not open" for every
  other socket function. Bytecode 1.20.

### `std:url`

✅ **Landed (M39)**, in `mah/std/url.mh`, pure Mah (no natives):

- `parse(text)` gives `Url { scheme, username, password, host, port, path,
  query, fragment }`. The scheme and host are lowercased; `path`, `query`
  and `fragment` stay as written (percent-encoded); `port`, `query` and
  `fragment` are `none` when absent (`?` with nothing after it is `""`).
  Throws `UrlError { kind: "invalid_url", text, description }` for no
  scheme, spaces or control characters, a port that isn't a number up to
  65535, an unclosed `[` IPv6 host, or an http/https URL without a host.
  `is_valid(text)` is the Bool form.
- Methods: `effective_port()` (the port or `default_port(scheme)`: 80
  http/ws, 443 https/wss, 21 ftp), `authority()`, `origin()`,
  `request_target()` (path, "/" when empty, plus the query), `query_pairs()`,
  `resolve(reference)` (RFC 3986 §5.2, every §5.4 example tested) and
  `with_query(params)`. It prints as the URL.
- `encode(text, safe = "")` escapes everything but letters, digits, `-._~`
  and `safe` as `%XX` of the UTF-8 bytes (uppercase hex); `decode(text)`
  reverses it, throwing kind `invalid_encoding` for a bad escape or
  non-UTF-8 bytes (`+` stays `+`).
- `encode_query(params)` takes a Map (a Vector value repeats the key) or a
  Vector of `[key, value]` pairs, form style (space as `+`);
  `parse_query(q)` gives decoded `[key, value]` pairs in order.

### `std:http`

✅ **Landed (M39, client; M42, server)**, in `mah/std/http.mh`: an HTTP/1.1
client and server written in Mah over `std:socket` (so both VMs share them),
TLS through `socket.start_tls` (client) and `socket.start_tls_server`
(server). The client and the server share the wire code (header-line reader,
header helpers, chunked decoder). Client decisions:

- **Surface**: `request(method, url, body = none, json = none, form = none,
  headers = [:], timeout = 30000, max_redirects = 10)`, and `get`, `post`,
  `put`, `patch`, `delete`, `head` wrappers. `body` is a String or Bytes;
  `json` sends `json.stringify(value)` as `application/json`; `form` sends
  `url.encode_query(form)` as `application/x-www-form-urlencoded`.
  `headers` is a Map or `[name, value]` pairs and replaces any default of
  the same name (Host, User-Agent `mah`, Accept `*/*`, Connection `close`,
  Content-Length, Content-Type). A header with CR/LF or `:` in its name,
  a bad `body`/`headers` type, or an unwritable `json` value is a
  `RuntimeError.ArgumentError`.
- **Response** `{ status, reason, headers, body, url, method }`: `headers`
  are the `[name, value]` pairs as received, `body` Bytes, `url` the final
  URL. `header(name)` (first value, any case, or `none`),
  `header_all(name)`, `text()` (lossy UTF-8), `json()`, `is_success()`
  (2xx) and `check_status()` (throws kind `status` for 4xx/5xx). **A 4xx or
  5xx is a normal Response.**
- **HttpError { kind, method, url, description }** with `message()` =
  `METHOD URL: description`: a `SocketError` kind for network failures
  (`connection_refused`, `host_not_found`, `timed_out`, `tls_certificate`,
  `tls`, `closed_early`, ...), or `invalid_url`, `unsupported_scheme`,
  `invalid_response`, `too_many_redirects`, `proxy`, `status`.
- **The client uses one connection per request** (`Connection: close`);
  no client keep-alive, no compression (no `Accept-Encoding` is sent), no HTTP/2. Bodies framed
  by chunked encoding (extensions and trailers ignored), Content-Length,
  or the connection closing; HEAD, 204 and 304 have none; 1xx responses
  are skipped.
- **Timeouts**: `timeout` (ms, or `none`) applies to each wait: connecting,
  the TLS handshake, and each read, not the request as a whole.
- **Redirects** 301/302/303/307/308 with a Location are followed up to
  `max_redirects` (0 returns the redirect): 303, and 301/302 after a POST,
  continue as a bodiless GET (HEAD stays HEAD); 307/308 keep the method
  and body. Authorization and Cookie are dropped when the origin changes.
- **Proxies** from the environment: `HTTP_PROXY`/`HTTPS_PROXY` (lowercase
  first, empty means unset; an `http://` proxy, with optional
  `user:password@` sent as Basic `Proxy-Authorization`), `NO_PROXY`
  (comma-separated; `*`, exact hosts and domain suffixes, a leading `.` or
  `*.` ignored, ports dropped; no CIDR ranges). Plain http goes to the
  proxy with the absolute URL; https uses `CONNECT host:port`, then TLS to
  the real host through the tunnel.

**Server** (M42, `docs/contracts/M42_http_server.md` part A):

- **Surface**: `serve(port, handler, host = "127.0.0.1", tls = none,
  max_head = 16384, max_body = 10485760, read_timeout = 30000,
  idle_timeout = 5000, max_connections = 256, backlog = 128,
  on_error = none)` listens (port 0 picks a free one), returns a
  **Server** `{ host, port, tls, state }` at once and serves in the
  background. Bad options are `RuntimeError.ArgumentError`s; bind failures
  throw `socket.SocketError` (`address_in_use`, ...) from `serve` itself.
- **Handler shape `fn(Request) -> Reply`** (Fetch/Hono style, not Node's
  `(req, res)`): routing and middleware are function composition, and a
  handler is testable without a socket by building an `http.Request`
  literal. **Request** `{ method, target, path, query, version, headers,
  body, peer_host, peer_port, tls }` (`path`/`query` split at the first `?`,
  still percent-encoded; `body` Bytes, read whole before the handler runs)
  with `header`, `header_all`, `content_type()` (media type, lowercased),
  `text()`, `json()`, `query_pairs()`, `query_param(name)`, `form()` and
  `multipart()` (a Vector of **Part** `{ name, filename, content_type,
  headers, body }`). **Reply** `{ status, headers, body }` with
  constructors `Reply.new`, `text`, `html`, `json`, `bytes`, `redirect`,
  `empty`, `stream` and `set_header`/`add_header`; `body` is `none`, a
  String, Bytes or a producer `fn(BodyWriter)` whose `write(data)` streams
  the body (chunked on HTTP/1.1, exactly the bytes of a given
  Content-Length, or close-delimited on HTTP/1.0). Also `reason_phrase`
  and `http_date`.
- **Limits and statuses**: a request line over `max_head` is 414, a head
  over it or more than 100 header fields 431, a body over `max_body` 413
  (checked before reading it), a request not fully received within
  `read_timeout` ms of its first byte 408, more than `max_connections`
  open connections 503; a connection waiting longer than `idle_timeout`
  for its next request is closed silently. Malformed requests (bad request
  line, missing or repeated Host, bad Content-Length, Transfer-Encoding
  with Content-Length, a coding other than a final `chunked`, obsolete
  line folding, bad header names) are 400, 501 or 505, always closing the
  connection after the reply (the server then reads what the client still
  sends for a moment, so the client sees the reply, not a reset).
- **Keep-alive** (HTTP/1.1 unless `Connection: close`; HTTP/1.0 with
  `Connection: keep-alive`), pipelined requests answered in order;
  **`Expect: 100-continue`** answered with `100 Continue` once the head has
  passed the checks (any other expectation is 417); chunked request bodies
  (extensions and trailers ignored). Every reply carries `Date`; HEAD, 204
  and 304 replies have no body.
- **Errors**: `Request.json`/`query_pairs`/`form`/`multipart` throw
  `HttpError` kind `bad_request`, which the server answers with 400 and its
  description; anything else a handler throws (or a non-Reply result) is a
  bare 500. `on_error(error, request)` sees every handler or body-producer
  error and may return the Reply to send instead. No default logging.
- **Shutdown**: `server.shutdown(grace = 10000)` returns at once, stops
  accepting, closes idle connections, lets requests in flight finish (with
  `Connection: close`) and force-closes what is left after `grace` ms;
  `server.wait()` returns once everything has stopped, `server.close(grace)`
  is both, `server.connections()` counts open connections. A server that is
  never shut down keeps the program running, so a script can end with
  `http.serve(...).wait()`.
- **Concurrency**: the accept loop and each connection are detached tasks,
  so connections are served concurrently whenever a handler waits (I/O,
  `sleep_async`, `.await`); the scheduler stays single-threaded (M44).
- **TLS**: `tls: socket.tls_server_config(cert_path, key_path)` (a PEM
  chain and key, loaded and checked once) serves HTTPS; each connection
  runs `start_tls_server` first, with `read_timeout` as its handshake
  timeout, and a failed handshake closes it silently.
- Not yet: HTTP/2, WebSockets/`Upgrade`/1xx replies, streaming request
  bodies, compression, Range, static files, cookie helpers, routing and
  middleware (the web framework, a separate repository, builds them on
  this API).

## `std:test` and `mah test`

Designed in [`MAH_TEST.md`](MAH_TEST.md). ✅ **Landed (M28)**, right after
the errors milestone and Phase 0's `std:` resolution, before the rest of
the standard library, so every std module can be tested in Mah.

## For every module

- Tests per `docs/TESTING.md`, Mah-level tests in `.test.mh` files
  once `mah test` exists, and parity cases in
  `runtime/tests/vm_diff.py` for anything implemented natively.
- Types in `mah/std/builtins.d.mh` or the module's own annotations.
- `mah/project/templates/` (language reference, AGENTS.md) and website
  docs updated in the same change.
- An example in `examples/`.
