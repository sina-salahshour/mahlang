---
title: std:url
order: 10
section: Text & data
summary: Parse, build and resolve URLs, and percent-encode text and query strings.
---

# `std:url`

Parse URLs into their parts, resolve relative links, and percent-encode
text and query strings. [`std:http`](/std/http) uses it for every request,
and you'll want it whenever you build a URL from user data.

```mah
import url from "std:url"

let u = url.parse("https://example.com:8443/docs/a?x=1#top")
print(u.scheme, u.host, u.port, u.path)      # https example.com 8443 /docs/a
print(u.query, u.fragment)                   # x=1 top
```

## Parsing

`parse(text)` reads an absolute URL into a `url.Url` with `scheme`,
`username`, `password`, `host`, `port`, `path`, `query` and `fragment`.
`port`, `query` and `fragment` are `none` when absent; `username` and
`password` are `""`.

```mah
import url from "std:url"

let u = url.parse("http://ann:secret@db.local/data")
print(u.port, u.effective_port(), u.username)     # none 80 ann
print(u.origin(), u.request_target())             # http://db.local /data
print(url.is_valid("not a url"), url.is_valid("mailto:a@b.c"))   # false true
```

`parse` throws `url.UrlError` (kind `"invalid_url"`) for text with no
scheme, spaces or control characters, a port that isn't a number up to
65535, or an `http`/`https` URL without a host. Use `is_valid` for a quick
check.

## Resolving relative links

`u.resolve(reference)` gives the URL a link points to when it appears on
the page at `u`, following RFC 3986. It's what you need when following
links in an HTML page or a `Location` header:

```mah
import url from "std:url"

let page = url.parse("https://example.com/docs/guide/intro.html")
print(page.resolve("setup.html"))          # https://example.com/docs/guide/setup.html
print(page.resolve("../api?v=2"))          # https://example.com/docs/api?v=2
print(page.resolve("/"), page.resolve("//cdn.example.com/x.js"))
# https://example.com/ https://cdn.example.com/x.js
```

## Query strings

`encode_query(params)` builds a query string from a Map (or a Vector of
`[key, value]` pairs, to control order or repeat a key). Spaces become
`+`, other special characters `%XX`. `parse_query` reads one back as
decoded pairs:

```mah
import url from "std:url"

print(url.encode_query(["q": "mah lang", "page": 2]))       # q=mah+lang&page=2
print(url.encode_query([["tag", "a"], ["tag", "b&c"]]))     # tag=a&tag=b%26c
print(url.parse_query("a=1&b=x+y&a=2"))                     # [[a, 1], [b, x y], [a, 2]]

let base = url.parse("https://example.com/search")
print(base.with_query(["q": "café"]))                       # https://example.com/search?q=caf%C3%A9
```

A Map value that is a Vector repeats the key: `["tag": ["a", "b"]]` gives
`tag=a&tag=b`. `u.query_pairs()` is `parse_query` on a parsed URL's query.

## Encoding path pieces

`encode(text, safe = "")` percent-encodes everything except letters,
digits and `-._~` (plus the characters in `safe`). Use it for a value you
put into a path. `decode` reverses it; note that it leaves `+` alone (only
query strings treat `+` as a space):

```mah
import url from "std:url"

let name = "my file/1.txt"
print(url.encode(name), url.encode("a b/c", safe: "/"))     # my%20file%2F1.txt a%20b/c
print(try url.decode("caf%C3%A9+x") else "")                # café+x
print(try url.decode("%zz") else "bad escape")              # bad escape
```

## Reference

| Function | |
|---|---|
| `parse(text)` | a `Url`; throws `UrlError` |
| `is_valid(text)` | whether `text` parses |
| `encode(text, safe = "")` | percent-encoded UTF-8 |
| `decode(text)` | `%XX` escapes decoded; throws for a bad escape or invalid UTF-8 |
| `encode_query(params)` | a form-style query string from a Map or pairs |
| `parse_query(query)` | decoded `[key, value]` pairs, in order |
| `default_port(scheme)` | 80, 443, 21, ... or `none` |

| `Url` method | |
|---|---|
| `effective_port()` | `port`, else the scheme's default |
| `authority()` | `[user[:password]@]host[:port]` |
| `origin()` | `scheme://host[:port]`, the port only if not the default |
| `request_target()` | the path (`/` if empty) and query, as an HTTP request line names it |
| `query_pairs()` | the decoded query |
| `resolve(reference)` | the URL a relative or absolute reference points to |
| `with_query(params)` | a copy with the query replaced |

A `Url` prints as its full text. `url.UrlError { kind, text, description }`
has `kind` `"invalid_url"` or `"invalid_encoding"`.
