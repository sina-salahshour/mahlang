"""M44 (docs/contracts/M44_threads.md #7.2): the `thread.*` natives behind
std:thread. Each delegates to the calling VM's `VmThreads` (`ctx.thread`)
and the process-wide `ThreadRuntime` (`ctx.thread.rt`, mah/thread_runtime.py).
The std:thread wrapper validates argument values before calling these."""

from __future__ import annotations

import os
from decimal import Decimal

from .runtime_values import (
    NONE_VALUE,
    Closure,
    MahRuntimeError,
    MahThrow,
    StructInstance,
    VectorValue,
    display_name,
    type_name_of,
)
from .thread_runtime import NOT_SENDABLE_MESSAGE, NotSendable, thread_error


def _shown_type(value) -> str:
    return "None" if value is NONE_VALUE else display_name(type_name_of(value))


def _whole(value) -> int:
    return int(value)


def _spawn(ctx, args):
    name, workers, capacity = args
    result = ctx.thread.rt.spawn(
        None if name is NONE_VALUE else name,
        _whole(workers),
        None if capacity is NONE_VALUE else _whole(capacity),
    )
    return VectorValue(result)


def _submit(ctx, args):
    thread, fn, values = args
    if not (
        isinstance(thread, StructInstance)
        and display_name(thread.type_name) == "Thread"
        and isinstance(thread.fields.get("id"), Decimal)
        and not isinstance(thread.fields.get("id"), bool)
    ):
        raise MahRuntimeError(
            f"detach(...) needs a thread.Thread to run on, got {_shown_type(thread)}", kind="TypeMismatch"
        )
    if not isinstance(fn, Closure):
        raise MahRuntimeError(f"thread.run: f must be a function, got {_shown_type(fn)}", kind="TypeMismatch")
    vt = ctx.thread
    rt = vt.rt
    with rt.lock:
        pool = rt._pool(thread.fields["id"])
    try:
        job = vt.make_job(fn, list(values.items))
    except NotSendable:
        raise MahThrow(thread_error("not_sendable", NOT_SENDABLE_MESSAGE)) from None
    return rt.submit(vt, pool, job)


def _pool_of(ctx, ident):
    rt = ctx.thread.rt
    with rt.lock:
        return rt._pool(ident)


def _close(ctx, args):
    ident, cancel = args
    ctx.thread.rt.close(_pool_of(ctx, ident), cancel is True)
    return NONE_VALUE


def _join(ctx, args):
    (ident,) = args
    return ctx.thread.rt.join(ctx.thread, _pool_of(ctx, ident))


def _pending(ctx, args):
    (ident,) = args
    rt = ctx.thread.rt
    with rt.lock:
        pool = rt._pool(ident)
        return Decimal(len(pool.jobs) + pool.running)


def _current(ctx, args):
    pool = ctx.thread.pool
    if pool is None:
        return VectorValue([Decimal(0), "main"])
    return VectorValue([Decimal(pool.id), pool.name])


def _cores(ctx, args):
    return Decimal(os.cpu_count() or 1)


def _semaphore_new(ctx, args):
    return ctx.thread.rt.semaphore_new(_whole(args[0]))


def _semaphore_acquire(ctx, args):
    return ctx.thread.rt.semaphore_acquire(ctx.thread, args[0])


def _semaphore_try_acquire(ctx, args):
    return ctx.thread.rt.semaphore_try_acquire(args[0])


def _semaphore_release(ctx, args):
    ctx.thread.rt.semaphore_release(args[0])
    return NONE_VALUE


def _semaphore_available(ctx, args):
    return ctx.thread.rt.semaphore_available(args[0])


def _channel_new(ctx, args):
    (capacity,) = args
    return ctx.thread.rt.channel_new(None if capacity is NONE_VALUE else _whole(capacity))


def _channel_send(ctx, args):
    return ctx.thread.rt.channel_send(ctx.thread, args[0], args[1])


def _channel_recv(ctx, args):
    return ctx.thread.rt.channel_recv(ctx.thread, args[0])


def _channel_try_recv(ctx, args):
    return ctx.thread.rt.channel_try_recv(args[0])


def _channel_close(ctx, args):
    ctx.thread.rt.channel_close(args[0])
    return NONE_VALUE


def _channel_len(ctx, args):
    return ctx.thread.rt.channel_len(args[0])


def _channel_closed(ctx, args):
    return ctx.thread.rt.channel_closed(args[0])


NATIVES = {
    "thread.spawn": (3, _spawn),
    "thread.submit": (3, _submit),
    "thread.close": (2, _close),
    "thread.join": (1, _join),
    "thread.pending": (1, _pending),
    "thread.current": (0, _current),
    "thread.cores": (0, _cores),
    "thread.semaphore_new": (1, _semaphore_new),
    "thread.semaphore_acquire": (1, _semaphore_acquire),
    "thread.semaphore_try_acquire": (1, _semaphore_try_acquire),
    "thread.semaphore_release": (1, _semaphore_release),
    "thread.semaphore_available": (1, _semaphore_available),
    "thread.channel_new": (1, _channel_new),
    "thread.channel_send": (2, _channel_send),
    "thread.channel_recv": (1, _channel_recv),
    "thread.channel_try_recv": (1, _channel_try_recv),
    "thread.channel_close": (1, _channel_close),
    "thread.channel_len": (1, _channel_len),
    "thread.channel_closed": (1, _channel_closed),
}
