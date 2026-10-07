"""Format constants for `.mahc` version 1.3 -- the single source of truth
mirroring docs/MAHC_FORMAT.md. Every other module in this package (and the
VM, `mah/code_interpreter.py`) imports its constants from here rather than
hard-coding a section id/opcode/tag number a second time.

M16 bumped MINOR to 1: default parameter values + keyword-argument calls
(the PARAMS section, `jmpset`, the `*kw` opcodes, operand kind `S*`) and
the `io.write` native -- see docs/MAHC_FORMAT.md #4.5a/#4.6/#7.

M17 bumps MINOR to 2: `matchrange` (range patterns), the `le`/`ge`/`not`
operators, and the native inherent methods `String.len`/`String.char_at`/
`Function.arity` (§6.7) -- see docs/MAHC_FORMAT.md #4.6/#6.3/#6.7/#7.
Ranges, iterators, and their adapters aren't VM features at all: they're
ordinary Mah code (the compiler's prelude, `mah/std/prelude.mh`) compiled
like any user program.

M19 bumps MINOR to 3: Vectors and Maps -- the `vector`/`map` opcodes, the
built-in types `Vector`/`Map`, their native inherent methods, and the
`Index`/`IndexAssign` system traits (docs/MAHC_FORMAT.md #6.7/#6.9).

M25 bumps MINOR to 4: typed, catchable errors (docs/ERRORS.md). New
built-in type `RuntimeError` (index 2; user types now numbered from 3),
`Promise` gains a `Failed { error }` variant, the new opcodes `matchtype`/
`deferdepth`/`deferabove`/`throw`, and a new required section HANDLERS
(`0x08`, required iff minor >= 4) -- docs/MAHC_FORMAT.md #4.1/#4.3/#4.6/#4.8.

M27 bumps MINOR to 5: new natives only (`math.tan`/`asin`/`acos`/`atan`/
`atan2`/`exp`/`log`/`log10`, behind `std:math`) -- docs/MAHC_FORMAT.md #4.4.

M29 bumps MINOR to 6: new native inherent methods only -- the String
methods and `Vector.join` (docs/MAHC_FORMAT.md #6.7, NATIVE_METHOD_SINCE_MINOR).

M30 bumps MINOR to 7: new natives only (`value.type_name`/`fields`/
`variant`, `string.chars`/`code_point`/`from_code_point`, behind std:json
and std:csv) -- docs/MAHC_FORMAT.md #4.4.

M31 bumps MINOR to 8: new natives only (`random.seed`/`fresh`/`next`/
`below`, the shared xoshiro256** generator behind std:random) --
docs/MAHC_FORMAT.md #4.4.

M32 bumps MINOR to 9: new natives only (`regex.find`/`find_all`, behind
std:regex) -- docs/MAHC_FORMAT.md #4.4.

M33 bumps MINOR to 10: a new native (`io.read_line`, a Promise of the next
line of standard input, behind the async `input`) and pending I/O in the
scheduler -- docs/MAHC_FORMAT.md #4.4/#6.4. The compiler stops emitting
`io.input`, which VMs keep for older files.

M34 bumps MINOR to 11: new natives only (`time.now_ms`/`monotonic_ms`/
`cancel`, `promise.new`/`resolve`/`fail`, behind std:time and std:async)
-- docs/MAHC_FORMAT.md #4.4.

M35 bumps MINOR to 12: new natives only (`fs.*`, behind std:fs; open files
are ids in the VM's handle table) -- docs/MAHC_FORMAT.md #4.4.

M36 bumps MINOR to 13: new natives only (`process.*`, behind std:process)
-- docs/MAHC_FORMAT.md #4.4.

M41a bumps MINOR to 14: type values (`loadtype`), spread calls (`callspread`/
`callmethodspread`, plus `spread`, which builds their argument Vector and
Map), the `reflect.*` natives behind std:reflect, and the optional META
section (0x82: written annotations, doc comments and constant defaults --
older VMs skip it, so a file whose only 1.14 content is META keeps its old
minor) -- docs/MAHC_FORMAT.md #4.4/#4.6/#4.10.

M41b bumps MINOR to 15: `decorate` and the `reflect.decorators` native.

M41c bumps MINOR to 16: rest parameters (PARAMS flag bits 1 and 2), the
`paramhooks` opcode, the `hooks.*` natives behind the hook traits, and
function-item impls (`defmethod` keys spelled `fn#<function index>`) --
docs/MAHC_FORMAT.md #4.4/#4.5a/#4.6/#6.7. A file uses 1.16 only when it
uses one of those.

M37 bumps MINOR to 17: the `Bytes` value (docs/MAHC_FORMAT.md #5/#6.9),
its native methods and `String.to_bytes` (#6.7), the `bytes.*` natives
behind std:bytes and the binary `fs.*` ones (#4.4), and the primitive type
code 8 (`loadtype 1, 8`, the Type `Bytes`). Bytes values only come from
1.17 natives and methods, so a file uses 1.17 only when it calls one.

M38 bumps MINOR to 18: new natives only (`socket.*`, behind std:socket;
sockets and listeners are ids in the VM's socket table) --
docs/MAHC_FORMAT.md #4.4.

M39 bumps MINOR to 19: a new native (`socket.start_tls`, TLS on an open
socket, behind std:http) -- docs/MAHC_FORMAT.md #4.4.

M42 bumps MINOR to 20: new natives only (`socket.tls_server_config`, a
TLS server's certificate and key loaded into the socket table, and
`socket.start_tls_server`, a TLS server handshake on an accepted socket;
behind std:socket and std:http's `serve(tls:)`) -- docs/MAHC_FORMAT.md #4.4.

M44 bumps MINOR to 21: the shared-variable opcodes `sharedget`/`sharedset`/
`sharedlock`/`sharedunlock` and the `thread.*` natives behind std:thread
(docs/MAHC_FORMAT.md #4.4/#4.6/#6.11).
"""

from __future__ import annotations

MAGIC = b"MAHC"
# `mah build` writes this line before MAGIC (and marks the file executable)
# so `./prog.mahc` runs it; decode() skips any leading `#!...\n` line --
# docs/MAHC_FORMAT.md #3. `env -S` splits "mah runc" into two arguments, so
# it works whatever the file is named (the `mah FILE` shorthand keys off a
# `.mahc` extension).
SHEBANG = b"#!/usr/bin/env -S mah runc\n"
MAJOR = 1
MINOR = 21

# -- section ids (docs/MAHC_FORMAT.md #3) -----------------------------------
SEC_STRINGS = 0x01
SEC_CONSTANTS = 0x02
SEC_TYPES = 0x03
SEC_NATIVES = 0x04
SEC_FUNCTIONS = 0x05
SEC_CODE = 0x06
SEC_PARAMS = 0x07  # M16 (1.1): required iff minor >= 1 -- docs/MAHC_FORMAT.md #4.5a
SEC_HANDLERS = 0x08  # M25 (1.4): required iff minor >= 4 -- docs/MAHC_FORMAT.md #4.8
SEC_DEBUG = 0x80
SEC_TESTS = 0x81  # M28: optional -- only in `mah test` builds, docs/MAHC_FORMAT.md #4.9
SEC_META = 0x82  # M41a: optional -- annotations/docs/defaults, docs/MAHC_FORMAT.md #4.10

REQUIRED_SECTIONS = (SEC_STRINGS, SEC_CONSTANTS, SEC_TYPES, SEC_NATIVES, SEC_FUNCTIONS, SEC_CODE)
# M16: PARAMS joins the required-section list only for minor >= 1 files --
# see decode.py's `_read_sections`, which picks between this and
# `REQUIRED_SECTIONS` once it has read the file's own minor version.
REQUIRED_SECTIONS_V1 = REQUIRED_SECTIONS + (SEC_PARAMS,)
# M25: HANDLERS joins the required-section list only for minor >= 4 files.
REQUIRED_SECTIONS_V4 = REQUIRED_SECTIONS_V1 + (SEC_HANDLERS,)

# -- constant tags (docs/MAHC_FORMAT.md #4.2) -------------------------------
TAG_NONE = 0
TAG_FALSE = 1
TAG_TRUE = 2
TAG_INT = 3
TAG_DEC = 4
TAG_STR = 5


class MahcFormatError(Exception):
    """Raised by decode.py for any structurally invalid `.mahc` file, and by
    code_interpreter.py's loader when a file declares a native the running
    VM doesn't implement (or implements with a different arity) -- see
    docs/MAHC_FORMAT.md #3 ("fail fast")."""


# -- built-in types (docs/MAHC_FORMAT.md #4.3) ------------------------------
# Type indices 0/1 are implicit -- never written in the TYPES section -- and
# every conforming VM/encoder must agree on this exact layout. Each entry:
# (type_name, [(variant_name, [field_name, ...]), ...]).
BUILTIN_TYPES = (
    ("Option", (("none", ()), ("some", ("value",)))),
    ("Promise", (("Pending", ()), ("Settled", ("value",)))),
)
# M25 (1.4, docs/MAHC_FORMAT.md #4.1): `Promise` gains a `Failed { error }`
# variant, and a new built-in enum `RuntimeError` (index 2) is added --
# user types are numbered from 3 in minor >= 4 files (still 2 in older
# ones). Variant order matches compiler/resolve.py's pre-seeded
# `enum_decls`/typecheck.py's `_register_types` exactly (asserted in
# `bytecode/lower.py`'s `build_types`).
BUILTIN_TYPES_V4 = (
    ("Option", (("none", ()), ("some", ("value",)))),
    ("Promise", (("Pending", ()), ("Settled", ("value",)), ("Failed", ("error",)))),
    (
        "RuntimeError",
        (
            ("DivisionByZero", ("message",)),
            ("TypeMismatch", ("message",)),
            ("NoSuchField", ("message",)),
            ("NoSuchMethod", ("message",)),
            ("ArgumentError", ("message",)),
            ("IndexOutOfRange", ("message",)),
            ("MatchFailed", ("message",)),
            ("InputError", ("message",)),
            ("Internal", ("message",)),
        ),
    ),
)


def builtin_types_for(minor: int) -> tuple:
    """The built-in TYPES-section-index-0.. entries a file of this minor
    version uses -- see `BUILTIN_TYPES_V4`'s docstring. Both VMs and every
    encoder/decoder/disassembler go through this rather than picking
    between the two tuples themselves."""
    return BUILTIN_TYPES_V4 if minor >= 4 else BUILTIN_TYPES

# -- natives (docs/MAHC_FORMAT.md #4.4) -------------------------------------
NATIVE_ARITIES = {
    "io.print": 1,
    "io.write": 1,  # M16 (1.1)
    "io.input": 0,
    "math.sin": 1,
    "math.cos": 1,
    "time.sleep_async": 1,
    # M27 (1.5): std:math's transcendental functions.
    "math.tan": 1,
    "math.asin": 1,
    "math.acos": 1,
    "math.atan": 1,
    "math.atan2": 2,
    "math.exp": 1,
    "math.log": 1,
    "math.log10": 1,
    # M30 (1.7): reflection and characters, for std:json/std:csv.
    "value.type_name": 1,
    "value.fields": 1,
    "value.variant": 1,
    "string.chars": 1,
    "string.code_point": 1,
    "string.from_code_point": 1,
    # M31 (1.8): the shared generator, for std:random.
    "random.seed": 1,
    "random.fresh": 0,
    "random.next": 1,
    "random.below": 2,
    # M32 (1.9): std:regex's matcher.
    "regex.find": 3,
    "regex.find_all": 2,
    # M33 (1.10): the async `input`.
    "io.read_line": 1,
    # M34 (1.11): std:time and std:async.
    "time.now_ms": 0,
    "time.monotonic_ms": 0,
    "time.cancel": 1,
    "promise.new": 0,
    "promise.resolve": 2,
    "promise.fail": 2,
    # M35 (1.12): std:fs.
    "fs.read_text": 1,
    "fs.write_text": 2,
    "fs.append_text": 2,
    "fs.info": 1,
    "fs.list_dir": 1,
    "fs.mkdir": 2,
    "fs.remove": 2,
    "fs.rename": 2,
    "fs.copy": 2,
    "fs.temp_dir": 0,
    "fs.open": 2,
    "fs.read_line": 1,
    "fs.read_all": 1,
    "fs.write": 2,
    "fs.close": 1,
    # M36 (1.13): std:process.
    "process.args": 0,
    "process.exit": 1,
    "process.env_get": 1,
    "process.env_set": 2,
    "process.env_remove": 1,
    "process.env_all": 0,
    "process.cwd": 0,
    "process.pid": 0,
    "process.platform": 0,
    "process.run": 5,
    # M41a (1.14): std:reflect.
    "reflect.type_of": 1,
    "reflect.signature": 1,
    "reflect.schema": 1,
    "reflect.methods": 1,
    "reflect.implements": 2,
    "reflect.construct": 2,
    "reflect.construct_variant": 3,
    # M41b (1.15): the decorators stored by `decorate` (kind, a, b).
    "reflect.decorators": 3,
    # M41c (1.16): the hook machinery behind std:reflect's WrapFn/WrapParam/
    # WrapStruct/WrapField (docs/MAHC_FORMAT.md #4.4).
    "hooks.has": 2,
    "hooks.adopt": 2,
    "hooks.same_fn": 2,
    "hooks.set_type": 2,
    "hooks.set_param": 3,
    "hooks.of": 1,
    "hooks.get_field": 2,
    "hooks.set_field": 3,
    "bytes.new": 2,
    "bytes.from_vector": 1,
    "bytes.from_hex": 1,
    "bytes.from_base64": 1,
    "fs.read_bytes": 1,
    "fs.write_bytes": 2,
    "fs.append_bytes": 2,
    "fs.file_read_bytes": 2,
    "fs.file_write_bytes": 2,
    # M38 (1.18): std:socket.
    "socket.connect": 3,
    "socket.listen": 3,
    "socket.accept": 2,
    "socket.send": 2,
    "socket.recv": 3,
    "socket.shutdown": 1,
    "socket.close": 1,
    # M39 (1.19): TLS, for std:http.
    "socket.start_tls": 3,
    # M42 (1.20): TLS servers.
    "socket.tls_server_config": 2,
    "socket.start_tls_server": 3,
    # M44 (1.21): std:thread.
    "thread.spawn": 3,
    "thread.submit": 3,
    "thread.close": 2,
    "thread.join": 1,
    "thread.pending": 1,
    "thread.current": 0,
    "thread.cores": 0,
    "thread.semaphore_new": 1,
    "thread.semaphore_acquire": 1,
    "thread.semaphore_try_acquire": 1,
    "thread.semaphore_release": 1,
    "thread.semaphore_available": 1,
    "thread.channel_new": 1,
    "thread.channel_send": 2,
    "thread.channel_recv": 1,
    "thread.channel_try_recv": 1,
    "thread.channel_close": 1,
    "thread.channel_len": 1,
    "thread.channel_closed": 1,
}

# M16: which minor version introduced each 1.1+ native -- a 1.0 file
# ('minor' == 0) using one is rejected at load (docs/MAHC_FORMAT.md #4.6's
# "must not appear in a file whose minor version is 0"). Absent = present
# since 1.0.
NATIVE_SINCE_MINOR = {
    "io.write": 1,
    "math.tan": 5,
    "math.asin": 5,
    "math.acos": 5,
    "math.atan": 5,
    "math.atan2": 5,
    "math.exp": 5,
    "math.log": 5,
    "math.log10": 5,
    "value.type_name": 7,
    "value.fields": 7,
    "value.variant": 7,
    "string.chars": 7,
    "string.code_point": 7,
    "string.from_code_point": 7,
    "random.seed": 8,
    "random.fresh": 8,
    "random.next": 8,
    "random.below": 8,
    "regex.find": 9,
    "regex.find_all": 9,
    "io.read_line": 10,
    "time.now_ms": 11,
    "time.monotonic_ms": 11,
    "time.cancel": 11,
    "promise.new": 11,
    "promise.resolve": 11,
    "promise.fail": 11,
    "fs.read_text": 12,
    "fs.write_text": 12,
    "fs.append_text": 12,
    "fs.info": 12,
    "fs.list_dir": 12,
    "fs.mkdir": 12,
    "fs.remove": 12,
    "fs.rename": 12,
    "fs.copy": 12,
    "fs.temp_dir": 12,
    "fs.open": 12,
    "fs.read_line": 12,
    "fs.read_all": 12,
    "fs.write": 12,
    "fs.close": 12,
    "process.args": 13,
    "process.exit": 13,
    "process.env_get": 13,
    "process.env_set": 13,
    "process.env_remove": 13,
    "process.env_all": 13,
    "process.cwd": 13,
    "process.pid": 13,
    "process.platform": 13,
    "process.run": 13,
    "reflect.type_of": 14,
    "reflect.signature": 14,
    "reflect.schema": 14,
    "reflect.methods": 14,
    "reflect.implements": 14,
    "reflect.construct": 14,
    "reflect.construct_variant": 14,
    "reflect.decorators": 15,
    "hooks.has": 16,
    "hooks.adopt": 16,
    "hooks.same_fn": 16,
    "hooks.set_type": 16,
    "hooks.set_param": 16,
    "hooks.of": 16,
    "hooks.get_field": 16,
    "hooks.set_field": 16,
    "bytes.new": 17,
    "bytes.from_vector": 17,
    "bytes.from_hex": 17,
    "bytes.from_base64": 17,
    "fs.read_bytes": 17,
    "fs.write_bytes": 17,
    "fs.append_bytes": 17,
    "fs.file_read_bytes": 17,
    "fs.file_write_bytes": 17,
    "socket.connect": 18,
    "socket.listen": 18,
    "socket.accept": 18,
    "socket.send": 18,
    "socket.recv": 18,
    "socket.shutdown": 18,
    "socket.close": 18,
    "socket.start_tls": 19,
    # M42 (1.20)
    "socket.tls_server_config": 20,
    "socket.start_tls_server": 20,
    # M44 (1.21): std:thread.
    "thread.spawn": 21,
    "thread.submit": 21,
    "thread.close": 21,
    "thread.join": 21,
    "thread.pending": 21,
    "thread.current": 21,
    "thread.cores": 21,
    "thread.semaphore_new": 21,
    "thread.semaphore_acquire": 21,
    "thread.semaphore_try_acquire": 21,
    "thread.semaphore_release": 21,
    "thread.semaphore_available": 21,
    "thread.channel_new": 21,
    "thread.channel_send": 21,
    "thread.channel_recv": 21,
    "thread.channel_try_recv": 21,
    "thread.channel_close": 21,
    "thread.channel_len": 21,
    "thread.channel_closed": 21,
}

# M29: native inherent methods added after 1.0, by the minor that added
# them. Methods are called by name, so the encoder can't know a call's
# target; a file calling any method of one of these names (on any value) is
# written with at least that minor -- docs/MAHC_FORMAT.md #3/#6.7.
NATIVE_METHOD_SINCE_MINOR = {
    name: 6
    for name in (
        "split", "trim", "trim_start", "trim_end", "pad_start", "pad_end", "replace", "replace_all",
        "starts_with", "ends_with", "contains", "index_of", "repeat", "to_upper", "to_lower", "lines",
        "parse_number", "join",
        # prelude methods (M29) that rely on the 1.6 native methods
        "to_number",
    )
}
# M37 (1.17): `String.to_bytes`, the only way to make Bytes without a 1.17
# native. The Bytes methods themselves need no entry: a Bytes value only
# exists in a file that already uses 1.17.
NATIVE_METHOD_SINCE_MINOR["to_bytes"] = 17

# The opcodes that call a method by name: operand 1 is the method's name.
METHOD_CALL_OPCODES = ("callmethod", "callmethodkw", "detachmethod", "detachmethodkw", "callmethodspread")

# -- opcodes (docs/MAHC_FORMAT.md #4.6) --------------------------------------
# name -> (code, operand_kinds); operand kinds use the letters of #4.6's
# table ("A", "A?", "A*", "K", "S", "S?", "L", "F", "T", "N", "X", "B"), in
# encoding order, exactly mirroring the opcode table there.
OPCODES: dict[str, tuple[int, tuple[str, ...]]] = {
    "halt": (0x00, ()),
    "move": (0x01, ("A", "A")),
    "loadk": (0x02, ("K", "A")),
    "jmp": (0x03, ("L",)),
    "jmpf": (0x04, ("A", "L")),
    "jmpset": (0x05, ("A", "L")),  # M16 (1.1)
    "add": (0x10, ("A", "A", "A")),
    "sub": (0x11, ("A", "A", "A")),
    "mul": (0x12, ("A", "A", "A")),
    "div": (0x13, ("A", "A", "A")),
    "idiv": (0x14, ("A", "A", "A")),
    "mod": (0x15, ("A", "A", "A")),
    "pow": (0x16, ("A", "A", "A")),
    "eq": (0x17, ("A", "A", "A")),
    "neq": (0x18, ("A", "A", "A")),
    "lt": (0x19, ("A", "A", "A")),
    "gt": (0x1A, ("A", "A", "A")),
    "and": (0x1B, ("A", "A", "A")),
    "or": (0x1C, ("A", "A", "A")),
    "neg": (0x1D, ("A", "A")),
    "le": (0x1E, ("A", "A", "A")),  # M17 (1.2)
    "ge": (0x1F, ("A", "A", "A")),  # M17 (1.2)
    "not": (0x0F, ("A", "A")),  # M17 (1.2)
    "closure": (0x20, ("F", "A")),
    "call": (0x21, ("A", "A*")),
    "ret": (0x22, ("A",)),
    "retval": (0x23, ("A",)),
    "callkw": (0x26, ("A", "A*", "S*")),  # M16 (1.1)
    "callmethod": (0x24, ("A", "S", "A*", "S?")),
    "defmethod": (0x25, ("A", "S", "S?", "S", "B")),
    "callmethodkw": (0x27, ("A", "S", "A*", "S*", "S?")),  # M16 (1.1)
    "callspread": (0x2D, ("A", "A", "A")),  # M41a (1.14): callee, positional Vector, keyword Map
    "callmethodspread": (0x2E, ("A", "S", "A", "A", "S?")),  # M41a (1.14): recv, name, Vector, Map, trait
    "detach": (0x28, ("A", "A*", "A")),
    "detachmethod": (0x29, ("A", "S", "A*", "S?", "A")),
    "await": (0x2A, ("A", "A")),
    "detachkw": (0x2B, ("A", "A*", "S*", "A")),  # M16 (1.1)
    "detachmethodkw": (0x2C, ("A", "S", "A*", "S*", "S?", "A")),  # M16 (1.1)
    "struct": (0x30, ("T", "A*", "A")),
    "enum": (0x31, ("T", "N", "A*", "A")),
    "getfield": (0x32, ("A", "S", "A")),
    "setfield": (0x33, ("A", "S", "A")),
    "matchstruct": (0x34, ("A", "T", "A")),
    "matchenum": (0x35, ("A", "T", "N", "A")),
    "matchfail": (0x36, ()),
    "matchrange": (0x37, ("A", "A?", "A?", "B", "A")),  # M17 (1.2)
    "vector": (0x38, ("A*", "A")),  # M19 (1.3)
    "map": (0x39, ("A*", "A")),  # M19 (1.3)
    "matchtype": (0x3A, ("A", "T", "A")),  # M25 (1.4)
    "loadtype": (0x3B, ("N", "N", "A")),  # M41a (1.14): kind (0 TYPES index, 1 primitive), index, dest
    "spread": (0x3C, ("A", "A", "B")),  # M41a (1.14): target, source, keyword?
    "decorate": (0x3D, ("N", "N", "N", "A*")),  # M41b (1.15): kind, a, b, the decorator values in source order
    "paramhooks": (0x3E, ("N", "N", "A")),  # M41c (1.16): function index, parameter index, dest
    "deferpush": (0x40, ()),
    "deferadd": (0x41, ("A",)),
    "deferpeek": (0x42, ("A",)),
    "deferpop": (0x43, ("A",)),
    "deferscopepop": (0x44, ()),
    "deferdepth": (0x45, ("A",)),  # M25 (1.4)
    "deferabove": (0x46, ("A", "A")),  # M25 (1.4)
    "native": (0x50, ("X", "A*", "A?")),
    "throw": (0x60, ("A",)),  # M25 (1.4)
    # M44 (1.21): shared variables -- index, name (for messages), [mode,] address.
    "sharedget": (0x70, ("N", "S", "N", "A")),
    "sharedset": (0x71, ("N", "S", "A")),
    "sharedlock": (0x72, ("N", "S", "A")),
    "sharedunlock": (0x73, ("N", "S", "N")),
}

OPCODES_BY_CODE: dict[int, tuple[str, tuple[str, ...]]] = {
    code: (name, kinds) for name, (code, kinds) in OPCODES.items()
}

# M16: which minor version introduced each opcode -- mirrors
# NATIVE_SINCE_MINOR above; a 1.0 file using one of these is rejected at
# load (docs/MAHC_FORMAT.md #4.6). Absent = present since 1.0.
OPCODE_SINCE_MINOR: dict[str, int] = {
    "jmpset": 1,
    "callkw": 1,
    "callmethodkw": 1,
    "detachkw": 1,
    "detachmethodkw": 1,
    "le": 2,
    "ge": 2,
    "not": 2,
    "matchrange": 2,
    "vector": 3,
    "map": 3,
    "matchtype": 4,
    "deferdepth": 4,
    "deferabove": 4,
    "throw": 4,
    "callspread": 14,
    "callmethodspread": 14,
    "loadtype": 14,
    "spread": 14,
    "decorate": 15,
    "paramhooks": 16,
    "sharedget": 21,
    "sharedset": 21,
    "sharedlock": 21,
    "sharedunlock": 21,
}
