# Testing Mah

## Running the suite

```sh
make test
# or directly:
python3 -m unittest discover -s tests -t . -v
```

No dependencies to install — the suite uses only Python's standard-library
`unittest`, matching this project's zero-dependency ethos (see the LSP's
own "pure Python, standard library only" design in `README.md`). Any test
file can also be run standalone (`python3 tests/test_language.py`) —
useful while iterating on one area.

## Layout

- `tests/support.py` — the only place that knows how to wire
  lexer → parser → resolve → codegen → `run_code` together and capture
  stdout/stdin. Test files call `run_source(mah_code, stdin="")` (in-memory
  snippet) or `run_file(path, stdin="")` (a real file, so relative
  `import`s resolve). **Don't reimplement this plumbing in a new test
  file** — import it from here, the same way `mah.py` itself does.
- `tests/test_lexer.py`, `tests/test_parser.py` — narrow unit tests
  pinning down tokenization and AST shape (precedence/associativity,
  `fn`-desugaring) directly, independent of running any code.
- `tests/test_language.py` — end-to-end behavioral tests: run real Mah
  source through the whole pipeline, assert on what it prints or that it
  raises. This is where most language-semantics coverage belongs (it
  exercises the actual user-visible contract, and survives internal
  refactors of resolve.py/codegen.py that don't change behavior).
- `tests/test_examples.py` — golden-output tests for everything under
  `examples/`. These are the project's existing demo programs; the tests
  just pin their current output so a regression is caught immediately.

## The policy — read this before finishing any milestone

**Every milestone in `docs/V2_DESIGN.md` (and anything in
`docs/NEXT_PHASES.md` once scheduled) must land with tests, not just an
`examples/*.mh` addition.** An example file is for a human to read and run
by hand; it is not checked by `make test` and will not catch a regression
by itself. Concretely, when you add or change language behavior:

1. Add or extend a test in `tests/test_language.py` (or a new
   `tests/test_<feature>.py` for a big enough feature — structs and enums
   each probably warrant their own file once they land) covering the new
   behavior's happy path *and* its error cases (undefined-name-shaped
   mistakes, arity/shape mismatches, wrong-context-use like `break`
   outside a loop — whatever the equivalent misuse is for the new
   construct).
2. If the change affects parsing/precedence/AST shape specifically, add a
   narrow test to `tests/test_parser.py` the way `FnDesugaringTests` does
   for M1's `fn`-unification, not just an end-to-end one — it pinpoints
   *which layer* broke much faster than an end-to-end failure does.
3. Run `make test` and confirm it's green before considering the
   milestone done. A milestone that changes runtime behavior on purpose
   (not a bug fix — an intentional semantic change) must also update
   whichever existing test asserted the old behavior, in the same change,
   with a comment or commit message saying why — never just delete an
   inconvenient assertion.
4. This applies whether you're implementing directly or delegating to a
   subagent (see `.claude/skills/mah-add-feature/SKILL.md`) — the task
   handed to a subagent should explicitly say "add tests to
   tests/test_language.py" and list the scenarios, the same way you'd
   list them for `examples/*.mh` today. `docs/V2_DESIGN.md`'s milestone
   writeups already record which manual scenarios were checked for M0/M1
   (written before this policy existed) — from M2 onward those scenarios
   should be actual test functions, not just prose in the design doc.

This mirrors how M0 and M1 were *actually* verified (a list of manual
`python3 mah.py <snippet>` checks) — the tests in this directory as of M1
landing are exactly those checks, now automated and pinned so they don't
have to be re-derived and re-run by hand for every future change.
