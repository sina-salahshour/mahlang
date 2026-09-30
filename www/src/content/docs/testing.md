---
title: Testing
order: 18
section: Tooling
---

Mah has a built-in test runner. Tests live in `*.test.mh` files next to
the code they test, and `mah test` runs them.

```mah
# src/calc.test.mh
import "std:test"

fn add(a, b) { a + b }

test "adds two numbers" {
    assert_eq(add(2, 3), 5)
    assert(add(1, 1) > 1, "should grow")
}

test "division by zero throws" {
    let e = assert_throws(fn() { 1 / 0 })
    assert_eq(e.message, "Division by zero")
}

test "not ready yet" {
    skip("needs real data")
}
```

```text
$ mah test
running 3 tests
test src/calc.test.mh::adds two numbers ... ok
test src/calc.test.mh::division by zero throws ... ok
test src/calc.test.mh::not ready yet ... skipped (needs real data)

test result: ok. 2 passed; 0 failed; 1 skipped; finished in 0.04s
```

A new project from `mah init` already has one, `src/main.test.mh`.

## Test files

- A test file holds only declarations (`import`, `fn`, `let`, `struct`,
  `enum`, `trait`, `impl`) and `test "name" { ... }` blocks. Anything that
  would run by itself, like a top-level `print`, is a compile error.
- `test` is only a keyword there: at the top level of a test file, right
  before the test's name. Everywhere else it's an ordinary name.
- `mah run` and `mah build` refuse a test file, and nothing can import one,
  so tests never end up in your program.
- `x.test.mh` importing `x.mh` from the same directory sees **all** of
  `x.mh`'s top-level names, exported or not, so private helpers can be
  tested directly. Every other importer still sees only exports.

## Assertions: `std:test`

| Function | |
|---|---|
| `assert(cond, message = "")` | fails unless `cond` is truthy |
| `assert_eq(actual, expected, message = "")` | fails unless `actual == expected`; the report shows both |
| `assert_ne(a, b, message = "")` | fails if `a == b` |
| `assert_throws(f)` | calls `f` and returns what it threw; fails if it returned normally |
| `fail(message)` | fails right away |
| `skip(reason = "")` | stops the test and reports it as skipped |

A failing assertion throws an `AssertionError`, and `skip` throws
`SkipTest` (both exported by `std:test`). A test body can throw anything else too: any other error
fails the test and is reported with its message and a Mah stack trace.
`assert_eq` compares with `==`, which compares Vectors, Maps, structs and
enums by identity, so `assert_eq([1], [1])` fails. Compare their parts
instead.

## Running tests

```text
mah test                 # every *.test.mh under the project
mah test divide          # tests whose name, or file::name, contains "divide"
mah test --file src/calc.test.mh
mah test --vm rust       # run on mah-vm instead of the Python VM
mah test --timeout 500   # fail any test that takes longer than 500ms
```

- `mah test` searches the whole project (the directory with
  `mah-project.toml`), skipping `build/` and hidden directories.
- Every test runs in a **fresh VM**: the file's declarations run again for
  each test, so no state leaks between tests.
- A test may `.await` and `sleep_async`; it's done when its body returns.
  Timers still pending then are dropped, with a warning next to the result.
- A failure is reported at the line of the failing assertion in your test
  file, with the values involved and anything the test printed:

```text
---- src/calc.test.mh::adds two numbers ----
assert_eq failed at src/calc.test.mh:8
  actual:   4
  expected: 5
```

The exit code is 1 if any test failed (or a test file doesn't compile),
so `mah test` works as a CI step.
