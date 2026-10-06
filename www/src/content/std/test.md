---
title: std:test
order: 17
section: Language & testing
summary: Assertions for mah test, assert, assert_eq, assert_throws, fail and skip, with readable failure reports.
---

# `std:test`

The assertions used in tests run by `mah test`. Tests live in `*.test.mh`
files as `test "name" { ... }` blocks; this module gives them `assert`,
`assert_eq` and friends. The test runner itself (files, filters, timeouts,
running on `mah-vm`) is covered on the [Testing](/docs/testing) page.

```mah
# src/calc.test.mh
import "std:test"

fn add(a, b) { a + b }

test "adds two numbers" {
    assert_eq(add(2, 3), 5)
    assert(add(1, 1) > 1, "should grow")
}
```

```text
$ mah test
running 1 test
test src/calc.test.mh::adds two numbers ... ok

test result: ok. 1 passed; 0 failed; 0 skipped; finished in 0.01s
```

Import it flat (`import "std:test"`) so the assertions read naturally.

## Assertions

- `assert(cond, message = "")` fails unless `cond` is truthy.
- `assert_eq(actual, expected, message = "")` fails unless
  `actual == expected`, and the report shows both values.
- `assert_ne(a, b, message = "")` fails if they're equal.
- `fail(message)` fails right away, for code paths that shouldn't run.

A failure is reported at the line of the failing assertion in your test
file, with the values involved and your message.

`assert_eq` compares with `==`, which compares Vectors, Maps, structs and
enums by **identity**, so `assert_eq([1], [1])` fails. Compare their parts,
or their printed form:

```mah
import "std:test"

test "comparing collections" {
    let got = [1, 2, 3].map(fn(x) { x * 2 }).reduce()
    assert_eq(got.len(), 3)
    assert_eq(got.join(","), "2,4,6")
    assert_eq(got.to_string(), "[2, 4, 6]")
}
```

## Testing errors

`assert_throws(f)` calls `f`, **returns the error it threw**, and fails if
it returned normally. Check the error's type and fields afterwards:

```mah
import "std:test"
import json from "std:json"

test "division by zero throws" {
    let e = assert_throws(fn() { 1 / 0 })
    assert_eq(e.message, "Division by zero")
}

test "bad JSON reports where" {
    let e = assert_throws(fn() { json.parse("[1,") })
    match e {
        json.JsonError.Syntax { message: m, line: l, column: c } => { assert_eq(l, 1) }
        _ => { fail("expected a syntax error") }
    }
}
```

## Skipping

`skip(reason = "")` stops the test and reports it as **skipped**, not
failed. Use it for tests that need something unavailable right now:

```mah
import "std:test"
import process from "std:process"

test "talks to the database" {
    if process.env_get("DATABASE_URL").is_none() {
        skip("DATABASE_URL not set")
    }
    # ... the real test
}
```

## How failures work

A failing assertion throws `AssertionError { message }`, and `skip`
throws `SkipTest { reason }`; both are exported. Any other error thrown
from a test body also fails the test, reported with its message and a Mah
stack trace, so a test doesn't need to catch errors it doesn't expect.

Every test runs in a **fresh VM**: the file's top-level declarations run
again for each test, so no state leaks between tests. A test file may
import the module it tests and see its non-exported names too, so private
helpers can be tested directly.

## Reference

| Function | |
|---|---|
| `assert(cond, message = "")` | fails unless `cond` is truthy |
| `assert_eq(actual, expected, message = "")` | fails unless `actual == expected` |
| `assert_ne(a, b, message = "")` | fails if `a == b` |
| `assert_throws(f)` | calls `f`; returns its error, fails if none |
| `fail(message)` | fails right away |
| `skip(reason = "")` | ends the test as skipped |

| Type | |
|---|---|
| `AssertionError` | `{ message }`, a failed assertion |
| `SkipTest` | `{ reason }`, thrown by `skip` |
