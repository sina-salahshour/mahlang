# Mah standard library

Status: **design, not implemented.** Agreed 2026-09-28. Depends on
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

1. **`std:` resolution.** `_resolve_import` in `mah/preprocessor.py`
   maps `std:<name>` to `mah/std/<name>.mh`. An unknown std module is a
   compile error. The LSP resolves go-to-definition and hover into std
   files. Bundles (`.mahc`) compile std code in like any other import.
2. **`extern fn`.** Std modules reach natives through declarations that
   only `std:` files may use:
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
5. **Native table versioning.** New natives bump the table version in
   `docs/MAHC_FORMAT.md` §4.4, so an old VM running a newer bundle fails
   with a clear link error naming the missing native.
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
| `to_number()` | throws `ParseError`; allows surrounding whitespace |

Plus `Vector.join(sep)`, and on `Option`: `unwrap()` (throws
`RuntimeError.UnwrapNone`), `unwrap_or(default)`, `is_some()`,
`is_none()`.

### `std:math`

`sqrt`, `pow`, `abs`, `floor`, `ceil`, `round(digits = 0)`, `min`, `max`,
`clamp`, `sin`, `cos`, `tan`, `asin`, `acos`, `atan`, `atan2`, `log`,
`log10`, `exp`, and the constants `pi`, `e`. This replaces today's
standalone `sin`/`cos` keywords and natives.

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

### `std:csv`

- `parse(text, delimiter = ",", header = false)` gives
  `Vector<Vector<String>>`, or `Vector<Map<String, String>>` with a header
  row. RFC 4180 quoting. Throws `CsvError`.
- `stringify(rows, delimiter = ",", header = none)`.
- `trait FromCsvRow { fn from_csv_row(row) -> Self }`, used the same way.

### `std:path`

`join(...)`, `dirname`, `basename`, `extension`, `stem`, `normalize`,
`is_absolute`, `relative(from, to)`. Pure string logic, POSIX and Windows
separators.

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

Designed in [`MAH_TEST.md`](MAH_TEST.md). It lands right after the
errors milestone and Phase 0's `std:` resolution, before the rest of the
standard library, so every std module can be tested in Mah.

## For every module

- Tests per `docs/TESTING.md`, Mah-level tests in `.test.mh` files
  once `mah test` exists, and parity cases in
  `runtime/tests/vm_diff.py` for anything implemented natively.
- Types in `mah/std/builtins.d.mh` or the module's own annotations.
- `mah/project/templates/` (language reference, AGENTS.md) and website
  docs updated in the same change.
- An example in `examples/`.
