---
title: std:collections
order: 3
section: Basics
summary: Set, Deque and PriorityQueue, the data structures beyond Vector and Map, all iterable and printable.
---

# `std:collections`

Three data structures that complement the built-in Vector and Map:

- **`Set`**: distinct values, in the order they were first added.
- **`Deque`**: a double-ended queue, push and pop at both ends in
  constant time.
- **`PriorityQueue`**: items come out smallest first (a binary heap).

All three work with `for`, `map`/`filter`/`reduce` and the other iterator
methods, and print with their name. They're usually imported flat:

```mah
import "std:collections"

let tags = Set.of(["red", "blue", "red"])
let line = Deque.of(["ana", "bo"])
let jobs = PriorityQueue.of([5, 1, 3])
print(tags, line, jobs.pop())          # Set[red, blue] Deque[ana, bo] 1
```

## `Set`: distinct values

A Set holds each value once. Values must be usable as Map keys: Strings,
Numbers or Bools.

```mah
import "std:collections"

let seen = Set.new()
for let word in "the cat and the hat".split(" ") {
    seen.add(word)
}
print(seen, seen.len(), seen.has("cat"))    # Set[the, cat, and, hat] 4 true
print(seen.remove("cat"), seen.remove("dog"))   # true false
```

Set algebra returns new Sets:

```mah
import "std:collections"

let a = Set.of([1, 2, 3, 4])
let b = Set.of([3, 4, 5])
print(a.union(b), a.intersection(b), a.difference(b))   # Set[1, 2, 3, 4, 5] Set[3, 4] Set[1, 2]
print(Set.of([3]).is_subset(a), a.equals(Set.of([4, 3, 2, 1])))   # true true
```

`==` on two Sets compares **identity**, like on Vectors and Maps; use
`equals` to compare contents.

A common use is de-duplicating while keeping the first-seen order:
`Set.of(items).to_vector()`.

## `Deque`: a double-ended queue

`Vector.push`/`pop` work at the end only; removing from the front of a
Vector shifts every item. A Deque does both ends in constant time, which
makes it the right structure for queues, sliding windows and breadth-first
search:

```mah
import "std:collections"

# Breadth-first search over a small graph.
let edges = ["a": ["b", "c"], "b": ["d"], "c": ["d"], "d": []]
let queue = Deque.of(["a"])
let visited = Set.of(["a"])
let order = []
while !queue.is_empty() {
    let node = queue.pop_front()
    order.push(node)
    for let next in edges[node] {
        if !visited.has(next) {
            visited.add(next)
            queue.push_back(next)
        }
    }
}
print(order)                          # [a, b, c, d]
```

`front()`/`back()` peek without removing; `get(i)` reads any position
(negative counts from the back). Popping or peeking an empty Deque gives
`none`, like `Vector.pop`.

## `PriorityQueue`: smallest first

`pop()` always gives the smallest item. Items are compared with `<`, or by
a **key function** you pass to `new`/`of`. Equal items come out in the
order they were pushed.

```mah
import "std:collections"

let tasks = PriorityQueue.new(fn(task) { task.len() })   # shortest first
for let t in ["write docs", "fix", "test it"] {
    tasks.push(t)
}
print(tasks.peek(), tasks.len())                # fix 3
print(tasks.pop(), tasks.pop(), tasks.pop())    # fix test it write docs
print(tasks.pop())                              # none
```

For **largest first**, use a key that negates: `PriorityQueue.new(fn(x) { 0 - x })`.
A key can also pick a field, so a queue of structs orders by priority:

```mah
import "std:collections"

struct Job { name: String, priority: Number }
let q = PriorityQueue.new(fn(j) { j.priority })
q.push(Job { name: "deploy", priority: 2 })
q.push(Job { name: "hotfix", priority: 1 })
q.push(Job { name: "lunch", priority: 3 })
print(q.to_vector().map(fn(j) { j.name }).reduce())   # [hotfix, deploy, lunch]
```

Iterating a PriorityQueue (or `to_vector()`) yields the items smallest
first and leaves the queue unchanged.

## Reference

Every type has `new()`, `of(values)`, `len()`, `is_empty()`, `clear()`,
`to_vector()`, and is Iterable and Printable.

**`Set<T>`**

| Method | |
|---|---|
| `add(x)` | adds `x` (nothing happens if it's there) |
| `remove(x)` | removes `x`; returns whether it was there |
| `has(x)` | whether `x` is in the Set |
| `copy()` | a new Set with the same values |
| `union(other)` | values in either Set |
| `intersection(other)` | values in both |
| `difference(other)` | values in this Set but not `other` |
| `is_subset(other)` | whether every value is also in `other` |
| `equals(other)` | the same values, in any order |

**`Deque<T>`**

| Method | |
|---|---|
| `push_back(x)`, `push_front(x)` | add at an end |
| `pop_back()`, `pop_front()` | remove and return from an end; `none` when empty |
| `back()`, `front()` | peek at an end; `none` when empty |
| `get(i)` | the item `i` from the front (negative: from the back), `none` out of range |

**`PriorityQueue<T>`**

| Method | |
|---|---|
| `new(key = none)`, `of(values, key = none)` | an empty / filled queue, ordered by `key(item)` if given |
| `push(x)` | adds `x` |
| `pop()` | removes and returns the smallest; `none` when empty |
| `peek()` | the smallest, left in place; `none` when empty |
