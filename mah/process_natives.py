"""M36 (1.13, docs/MAHC_FORMAT.md #4.4): std:process's natives, as the
Python VM implements them. `runtime/src/vm/process.rs` mirrors every rule
here.

Each VM run owns an **environment table**: a snapshot of the process's
environment taken when the VM starts (entries that aren't valid Unicode are
left out). `env_get`/`env_set`/`env_remove`/`env_all` read and write only
that table, never the real environment, and `run` starts its child with
exactly that table plus the call's overrides.

`process.run` is asynchronous like std:fs's natives (`_IoHub.submit`), and
settles with the same result Vectors: `[true, [code, stdout, stderr]]`, or
`[false, kind, description]` when the program couldn't be started -- kind
"not_found", "permission_denied" or "other" (the OS's own text). A non-zero
exit is not a failure. A child killed by signal N has code 128 + N.

Like the VM, this module never imports the compiler.
"""

from __future__ import annotations

import os
import subprocess
import sys
from decimal import Decimal

from .fs_natives import _failure
from .runtime_values import NONE_VALUE, MahRuntimeError, MapValue, VectorValue, map_key, type_name_of


def snapshot_environment() -> dict:
    """The process's environment as `{name: value}`, without the entries
    whose name or value isn't valid Unicode."""
    env = {}
    for name, value in os.environ.items():
        try:
            name.encode("utf-8")
            value.encode("utf-8")
        except UnicodeEncodeError:
            continue
        env[name] = value
    return env


def _string(fn: str, what: str, value) -> str:
    if not isinstance(value, str):
        raise MahRuntimeError(f"{fn}: {what} must be a String, got {type_name_of(value)}", kind="TypeMismatch")
    return value


def _text(fn: str, what: str, value) -> str:
    """A String without NUL."""
    text = _string(fn, what, value)
    if "\0" in text:
        raise MahRuntimeError(f"{fn}: {what} must not contain a NUL character", kind="ArgumentError")
    return text


def _name(fn: str, what: str, value) -> str:
    """An environment variable name: non-empty, no `=`, no NUL."""
    text = _text(fn, what, value)
    if text == "":
        raise MahRuntimeError(f"{fn}: {what} must not be empty", kind="ArgumentError")
    if "=" in text:
        raise MahRuntimeError(f'{fn}: {what} must not contain "="', kind="ArgumentError")
    return text


# -- arguments, exit, the environment table -------------------------------------


def _args(ctx, args):
    return VectorValue(list(ctx.io.args))


def _exit(ctx, args):
    from .code_interpreter import ProgramExit

    code = args[0]
    if isinstance(code, bool) or not isinstance(code, Decimal) or code != code.to_integral_value() or not 0 <= code <= 255:
        raise MahRuntimeError("exit code must be a whole number from 0 to 255", kind="ArgumentError")
    sys.stdout.flush()
    raise ProgramExit(int(code))


def _env_get(ctx, args):
    name = _name("env_get", "name", args[0])
    value = ctx.io.env.get(name)
    return NONE_VALUE if value is None else value


def _env_set(ctx, args):
    name = _name("env_set", "name", args[0])
    value = _text("env_set", "value", args[1])
    ctx.io.env[name] = value
    return NONE_VALUE


def _env_remove(ctx, args):
    name = _name("env_remove", "name", args[0])
    ctx.io.env.pop(name, None)
    return NONE_VALUE


def _env_all(ctx, args):
    return MapValue({map_key(k): (k, v) for k, v in sorted(ctx.io.env.items())})


def _cwd(ctx, args):
    try:
        return os.getcwd()
    except OSError as exc:
        raise MahRuntimeError(f"cwd: {exc.strerror or exc}", kind="Internal") from None


def _pid(ctx, args):
    return Decimal(os.getpid())


def _platform(ctx, args):
    p = sys.platform
    if p.startswith("linux"):
        return "linux"
    if p == "darwin":
        return "macos"
    if p == "win32":
        return "windows"
    return p


# -- running programs -----------------------------------------------------------


def _start_failure(exc: OSError):
    if isinstance(exc, FileNotFoundError):
        return _failure("not_found")
    if isinstance(exc, PermissionError):
        return _failure("permission_denied")
    return _failure("other", exc.strerror or str(exc))


def _run(ctx, args):
    from .runtime_values import PromiseInstance

    program = _text("run", "program", args[0])
    if program == "":
        raise MahRuntimeError("run: program must not be empty", kind="ArgumentError")
    argv = args[1]
    if not isinstance(argv, VectorValue):
        raise MahRuntimeError(f"run: args must be a Vector of Strings, got {type_name_of(argv)}", kind="TypeMismatch")
    arguments = [_text("run", "argument", a) for a in argv.items]
    cwd = args[2]
    cwd = None if cwd is NONE_VALUE else _text("run", "cwd", cwd)
    overrides = args[3]
    env = dict(ctx.io.env)
    if overrides is not NONE_VALUE:
        if not isinstance(overrides, MapValue):
            raise MahRuntimeError(
                f"run: env must be a Map of Strings or none, got {type_name_of(overrides)}", kind="TypeMismatch"
            )
        for k, v in overrides.entries.values():
            env[_name("run", "env name", k)] = _text("run", "env value", v)
    stdin = _text("run", "stdin", args[4]).encode("utf-8")

    def job():
        try:
            done = subprocess.run(
                [program, *arguments], input=stdin, capture_output=True, cwd=cwd, env=env
            )
        except OSError as exc:
            return _start_failure(exc)
        code = done.returncode
        if code < 0:
            code = 128 - code
        return VectorValue(
            [
                True,
                VectorValue(
                    [
                        Decimal(code),
                        done.stdout.decode("utf-8", errors="replace"),
                        done.stderr.decode("utf-8", errors="replace"),
                    ]
                ),
            ]
        )

    promise = PromiseInstance()
    ctx.io.submit(promise, job)
    return promise


NATIVES = {
    "process.args": (0, _args),
    "process.exit": (1, _exit),
    "process.env_get": (1, _env_get),
    "process.env_set": (2, _env_set),
    "process.env_remove": (1, _env_remove),
    "process.env_all": (0, _env_all),
    "process.cwd": (0, _cwd),
    "process.pid": (0, _pid),
    "process.platform": (0, _platform),
    "process.run": (5, _run),
}
