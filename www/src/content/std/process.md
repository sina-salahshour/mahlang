---
title: std:process
order: 13
section: Async & system
summary: Command-line arguments, environment variables, exit codes, and running other programs or shell commands.
---

# `std:process`

Everything about the running program and the programs it starts: its
command-line arguments, environment variables, working directory and exit
code, and running other programs to capture their output.

```mah
import process from "std:process"

let port = process.env_get("PORT").unwrap_or("8080")
print(port.len() > 0, process.platform() != "")      # true true
```

## Arguments

`args()` gives what follows `--` on the command line, so
`mah run app.mh -- input.txt --verbose` (or `mah runc app.mahc -- ...`)
gives `["input.txt", "--verbose"]`:

```mah
import process from "std:process"

let args = process.args()
let verbose = args.filter(fn(a) { a == "--verbose" }).reduce().len() > 0
let files = args.filter(fn(a) { !a.starts_with("--") }).reduce()
print(verbose, files)       # false [] (when run without arguments)
```

## Exiting

`exit(code = 0)` ends the program **at once** with an exit code from 0 to
255. Output written so far is flushed, but pending `defer`s don't run,
background tasks are abandoned, and no `try`/`catch` can stop it. Use it
to report failure to a shell script:

```mah
import process from "std:process"

let args = process.args()
if args.len() == 0 {
    print("usage: tool FILE")       # then `echo $?` in the shell shows 2
    process.exit(2)
}
print("processing", args[0])
```

## Environment variables

`env_get(name)` is `some(value)` or `none`. `env()` gives every variable as
a Map, sorted by name. `env_set` and `env_remove` change the program's
**own copy** of the environment: they never change the real one, but the
programs `run` starts get the copy.

```mah
import process from "std:process"

process.env_set("GREETING", "hello")
print(process.env_get("GREETING"), process.env_get("NO_SUCH_VAR"))   # some(hello) none
process.env_remove("GREETING")
print(process.env_get("GREETING"))                                   # none
```

Names must be non-empty and contain no `=`; names and values can't
contain a NUL character.

## Running programs

`run(program, args = [], cwd = none, env = none, stdin = "")` starts a
program **directly** (looked up in `PATH`, no shell, so arguments need no
quoting), waits for it, and returns a `process.Output { code, stdout,
stderr }`:

```mah
import process from "std:process"

try {
    let out = process.run("echo", ["hello", "world"])
    print(out.ok(), out.code, out.stdout.trim())          # true 0 hello world

    let sorted = process.run("sort", stdin: "b\na\nc\n")
    print(sorted.stdout.split("\n")[0])                   # a
} catch {
    e: process.ProcessError => { print(e.kind, e.message()) }
}
```

- A **non-zero exit is not an error**: check `out.ok()` (`code == 0`) or
  `out.code`. A program killed by signal N has code `128 + N`.
- Only a program that **can't be started** throws `process.ProcessError`,
  with `kind` `"not_found"`, `"permission_denied"` or `"other"`.
- `cwd` is a `some(path)` to run in another directory; `env` is
  `some(map)` of variables to add.
- Output is decoded as UTF-8, invalid bytes becoming U+FFFD.

```mah
import process from "std:process"

try {
    process.run("no-such-program-xyz")
} catch {
    e: process.ProcessError => { print(e.kind) }    # not_found
}
```

## Shell commands

`shell(command, cwd = none, env = none, stdin = "")` runs a command line
through the system shell (`/bin/sh -c`, or `cmd /C` on Windows), so pipes,
globs and redirection work. A command the shell can't find is just a
non-zero exit code. Never build a shell command from untrusted input; use
`run` with an argument Vector instead.

```mah
import process from "std:process"

try {
    print(process.shell("echo a b c | wc -w").stdout.trim())    # 3
    let r = process.shell("exit 3")
    print(r.ok(), r.code)                                       # false 3
} catch {
    e: process.ProcessError => { print(e.message()) }
}
```

Both `run` and `shell` wait like any call, but the work happens off your
program's thread, so `detach process.run(...)` runs a program in the
background while other tasks continue.

## Reference

| Function | |
|---|---|
| `args()` | the arguments after `--` |
| `exit(code = 0)` | end the program now (0 to 255) |
| `env_get(name)` | `some(value)` or `none` |
| `env_set(name, value)`, `env_remove(name)` | change the program's copy of the environment |
| `env()` | a Map of every variable, sorted by name |
| `cwd()` | the current directory, as an absolute path |
| `pid()` | this program's process id |
| `platform()` | `"linux"`, `"macos"`, `"windows"`, or the OS's own name |
| `run(program, args = [], cwd = none, env = none, stdin = "")` | run a program, no shell |
| `shell(command, cwd = none, env = none, stdin = "")` | run a command line through the shell |

| Type | |
|---|---|
| `Output` | `{ code, stdout, stderr }` with `ok()` |
| `ProcessError` | `{ kind, command, description }` |
