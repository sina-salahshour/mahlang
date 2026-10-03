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
handles); **M36 landed `std:process`**; **M41a landed `std:reflect`** and `json.decode`
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
  `ureq` with `rustls` for HTTP/HTTPS, and `regex`. Anything small (glob
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
  written at bytecode 1.14 (it was 1.7). Since M41s `std:reflect`'s types
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

### `std:fs`

- Whole files: `read_text`, `write_text`, `append_text`.
- Paths: `exists`, `is_file`, `is_dir`, `remove`, `rename`, `copy`,
  `mkdir(path, parents = false)`, `list_dir`, `glob(pattern)`.
- Handles: `open(path, mode)` returns a `File` with `read_line()`,
  `lines()` (Iterable), `write(s)` and `close()`.
- Throws `FsError`. Binary reads/writes wait for a `Bytes` type.

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
  a way to change the working directory wait for a `Bytes` type and the
  network work.

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
  target)` picks one by type or `==` (docs/REFLECTION.md).
- It reports what the source *wrote*, never what the checker inferred; an
  unannotated parameter's type is `TypeRef.Unknown`.
- Bare type names are values (`User`, `Number`), spread calls
  (`f(...xs, **m)`) exist so `call` can be written in Mah, and `##` comments
  above a declaration are its documentation. The standard library's own
  exported functions carry `##` docs, which the editor's hover shows too.

## Phase 4: network

### `std:socket`

TCP: `connect(host, port)` returns a `Socket`; `listen(port)` returns a
`Listener` whose `accept()` gives Sockets. `send`, `recv`, `close`.
Throws `SocketError`. UDP later.

### `std:http`

`get(url, headers =)`, `post(url, body =, json =, headers =)` and
`request(method, url, ...)`. They return a
`Response { status, headers, text(), json() }`. Throws `HttpError` for
transport failures; a 4xx/5xx status is a normal Response. Rust uses
`ureq` + `rustls`; Python uses `urllib`.

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
