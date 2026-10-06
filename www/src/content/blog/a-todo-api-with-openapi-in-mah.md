---
title: "A todo API in Mah, documented by its own reflection"
date: 2026-10-06
description: "A REST API with an OpenAPI document and Swagger UI, written in Mah with no framework: routes are decorated functions, std:reflect reads them, and the docs can't drift from the code. Plus what comes next: a backend framework for Mah."
tags: [showcase, reflection, web]
---

v0.3.0 gave Mah [reflection, decorators and hooks](/blog/v0-3-0-reflection-decorators-bytes-networking),
plus TCP, URLs and an HTTP client. To see how those hold up in a real
program, we wrote a small web app with them:
**[mah-todo-demo](https://github.com/sina-salahshour/mah-todo-demo)**, a
todo list with a JSON API, an OpenAPI document and Swagger UI. It's all
Mah, with no framework underneath. This post walks through how it works,
what it turned up, and what we'll build next.

```sh
git clone https://github.com/sina-salahshour/mah-todo-demo
cd mah-todo-demo
mah run          # then open http://127.0.0.1:4000 and /docs
```

## What's in it

- `/` is the todo list: add, tick, rename with a double-click, filter, and
  clear the finished ones. It has light and dark themes, and every action
  is a plain `fetch` against the same API anything else would use.
- `/todos` and `/todos/{id}` are the REST API: `GET`, `POST`, `PUT`,
  `PATCH`, `DELETE`, a toggle, and `DELETE /todos/done`.
- `/openapi.json` is an OpenAPI 3.0 document **written at run time from
  the code**, and `/docs` shows it in Swagger UI, where "Try it out" calls
  the live server.

The HTTP server is about 300 lines of Mah on `std:socket`: it reads a
request line, the headers and a `Content-Length` body, and writes a
response back. The todos live in a variable, so a restart starts with an
empty list.

## A route is a function

This is the whole of one route:

```mah
## Get one todo.
@get("/todos/{id}", errors: [404], tags: ["todos"])
fn get_todo(
    db: store.Store,
    ## The todo's id.
    @path id: Number
) -> store.Todo {
    let todo = db.get(id)
    if todo == none { throw missing(id) }
    todo.unwrap()
}
```

There's no router call and no closure, and the function doesn't touch a
request or a response object. Everything the server needs is in the
declaration, and `std:reflect` can read all of it:

- `@get(...)` is a plain function call that returns a `Route` value. Mah
  evaluates decorators once at startup and keeps them, so
  `reflect.signature(get_todo).decorators` holds that `Route`.
- `@path` before a parameter is a decorator too. Here the decorator is a
  function itself, and `reflect.find(param.decorators, path)` finds it.
- The parameter's **type** is `Number`, so the `"7"` in `/todos/7` is
  converted before the call. `/todos/abc` gets a `400` that says `the id in
  the path must be a number, not abc`, and the handler never runs.
- `db: store.Store` has no decorator, so it's filled with the value of that
  type the app was given. That's how the store gets into the handler.
- The **return type** decides the answer: a struct becomes JSON,
  `Created<Todo>` becomes a `201` with a `Location` header, and `-> None`
  becomes a `204`. A thrown `ApiError` becomes its status code.

### Registration is a hook

The route decorator doesn't just sit there. `Route` implements
`reflect.WrapFn`, so at startup Mah hands it the function it decorates.
The hook records the function and returns it unchanged:

```mah
let declared: Vector<Unknown> = []

impl reflect.WrapFn for Route {
    fn wrap(self, f: Unknown, info: reflect.FnInfo) -> Unknown {
        declared.push(info.function)
        f
    }
}

export fn registered() -> Vector<Unknown> { declared.copy() }
```

So there's no list of routes to keep in sync either. Writing the function
is enough, and the app mounts them like this:

```mah
let api = Api.new("p05 todo api", version)
mount(api, registered(), [db])
```

`mount` reads each signature once. On every request it builds the
arguments as a Map by parameter name and calls
`reflect.call(handler, [], args)`. A query parameter that wasn't sent is
simply left out, so the function's own default applies.

## The OpenAPI document reads the same declarations

Because a route is just a declaration, the documentation can be generated
from it. `openapi.mh` walks the same signatures:

| in the code | in the OpenAPI document |
|---|---|
| the `##` doc above the function | `summary` (first line) and `description` (the rest) |
| the function's name | `operationId` |
| `@path id: Number` with a `##` doc | a required path parameter, `type: number`, with a description |
| `@query done: Option<Bool>` | an optional query parameter, `type: boolean` |
| `@body input: NewTodo` | `requestBody`, a `$ref` to the `NewTodo` schema |
| `-> Created<store.Todo>` | a `201` response with the `Todo` schema and a `Location` header |
| `errors: [404, 422]` | those responses, with the shared `ErrorBody` schema |

Every struct mentioned along the way goes into `components/schemas`, built
from `reflect.schema`. A field's `##` doc becomes its description, an
`Option<...>` field is optional, and an `@example(...)` decorator on a field
becomes the example Swagger UI shows:

```mah
## A todo to add, or the whole of one to put in place of another.
struct NewTodo {
    ## What needs doing. Surrounding whitespace is dropped; it can't be blank.
    @example("buy milk") title: String,
    ## Whether it is already finished (false when left out).
    @example(false) done: Option<Bool>
}
```

Nothing in `openapi.mh` mentions todos. Add a route and it shows up in
`/docs`. Change a type and the schema changes with it. The output also
passes `openapi-spec-validator`.

## Using the standard library

The demo uses the standard library wherever it has something to offer:

- `std:socket` for the server: `listen`, `accept`, `read_line`,
  `recv_exactly`. Each connection is handled in its own `detach`ed task.
- `std:url` decodes the query string and path parameters, so `%20` and `+`
  mean what they should.
- `std:json` writes every response and reads every body, with `parse_as`
  checking the body against the struct's declared types.
- `std:time` gives each todo its `created_at`, and `std:process` reads
  `PORT` and `HOST`.
- `std:http`, the client, drives the **end-to-end tests**. Each test starts
  the real server on a free port and talks to it over the wire:

```mah
test "a real client can use the whole api over the wire" {
    with_server(fn(server: Server) {
        let base = server.url()
        let created = http.post(base + "/todos", json: ["title": "  wash up "])
        assert_eq(created.status, 201)
        assert_eq(created.header("Location"), "/todos/1")
        assert_eq(created.json()["title"], "wash up")
        ...
    })
}
```

There are 80 tests, and they pass on both the Rust VM and the Python
reference VM.

## What the demo turned up

Real programs find real bugs, and this one found two. Both are fixed on
`main`:

- **A flat-imported type inside a function type didn't resolve.** In
  `fn with_server(body: fn(Server))`, `Server` came from `import "./app"`
  and the compiler reported *Unknown type 'Server'*. The pass that renames
  imported names read `fn(Server)` as a closure with a *parameter* called
  `Server`, and left the name alone everywhere after it. It now tells a
  function type from a closure.
- **`mah format` refused files with a `##` doc on a function's only
  parameter.** The layout that keeps `f(a, fn(x) {` together pulled the
  comment up onto the `(` line, where it's no longer a doc. The formatter
  checks its own output, noticed the meaning had changed, and left the
  file alone. That was the right thing to do, but the layout was still
  wrong. It now keeps such a list broken across lines.

## Next: a backend framework for Mah

The demo's `rest.mh` and `openapi.mh` together are about 500 lines. They
work, but they're a sketch of something that should be part of Mah. These
are the next milestones, all following [the roadmap](https://github.com/sina-salahshour/mahlang/blob/main/docs/NEXT_PHASES.md):

1. **An HTTP server in `std:http`.** The server side of the client
   that's already there, sharing its request and response parsing. It
   will add keep-alive, chunked bodies, form and multipart bodies, and
   graceful shutdown. The demo's hand-written `http.mh` is the prototype
   it replaces.
2. **A web framework built on decorators and hooks**, in the spirit of
   NestJS and Hono:
   - controllers and routes declared with decorators, as in the demo;
   - parameter decorators that bind path, query, **header** and body
     values to typed parameters;
   - **validation** from the declared types plus validator decorators
     (`@min_length(1)`, `@range(0, 100)`), using `WrapField` and
     `WrapStruct` so a bad body is rejected before the handler runs;
   - **pipes and transforms** that convert values on the way in, and
     **guards and interceptors** around the handler (auth, logging,
     timing), built as `WrapFn` hooks;
   - Hono-style **middleware** for the cross-cutting parts;
   - dependency injection by type, as the demo does for the store;
   - **OpenAPI and Swagger UI built in**, generated from the same metadata
     with `std:reflect`, as in this demo.
3. **Packages from GitHub repositories.** `[dependencies]` in
   `mah-project.toml` will finally do something: a dependency names a
   repository and a tag or commit, and a lock file pins the exact
   version. The framework will be the first package, so an app like this
   one would start with a dependency line instead of a copy of `rest.mh`.
4. **Async handlers and hooks all the way down,** and later, opt-in
   multithreading for `detach`ed work, so a busy server isn't limited to
   one core.

The goal is that the demo shrinks to its domain: `store.mh`, the
handlers, and a page.

If you build something with Mah, or want to help with any of the above,
the code is on [GitHub](https://github.com/sina-salahshour/mahlang). The
todo demo's source is at
[sina-salahshour/mah-todo-demo](https://github.com/sina-salahshour/mah-todo-demo).
