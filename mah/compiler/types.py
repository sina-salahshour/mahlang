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
    `Promise<T>`), or a struct/enum, with its type arguments. `throws` is
    only ever set on a `Promise`: the error set its `.await` re-throws
    (M26, docs/ERRORS.md's `Promise<T, E>`), or None when it's untracked."""

    __slots__ = ("name", "args", "throws")

    def __init__(self, name: str, args: tuple = (), throws=None):
        self.name = name
        self.args = tuple(args)
        self.throws = throws


class TFn(Type):
    """A function type. `required` is how many leading parameters have no
    default; `names` are the parameter names (for keyword arguments), or
    `None` when unknown (a written `fn(A) -> B` type). `throws` is the
    function's error set (an `ESet`, M26), or None for a function that
    throws nothing the checker tracks (a native method).

    M41c: `rest` says the last parameter(s) are rest parameters (bit 0 the
    `...` one, bit 1 the `**` one; their types are the collections', and
    `required` counts only ordinary parameters). `item` is the key
    (`fn#name`) of the item type of the top-level function this type is the
    type of, or None -- an item type is assignable to (and unifies with) the
    ordinary function type, and method lookup on it finds `impl somefn`'s
    methods."""

    __slots__ = ("params", "ret", "required", "names", "throws", "rest", "item")

    def __init__(self, params, ret, required: int | None = None, names=None, throws=None, rest: int = 0, item=None):
        self.params = tuple(params)
        self.ret = ret
        self.required = len(self.params) if required is None else required
        self.names = tuple(names) if names is not None else None
        self.throws = throws
        self.rest = rest
        self.item = item


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


# -- error sets (M26, docs/ERRORS.md's "Inference") ---------------------------

# The marker for "something the checker can't see through threw here" (a
# call to an `Unknown` callee, a `throw` of a value of unknown type).
UNKNOWN_ERROR = "Unknown"
# An exclusion that drops everything (a catch-all arm).
ALL = frozenset({"*"})
EMPTY = frozenset()

_eset_ids = itertools.count()


class ESet:
    """A node in the program-wide error-set graph. Its value is `names`
    (error type names, plus `UNKNOWN_ERROR`) together with the value of
    every `(node, excluded)` in `subs`, minus `excluded` (`ALL` drops
    everything). Values are the least fixpoint of those equations
    (`solve`), so recursion needs nothing special.

    - kind "acc": a function body's (or a `try` body's) accumulated set.
    - kind "var": an error set nothing declares or accumulates -- a
      callback parameter's, say. Generalization quantifies these, like
      type variables (`level` plays the same role as `TVar.level`).
    - kind "sealed": a written `throws` list; its value is `declared`,
      whatever flows in (what flows in is checked against it instead).

    `solved` holds the value once `solve` has run over the node."""

    __slots__ = ("id", "kind", "level", "names", "subs", "declared", "position", "solved")

    def __init__(self, kind: str, level: int, names=EMPTY, declared=None, position=None):
        self.id = next(_eset_ids)
        self.kind = kind
        self.level = level
        self.names = frozenset(names)
        self.subs: tuple = ()
        self.declared = frozenset(declared) if declared is not None else None
        self.position = position
        self.solved = None


def _minus(names: frozenset, excluded: frozenset) -> frozenset:
    if not excluded:
        return names
    if "*" in excluded:
        return EMPTY
    return names - excluded


def solve(roots) -> None:
    """Compute (and store in `.solved`) the value of every node reachable
    from `roots`: a plain least-fixpoint iteration over the graph."""
    nodes: list = []
    seen: set = set()
    stack = [r for r in roots if r is not None]
    while stack:
        n = stack.pop()
        if n.id in seen:
            continue
        seen.add(n.id)
        nodes.append(n)
        if n.kind != "sealed":
            stack.extend(sub for sub, _ex in n.subs)
    value = {n.id: (n.declared if n.kind == "sealed" else n.names) for n in nodes}
    changed = True
    while changed:
        changed = False
        for n in nodes:
            if n.kind == "sealed":
                continue
            v = value[n.id]
            for sub, excluded in n.subs:
                extra = _minus(value[sub.id], excluded) - v
                if extra:
                    v = v | extra
            if v != value[n.id]:
                value[n.id] = v
                changed = True
    for n in nodes:
        n.solved = value[n.id]


def esets(t, out: list | None = None) -> list:
    """Every ESet directly inside `t` (function types' and Promises'), each
    once."""
    if out is None:
        out = []
    t = prune(t)
    if isinstance(t, TCon):
        if t.throws is not None and not any(n is t.throws for n in out):
            out.append(t.throws)
        for a in t.args:
            esets(a, out)
    elif isinstance(t, TFn):
        if t.throws is not None and not any(n is t.throws for n in out):
            out.append(t.throws)
        for p in t.params:
            esets(p, out)
        esets(t.ret, out)
    return out


def show_throws(node) -> str:
    """` throws A | B` for a solved, non-empty error set, else ''."""
    if node is None or node.solved is None:
        return ""
    names = sorted(n for n in node.solved if n != UNKNOWN_ERROR)
    if UNKNOWN_ERROR in node.solved:
        names.append(UNKNOWN_ERROR)
    return f" throws {' | '.join(names)}" if names else ""


NUMBER = TCon("Number")
STRING = TCon("String")
BOOL = TCon("Bool")
NONE = TCon("None")
NEVER = TCon("Never")
BYTES = TCon("Bytes")  # M37

PRIMITIVES = {"Number": NUMBER, "String": STRING, "Bool": BOOL, "None": NONE, "Never": NEVER, "Bytes": BYTES}
# Built-in generic types whose arguments are covariant (docs/TYPES.md,
# assignability rule 5); every other nominal type is invariant.
COVARIANT = {"Option", "Promise"}


class Scheme:
    """A generalized type: `params` are quantified in `type`. `evars` are
    its quantified error-set variables (M26), and `enodes` the error sets
    in `type` that refer to one of them, so need a copy per instance."""

    __slots__ = ("params", "type", "evars", "enodes")

    def __init__(self, params, type_, evars=(), enodes=()):
        self.params = tuple(params)
        self.type = type_
        self.evars = tuple(evars)
        self.enodes = tuple(enodes)


def prune(t: Type) -> Type:
    while isinstance(t, TVar) and t.ref is not None:
        t = t.ref
    return t


def is_con(t: Type, name: str) -> bool:
    return isinstance(t, TCon) and t.name == name


def subst(t: Type, mapping: dict, emap: dict | None = None) -> Type:
    """Replace TParams in `t` by `mapping` (TParam -> Type), and error sets
    by `emap` (ESet id -> ESet)."""
    t = prune(t)
    if isinstance(t, TParam):
        return mapping.get(t, t)
    if isinstance(t, TCon):
        throws = t.throws
        if emap and throws is not None:
            throws = emap.get(throws.id, throws)
        if not t.args and throws is t.throws:
            return t
        return TCon(t.name, [subst(a, mapping, emap) for a in t.args], throws)
    if isinstance(t, TFn):
        throws = t.throws
        if emap and throws is not None:
            throws = emap.get(throws.id, throws)
        return TFn(
            [subst(p, mapping, emap) for p in t.params],
            subst(t.ret, mapping, emap),
            t.required,
            t.names,
            throws,
            t.rest,
            t.item,
        )
    return t


def instantiate(scheme: Scheme, level: int) -> Type:
    if not scheme.params and not scheme.evars:
        return scheme.type
    emap = None
    if scheme.evars:
        emap = {v.id: ESet("var", level) for v in scheme.evars}
        for node in scheme.enodes:
            copy = ESet(node.kind, level, node.names, node.declared, node.position)
            copy.subs = tuple((emap.get(sub.id, sub), ex) for sub, ex in node.subs)
            emap[node.id] = copy
    return subst(scheme.type, {p: TVar(level) for p in scheme.params}, emap)


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


def free_params(t: Type, out: list | None = None) -> list:
    """The TParams in `t`, each once, in first-occurrence order."""
    if out is None:
        out = []
    t = prune(t)
    if isinstance(t, TParam):
        if t not in out:
            out.append(t)
    elif isinstance(t, TCon):
        for a in t.args:
            free_params(a, out)
    elif isinstance(t, TFn):
        for p in t.params:
            free_params(p, out)
        free_params(t.ret, out)
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
            return f"fn({params}){show_throws(t.throws)}"
        return f"fn({params}) -> {show(ret)}{show_throws(t.throws)}"
    return repr(t)


class Unifier:
    """Binds inference variables, recording every change on a trail so a
    failed `unify`/`assign` can be undone with `rollback`. `bound` lists the
    variables bound since the last `take_bound`, so the checker can re-try
    the operator constraints waiting on them."""

    def __init__(self):
        self.trail: list = []
        self.bound: list = []
        # M26: `(source ESet, sealed ESet)` pairs -- a value whose error set
        # is `source` flowed where only the sealed (declared) set is
        # allowed. The checker drains and verifies these after solving.
        self.checks: list = []

    # -- trail ---------------------------------------------------------

    def _set(self, var: TVar, attr: str, value) -> None:
        self.trail.append((var, attr, getattr(var, attr)))
        setattr(var, attr, value)

    def mark(self) -> tuple:
        return len(self.trail), len(self.bound), len(self.checks)

    def rollback(self, mark: tuple) -> None:
        trail_len, bound_len, checks_len = mark
        while len(self.trail) > trail_len:
            var, attr, old = self.trail.pop()
            setattr(var, attr, old)
        del self.bound[bound_len:]
        del self.checks[checks_len:]

    # -- error sets (M26) ---------------------------------------------------

    def flow(self, source, target, excluded: frozenset = EMPTY) -> None:
        """Errors in `source` (minus `excluded`) may also come out of
        `target`: an edge, or a check when `target` is sealed."""
        if source is None or target is None or source is target:
            return
        if target.kind == "sealed":
            if source.kind != "sealed" or not source.declared <= target.declared:
                self.checks.append((source, target))
            return
        if any(sub is source and ex == excluded for sub, ex in target.subs):
            return
        self._set(target, "subs", target.subs + ((source, excluded),))
        if source.level > target.level:
            self._lower_eset(source, target.level)

    def _lower_eset(self, node, level: int) -> None:
        if node.level > level:
            self._set(node, "level", level)

    def _flow_both(self, a, b) -> None:
        self.flow(a, b)
        self.flow(b, a)

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
            if t.throws is not None:
                self._lower_eset(t.throws, level)
            for a in t.args:
                self._lower_levels(a, level)
        elif isinstance(t, TFn):
            if t.throws is not None:
                self._lower_eset(t.throws, level)
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
            ok = (
                a.name == b.name
                and len(a.args) == len(b.args)
                and all(self.unify(x, y) for x, y in zip(a.args, b.args))
            )
            if ok:
                self._flow_both(a.throws, b.throws)
            return ok
        if isinstance(a, TFn) and isinstance(b, TFn):
            ok = (
                len(a.params) == len(b.params)
                and all(self.unify(x, y) for x, y in zip(a.params, b.params))
                and self.unify(a.ret, b.ret)
            )
            if ok:
                self._flow_both(a.throws, b.throws)
            return ok
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
            ok = all(rel(x, y) for x, y in zip(s.args, t.args))
            if ok:
                self.flow(s.throws, t.throws)
            return ok
        if isinstance(s, TFn) and isinstance(t, TFn):
            # A function may take fewer parameters than expected (a
            # callback that ignores the index), and extra ones only if
            # they have defaults.
            if s.required > len(t.params):
                return False
            ok = all(self.unify(x, y) for x, y in zip(s.params, t.params)) and self.assign(s.ret, t.ret)
            if ok:
                # Its errors are a subset of what the expected type allows.
                self.flow(s.throws, t.throws)
            return ok
        return False
