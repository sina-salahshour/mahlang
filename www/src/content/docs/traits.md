---
title: Traits
order: 6
section: Language
---

```mah
trait Shape {
    fn area(self)                           # required method
    fn describe(self) { "area " + self.area() }   # default method
    fn unit()                               # static (no `self`)
}

struct Rect { w, h }

impl Rect {                                 # the type's own methods
    fn new(w, h) { Self { w: w, h: h } }    # `Self` = the impl's type
    fn scale(self, k) { self.w = self.w * k; self }
}

impl Shape for Rect {
    fn area(self) { self.w * self.h }
    fn unit() { Rect { w: 1, h: 1 } }
}

let r = Rect.new(2, 3)          # static function: Type.name(...)
print(r.area())                 # method call
print(r.describe())             # inherited default method
print(Shape.area(r))            # trait-qualified call
```

- A trait/impl `fn` whose first parameter is `self` is a method;
  otherwise it's a static function called as `Type.name(...)`. `self`
  can't appear anywhere else.
- `trait` and `impl` must be at the top level. They (and top-level
  `struct`/`enum`) can be used before they're declared, and method
  bodies can use any top-level name.
- `impl Type { }` only works for your own structs/enums. `impl Trait for
  Type { }` works if the trait or the type is yours, so `impl MyTrait for
  Number` is fine (the orphan rule). Built-in type names: `Number`,
  `String`, `Bool`, `Function`, `Option`, `Promise`, `Vector`, `Map`.
  Built-in traits: `Printable`, `Index`, `IndexAssign`, `Iterable`,
  `Iterator`.
- A top-level `fn` can be an impl target too: `impl Tr for somefn { ... }` or
  `impl somefn { ... }` give *that function* methods (`somefn.describe()`),
  in the function's module or the trait's. It is how a plain function is a
  [decorator hook](/docs/decorators). Other functions and closures don't
  have them, and a `let`, a nested `fn` or a closure isn't a target.
- The impl must define every required method, with the same parameters.
- If two traits give one type the same method name, call it as
  `Trait.name(value)`.
- `p.f()` calls a function stored in field `f` if there's no method `f`
  of that name — handy for callback-style fields:

```mah
struct Adder { step, add }
let a = Adder { step: 10, add: none }
a.add = fn(x) { x + a.step }
print(a.add(5))   # 15
```

## Implementing a trait for a built-in type

```mah
trait Double {
    fn double(self)
}
impl Double for Number {
    fn double(self) { self * 2 }
}
print(21.double())   # 42
```

## `Printable`: custom printing

```mah
struct Point { x, y }
impl Printable for Point {
    fn to_string(self) { "(" + self.x + ", " + self.y + ")" }
}
print(Point { x: 10, y: 2 })   # (10, 2)
```

## `Iterable`/`Iterator`: give your type `for`, `map`, `filter`, ...

Implement `Iterable` (an `iter` method returning an iterator) and
`Iterator` (a `next` method returning `some(item)` or `none`), and every
lazy adapter method comes for free — see
[Iterators & ranges](/docs/iterators-ranges).

## `detach` on method calls

```mah
impl Rect {
    fn grow(self, by) { sleep_async(5); Rect { w: self.w + by, h: self.h + by } }
}
let big = detach Rect.new(1, 1).grow(4)
print(big.await)
```

`detach` accepts any call chain, not just a plain `name(args)` — a
dynamic method call works the same as a bare function call. See
[Async](/docs/async).
