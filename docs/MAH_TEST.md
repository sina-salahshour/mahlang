# `std:test` and `mah test`

Status: **design, not implemented.** Agreed 2026-09-28. Depends on
[`ERRORS.md`](ERRORS.md) (a failing assertion throws) and on `std:`
import resolution from [`STDLIB.md`](STDLIB.md) Phase 0. It lands right
after those two, before the rest of the standard library, so each std
module can ship with tests written in Mah.

(Not to be confused with [`TESTING.md`](TESTING.md), which is about
testing the Mah implementation itself.)

## Example

```mah
# src/calc.test.mh
import "std:test"
import calc from "./calc"

test "adds two numbers" {
    assert_eq(calc.add(2, 3), 5)
    assert(calc.is_even(4), "4 should be even")
}

test "rejects text that isn't a number" {
    let e = assert_throws(fn() { "abc".to_number() })   # e: ParseError
    assert_eq(e.text, "abc")
}

test "uses a private helper" {
    # calc.test.mh sits next to calc.mh, so it sees non-exported names too
    assert_eq(calc.clamp_positive(-3), 0)
}

test "big input" {
    skip("slow; run with a real dataset")
    ...
}

test "fetches after a delay" {
    sleep_async(10)                 # async tests just await
    assert_eq(load().await.len(), 3)
}
```

## Test files

- A test file is any `*.test.mh` file. `mah test` finds every one under
  the project root (the directory holding `mah-project.toml`), skipping
  `build/` and hidden directories.
- A test file holds only declarations: `import`, `fn`, `struct`, `enum`,
  `trait`, `impl`, constant `let`s, and `test` blocks. Top-level
  statements that run on their own are a compile error, as in a Rust
  test module.
- `mah run` refuses to run a `.test.mh` file, and `mah build` never
  includes one.

## Syntax: `test` blocks

```
test "name" { body }
```

- `test` is a **contextual keyword**: only at the top level of a
  `.test.mh` file, and only when followed by a string literal. Elsewhere
  it's an ordinary identifier, so existing code using `test` as a name
  keeps working.
- The name is a plain string literal (no interpolation). Two tests with
  the same name in one file are a compile error.
- The body is a block, compiled like a function body with no parameters.
  It may throw anything and may `.await`. The checker doesn't report
  "unhandled error" for a test body: the runner catches everything.
- Pipeline changes (via the `mah-add-feature` skill): lexer/parser
  (`TestDecl` AST node), resolver, codegen (each test compiles to a
  hidden function plus an entry in a test table), formatter, tree-sitter
  grammar and highlighting, and the LSP. The LSP lists tests as document
  symbols, and later as "run test" code lenses.

## Private access for sibling test files

A test file whose name is `<stem>.test.mh` sees **every** top-level name
of `<stem>.mh` in the same directory when it imports it, exported or not.
Any other importer, including other test files, still sees only exports.
This is one exception in the preprocessor's export filtering
(`mah/preprocessor.py`), keyed on the two paths.

## `std:test`

| Function | Behaviour |
|---|---|
| `assert(cond, message = "")` | fails when `cond` is falsy |
| `assert_eq(actual, expected, message = "")` | fails when `actual != expected`; the report shows both values |
| `assert_ne(a, b, message = "")` | fails when `a == b` |
| `assert_throws(f) -> E` | runs `f`; returns the error it threw, fails if it returned normally |
| `fail(message)` | fails immediately |
| `skip(reason = "")` | stops the test and reports it as skipped |

- Failing throws `AssertionError { message, actual, expected, location }`.
  `skip` throws `SkipTest { reason }`, which the runner treats specially.
  Both implement `Error`.
- `assert_throws` is typed through the error set of its argument:
  `fn assert_throws<E>(f: fn() -> Unknown throws E) -> E`. The caller
  gets back a value of the thrown type, ready to inspect or `match`.
- `assert_eq`/`assert_ne` compare with Mah's `==`. The failure report
  prints both sides with `Printable.to_string`.
- `location` is the file and line of the failing assertion call, taken
  from the caller's frame.

## The runner: `mah test`

```
mah test [FILTER] [--file PATH] [--vm python|rust] [--timeout MS]
```

- `FILTER` selects tests whose name, or `file::name`, contains it.
- `--file` runs one test file. `--vm` overrides `[run] vm` from the
  manifest. `--timeout` fails any test that runs longer (default: none).
- **Isolation**: each test runs in a fresh VM, so a module's top-level
  state (and any `let` constants it computed) starts clean. The test
  file is compiled once, and the bundle's test table says which hidden
  function to call for each test.
- **Async**: a test finishes when its body has returned (after any
  auto-awaits). Timers or detached tasks still pending then are
  cancelled, with a warning next to the test's result.
- **Outcome**: *ok* if the body returns, *skipped* on `SkipTest`,
  *FAILED* on any other escaping error (`AssertionError`, `FsError`,
  `RuntimeError`, ...). The exit code is non-zero if anything failed.
- Tests run one after another in file order. Running files in parallel
  is a later option.

Output:

```
running 4 tests
test src/calc.test.mh::adds two numbers ... ok
test src/calc.test.mh::rejects text that isn't a number ... FAILED
test src/calc.test.mh::uses a private helper ... ok
test src/calc.test.mh::big input ... skipped (slow; run with a real dataset)

failures:

---- src/calc.test.mh::rejects text that isn't a number ----
assert_eq failed at src/calc.test.mh:13
  actual:   "ab"
  expected: "abc"

test result: FAILED. 2 passed; 1 failed; 1 skipped; finished in 0.04s
```

A non-assertion error prints its `message()` and Mah stack trace, as an
uncaught error does in `mah run`.

## Bytecode

A bundle built for testing carries a **test table**: `(name, function
index, source line)` per test. The runner asks the VM to run one entry.
Normal `mah build` output never contains a test table. Both runtimes
need the "run test N" entry point and the `SkipTest`/`AssertionError`
reporting, with parity tests in `runtime/tests/vm_diff.py`.
`docs/MAHC_FORMAT.md` gets the new optional section.

## Implementation plan

1. `test` blocks through the whole pipeline, and the `.test.mh` rules
   (declarations only, excluded from run/build).
2. Sibling private access in the preprocessor.
3. `std:test` (`mah/std/test.mh`): the assertions, `AssertionError`,
   `SkipTest`.
4. Test table in the bundle, and the per-test entry point in both VMs.
5. `mah test` CLI: discovery, filtering, isolation, timeouts, output,
   exit code.
6. `mah init` template gains `src/main.test.mh`; update
   `mah/project/templates/` (language reference, AGENTS.md: "run
   `mah test`"), the website docs, and add `examples/` coverage.

Tests per `docs/TESTING.md`: parser/formatter round-trips for `test`
blocks, the contextual-keyword rule, private-access scoping, each
assertion's pass/fail output, skip, isolation between tests, async
tests, leftover-timer warnings, filter and exit codes, on both VMs.
