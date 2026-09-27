"""M22: static types -- representation, unification, assignability (see
docs/TYPES.md's "The type model"). The checker pass that uses these lives
in typecheck.py.

Types are immutable except for inference variables (`TVar`), which get
bound in place. Every binding goes through a `Unifier`, which records it on
a trail so a failed check can be rolled back completely (a mismatch never
leaves half of a structure unified).
"""

from __future__ import annotations

import itertools


class Type:
    __slots__ = ()


class TCon(Type):
    """A nominal type: a primitive (`Number`, `String`, `Bool`, `None`,
    `Never`), a built-in generic (`Vector<T>`, `Map<K, V>`, `Option<T>`,
    `Promise<T>`), or a struct/enum, with its type arguments."""

    __slots__ = ("name", "args")

    def __init__(self, name: str, args: tuple = ()):
        self.name = name
        self.args = tuple(args)


class TFn(Type):
    """A function type. `required` is how many leading parameters have no
    default; `names` are the parameter names (for keyword arguments), or
    `None` when unknown (a written `fn(A) -> B` type)."""

    __slots__ = ("params", "ret", "required", "names")

    def __init__(self, params, ret, required: int | None = None, names=None):
        self.params = tuple(params)
        self.ret = ret
        self.required = len(self.params) if required is None else required
        self.names = tuple(names) if names is not None else None


class TParam(Type):
    """A rigid type parameter: declared (`<T>`) or produced by
    generalization. Compared by identity."""

    __slots__ = ("name",)

    def __init__(self, name: str):
        self.name = name


class TUnknown(Type):
    """The gradual escape hatch. `kind` is "explicit" (written `Unknown`),
    "implicit" (the checker couldn't infer it -- reported at the explicit
    level), or "unchecked" (something this version of the checker doesn't
    type yet, or the recovery value after a reported error -- never
    reported). `position`/`what` say where an implicit one came from."""

    __slots__ = ("kind", "position", "what")

    def __init__(self, kind: str, position: int | None = None, what: str | None = None):
        self.kind = kind
        self.position = position
        self.what = what


_var_ids = itertools.count()


class TVar(Type):
    """An inference variable. `level` drives generalization (ML-style):
    only variables created deeper than the function being generalized
    become its type parameters. `none_seen` records that `none` was
    assigned to it (a lower bound that doesn't bind it). `constraints` are
    pending operator constraints waiting for it to be bound."""

    __slots__ = ("id", "ref", "level", "none_seen", "constraints")

    def __init__(self, level: int):
        self.id = next(_var_ids)
        self.ref = None
        self.level = level
        self.none_seen = False
        self.constraints = []


NUMBER = TCon("Number")
STRING = TCon("String")
BOOL = TCon("Bool")
NONE = TCon("None")
NEVER = TCon("Never")

PRIMITIVES = {"Number": NUMBER, "String": STRING, "Bool": BOOL, "None": NONE, "Never": NEVER}
# Built-in generic types whose arguments are covariant (docs/TYPES.md,
# assignability rule 5); every other nominal type is invariant.
COVARIANT = {"Option", "Promise"}


class Scheme:
    """A generalized type: `params` are quantified in `type`."""

    __slots__ = ("params", "type")

    def __init__(self, params, type_):
        self.params = tuple(params)
        self.type = type_


def prune(t: Type) -> Type:
    while isinstance(t, TVar) and t.ref is not None:
        t = t.ref
    return t


def is_con(t: Type, name: str) -> bool:
    return isinstance(t, TCon) and t.name == name


def subst(t: Type, mapping: dict) -> Type:
    """Replace TParams in `t` by `mapping` (TParam -> Type)."""
    t = prune(t)
    if isinstance(t, TParam):
        return mapping.get(t, t)
    if isinstance(t, TCon):
        if not t.args:
            return t
        return TCon(t.name, [subst(a, mapping) for a in t.args])
    if isinstance(t, TFn):
        return TFn([subst(p, mapping) for p in t.params], subst(t.ret, mapping), t.required, t.names)
    return t


def instantiate(scheme: Scheme, level: int) -> Type:
    if not scheme.params:
        return scheme.type
    return subst(scheme.type, {p: TVar(level) for p in scheme.params})


def free_vars(t: Type, out: list | None = None) -> list:
    """Unbound TVars in `t`, each once, in first-occurrence order."""
    if out is None:
        out = []
    t = prune(t)
    if isinstance(t, TVar):
        if t not in out:
            out.append(t)
    elif isinstance(t, TCon):
        for a in t.args:
            free_vars(a, out)
    elif isinstance(t, TFn):
        for p in t.params:
            free_vars(p, out)
        free_vars(t.ret, out)
    return out


def unknowns(t: Type, out: list | None = None) -> list:
    """Every TUnknown object inside `t`."""
    if out is None:
        out = []
    t = prune(t)
    if isinstance(t, TUnknown):
        if not any(u is t for u in out):
            out.append(t)
    elif isinstance(t, TCon):
        for a in t.args:
            unknowns(a, out)
    elif isinstance(t, TFn):
        for p in t.params:
            unknowns(p, out)
        unknowns(t.ret, out)
    return out


def show(t: Type) -> str:
    t = prune(t)
    if isinstance(t, TVar):
        return "None" if t.none_seen else "?"
    if isinstance(t, TUnknown):
        return "Unknown"
    if isinstance(t, TParam):
        return t.name
    if isinstance(t, TCon):
        if not t.args:
            return t.name
        return f"{t.name}<{', '.join(show(a) for a in t.args)}>"
    if isinstance(t, TFn):
        params = ", ".join(show(p) for p in t.params)
        ret = prune(t.ret)
        if is_con(ret, "None"):
            return f"fn({params})"
        return f"fn({params}) -> {show(ret)}"
    return repr(t)


class Unifier:
    """Binds inference variables, recording every change on a trail so a
    failed `unify`/`assign` can be undone with `rollback`. `bound` lists the
    variables bound since the last `take_bound`, so the checker can re-try
    the operator constraints waiting on them."""

    def __init__(self):
        self.trail: list = []
        self.bound: list = []

    # -- trail ---------------------------------------------------------

    def _set(self, var: TVar, attr: str, value) -> None:
        self.trail.append((var, attr, getattr(var, attr)))
        setattr(var, attr, value)

    def mark(self) -> tuple:
        return len(self.trail), len(self.bound)

    def rollback(self, mark: tuple) -> None:
        trail_len, bound_len = mark
        while len(self.trail) > trail_len:
            var, attr, old = self.trail.pop()
            setattr(var, attr, old)
        del self.bound[bound_len:]

    def commit(self) -> None:
        self.trail.clear()

    def take_bound(self) -> list:
        bound, self.bound = self.bound, []
        return bound

    def note_none(self, var: TVar) -> None:
        if not var.none_seen:
            self._set(var, "none_seen", True)

    # -- binding -------------------------------------------------------

    def _occurs(self, var: TVar, t: Type) -> bool:
        t = prune(t)
        if t is var:
            return True
        if isinstance(t, TCon):
            return any(self._occurs(var, a) for a in t.args)
        if isinstance(t, TFn):
            return any(self._occurs(var, p) for p in t.params) or self._occurs(var, t.ret)
        return False

    def _lower_levels(self, t: Type, level: int) -> None:
        t = prune(t)
        if isinstance(t, TVar):
            if t.level > level:
                self._set(t, "level", level)
        elif isinstance(t, TCon):
            for a in t.args:
                self._lower_levels(a, level)
        elif isinstance(t, TFn):
            for p in t.params:
                self._lower_levels(p, level)
            self._lower_levels(t.ret, level)

    def bind(self, var: TVar, t: Type) -> bool:
        t = prune(t)
        if t is var:
            return True
        if self._occurs(var, t):
            return False
        self._lower_levels(t, var.level)
        if isinstance(t, TVar) and var.none_seen:
            self.note_none(t)
        self._set(var, "ref", t)
        self.bound.append(var)
        return True

    # -- relations -----------------------------------------------------

    def unify(self, a: Type, b: Type) -> bool:
        """Type equality, binding variables on either side."""
        a, b = prune(a), prune(b)
        if a is b:
            return True
        if isinstance(a, TVar):
            return self.bind(a, b)
        if isinstance(b, TVar):
            return self.bind(b, a)
        if isinstance(a, TUnknown) or isinstance(b, TUnknown):
            return True
        if isinstance(a, TCon) and isinstance(b, TCon):
            return (
                a.name == b.name
                and len(a.args) == len(b.args)
                and all(self.unify(x, y) for x, y in zip(a.args, b.args))
            )
        if isinstance(a, TFn) and isinstance(b, TFn):
            return (
                len(a.params) == len(b.params)
                and all(self.unify(x, y) for x, y in zip(a.params, b.params))
                and self.unify(a.ret, b.ret)
            )
        return False

    def assign(self, s: Type, t: Type) -> bool:
        """Is a value of type `s` assignable where `t` is expected
        (docs/TYPES.md's assignability rules 1-7)?"""
        s, t = prune(s), prune(t)
        if s is t:
            return True
        if isinstance(s, TUnknown) and isinstance(t, TVar):
            # An Unknown value flowing into a variable makes it Unknown
            # (with the same origin); the reverse, a variable passed where
            # Unknown is accepted, says nothing about the variable.
            return self.bind(t, s)
        if isinstance(s, TUnknown) or isinstance(t, TUnknown):
            return True
        if is_con(s, "Never"):
            return True
        if is_con(s, "None"):
            if isinstance(t, TVar):
                self.note_none(t)
            return True
        if isinstance(s, TVar) or isinstance(t, TVar):
            return self.unify(s, t)
        if isinstance(s, TCon) and isinstance(t, TCon):
            if s.name != t.name or len(s.args) != len(t.args):
                return False
            rel = self.assign if s.name in COVARIANT else self.unify
            return all(rel(x, y) for x, y in zip(s.args, t.args))
        if isinstance(s, TFn) and isinstance(t, TFn):
            # A function may take fewer parameters than expected (a
            # callback that ignores the index), and extra ones only if
            # they have defaults.
            if s.required > len(t.params):
                return False
            return all(self.unify(x, y) for x, y in zip(s.params, t.params)) and self.assign(s.ret, t.ret)
        return False
