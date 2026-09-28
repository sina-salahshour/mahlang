"""M35 (1.12, docs/MAHC_FORMAT.md #4.4): std:fs's natives, as the Python VM
implements them. `runtime/src/vm/fs.rs` mirrors every rule here.

Every one returns a Promise at once and does its blocking work on a worker
thread (code_interpreter's `_IoHub.submit`), which settles it with a
**result** Vector: `[true, value]`, or `[false, kind, description]` for a
failure -- std:fs turns that into an `FsError`, so natives never build Mah
error values. `kind` is one of `KINDS`; `description` is fixed per kind,
except for "other", where it's the OS's own text (strerror).

Text is UTF-8, read and written byte for byte: no newline translation, and
lines split at `\\n` only (a `\\r` before it is dropped, like `input`).
Open files live in the hub's table by id (the VM's handle table); std:fs
wraps an id in its `File` struct.

Like the VM, this module never imports the compiler.
"""

from __future__ import annotations

import errno
import os
import shutil
import tempfile
from decimal import Decimal

from .runtime_values import NONE_VALUE, MahRuntimeError, VectorValue, type_name_of

DESCRIPTIONS = {
    "not_found": "no such file or directory",
    "permission_denied": "permission denied",
    "already_exists": "already exists",
    "is_a_directory": "is a directory",
    "not_a_directory": "not a directory",
    "directory_not_empty": "directory not empty",
    "invalid_utf8": "not valid UTF-8 text",
    "closed": "the file is closed",
}
KINDS = frozenset(DESCRIPTIONS) | {"other"}


def _ok(value) -> VectorValue:
    return VectorValue([True, value])


def _failure(kind: str, description: str | None = None) -> VectorValue:
    return VectorValue([False, kind, description if description is not None else DESCRIPTIONS[kind]])


def _classify(exc: BaseException) -> VectorValue:
    if isinstance(exc, UnicodeDecodeError):
        return _failure("invalid_utf8")
    if isinstance(exc, FileNotFoundError):
        return _failure("not_found")
    if isinstance(exc, PermissionError):
        return _failure("permission_denied")
    if isinstance(exc, FileExistsError):
        return _failure("already_exists")
    if isinstance(exc, IsADirectoryError):
        return _failure("is_a_directory")
    if isinstance(exc, NotADirectoryError):
        return _failure("not_a_directory")
    if isinstance(exc, OSError) and exc.errno == errno.ENOTEMPTY:
        return _failure("directory_not_empty")
    if isinstance(exc, OSError) and exc.strerror:
        return _failure("other", exc.strerror)
    return _failure("other", str(exc))


def _guarded(job):
    def run():
        try:
            return _ok(job())
        except (OSError, UnicodeDecodeError) as exc:
            return _classify(exc)

    return run


def _string(name: str, what: str, value) -> str:
    if not isinstance(value, str):
        raise MahRuntimeError(f"{name}: {what} must be a String, got {type_name_of(value)}", kind="TypeMismatch")
    return value


def _async(ctx, job):
    from .runtime_values import PromiseInstance

    promise = PromiseInstance()
    ctx.io.submit(promise, _guarded(job))
    return promise


def _decode(data: bytes) -> str:
    return data.decode("utf-8")


# -- whole files and paths -----------------------------------------------------


def _read_text(ctx, args):
    path = _string("read_text", "path", args[0])

    def job():
        with open(path, "rb") as f:
            return _decode(f.read())

    return _async(ctx, job)


def _write(mode: str, name: str):
    def native(ctx, args):
        path = _string(name, "path", args[0])
        data = _string(name, "text", args[1]).encode("utf-8")

        def job():
            with open(path, mode) as f:
                f.write(data)
            return NONE_VALUE

        return _async(ctx, job)

    return native


def _info(ctx, args):
    """[kind ("file", "dir" or "other"), size in bytes, modified (ms since
    1970, 0 before it)], following symlinks."""
    path = _string("info", "path", args[0])

    def job():
        import stat

        st = os.stat(path)
        kind = "dir" if stat.S_ISDIR(st.st_mode) else "file" if stat.S_ISREG(st.st_mode) else "other"
        modified = max(0, st.st_mtime_ns // 1_000_000)
        return VectorValue([kind, Decimal(st.st_size), Decimal(modified)])

    return _async(ctx, job)


def _list_dir(ctx, args):
    path = _string("list_dir", "path", args[0])
    return _async(ctx, lambda: VectorValue(sorted(os.listdir(path))))


def _mkdir(ctx, args):
    path = _string("mkdir", "path", args[0])
    parents = args[1] is True

    def job():
        if parents:
            os.makedirs(path, exist_ok=True)
        else:
            os.mkdir(path)
        return NONE_VALUE

    return _async(ctx, job)


def _remove(ctx, args):
    """A file, or a directory: an empty one, or with `recursive` a whole
    tree. Symlinks themselves are removed, never followed."""
    path = _string("remove", "path", args[0])
    recursive = args[1] is True

    def job():
        if os.path.isdir(path) and not os.path.islink(path):
            if recursive:
                shutil.rmtree(path)
            else:
                os.rmdir(path)
        else:
            os.remove(path)
        return NONE_VALUE

    return _async(ctx, job)


def _rename(ctx, args):
    src = _string("rename", "from", args[0])
    dst = _string("rename", "to", args[1])

    def job():
        os.replace(src, dst)
        return NONE_VALUE

    return _async(ctx, job)


def _copy(ctx, args):
    """A file's contents to `to` (replacing it); a directory is refused."""
    src = _string("copy", "from", args[0])
    dst = _string("copy", "to", args[1])

    def job():
        if os.path.isdir(src):
            raise IsADirectoryError(errno.EISDIR, "is a directory", src)
        shutil.copyfile(src, dst)
        return NONE_VALUE

    return _async(ctx, job)


def _temp_dir(ctx, args):
    """A new, empty directory in the system's temporary directory."""
    return _async(ctx, lambda: tempfile.mkdtemp(prefix="mah-"))


# -- open files (the handle table) ---------------------------------------------

_MODES = {"r": "rb", "w": "wb", "a": "ab"}


def _open(ctx, args):
    """A file id, for mode "r" (read), "w" (write, replacing) or "a"
    (append)."""
    path = _string("open", "path", args[0])
    mode = _string("open", "mode", args[1])
    if mode not in _MODES:
        raise MahRuntimeError(f'open: mode must be "r", "w" or "a", got "{mode}"', kind="ArgumentError")
    io = ctx.io

    def job():
        if mode == "r" and os.path.isdir(path):
            raise IsADirectoryError(errno.EISDIR, "is a directory", path)
        # writes go straight to the file (unbuffered), as in the Rust VM
        buffering = -1 if mode == "r" else 0
        return Decimal(io.add_file(open(path, _MODES[mode], buffering=buffering)))

    return _async(ctx, job)


def _file(ctx, name: str, file_id):
    if isinstance(file_id, bool) or not isinstance(file_id, Decimal):
        raise MahRuntimeError(f"{name}: expected a file id, got {type_name_of(file_id)}", kind="TypeMismatch")
    return ctx.io.files.get(int(file_id)) if file_id == file_id.to_integral_value() else None


def _file_op(name: str, op):
    def native(ctx, args):
        if name == "write":
            _string("write", "text", args[1])
        f = _file(ctx, name, args[0])
        rest = args[1:]
        if f is None:
            from .runtime_values import PromiseInstance

            promise = PromiseInstance()
            promise.resolve(_failure("closed"))
            return promise
        return _async(ctx, lambda: op(ctx, args[0], f, rest))

    return native


def _read_line_op(ctx, file_id, f, rest):
    """The next line without its `\\n` (or `\\r\\n`), or `none` at the end."""
    if "r" not in f.mode:
        raise OSError(errno.EBADF, "the file isn't open for reading")
    line = f.readline()
    if line == b"":
        return NONE_VALUE
    if line.endswith(b"\n"):
        line = line[:-1]
        if line.endswith(b"\r"):
            line = line[:-1]
    return _decode(line)


def _read_all_op(ctx, file_id, f, rest):
    if "r" not in f.mode:
        raise OSError(errno.EBADF, "the file isn't open for reading")
    return _decode(f.read())


def _write_op(ctx, file_id, f, rest):
    text = rest[0]
    if "r" in f.mode:
        raise OSError(errno.EBADF, "the file isn't open for writing")
    f.write(text.encode("utf-8"))
    return NONE_VALUE


def _close(ctx, args):
    f = _file(ctx, "close", args[0])
    if f is not None:
        del ctx.io.files[int(args[0])]

    def job():
        if f is not None:
            f.close()
        return NONE_VALUE

    return _async(ctx, job)


NATIVES = {
    "fs.read_text": (1, _read_text),
    "fs.write_text": (2, _write("wb", "write_text")),
    "fs.append_text": (2, _write("ab", "append_text")),
    "fs.info": (1, _info),
    "fs.list_dir": (1, _list_dir),
    "fs.mkdir": (2, _mkdir),
    "fs.remove": (2, _remove),
    "fs.rename": (2, _rename),
    "fs.copy": (2, _copy),
    "fs.temp_dir": (0, _temp_dir),
    "fs.open": (2, _open),
    "fs.read_line": (1, _file_op("read_line", _read_line_op)),
    "fs.read_all": (1, _file_op("read_all", _read_all_op)),
    "fs.write": (2, _file_op("write", _write_op)),
    "fs.close": (1, _close),
}
