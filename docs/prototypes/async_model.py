"""Prototype / spike -- validates Mah's future async scheduling policy in
isolation. NOT the real interpreter; nothing here is wired into mah.py or
the v2 pipeline. See docs/NEXT_PHASES.md's "Async" section.

The policy being tested (from the user's own example):

    fn foo() { print("hey") }

    print("1")
    detach foo()
    print("2")

    # -> 1, hey, 2  (NOT 1, 2, hey)

`detach` is not itself a scheduling boundary. It means "start running this
call right now, synchronously, but hand me a Promise instead of implicitly
unwrapping its result." The callee runs eagerly -- exactly as if called
directly -- for as long as it never hits a *real* suspension point. Only a
genuine scheduled operation (here: file read/write on a background thread)
actually yields control back to the caller before finishing; everything
else, however many calls deep, just runs straight through in the same tick.

Mah function bodies are modeled here as Python generators: a plain call
with no real suspension point is just a generator that runs to completion
without ever yielding (see `sync_body`); an async-shaped call `yield`s a
Promise at each point Mah would write `promise.await`. This mirrors
V2_DESIGN.md's calling convention on purpose: a generator's frame is a
heap object independent of the Python call stack, resumable later --
exactly the property that section requires of the real interpreter's
frame/pc state, just borrowed from the host language for this spike
instead of hand-rolled. The real interpreter must implement the same
policy over its own explicit (pc, frame, return-stack) loop, not by
literally reusing Python generators.
"""

from __future__ import annotations

import queue
import threading


class Promise:
    """A handle to a value that may not be ready yet."""

    def __init__(self):
        self.done = False
        self.value = None
        self._callbacks = []

    def resolve(self, value):
        # Only ever called on the scheduler thread (see io_completions
        # below) -- this is the single-threaded, JS-style model: no two
        # callbacks ever run concurrently.
        if self.done:
            return
        self.done = True
        self.value = value
        callbacks, self._callbacks = self._callbacks, []
        for callback in callbacks:
            callback(value)

    def on_resolve(self, callback):
        if self.done:
            # Already resolved -- call back immediately, synchronously.
            # This is *why* awaiting an already-available value never
            # causes real scheduling: no queueing happens here at all.
            callback(self.value)
        else:
            self._callbacks.append(callback)


class Scheduler:
    """The 'microtask queue': completions reported by background I/O
    threads, drained one at a time on the single logical main thread."""

    def __init__(self):
        self.io_completions: queue.Queue = queue.Queue()

    def run_until_idle(self, pending_threads):
        while any(t.is_alive() for t in pending_threads) or not self.io_completions.empty():
            promise, value = self.io_completions.get()
            promise.resolve(value)


SCHEDULER = Scheduler()
_background_threads: list[threading.Thread] = []


def _drive(gen, send_value, result_promise):
    """Step `gen` forward. Keeps stepping synchronously through every
    already-resolved `yield` (no real suspension); stops -- without
    blocking -- the moment it yields a Promise that isn't resolved yet."""
    try:
        awaited = gen.send(send_value)
    except StopIteration as stop:
        result_promise.resolve(stop.value)
        return
    awaited.on_resolve(lambda value: _drive(gen, value, result_promise))


def detach(gen):
    """`detach foo()` -- start `foo`'s body running right now. Returns a
    Promise for its eventual result without blocking the caller."""
    result_promise = Promise()
    _drive(gen, None, result_promise)
    return result_promise


def sync_body(fn):
    """Wrap a plain function that never awaits anything so it fits the
    same generator-based calling convention as an async-shaped one."""

    def make_gen(*args, **kwargs):
        return fn(*args, **kwargs)
        yield  # noqa: unreachable -- presence of `yield` makes this a
        # generator function; the body above still runs to completion in
        # one synchronous step, so no real suspension is ever introduced.

    return make_gen


# ---------------------------------------------------------------------------
# The first real scheduled primitives: file read/write on a background
# thread, so the "main thread" is never blocked on disk I/O -- this is what
# makes awaiting them a genuine suspension instead of an immediate resume.
# ---------------------------------------------------------------------------


def read_file_async(path: str) -> Promise:
    promise = Promise()

    def work():
        with open(path, "r", encoding="utf-8") as f:
            data = f.read()
        SCHEDULER.io_completions.put((promise, data))

    thread = threading.Thread(target=work, daemon=True)
    _background_threads.append(thread)
    thread.start()
    return promise


def write_file_async(path: str, data: str) -> Promise:
    promise = Promise()

    def work():
        with open(path, "w", encoding="utf-8") as f:
            f.write(data)
        SCHEDULER.io_completions.put((promise, None))

    thread = threading.Thread(target=work, daemon=True)
    _background_threads.append(thread)
    thread.start()
    return promise


# ---------------------------------------------------------------------------
# Demo 1: the user's exact example -- detach with no real suspension point
# runs eagerly, in place, before the caller's next line.
# ---------------------------------------------------------------------------


@sync_body
def foo():
    print("hey")


# ---------------------------------------------------------------------------
# Demo 2: detach around a call that DOES hit real (scheduled) I/O -- this
# one genuinely returns before finishing, and the rest only happens once
# the scheduler drains the background thread's completion.
# ---------------------------------------------------------------------------


def slow_task():
    yield write_file_async("/tmp/mah_async_demo.txt", "hello from mah\n")
    content = yield read_file_async("/tmp/mah_async_demo.txt")
    print("[slow_task] read:", content.strip())


def main():
    print("1")
    detach(foo())
    print("2")

    print("3")
    detach(slow_task())
    print("4")

    SCHEDULER.run_until_idle(_background_threads)


if __name__ == "__main__":
    main()
