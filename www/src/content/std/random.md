---
title: std:random
order: 2
section: Basics
summary: Seedable pseudo-random numbers, integers in a range, picking, shuffling and sampling, identical on every runtime.
---

# `std:random`

Pseudo-random numbers: floats, whole numbers in a range, and picking,
shuffling or sampling from a Vector. Every runtime uses the same generator
(xoshiro256\*\*), so a seeded program prints the same numbers on the Python
VM and on `mah-vm`.

```mah
import random from "std:random"

let roll = random.randint(1, 6)                     # 1 to 6, both included
let coin = random.choice(["heads", "tails"])
print(roll >= 1 & roll <= 6, coin.len() >= 5)       # true true
```

## Repeatable runs with `seed`

Without a seed the generator starts from the operating system's
randomness, so each run differs. Call `seed(n)` once at the start to make
everything after it repeatable, which is what you want in tests and
simulations:

```mah
import random from "std:random"

random.seed(42)
print(random.random())               # 0.08386297105988216316063699196
print(random.randint(1, 100))        # 3
```

## Picking, shuffling, sampling

```mah
import random from "std:random"

random.seed(7)
let deck = ["A", "K", "Q", "J", "10"]
let hand = random.sample(deck, 2)          # 2 cards from different positions
print(hand.len(), hand[0] != hand[1])      # 2 true

let order = random.shuffled(deck)          # a new Vector; deck is unchanged
print(order.len(), deck[0])                # 5 A
random.shuffle(deck)                       # in place
print(deck.len())                          # 5
```

`uniform(lo, hi)` gives any Number in `[lo, hi)`; `randint(lo, hi)` gives
whole Numbers with both ends included, each equally likely.

## Independent generators: `Rng`

The module-level functions share one generator. When one part of a program
needs its own repeatable sequence (a level generator seeded from a level
number, say) while the rest stays random, make a `random.Rng`:

```mah
import random from "std:random"

fn level_layout(level: Number) -> Vector<Number> {
    let rng = random.Rng.new(level)          # same level, same layout
    (0..4).map(fn(i) { rng.randint(0, 9) }).reduce()   # reduce() collects into a Vector
}
print(level_layout(3) == level_layout(3))    # false (Vectors compare by identity)
print(level_layout(3).join(",") == level_layout(3).join(","))   # true
```

`Rng.new()` with no seed starts from system randomness. An `Rng` has the
same methods as the module: `random`, `uniform`, `randint`, `choice`,
`shuffle`, `shuffled` and `sample`.

## Errors and limits

Bad arguments, like `choice([])`, `randint(5, 1)` or `sample(v, 99)` on a
shorter Vector, throw a `RuntimeError.ArgumentError`. A seed must be a
whole Number below 2^64.

This generator is fast and statistically good, but it **is not for
cryptography**: never use it for passwords, tokens or keys.

## Reference

| Function | |
|---|---|
| `seed(n)` | seeds the module's generator |
| `random()` | a Number in `[0, 1)`, a multiple of 2^-53 |
| `uniform(lo, hi)` | a Number in `[lo, hi)` |
| `randint(lo, hi)` | a whole Number from `lo` to `hi`, both included |
| `choice(items)` | one item of a non-empty Vector |
| `shuffle(items)` | puts the Vector in a random order, in place (Fisher-Yates) |
| `shuffled(items)` | a new, shuffled Vector |
| `sample(items, k)` | `k` items from different positions, in random order |
| `Rng.new(seed = none)` | an independent generator with all of the above as methods |
