# M43 contract: packages from GitHub repositories

`docs/NEXT_PHASES.md`'s "Packages from GitHub repositories". `[dependencies]` in
`mah-project.toml` (reserved and required to be empty since M15) becomes real: each entry
names a GitHub repository, optionally a tag, branch or commit, and optionally a
subdirectory to use as the package's root. `mah install` fetches them into the project, a
lock file pins the exact commit and a content hash of each, and `import x from "pkg:NAME"`
resolves to the installed package.

This file is normative. Everything below is decided; a coder should not need to make a
design call. Where an exact message is given, tests pin that exact text.

**No bytecode change, no VM change, no new natives.** The preprocessor inlines package
files like any other import (`mah/preprocessor.py`), so a `.mahc` built from a project that
uses packages is self-contained and runs on both VMs unchanged; neither VM resolves imports
(the Rust VM in `runtime/` only loads compiled bytecode: `decode.rs`/`link.rs`/`exec.rs`,
and has no parser or import handling). MINOR stays at whatever it is when M43 lands. The
only visible change in bytecode is the DEBUG file name of package files (section 9), which
is a string like any other file name. VM parity is therefore by construction; the tests in
section 13 still run package-using programs on the VM `MAH_TEST_VM` selects.

---

## 1. Decisions (with the reasons)

1. **Import syntax: a reserved `pkg:` prefix**, like `std:`: `import json5 from
   "pkg:json5"` (the package's library entry) and `import "pkg:json5/src/extra.mh"` (any
   file in it, `.mh` optional). A bare `"json5"` already means the relative file
   `json5.mh`, so a bare package name would collide with relative imports; a prefix can't,
   reads the same as `std:`, and makes package imports greppable.
2. **Project-local install**: `<project>/.mah/packages/<name>/`, holding exactly the
   package's files (the `path` subdirectory's contents, not the whole repository, and no
   `.git`). Imports must resolve to the locked version for *this* project, the LSP should
   jump to real files the user can read, and deleting `.mah/` must be a complete reset.
   A global cache would need per-commit directories plus an indirection for no gain at
   this size. (A shared git object cache is left for later; see section 15.)
3. **Fetching with the `git` CLI** (subprocess), not GitHub tarballs over `urllib`:
   - one mechanism for tags (annotated or not), branches, the default branch and exact
     commits (`git ls-remote`, then a shallow `git fetch` of one commit);
   - private repositories work with the user's existing git credentials (credential
     helpers), plus `GITHUB_TOKEN` (section 7);
   - proxies work through git's own support (`https_proxy`/`HTTPS_PROXY`/`http.proxy`),
     with nothing for mah to do;
   - no GitHub API, so no API rate limit;
   - tests use real git against local bare repositories over `file://` (section 13), so
     the code path under test is the real one, with no fake HTTP server.
   The cost: `git` must be on `PATH`, but only when something actually has to be fetched.
   Mah stays free of Python dependencies.
4. **Files are read from git objects** (`git ls-tree` + `git cat-file --batch`), not
   `git archive` or a checkout, so `.gitattributes` (`export-ignore`, `export-subst`,
   `eol`) and `core.autocrlf` can't change the installed bytes: the installed files are
   exactly the committed blobs on every machine, which keeps the content hash stable.
5. **Transitive dependencies are supported, in one flat namespace.** A package's own
   `mah-project.toml` `[dependencies]` (same syntax) are installed too, all into
   `.mah/packages/`. A name means one package per project. If two packages ask for the
   same name with different specs, that's an error unless the root project declares that
   name itself, and then the root's spec wins (with a `note:`). There's no semver and no
   version solving: specs are compared, not ordered.
6. **Imports are scoped to declared dependencies.** The root project's files may import
   only the root's own `[dependencies]`; a package's files only that package's own
   `[dependencies]` (as recorded in the lock) and the package itself. Transitive packages
   are installed but aren't importable from code that didn't declare them.
7. **The package's library entry** is `[package] lib` in the package's
   `mah-project.toml` (new optional key, default `"src/lib.mh"`). A package root with no
   manifest at all (a plain directory, typically chosen with `path`) has entry `lib.mh` and
   no dependencies.
8. **Missing or stale packages never auto-install.** `mah run/build/test/check` and the LSP
   report an error at the `pkg:` import that says to run `mah install`. The check is
   **lazy**: it only happens when a `pkg:` import is resolved, so a project whose code
   imports no package (or a file that doesn't) compiles even if nothing is installed.
9. **The lock** is `mah-lock.toml` next to the manifest, written by `mah install` with a
   fixed layout (section 5), read with `tomllib`. It's meant to be committed. `.mah/` is
   not committed (template `.gitignore`, and `mah install` writes `.mah/.gitignore`).
10. **`mah install` flags**: `--frozen` (install exactly the lock; fail if it's missing or
    out of date; never write it) and `--update [NAME ...]` (re-resolve branches, tags and
    default branches: the named packages, or all). **`mah add`/`mah remove` are out of
    scope**: you edit `[dependencies]` by hand, then run `mah install`.
11. **Type diagnostics located in package files are dropped**, by `mah run`/`build`/`check`
    and the LSP, so a strict or explicit project can use a package written loosely. The
    package's own `mah check` is for the package's author.
12. **GitHub only** (the `github` key). `MAH_GITHUB_URL_BASE` changes the base URL (tests,
    mirrors, GitHub Enterprise). Other git hosts and plain `git = "url"` sources are out of
    scope; an unknown key is an error, so they can be added later without ambiguity.

---

## 2. The `[dependencies]` table

```toml
[dependencies]
json5  = { github = "acme/mah-json5", tag = "v1.2.0" }
utils  = { github = "acme/monorepo", branch = "main", path = "packages/utils" }
pinned = { github = "acme/thing", rev = "0123456789abcdef0123456789abcdef01234567" }
latest = { github = "acme/other" }            # the repository's default branch
```

Each key is the package's **local name**, which is what `pkg:NAME` uses and the directory
under `.mah/packages/`. It doesn't need to match the repository name or the package's own
`[package] name`.

### 2.1 Validation (`mah/project/manifest.py`)

`load_project` replaces the "aren't supported yet" check. Dependencies are validated in
**sorted name order**, and the first problem fails through the existing `fail(...)`, so
every message below is prefixed with `"{manifest_path}: "`:

| problem | message |
|---|---|
| `dependencies` not a table | `dependencies must be a table` (unchanged) |
| name not matching `[a-z][a-z0-9_-]*` | `dependency name 'NAME' must be lowercase letters, digits, '_' and '-', starting with a letter` |
| value not a table (e.g. `foo = "1.0"`) | `dependency 'NAME' must be a table, like NAME = { github = "owner/repo", tag = "v1.0" }` |
| key not in `github`, `tag`, `branch`, `rev`, `path` | `unknown key 'dependencies.NAME.KEY'` |
| no `github` | `dependency 'NAME' needs github = "owner/repo"` |
| `github` a string ending in `.git` | `dependency 'NAME': write github = "owner/repo" without ".git"` |
| `github` not a string matching `[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+`, or repo part `.`/`..` | `dependency 'NAME': github must be "owner/repo" (got VALUE)` where VALUE is `repr`-like: a string as `"text"` (JSON-quoted with `json.dumps`), anything else as its TOML type name (`integer`, `boolean`, `table`, ...) |
| more than one of `tag`/`branch`/`rev` | `dependency 'NAME': use only one of tag, branch and rev` |
| `tag` not a non-empty string | `dependency 'NAME': tag must be a non-empty string` |
| `branch` not a non-empty string | `dependency 'NAME': branch must be a non-empty string` |
| `rev` not a string of exactly 40 hex digits | `dependency 'NAME': rev must be a full 40-character commit hash` |
| `path` invalid (below) | `dependency 'NAME': path must be a relative path inside the repository, like "lib" (got VALUE)` |

The checks run in that table order per dependency. `rev` is stored lowercased. **`path`**
must be a string. One trailing `/` is removed, and then it's rejected if it is empty, starts
with `/`, contains `\` or `:`, or has a segment (split on `/`) that is empty, `.` or `..`.
Absent `path` means the repository root (stored as `""`).

### 2.2 `[package] lib`

`_PACKAGE_KEYS` gains `"lib"`. If present it must be a string (`package.lib must be a
string`). `Project.lib` is the absolute path `os.path.join(root, lib)`, with a default of
`os.path.join(root, "src/lib.mh")`. Its existence isn't checked at load time. Any project
may set it; it only matters when the project is installed as a package.

### 2.3 Data model

In `mah/project/manifest.py` (so `packages.py` can import it without a cycle):

```python
@dataclass(frozen=True)
class Dependency:
    name: str
    github: str        # "owner/repo", as written
    ref_kind: str      # "tag" | "branch" | "rev" | "default"
    ref: str | None    # None iff ref_kind == "default"; rev lowercased
    path: str          # "" = repository root; normalized as in 2.1

    def spec(self) -> tuple:           # what "same dependency" means everywhere
        return (self.github, self.ref_kind, self.ref, self.path)

    def describe(self) -> str: ...
```

`describe()` gives `acme/greet tag v1`, `acme/greet branch main`, `acme/greet rev 0123abc`
(the first 7 characters), or `acme/greet default branch`, followed by `, path P` when
`path` isn't empty: `acme/mono branch main, path packages/utils`.

`Project.dependencies` becomes `dict[str, Dependency]`, inserted in sorted name order
(`{}` when there are none, so the existing `assertEqual(project.dependencies, {})` holds).
The parser is `parse_dependency(name, raw) -> Dependency`, which raises `ValueError(message
without the manifest prefix)`; `load_project` calls it and `fail`s with the message.

---

## 3. Packages on disk

```
myapp/
  mah-project.toml
  mah-lock.toml                  # committed
  src/main.mh
  .mah/                          # not committed
    .gitignore                   # "*\n", written by mah install
    installed.toml               # what is installed (section 6)
    packages/
      json5/                     # the package root: the files of `path` at the locked commit
        mah-project.toml
        src/lib.mh
      utils/
        lib.mh
    tmp/                         # mah install's scratch space, removed when it finishes
```

- **Package root** = `.mah/packages/<name>/`. Its contents are every regular file under
  the dependency's `path` at the locked commit, at its path relative to `path`. Nothing
  else is written inside a package root.
- **Library entry** (`library_entry(package_root)`):
  - If `<package_root>/mah-project.toml` exists, it's `load_project(...)`'s `.lib`. An
    invalid manifest gives `package 'NAME' has an invalid mah-project.toml: MSG`, where
    MSG is the `MahProjectError` text with its leading `"{manifest_path}: "` removed.
  - Otherwise it's `<package_root>/lib.mh`.
- The package's `[[target]]`, `[run]` and `[types]` and its `entry` are ignored when it is
  used as a dependency.

---

## 4. Imports

### 4.1 Syntax

A literal starting with `pkg:` is a package import (the prefix is reserved, like `std:`,
and never falls back to a relative file):

- `pkg:NAME`: the package's library entry.
- `pkg:NAME/REL`: the file `REL` relative to the package root. `.mh` is optional, with the
  exact same rule as relative imports: try `REL` as written, then `REL + ".mh"` if `REL`
  doesn't already end in `.mh`. For the error message the `.mh` form counts as the
  intended path.

`NAME` must match `[a-z][a-z0-9_-]*`. `REL` is split on `/` and must have no empty, `.` or
`..` segment, and no `\`. Anything else, including a bare `pkg:` or `pkg:Foo`, is invalid
(4.3 #1). `.test.mh` files are refused by the existing rule (`can't import the test file
'...'`). Both `import NS from "pkg:..."` and the flat `import "pkg:..."` work, with the
usual export rules.

### 4.2 Which project, and which package is importing

A **package context** belongs to one preprocess call and is loaded lazily, the first time
a `pkg:` import is seen (so programs that import no package never read the manifest
again). `PackageContext.find(start)` takes as `start` the entry path, or
`os.path.join(os.getcwd(), "<buffer>")` for an in-memory buffer:

1. If `package_of_path(start)` (4.4) says `start` is inside
   `<X>/.mah/packages/<name>/` and `<X>/mah-project.toml` exists, the project root is `<X>`.
   This is how the LSP works on a package file opened from `.mah/`.
2. Otherwise `find_manifest(os.path.dirname(start))`, the existing upward search.
3. No manifest means no context (4.3 #2).

The **importing package** of a file is `package_of_path(importer)`'s name when the file is
under `<root>/.mah/packages/<name>/`; otherwise it's `None`, meaning the root project.

### 4.3 Resolution and its errors

`PackageContext.resolve(importer_path, literal) -> (resolved_path | None, error | None)`.
The checks run in this order; the first failure is the error:

1. **Literal** invalid:
   `invalid package import 'LITERAL': write "pkg:NAME" or "pkg:NAME/path/to/file.mh"`
2. **No project**:
   `package imports need a project, but no mah-project.toml was found for 'FILE'`
   (FILE = `os.path.basename(importer)`, or `<buffer>`).
3. **Manifest invalid**: the `MahProjectError` message as-is.
4. **Root importer only**, `NAME` not in the root's `[dependencies]`:
   `'NAME' isn't in the [dependencies] of mah-project.toml`, plus
   ` (it's installed only for 'P')` when the lock exists and lists NAME as a dependency
   of some package. P is the first such package in sorted name order.
5. **No lock** (`mah-lock.toml` missing while `[dependencies]` isn't empty):
   ``mah-project.toml has [dependencies] but there's no mah-lock.toml; run `mah install` ``
   Invalid lock: ``mah-lock.toml is invalid (DETAIL); delete it and run `mah install` ``.
   Newer lock: `mah-lock.toml was written by a newer mah (lock version N); upgrade mah`
   (section 5 defines DETAIL and N).
6. **Lock out of date**, `lock_out_of_date(project.dependencies, lock)` (section 5) not
   None: ``mah-lock.toml is out of date with mah-project.toml ('X' was added); run `mah
   install` `` (or `was changed`).
7. **Package importer only**: when importer package P is not NAME and NAME isn't in the
   lock's `dependencies` for P:
   `package 'P' imports 'pkg:NAME', but 'NAME' isn't in its [dependencies]`
8. **Not installed**: `.mah/packages/NAME` isn't a directory, or `.mah/installed.toml` has
   no entry for NAME: ``package 'NAME' isn't installed; run `mah install` ``.
   **Installed but different**: the installed entry's `(github, ref_kind, ref, path,
   commit, hash)` differs from the lock entry's:
   ``package 'NAME' in .mah/packages doesn't match mah-lock.toml; run `mah install` ``.
   (Compilation does **not** rehash the files; see 6.)
9. **Target file**:
   - `pkg:NAME` resolves to `library_entry` (any error from it is the message). If that
     file doesn't exist: `package 'NAME' has no library file REL; import one of its files
     as "pkg:NAME/path/to/file.mh"`, where REL is the entry relative to the package root
     with `/` separators (`src/lib.mh`).
   - `pkg:NAME/REL` that doesn't exist: `package 'NAME' has no file 'REL'`, with REL in
     its `.mh` form as described in 4.1.

On success the result is the absolute path, and the module is inlined like any other
import (same file, same module: the include guard works on absolute paths, so reaching a
file through `pkg:` and through a relative import inlines it once).

**Relative imports inside a package** (importer under `.mah/packages/P/`) resolve as
today, with two additions:
- a relative literal that resolves outside P's root is an error:
  `import 'LITERAL' leaves package 'P'; import other packages as "pkg:NAME"`;
- a relative literal that doesn't exist is an error, even in a non-entry file:
  `cannot find imported file 'LITERAL'`. Today that's silent for non-entry files and stays
  silent outside packages.

### 4.4 Path helpers (`mah/project/package_paths.py`, no imports from `mah`)

The preprocessor and the lowerer import this at top level. It must not import
`manifest.py`, `typecheck` or anything else from `mah`, to keep imports acyclic.

```python
PKG_PREFIX = "pkg:"
NAME_RE = re.compile(r"[a-z][a-z0-9_-]*")

def package_of_path(path) -> tuple[str, str, str] | None:
    """(project_root, name, rel) when abspath(path)'s components contain
    `.mah`, `packages`, NAME, then at least one more component; the FIRST
    such occurrence counts. rel uses '/'. Purely lexical: no file access."""

def package_label(path) -> str | None:
    """'pkg:NAME/REL' for a file inside an installed package, else None."""
```

### 4.5 Where the errors are reported (`mah/preprocessor.py`)

- `_resolve_import(base_dir, literal)` keeps its signature. Its only change is to return
  `(literal, False)` for a `pkg:` literal before any filesystem lookup. The LSP's
  `_build_reverse_import_graph` keeps calling it, so `pkg:` edges are simply absent there,
  which is fine because package files are never renamed.
- In `preprocess`'s directive handling, a `pkg:` literal goes to the lazily created
  context's `resolve(fpath, literal)` instead. On error:
  - in the entry file, the existing `errors.append((message, str_tok.start,
    len(str_tok.value)))`, replacing the generic `cannot find imported file` message;
  - in any other file whose `root` isn't None,
    `errors.append((f"{message} (in '{source_label(fpath)}')", root.offset,
    root.length))`, the same pattern as the `extern fn` error.
  - The import's `ImportSite`/`NamespaceImport` record (entry file) gets `resolved=None,
    exists=False`, as for a missing file.
- The two relative-import errors for package files (4.3) are reported the same way.
- `source_label(path)` returns `package_label(path)` when it isn't None. That check goes
  after the prelude/std checks and before the basename fallback.

The CLI already raises the first `pp.errors` entry as a `SyntaxError` located `at position
#L:C` in the entry file (`mah/cli/main.py`, `driver._parse_and_resolve`), so these errors
reach `mah run/build/check/test` unchanged.

---

## 5. The lock file `mah-lock.toml`

Written only by `mah install` (`packages.lock_text`/`write_lock`), always in exactly this
form. The writer is hand-written. Each string is written as `json.dumps(s)`, which is a
valid TOML basic string (non-ASCII becomes `\uXXXX`):

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

- Packages are sorted by name, with every package (direct and transitive) listed.
- Within a package the keys appear in this order: `name`, `github`, then at most one of
  `tag`/`branch`/`rev` (none for the default branch), `path` only when not empty,
  `commit`, `hash`, `dependencies`.
- `dependencies` is the sorted names from the package's own manifest (`[]` without one),
  written `["a", "b"]`.
- A blank line separates blocks, and the file ends with one `\n`. A project whose lock has
  no packages is just the header comment, a blank line and `version = 1\n`.
- Written atomically: a temp file in the same directory, then `os.replace`.

**Reading** (`read_lock(root) -> dict[str, LockedPackage] | None`): None if the file doesn't
exist. Parse with `tomllib`.
- `version` greater than 1 raises `PackageError("mah-lock.toml was written by a newer mah
  (lock version N); upgrade mah")`.
- Any other problem raises `PackageError("mah-lock.toml is invalid (DETAIL); delete it and
  run `mah install`")`. DETAIL is the `tomllib` message, or one of `missing version`,
  `package N: missing KEY`, `package N: bad KEY`, `duplicate package 'N'`. Field checks
  reuse `parse_dependency`'s rules (a failure is `package N: bad KEY`); `commit` must be 40
  lowercase hex characters; `hash` must match `sha256:[0-9a-f]{64}`.

```python
@dataclass(frozen=True)
class LockedPackage:
    dep: Dependency
    commit: str
    hash: str
    dependencies: tuple[str, ...]
```

**In sync** (`lock_out_of_date(deps, lock) -> str | None`): go through the root's
`[dependencies]` in sorted name order. The first NAME missing from the lock gives `'NAME'
was added`; the first whose `dep.spec()` differs from the lock entry's gives `'NAME' was
changed`. Otherwise None. Extra lock entries are never "out of date" (they're transitive,
or leftovers that `mah install` prunes).

---

## 6. Installed state `.mah/installed.toml`

Same writer and layout as the lock, with the header comment `# written by \`mah install\`;
delete .mah/ to reinstall everything` and without the `dependencies` key. It records
what's actually in `.mah/packages/`. Compilation (4.3 #8) compares it to the lock without
reading the package files. `mah install` always rehashes (7.4), so a hand-edited package
is noticed and repaired by `mah install`, not by `mah run`.
`read_installed(root)` returns `{}` if the file is missing **or invalid** (that's treated
as "nothing installed").

---

## 7. Content hash and fetching

### 7.1 The content hash

It covers the package root's regular files: their relative paths (`/`-separated, UTF-8)
and exact bytes. Not covered: file modes, symlinks, submodules, empty directories, and
anything outside `path`.

```python
def hash_files(entries) -> str:          # entries: iterable of (rel: str, data: bytes)
    h = hashlib.sha256()
    for rel, data in sorted(entries, key=lambda e: e[0].encode("utf-8")):
        h.update(rel.encode("utf-8") + b"\0" + str(len(data)).encode("ascii") + b"\0" + data)
    return "sha256:" + h.hexdigest()

def hash_tree(directory) -> str:         # os.walk; regular files only (not symlinks);
    ...                                  # symlinked dirs are not followed; rel with '/'
```

Pinned values, which a test asserts:
- `hash_files([("src/lib.mh", b"export fn hi() { return 1 }\n"), ("README.md", b"hi\n")])`
  is `sha256:24e49e36d4e16f5d6eb1a8f28697aa0218a8df1660577b347eeef9c8a43b6498`
- `hash_files([("a.mh", b"")])` is
  `sha256:455e7441c84a31e91d1ec696f78618c91d0c1ebd2f3b5fcf6834590e72946675`

### 7.2 Git (`mah/project/fetch.py`)

- **git executable**: `shutil.which("git")`. If it's missing, `PackageError("mah install
  needs git to fetch packages, and git wasn't found on PATH")`. Only look this up when a
  fetch or ls-remote is actually needed.
- **URL**: `base = os.environ.get("MAH_GITHUB_URL_BASE") or "https://github.com"`, then
  `url = base.rstrip("/") + "/" + github + ".git"`. Tests set
  `MAH_GITHUB_URL_BASE=file:///tmp/.../gh`, with bare repositories at
  `gh/<owner>/<repo>.git`.
- **Environment** (`git_env(url) -> dict`): a copy of `os.environ` with
  `GIT_TERMINAL_PROMPT=0` (never hang on a password prompt). If `GITHUB_TOKEN` is set and
  not empty **and** `url` starts with `https://github.com/`, it also adds an
  `http.extraHeader` through git's environment config: with `n = int(env.get(
  "GIT_CONFIG_COUNT") or 0)`, set `GIT_CONFIG_KEY_{n}=http.extraHeader`,
  `GIT_CONFIG_VALUE_{n}=Authorization: Basic ` + base64 of `x-access-token:TOKEN`, and
  `GIT_CONFIG_COUNT=n+1`. That keeps the token off the command line, and it is never sent
  to any other host. (Git older than 2.31 ignores these variables; the fetch is then
  unauthenticated, which is documented.)
- **Every command** is `subprocess.run([git, "-c", "http.lowSpeedLimit=1000", "-c",
  "http.lowSpeedTime=60", *args], env=git_env(url), stdin=DEVNULL, capture_output=True)`,
  except that `cat-file --batch` gets its input on stdin. A non-zero exit becomes
  `GitError(detail)`, where detail is the first non-empty stderr line with a leading
  `fatal: ` removed.
- **Resolving a ref to a commit** (`resolve_commit(dep) -> str`), using `git ls-remote URL
  PATTERNS...`. Parse the output lines `SHA\tREF` and match REF **exactly**:
  - `tag`: patterns `refs/tags/T` and `refs/tags/T^{}`. Use the `^{}` (peeled) line if
    present, else the plain one. If neither exists: `tag 'T' not found in OWNER/REPO`.
  - `branch`: pattern `refs/heads/B`. If missing: `branch 'B' not found in OWNER/REPO`.
  - `default`: pattern `HEAD`. If missing: `OWNER/REPO has no default branch`.
  - `rev`: no ls-remote; the commit is `rev`.
  - If ls-remote fails: `couldn't reach OWNER/REPO (URL): DETAIL`.
- **Fetching one commit** (`fetch_files(dep, commit, scratch_dir) -> (files, skipped)`):
  1. `git init --bare -q SCRATCH/repo.git`
  2. `git -C SCRATCH/repo.git fetch -q --no-tags --depth=1 URL COMMIT`. On failure: if
     DETAIL contains `not our ref` or `couldn't find remote ref`, the error is `commit C7
     not found in OWNER/REPO`; otherwise `couldn't fetch OWNER/REPO (URL): DETAIL`.
  3. `git -C ... cat-file -t COMMIT` must print `commit`. Otherwise (a tag pointing at a
     tree or blob): `DESCRIBE doesn't point to a commit`.
  4. `git -C ... ls-tree -r -z --full-tree COMMIT`. Entries are `MODE SP TYPE SP OID TAB
     PATH NUL` (bytes); decode PATH as strict UTF-8 (on failure: `OWNER/REPO has a file
     name that isn't UTF-8`). Keep the entries under `path` (all entries when `path` is
     `""`, else those starting with `path + "/"`, with that prefix removed). Of those:
     - mode `100644`/`100755` blobs are files;
     - mode `120000` (symlink) and `160000` (submodule) are **skipped**, and the relative
       path is recorded in `skipped`;
     - any entry with a path component `.mah` is skipped silently.
  5. If no files remain: `DESCRIBE has no files` when `path` is empty, else `'P' isn't a
     directory in OWNER/REPO at C7` (`P` = path, `C7` = first 7 of the commit).
  6. `git -C ... cat-file --batch`, with stdin the blob OIDs joined by `\n` plus a final
     `\n`. The output is, per object, `OID SP blob SP SIZE LF` + SIZE bytes + `LF`.
     Return `[(rel, data)]` in ls-tree order.

  The error messages above are raised as `PackageError` by `install`, prefixed with
  `package 'NAME': ` (7.3).

### 7.3 `mah install` (`mah/project/install.py`, `install(root, frozen, update) -> int`)

The CLI is `mah install [DIR] [--frozen] [--update [NAME ...]]`:
- Add `"install"` to `_SUBCOMMANDS`.
- `DIR` must contain the manifest; with no `DIR`, search upward from cwd. The messages
  are the ones `_resolve_project` prints, but `install` does **not** require the entry
  file to exist (library-only projects).
- `--update` is `nargs="*"`: `None` when not given, `[]` for all packages, or names.
- `--frozen` together with `--update` is a usage error (exit 2):
  `error: --frozen and --update can't be used together`.

Exit codes: 2 for a missing or invalid manifest and usage errors; 1 for any install
failure; 0 on success. Errors are printed as `error: MESSAGE` on stderr. The normal output
goes to stdout, one line per event.

**Algorithm**:

1. Load the project. `lock = read_lock(root)` (a `PackageError` gives exit 1). `installed
   = read_installed(root)`.
2. If `project.dependencies` is empty and `lock is None`: print `no dependencies to
   install`, return 0, and create nothing.
3. With `--frozen`, check the lock: if it's None, the error is ``--frozen: there's no
   mah-lock.toml; run `mah install` first``; if `lock_out_of_date` gives a reason R, the
   error is `--frozen: mah-lock.toml is out of date with mah-project.toml (R)`.
4. With `--update NAMES`, each name must be in `project.dependencies` or in the lock; if
   not, `error: --update: no package named 'X'` with exit 2.
5. Make `.mah/` and `.mah/packages/`; write `.mah/.gitignore` (`*\n`) if it's missing.
6. **Choose packages.**
   - *Frozen*: the chosen set is every lock entry, in sorted order, each with a known
     commit and expected hash. Install each (7.4). Then, if some package's manifest
     dependency names (sorted) differ from its lock `dependencies`, the error is
     `--frozen: mah-lock.toml is out of date (package 'N' now depends on [a, b])` (the
     list written `[` + `", ".join` + `]`).
   - *Otherwise*, a breadth-first walk:
     ```
     root = project.dependencies
     requested = {}                         # name -> (Dependency, requester name)
     queue = sorted(root)
     chosen = {}                            # name -> LockedPackage, in processing order
     noted = set()
     while queue:
         name = queue.pop(0)
         if name in chosen: continue
         dep = root[name] if name in root else requested[name][0]
         updating = update is not None and (update == [] or name in update)
         locked = lock.get(name) if lock and not updating else None
         if locked and locked.dep.spec() == dep.spec():
             commit, expected = locked.commit, locked.hash
         else:
             commit, expected = resolve_commit(dep), None
         hash_ = ensure_installed(dep, commit, expected)        # 7.4
         sub = dependencies of .mah/packages/name (its manifest's, sorted; none without one)
         for s in sub:                       # s: Dependency
             if s.name in root:
                 if s.spec() != root[s.name].spec() and (name, s.name) not in noted:
                     print note (below); noted.add((name, s.name))
             elif s.name in requested:
                 if s.spec() != requested[s.name][0].spec(): conflict error (below)
             else:
                 requested[s.name] = (s, name); queue.append(s.name)
         chosen[name] = LockedPackage(dep with name, commit, hash_, tuple(x.name for x in sub))
     ```
     A package's manifest is read with `load_project`; a failure is `package 'NAME' has
     an invalid mah-project.toml: MSG` (as in 3).
     - **Note**: `note: 'P' asks for 'N' as DESC_P, but mah-project.toml chooses DESC_ROOT`
     - **Conflict**: `packages 'A' and 'B' both depend on 'N' but ask for different
       versions: DESC_A vs DESC_B; add 'N' to your [dependencies] to choose one`. Here A
       is the earlier requester, B the current package, and DESC is `Dependency.describe()`.
7. **Prune**: every directory in `.mah/packages/` that isn't chosen is removed
   (`shutil.rmtree`), printing `removed NAME`, in sorted order.
8. Write `.mah/installed.toml` for the chosen packages.
9. Unless `--frozen`: if the new lock text equals the existing file's text, print
   `mah-lock.toml is up to date`; otherwise write it and print `wrote mah-lock.toml`.
10. Remove `.mah/tmp/` (also on failure, in a `finally`).

Any `PackageError`/`GitError` from steps 6 to 9 is printed as `error: package 'NAME':
MESSAGE` and gives exit 1. NAME is the package being processed; the conflict, `--frozen`
and lock errors have no `package 'NAME': ` prefix. On failure the lock is never written.

### 7.4 `ensure_installed(dep, commit, expected) -> hash`

```
dir = .mah/packages/<name>;  state = installed.get(name)
if dir is a directory:
    h = hash_tree(dir)
    if expected is not None and h == expected:                      -> "kept"
    if expected is None and state and state.commit == commit
       and state.dep.spec() == dep.spec() and h == state.hash:       -> "kept"
    changed = state is not None and state.commit == commit
              and state.dep.spec() == dep.spec() and h != state.hash
files, skipped = fetch_files(dep, commit, .mah/tmp/<name>)           (network)
h = hash_files(files)
if expected is not None and h != expected:
    error: "content hash mismatch at C7: mah-lock.toml has EXPECTED, the fetched files
            hash to H; if the repository's history was rewritten, run `mah install --update NAME`"
before the FIRST replacement in this run: delete .mah/installed.toml (if present)
write files into .mah/tmp/<name>/tree/ (makedirs; binary writes), then
shutil.rmtree(dir) if it exists, os.replace(tree, dir)
for each skipped path: print "note: NAME: skipped PATH (only regular files are installed)"
-> "reinstalled" if changed else "installed"
```

Deleting `installed.toml` before the first replacement means a crash partway through
leaves compilation saying "isn't installed" (4.3 #8) instead of trusting stale state.

The output line for each package, in processing order, is `STATUS NAME C7 (DESCRIBE)`,
with STATUS `installed`, `kept` or `reinstalled`; `reinstalled` lines end with `: its
files had been changed`. Example:

```
installed greet 3aaab8a (acme/greet tag v1)
kept util 9f00e12 (acme/mono branch main, path packages/util)
removed old
wrote mah-lock.toml
```

**Offline**: when every chosen package is `kept` and no ref needs resolving (all come
from an in-sync lock), `mah install` runs no git command at all, and doesn't need git
installed.

---

## 8. What run / build / test / check do

They change only through the preprocessor (section 4). Missing or stale dependencies
are compile errors at the `pkg:` import, with the messages from 4.3, located like any
preprocess error, for example:

```
SyntaxError: package 'greet' isn't installed; run `mah install` at position #1:19
```

(This is the existing `pp.errors` rendering in `mah/cli/main.py`/`driver.py`.) A built
`.mahc` contains the package code and runs without `.mah/` (`mah runc`, both VMs).

`mah test` discovery and `mah format` already skip dot-directories, so `.mah/` is never
tested or formatted (pinned by tests). The LSP's `_find_mh_files` skips them too.

**Type diagnostics in package files** (decision 11): add
`drop_package_diagnostics(pp, diagnostics)` in `mah/compiler/driver.py`, which keeps a
diagnostic unless `package_of_path(pp.map_to_source(d.position)[0])` is not None. It is
applied in `compile_to_program` (before `reportable`), in `type_check` (so `mah check`
neither prints nor counts them), and in `mah/lsp/analysis.py` `get_diagnostics` before its
`typecheck.reportable` loop.

---

## 9. Locations in messages and bytecode

- `source_label(path)` gives `pkg:NAME/REL` for package files (4.5). Compile-error
  locations read `pkg:greet/src/lib.mh#3:5`.
- `mah/bytecode/lower.py`'s DEBUG file name: add a branch after the prelude/std one, `elif
  package_label(path) is not None: name = package_label(path)`. Runtime errors raised in
  package code are then located `pkg:greet/src/lib.mh#L:C` on **both** VMs (each just
  prints the DEBUG string). `pkg:` is **not** library code: `_is_library_path` in
  `code_interpreter.py` and `exec.rs:888` stay as they are, so an error thrown inside a
  package is located inside the package, the same as user code.
- `mah/cli/test_runner.py` `_display_file`: treat `file.startswith("pkg:")` like `std:` and
  return it unchanged.

---

## 10. LSP (`mah/lsp/analysis.py`)

These need no code change, since they go through `preprocess`: diagnostics (4.3 errors at
the literal), go-to-definition on `"pkg:..."` (jumps to `resolved`), and hover on package
symbols (`*declared in \`pkg:greet/src/lib.mh\`*` through `source_label`). Changes:

1. **Completion** in an import string (`_import_path_completions`):
   - When `partial` has no `/` and (`partial.startswith("pkg:")` or
     `"pkg:".startswith(partial)`), add one item per importable name. The project is the
     context found from `path` as in 4.2. The names are the root's `[dependencies]` when
     the file isn't in a package, else that package's lock `dependencies` plus itself.
     Each item is `{"label": "pkg:NAME", "kind": COMPLETION_MODULE, "detail":
     dep.describe(), "filterText": "pkg:NAME", "textEdit": {range quote..cursor,
     "newText": "pkg:NAME"}}`, built like `_std_module_completions`. Anything that fails
     (no project, invalid manifest or lock) adds nothing.
   - When `partial` starts with `pkg:NAME/` and NAME is importable, list entries of
     `.mah/packages/NAME/<typed dir>` exactly like the relative-path branch does (folders,
     `.mh` files without the extension, excluding `.test.mh`). If the directory doesn't
     exist, the list is empty.
   - A `partial` starting with `pkg:` never falls through to the relative-path listing.
2. **Rename** refuses (returns None) when the declaration is in a package file: in
   `_rename_type_cross_file` add `or package_of_path(decl_path) is not None` to the
   prelude/std check. Apply the same check at the start of `_rename_variable_cross_file`
   and wherever else rename refuses std declarations (the coder greps for `STD_DIR` /
   `PRELUDE_PATH` in rename paths).

---

## 11. Files to change (complete list)

| file | change |
|---|---|
| `mah/project/package_paths.py` (new) | `PKG_PREFIX`, `NAME_RE`, `package_of_path`, `package_label` (4.4) |
| `mah/project/manifest.py` | `Dependency`, `parse_dependency`, `[dependencies]` validation, `[package] lib`, `Project.lib`, `Project.dependencies: dict[str, Dependency]` (section 2) |
| `mah/project/packages.py` (new) | `LOCK_NAME = "mah-lock.toml"`, `PackageError`, `LockedPackage`, `read_lock`/`lock_text`/`write_lock`, `read_installed`/`write_installed`, `lock_out_of_date`, `hash_files`/`hash_tree`, `library_entry`, `PackageContext` (`find`, `resolve`, `importable_names(importer)` for the LSP) |
| `mah/project/fetch.py` (new) | `GitError`, `git_env`, `repo_url`, `resolve_commit`, `fetch_files` (7.2) |
| `mah/project/install.py` (new) | `install(root, frozen, update, out=sys.stdout, err=sys.stderr) -> int` (7.3, 7.4) |
| `mah/preprocessor.py` | `pkg:` resolution and error reporting, package-relative checks, `source_label` (4.5) |
| `mah/bytecode/lower.py` | DEBUG name for package files (9) |
| `mah/compiler/driver.py` | `drop_package_diagnostics`, used in `compile_to_program` and `type_check` (8) |
| `mah/cli/main.py` | `install` subcommand, `_SUBCOMMANDS` (7.3) |
| `mah/cli/test_runner.py` | `_display_file` `pkg:` (9) |
| `mah/lsp/analysis.py` | completion, rename refusal, diagnostics filter (8, 10) |
| `mah/project/templates/gitignore` | add `.mah/` |
| `mah/project/templates/mah-project.toml` | replace the `[dependencies]` comment (12) |
| `mah/project/templates/AGENTS.md` | `[dependencies]` bullet, `mah install` in Commands, `pkg:` in "Writing Mah here" (12) |
| `mah/project/templates/docs/mah-language.md` | one paragraph after the `std:` modules paragraph (12) |
| `tests/test_project.py` | `test_nonempty_dependencies` now expects the new message for `foo = "1.0"` |
| `tests/test_packages.py` (new), `tests/test_lsp_packages.py` (new) | section 13 |
| docs | section 12 |

**Don't touch**: `runtime/` (no Rust change), `mah/code_interpreter.py`, the bytecode
format and MINOR, `examples/` (an example would need a fetched package; see 13), the
tree-sitter/editor grammars (a `pkg:` import is an ordinary string literal).

---

## 12. Docs

- **`docs/PACKAGES.md`** (new, normative user and implementer reference): sections 2 to 8
  in prose. That's the manifest syntax, `pkg:` imports and the library entry, the scoping
  rule, `mah install` and its flags and output, the lock and its fields, `.mah/`, the hash
  definition, transitive rules with the root override, the environment variables
  (`MAH_GITHUB_URL_BASE`, `GITHUB_TOKEN`, proxies through git, `GIT_TERMINAL_PROMPT`), the
  error messages, and the out-of-scope list.
- **`docs/V2_DESIGN.md`**: a new numbered entry after M42's, "**M43 — packages from GitHub
  repositories. ✅ Landed.**", in the M38/M39 style (decisions, files, "no bytecode
  change", test changes, a pointer to this contract). Update "## Status": M43 has landed,
  `pkg:` packages via `mah install` are listed in the summary, and the "what comes next"
  sentence drops packages.
- **`docs/NEXT_PHASES.md`**: the "Packages from GitHub repositories" section starts with
  `Landed as M43 -- see docs/V2_DESIGN.md's M43 entry and docs/PACKAGES.md.`, the same
  way landed sections are marked elsewhere in that file (e.g. "Landed as M20 -- see ..."),
  keeping the text below it. Add a short note of what is deferred
  (section 15).
- **`docs/STDLIB.md`** Phase 0 item 1 (`std:` resolution): one sentence saying `pkg:` is
  the other reserved prefix (docs/PACKAGES.md).
- **`README.md`**: `mah install` in the projects command block (around `mah test`), and
  `docs/PACKAGES.md` in the docs list.
- **`www/src/content/docs/projects.md`**: the manifest example's `[dependencies]` shows one
  commented entry; replace the "reserved" bullet; add `mah install` to Commands; add a
  `## Packages` section (syntax, `pkg:` imports, lock, `.mah/`, `--frozen`/`--update`).
  **`www/src/content/docs/modules-imports.md`**: a short "Packages" paragraph linking to
  Projects. No new www page or nav change.
- **Templates**:
  - `mah-project.toml`: replace the last comment with:
    ```
    # Packages from GitHub, fetched by `mah install` into .mah/ (pinned in mah-lock.toml,
    # which you commit):
    #   json5 = { github = "owner/repo", tag = "v1.0.0" }    # or branch = "main", or rev = "<commit>"
    #   utils = { github = "owner/monorepo", branch = "main", path = "packages/utils" }
    # Import them as `import json5 from "pkg:json5"`.
    [dependencies]
    ```
  - `AGENTS.md`: the `[dependencies]` bullet says this; Commands gains `mah install   #
    fetch [dependencies] into .mah/ and write mah-lock.toml` (and `--frozen`); "Writing Mah
    here" gains: packages are imported as `"pkg:NAME"` / `"pkg:NAME/file.mh"`, and don't
    edit `.mah/`.
  - `docs/mah-language.md`: after the `std:` paragraph, one paragraph in prose with
    **inline code only**, no ```` ```mah ```` block, because
    `test_every_mah_code_block_in_template_docs_compiles` would try to compile it without
    an installed package.
  - Keep `test_agent_docs_match_manifest_entry` and the other template tests green.

---

## 13. Tests

All of these run **without network**. Git-using tests are skipped
(`unittest.skipUnless(shutil.which("git"), "needs git")`) when git is missing; everything
else doesn't need git.

**Fixtures** (in `tests/test_packages.py`):
- `FakeGitHub(tmpdir)` creates `tmpdir/gh`. `make_repo(owner, repo, files: dict[str,
  str|bytes]) -> sha` runs `git init -q -b main` in a work dir, writes the files, commits,
  and `git clone -q --bare` to `gh/owner/repo.git` (the work dir is kept with the bare repo
  as its `origin`). `commit(owner, repo, files, remove=()) -> sha` adds a commit on `main`
  and pushes. `tag(owner, repo, name, sha=None, annotated=False)` tags and pushes. Also
  `branch(owner, repo, name, sha)` and `symlink(owner, repo, link, target)`. Every git call
  passes `-c user.name=t -c user.email=t@t -c commit.gpgsign=false -c tag.gpgsign=false`.
- During each test, the environment has `MAH_GITHUB_URL_BASE="file://" + gh`, and
  `GITHUB_TOKEN` and `GIT_CONFIG_COUNT` removed (restored afterwards).
- `project(tmp, deps_toml, main_src)` writes a project (manifest with the given
  `[dependencies]` body, `src/main.mh`).
- `fake_install(root, name, files, dep=Dependency(...), deps=())` (no git) writes
  `.mah/packages/name/`, then lock and installed entries with `commit="a"*40` and
  `hash_files(files)`. It's used by the compile-only and LSP tests.
- CLI calls use the `_run_main` + `_chdir` pattern from `tests/test_project.py`. Runs use
  `["run", "--vm", vm]` with `vm = "rust" if os.environ.get("MAH_TEST_VM") == "rust" else
  "python"`, and are skipped on rust when `mah-vm` isn't found (as `tests/support.py`
  does).

The standard packages used below:
- `acme/greet`: `mah-project.toml` (`[package] name = "greet"`, `version = "1.0.0"`) and
  `src/lib.mh` = `export fn hello(name) { return "hello " + name }\n`;
  `src/extra.mh` = `export let answer = 42\n`; a `src/lib.test.mh`; and a `README.md`.

**Manifest** (`ManifestDependencyTests`, no git):
1. Each of the four forms in section 2 parses to the expected `Dependency` fields;
   `rev` is lowercased; `path = "lib/"` gives `"lib"`; `describe()` strings exactly as in 2.3.
2. Every row of the 2.1 table gives its exact message, one test each (including
   `foo = "1.0"`, `Foo = {...}`, `{ github = "acme/greet.git" }`, `{ github = "acme" }`,
   `{ github = "acme/greet", tag = "v1", branch = "main" }`, `rev = "abc"`, and paths
   `""`, `"/x"`, `"a/../b"`, `"a\\b"`, `"./a"`).
3. `[package] lib = "lib/main.mh"` is reflected in `Project.lib`; `lib = 3` gives `package.lib
   must be a string`; the default is `src/lib.mh`.

**Lock, hash and paths** (no git):
4. The two pinned `hash_files` values (7.1); `hash_tree` of a directory with those files
   gives the same value as `hash_files`, ignores a symlink in it, and changes when one byte
   changes.
5. `lock_text` of the two-package example in section 5 (with concrete commits and hashes)
   equals a literal expected string. `read_lock(write_lock(x)) == x`. A lock with `version
   = 2` gives the "newer mah" message; a lock with a missing `commit` gives `mah-lock.toml
   is invalid (package greet: missing commit); delete it and run `mah install``.
6. `package_of_path`/`package_label`: `/p/.mah/packages/greet/src/lib.mh` gives
   `("/p", "greet", "src/lib.mh")` and `pkg:greet/src/lib.mh`; `/p/src/a.mh` gives None;
   `/p/.mah/packages/greet` (no further component) gives None.
7. `lock_out_of_date`: added, changed (tag v1 to v2), in sync, and an extra lock entry
   is in sync.
8. `git_env`: `GIT_TERMINAL_PROMPT == "0"` always. With `GITHUB_TOKEN=tok` and URL
   `https://github.com/a/b.git`, `GIT_CONFIG_COUNT == "1"`, `GIT_CONFIG_KEY_0 ==
   "http.extraHeader"` and `GIT_CONFIG_VALUE_0 == "Authorization: Basic eC1hY2Nlc3MtdG9rZW46dG9r"`.
   With a pre-existing `GIT_CONFIG_COUNT=2`, it uses index 2 and sets the count to 3. With
   a `file://` URL, or no token, no `GIT_CONFIG_*` is added.

**Compile-time resolution** (`fake_install`, no git; run on the `MAH_TEST_VM` VM):
9. `import greet from "pkg:greet"\nprint(greet.hello("mah"))` prints `hello mah`.
   `import "pkg:greet/src/extra"\nprint(answer)` prints `42`. The same file imported
   through `pkg:greet` and through `pkg:greet/src/lib.mh` is inlined once (the program
   compiles, and calling it twice works).
10. A package without a manifest and with `lib.mh` resolves `pkg:NAME` to `lib.mh`. A
    package with `lib = "main.mh"` in its manifest resolves to `main.mh`. A package whose
    lib file is missing gives the "has no library file src/lib.mh" message.
11. Each 4.3 error, with its exact text and location `at position #1:N` (N = the column of
    the opening quote, as for `cannot find imported file`): an invalid literal (`pkg:`,
    `pkg:Foo`, `pkg:greet/../x`); no project (a lone file in a temp dir); not in
    [dependencies], with and without `(it's installed only for 'a')`; no lock; lock out of
    date (added and changed); not installed (`.mah/` deleted); installed doesn't match
    (installed.toml commit edited); `pkg:greet/nope` gives `package 'greet' has no file
    'nope.mh'`; `pkg:greet/src/lib.test.mh` gives `can't import the test file`.
12. Non-entry errors: `src/api.mh` imports `pkg:missing`, and `src/main.mh` imports
    `"api.mh"`. The error is `'missing' isn't in the [dependencies] of mah-project.toml (in
    'api.mh')`, located at main.mh's import of `"api.mh"`.
13. Package scoping: package `a` (deps `[b]` in the lock) has `src/lib.mh` importing
    `pkg:b`, which works. Package `a` importing `pkg:c` (installed, not in its deps) gives
    `package 'a' imports 'pkg:c', but 'c' isn't in its [dependencies] (in
    'pkg:a/src/lib.mh')`. A package's `lib.mh` with `import "../../../src/main.mh"` gives
    `import '../../../src/main.mh' leaves package 'a'; import other packages as
    "pkg:NAME" (in 'pkg:a/src/lib.mh')`. A package importing a missing relative file
    gives `cannot find imported file 'nope' (in 'pkg:a/src/lib.mh')`.
14. Laziness: a project with `[dependencies]` declared, nothing installed and no lock,
    whose main.mh imports no package, runs fine.
15. Runtime location, both VMs: greet's `src/lib.mh` has `export fn boom() { let v = [1];
    return v[5] }`. `mah run` of a main calling `greet.boom()` fails with a
    `MahRuntimeError` whose message contains `pkg:greet/src/lib.mh#1:`. A **compile** error
    inside a package (a parse error in its lib.mh) is located `pkg:greet/src/lib.mh#L:C`.
16. Build: `mah build` (project) writes a `.mahc` whose DEBUG files list contains
    `pkg:greet/src/lib.mh` (via `decode(...)` and its strings). After `shutil.rmtree(.mah)`,
    `mah runc` of it still prints `hello mah` (and so does the Rust VM when
    `MAH_TEST_VM=rust`).
17. `mah test` in the project finds only the project's own `*.test.mh` (greet's
    `src/lib.test.mh` isn't run: the count is pinned). `mah format --check` is unaffected by
    a deliberately badly formatted package file.
18. Strict project (`[types] check = "strict"`): greet's lib has a type mismatch that
    `mah check` reports when the same code is in the root. Through `pkg:` it gives no
    error: `mah run` works and `mah check` prints `no type errors`.

**`mah install`** (git, `FakeGitHub`):
19. Tag: `greet = { github = "acme/greet", tag = "v1" }` gives exit 0 and stdout `installed
    greet C7 (acme/greet tag v1)\nwrote mah-lock.toml\n`. The lock's commit is the tagged
    sha and its hash is `hash_files` of every greet file. `.mah/packages/greet/src/lib.mh`
    exists; `.mah/.gitignore` is `*\n`; `.mah/tmp` doesn't exist. Then `mah run` prints
    `hello mah`. A second `mah install` prints `kept greet ...` and `mah-lock.toml is up to
    date`.
20. Annotated tag: the lock's commit is the peeled commit, not the tag object.
21. Branch: the lock pins main's sha. After a new commit on main, `mah install` keeps the
    old commit; `mah install --update` moves to the new one and the output says
    `installed`; `--update greet` does the same, and `--update nope` exits 2 with
    `--update: no package named 'nope'`.
22. `rev` set to the first (non-tip) commit installs exactly that commit's files.
23. Default branch: `{ github = "acme/greet" }` pins the HEAD sha.
24. `path`: monorepo `acme/mono` with `packages/util/lib.mh` (no manifest) and
    `other/x.mh`, and `util = { github = "acme/mono", path = "packages/util" }`. Only
    `lib.mh` is installed (no `other/`); `import u from "pkg:util"` works. `path =
    "nope"` gives exit 1 with `error: package 'util': 'nope' isn't a directory in acme/mono
    at C7`.
25. Frozen: no lock gives exit 1 with the "--frozen: there's no mah-lock.toml" message.
    The manifest changed after install gives the "--frozen: ... out of date ('greet' was
    changed)" message. After `rmtree(.mah)` and a new commit on the branch, `--frozen`
    reinstalls the **locked** commit, and the lock file's bytes and mtime are unchanged.
    `--frozen --update` exits 2 with `--frozen and --update can't be used together`.
26. Offline: after an install, set `MAH_GITHUB_URL_BASE` to a nonexistent `file://` path
    and `PATH` to an empty directory (no git). `mah install` exits 0 with `kept` and `mah
    run` works. With `.mah/` removed as well, the error is `mah install needs git to fetch
    packages, and git wasn't found on PATH` (exit 1, with the `package 'greet': ` prefix).
27. Local edit: change `.mah/packages/greet/src/lib.mh`. `mah install` prints `reinstalled
    greet C7 (acme/greet tag v1): its files had been changed` and the file is restored.
28. Hash mismatch: edit the lock's `hash` to `"sha256:" + "0"*64` and `rmtree(.mah)`. Exit
    1, and the message contains `content hash mismatch at C7: mah-lock.toml has
    sha256:000`.
29. Removal: drop greet from the manifest. Output contains `removed greet`, the directory
    is gone, and the lock has no packages (the exact header and `version = 1` text).
    No deps and no lock: `no dependencies to install`, and no `.mah/` or lock is created.
30. Fetch errors (exit 1, exact text): `tag 'v9' not found in acme/greet`, `branch 'dev'
    not found in acme/greet`, `commit 1111111 not found in acme/greet` (rev of 40 `1`s),
    and an unknown repo starts with `couldn't reach acme/nope (file://`.
31. Transitive: `acme/a`'s manifest depends on `b = { github = "acme/b", tag = "v1" }`, and
    the root depends on `a` only. Both are installed, with output lines in order `installed
    a`, `installed b`; the lock's `a.dependencies == ["b"]`; a's lib importing `pkg:b`
    runs; root main importing `pkg:b` fails with `(it's installed only for 'a')`.
32. Conflict: root depends on `a` and `c`, which ask for `b` tags `v1` and `v2`. Exit 1
    with `packages 'a' and 'c' both depend on 'b' but ask for different versions:
    acme/b tag v1 vs acme/b tag v2; add 'b' to your [dependencies] to choose one`. Adding
    `b = { github = "acme/b", tag = "v2" }` to the root makes it succeed, with stdout
    containing `note: 'a' asks for 'b' as acme/b tag v1, but mah-project.toml chooses
    acme/b tag v2`.
33. Symlink in the repo: it isn't installed, `note: greet: skipped LINK (only regular files
    are installed)` is printed, and the hash ignores it.
34. Invalid package manifest (greet's `mah-project.toml` has an unknown key): `error:
    package 'greet': package 'greet' has an invalid mah-project.toml: unknown key 'bogus'`.
35. Library-only root project (no `src/main.mh`): `mah install` still works.

**LSP** (`tests/test_lsp_packages.py`, `fake_install`, no git):
36. `get_definition` on the `"pkg:greet"` string jumps to `.mah/packages/greet/src/lib.mh`.
37. `get_diagnostics` of `import "pkg:greet"` with no lock contains the exact "no
    mah-lock.toml" message, ranged on the string literal.
38. Completion: inside `import x from "pkg:` it lists `pkg:greet` with detail `acme/greet tag
    v1`; inside `"p` it lists `pkg:greet` as well as the `std:` items; inside `"pkg:greet/`
    it lists the `src` folder; inside `"pkg:greet/src/` it lists `lib` and `extra` (not
    `lib.test`).
39. Hover on `greet.hello` shows `declared in \`pkg:greet/src/lib.mh\``.
40. Rename of `hello` at its use site in main.mh returns None (declared in a package).

**Unchanged suites**: `make test` and `make test-rust` stay green. There's no `vm_diff.py`
case and no `examples/` program, because nothing changes in the VMs and an example would
need a fetched package. Test 16 covers both VMs running package code from a `.mahc`.

---

## 14. Behavior summary

| situation | result |
|---|---|
| `pkg:` import, everything installed | compiles; package code inlined |
| no `pkg:` import anywhere | nothing about packages is checked |
| deps declared, no lock | compile error at the import: run `mah install` |
| manifest dep added or changed since install | compile error: lock out of date |
| `.mah/` missing or different from the lock | compile error: isn't installed / doesn't match |
| package files hand-edited | compiles (not rehashed); `mah install` reinstalls them |
| `mah install`, everything in sync | no git calls, works offline |
| branch moved upstream | ignored until `mah install --update [NAME]` |
| CI | `mah install --frozen` |

---

## 15. Out of scope (for later milestones)

`mah add`/`mah remove`; hosts other than GitHub and `git = "url"` sources; semver ranges and
version solving; a global download cache shared across projects; package registries;
`mah init --lib`; installing several versions of one name; packages bringing natives
(`extern fn` stays `std:`-only, so a package can't bind natives); concurrent `mah install`
runs in one project (not locked against each other); verifying git commit signatures.
