# Development workflow: model tiers and delegation

How to split design/implementation/verification work across model tiers
for mahlang development, for cost efficiency without giving up
correctness. Derived from how M1 (heap frames + closures) was actually
built — read `docs/V2_DESIGN.md`'s M1 entry alongside this for a concrete
example of the pattern below in action.

## The three roles

**Thinker (top-tier model)** — design, spec-writing, and *final*
correctness verification. Does not scale down. An error in the design
(e.g. getting the static-chain `depth` arithmetic wrong in M1) silently
corrupts everything built on top of it, and *catching* that kind of bug
requires reconstructing what "should" happen well enough to notice it
didn't — the same caliber of reasoning needed to design it. Never
delegate this role to a cheaper model, including the review step: a
model weaker than the one that wrote the code is a bad reviewer of that
code.

**Coder (mid-tier model, e.g. Sonnet)** — mechanical implementation from
a spec precise enough to leave no open design decisions. This is where
token *volume* actually lives (writing five interlocking files is
expensive in tokens, not in judgment, once the spec is complete) — so
it's the right place to downgrade, *provided* the handoff is a complete
spec, not an invitation to design. The test: if the coder has to make a
real judgment call partway through, the spec was incomplete. (M1's one
judgment call — `none` needing `__bool__` — was narrow and forced by an
existing regression test, not open-ended; that's the difference between a
safe and unsafe thing to leave unresolved.)

**Cheap gate (cheapest model, e.g. Haiku)** — mechanical pre-checks only,
run *before* the thinker spends its own attention: did `make test` pass,
does the changed-file list match what was asked, any stray debug output
left in. This is a checklist, not a review — it filters obvious misses
for near-zero cost so the thinker's (expensive) verification pass only
has to fire when something has already cleared the mechanical bar. Do
**not** use a cheap model as a substitute for the thinker's correctness
review — it will pattern-match rather than reason, and compiler-internals
bugs are exactly the kind of thing that requires reasoning to catch.

## The actual mechanism: dispatching a coder

Use a fresh subagent (`Agent` tool, `subagent_type: "general-purpose"`,
`model: "sonnet"` or similar) with a **fully self-contained prompt** — it
has no memory of the conversation that produced the design. Concretely,
the prompt needs:

1. Enough background that the agent understands *why*, not just *what*
   (one paragraph, not the whole design doc).
2. The complete, unambiguous design: exact data-structure shapes, exact
   instruction/opcode formats, exact algorithms — worked out fully by the
   thinker beforehand, not sketched. If two files need to agree on a
   shared format (e.g. codegen's IR tuples and the interpreter's opcode
   handlers), the prompt must give both sides the *same* exact shape, not
   independently-described equivalents that might drift.
3. An explicit file list and, for each file, what changes.
4. An explicit **test list** with concrete expected outputs (not "test
   that it works" — literal input/output pairs) — see `docs/TESTING.md`;
   the coder should add these to `tests/` as part of the task, not just
   run ad hoc checks.
5. An explicit **out-of-scope** list — what not to touch. Scope creep in
   a cheaper agent is harder to catch later than scope creep you'd catch
   live.
6. An instruction to report back: files changed, exact test output, and
   any judgment call it was forced to make and why.

**Mechanical constraint:** `fork` (the Agent tool's context-inheriting
mode) always runs on *your own* model — there's no "fork but cheaper."
A cheaper coder has to be a fresh agent with an explicit `model`
override, which means no shared context. The fully-self-contained-prompt
requirement above isn't a style preference, it's the necessary trade for
using a cheaper model at all.

## After the coder returns: verify yourself, don't just read its summary

An agent's self-report describes what it *intended* and *believes* it
did, not a guarantee. After M1 landed, verification meant: reading the
actual diffs (not trusting the file-list summary), re-running the exact
tests the coder ran, and adding at least one scenario the coder's test
list didn't cover (a 3-level-deep closure capture, beyond the 1-level
case that was specified) to stress the design past exactly what was
asked. That last part matters — a coder will reliably satisfy the literal
test list; only the thinker's own additional probing catches gaps *in*
the test list itself.

## When to parallelize, and when not to

**Don't split tightly-coupled core changes across multiple agents.**
Parser, resolve, codegen, and the VM's opcode handlers have to agree on
exact shapes (what an address looks like, what an opcode's payload
means); splitting them across agents risks two independently-plausible
but incompatible formats meeting at runtime, which is a much worse
failure mode than a single agent taking longer. M1 was one dispatch for
exactly this reason.

**Do parallelize independent, low-risk trailing work once the core spec
is stable**: syncing `syntax-highlight/grammar.js` to new syntax, adding
`examples/*.mh` files, expanding docs — these touch disjoint files with
low interdependency risk, and are reasonable to hand to multiple cheap
agents (or the same one, sequentially) after the risky core lands and is
verified.

## The compounding lever: the test suite itself

Before `tests/` existed, "verify a milestone" meant the thinker manually
re-deriving and re-running scenarios from scratch every time — expensive,
and it doesn't accumulate. Now, "does `make test` pass" is a cheap,
durable proxy for correctness that gets *stronger* every milestone as
tests accumulate (see `docs/TESTING.md`). This is the actual highest-
leverage cost reduction available here — not the model tier picked for
review, but shrinking how much manual, human-grade re-verification the
thinker needs to do per milestone by making the coder's own task include
extending the thing that checks its work automatically next time.
