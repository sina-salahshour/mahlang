# Mah standard library

Status: **M27 landed 2026-09-28: Phase 0 steps 1, 2 and 5 (`std:`
resolution, `extern fn`, native table versioning) and `std:math`**, the
first module; **M29 landed the String methods** (Phase 1's first item);
**M30 landed `std:path`, `std:json` and `std:csv`**. The rest is design. Agreed 2026-09-28. Depends on
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
  has its own error enum (`FsError`, `JsonError`, ...).
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
   enum (`runtime/src/vm/value.rs`).
4. **Async scheduler.** Today the scheduler only handles `sleep_async`
   timers. It gains:
   - worker threads that run blocking I/O and settle a Promise (with a
     value, or an error per `ERRORS.md`);
   - callback timers with cancellation, for `std:async`;
   - "the program is done when the main task has finished and no timer
     or I/O is pending", as in Node.
5. **Native table versioning. ✅ Landed (M27).** New natives bump the
   minor version (`docs/MAHC_FORMAT.md` §4.4/§7: the `std:math` ones are
   1.5). The encoder writes the lowest minor a file needs (1.4 unless it
   lists a 1.5 native), so old VMs keep running programs that don't need
   the new natives. A VM refusing a newer file reads its NATIVES section
   and names the natives it's missing: `unsupported minor version 6 (...):
   it uses natives this VM doesn't have ('fs.read_text'); upgrade mah to
   run it`.
6. **A shared PRNG.** The same algorithm (PCG or xoshiro256**) in both
   runtimes, not Python's `random`, so seeded output is identical and
   `runtime/tests/vm_diff.py` keeps working.
7. **`input` becomes an ordinary async function.** See below.

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

### `std:json`

- `parse(text)` returns Map, Vector, Number, String, Bool or `none`.
  Throws `JsonError`.
- `stringify(value, indent = 0)` serializes Map, Vector, Number, String,
  Bool, `none`, and structs/enums by field name.
- `trait FromJson { fn from_json(value) -> Self }`, implemented per
  struct and called as `Point.from_json(json.parse(text))`. It throws
  `JsonError` on a shape mismatch. (A `decode(text, Point)` form waits
  for types as runtime values.)

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

### `std:regex`

`compile(pattern)` returns a `Regex` (throws `RegexError` on a bad
pattern) with `is_match(s)`, `find(s)`, `find_all(s)`, `captures(s)`,
`replace(s, with)`, `replace_all(s, with)`, `split(s)`. Rust uses the
`regex` crate; Python uses `re`, restricted to the syntax both engines
share, and parity tests cover that subset.

## Phase 2: time and async

### `std:time`

`now()` (wall-clock time), `monotonic()`, a `Duration` type with
arithmetic, `format(time, pattern)` and `parse(text, pattern)`.

### `std:async`

- `all(promises)`, `race(promises)`.
- `timeout(promise, ms)`: throws `TimeoutError` if `promise` isn't
  settled in time.
- `set_timeout(f, ms)` and `set_interval(f, ms)` return a `TimerId`;
  `clear_timeout(id)` and `clear_interval(id)` cancel it. Callbacks must
  throw nothing (`ERRORS.md`, "Interactions"). An active interval keeps
  the program running.

## Phase 3: OS access

### `std:fs`

- Whole files: `read_text`, `write_text`, `append_text`.
- Paths: `exists`, `is_file`, `is_dir`, `remove`, `rename`, `copy`,
  `mkdir(path, parents = false)`, `list_dir`, `glob(pattern)`.
- Handles: `open(path, mode)` returns a `File` with `read_line()`,
  `lines()` (Iterable), `write(s)` and `close()`.
- Throws `FsError`. Binary reads/writes wait for a `Bytes` type.

### `std:process`

- `run(cmd, args = [], cwd =, env =, stdin =)` takes an argv list (no
  shell) and returns `{ code, stdout, stderr }`. Throws `ProcessError`
  (for failing to start; a non-zero exit is just `code`).
- `shell(cmdline)`: runs through the system shell. Named separately so
  shell use is explicit.
- `spawn(...)` returns a `Process` handle for streaming.
- `args()`, `exit(code)`, `env_get(name)`, `env_set(name, value)`,
  `cwd()`.

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
