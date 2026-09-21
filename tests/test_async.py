"""End-to-end behavioral tests for M10: async (`detach` / `.await` /
`sleep_async`) -- see docs/TESTING.md and docs/V2_DESIGN.md's M10
milestone.

Single-threaded cooperative concurrency: `detach <call>` starts the call
immediately, synchronously, stepping it exactly like a direct call until
it hits a *real* suspension (currently only `.await`ing a still-pending
`Promise`) -- `detach` is never itself a scheduling boundary. `sleep_async
(ms)` is the first genuinely scheduled builtin: it schedules a timer that
fires after `ms` milliseconds.

Key rule, revised after the first cut of this milestone: **`.await` is
only needed once you've explicitly opted out of blocking via `detach`.**
A bare `sleep_async(ms)` -- not wrapped in `detach` -- auto-awaits its own
result immediately, so it just blocks the current task, exactly like an
ordinary synchronous function call already does. `detach sleep_async(ms)`
is the one exception to "detach wraps a plain function call": it hands
back the raw, still-pending `Promise` instead, for you to `.await`
whenever you're ready. A `Promise` itself is a real built-in Mah enum
(`Promise.Pending` / `Promise.Settled { value }`, see
runtime_values.PromiseInstance), not an opaque host value -- it prints and
pattern-matches through the same generic machinery any other enum does.

These are the scenarios worked out by hand while landing this milestone;
each test's docstring retraces the control-flow reasoning that pins down
its expected stdout.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support import run_source


class EagerDetachTests(unittest.TestCase):
    def test_detach_with_no_real_suspension_runs_eagerly_in_place(self):
        # foo() never touches anything that needs real scheduling, so
        # `detach foo()` runs it to completion in place before the next
        # line even starts -- 1, hey, 2, NOT 1, 2, hey.
        src = """
        fn foo() { print("hey") }
        print("1")
        detach foo()
        print("2")
        """
        self.assertEqual(run_source(src), "1\nhey\n2\n")

    def test_detach_returns_an_already_resolved_promise_when_never_suspended(self):
        # No timer involved anywhere in this script, so there is nothing
        # to drain -- `p.await` right after `detach get_value()` must
        # yield 42 immediately, with no hang and no reordering.
        src = """
        fn get_value() { return 42 }
        let p = detach get_value()
        print(p.await)
        """
        self.assertEqual(run_source(src), "42\n")


class BareSleepAsyncBlocksTests(unittest.TestCase):
    """The core DX rule: a bare (non-detached) `sleep_async` needs no
    `.await` at all -- it just blocks, like any ordinary call."""

    def test_bare_sleep_async_blocks_with_no_await_needed(self):
        src = """
        print("before")
        sleep_async(20)
        print("after")
        """
        self.assertEqual(run_source(src), "before\nafter\n")

    def test_writing_await_on_a_bare_sleep_async_result_is_an_error(self):
        # A bare sleep_async(ms) already auto-awaits itself and evaluates
        # to `none` (not a Promise) -- explicitly `.await`ing that `none`
        # result is a genuine mistake, and must raise a clean error rather
        # than silently doing something unexpected.
        with self.assertRaisesRegex(Exception, r"\.await|[Pp]romise"):
            run_source("sleep_async(10).await\n")

    def test_bare_sleep_async_inside_a_function_blocks_that_functions_caller(self):
        # Calling `waiter()` directly (no detach) blocks exactly like any
        # other synchronous call with a slow body would.
        src = """
        fn waiter(ms) {
            sleep_async(ms)
            print("waited")
        }
        print("start")
        waiter(15)
        print("end")
        """
        self.assertEqual(run_source(src), "start\nwaited\nend\n")


class DetachSleepAsyncTests(unittest.TestCase):
    """`detach sleep_async(ms)` -- the one exception to "detach wraps a
    plain function call" -- hands back the raw, still-pending `Promise`
    for you to `.await` later, instead of auto-blocking."""

    def test_detach_sleep_async_returns_pending_promise_then_await_gets_the_value(self):
        src = """
        let x = detach sleep_async(20)
        print("started")
        x.await
        print("finished")
        """
        self.assertEqual(run_source(src), "started\nfinished\n")

    def test_top_level_detach_sleep_async_does_not_block_other_work(self):
        # Detaching the sleep must NOT block "middle" from printing before
        # the timer fires -- that's the entire point of detach over a bare
        # sleep_async call.
        src = """
        let x = detach sleep_async(20)
        print("first")
        print("middle")
        x.await
        print("last")
        """
        self.assertEqual(run_source(src), "first\nmiddle\nlast\n")


class PromiseAsEnumTests(unittest.TestCase):
    """A `Promise` is a real built-in enum (Pending/Settled), not an
    opaque host value -- it prints and pattern-matches accordingly."""

    def test_promise_prints_as_pending_then_settled(self):
        src = """
        let x = detach sleep_async(10)
        print(x)
        x.await
        print(x)
        """
        self.assertEqual(run_source(src), "Promise.Pending\nPromise.Settled { value: none }\n")

    def test_promise_can_be_pattern_matched_directly(self):
        src = """
        fn describe(p) {
            match p {
                Promise.Pending => { print("still waiting") }
                Promise.Settled { value } => { print("got it") }
            }
        }
        let x = detach sleep_async(10)
        describe(x)
        x.await
        describe(x)
        """
        self.assertEqual(run_source(src), "still waiting\ngot it\n")


class RealSuspensionTests(unittest.TestCase):
    def test_detach_around_a_real_suspension_returns_immediately(self):
        # `foo` calls bare `sleep_async(50)` -- which auto-awaits itself
        # -- so THAT auto-await is the real suspension point `detach`
        # hits while driving foo eagerly. detach hands back a pending
        # Promise for foo's own eventual completion; print("2") then runs
        # immediately, and only once the interpreter later drains the
        # 50ms timer does foo resume and print "done sleeping".
        src = """
        fn foo() {
            sleep_async(50)
            print("done sleeping")
        }
        print("1")
        detach foo()
        print("2")
        """
        self.assertEqual(run_source(src), "1\n2\ndone sleeping\n")

    def test_concurrent_detached_sleeps_resolve_in_wake_time_order(self):
        # The 10ms timer fires before the 30ms one regardless of which
        # `detach` ran first -- wake-time order, not detach order.
        src = """
        fn after(ms, label) {
            sleep_async(ms)
            print(label)
        }
        detach after(30, "second")
        detach after(10, "first")
        print("start")
        """
        self.assertEqual(run_source(src), "start\nfirst\nsecond\n")

    def test_process_does_not_exit_before_a_detached_timer_fires(self):
        # The program must not exit the instant top-level code finishes
        # (right after `detach later()` / `print("main done")`) -- it
        # keeps running until the detached timer drains (Node-like
        # process lifetime).
        src = """
        fn later() {
            sleep_async(10)
            print("fired")
        }
        detach later()
        print("main done")
        """
        self.assertEqual(run_source(src), "main done\nfired\n")


class DeferIsolationTests(unittest.TestCase):
    def test_defer_stack_is_per_task_not_shared(self):
        # detach inner() starts inner, hits bare sleep_async(10)'s own
        # auto-await, and suspends -- inner's own `defer print("inner-
        # defer")` is now on INNER's task's own defer_stack, not yet run.
        # Main continues: outer_main() runs synchronously (an ordinary
        # call, not detach) -- prints "outer-body", falls off the end, its
        # own defer runs, printing "outer-defer" -- all of this happens
        # before inner's timer ever fires. Only once the interpreter later
        # drains inner's timer does inner resume, print "inner-body", fall
        # off the end, and run its OWN defer, printing "inner-defer". If
        # defer_stack were still a single global stack instead of
        # per-Task, inner's deferred closure would either leak into
        # outer_main's own defer-draining or run at the wrong time.
        src = """
        fn inner() {
            defer print("inner-defer")
            sleep_async(10)
            print("inner-body")
        }
        fn outer_main() {
            defer print("outer-defer")
            print("outer-body")
        }
        detach inner()
        outer_main()
        """
        self.assertEqual(
            run_source(src),
            "outer-body\nouter-defer\ninner-body\ninner-defer\n",
        )


class ErrorCaseTests(unittest.TestCase):
    def test_await_on_a_non_promise_value_raises_a_clean_error(self):
        with self.assertRaisesRegex(Exception, r"\.await|[Pp]romise"):
            run_source("let x = 5\nx.await")

    def test_detach_on_a_non_function_raises_a_clean_error(self):
        with self.assertRaises(Exception):
            run_source("let x = 5\ndetach x()")


if __name__ == "__main__":
    unittest.main()
