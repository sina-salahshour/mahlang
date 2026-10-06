---
title: std:fs
order: 12
section: Async & system
summary: Read and write text and binary files, open files for streaming, and manage directories, globs and temp dirs.
---

# `std:fs`

Files and directories: reading and writing whole files (text or binary),
opening a file to read or write it piece by piece, listing, globbing,
copying, moving and removing.

```mah
import fs from "std:fs"

try {
    let dir = fs.temp_dir()                     # a fresh temporary directory
    defer fs.remove(dir, recursive: true)
    fs.write_text(dir + "/hello.txt", "hi there\n")
    print(fs.read_text(dir + "/hello.txt").trim())   # hi there
} catch {
    e: fs.FsError => { print(e.kind, e.message()) }
}
```

Every function **waits like an ordinary call**, but the actual work
happens off your program's thread. So `detach fs.read_text(path)` lets
other tasks and timers run while a big file loads. Build paths with
[`std:path`](/std/path).

## Whole files

`read_text`, `write_text` and `append_text` read or write a file in one
go. Text is UTF-8, read and written exactly as is (no newline conversion).
`write_*` creates the file or replaces what it held; `append_*` adds to the
end, creating it if need be.

```mah
import fs from "std:fs"

try {
    let dir = fs.temp_dir()
    defer fs.remove(dir, recursive: true)
    let log = dir + "/app.log"
    fs.append_text(log, "started\n")
    fs.append_text(log, "ready\n")
    print(fs.read_text(log).split("\n").len())     # 3
} catch {
    e: fs.FsError => { print(e.message()) }
}
```

For binary files, use the same functions with **Bytes**: `read_bytes`,
`write_bytes`, `append_bytes` (see [`std:bytes`](/std/bytes)).

## Streaming with `open`

For large files, or when you want a line at a time, `open(path, mode)`
gives a `fs.File`. Mode `"r"` reads (the default), `"w"` writes (replacing
the file), `"a"` appends. Always close it; `defer` makes that automatic:

```mah
import fs from "std:fs"

try {
    let dir = fs.temp_dir()
    defer fs.remove(dir, recursive: true)
    let p = dir + "/todo.txt"

    let out = fs.open(p, "w")
    for let item in ["buy milk", "walk dog", "call mom"] {
        out.write(item + "\n")
    }
    out.close()

    let f = fs.open(p)
    defer f.close()
    for let line, let i in f.lines() {
        print(i + 1, line)              # 1 buy milk / 2 walk dog / 3 call mom
    }
} catch {
    e: fs.FsError => { print(e.message()) }
}
```

`lines()` reads lazily (a line at a time, without line endings);
`read_line()` gives the next line or `none` at the end, and `read_all()`
the rest. For binary data, `read_bytes(max = none)` reads up to `max`
bytes (empty Bytes at the end) and `write_bytes(data)` writes them. Text
and binary reads can be mixed on one file:

```mah
import bytes from "std:bytes"
import fs from "std:fs"

try {
    let dir = fs.temp_dir()
    defer fs.remove(dir, recursive: true)
    let p = dir + "/data.bin"
    fs.write_bytes(p, bytes.from_hex("89504e47"))
    fs.append_bytes(p, bytes.new(4, 0))
    let f = fs.open(p)
    defer f.close()
    print(f.read_bytes(max: 4).to_hex(), f.read_bytes().len())   # 89504e47 4
} catch {
    e: fs.FsError => { print(e.kind) }
}
```

## Directories

```mah
import fs from "std:fs"

try {
    let root = fs.temp_dir()
    defer fs.remove(root, recursive: true)
    fs.mkdir(root + "/src/lib", parents: true)      # like mkdir -p
    fs.write_text(root + "/src/main.mh", "")
    fs.write_text(root + "/src/lib/util.mh", "")
    fs.write_text(root + "/README.md", "")

    print(fs.list_dir(root))                        # [README.md, src]
    print(fs.is_dir(root + "/src"), fs.exists(root + "/nope"))   # true false
    let found = fs.glob(root + "/src/**/*.mh").map(fn(p) { p.replace(root + "/", "") }).reduce()
    print(found)                                    # [src/lib/util.mh, src/main.mh]

    fs.copy(root + "/README.md", root + "/README.bak")
    fs.rename(root + "/README.bak", root + "/OLD.md")
    print(fs.info(root + "/OLD.md").kind)           # file
} catch {
    e: fs.FsError => { print(e.message()) }
}
```

- `list_dir` gives names (not paths), sorted.
- `glob(pattern)`: `*`, `?` and `[abc]` match within a name, `**` any
  number of directories. Names starting with `.` match only a pattern part
  that starts with `.` too. Results are sorted, in the same form as the
  pattern (relative or absolute).
- `mkdir(path, parents: true)` also creates missing parents and is fine if
  the directory exists.
- `remove(path)` deletes a file or an **empty** directory;
  `recursive: true` deletes a whole tree.
- `rename` and `copy` replace a file at the destination.
- `info(path)` gives `fs.FileInfo { kind, size, modified }`: `kind` is
  `"file"`, `"dir"` or `"other"`, `size` in bytes, `modified` in seconds
  since 1970 (compare with [`time.now()`](/std/time)).
- `exists`, `is_file` and `is_dir` never throw.

## Errors

Everything else throws `fs.FsError { kind, op, path, description }`, so
you can react to the specific problem:

```mah
import fs from "std:fs"

fn read_config(path: String) -> String {
    try {
        fs.read_text(path)
    } catch {
        e: fs.FsError => {
            if e.kind == "not_found" { "{}" } else { throw e }
        }
    }
}
print(read_config("/definitely/not/here.json"))     # {}
```

`kind` is one of `"not_found"`, `"permission_denied"`, `"already_exists"`,
`"is_a_directory"`, `"not_a_directory"`, `"directory_not_empty"`,
`"invalid_utf8"` (reading non-UTF-8 as text), `"closed"` (using a closed
File) or `"other"`. `op` names the function and `path` the path it was
given.

## Reference

| Function | |
|---|---|
| `read_text(path)`, `write_text(path, text)`, `append_text(path, text)` | a whole text file |
| `read_bytes(path)`, `write_bytes(path, data)`, `append_bytes(path, data)` | a whole binary file |
| `open(path, mode = "r")` | a `File` ("r", "w" or "a") |
| `exists(path)`, `is_file(path)`, `is_dir(path)` | Bools, never throwing |
| `info(path)` | a `FileInfo { kind, size, modified }` |
| `list_dir(path)` | the names in a directory, sorted |
| `glob(pattern)` | matching paths, sorted |
| `mkdir(path, parents = false)` | make a directory |
| `remove(path, recursive = false)` | delete a file or directory |
| `rename(from, to)`, `copy(from, to)` | move / copy a file |
| `temp_dir()` | a new, empty temporary directory (yours to remove) |

| `File` method | |
|---|---|
| `read_line()` | the next line, or `none` at the end |
| `lines()` | an iterator over the remaining lines |
| `read_all()` | everything from here to the end |
| `write(text)` | write text |
| `read_bytes(max = none)`, `write_bytes(data)` | binary reads and writes |
| `close()` | close it (twice does nothing) |

Programs that import `std:fs` need bytecode 1.17.
