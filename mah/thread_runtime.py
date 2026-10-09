"""M44 (docs/contracts/M44_threads.md): the process-wide thread runtime of
the Python VM -- jobs on other threads, shared variables, semaphores,
channels, the wait-for graph and the quiescence rule. M45
(docs/contracts/M45_atomic.md): the shared variables' store with versions,
`atomic { }` transactions (TL2-style validation under the one runtime
mutex), `retry` waits and the exclusivity token.

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
    Tx,
    TxEntry,
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
AWAIT_DEADLOCK_MESSAGE = "deadlock: this await would never end (it waits, through threads, for itself)"

# -- M45: transactions (docs/contracts/M45_atomic.md #5, #6) ------------------

# A transaction that failed this many attempts runs its next one exclusive.
ATOMIC_ATTEMPTS = 8
RETRY_NO_READS_MESSAGE = "retry can never wake up: this transaction read no shared variable"
# The natives a task in a transaction may not call (#5.2) -- exactly the
# Rust VM's `ATOMIC_REFUSED_NATIVES` (runtime/src/vm/thread.rs).
ATOMIC_REFUSED_NATIVES: frozenset = frozenset(
    {
        "io.print", "io.write", "io.input", "io.read_line",
        "time.sleep_async", "time.cancel",
        "promise.resolve", "promise.fail",
        "fs.read_text", "fs.write_text", "fs.append_text", "fs.info", "fs.list_dir", "fs.mkdir", "fs.remove",
        "fs.rename", "fs.copy", "fs.temp_dir", "fs.open", "fs.read_line", "fs.read_all", "fs.write", "fs.close",
        "fs.read_bytes", "fs.write_bytes", "fs.append_bytes", "fs.file_read_bytes", "fs.file_write_bytes",
        "process.exit", "process.run", "process.env_set", "process.env_remove",
        "socket.connect", "socket.listen", "socket.accept", "socket.send", "socket.recv", "socket.shutdown",
        "socket.close", "socket.start_tls", "socket.tls_server_config", "socket.start_tls_server",
        "thread.spawn", "thread.submit", "thread.close", "thread.join", "thread.semaphore_acquire",
        "thread.semaphore_try_acquire", "thread.semaphore_release", "thread.channel_send", "thread.channel_recv",
        "thread.channel_try_recv", "thread.channel_close",
    }
)
# Debug counters for tests (never visible to Mah programs).
TX_STATS = {"conflicts": 0, "exclusive": 0}


def in_atomic(name: str) -> MahThrow:
    return MahThrow(
        thread_error("in_atomic", f"'{name}' can't run inside 'atomic {{ }}': its body may run more than once")
    )


class TxRestart(BaseException):
    """Abandon the current attempt of the task's transaction: `kind`
    "conflict" (run it again) or "retry" (wait for a change first). A
    BaseException, so no `except Exception` swallows it on its way to the
    owner's step loop."""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


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


def _is_num(v) -> bool:
    return isinstance(v, (Decimal, int, float)) and not isinstance(v, bool)


def same_copy(a, b) -> bool:
    """M45 #6.6: whether two strict copies are the same value graph (decides
    whether an exposed, unassigned working value changed). Closures and
    frames never count as the same."""
    left: dict = {}
    right: dict = {}
    stack = [(a, b)]
    while stack:
        x, y = stack.pop()
        if x is NONE_VALUE or x is None or y is NONE_VALUE or y is None:
            if not ((x is NONE_VALUE or x is None) and (y is NONE_VALUE or y is None)):
                return False
            continue
        if isinstance(x, bool) or isinstance(y, bool):
            if not (isinstance(x, bool) and isinstance(y, bool) and x == y):
                return False
            continue
        if _is_num(x) or _is_num(y):
            if not (_is_num(x) and _is_num(y) and x == y):
                return False
            continue
        if isinstance(x, str) or isinstance(y, str):
            if not (isinstance(x, str) and isinstance(y, str) and x == y):
                return False
            continue
        if isinstance(x, TypeValue) or isinstance(y, TypeValue):
            if not (isinstance(x, TypeValue) and isinstance(y, TypeValue) and x.kind == y.kind and x.index == y.index):
                return False
            continue
        if isinstance(x, _Absent) or isinstance(y, _Absent):
            if not (isinstance(x, _Absent) and isinstance(y, _Absent)):
                return False
            continue
        if type(x) is not type(y):
            return False
        if not isinstance(x, (VectorValue, MapValue, BytesValue, StructInstance, EnumInstance)):
            return False  # a Closure, Frame (or anything else): conservatively changed
        paired_x = left.get(id(x))
        paired_y = right.get(id(y))
        if paired_x is not None or paired_y is not None:
            if paired_x == id(y) and paired_y == id(x):
                continue
            return False
        left[id(x)] = id(y)
        right[id(y)] = id(x)
        if isinstance(x, VectorValue):
            if len(x.items) != len(y.items):
                return False
            stack.extend(zip(x.items, y.items))
        elif isinstance(x, MapValue):
            if len(x.entries) != len(y.entries) or list(x.entries) != list(y.entries):
                return False
            for (kx, vx), (ky, vy) in zip(x.entries.values(), y.entries.values()):
                stack.append((kx, ky))
                stack.append((vx, vy))
        elif isinstance(x, BytesValue):
            if bytes(x.data) != bytes(y.data):
                return False
        else:
            if x.type_name != y.type_name:
                return False
            if isinstance(x, EnumInstance) and x.variant != y.variant:
                return False
            if list(x.fields) != list(y.fields):
                return False
            stack.extend(zip(x.fields.values(), y.fields.values()))
    return True


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
        # runtime waits (kind "retry" | "sem" | "recv" | "send" | "join" | "job").
        self.waits: dict = {}
        self.out = LineBuffer(self)
        # the job this VM runs (None in the main VM), and the tasks with an
        # await edge recorded in the wait-for graph
        self.job_id = None
        self.edge_tasks: set = set()
        # M45: called while blocked in an exclusivity wait (set by the VM:
        # test deadline, exit requests, abandonment).
        self.wait_check = lambda: None

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

    # -- shared variables and transactions (docs/contracts/M45_atomic.md #6.3)

    def get(self, task, k: int, name: str, mode: int):
        tx = task.tx
        rt = self.rt
        if tx is None:
            if mode == 1:
                raise MahRuntimeError("sharedget in working mode outside a transaction", kind="Internal")
            value, _version = rt.read_shared(k)
            return copy_value(value, local=True)
        entry = tx.entries.get(k)
        if entry is None:
            value, version = rt.read_shared(k)
            if version > tx.rv:
                raise TxRestart("conflict")
            tx.reads[k] = version
            entry = tx.entries[k] = TxEntry(name, value, copy_value(value, local=True))
        if mode == 1:
            entry.exposed = True
            return entry.working
        return copy_value(entry.working, local=True)

    def set(self, task, k: int, name: str, value) -> None:
        tx = task.tx
        if tx is not None:
            working = copy_value(value, local=True)
            entry = tx.entries.get(k)
            if entry is None:
                tx.entries[k] = TxEntry(name, None, working, assigned=True)
            else:
                entry.working = working
                entry.assigned = True
            return
        try:
            stored = copy_value(value, strict=True)
        except NotSendable:
            raise MahThrow(thread_error("not_sendable", f"shared variable '{name}' can't hold a Promise")) from None
        self.rt.write_shared(self, k, stored)

    def _start(self, task, tx) -> None:
        """`tx_start`, guarded: any error but a restart ends the transaction."""
        try:
            self.rt.tx_start(self, tx)
        except TxRestart:
            raise
        except BaseException:
            self.rt.end_attempt(tx)
            task.tx = None
            raise

    def begin(self, task, pc: int) -> None:
        tx = task.tx
        if tx is None:
            tx = Tx(task, task.implicit, (pc, task.current_frame, len(task.return_stack), len(task.defer_stack)))
            task.tx = tx
            self._start(task, tx)
        elif tx.depth > 0:
            tx.depth += 1
        else:
            tx.depth = 1
            if tx.attempts >= ATOMIC_ATTEMPTS:
                tx.irrevocable = True
            self._start(task, tx)

    def end(self, task) -> None:
        tx = task.tx
        if tx is None:
            raise MahRuntimeError("atomicend outside a transaction", kind="Internal")
        if tx.depth > 1:
            tx.depth -= 1
            return
        rt = self.rt
        try:
            publish = []
            refused = None
            for k, entry in tx.entries.items():
                if not (entry.assigned or entry.exposed):
                    continue
                try:
                    stored = copy_value(entry.working, strict=True)
                except NotSendable:
                    refused = entry.name
                    break
                if not entry.assigned and same_copy(stored, entry.base):
                    continue
                publish.append((k, stored))
            if refused is not None:
                rt.end_attempt(tx)
                task.tx = None
                raise MahThrow(thread_error("not_sendable", f"shared variable '{refused}' can't hold a Promise"))
            if not rt.tx_commit(self, tx, publish):
                raise TxRestart("conflict")
            task.tx = None
        except TxRestart:
            raise
        except BaseException:
            rt.end_attempt(tx)
            if task.tx is tx:
                task.tx = None
            raise

    def abort(self, task) -> None:
        tx = task.tx
        if tx is None:
            raise MahRuntimeError("atomicabort outside a transaction", kind="Internal")
        if tx.depth > 1:
            tx.depth -= 1
            return
        self.rt.end_attempt(tx)
        task.tx = None

    def retry(self, task) -> None:
        tx = task.tx
        if tx is None:
            raise MahRuntimeError("retry outside a transaction", kind="Internal")
        if tx.implicit is not None:
            raise MahRuntimeError(
                f"'{tx.implicit}' cannot suspend (it used 'retry') when called implicitly by the runtime"
            )
        if not tx.reads:
            raise MahThrow(thread_error("stuck", RETRY_NO_READS_MESSAGE))
        raise TxRestart("retry")

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
        # M45 (#6.1): index -> (stored strict copy, version); the version
        # clock; retry waits (key = id(promise)) and the indices they watch;
        # the exclusivity token.
        self.shared: dict = {}
        self.clock = 0
        self.watchers: dict = {}
        self.retry_waits: dict = {}
        self.excl_owner = None  # (tx serial, vm id)
        self.excl_queue: collections.deque = collections.deque()
        self.commits_waiting = 0
        self.excl_cv = threading.Condition(self.lock)
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
        # jobs whose VM is torn down but whose reply isn't posted yet: they
        # still settle a Promise, so the run isn't stuck (#6.10)
        self.finishing: set = set()
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

    def await_check(self, vt, task, promise) -> None:
        """Called (only when tracking) before `task` suspends on `promise`:
        a deadlock throws in the task; else the await edge is recorded."""
        producer = promise.producer
        with self.lock:
            strong = producer[0] != "task"
            if self.cycle_from(self.producer_edges(producer), task.id, strong):
                if producer[0] == "join":
                    # M45: std:thread's `join` awaits its Promise at once and
                    # drops it, so a join that fails here can never be
                    # awaited again: drop its waiter and pending entry, or
                    # it would keep this VM (and its job) alive forever.
                    self._remove_waiter(promise)
                    vt.take_wait(promise)
                raise MahThrow(thread_error("deadlock", AWAIT_DEADLOCK_MESSAGE))
            self.awaiting[task.id] = producer
            vt.edge_tasks.add(task.id)
            task.awaits_edge = True

    def clear_await(self, vt, task) -> None:
        with self.lock:
            self.awaiting.pop(task.id, None)
            vt.edge_tasks.discard(task.id)
        task.awaits_edge = False

    # -- shared variables and transactions (docs/contracts/M45_atomic.md #6) --

    def read_shared(self, k: int):
        with self.lock:
            return self.shared.get(k, (NONE_VALUE, 0))

    def write_shared(self, vt, k: int, stored) -> None:
        """A plain assignment outside a transaction (`stored` is a strict
        copy): a new version, waking `retry` waiters of `k`."""
        with self.lock:
            self._wait_no_excl(vt, None)
            self.clock += 1
            self.shared[k] = (stored, self.clock)
            self._wake_watchers([k])

    def tx_start(self, vt, tx) -> None:
        with self.lock:
            if tx.irrevocable:
                self._acquire_excl(vt, tx.serial)
                TX_STATS["exclusive"] += 1
            tx.rv = self.clock

    def tx_commit(self, vt, tx, publish: list) -> bool:
        with self.lock:
            if publish:
                self._wait_no_excl(vt, tx.serial)
                if not self._reads_valid(tx.reads):
                    return False
                self.clock += 1
                for k, stored in publish:
                    self.shared[k] = (stored, self.clock)
                self._wake_watchers([k for k, _stored in publish])
            self._release_excl(tx.serial)
            return True

    def end_attempt(self, tx) -> None:
        with self.lock:
            self._release_excl(tx.serial)

    def count_conflict(self) -> None:
        with self.lock:
            TX_STATS["conflicts"] += 1

    def retry_wait(self, vt, reads: dict):
        """A pending Promise settled when a variable of `reads` gets another
        version -- or None when one already has."""
        with self.lock:
            if not self._reads_valid(reads):
                return None
            promise = PromiseInstance()
            keys = list(reads)
            key = id(promise)
            vt.add_wait(promise, "retry", keys)
            self.retry_waits[key] = (vt, promise, keys)
            for k in keys:
                # a dict as an ordered set: waiters wake in the order they
                # started waiting, like the Rust VM's (vm id, pending id) order
                self.watchers.setdefault(k, {})[key] = None
            return promise

    def _reads_valid(self, reads: dict) -> bool:
        shared = self.shared
        for k, version in reads.items():
            entry = shared.get(k)
            if (entry[1] if entry is not None else 0) != version:
                return False
        return True

    def _wake_watchers(self, keys) -> None:
        for k in keys:
            for key in self.watchers.pop(k, ()):
                entry = self.retry_waits.pop(key, None)
                if entry is None:
                    continue
                vt, promise, indices = entry
                for other in indices:
                    if other != k:
                        watching = self.watchers.get(other)
                        if watching is not None:
                            watching.pop(key, None)
                self.post(vt, (promise, "settle", (True, NONE_VALUE)))

    def _drop_retry_wait(self, key) -> None:
        entry = self.retry_waits.pop(key, None)
        if entry is None:
            return
        for k in entry[2]:
            watching = self.watchers.get(k)
            if watching is not None:
                watching.pop(key, None)
                if not watching:
                    del self.watchers[k]

    def _excl_wait(self, vt) -> None:
        self.excl_cv.wait(0.05)
        vt.wait_check()

    def _acquire_excl(self, vt, serial) -> None:
        """Become the one exclusive transaction (#6.9): FIFO among the
        waiting ones, after the commits already waiting have gone through."""
        self.excl_queue.append(serial)
        try:
            while not (self.excl_owner is None and self.excl_queue[0] == serial and self.commits_waiting == 0):
                self._excl_wait(vt)
        except BaseException:
            try:
                self.excl_queue.remove(serial)
            except ValueError:
                pass
            self.excl_cv.notify_all()
            raise
        self.excl_queue.popleft()
        self.excl_owner = (serial, vt.vm_id)

    def _wait_no_excl(self, vt, serial) -> None:
        """Wait while another transaction is exclusive (a commit of `serial`,
        or a plain write when `serial` is None)."""
        if self.excl_owner is None or self.excl_owner[0] == serial:
            return
        self.commits_waiting += 1
        try:
            while self.excl_owner is not None and self.excl_owner[0] != serial:
                self._excl_wait(vt)
        finally:
            self.commits_waiting -= 1
            if self.commits_waiting == 0:
                self.excl_cv.notify_all()

    def _release_excl(self, serial) -> None:
        if self.excl_owner is not None and self.excl_owner[0] == serial:
            self.excl_owner = None
            self.excl_cv.notify_all()

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
        self._drop_retry_wait(id(promise))
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
        # exit and settle its joiners, can still make progress -- and so
        # can one whose job VM is gone but whose reply isn't posted yet.
        starting = self.finishing or any(
            (p.jobs or p.closed) and p.running < p.alive for p in self.threads.values()
        )
        if live and self.blocked_count == live and not starting:
            main = self.vms.get(0)
            if main is not None:
                self.post(main, (None, "stuck", None))

    def forget_vm(self, vt) -> None:
        with self.lock:
            # M45 (#6.8): this VM's retry waits and its exclusivity token
            for key in [key for key, entry in self.retry_waits.items() if entry[0] is vt]:
                self._drop_retry_wait(key)
            if self.excl_owner is not None and self.excl_owner[1] == vt.vm_id:
                self._release_excl(self.excl_owner[0])
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
                self.finishing.add(vt.job_id)
            if self.vms.pop(vt.vm_id, None) is not None and vt.blocked:
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
            # anything else (a job reply, a settle) is dropped
        vt.dead = True

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
            rt.finishing.discard(job.id)
            if outcome is not None and not rt.stopped:
                rt.post(job.reply, (job.promise, "job", outcome))
            rt.check_quiescence()
        if outcome is None:
            # exited or abandoned: this worker stops too
            with rt.lock:
                pool.alive -= 1
            return


__all__ = [
    "ATOMIC_ATTEMPTS",
    "ATOMIC_REFUSED_NATIVES",
    "Abandoned",
    "HandleTables",
    "Job",
    "LineBuffer",
    "NotSendable",
    "ThreadRuntime",
    "TxRestart",
    "VmThreads",
    "copy_value",
    "copy_values",
    "in_atomic",
    "same_copy",
    "thread_error",
]
