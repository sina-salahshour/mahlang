"""M28 (docs/MAH_TEST.md, docs/MAHC_FORMAT.md #6.10): the result of
running one test of a `mah test` build, and its text form.

Both VMs produce the same outcome. The Python VM returns it directly
(`code_interpreter.run_test_bytes`); the Rust VM (`mah-vm test FILE INDEX`)
prints `format_outcome`'s text to stderr, and the runner reads it back with
`parse_outcome`. Like the VM, this module never imports the compiler.

Text form, one item per line:

    status ok|skipped|failed|timeout
    leftover 0|1
    frame LINE FILE        (zero or more, innermost first; FILE is `-` for
                            the test file itself)
    message
    ...the message, to the end...
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TestOutcome:
    # "ok", "skipped" (SkipTest), "failed" (any other escaping error), or
    # "timeout" (ran past the runner's deadline).
    status: str
    # The skip reason, or the failure's report (an AssertionError's own
    # message, `Uncaught T: m`, or a RuntimeError's message).
    message: str = ""
    # Where the error was thrown, then each call site enclosing it:
    # `(file, line)`, file `None` for the test file itself.
    frames: list = field(default_factory=list)
    # Timers (sleeps, detached work) were still pending when the test body
    # finished; they were dropped.
    leftover: bool = False


def format_outcome(outcome: TestOutcome) -> str:
    lines = [f"status {outcome.status}", f"leftover {1 if outcome.leftover else 0}"]
    for file, line in outcome.frames:
        lines.append(f"frame {line} {file if file is not None else '-'}")
    lines.append("message")
    return "\n".join(lines) + "\n" + outcome.message


def parse_outcome(text: str) -> TestOutcome | None:
    """The outcome in `text` (a `mah-vm test` run's stderr), or None when
    it isn't one (the VM failed before running the test)."""
    lines = text.split("\n")
    if len(lines) < 3 or not lines[0].startswith("status ") or not lines[1].startswith("leftover "):
        return None
    outcome = TestOutcome(status=lines[0][len("status ") :], leftover=lines[1] == "leftover 1")
    i = 2
    while i < len(lines) and lines[i].startswith("frame "):
        _frame, line, file = lines[i].split(" ", 2)
        outcome.frames.append((None if file == "-" else file, int(line)))
        i += 1
    if i >= len(lines) or lines[i] != "message":
        return None
    outcome.message = "\n".join(lines[i + 1 :])
    return outcome
