"""End-to-end behavioral tests for M9: block-scoped `defer` -- see
docs/TESTING.md and docs/V2_DESIGN.md's M9 milestone ("`defer`").

`defer <stmt>` schedules `<stmt>` to run when the block it's written in
exits (fallthrough, or a `return`/`break`/`continue` that jumps out of
it), most-recently-deferred first (LIFO). Block-scoped like Zig's
`defer`, not function-scoped like Go's. These are the scenarios worked
out by hand while landing M9; each test's docstring/comments retrace the
control-flow reasoning that pins down its expected stdout.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support import run_source


class BasicLifoTests(unittest.TestCase):
    def test_multiple_defers_in_one_block_run_lifo_at_block_end(self):
        src = """
        fn demo() {
            defer print("one");
            defer print("two");
            defer print("three");
            print("body");
        }
        demo();
        """
        # Registration order one/two/three; block-exit fires them
        # most-recently-registered first: three, two, one -- after the
        # block's own statements (print("body")) already ran.
        self.assertEqual(run_source(src), "body\nthree\ntwo\none\n")


class ReturnUnwindTests(unittest.TestCase):
    def test_return_unwinds_nested_blocks_innermost_first(self):
        src = """
        fn demo() {
            defer print("outer");
            if true {
                defer print("inner");
                return 42;
            }
            return 0;
        }
        print(demo());
        """
        # `return 42` inside the `if` block first drains that block's own
        # defer scope ("inner"), then -- since it's also leaving the
        # function's outer block -- drains that scope too ("outer"),
        # before the call actually returns 42 to the caller's print.
        self.assertEqual(run_source(src), "inner\nouter\n42\n")


class OnlyExecutedDefersRegisterTests(unittest.TestCase):
    def test_defer_in_untaken_branch_never_fires(self):
        src = """
        fn demo() {
            if false {
                defer print("never");
            }
            print("done");
        }
        demo();
        """
        self.assertEqual(run_source(src), "done\n")


class BlockScopedNotFunctionScopedTests(unittest.TestCase):
    def test_defer_in_while_body_fires_once_per_iteration(self):
        src = """
        let i = 0;
        while i < 3 {
            defer print("iter");
            i = i + 1;
        }
        print("after");
        """
        # Block-scoped (Zig-style): each of the 3 iterations opens and
        # drains its OWN defer scope at the end of that iteration's body,
        # rather than piling all three up until the loop as a whole ends
        # (which would be Go's function-scoped behavior) -- see
        # BreakContinueDrainTests below for a version that actually
        # distinguishes the two (interleaved prints inside the loop).
        self.assertEqual(run_source(src), "iter\niter\niter\nafter\n")


class BreakContinueDrainTests(unittest.TestCase):
    def test_break_and_continue_drain_current_iteration_scope(self):
        src = """
        let i = 0;
        while true {
            defer print("cleanup");
            i = i + 1;
            if i == 2 {
                continue;
            }
            if i == 3 {
                break;
            }
            print("normal");
        }
        print("done");
        """
        # Traced by hand:
        # iteration 1 (i becomes 1): neither if fires -> print("normal")
        #   -> falls off the end of the loop body block -> drains its
        #   defer scope -> "cleanup" -> jumps back to the condition check.
        # iteration 2 (i becomes 2): i == 2 -> `continue` must drain THIS
        #   iteration's defer scope ("cleanup") before jumping back to the
        #   condition check -- "normal" is never reached.
        # iteration 3 (i becomes 3): i == 3 -> `break` must likewise drain
        #   this iteration's defer scope ("cleanup") before leaving the
        #   loop -- "normal" is never reached.
        # after the loop: "done".
        self.assertEqual(
            run_source(src),
            "normal\ncleanup\ncleanup\ncleanup\ndone\n",
        )


class RecursiveCallsIndependentDefersTests(unittest.TestCase):
    def test_each_recursive_call_runs_its_own_defer_on_its_own_return(self):
        src = """
        fn recurse(n) {
            defer print(n);
            if n == 0 {
                return;
            }
            recurse(n - 1);
        }
        recurse(2);
        """
        # recurse(2) calls recurse(1) calls recurse(0); recurse(0) returns
        # (and fires its own defer) first, then recurse(1) resumes and
        # returns (firing its defer), then recurse(2) does the same --
        # innermost/last-called-first.
        self.assertEqual(run_source(src), "0\n1\n2\n")


class CaptureByReferenceTests(unittest.TestCase):
    def test_defer_sees_value_at_run_time_not_at_defer_time(self):
        src = """
        fn demo() {
            let x = 1;
            defer print(x);
            x = 2;
        }
        demo();
        """
        # The deferred closure captures `x` by reference (same closure/
        # frame machinery as any other `fn`), so it observes the value at
        # the moment it actually runs (block exit, after `x = 2`), not the
        # value at the moment `defer` was written.
        self.assertEqual(run_source(src), "2\n")


class NestedFunctionOwnScopeTests(unittest.TestCase):
    def test_inner_function_defer_independent_of_outer_loop_defer(self):
        src = """
        fn inner() {
            defer print("inner-defer");
            print("inner-body");
        }
        let i = 0;
        while i < 2 {
            defer print("outer-defer");
            inner();
            i = i + 1;
        }
        """
        # Verifies the compile-time defer-depth counter correctly resets
        # across a nested function boundary: inner()'s own defer fires
        # when IT returns, well before the outer while-loop body's own
        # defer scope drains at the end of that iteration.
        self.assertEqual(
            run_source(src),
            "inner-body\ninner-defer\nouter-defer\n" * 2,
        )


class BlockFormTests(unittest.TestCase):
    def test_defer_block_form_runs_all_statements_in_order(self):
        src = """
        fn demo() {
            defer {
                print("a");
                print("b");
            }
            print("body");
        }
        demo();
        """
        # A single defer registers one closure; that closure's body runs
        # its own statements in their own written order ("a" then "b")
        # when the closure itself is invoked at block-exit.
        self.assertEqual(run_source(src), "body\na\nb\n")


class AssignmentFormTests(unittest.TestCase):
    def test_defer_assignment_runs_at_block_exit(self):
        src = """
        struct Box { value }
        fn demo(b) {
            defer b.value = 99;
            print(b.value);
        }
        let box = Box { value: 1 };
        demo(box);
        print(box.value);
        """
        # Structs are reference types in Mah: the deferred assignment
        # writes through the captured `b` (same heap StructInstance as
        # `box`), observable via `box.value` after `demo` returns.
        self.assertEqual(run_source(src), "1\n99\n")


class DeferBreakContinueRaiseCleanlyTests(unittest.TestCase):
    def test_defer_break_raises_cleanly_instead_of_corrupting(self):
        # `defer` accepting any `_STATEMENT_LEADING` statement (needed for
        # `defer print(...)`, the most common form) also makes `defer
        # break`/`defer continue` reachable -- these desugar to a `break`/
        # `continue` inside the deferred closure's own synthesized body,
        # which is a nested `fn`. Found while landing M9: a nested `fn`'s
        # `break`/`continue` used to silently target the ENCLOSING loop
        # instead of raising, corrupting execution (see
        # tests/test_language.py's ErrorTests for the general-case
        # regression tests) -- must raise the same clean error here too.
        src = """
        let i = 0
        while i < 5 {
            defer break
            i = i + 1
        }
        print(i)
        """
        with self.assertRaises(Exception):
            run_source(src)

    def test_defer_continue_raises_cleanly_instead_of_corrupting(self):
        src = """
        let i = 0
        while i < 5 {
            defer continue
            i = i + 1
        }
        print(i)
        """
        with self.assertRaises(Exception):
            run_source(src)


if __name__ == "__main__":
    unittest.main()
