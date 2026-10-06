---
title: std:regex
order: 8
section: Text & data
summary: Regular expressions with named groups, find/replace/split, and flags, matching identically on every runtime.
---

# `std:regex`

Regular expressions: test, find, replace and split text by pattern. The
syntax is the familiar one, restricted to what every runtime matches
identically, so a pattern never behaves differently on the Python VM and
`mah-vm`.

```mah
import regex from "std:regex"

let date = regex.must_compile("(?<y>\\d{4})-(?<m>\\d\\d)")
let m = date.find("due 2026-09, paid 2026-10").unwrap()
print(m.text, m.start, m.group("y"))                      # 2026-09 4 2026
print(date.replace_all("2026-09 2027-01", "$m/$y"))       # 09/2026 01/2027
```

A Mah String has its own escapes, so the pattern `\d+` is written
`"\\d+"` in source.

## Compiling: `compile` vs `must_compile`

A pattern is compiled once into a `regex.Regex`, then used many times.

- **`must_compile(pattern, flags = "")`** is for patterns written into your
  program. A mistake there is a bug, so it throws a `RuntimeError` that the
  type checker doesn't ask you to handle (like Go's `MustCompile`).
- **`compile(pattern, flags = "")`** is for patterns that come from
  outside (user input, config). It throws a `regex.RegexError` saying what's
  wrong and where, which you should catch:

```mah
import regex from "std:regex"

fn user_pattern(p: String) -> Option<regex.Regex> {
    try {
        some(regex.compile(p))
    } catch {
        e: regex.RegexError => {
            print(e.message())               # missing ) at position 0 in "(\d+"
            none
        }
    }
}
print(user_pattern("(\\d+"), user_pattern("\\d+").is_some())   # none true
```

## Matching

```mah
import regex from "std:regex"

let word = regex.must_compile("\\b\\w+\\b")
print(word.is_match("  hi "), word.is_match("  "))     # true false
print(word.find("  hi there").unwrap().text)            # hi
let all = word.find_all("one two three")
print(all.len(), all.map(fn(m) { m.text }).reduce())   # 3 [one, two, three]
```

A `regex.Match` has `text`, `start` and `end` (positions count characters,
like String indexing), and `group(n)` / `group(name)`. Group 0 is the
whole match. A group that didn't take part gives `none`:

```mah
import regex from "std:regex"

let kv = regex.must_compile("(\\w+)(?:=(\\w*))?")
let a = kv.find("debug").unwrap()
let b = kv.find("level=3").unwrap()
print(a.group(1), a.group(2), b.group(1), b.group(2))   # debug none level 3
```

## Replacing

`replace` changes the first match and `replace_all` every match. The
replacement is either a template (`$1`, `$name`, `${name}`, `$$` for a
literal `$`) or a **function** of the Match:

```mah
import regex from "std:regex"

let money = regex.must_compile("\\$(\\d+)")
print(money.replace_all("cost: $5, tax: $1", "$1 USD"))   # cost: 5 USD, tax: 1 USD
let caps = regex.must_compile("cat", "i")
print(caps.replace_all("Cat CAT cat", fn(m) { m.text.to_lower() }))   # cat cat cat
let n = regex.must_compile("\\d+")
print(n.replace_all("a1b22", fn(m) { (m.text.to_number() * 2).to_string() }))   # a2b44
```

## Splitting

`split(text, limit = none)` gives the pieces between matches, at most
`limit + 1` of them:

```mah
import regex from "std:regex"

let sep = regex.must_compile("\\s*,\\s*")
print(sep.split("a , b,c"))                 # [a, b, c]
let two = sep.split("a, b, c", 1)
print(two.len(), two[1])                    # 2 b, c
```

To match a piece of text literally, escape it: `regex.escape("1+1=2")`
gives a pattern that matches exactly `1+1=2`.

## Syntax

| Pattern | Matches |
|---|---|
| `.` | any character except a newline (with flag `s`: any) |
| `[abc]` `[^a-z]` | a class / a negated class |
| `\d` `\w` `\s` | ASCII digit, word character `[0-9A-Za-z_]`, whitespace |
| `\D` `\W` `\S` | their opposites |
| `^` `$` | start / end of the text (with flag `m`: of each line) |
| `\A` `\z` | start / end of the text, always |
| `\b` `\B` | a word boundary / not one |
| `*` `+` `?` | 0 or more, 1 or more, 0 or 1 (add `?` for lazy: `*?`) |
| `{n}` `{n,}` `{n,m}` | counted repeats, up to 1000 |
| `(x)` `(?:x)` `(?<name>x)` | capturing, non-capturing, and named groups |
| `a\|b` | alternatives |
| `\.` `\\` `\n` `\t` | escapes: any punctuation after `\` is literal |

Flags are a String of letters passed to `compile`: `"i"` (ASCII letters
match either case), `"m"` (`^`/`$` at every line), `"s"` (`.` matches
newlines too).

**Not supported**: lookahead/lookbehind, backreferences, and inline flags
like `(?i)`. A pattern that uses them is a `RegexError`, never something
that silently behaves differently on another runtime.

## Reference

| Function | |
|---|---|
| `compile(pattern, flags = "")` | a `Regex`; throws `RegexError` |
| `must_compile(pattern, flags = "")` | a `Regex`; a bad pattern is a RuntimeError |
| `escape(text)` | a pattern matching exactly `text` |

| `Regex` method | |
|---|---|
| `is_match(text)` | whether it matches anywhere |
| `find(text)` | the first `Match`, as an `Option` |
| `find_all(text)` | every `Match`, left to right |
| `replace(text, with)`, `replace_all(text, with)` | `with` is a template or `fn(Match) -> String` |
| `split(text, limit = none)` | the pieces between matches |

A `Regex` also has `pattern`, `flags`, `source` (its canonical form),
`groups` (how many) and `names`. A `Match` has `text`, `start`, `end` and
`group(key)`. `regex.RegexError { message, pattern, position }` is
`compile`'s error; misusing a compiled Regex (a group it doesn't have, a
bad template) is a `RuntimeError.ArgumentError`.
