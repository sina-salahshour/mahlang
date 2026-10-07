# Packages from GitHub repositories

Landed as M43 (see `docs/V2_DESIGN.md`'s M43 entry; the design contract is
`docs/contracts/M43_packages.md`). This is the user and implementer reference:
the manifest syntax, `pkg:` imports, `mah install`, the lock file, what's in
`.mah/`, and every error message.

A package is a directory of Mah files in a GitHub repository (the whole
repository or a subdirectory of it). A project declares its packages in
`mah-project.toml`, `mah install` fetches them into the project's `.mah/`
directory and pins the exact commits in `mah-lock.toml`, and the code imports
them with the reserved `pkg:` prefix. Package files are inlined by the
preprocessor like any other import, so a built `.mahc` contains them and runs
without `.mah/` on both VMs. Nothing changed in the bytecode or the VMs.

## Declaring packages

```toml
[dependencies]
json5  = { github = "acme/mah-json5", tag = "v1.2.0" }
utils  = { github = "acme/monorepo", branch = "main", path = "packages/utils" }
pinned = { github = "acme/thing", rev = "0123456789abcdef0123456789abcdef01234567" }
latest = { github = "acme/other" }            # the repository's default branch
```

Each key is the package's **local name**: what `pkg:NAME` uses and the
directory under `.mah/packages/`. It doesn't have to match the repository's
name or the package's own `[package] name`.

- `github = "owner/repo"` (required) names the repository, without `.git`.
- At most one of `tag`, `branch` and `rev` chooses the version; `rev` is a
  full 40-character commit hash (stored lowercased). With none of them, the
  repository's default branch is used.
- `path = "sub/dir"` uses that subdirectory as the package's root (one
  trailing `/` is allowed). Without it the repository's root is the package.

`load_project` checks the entries in sorted name order and reports the first
problem (each message starts with the manifest's path and `: `):

| problem | message |
|---|---|
| `dependencies` not a table | `dependencies must be a table` |
| bad name | `dependency name 'NAME' must be lowercase letters, digits, '_' and '-', starting with a letter` |
| not a table (`foo = "1.0"`) | `dependency 'NAME' must be a table, like NAME = { github = "owner/repo", tag = "v1.0" }` |
| other key | `unknown key 'dependencies.NAME.KEY'` |
| no `github` | `dependency 'NAME' needs github = "owner/repo"` |
| `github` ending in `.git` | `dependency 'NAME': write github = "owner/repo" without ".git"` |
| bad `github` | `dependency 'NAME': github must be "owner/repo" (got VALUE)` (a string JSON-quoted, else its TOML type: `integer`, `table`, ...) |
| two of tag/branch/rev | `dependency 'NAME': use only one of tag, branch and rev` |
| bad `tag` / `branch` | `dependency 'NAME': tag must be a non-empty string` (or `branch`) |
| bad `rev` | `dependency 'NAME': rev must be a full 40-character commit hash` |
| bad `path` | `dependency 'NAME': path must be a relative path inside the repository, like "lib" (got VALUE)` |

A `path` is bad when it isn't a string or (after removing one trailing `/`) is
empty, starts with `/`, contains `\` or `:`, or has an empty, `.` or `..`
segment.

### The library entry

`[package] lib` (new, optional, default `"src/lib.mh"`) names the file
`pkg:NAME` imports when the project is used as a package. A package root
without any `mah-project.toml` (a plain directory, typically chosen with
`path`) has `lib.mh` as its library file and no dependencies. `lib` must
stay inside the package (`package 'NAME' has an invalid mah-project.toml:
package.lib must be a path inside the package`). A package's
`entry`, `[[target]]`, `[run]` and `[types]` are ignored when it's used as a
dependency.

## Importing packages

`pkg:` is reserved like `std:` and never falls back to a relative file:

```
import json5 from "pkg:json5"            # the package's library file
import "pkg:json5/src/extra.mh"          # any file in it (.mh is optional)
```

`NAME` must match `[a-z][a-z0-9_-]*`, and the path after it may not have an
empty, `.` or `..` segment or a `\`. The usual export rules apply, and
`.test.mh` files can't be imported. A file reached both through `pkg:` and
through a relative import is the same module, inlined once.

**Scoping.** The root project's files may import only the root's own
`[dependencies]`. A package's files may import the package itself and only the
packages its own manifest declares (as recorded in the lock). A package that
is installed only because another package needs it isn't importable from code
that didn't declare it. Relative imports inside a package must stay inside
it, and a missing relative file is an error there even in a non-entry file.

**Nothing is installed automatically.** `mah run`, `build`, `test`, `check`
and the language server report a missing or stale package as a compile error
at the `pkg:` import, telling you to run `mah install`. The check is lazy: a
program that imports no package never looks at packages, so it compiles even
when nothing is installed. Compiling compares `.mah/installed.toml` with the
lock; it doesn't rehash the package files.

Locations inside package files read `pkg:NAME/REL` (`pkg:greet/src/lib.mh#3:5`)
in compile errors, in the DEBUG file names of a built `.mahc` and so in
runtime errors on both VMs. A package is not library code: an error thrown in
it is located in the package, like user code. Type diagnostics located in
package files are dropped by `mah run`/`build`/`check` and the language
server, so a strict project can use a package written loosely.

### Resolution errors

In the order they're checked (an error in an imported file gets
` (in 'LABEL')` added and is reported at the entry file's import of it):

1. `invalid package import 'LITERAL': write "pkg:NAME" or "pkg:NAME/path/to/file.mh"`
2. `package imports need a project, but no mah-project.toml was found for 'FILE'`
3. the manifest's own error, as-is
4. `'NAME' isn't in the [dependencies] of mah-project.toml`, plus
   ` (it's installed only for 'P')` when the lock lists NAME as a dependency of
   some package P (root project files only)
5. ``mah-project.toml has [dependencies] but there's no mah-lock.toml; run `mah install` ``,
   ``mah-lock.toml is invalid (DETAIL); delete it and run `mah install` `` or
   `mah-lock.toml was written by a newer mah (lock version N); upgrade mah`
6. ``mah-lock.toml is out of date with mah-project.toml ('X' was added); run `mah install` ``
   (or `was changed`)
7. `package 'P' imports 'pkg:NAME', but 'NAME' isn't in its [dependencies]`
   (package files only)
8. ``package 'NAME' isn't installed; run `mah install` `` or
   ``package 'NAME' in .mah/packages doesn't match mah-lock.toml; run `mah install` ``
9. `package 'NAME' has no library file REL; import one of its files as "pkg:NAME/path/to/file.mh"`
   or `package 'NAME' has no file 'REL'`

Inside a package: `import 'LITERAL' leaves package 'P'; import other packages
as "pkg:NAME"` and `cannot find imported file 'LITERAL'`.

## `mah install`

```
mah install [DIR] [--frozen] [--update [NAME ...]]
```

With no `DIR` the project is found by searching upward from the current
directory. The entry file doesn't have to exist (a library-only project can
install its dependencies). It prints one line per event on stdout and errors
as `error: MESSAGE` on stderr; it exits 2 for a missing or invalid manifest
and usage errors, 1 for an install failure, 0 on success.

- Plain `mah install` installs what the lock pins, resolving only the
  packages that are new or whose spec changed, and writes the lock.
- `--update` re-resolves every branch, tag and default branch (moving to
  their current commits); `--update NAME ...` only those packages.
- `--frozen` installs exactly the lock and never writes it: it fails when
  there's no lock (``--frozen: there's no mah-lock.toml; run `mah install` first``)
  or the lock is out of date with the manifest. Use it in CI. It can't be
  combined with `--update`.

Output, per package in processing order, then pruning and the lock:

```
installed greet 3aaab8a (acme/greet tag v1)
kept util 9f00e12 (acme/mono branch main, path packages/util)
reinstalled other 1234567 (acme/other default branch): its files had been changed
note: greet: skipped docs/link.md (only regular files are installed)
removed old
wrote mah-lock.toml
```

`kept` means the installed files already hash to the expected value, so
nothing was fetched. Files edited by hand under `.mah/` are noticed (their
hash changed) and reinstalled. A package no longer needed is removed. The
last line is `wrote mah-lock.toml` or `mah-lock.toml is up to date`. With no
dependencies and no lock it prints `no dependencies to install` and creates
nothing.

When every package is kept and everything comes from an in-sync lock,
`mah install` runs no git command at all, so it works offline and without
git installed.

### Transitive dependencies

A package's own `mah-project.toml` `[dependencies]` are installed too, all
into the one flat `.mah/packages/` (a name means one package per project).
Packages are processed breadth-first, the root's in sorted order first. If
two packages ask for the same name with different specs, that's an error:

```
packages 'a' and 'c' both depend on 'b' but ask for different versions: acme/b tag v1 vs acme/b tag v2; add 'b' to your [dependencies] to choose one
```

unless the root project declares that name itself: the root's spec wins, with
`note: 'a' asks for 'b' as acme/b tag v1, but mah-project.toml chooses acme/b tag v2`.
There are no version ranges and no solving: specs are compared, not ordered.

### Fetch errors

Prefixed `package 'NAME': ` in the output:

- `tag 'T' not found in OWNER/REPO`, `branch 'B' not found in OWNER/REPO`,
  `OWNER/REPO has no default branch`, `commit C7 not found in OWNER/REPO`
- `couldn't reach OWNER/REPO (URL): DETAIL`, `couldn't fetch OWNER/REPO (URL): DETAIL`
  (DETAIL is git's first error line)
- `DESCRIBE doesn't point to a commit`, `DESCRIBE has no files`,
  `'P' isn't a directory in OWNER/REPO at C7`, `OWNER/REPO has a file name that isn't UTF-8`,
  `OWNER/REPO has an unsafe file name "REL" at C7` (a crafted tree with a `.`, `..`,
  empty or `.git` path segment, or on Windows a `\` or `:` in one: it would be written
  outside the package root; nothing is installed)
- `content hash mismatch at C7: mah-lock.toml has EXPECTED, the fetched files hash to H;
  if the repository's history was rewritten, run `mah install --update NAME``
- `mah install needs git to fetch packages, and git wasn't found on PATH`
- `package 'NAME' has an invalid mah-project.toml: MSG`

On any failure the lock isn't written.

## The lock file, `mah-lock.toml`

Written only by `mah install`, always in exactly this layout, and meant to be
committed:

```toml
# mah-lock.toml -- written by `mah install`. Commit it; don't edit it by hand.
version = 1

[[package]]
name = "greet"
github = "acme/greet"
tag = "v1"
commit = "3aaab8a9e960398e6603109e12700059417f1d7d"
hash = "sha256:24e49e36d4e16f5d6eb1a8f28697aa0218a8df1660577b347eeef9c8a43b6498"
dependencies = ["util"]

[[package]]
name = "util"
github = "acme/mono"
branch = "main"
path = "packages/util"
commit = "..."
hash = "sha256:..."
dependencies = []
```

Every package (direct and transitive) is listed, sorted by name; the keys
appear in that order, with at most one of `tag`/`branch`/`rev` (none for the
default branch) and `path` only when it isn't empty. `dependencies` is the
sorted names from the package's own manifest. Strings are written with JSON
quoting. A lock with no packages is just the header and `version = 1`. The
file is written atomically (a temporary file, then a rename).

The lock is out of date when a root `[dependencies]` entry is missing from it
(`'NAME' was added`) or has a different spec (`'NAME' was changed`). Extra
entries never make it out of date.

## `.mah/`

```
.mah/
  .gitignore        # "*", so .mah/ is never committed
  installed.toml    # what is installed: the lock's layout without `dependencies`
  packages/
    greet/          # exactly the package's files at the locked commit
  tmp/              # mah install's scratch space, removed when it finishes
```

A package root holds every regular file under the dependency's `path` at the
locked commit, nothing else. Deleting `.mah/` is a complete reset; `mah test`,
`mah format` and the language server's workspace scans skip it. `mah install`
deletes `installed.toml` before it replaces the first package, so a crash in
the middle leaves "isn't installed" errors rather than stale state.

## The content hash

`hash` covers the package root's regular files: their relative paths
(`/`-separated, UTF-8) and exact bytes, not file modes, symlinks, submodules,
empty directories or anything outside `path`:

```python
h = hashlib.sha256()
for rel, data in sorted(files, key=lambda e: e[0].encode("utf-8")):
    h.update(rel.encode("utf-8") + b"\0" + str(len(data)).encode("ascii") + b"\0" + data)
hash = "sha256:" + h.hexdigest()
```

Files are read from git objects (`git ls-tree` and `git cat-file --batch`),
never from a checkout, so `.gitattributes` and `core.autocrlf` can't change
the bytes and the hash is the same on every machine. Symlinks and submodules
are skipped with a note; anything under a `.mah` directory is skipped
silently.

## Fetching and the environment

`mah install` uses the `git` command (it must be on `PATH` when something has
to be fetched): `git ls-remote` to resolve a tag (the peeled commit of an
annotated tag), branch or `HEAD`, then a shallow `git fetch` of the one
commit into `.mah/tmp/`.

- `MAH_GITHUB_URL_BASE` replaces `https://github.com` (mirrors, GitHub
  Enterprise, tests); the URL is `BASE/owner/repo.git`.
- `GITHUB_TOKEN`, when set, is sent to `https://github.com/` only, as an
  `http.extraHeader` passed through git's environment configuration
  (`GIT_CONFIG_COUNT`/`KEY`/`VALUE`), so it's never on a command line. Git
  older than 2.31 ignores those variables, and the fetch is then
  unauthenticated. Your own git credentials (credential helpers) work too.
- Proxies work through git's own support (`https_proxy`, `HTTPS_PROXY`,
  `http.proxy`).
- `GIT_TERMINAL_PROMPT=0` is always set, so a private repository without
  credentials fails instead of waiting for a password.
- Variables that point git at a repository (`GIT_DIR`, `GIT_WORK_TREE`,
  `GIT_INDEX_FILE`, `GIT_OBJECT_DIRECTORY`, ...) are removed from git's
  environment, so `mah install` run from a git hook works in its own scratch
  repository and never touches yours.

## Out of scope

`mah add`/`mah remove` (edit `[dependencies]` by hand); hosts other than
GitHub and `git = "url"` sources (an unknown key is an error, so they can be
added later); semver ranges and version solving; a download cache shared
between projects; package registries; `mah init --lib`; several versions of
one name in a project; packages binding natives (`extern fn` stays
`std:`-only); concurrent `mah install` runs in one project; verifying commit
signatures.
