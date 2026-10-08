"""M44 (docs/contracts/M44_threads.md): the process-wide thread runtime of
the Python VM -- jobs on other threads, shared variables and their locks,
semaphores, channels, the wait-for graph and the quiescence rule.

Every VM of a run (the main VM and one per running job) has its own heap;
nothing of one VM's heap is ever touched by another thread. Values cross
threads only as copies (`copy_values`), and every cross-thread event is a
completion posted to the receiving VM's done queue (`ThreadRuntime.post`),
which that VM handles on its own thread -- Promises never cross threads.

This module must not import `mah.compiler`, `mah.preprocessor` or
`mah.lsp` (the VM's independence rule); `code_interpreter` is imported
lazily by the worker.
"""

from __future__ import annotations

import collections
import decimal
import itertools
import queue
import sys
import threading
import time
from decimal import Decimal

from .runtime_values import (
    NONE_VALUE,
    BytesValue,
    Closure,
    EnumInstance,
    Frame,
    MahRuntimeError,
    MahThrow,
    MapValue,
    PromiseInstance,
    StructInstance,
    TypeValue,
    VectorValue,
    _Absent,
)


class NotSendable(Exception):
    """A strict copy met a Promise (docs/contracts/M44_threads.md #4.2)."""


class Abandoned(BaseException):
    """A job VM told to stop (program end, or `process.exit` elsewhere)."""


def thread_error(kind: str, message: str) -> StructInstance:
    return StructInstance("ThreadError", {"kind": kind, "message": message})


NOT_SENDABLE_MESSAGE = "a Promise can't be sent to another thread"
FOREIGN_PROMISE_MESSAGE = (
    "a Promise from another thread can't be awaited here (it was still pending when it was copied)"
)
STUCK_MESSAGE = "the wait can never finish: every thread is waiting"
JOB_STUCK_MESSAGE = "the job never finished: it waits on a Promise nothing will settle"
AWAIT_DEADLOCK_MESSAGE = "deadlock: this await would never end (it waits, through locks or threads, for itself)"


def _settled(value=NONE_VALUE) -> PromiseInstance:
    p = PromiseInstance()
    p.resolve(value)
    return p


def _failed(error) -> PromiseInstance:
    p = PromiseInstance()
    p.fail(error)
    return p


# ---------------------------------------------------------------------------
# Copies (docs/contracts/M44_threads.md #4.2)
# ---------------------------------------------------------------------------

_IMMUTABLE = (bool, str, Decimal, int, float, TypeValue, _Absent)


def copy_values(roots: list, local: bool = False) -> list:
    """Copy every `(value, strict)` root with one identity memo. Strict roots
    must come first: every object reachable from a strict root through
    containers is walked in strict mode before anything else (a frame, and
    so everything below it, is walked in environment mode). A strict Promise
    raises `NotSendable`; an environment-mode Promise is copied by state;
    with `local`, a Promise is kept as the same object. Iterative: shells
    first, filled from a work list."""
    memo: dict = {}
    keep: list = []
    strict_work: collections.deque = collections.deque()
    env_work: collections.deque = collections.deque()

    def conv(v, strict: bool):
        if v is NONE_VALUE or v is None or isinstance(v, _IMMUTABLE):
            return v
        if isinstance(v, PromiseInstance):
            if local:
                return v
            if strict:
                raise NotSendable()
            hit = memo.get(id(v))
            if hit is not None:
                return hit
            p = PromiseInstance()
            p.observed = True
            memo[id(v)] = p
            keep.append(v)
            if v.variant == "Settled":
                p.variant = "Settled"
                p.fields = {"value": NONE_VALUE}
                env_work.append((v, p))
            elif v.variant == "Failed":
                p.variant = "Failed"
                p.fields = {"error": NONE_VALUE}
                env_work.append((v, p))
            else:
                p.variant = "Failed"
                p.fields = {"error": thread_error("foreign_promise", FOREIGN_PROMISE_MESSAGE)}
            return p
        hit = memo.get(id(v))
        if hit is not None:
            return hit
        if isinstance(v, VectorValue):
            shell = VectorValue([])
        elif isinstance(v, MapValue):
            shell = MapValue()
        elif isinstance(v, BytesValue):
            shell = BytesValue(v.data)
            memo[id(v)] = shell
            keep.append(v)
            return shell
        elif isinstance(v, StructInstance):
            shell = StructInstance(v.type_name, {})
            shell.thrown_at = v.thrown_at
            shell.backtrace = list(v.backtrace) if v.backtrace is not None else None
        elif isinstance(v, EnumInstance):
            shell = EnumInstance(v.type_name, v.variant, {})
            shell.thrown_at = v.thrown_at
            shell.backtrace = list(v.backtrace) if v.backtrace is not None else None
        elif isinstance(v, Closure):
            shell = Closure(
                v.code_address, None, v.slot_count, v.param_count, v.name, v.params, v.index, v.rest
            )
            shell.identity = v.identity
            strict = False
        elif isinstance(v, Frame):
            shell = Frame([], None)
            strict = False
        else:
            return v  # anything else is immutable host data
        memo[id(v)] = shell
        keep.append(v)
        (strict_work if strict else env_work).append((v, shell))
        return shell

    def fill(src, dst, strict: bool) -> None:
        if isinstance(src, PromiseInstance):
            if src.variant == "Settled":
                dst.fields = {"value": conv(src.fields["value"], False)}
            else:
                dst.fields = {"error": conv(src.fields["error"], False)}
        elif isinstance(src, VectorValue):
            dst.items = [conv(x, strict) for x in src.items]
        elif isinstance(src, MapValue):
            dst.entries = {k: (key, conv(val, strict)) for k, (key, val) in src.entries.items()}
        elif isinstance(src, (StructInstance, EnumInstance)):
            dst.fields = {k: conv(val, strict) for k, val in src.fields.items()}
        elif isinstance(src, Closure):
            dst.defining_frame = conv(src.defining_frame, False)
        elif isinstance(src, Frame):
            dst.slots = [conv(x, False) for x in src.slots]
            dst.static_parent = conv(src.static_parent, False) if src.static_parent is not None else None

    out = [None] * len(roots)
    order = sorted(range(len(roots)), key=lambda i: not roots[i][1])  # strict roots first
    for i in order:
        value, strict = roots[i]
        out[i] = conv(value, strict)
        if strict:
            while strict_work:
                src, dst = strict_work.popleft()
                fill(src, dst, True)
    while strict_work or env_work:
        if strict_work:
            src, dst = strict_work.popleft()
            fill(src, dst, True)
        else:
            src, dst = env_work.popleft()
            fill(src, dst, False)
    return out


def copy_value(value, strict: bool = True, local: bool = False):
    return copy_values([(value, strict)], local=local)[0]


# ---------------------------------------------------------------------------
# Runtime records
# ---------------------------------------------------------------------------


class HandleTables:
    """The run's open files and sockets, shared by every VM."""

    def __init__(self):
        self.files: dict = {}
        self.sockets: dict = {}
        self.next_file = 1
        self.next_socket = 1
        self.lock = threading.Lock()


class Job:
    __slots__ = (
        "id", "callee", "bound", "methods", "decorators", "hook_types", "hook_params", "fn_items", "env",
        "reply", "promise", "pool",
    )

    def __init__(self, callee, bound, methods, decorators, hook_types, hook_params, fn_items, env):
        self.id = None
        self.callee = callee
        self.bound = bound
        self.methods = methods
        self.decorators = decorators
        self.hook_types = hook_types
        self.hook_params = hook_params
        self.fn_items = fn_items
        self.env = env
        self.reply = None  # the submitting VM's VmThreads
        self.promise = None
        self.pool = None


class Pool:
    def __init__(self, rt, pool_id: int, name: str, workers: int, capacity):
        self.id = pool_id
        self.name = name
        self.workers = workers
        self.capacity = capacity
        self.jobs: collections.deque = collections.deque()
        self.running = 0
        self.running_set: set = set()
        self.closed = False
        self.alive = 0
        self.joiners: list = []  # (vt, promise)
        self.cv = threading.Condition(rt.lock)


class _LockState:
    __slots__ = ("owner", "waiters")

    def __init__(self):
        self.owner = None  # (task id, vt)
        self.waiters: collections.deque = collections.deque()  # (task id, vt, promise)


class _Semaphore:
    __slots__ = ("permits", "available", "waiters")

    def __init__(self, permits: int):
        self.permits = permits
        self.available = permits
        self.waiters: collections.deque = collections.deque()  # (vt, promise)


class _Channel:
    __slots__ = ("queue", "capacity", "closed", "receivers", "senders")

    def __init__(self, capacity):
        self.queue: collections.deque = collections.deque()
        self.capacity = capacity
        self.closed = False
        self.receivers: collections.deque = collections.deque()  # (vt, promise)
        self.senders: collections.deque = collections.deque()  # (vt, promise, copy)


class _StdinReader:
    """One reader thread per run: requests are `(done queue, promise)`,
    answered in order."""

    def __init__(self):
        self.requests: queue.Queue | None = None

    def request(self, done, promise) -> None:
        if self.requests is None:
            self.requests = queue.Queue()
            threading.Thread(target=self._worker, args=(sys.stdin, self.requests), daemon=True).start()
        self.requests.put((done, promise))

    @staticmethod
    def _worker(stream, requests: queue.Queue) -> None:
        while True:
            request = requests.get()
            if request is None:
                return
            done, promise = request
            try:
                line = stream.readline()
            except (OSError, ValueError):
                line = ""
            if line == "":
                done.put((promise, "line", None))
                continue
            if line.endswith("\n"):
                line = line[:-1]
                if line.endswith("\r"):
                    line = line[:-1]
            done.put((promise, "line", line))

    def stop(self) -> None:
        if self.requests is not None:
            self.requests.put(None)


class LineBuffer:
    """A VM's stdout line buffer (docs/contracts/M44_threads.md #6.3): only
    whole lines reach the shared stdout, except at flush points."""

    __slots__ = ("vt", "pieces")

    def __init__(self, vt):
        self.vt = vt
        self.pieces: list = []

    def write(self, text: str) -> int:
        if "\n" not in text:
            self.pieces.append(text)
            return len(text)
        self.pieces.append(text)
        data = "".join(self.pieces)
        cut = data.rindex("\n") + 1
        self.pieces = [data[cut:]] if cut < len(data) else []
        self._hand_over(data[:cut])
        return len(text)

    def flush(self) -> None:
        if self.pieces:
            data = "".join(self.pieces)
            self.pieces = []
            self._hand_over(data)
        sys.stdout.flush()

    def _hand_over(self, text: str) -> None:
        rt = self.vt.rt
        with rt.stdout_lock:
            if rt.stopped and self.vt.pool is not None:
                raise Abandoned()
            sys.stdout.write(text)


class VmThreads:
    """One VM's view of the runtime: its id, done queue, line buffer and
    runtime waits, and the opcode helpers for shared variables."""

    def __init__(self, rt, vm_id: int, io, pool):
        self.rt = rt
        self.vm_id = vm_id
        self.io = io
        self.done: queue.Queue = io.done if io is not None else queue.Queue()
        self.pool = pool
        self.dead = False
        self.make_job = None
        self.blocked = False
        self.internal = 0
        # id(promise) -> (promise, kind, info), in creation order: this VM's
        # runtime waits (kind "lock" | "sem" | "recv" | "send" | "join" | "job").
        self.waits: dict = {}
        self.out = LineBuffer(self)
        # the job this VM runs (None in the main VM), and the tasks with an
        # await edge recorded in the wait-for graph
        self.job_id = None
        self.edge_tasks: set = set()

    # -- pending entries --------------------------------------------------

    def add_wait(self, promise, kind: str, info=None) -> None:
        self.waits[id(promise)] = (promise, kind, info)
        self.io.pending += 1
        self.internal += 1

    def take_wait(self, promise):
        entry = self.waits.pop(id(promise), None)
        if entry is not None:
            self.io.pending -= 1
            self.internal -= 1
        return entry

    # -- shared variables (docs/contracts/M44_threads.md #6.4) --------------

    def get(self, task, k: int, mode: int):
        held = task.held.get(k)
        if mode == 1:
            if held is None:
                raise MahRuntimeError("sharedget without holding the lock", kind="Internal")
            return held[0]
        if held is not None:
            return copy_value(held[0], local=True)
        rt = self.rt
        with rt.lock:
            stored = rt.shared.get(k, NONE_VALUE)
        return copy_value(stored, local=True)

    def set(self, task, k: int, value) -> None:
        held = task.held.get(k)
        if held is None:
            raise MahRuntimeError("sharedset without holding the lock", kind="Internal")
        held[0] = value

    def lock(self, task, k: int, name: str) -> PromiseInstance:
        return self.rt.acquire(self, task, k, name)

    def unlock(self, task, k: int, name: str, mode: int) -> None:
        if mode == 1:
            held = task.held.get(k)
            if held is not None:
                held[2] = held[1]
            return
        self.rt.release(task, k, name)

    def poll(self) -> None:
        rt = self.rt
        if self.pool is None:
            if rt.exit_code is not None:
                from .code_interpreter import ProgramExit

                raise ProgramExit(rt.exit_code)
        elif rt.stopped:
            raise Abandoned()


# ---------------------------------------------------------------------------
# The runtime
# ---------------------------------------------------------------------------

_STACK_SIZE_SET = [False]


class ThreadRuntime:
    def __init__(self, linked, args=()):
        self.linked = linked
        self.args = list(args)
        self.lock = threading.Lock()
        self.stdout_lock = threading.Lock()
        self.shared: dict = {}
        self.locks: dict = {}
        self.waiting_on: dict = {}
        self.awaiting: dict = {}
        self.job_roots: dict = {}
        self.tracking = False
        self.threads: dict = {}
        self.next_thread = 1
        self.next_job = 1
        self.semaphores: dict = {}
        self.next_semaphore = 1
        self.channels: dict = {}
        self.next_channel = 1
        self.vms: dict = {}
        self.blocked_count = 0
        self.tables = HandleTables()
        self.stdin = _StdinReader()
        self.started = time.monotonic()
        self.decimal_context = decimal.getcontext().copy()
        self.vm_ids = itertools.count(1)
        self.active = False
        self.stopped = False
        self.exit_code = None
        self.root_done = None

    # -- posting ------------------------------------------------------------

    def post(self, vt, item) -> None:
        """Wake `vt` with `item` (with `self.lock` held)."""
        if vt.blocked:
            vt.blocked = False
            self.blocked_count -= 1
        vt.done.put(item)

    # -- the wait-for graph (#6.4) -------------------------------------------

    def producer_edges(self, producer) -> list:
        """The (task id, strong) nodes a producer leads to."""
        kind, ident = producer
        if kind == "task":
            return [(ident, False)]
        if kind == "job":
            root = self.job_roots.get(ident)
            return [] if root is None else [(root, True)]
        pool = self.threads.get(ident)
        if pool is None:
            return []
        return [(self.job_roots[j], True) for j in pool.running_set if j in self.job_roots]

    def _out_edges(self, node) -> list:
        edges = []
        k = self.waiting_on.get(node)
        if k is not None:
            state = self.locks.get(k)
            if state is not None and state.owner is not None:
                edges.append((state.owner[0], True))
        producer = self.awaiting.get(node)
        if producer is not None:
            edges.extend(self.producer_edges(producer))
        return edges

    def cycle_from(self, edges: list, target, strong: bool) -> bool:
        """Whether following `edges` (and the graph from there) reaches the
        task `target` along a path that, with the new edge (strong or not),
        contains a strong edge."""
        stack = [(node, strong or s) for node, s in edges]
        seen = set()
        while stack:
            node, has_strong = stack.pop()
            if node == target:
                if has_strong:
                    return True
                continue
            if (node, has_strong) in seen:
                continue
            seen.add((node, has_strong))
            for nxt, s in self._out_edges(node):
                stack.append((nxt, has_strong or s))
        return False

    def cycle(self, start, target, strong: bool = True) -> bool:
        return self.cycle_from([(start, False)], target, strong)

    def await_check(self, vt, task, promise) -> None:
        """Called (only when tracking) before `task` suspends on `promise`:
        a deadlock throws in the task; else the await edge is recorded."""
        producer = promise.producer
        with self.lock:
            strong = producer[0] != "task"
            if self.cycle_from(self.producer_edges(producer), task.id, strong):
                raise MahThrow(thread_error("deadlock", AWAIT_DEADLOCK_MESSAGE))
            self.awaiting[task.id] = producer
            vt.edge_tasks.add(task.id)
            task.awaits_edge = True

    def clear_await(self, vt, task) -> None:
        with self.lock:
            self.awaiting.pop(task.id, None)
            vt.edge_tasks.discard(task.id)
        task.awaits_edge = False

    # -- locks (#6.4) ---------------------------------------------------------

    def acquire(self, vt, task, k: int, name: str) -> PromiseInstance:
        held = task.held.get(k)
        if held is not None:
            held[1] += 1
            return _settled()
        with self.lock:
            self.tracking = True
            state = self.locks.get(k)
            if state is None:
                state = self.locks[k] = _LockState()
            if state.owner is None:
                state.owner = (task.id, vt)
                task.held[k] = [copy_value(self.shared.get(k, NONE_VALUE), local=True), 1, 0]
                return _settled()
            if self.cycle(state.owner[0], task.id, True):
                return _failed(thread_error("deadlock", f"deadlock: waiting for '{name}' would never end"))
            promise = PromiseInstance()
            state.waiters.append((task.id, vt, promise))
            self.waiting_on[task.id] = k
            vt.add_wait(promise, "lock", (task, k))
            return promise

    def release(self, task, k: int, name: str) -> None:
        entry = task.held.get(k)
        if entry is None:
            raise MahRuntimeError("sharedunlock without holding the lock", kind="Internal")
        leaving_by_throw = entry[2] == entry[1]
        if leaving_by_throw:
            entry[2] = 0
        entry[1] -= 1
        if entry[1] > 0:
            return
        del task.held[k]
        refused = False
        try:
            value = copy_value(entry[0], strict=True)
        except NotSendable:
            refused = True
        with self.lock:
            if not refused:
                self.shared[k] = value
            self.grant_next(k)
        if refused and not leaving_by_throw:
            raise MahThrow(thread_error("not_sendable", f"shared variable '{name}' can't hold a Promise"))

    def grant_next(self, k: int) -> None:
        state = self.locks.get(k)
        if state is None:
            return
        state.owner = None
        while state.waiters:
            task_id, vt, promise = state.waiters.popleft()
            self.waiting_on.pop(task_id, None)
            state.owner = (task_id, vt)
            self.post(vt, (promise, "lock", (k, copy_value(self.shared.get(k, NONE_VALUE), local=True))))
            return

    # -- pools (#2.3, #6.5) ----------------------------------------------------

    def _pool(self, ident):
        pool = self.threads.get(_ident_key(ident))
        if pool is None:
            raise MahRuntimeError(f"thread: no such thread {_num(ident)}", kind="ArgumentError")
        return pool

    def spawn(self, name, workers: int, capacity) -> list:
        if not _STACK_SIZE_SET[0]:
            _STACK_SIZE_SET[0] = True
            try:
                threading.stack_size(64 * 1024 * 1024)
            except (ValueError, RuntimeError):
                pass
        with self.lock:
            self.tracking = True
            self.active = True
            pool_id = self.next_thread
            self.next_thread += 1
            pool = Pool(self, pool_id, name if name is not None else f"thread-{pool_id}", workers, capacity)
            self.threads[pool_id] = pool
            pool.alive = workers
        for i in range(workers):
            threading.Thread(target=_worker, args=(self, pool), daemon=True, name=f"mah-{pool.name}-{i}").start()
        return [Decimal(pool_id), pool.name]

    def submit(self, vt, pool, job) -> PromiseInstance:
        promise = PromiseInstance()
        with self.lock:
            if pool.closed:
                raise MahThrow(thread_error("closed", f"thread '{pool.name}' is closed"))
            if pool.capacity is not None and len(pool.jobs) + pool.running >= pool.workers + pool.capacity:
                limit = pool.workers + pool.capacity
                raise MahThrow(
                    thread_error("full", f"thread '{pool.name}' is full ({limit} jobs queued or running)")
                )
            job.id = self.next_job
            self.next_job += 1
            job.reply = vt
            job.promise = promise
            job.pool = pool
            promise.producer = ("job", job.id)
            pool.jobs.append(job)
            vt.add_wait(promise, "job", job.id)
            pool.cv.notify()
        return promise

    def close(self, pool, cancel: bool) -> None:
        with self.lock:
            pool.closed = True
            if cancel:
                while pool.jobs:
                    job = pool.jobs.popleft()
                    error = thread_error("cancelled", f"thread '{pool.name}' was closed before this job started")
                    self.post(job.reply, (job.promise, "job", (False, error)))
            pool.cv.notify_all()
            self.check_quiescence()

    def join(self, vt, pool) -> PromiseInstance:
        if vt.pool is pool:
            raise MahThrow(thread_error("deadlock", "deadlock: a thread can't join itself"))
        self.close(pool, False)
        with self.lock:
            if pool.alive == 0:
                return _settled()
            promise = PromiseInstance()
            promise.producer = ("join", pool.id)
            pool.joiners.append((vt, promise))
            vt.add_wait(promise, "join", pool.id)
            return promise

    # -- semaphores (#6.7) ---------------------------------------------------

    def _semaphore(self, ident):
        sem = self.semaphores.get(_ident_key(ident))
        if sem is None:
            raise MahRuntimeError(f"thread: no such semaphore {_num(ident)}", kind="ArgumentError")
        return sem

    def semaphore_new(self, permits: int) -> Decimal:
        with self.lock:
            sid = self.next_semaphore
            self.next_semaphore += 1
            self.semaphores[sid] = _Semaphore(permits)
            return Decimal(sid)

    def semaphore_acquire(self, vt, ident) -> PromiseInstance:
        with self.lock:
            sem = self._semaphore(ident)
            if sem.available > 0 and not sem.waiters:
                sem.available -= 1
                return _settled()
            promise = PromiseInstance()
            sem.waiters.append((vt, promise))
            vt.add_wait(promise, "sem", int(ident))
            return promise

    def semaphore_try_acquire(self, ident) -> bool:
        with self.lock:
            sem = self._semaphore(ident)
            if sem.available > 0 and not sem.waiters:
                sem.available -= 1
                return True
            return False

    def semaphore_release(self, ident, check: bool = True) -> None:
        with self.lock:
            sem = self._semaphore(ident)
            self._semaphore_release(sem, int(ident), check)

    def _semaphore_release(self, sem, sid: int, check: bool) -> None:
        if sem.waiters:
            vt, promise = sem.waiters.popleft()
            self.post(vt, (promise, "sem", sid))
        elif sem.available == sem.permits:
            if check:
                raise MahThrow(
                    thread_error(
                        "over_release", f"release without a matching acquire (all {sem.permits} permits are free)"
                    )
                )
        else:
            sem.available += 1

    def semaphore_available(self, ident) -> Decimal:
        with self.lock:
            return Decimal(self._semaphore(ident).available)

    # -- channels (#6.8) -----------------------------------------------------

    def _channel(self, ident):
        ch = self.channels.get(_ident_key(ident))
        if ch is None:
            raise MahRuntimeError(f"thread: no such channel {_num(ident)}", kind="ArgumentError")
        return ch

    def channel_new(self, capacity) -> Decimal:
        with self.lock:
            cid = self.next_channel
            self.next_channel += 1
            self.channels[cid] = _Channel(capacity)
            return Decimal(cid)

    def channel_send(self, vt, ident, value) -> PromiseInstance:
        with self.lock:
            self._channel(ident)
        try:
            message = copy_value(value, strict=True)
        except NotSendable:
            raise MahThrow(thread_error("not_sendable", NOT_SENDABLE_MESSAGE)) from None
        with self.lock:
            ch = self._channel(ident)
            if ch.closed:
                raise MahThrow(thread_error("closed", "the channel is closed"))
            if ch.receivers:
                r_vt, r_promise = ch.receivers.popleft()
                self.post(r_vt, (r_promise, "recv", (int(ident), message)))
                return _settled()
            if ch.capacity is None or len(ch.queue) < ch.capacity:
                ch.queue.append(message)
                return _settled()
            promise = PromiseInstance()
            ch.senders.append((vt, promise, message))
            vt.add_wait(promise, "send", int(ident))
            return promise

    def _take_message(self, ch):
        """recv's non-waiting branches: (True, message) or (False, None)."""
        if ch.queue:
            message = ch.queue.popleft()
            if ch.senders:
                s_vt, s_promise, s_message = ch.senders.popleft()
                ch.queue.append(s_message)
                self.post(s_vt, (s_promise, "settle", (True, NONE_VALUE)))
            return True, message
        if ch.senders:
            s_vt, s_promise, s_message = ch.senders.popleft()
            self.post(s_vt, (s_promise, "settle", (True, NONE_VALUE)))
            return True, s_message
        return False, None

    def channel_recv(self, vt, ident) -> PromiseInstance:
        with self.lock:
            ch = self._channel(ident)
            got, message = self._take_message(ch)
            if got:
                return _settled(message)
            if ch.closed:
                return _failed(thread_error("closed", "the channel is closed"))
            promise = PromiseInstance()
            ch.receivers.append((vt, promise))
            vt.add_wait(promise, "recv", int(ident))
            return promise

    def channel_try_recv(self, ident):
        with self.lock:
            ch = self._channel(ident)
            got, message = self._take_message(ch)
        if got:
            return EnumInstance("Option", "some", {"value": message})
        return NONE_VALUE

    def channel_close(self, ident) -> None:
        with self.lock:
            ch = self._channel(ident)
            if ch.closed:
                return
            ch.closed = True
            while ch.receivers:
                r_vt, r_promise = ch.receivers.popleft()
                self.post(r_vt, (r_promise, "settle", (False, thread_error("closed", "the channel is closed"))))
            while ch.senders:
                s_vt, s_promise, _m = ch.senders.popleft()
                self.post(s_vt, (s_promise, "settle", (False, thread_error("closed", "the channel is closed"))))

    def channel_len(self, ident) -> Decimal:
        with self.lock:
            return Decimal(len(self._channel(ident).queue))

    def channel_closed(self, ident) -> bool:
        with self.lock:
            return self._channel(ident).closed

    # -- waiters, quiescence (#6.6, #6.10) -------------------------------------

    def _remove_waiter(self, promise) -> None:
        """Drop `promise`'s runtime waiter, wherever it is (`self.lock` held)."""
        for state in self.locks.values():
            for w in list(state.waiters):
                if w[2] is promise:
                    state.waiters.remove(w)
                    self.waiting_on.pop(w[0], None)
        for sem in self.semaphores.values():
            for w in list(sem.waiters):
                if w[1] is promise:
                    sem.waiters.remove(w)
        for ch in self.channels.values():
            for w in list(ch.receivers):
                if w[1] is promise:
                    ch.receivers.remove(w)
            for w in list(ch.senders):
                if w[1] is promise:
                    ch.senders.remove(w)
        for pool in self.threads.values():
            pool.joiners = [w for w in pool.joiners if w[1] is not promise]

    def take_stuck(self, vt) -> list:
        """The main VM's `stuck` completion: remove every internal wait of
        `vt` (runtime waiter and pending entry); returns their Promises, to
        be failed by the caller outside the lock."""
        with self.lock:
            promises = []
            for promise, _kind, _info in list(vt.waits.values()):
                self._remove_waiter(promise)
                vt.take_wait(promise)
                promises.append(promise)
            return promises

    def about_to_block(self, vt) -> None:
        with self.lock:
            if vt.io.pending == vt.internal and vt.done.empty() and not vt.blocked:
                vt.blocked = True
                self.blocked_count += 1
                self.check_quiescence()

    def check_quiescence(self) -> None:
        live = len(self.vms)
        # A worker that has a job to start, or (closed pool) is about to
        # exit and settle its joiners, can still make progress.
        starting = any(
            (p.jobs or p.closed) and p.running < p.alive for p in self.threads.values()
        )
        if live and self.blocked_count == live and not starting:
            main = self.vms.get(0)
            if main is not None:
                self.post(main, (None, "stuck", None))

    def forget_vm(self, vt) -> None:
        with self.lock:
            # This VM's lock waiters go first: else releasing a lock this VM
            # owns could grant it to another of its own waiting tasks, and
            # the lock would stay owned by a VM that no longer exists.
            for state in self.locks.values():
                for w in list(state.waiters):
                    if w[1] is vt:
                        state.waiters.remove(w)
                        self.waiting_on.pop(w[0], None)
            for k, state in self.locks.items():
                if state.owner is not None and state.owner[1] is vt:
                    self.grant_next(k)
            for sem in self.semaphores.values():
                sem.waiters = collections.deque(w for w in sem.waiters if w[0] is not vt)
            for ch in self.channels.values():
                ch.receivers = collections.deque(w for w in ch.receivers if w[0] is not vt)
                ch.senders = collections.deque(w for w in ch.senders if w[0] is not vt)
            for pool in self.threads.values():
                pool.joiners = [w for w in pool.joiners if w[0] is not vt]
            for tid in vt.edge_tasks:
                self.awaiting.pop(tid, None)
            vt.edge_tasks.clear()
            if vt.job_id is not None:
                self.job_roots.pop(vt.job_id, None)
            # The VM stays in `vms` (live, not blocked) until its worker
            # posts the job's reply (`finish_vm`): were it removed here, a
            # quiescence check run in between by another thread would see
            # every remaining VM blocked and fail a wait (this job's reply
            # among them) with `stuck` although the reply is on its way.
            if vt.blocked:
                vt.blocked = False
                self.blocked_count -= 1
        # Completions that arrived for this VM: give permits and messages back.
        while True:
            try:
                promise, what, payload = vt.done.get_nowait()
            except queue.Empty:
                break
            if what == "sem":
                with self.lock:
                    sem = self.semaphores.get(payload)
                    if sem is not None:
                        self._semaphore_release(sem, payload, False)
            elif what == "recv":
                cid, message = payload
                with self.lock:
                    ch = self.channels.get(cid)
                    if ch is not None:
                        if ch.receivers:
                            r_vt, r_promise = ch.receivers.popleft()
                            self.post(r_vt, (r_promise, "recv", (cid, message)))
                        else:
                            ch.queue.appendleft(message)
            # anything else (a lock granted to this VM was released above,
            # a job reply, a settle) is dropped
        vt.dead = True

    def finish_vm(self, vt) -> None:
        """The worker's last step for a job VM (`self.lock` held, right
        before the reply is posted): the VM leaves `vms`."""
        if self.vms.pop(vt.vm_id, None) is not None and vt.blocked:
            vt.blocked = False
            self.blocked_count -= 1

    # -- exit and shutdown -------------------------------------------------------

    def claim_exit(self, code: int) -> int:
        with self.lock:
            if self.exit_code is None:
                self.exit_code = code
            return self.exit_code

    def request_exit(self, code: int) -> None:
        """A job called `process.exit`: the first exit wins."""
        with self.stdout_lock:
            with self.lock:
                if self.exit_code is None:
                    self.exit_code = code
            self.stopped = True
        with self.lock:
            if self.root_done is not None:
                self.root_done.put((None, "exit", self.exit_code))

    def shutdown(self) -> None:
        with self.stdout_lock:
            self.stopped = True
        with self.lock:
            for pool in self.threads.values():
                pool.closed = True
                pool.jobs.clear()
                pool.cv.notify_all()
            for vt in list(self.vms.values()):
                if vt.pool is not None:
                    self.post(vt, (None, "abandon", None))
        self.stdin.stop()


def _ident_key(value):
    """A handle id as a dict key, or None when it can't be one."""
    if isinstance(value, Decimal) and not isinstance(value, bool) and value == value.to_integral_value():
        return int(value)
    return None


def _num(value) -> str:
    if isinstance(value, Decimal) and not isinstance(value, bool):
        if value == value.to_integral_value():
            text = format(value, "f").partition(".")[0]
            return "0" if text == "-0" else text
        return format(value.normalize(), "f")
    return str(value)


# ---------------------------------------------------------------------------
# Workers (#6.5)
# ---------------------------------------------------------------------------


def _worker(rt: ThreadRuntime, pool: Pool) -> None:
    from .code_interpreter import ProgramExit, run_job

    decimal.setcontext(rt.decimal_context.copy())
    while True:
        with rt.lock:
            while not pool.jobs and not pool.closed and not rt.stopped:
                pool.cv.wait()
            if rt.stopped:
                pool.alive -= 1
                return
            if not pool.jobs:
                pool.alive -= 1
                if pool.alive == 0:
                    for vt, promise in pool.joiners:
                        rt.post(vt, (promise, "settle", (True, NONE_VALUE)))
                    pool.joiners = []
                rt.check_quiescence()
                return
            job = pool.jobs.popleft()
            pool.running += 1
            pool.running_set.add(job.id)
            vt = VmThreads(rt, next(rt.vm_ids), None, pool)
            vt.job_id = job.id
            rt.vms[vt.vm_id] = vt
        outcome = None
        try:
            outcome = run_job(rt.linked, rt, pool, job, vt)
        except ProgramExit as exit_:
            try:
                vt.out.flush()
            except Abandoned:
                pass
            rt.request_exit(exit_.code)
        except Abandoned:
            pass
        except Exception as exc:  # noqa: BLE001 -- a host failure fails the job
            outcome = (False, EnumInstance("RuntimeError", "Internal", {"message": str(exc)}))
        with rt.lock:
            pool.running -= 1
            pool.running_set.discard(job.id)
            rt.finish_vm(vt)
            if outcome is not None and not rt.stopped:
                rt.post(job.reply, (job.promise, "job", outcome))
            rt.check_quiescence()
        if outcome is None:
            # exited or abandoned: this worker stops too
            with rt.lock:
                pool.alive -= 1
            return


__all__ = [
    "Abandoned",
    "HandleTables",
    "Job",
    "LineBuffer",
    "NotSendable",
    "ThreadRuntime",
    "VmThreads",
    "copy_value",
    "copy_values",
    "thread_error",
]
