---
title: std:path
order: 5
section: Text & data
summary: Join, split and normalize file paths as Strings, without touching the file system.
---

# `std:path`

File paths as plain Strings: joining, splitting into parts, normalizing,
and working out relative paths. **Nothing here touches the file system**
(for that, see [`std:fs`](/std/fs)), so these functions never throw and
work the same for paths that don't exist.

```mah
import path from "std:path"

let p = path.join("src", "lib/util.mh")
print(p, path.dirname(p), path.basename(p))     # src/lib/util.mh src/lib util.mh
print(path.stem(p), path.extension(p))          # util .mh
```

Input may use `/` or `\` and start with a drive (`C:`), so Windows paths
work too. `normalize`, `relative` and `join` answer with `/`.

## Building paths

`join(a, b)` puts a separator between two parts. If `b` is absolute, it
**replaces** `a`, just as `cd` would. `join_all` joins a whole Vector left
to right. Neither normalizes:

```mah
import path from "std:path"

print(path.join("a/b", "c"), path.join("a/b", "/etc/hosts"))   # a/b/c /etc/hosts
print(path.join_all(["home", "ann", "notes.txt"]))           # home/ann/notes.txt
print(path.join("a", "../b"))                                # a/../b
```

## Taking paths apart

| Function | For `"docs/archive/report.tar.gz"` |
|---|---|
| `dirname(p)` | `docs/archive` |
| `basename(p)` | `report.tar.gz` |
| `extension(p)` | `.gz` (from the **last** `.`) |
| `stem(p)` | `report.tar` |

A name that only *starts* with a dot, like `.bashrc`, has no extension.
`dirname` of a bare name is `"."`, and `basename` of a root is `""`.

```mah
import path from "std:path"

print(path.extension(".bashrc"), path.stem(".bashrc"))   #  .bashrc
print(path.dirname("file.txt"), path.dirname("/file.txt"))   # . /

# Swap a file's extension.
fn with_extension(p: String, ext: String) -> String {
    path.join(path.dirname(p), path.stem(p) + ext)
}
print(with_extension("src/main.mh", ".mahc"))            # src/main.mahc
```

## Normalizing

`normalize(p)` removes `.` segments and repeated separators and applies
each `..` to the segment before it. At a root, `..` stays at the root. The
result never has a trailing separator, and is `"."` when nothing is left:

```mah
import path from "std:path"

print(path.normalize("a/./b/../c//d/"))     # a/c/d
print(path.normalize("/../etc"))            # /etc
print(path.normalize("a/.."), path.normalize("../x"))   # . ../x
print(path.normalize("C:\\Users\\ann\\..\\bo"))          # C:/Users/bo
```

## Relative paths

`relative(from, to)` is the path that leads from **directory** `from` to
`to`. Both are normalized first. When they have different roots (or `from`
climbs out further than `to` can follow), there is no relative path and the
answer is `to`, normalized:

```mah
import path from "std:path"

print(path.relative("/a/b", "/a/c/d"))     # ../c/d
print(path.relative("src", "src/lib/x.mh"))   # lib/x.mh
print(path.relative("x", "x"))             # .
print(path.is_absolute("/etc"), path.is_absolute("C:\\x"), path.is_absolute("a/b"))   # true true false
```

## Reference

| Function | |
|---|---|
| `join(a, b)` | `b` after `a` with a separator; an absolute `b` replaces `a` |
| `join_all(parts)` | `join` over a Vector, left to right |
| `dirname(p)` | everything before the last segment (`"."` if none) |
| `basename(p)` | the last segment (`""` for a root) |
| `extension(p)` | the last segment's extension, from its last `.` |
| `stem(p)` | the last segment without its extension |
| `normalize(p)` | `.`, `..` and repeated separators resolved |
| `is_absolute(p)` | starts with a separator, or a drive and a separator |
| `relative(from, to)` | the path from directory `from` to `to` |
