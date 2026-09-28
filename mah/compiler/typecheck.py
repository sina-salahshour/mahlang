"""M22: the static type checker (see docs/TYPES.md).

Runs after `resolve` and before `codegen`, over the same AST, and never
changes anything codegen reads: every result lives on the `Checker`.
Variables are looked up through the resolver's symbol table
(`position_index`), so the checker never re-implements scoping.

Order (docs/TYPES.md's "Items and order"):

1. Declarations: every struct/enum (anywhere in the program) gets its type
   parameters and field types. An unannotated user field gets one inference
   variable shared by the whole program; an unannotated field declared by
   the prelude is `Unknown` (the prelude gets real annotations in M23).
   Top-level non-function `let`s get their type up front (the annotation,
   or a variable), since function bodies may refer to them.
2. Top-level functions (`fn f` / `let f = fn ...`) and user impl/trait
   method bodies, in source order. A reference to a top-level function
   that hasn't been checked yet checks it first, depth-first; a reference
   to one that's in progress (recursion) uses its signature as it is.
   A finished function is generalized: its free inference variables
   created at a deeper level than its definition become type parameters
   (ML-style levels, see types.TVar).
3. The main program: the remaining top-level statements, in order.

Method calls (M23, first slice): every impl/trait method is registered up
front (`_register_methods`) and its body checked on demand like a
top-level function, with `self` typed as the impl's target. `obj.m(...)`
looks `m` up on the receiver's type (inherent impl first, then trait
impls and their defaults, then the native methods of the built-in types);
`Type.f(...)`/`Trait.m(recv, ...)` use the named type's/trait's. An
unbound receiver whose method name exactly one type provides becomes that
type. Not typed yet: the prelude's methods (unchecked), trait types and
bounds, generic defaults, and user `Index`/`Iterable` impls. Built-in indexing and
`for` over built-in types are typed directly. The prelude's bodies aren't
checked at all, and no diagnostic is ever reported inside the prelude.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from .ast_nodes import (
    NativeCall,
    AssignStmt,
    Binary,
    BindPat,
    Block,
    BoolLit,
    BreakStmt,
    Call,
    ContinueStmt,
    DeferStmt,
    DetachExpr,
    EnumDecl,
    EnumLit,
    EnumPat,
    ErrorNode,
    ExprStmt,
    FieldAccess,
    FnExpr,
    FnType,
    ForStmt,
    Ident,
    IfStmt,
    ImplDecl,
    Index,
    InputExpr,
    LetStmt,
    MapLit,
    MatchStmt,
    MethodCall,
    NamedType,
    NumberLit,
    PrintStmt,
    RangePat,
    ReturnStmt,
    SleepAsyncExpr,
    StringLit,
    StructDecl,
    StructLit,
    StructPat,
    ThrowExpr,
    TraitDecl,
    TryExpr,
    TypePat,
    Unary,
    VectorLit,
    WhileStmt,
    WildcardPat,
)
from .types import (
    ALL,
    BOOL,
    EMPTY,
    NEVER,
    NONE,
    NUMBER,
    PRIMITIVES,
    STRING,
    Scheme,
    UNKNOWN_ERROR,
    ESet,
    TCon,
    TFn,
    TParam,
    TUnknown,
    TVar,
    Unifier,
    free_params,
    free_vars,
    instantiate,
    is_con,
    prune,
    esets,
    show,
    solve,
    subst,
    unknowns,
)

CHECK_LEVELS = ("loose", "strict", "explicit")
DEFAULT_CHECK_LEVEL = "loose"


@dataclass
class TypeDiagnostic:
    # "mismatch": a type error (warning in loose, error in strict/explicit).
    # "unhandled" (M26): an error that can escape to the top of the program,
    # or a function that can throw something its `throws` clause doesn't
    # list -- reported exactly like "mismatch".
    # "implicit": a declaration whose type couldn't be inferred, or a call
    # whose errors couldn't be (reported only at the explicit level).
    # "warning" (M26): reported at every level, but always as a warning
    # (a `catch` arm naming an error type the `try` body never throws).
    kind: str
    message: str  # no position in it
    position: int  # offset in the combined (preprocessed) text

    def text(self) -> str:
        """The message in the compiler's usual `... at position N` form, so
        the existing position mapping and demangling apply to it."""
        return f"{self.message} at position {self.position}"


def check_program(program: list, resolver) -> list[TypeDiagnostic]:
    """Type-check a resolved program. Returns every diagnostic, sorted by
    position, whatever the level; filter with `reportable`."""
    return Checker(resolver).check(program)


def reportable(diagnostics: list[TypeDiagnostic], level: str) -> list[TypeDiagnostic]:
    """The diagnostics that count at `level`: everything but the
    implicit-Unknown ones at every level; those only at "explicit"."""
    if level == "explicit":
        return list(diagnostics)
    return [d for d in diagnostics if d.kind != "implicit"]


def is_warning(diagnostic: TypeDiagnostic, level: str) -> bool:
    """Whether `diagnostic` is only a warning at `level` (everything is, in
    "loose"); the rest are errors that fail `mah run`/`build`/`check`."""
    return level == "loose" or diagnostic.kind == "warning"


# -- helpers -----------------------------------------------------------------

_ARITH_OPS = {"-", "/", "//", "%", "**"}
_COMPARE_OPS = {"lt": "<", "gt": ">", "le": "<=", "ge": ">="}
_RANGE_TYPES = {"Range", "FromRange", "ToRange"}
_PARAM_NAMES = ["T", "U", "V", "W", "A", "B", "C", "D", "E"]


def _unchecked() -> TUnknown:
    return TUnknown("unchecked")


def _walk(node):
    """Every AST node reachable from `node` (a node or a list of them)."""
    stack = [node]
    while stack:
        n = stack.pop()
        if isinstance(n, (list, tuple)):
            stack.extend(n)
        elif dataclasses.is_dataclass(n) and not isinstance(n, type):
            yield n
            for f in dataclasses.fields(n):
                value = getattr(n, f.name)
                if isinstance(value, (list, tuple)) or dataclasses.is_dataclass(value):
                    stack.append(value)


class _TypeInfo:
    """A struct's or enum's type parameters and field types. For an enum,
    `variants` maps each variant to its own field dict; for a struct,
    `fields` is the field dict. `inferred` holds the (variant, field) keys
    whose type is a program-wide inference variable (unannotated user
    fields), which get the "sites disagree" treatment."""

    def __init__(self, name: str, params: list):
        self.name = name
        self.params = params
        self.fields: dict = {}
        self.variants: dict = {}
        self.inferred: set = set()
        self.positions: dict = {}  # (variant, field) -> declaration position

    def fresh_args(self, level: int) -> list:
        return [TVar(level) for _ in self.params]


class _Item:
    """A top-level function (`fn f` or `let f = fn ...`)."""

    def __init__(self, stmt: LetStmt):
        self.stmt = stmt
        self.state = "todo"  # "todo" | "busy" | "done"
        self.mono = None  # its signature while it's being checked
        self.scheme = None


class _Method:
    """One impl or trait method, checked on demand like `_Item`."""

    def __init__(self, decl, target, scope: dict, params: list, system: bool):
        self.decl = decl  # MethodDecl
        self.target = target  # `self`'s type (unchecked Unknown in a trait)
        self.scope = scope  # the impl's/trait's own type parameters
        self.params = params  # TParams to quantify besides the method's own
        self.system = system  # declared in the prelude: not checked
        self.state = "todo"  # "todo" | "busy" | "done"
        self.mono = None
        self.scheme = None


class _Loop:
    def __init__(self, var: TVar):
        self.var = var
        self.broke = False


class _FnCtx:
    def __init__(self, ret, acc):
        self.ret = ret
        self.loops: list = []
        # M26: where errors raised right now go -- the function body's own
        # error set, then one more per enclosing `try` body / `detach`.
        self.accs: list = [acc]


def _union_ex(a: frozenset, b: frozenset) -> frozenset:
    """Excluding `a`, then `b`."""
    if "*" in a or "*" in b:
        return ALL
    return a | b


def _meet_ex(a: frozenset, b: frozenset) -> frozenset:
    """What two paths excluding `a` and `b` both exclude."""
    if "*" in a:
        return b
    if "*" in b:
        return a
    return a & b


def _minus_ex(names: frozenset, excluded: frozenset) -> frozenset:
    if "*" in excluded:
        return EMPTY
    return names - excluded


class _Constraint:
    """A pending `+`/`*`/comparison whose operand types aren't known yet
    (docs/TYPES.md's "Pending operator constraints")."""

    __slots__ = ("op", "lhs", "rhs", "result", "position", "done")

    def __init__(self, op, lhs, rhs, result, position):
        self.op = op
        self.lhs = lhs
        self.rhs = rhs
        self.result = result
        self.position = position
        self.done = False


class Checker:
    def __init__(self, resolver):
        self.r = resolver
        self.prelude_start = resolver.prelude_start
        self.u = Unifier()
        self.level = 0
        self.diagnostics: list[TypeDiagnostic] = []
        # Symbol -> Type or Scheme, for every variable/parameter/binding.
        self.env: dict = {}
        self.items: dict = {}  # Symbol -> _Item
        self.structs: dict = {}  # name -> _TypeInfo
        self.enums: dict = {}  # name -> _TypeInfo
        # Declarations whose final type is checked for implicit Unknowns at
        # the end: (position, description, type).
        self.decls: list = []
        # Declaration name position -> its type (for tests and LSP hover).
        self.decl_types: dict = {}
        # For LSP hover: field-access position -> (the object's type, the
        # field's type), and method-call position -> the called method's
        # instantiated signature (receiver included).
        self.field_types: dict = {}
        self.method_types: dict = {}
        # type name -> {"inherent": {name: _Method}, "traits": {trait: {name: _Method}}}
        self.impl_methods: dict = {}
        self.trait_methods: dict = {}  # trait name -> {name: _Method}
        self.fn_stack: list = []
        self.tparams: list = []  # stack of {name: TParam}
        self.self_type = None
        self.cstack: list = [[]]  # pending constraints, one list per level
        self.globals: set = set()  # Symbols of top-level non-function `let`s
        self.main_loops: list = []  # loops in the main program, outside any function
        # M26 (docs/ERRORS.md): error sets. `main_accs` is the main
        # program's accumulator stack (like `_FnCtx.accs`); `sites` are the
        # places errors flow straight into the top of the program:
        # (position, error set, excluded).
        self.main_accs: list = [ESet("acc", 0)]
        self.sites: list = []
        # (inferred error set, sealed error set, position, whose) -- checked
        # once every set is solved. `whose` is a function's name, or None for
        # a value flowing where a written `throws` list is expected.
        self.throw_checks: list = []
        self._check_pos = None  # the position `_expect` is checking at
        # (try-body error set, type name, position) per catch arm naming a
        # type, for the "never thrown here" warning.
        self.arm_checks: list = []
        # Where an error the checker can't see through was raised:
        # (position, the try-body error sets enclosing it in its function).
        self.unknown_sites: list = []
        self.catch_all: set = set()  # ids of try-body sets a catch-all handles
        # A catch-all arm's binding -> its try body's error set, so `throw e`
        # re-throws exactly what the body could.
        self.rethrow: dict = {}
        # (type name, position) of every name in a written `throws` list,
        # checked to implement Error once every impl is known.
        self.throws_names: list = []

    # -- entry -----------------------------------------------------------

    def check(self, program: list) -> list[TypeDiagnostic]:
        self._register_types(program)
        self._register_globals(program)
        self._register_methods(program)
        for stmt in program:
            if isinstance(stmt, LetStmt) and self._sym(self._name_pos(stmt)) in self.items:
                self._check_item(self.items[self._sym(self._name_pos(stmt))])
            elif isinstance(stmt, (ImplDecl, TraitDecl)) and not self._in_prelude(stmt.position):
                for method in self._methods_of(stmt):
                    self._method_scheme(method)
        for stmt in program:
            if isinstance(stmt, (StructDecl, EnumDecl, TraitDecl, ImplDecl)):
                continue
            if isinstance(stmt, LetStmt) and self._sym(self._name_pos(stmt)) in self.items:
                continue
            self._check_stmt(stmt)
        self._finish_constraints()
        self._report_implicit()
        self._report_errors()
        diagnostics = [d for d in self.diagnostics if not self._in_prelude(d.position)]
        diagnostics.sort(key=lambda d: d.position)
        return diagnostics

    # -- small utilities -------------------------------------------------

    def _in_prelude(self, position) -> bool:
        return self.prelude_start is not None and position is not None and position >= self.prelude_start

    def _sym(self, position):
        return self.r.position_index.get(position)

    @staticmethod
    def _name_pos(stmt: LetStmt) -> int:
        return stmt.name_position if stmt.name_position is not None else stmt.position

    def _error(self, position: int, message: str) -> None:
        self.diagnostics.append(TypeDiagnostic("mismatch", message, position))

    def _fresh(self) -> TVar:
        return TVar(self.level)

    def _declare(self, symbol, t, position: int, what: str) -> None:
        if symbol is not None:
            self.env[symbol] = t
        self._record(position, what, t)

    def _record(self, position: int, what: str, t) -> None:
        self.decl_types[position] = t
        self.decls.append((position, what, t))

    # -- unification with reporting --------------------------------------

    def _try(self, relation, a, b) -> bool:
        mark = self.u.mark()
        if relation(a, b):
            self.u.commit()
            for source, sealed in self.u.checks:
                self.throw_checks.append((source, sealed, self._check_pos or sealed.position, None))
            self.u.checks = []
            self._after_binding()
            return True
        self.u.rollback(mark)
        self.u.commit()
        return False

    def _try_assign(self, actual, expected) -> bool:
        return self._try(self.u.assign, actual, expected)

    def _try_unify(self, a, b) -> bool:
        return self._try(self.u.unify, a, b)

    def _expect(self, actual, expected, position: int, context: str = "") -> bool:
        """`actual` must be assignable to `expected`; report otherwise."""
        self._check_pos = position
        try:
            ok = self._try_assign(actual, expected)
        finally:
            self._check_pos = None
        if ok:
            return True
        where = f" {context}" if context else ""
        self._error(position, f"Type mismatch{where}: expected {show(expected)}, found {show(actual)}")
        return False

    def _note_none(self, var) -> None:
        var = prune(var)
        if isinstance(var, TVar):
            self.u.note_none(var)
            self.u.commit()

    # -- operator constraints --------------------------------------------

    def _after_binding(self) -> None:
        for var in self.u.take_bound():
            for c in list(var.constraints):
                if not c.done:
                    self._retry(c)

    def _resolve_binop(self, op: str, lhs, rhs):
        """None while undecided, ("ok", type) or ("error", message)."""
        L, R = prune(lhs), prune(rhs)
        unknown = L if isinstance(L, TUnknown) else R if isinstance(R, TUnknown) else None
        symbol = _COMPARE_OPS.get(op, op)
        if op == "+":
            if is_con(L, "String") or is_con(R, "String"):
                return ("ok", STRING)
            if unknown is not None:
                return ("ok", unknown)
            if is_con(L, "Number") and is_con(R, "Number"):
                return ("ok", NUMBER)
            if isinstance(L, TVar) or isinstance(R, TVar):
                return None
        elif op == "*":
            if is_con(L, "Number") and is_con(R, "Number"):
                return ("ok", NUMBER)
            for s, n in ((L, R), (R, L)):
                if is_con(s, "String") and (is_con(n, "Number") or (isinstance(n, TVar) and self._try_unify(n, NUMBER))):
                    return ("ok", STRING)
            if unknown is not None:
                return ("ok", unknown)
            if isinstance(L, TVar) or isinstance(R, TVar):
                return None
        else:  # comparison
            if unknown is not None:
                return ("ok", BOOL)
            for a, b in ((L, R), (R, L)):
                if (is_con(a, "Number") or is_con(a, "String")) and (b is a or is_con(b, a.name)):
                    return ("ok", BOOL)
                if isinstance(b, TVar) and (is_con(a, "Number") or is_con(a, "String")) and self._try_unify(b, a):
                    return ("ok", BOOL)
            if isinstance(L, TVar) and isinstance(R, TVar):
                return None
        return ("error", f"Can't apply '{symbol}' to {show(L)} and {show(R)}")

    def _binop(self, op: str, lhs, rhs, position: int):
        is_compare = op in _COMPARE_OPS
        outcome = self._resolve_binop(op, lhs, rhs)
        if outcome is None:
            result = BOOL if is_compare else self._fresh()
            c = _Constraint(op, lhs, rhs, result, position)
            self._attach(c)
            self.cstack[-1].append(c)
            return result
        kind, value = outcome
        if kind == "error":
            self._error(position, value)
            return BOOL if is_compare else _unchecked()
        return value

    def _attach(self, c: _Constraint) -> None:
        for side in (c.lhs, c.rhs):
            side = prune(side)
            if isinstance(side, TVar) and c not in side.constraints:
                side.constraints.append(c)

    def _retry(self, c: _Constraint) -> None:
        if c.done:
            return
        outcome = self._resolve_binop(c.op, c.lhs, c.rhs)
        if outcome is None:
            self._attach(c)
            return
        c.done = True
        kind, value = outcome
        if kind == "error":
            self._error(c.position, value)
            value = _unchecked()
        if c.op not in _COMPARE_OPS:
            self._expect(value, c.result, c.position)

    def _default_constraints(self, constraints: list, level: int | None) -> list:
        """Default the still-open operands of `constraints` created deeper
        than `level` (all of them when `level` is None) to Number; return
        the constraints still open afterwards."""
        still_open = []
        for c in constraints:
            if c.done:
                continue
            for side in (c.lhs, c.rhs):
                side = prune(side)
                if isinstance(side, TVar) and (level is None or side.level > level):
                    self._try_unify(side, NUMBER)
            self._retry(c)
            if not c.done:
                still_open.append(c)
        return still_open

    def _enter_level(self) -> None:
        self.level += 1
        self.cstack.append([])

    def _exit_level(self) -> None:
        constraints = self.cstack.pop()
        self.level -= 1
        still_open = self._default_constraints(constraints, self.level)
        # A constraint that waits on outer variables belongs to the outer
        # level now, and so does its result: it must not be generalized.
        for c in still_open:
            self.u._lower_levels(c.result, self.level)
        self.u.commit()
        self.cstack[-1].extend(still_open)

    def _finish_constraints(self) -> None:
        self._default_constraints(self.cstack[-1], None)
        self.cstack[-1] = []

    # -- generalization --------------------------------------------------

    def _generalize(self, sig: TFn, declared: list) -> Scheme:
        """Called right after `_exit_level`, so `self.level` is the level
        the function was defined at."""
        ret = prune(sig.ret)
        if (
            isinstance(ret, TVar)
            and ret.none_seen
            and ret.level > self.level
            and not any(v is ret for p in sig.params for v in free_vars(p))
        ):
            ret.ref = NONE  # nothing but `none` ever returned
        used = {p.name for p in declared}
        params = list(declared)
        for var in free_vars(sig):
            if var.level > self.level:
                param = TParam(self._param_name(used))
                var.ref = param
                params.append(param)
        evars, enodes = self._generalize_esets(sig)
        return Scheme(params, sig, evars, enodes)

    def _generalize_esets(self, sig):
        """M26: the signature's error-set variables created inside the
        function (a callback parameter's, say) are quantified like type
        variables. Every other error set created inside it is flattened in
        place, down to error names plus references to outer sets and those
        variables -- so an instance only needs to copy the flattened ones
        that mention a variable."""
        nodes = [n for n in esets(sig) if n.level > self.level]
        evars = [n for n in nodes if n.kind == "var"]
        stop = {n.id for n in evars}
        for node in nodes:
            if node.kind == "acc":
                self._flatten(node, stop)
        enodes = [n for n in nodes if n.kind == "acc" and any(sub.id in stop for sub, _ex in n.subs)]
        return evars, enodes

    def _flatten(self, node, stop: set) -> None:
        names: set = set()
        terminals: dict = {}
        seen: dict = {}
        stack = [(node, EMPTY)]
        while stack:
            n, excluded = stack.pop()
            if n.id in seen:
                met = _meet_ex(seen[n.id], excluded)
                if met == seen[n.id]:
                    continue
                excluded = met
            seen[n.id] = excluded
            if n is not node and (n.level <= self.level or n.kind == "sealed" or n.id in stop):
                terminals[n.id] = (n, excluded)
                continue
            names |= _minus_ex(n.names, excluded)
            for sub, sub_excluded in n.subs:
                stack.append((sub, _union_ex(excluded, sub_excluded)))
        node.names = frozenset(names)
        node.subs = tuple(terminals.values())

    @staticmethod
    def _param_name(used: set) -> str:
        for name in _PARAM_NAMES:
            if name not in used:
                used.add(name)
                return name
        n = 1
        while f"T{n}" in used:
            n += 1
        used.add(f"T{n}")
        return f"T{n}"

    # -- declarations ------------------------------------------------------

    def _register_types(self, program: list) -> None:
        t = TParam("T")
        option = _TypeInfo("Option", [t])
        option.variants = {"none": {}, "some": {"value": t}}
        self.enums["Option"] = option
        t = TParam("T")
        promise = _TypeInfo("Promise", [t])
        # M25 (docs/ERRORS.md, docs/MAHC_FORMAT.md #4.1): `Promise` gains a
        # third variant, `Failed { error }`. The checker doesn't track
        # error types at all yet (M26 is the error-set checker) -- `error`
        # is Unknown/unchecked, exactly like every `RuntimeError` value is
        # never tracked.
        promise.variants = {"Pending": {}, "Settled": {"value": t}, "Failed": {"error": _unchecked()}}
        self.enums["Promise"] = promise

        # M25: the built-in `RuntimeError` enum -- pre-seeded here exactly
        # like `Option`/`Promise` above, since (unlike a user struct/enum)
        # it has no `EnumDecl` AST node for `_register_types`'s own walk,
        # below, to find. Every variant has one field, `message: String`
        # (see compiler/resolve.py's matching pre-seed of `enum_decls`).
        runtime_error = _TypeInfo("RuntimeError", [])
        runtime_error.variants = {
            name: {"message": STRING}
            for name in (
                "DivisionByZero",
                "TypeMismatch",
                "NoSuchField",
                "NoSuchMethod",
                "ArgumentError",
                "IndexOutOfRange",
                "MatchFailed",
                "InputError",
                "Internal",
            )
        }
        self.enums["RuntimeError"] = runtime_error

        decls = [n for n in _walk(program) if isinstance(n, (StructDecl, EnumDecl))]
        # Create every type first so field annotations can name any of them.
        for decl in decls:
            info = _TypeInfo(decl.name, [TParam(tp.name) for tp in decl.type_params])
            (self.structs if isinstance(decl, StructDecl) else self.enums)[decl.name] = info
        for decl in decls:
            if isinstance(decl, StructDecl):
                info = self.structs[decl.name]
                info.fields = self._field_types(
                    info, None, decl.fields, decl.field_types, decl.field_positions, decl.position
                )
            else:
                info = self.enums[decl.name]
                for index, (variant, names) in enumerate(decl.variants):
                    types = decl.variant_field_types[index] if index < len(decl.variant_field_types) else []
                    positions = (
                        decl.variant_field_positions[index] if index < len(decl.variant_field_positions) else []
                    )
                    info.variants[variant] = self._field_types(info, variant, names, types, positions, decl.position)

    def _field_types(self, info, variant, names, types, positions, decl_position) -> dict:
        system = self._in_prelude(decl_position)
        scope = {p.name: p for p in info.params}
        out = {}
        self.tparams.append(scope)
        try:
            for index, name in enumerate(names):
                annotation = types[index] if index < len(types) else None
                position = positions[index] if index < len(positions) else decl_position
                if annotation is not None:
                    out[name] = self._convert(annotation)
                elif system:
                    out[name] = TUnknown("explicit")
                else:
                    var = TVar(0)
                    out[name] = var
                    info.inferred.add((variant, name))
                    info.positions[(variant, name)] = position
                    owner = f"{info.name}.{variant}" if variant else info.name
                    self._record(position, f"field '{name}' of '{owner}'", var)
        finally:
            self.tparams.pop()
        return out

    def _register_globals(self, program: list) -> None:
        for stmt in program:
            if not isinstance(stmt, LetStmt):
                continue
            symbol = self._sym(self._name_pos(stmt))
            if symbol is None:
                continue
            if isinstance(stmt.value, FnExpr):
                self.items[symbol] = _Item(stmt)
            else:
                self.globals.add(symbol)
                self.env[symbol] = self._convert(stmt.type_ann) if stmt.type_ann is not None else TVar(0)

    # -- annotations -------------------------------------------------------

    def _convert(self, texpr):
        if isinstance(texpr, FnType):
            params = [self._convert(p) for p in texpr.params]
            ret = self._convert(texpr.ret) if texpr.ret is not None else NONE
            # M26: no `throws` clause means "inferred", like any other part
            # of a type left unwritten; one seals the function's error set.
            throws = self._sealed(texpr.throws, texpr.position) if texpr.throws is not None else None
            if throws is None:
                throws = ESet("var", self.level)
            return TFn(params, ret, throws=throws)
        if not isinstance(texpr, NamedType):
            return _unchecked()
        name = texpr.name
        args = [self._convert(a) for a in texpr.args]
        if name in PRIMITIVES:
            return PRIMITIVES[name]
        if name == "Unknown":
            return TUnknown("explicit")
        if name == "Self":
            return self.self_type if self.self_type is not None else _unchecked()
        for scope in reversed(self.tparams):
            if name in scope:
                return scope[name]
        if name in ("Vector", "Map"):
            return TCon(name, args)
        info = self.structs.get(name) or self.enums.get(name)
        if info is not None:
            if len(args) != len(info.params):
                args = info.fresh_args(self.level)
            if name == "Promise":
                # What its `.await` re-throws: inferred (M26).
                return TCon(name, args, ESet("var", self.level))
            return TCon(name, args)
        # A trait type (M23) or something the resolver already rejected.
        return _unchecked()

    # -- functions -----------------------------------------------------------

    def _check_item(self, item: _Item):
        if item.state == "done":
            return item.scheme
        if item.state == "busy":
            return Scheme((), item.mono)
        item.state = "busy"
        saved = (self.fn_stack, self.tparams, self.self_type)
        self.fn_stack, self.tparams, self.self_type = [], [], None
        stmt = item.stmt
        annotation = self._convert(stmt.type_ann) if stmt.type_ann is not None else None
        try:
            self._enter_level()

            def on_sig(sig):
                item.mono = annotation if annotation is not None else sig

            sig, declared = self._check_fn(stmt.value, annotation, on_sig=on_sig)
            if annotation is not None:
                self._expect(sig, annotation, stmt.value.position)
            self._exit_level()
            if annotation is not None:
                item.scheme = Scheme((), annotation)
            else:
                item.scheme = self._generalize(sig, declared)
            self._record_fn(stmt, item.scheme.type)
        finally:
            self.fn_stack, self.tparams, self.self_type = saved
            item.state = "done"
        return item.scheme

    def _record_fn(self, stmt: LetStmt, fn_type) -> None:
        fn_type = prune(fn_type)
        self.decl_types[self._name_pos(stmt)] = fn_type
        if isinstance(fn_type, TFn):
            self.decls.append((self._name_pos(stmt), f"the return type of '{stmt.name}'", fn_type.ret))

    def _check_fn(self, fn: FnExpr, expected=None, on_sig=None, self_type=None):
        """Check a function literal. Returns its signature and its own
        declared type parameters."""
        own = {tp.name: TParam(tp.name) for tp in fn.type_params}
        self.tparams.append(own)
        try:
            exp = prune(expected) if expected is not None else None
            if not isinstance(exp, TFn):
                exp = None
            params = []
            for index, name in enumerate(fn.params):
                annotation = fn.param_types[index] if index < len(fn.param_types) else None
                if index == 0 and name == "self" and self_type is not None:
                    params.append(self_type)
                elif annotation is not None:
                    params.append(self._convert(annotation))
                elif exp is not None and index < len(exp.params):
                    params.append(exp.params[index])
                else:
                    params.append(self._fresh())
            required = len(fn.params)
            for index, default in enumerate(fn.defaults):
                if default is not None:
                    required = index
                    break
            if fn.return_type is not None:
                ret = self._convert(fn.return_type)
            elif exp is not None:
                ret = exp.ret
            else:
                ret = self._fresh()
            # M26: the body's error set. A `throws` clause seals what
            # callers see; the body is checked against it at the end.
            acc = ESet("acc", self.level, position=fn.position)
            throws = acc
            sealed = self._sealed(fn.throws, fn.position) if fn.throws is not None else None
            if sealed is not None:
                throws = sealed
                whose = f"'{fn.name}'" if fn.name else "This function"
                self.throw_checks.append((acc, throws, fn.position, whose))
            sig = TFn(params, ret, required, fn.params, throws)
            if on_sig is not None:
                on_sig(sig)

            self.fn_stack.append(_FnCtx(ret, acc))
            try:
                for index, name in enumerate(fn.params):
                    position = fn.param_positions[index] if index < len(fn.param_positions) else fn.position
                    default = fn.defaults[index] if index < len(fn.defaults) else None
                    if default is not None:
                        self._expect(self._check_expr(default, params[index]), params[index], default.position)
                    what = "'self'" if name == "self" else f"parameter '{name}'"
                    self._declare(self._sym(position), params[index], position, what)
                body = self._check_block(fn.body, ret)
                tail = fn.body.tail
                self._expect(body, ret, tail.position if tail is not None else fn.position, "in the returned value")
            finally:
                self.fn_stack.pop()
            return sig, list(own.values())
        finally:
            self.tparams.pop()

    def _check_let_fn(self, stmt: LetStmt, symbol) -> None:
        """A nested `fn f` / `let f = fn ...`: generalized like a top-level
        one, but checked in place."""
        if stmt.type_ann is not None:
            annotation = self._convert(stmt.type_ann)
            self.env[symbol] = annotation
            sig, _ = self._check_fn(stmt.value, annotation)
            self._expect(sig, annotation, stmt.value.position)
            self.decl_types[self._name_pos(stmt)] = annotation
            return

        def on_sig(sig):
            self.env[symbol] = sig

        self._enter_level()
        sig, declared = self._check_fn(stmt.value, None, on_sig=on_sig)
        self._exit_level()
        scheme = self._generalize(sig, declared)
        self.env[symbol] = scheme
        self._record_fn(stmt, sig)

    # -- methods -------------------------------------------------------------

    def _impl_target(self, impl: ImplDecl):
        info = self.structs.get(impl.type_name) or self.enums.get(impl.type_name)
        if impl.type_args:
            return TCon(impl.type_name, [self._convert(a) for a in impl.type_args])
        if info is not None:
            return TCon(impl.type_name, [TParam(p.name) for p in info.params])
        if impl.type_name == "Vector":
            return TCon("Vector", [TParam("T")])
        if impl.type_name == "Map":
            return TCon("Map", [TParam("K"), TParam("V")])
        if impl.type_name in PRIMITIVES:
            return PRIMITIVES[impl.type_name]
        return _unchecked()

    def _register_methods(self, program: list) -> None:
        for decl in _walk(program):
            if isinstance(decl, TraitDecl):
                scope = {tp.name: TParam(tp.name) for tp in decl.type_params}
                system = self._in_prelude(decl.position)
                table = self.trait_methods.setdefault(decl.name, {})
                for method in decl.methods:
                    table[method.name] = _Method(method, _unchecked(), scope, list(scope.values()), system)
            elif isinstance(decl, ImplDecl):
                scope = {tp.name: TParam(tp.name) for tp in decl.type_params}
                self.tparams = [scope]
                try:
                    target = self._impl_target(decl)
                finally:
                    self.tparams = []
                params = list(scope.values())
                for p in free_params(target):
                    if p not in params:
                        params.append(p)
                system = self._in_prelude(decl.position)
                entry = self.impl_methods.setdefault(decl.type_name, {"inherent": {}, "traits": {}})
                table = entry["inherent"] if decl.trait_name is None else entry["traits"].setdefault(decl.trait_name, {})
                for method in decl.methods:
                    table[method.name] = _Method(method, target, scope, params, system)

    def _methods_of(self, decl) -> list:
        if isinstance(decl, TraitDecl):
            table = self.trait_methods.get(decl.name, {})
        else:
            entry = self.impl_methods.get(decl.type_name, {"inherent": {}, "traits": {}})
            table = entry["inherent"] if decl.trait_name is None else entry["traits"].get(decl.trait_name, {})
        return [table[m.name] for m in decl.methods if table.get(m.name) is not None and table[m.name].decl is m]

    def _method_scheme(self, m: _Method):
        """The method's generalized signature (receiver included), checking
        its body first if it hasn't been; None when it isn't typed (a
        prelude method)."""
        if m.state == "done":
            return m.scheme
        if m.state == "busy":
            return Scheme((), m.mono)
        if m.system:
            m.state = "done"
            return None
        m.state = "busy"
        saved = (self.fn_stack, self.tparams, self.self_type)
        self.fn_stack, self.tparams, self.self_type = [], [m.scope], m.target
        try:
            decl = m.decl
            if decl.fn is None:
                # A required trait method: its written signature only.
                own = {tp.name: TParam(tp.name) for tp in decl.type_params}
                self.tparams.append(own)
                params = []
                for index, name in enumerate(decl.params):
                    annotation = decl.param_types[index] if index < len(decl.param_types) else None
                    if index == 0 and name == "self":
                        params.append(m.target)
                    else:
                        params.append(self._convert(annotation) if annotation is not None else _unchecked())
                ret = self._convert(decl.return_type) if decl.return_type is not None else _unchecked()
                required = next((i for i, d in enumerate(decl.defaults) if d is not None), len(decl.params))
                throws = self._sealed(decl.throws, decl.position) if decl.throws is not None else None
                m.scheme = Scheme(list(own.values()) + m.params, TFn(params, ret, required, decl.params, throws))
                return m.scheme
            self._enter_level()

            def on_sig(sig):
                m.mono = sig

            sig, declared = self._check_fn(decl.fn, None, on_sig=on_sig, self_type=m.target)
            self._exit_level()
            m.scheme = self._generalize(sig, declared + m.params)
            if decl.fn.name_position is not None:
                self.decl_types[decl.fn.name_position] = sig
            return m.scheme
        finally:
            self.fn_stack, self.tparams, self.self_type = saved
            m.state = "done"

    def _find_method(self, type_name: str, name: str):
        """The `_Method` `Type.name` dispatches to: the inherent impl first,
        then the one trait impl (or its trait's default) providing it."""
        entry = self.impl_methods.get(type_name)
        if entry is None:
            return None
        if name in entry["inherent"]:
            return entry["inherent"][name]
        hits = []
        for trait, fns in entry["traits"].items():
            if name in fns:
                hits.append(fns[name])
            elif name in self.trait_methods.get(trait, {}):
                hits.append(self.trait_methods[trait][name])
        return hits[0] if len(hits) == 1 else None

    def _native_method(self, receiver, name: str):
        """The signature (receiver included) of a built-in type's native
        method, or None."""
        if not isinstance(receiver, TCon):
            return None
        if name == "to_string":
            return TFn([receiver], STRING, 1, ["self"])
        if receiver.name == "String":
            if name == "len":
                return TFn([STRING], NUMBER, 1, ["self"])
            if name == "char_at":
                return TFn([STRING, NUMBER], STRING, 2, ["self", "i"])
        elif receiver.name == "Vector" and len(receiver.args) == 1:
            (t,) = receiver.args
            table = {
                "len": ([], NUMBER),
                "push": ([("value", t)], NONE),
                "pop": ([], t),
                "push_start": ([("value", t)], NONE),
                "pop_start": ([], t),
                "copy": ([("deep", BOOL)], receiver),
            }
        elif receiver.name == "Map" and len(receiver.args) == 2:
            k, v = receiver.args
            table = {
                "len": ([], NUMBER),
                "keys": ([], TCon("Vector", [k])),
                "values": ([], TCon("Vector", [v])),
                "has": ([("key", k)], BOOL),
                "remove": ([("key", k)], v),
                "copy": ([("deep", BOOL)], receiver),
            }
        else:
            return None
        if receiver.name == "String" or name not in table:
            return None
        extra, ret = table[name]
        required = 1 if name == "copy" else 1 + len(extra)
        return TFn([receiver] + [t for _n, t in extra], ret, required, ["self"] + [n for n, _t in extra])

    def _instance(self, type_name: str):
        """A fresh instance of a named type (for binding an unknown
        receiver), or None."""
        info = self.structs.get(type_name) or self.enums.get(type_name)
        if info is not None:
            return TCon(type_name, info.fresh_args(self.level))
        if type_name in ("String", "Number", "Bool"):
            return PRIMITIVES[type_name]
        if type_name == "Vector":
            return TCon("Vector", [self._fresh()])
        if type_name == "Map":
            return TCon("Map", [self._fresh(), self._fresh()])
        return None

    def _method_sig(self, receiver, name: str):
        """The instantiated signature (receiver included) of `receiver.name`,
        or None."""
        receiver = prune(receiver)
        if isinstance(receiver, TCon):
            m = self._find_method(receiver.name, name)
            if m is not None:
                scheme = self._method_scheme(m)
                return instantiate(scheme, self.level) if scheme is not None else _unchecked()
            return self._native_method(receiver, name)
        return None

    def _infer_receiver(self, var, name: str) -> None:
        """An unbound receiver becomes the one type that has a method
        `name` (docs/TYPES.md's "Inferring a parameter from its uses")."""
        found = set()
        for type_name, entry in self.impl_methods.items():
            if self._find_method(type_name, name) is not None:
                found.add(type_name)
        for type_name, sample in (("String", STRING), ("Vector", TCon("Vector", [NONE])), ("Map", TCon("Map", [NONE, NONE]))):
            if name != "to_string" and self._native_method(sample, name) is not None:
                found.add(type_name)
        if len(found) != 1:
            return
        instance = self._instance(found.pop())
        if instance is not None:
            self._try_unify(var, instance)

    def _check_args_only(self, args, kwargs) -> None:
        for arg in args:
            self._escape(self._check_expr(arg), arg.position)
        for _name, value, _position in kwargs:
            self._escape(self._check_expr(value), value.position)

    def _escape(self, actual, position: int) -> None:
        """M26: a function value passed where the checker can't follow it
        (an `Unknown` parameter, an unchecked prelude method) counts as
        throwing its errors right there."""
        actual = prune(actual)
        if isinstance(actual, TFn):
            self._raise(actual.throws, position)

    def _call_bound(self, sig, receiver, receiver_position, args, kwargs, position):
        """Call a method signature whose receiver was already checked."""
        sig = prune(sig)
        if not isinstance(sig, TFn) or not sig.params:
            self._check_args_only(args, kwargs)
            return sig if isinstance(sig, TUnknown) else _unchecked()
        self._expect(receiver, sig.params[0], receiver_position, "in the receiver")
        names = sig.names[1:] if sig.names is not None else None
        bound = TFn(sig.params[1:], sig.ret, max(sig.required - 1, 0), names, sig.throws)
        return self._apply(bound, args, kwargs, position)

    def _check_method_call(self, expr: MethodCall):
        obj = expr.obj
        if isinstance(obj, Ident) and self._sym(obj.position) is None:
            return self._check_path_call(expr)
        receiver = prune(self._check_expr(obj))
        if isinstance(receiver, TVar):
            self._infer_receiver(receiver, expr.method)
            receiver = prune(receiver)
        sig = self._method_sig(receiver, expr.method)
        if sig is None and isinstance(receiver, TCon) and receiver.name in self.structs:
            if expr.method in self.structs[receiver.name].fields:
                # A closure stored in a field: `p.f()`.
                field = self._field_type(receiver, expr.method, expr.position)[0]
                self.field_types[expr.position] = (receiver, field)
                return self._apply_callee(prune(field), expr.args, expr.kwargs, expr.position)
        if sig is None:
            if isinstance(receiver, TUnknown) and receiver.kind != "unchecked":
                self._raise_unknown(expr.position)
            self._check_args_only(expr.args, expr.kwargs)
            return _unchecked()
        self.method_types[expr.position] = sig
        return self._call_bound(sig, receiver, obj.position, expr.args, expr.kwargs, expr.position)

    def _check_path_call(self, expr: MethodCall):
        """`Type.f(args)` / `Trait.m(recv, args)`."""
        name = expr.obj.name
        if name == "Self" and isinstance(prune(self.self_type), TCon):
            name = prune(self.self_type).name
        if name in self.trait_methods and name not in self.impl_methods and expr.args:
            receiver = prune(self._check_expr(expr.args[0]))
            sig = None
            if isinstance(receiver, TCon):
                sig = self._method_sig(receiver, expr.method)
            if sig is None and expr.method in self.trait_methods[name]:
                scheme = self._method_scheme(self.trait_methods[name][expr.method])
                sig = instantiate(scheme, self.level) if scheme is not None else None
            if sig is None:
                self._check_args_only(expr.args[1:], expr.kwargs)
                return _unchecked()
            self.method_types[expr.position] = sig
            return self._call_bound(sig, receiver, expr.args[0].position, expr.args[1:], expr.kwargs, expr.position)
        m = self._find_method(name, expr.method)
        sig = None
        if m is not None:
            scheme = self._method_scheme(m)
            sig = instantiate(scheme, self.level) if scheme is not None else None
        else:
            instance = self._instance(name)
            if instance is not None:
                sig = self._native_method(instance, expr.method)
        if sig is None:
            self._check_args_only(expr.args, expr.kwargs)
            return _unchecked()
        self.method_types[expr.position] = sig
        return self._apply_callee(prune(sig), expr.args, expr.kwargs, expr.position)

    # -- statements ------------------------------------------------------

    def _check_block(self, block: Block, hint=None):
        diverges = False
        for stmt in block.stmts:
            if self._check_stmt(stmt):
                diverges = True
        result = self._check_expr(block.tail, hint) if block.tail is not None else NONE
        return NEVER if diverges else result

    def _check_stmt(self, stmt) -> bool:
        """Check a statement; True if it never finishes normally."""
        if isinstance(stmt, LetStmt):
            return self._check_let(stmt)
        if isinstance(stmt, AssignStmt):
            self._check_assign(stmt)
            return False
        if isinstance(stmt, ExprStmt):
            return is_con(prune(self._check_expr(stmt.value, used=False)), "Never")
        if isinstance(stmt, PrintStmt):
            for arg in stmt.args:
                self._check_expr(arg)
            for option in (stmt.sep, stmt.end):
                if option is not None:
                    self._check_expr(option)
            return False
        if isinstance(stmt, (IfStmt, MatchStmt, WhileStmt, ForStmt, Block)):
            return is_con(prune(self._check_expr(stmt, used=False)), "Never")
        if isinstance(stmt, ReturnStmt):
            ctx = self.fn_stack[-1] if self.fn_stack else None
            hint = ctx.ret if ctx is not None else None
            value = self._check_expr(stmt.value, hint) if stmt.value is not None else NONE
            if ctx is not None:
                self._expect(value, ctx.ret, stmt.position, "in the returned value")
            return True
        if isinstance(stmt, BreakStmt):
            loops = self._loops()
            loop = loops[-1] if loops else None
            value = self._check_expr(stmt.value) if stmt.value is not None else NONE
            if loop is not None:
                loop.broke = True
                self._expect(value, loop.var, stmt.position, "in the loop's break value")
            return True
        if isinstance(stmt, ContinueStmt):
            return True
        if isinstance(stmt, DeferStmt):
            # M26: a deferred block runs as this scope exits, so its
            # errors are this scope's.
            closure = prune(self._check_expr(stmt.closure_expr))
            if isinstance(closure, TFn):
                self._raise(closure.throws, stmt.position)
            return False
        if isinstance(stmt, (StructDecl, EnumDecl, TraitDecl, ImplDecl)):
            return False
        self._check_expr(stmt)
        return False

    def _check_let(self, stmt: LetStmt) -> bool:
        position = self._name_pos(stmt)
        symbol = self._sym(position)
        if isinstance(stmt.value, FnExpr):
            self._check_let_fn(stmt, symbol)
            return False
        what = f"'{stmt.name}'"
        if symbol in self.globals:
            # A top-level `let`, registered up front (see `_register_globals`).
            declared = self.env[symbol]
            value = self._check_expr(stmt.value, declared)
            self._expect(value, declared, stmt.value.position)
            self._record(position, what, declared)
            return is_con(prune(value), "Never")
        if stmt.type_ann is not None:
            declared = self._convert(stmt.type_ann)
            value = self._check_expr(stmt.value, declared)
            self._expect(value, declared, stmt.value.position)
            self._declare(symbol, declared, position, what)
            return is_con(prune(value), "Never")
        value = self._check_expr(stmt.value)
        pruned = prune(value)
        if is_con(pruned, "None") or is_con(pruned, "Never"):
            # `let x = none` leaves the type open: the first non-none value
            # assigned later decides it.
            var = self._fresh()
            if is_con(pruned, "None"):
                self._note_none(var)
            value = var
        self._declare(symbol, value, position, what)
        return is_con(pruned, "Never")

    def _check_assign(self, stmt: AssignStmt) -> None:
        target = stmt.target
        if isinstance(target, Ident):
            expected = self._ident_type(target)
            value = self._check_expr(stmt.value, expected)
            self._expect(value, expected, stmt.value.position, f"assigning to '{target.name}'")
        elif isinstance(target, FieldAccess):
            obj = self._check_expr(target.obj)
            field_type, info, key = self._field_type(obj, target.field, target.position)
            value = self._check_expr(stmt.value, field_type)
            self._expect_field(value, field_type, info, key, stmt.value.position)
        elif isinstance(target, Index):
            obj = prune(self._check_expr(target.obj))
            key = self._check_expr(target.key)
            value = self._check_expr(stmt.value)
            if is_con(obj, "Vector"):
                self._expect(key, NUMBER, target.key.position, "in the index")
                self._expect(value, obj.args[0], stmt.value.position)
            elif is_con(obj, "Map"):
                self._expect(key, obj.args[0], target.key.position, "in the key")
                self._expect(value, obj.args[1], stmt.value.position)
            elif isinstance(obj, TCon) and obj.name in ("Number", "String", "Bool", "None"):
                self._error(target.position, f"{show(obj)} doesn't support index assignment")
        else:
            self._check_expr(target)
            self._check_expr(stmt.value)

    # -- expressions -----------------------------------------------------

    def _check_expr(self, expr, hint=None, used: bool = True):
        if isinstance(expr, NumberLit):
            return NUMBER
        if isinstance(expr, StringLit):
            return STRING
        if isinstance(expr, BoolLit):
            return BOOL
        if isinstance(expr, Ident):
            return self._ident_type(expr)
        if isinstance(expr, Unary):
            operand = self._check_expr(expr.operand)
            if expr.op == "-":
                self._expect(operand, NUMBER, expr.operand.position, "in the negated value")
                return NUMBER
            return BOOL
        if isinstance(expr, Binary):
            return self._check_binary(expr)
        if isinstance(expr, Call):
            return self._check_call(expr)
        if isinstance(expr, InputExpr):
            return NUMBER
        if isinstance(expr, SleepAsyncExpr):
            self._expect(self._check_expr(expr.arg), NUMBER, expr.arg.position)
            return NONE
        if isinstance(expr, DetachExpr):
            # M26: the task's errors fail its Promise instead of happening
            # here; `.await` re-throws them.
            node = ESet("acc", self.level, position=expr.position)
            accs = self._accs()
            accs.append(node)
            try:
                inner = self._check_expr(expr.call)
            finally:
                accs.pop()
            return TCon("Promise", [inner], node)
        if isinstance(expr, FnExpr):
            sig, _ = self._check_fn(expr, hint)
            return sig
        if isinstance(expr, StructLit):
            return self._check_struct_lit(expr)
        if isinstance(expr, EnumLit):
            return self._check_enum_lit(expr)
        if isinstance(expr, FieldAccess):
            return self._check_field_access(expr)
        if isinstance(expr, MethodCall):
            return self._check_method_call(expr)
        if isinstance(expr, IfStmt):
            return self._check_if(expr, hint, used)
        if isinstance(expr, MatchStmt):
            return self._check_match(expr, hint, used)
        if isinstance(expr, WhileStmt):
            return self._check_while(expr, used)
        if isinstance(expr, ForStmt):
            return self._check_for(expr, used)
        if isinstance(expr, Block):
            return self._check_block(expr, hint)
        if isinstance(expr, VectorLit):
            hinted = prune(hint) if hint is not None else None
            element_hint = hinted.args[0] if is_con(hinted, "Vector") else None
            element = self._fresh()
            for item in expr.items:
                self._expect(self._check_expr(item, element_hint), element, item.position, "in a Vector element")
            return TCon("Vector", [element])
        if isinstance(expr, MapLit):
            key, value = self._fresh(), self._fresh()
            for k, v in expr.pairs:
                self._expect(self._check_expr(k), key, k.position, "in a Map key")
                self._expect(self._check_expr(v), value, v.position, "in a Map value")
            return TCon("Map", [key, value])
        if isinstance(expr, Index):
            return self._check_index(expr)
        if isinstance(expr, NativeCall):
            # M27: an `extern fn`'s body; the declaration's annotations are
            # the native's type, so the call itself is unchecked.
            for arg in expr.args:
                self._check_expr(arg)
            return TUnknown("explicit")
        if isinstance(expr, ThrowExpr):
            # M25 (docs/ERRORS.md): `throw e` never produces a value.
            self._check_throw(expr)
            return NEVER
        if isinstance(expr, TryExpr):
            return self._check_try(expr, hint, used)
        if isinstance(expr, ErrorNode):
            return _unchecked()
        return _unchecked()

    def _ident_type(self, ident: Ident):
        symbol = self._sym(ident.position)
        if symbol is None:
            return _unchecked()
        item = self.items.get(symbol)
        if item is not None:
            return instantiate(self._check_item(item), self.level)
        t = self.env.get(symbol)
        if t is None:
            return _unchecked()
        if isinstance(t, Scheme):
            return instantiate(t, self.level)
        return t

    def _check_binary(self, expr: Binary):
        lhs = self._check_expr(expr.lhs)
        rhs = self._check_expr(expr.rhs)
        op = expr.op
        if op in ("and", "or", "eq", "neq"):
            return BOOL
        if op in _ARITH_OPS:
            self._expect(lhs, NUMBER, expr.lhs.position, f"in the left operand of '{op}'")
            self._expect(rhs, NUMBER, expr.rhs.position, f"in the right operand of '{op}'")
            return NUMBER
        return self._binop(op, lhs, rhs, expr.position)

    def _check_call(self, expr: Call):
        if expr.builtin is not None:
            # M27: the built-in `sin`/`cos` (see resolve.py).
            self._expect(self._check_expr(expr.args[0]), NUMBER, expr.args[0].position)
            return NUMBER
        callee = prune(self._check_expr(expr.callee))
        return self._apply_callee(callee, expr.args, expr.kwargs, expr.position)

    def _apply_callee(self, callee, args, kwargs, position: int):
        """Call a value of type `callee` (already pruned)."""
        if isinstance(callee, TVar):
            fn = TFn([self._fresh() for _ in args], self._fresh(), throws=ESet("var", self.level))
            self._try_unify(callee, fn)
            callee = fn
        if isinstance(callee, TUnknown) or not isinstance(callee, TFn):
            if not isinstance(callee, TUnknown):
                self._error(position, f"{show(callee)} is not a function")
                callee = _unchecked()
            elif callee.kind != "unchecked":
                self._raise_unknown(position)
            self._check_args_only(args, kwargs)
            return callee
        return self._apply(callee, args, kwargs, position)

    def _apply(self, callee: TFn, args, kwargs, position: int):
        """Match arguments to a function type's parameters by position and
        keyword, check each, and give the return type."""
        params = callee.params
        pairs = []  # (argument expression, parameter type or None)
        filled = set()
        for index, arg in enumerate(args):
            if index < len(params):
                pairs.append((arg, params[index]))
                filled.add(index)
            else:
                pairs.append((arg, None))
        if len(args) > len(params):
            self._error(
                position,
                f"Too many arguments: expected at most {len(params)}, found {len(args)}",
            )
        for name, value, kw_position in kwargs:
            if callee.names is None:
                pairs.append((value, None))
                continue
            if name not in callee.names:
                self._error(kw_position, f"No parameter named '{name}'")
                pairs.append((value, None))
                continue
            index = callee.names.index(name)
            if index in filled:
                self._error(kw_position, f"Argument '{name}' is given twice")
            filled.add(index)
            pairs.append((value, params[index]))
        missing = [i for i in range(callee.required) if i not in filled]
        if missing:
            if callee.names is not None:
                names = ", ".join(f"'{callee.names[i]}'" for i in missing)
                self._error(position, f"Missing argument {names}")
            else:
                self._error(
                    position,
                    f"Too few arguments: expected {callee.required}, found {len(filled)}",
                )
        # Closures last, so the other arguments bind type variables first.
        ordered = [p for p in pairs if not isinstance(p[0], FnExpr)] + [p for p in pairs if isinstance(p[0], FnExpr)]
        for arg, param in ordered:
            actual = self._check_expr(arg, param)
            if param is not None:
                self._expect(actual, param, arg.position, "in an argument")
            if param is None or isinstance(prune(param), TUnknown):
                self._escape(actual, arg.position)
        self._raise(callee.throws, position)
        return callee.ret

    # -- structs and enums -------------------------------------------------

    def _expect_field(self, value, field_type, info, key, position) -> None:
        """Assign to a field. An unannotated field's type is inferred from
        every site; when two sites disagree it becomes Unknown, with an
        error at the second one."""
        if info is not None and key in info.inferred:
            if self._try_assign(value, field_type):
                return
            variant, name = key
            owner = f"{info.name}.{variant}" if variant else info.name
            self._error(
                position,
                f"Field '{name}' of '{owner}' was inferred as {show(field_type)} elsewhere, "
                f"but is given {show(value)} here; annotate the field",
            )
            fields = info.variants[variant] if variant else info.fields
            what = f"field '{name}' of '{owner}'"
            fields[name] = TUnknown("implicit", position, what)
            self.decls.append((info.positions[key], what, fields[name]))
            info.inferred.discard(key)
            return
        self._expect(value, field_type, position)

    def _check_struct_lit(self, expr: StructLit):
        info = self.structs.get(expr.type_name)
        if info is None:
            for _name, value in expr.fields:
                self._check_expr(value)
            return _unchecked()
        args = info.fresh_args(self.level)
        mapping = dict(zip(info.params, args))
        for name, value in expr.fields:
            field_type = subst(info.fields.get(name, _unchecked()), mapping)
            actual = self._check_expr(value, field_type)
            self._expect_field(actual, field_type, info, (None, name), value.position)
        return TCon(expr.type_name, args)

    def _check_enum_lit(self, expr: EnumLit):
        if expr.type_name == "Option" and expr.variant == "none":
            return NONE
        info = self.enums.get(expr.type_name)
        fields = info.variants.get(expr.variant) if info is not None else None
        if fields is None:
            for _name, value in expr.fields:
                self._check_expr(value)
            return _unchecked()
        args = info.fresh_args(self.level)
        mapping = dict(zip(info.params, args))
        for name, value in expr.fields:
            field_type = subst(fields.get(name, _unchecked()), mapping)
            actual = self._check_expr(value, field_type)
            self._expect_field(actual, field_type, info, (expr.variant, name), value.position)
        return TCon(expr.type_name, args)

    def _unique_type_with_field(self, field: str):
        """The one struct, or enum with exactly one variant, that has a
        field named `field` -- None when there isn't exactly one."""
        found = [info for info in self.structs.values() if field in info.fields]
        for info in self.enums.values():
            having = [v for v, fields in info.variants.items() if field in fields]
            if having:
                found.append(info if len(having) == 1 else None)
        return found[0] if len(found) == 1 else None

    def _field_type(self, obj, field: str, position: int):
        """(field type, _TypeInfo or None, key) of `obj.field`, reporting
        a missing field."""
        obj = prune(obj)
        if isinstance(obj, TVar):
            info = self._unique_type_with_field(field)
            if info is None:
                return TUnknown("implicit", position, f"'.{field}'"), None, None
            self._try_unify(obj, TCon(info.name, info.fresh_args(self.level)))
            obj = prune(obj)
        if isinstance(obj, TUnknown):
            return obj, None, None
        if isinstance(obj, TCon):
            info = self.structs.get(obj.name)
            if info is not None:
                if field not in info.fields:
                    self._error(position, f"'{obj.name}' has no field '{field}'")
                    return _unchecked(), None, None
                mapping = dict(zip(info.params, obj.args))
                return subst(info.fields[field], mapping), info, (None, field)
            info = self.enums.get(obj.name)
            if info is not None:
                having = [v for v, fields in info.variants.items() if field in fields]
                if len(having) == 1:
                    mapping = dict(zip(info.params, obj.args))
                    return subst(info.variants[having[0]][field], mapping), info, (having[0], field)
                return _unchecked(), None, None
        if isinstance(obj, (TCon, TFn, TParam)):
            self._error(position, f"{show(obj)} has no field '{field}'")
        return _unchecked(), None, None

    def _check_field_access(self, expr: FieldAccess):
        if expr.enum_unit_type is not None:
            info = self.enums.get(expr.enum_unit_type)
            if info is None:
                return _unchecked()
            return TCon(info.name, info.fresh_args(self.level))
        obj = prune(self._check_expr(expr.obj))
        if expr.field == "await":
            if isinstance(obj, TUnknown):
                return obj
            result = self._fresh()
            if not self._try_unify(obj, TCon("Promise", [result], ESet("var", self.level))):
                self._error(expr.position, f"'.await' needs a Promise, found {show(obj)}")
                return _unchecked()
            promise = prune(obj)
            if isinstance(promise, TCon):
                self._raise(promise.throws, expr.position)
            return result
        field = self._field_type(obj, expr.field, expr.position)[0]
        self.field_types[expr.position] = (obj, field)
        return field

    # -- indexing ----------------------------------------------------------

    def _check_index(self, expr: Index):
        obj = prune(self._check_expr(expr.obj))
        key = prune(self._check_expr(expr.key))
        is_range = isinstance(key, TCon) and key.name in _RANGE_TYPES
        if is_con(obj, "Vector"):
            if is_range:
                return obj
            self._expect(key, NUMBER, expr.key.position, "in the index")
            return obj.args[0]
        if is_con(obj, "String"):
            if not is_range:
                self._expect(key, NUMBER, expr.key.position, "in the index")
            return STRING
        if is_con(obj, "Map"):
            self._expect(key, obj.args[0], expr.key.position, "in the key")
            return obj.args[1]
        if isinstance(obj, TUnknown):
            return obj
        if isinstance(obj, TCon) and obj.name in ("Number", "Bool", "None") or isinstance(obj, TFn):
            self._error(expr.position, f"{show(obj)} can't be indexed")
        # A user `Index` impl (M23), a type parameter, or not known yet.
        return _unchecked()

    # -- control flow --------------------------------------------------------

    def _join(self, types: list, position: int):
        """The type of an if/match: every branch's type unified. Branches
        that disagree make it an implicit Unknown, without an error, since
        the value may never be used."""
        live = [t for t in types if not is_con(prune(t), "Never")]
        if not live:
            return NEVER
        if len(live) == 1:
            return live[0]
        result = self._fresh()
        for t in live:
            if not self._try_assign(t, result):
                return TUnknown("implicit", position, "the branches' values")
        return prune(result)

    def _check_if(self, expr: IfStmt, hint, used: bool):
        self._check_expr(expr.cond)
        types = [self._check_block(expr.then, hint)]
        for cond, block in expr.elifs:
            self._check_expr(cond)
            types.append(self._check_block(block, hint))
        if expr.else_ is not None:
            types.append(self._check_block(expr.else_, hint))
        else:
            types.append(NONE)
        if not used:
            return NEVER if all(is_con(prune(t), "Never") for t in types) else NONE
        return self._join(types, expr.position)

    def _check_match(self, expr: MatchStmt, hint, used: bool):
        scrutinee = self._check_expr(expr.scrutinee)
        types = []
        for arm in expr.arms:
            self._check_pattern(arm.pattern, scrutinee)
            if arm.guard is not None:
                self._check_expr(arm.guard)
            types.append(self._check_block(arm.body, hint))
        if not used:
            return NEVER if types and all(is_con(prune(t), "Never") for t in types) else NONE
        return self._join(types, expr.position)

    def _check_try(self, expr: TryExpr, hint, used: bool):
        """M25 (docs/ERRORS.md; minimal -- error-set checking is M26): catch
        form -> check the body and each arm like `_check_match` (a
        `TypePat`'s scrutinee type is the implicit Unknown, unchecked --
        the value's real runtime type test happens at runtime, not here).
        Else form -> join the body and the fallback, exactly like
        `try/else` sugars to `try { E } catch { _ => { F } }`."""
        # M26: the body's errors go to their own set; what the arms don't
        # fully handle flows on to the enclosing one (the arms' own errors
        # go there directly).
        body_errors = ESet("acc", self.level, position=expr.position)
        accs = self._accs()
        accs.append(body_errors)
        try:
            body_type = (
                self._check_block(expr.body, hint)
                if isinstance(expr.body, Block)
                else self._check_expr(expr.body, hint)
            )
        finally:
            accs.pop()
        types = [body_type]
        if expr.fallback is not None:
            handled = ALL
            types.append(self._check_expr(expr.fallback, hint))
        else:
            handled = self._handled(expr.arms)
            for arm in expr.arms:
                named = self._arm_type_name(arm.pattern)
                if named is not None:
                    self.arm_checks.append((body_errors, named, arm.pattern.position))
                self._check_pattern(arm.pattern, _unchecked())
                if isinstance(arm.pattern, BindPat):
                    self.rethrow[self._sym(arm.pattern.position)] = body_errors
                if arm.guard is not None:
                    self._check_expr(arm.guard)
                types.append(self._check_block(arm.body, hint))
        if "*" in handled:
            self.catch_all.add(body_errors.id)
        self._raise(body_errors, expr.position, handled)
        if not used:
            return NEVER if types and all(is_con(prune(t), "Never") for t in types) else NONE
        return self._join(types, expr.position)

    def _loops(self) -> list:
        return self.fn_stack[-1].loops if self.fn_stack else self.main_loops

    def _run_loop(self, body: Block):
        loop = _Loop(self._fresh())
        self._loops().append(loop)
        try:
            self._check_block(body)
        finally:
            self._loops().pop()
        return loop

    def _check_while(self, expr: WhileStmt, used: bool):
        self._check_expr(expr.cond)
        loop = self._run_loop(expr.body)
        endless = isinstance(expr.cond, BoolLit) and expr.cond.value
        if endless and not loop.broke:
            return NEVER
        if not endless:
            self._note_none(loop.var)
        return prune(loop.var) if used else NONE

    def _check_for(self, expr: ForStmt, used: bool):
        iterable = prune(self._check_expr(expr.iterable))
        element = self._element_type(iterable, expr.iterable.position)
        value_symbol = self._sym(expr.value_position)
        if expr.value_type is not None:
            declared = self._convert(expr.value_type)
            self._expect(element, declared, expr.value_position, "in the loop variable")
            element = declared
        self._declare(value_symbol, element, expr.value_position, f"'{expr.value_name}'")
        if expr.index_name is not None:
            index_type = NUMBER
            if expr.index_type is not None:
                index_type = self._convert(expr.index_type)
                self._expect(NUMBER, index_type, expr.index_position, "in the loop index")
            self._declare(self._sym(expr.index_position), index_type, expr.index_position, f"'{expr.index_name}'")
        loop = self._run_loop(expr.body)
        self._note_none(loop.var)
        return prune(loop.var) if used else NONE

    def _element_type(self, iterable, position: int):
        if is_con(iterable, "Vector"):
            return iterable.args[0]
        if is_con(iterable, "Map"):
            return iterable.args[0]
        if is_con(iterable, "String"):
            return STRING
        if is_con(iterable, "Range") or is_con(iterable, "FromRange"):
            return NUMBER
        if isinstance(iterable, TUnknown):
            return iterable
        if isinstance(iterable, TCon) and iterable.name in ("Number", "Bool", "None", "ToRange", "Option", "Promise"):
            self._error(position, f"{show(iterable)} can't be iterated")
        elif isinstance(iterable, TFn):
            self._error(position, f"{show(iterable)} can't be iterated")
        # A user Iterable (M23), a type parameter, or not known yet.
        return _unchecked()

    # -- patterns ------------------------------------------------------------

    def _check_pattern(self, pattern, t) -> None:
        if isinstance(pattern, WildcardPat):
            return
        if isinstance(pattern, (NumberLit, StringLit, BoolLit)):
            literal = self._check_expr(pattern)
            if not self._try_assign(literal, t):
                self._error(pattern.position, f"Pattern of type {show(literal)} can't match a value of type {show(t)}")
            return
        if isinstance(pattern, RangePat):
            bound = pattern.lo if pattern.lo is not None else pattern.hi
            if bound is not None:
                literal = self._check_expr(bound)
                if not self._try_assign(literal, t):
                    self._error(
                        pattern.position, f"Range pattern of {show(literal)} can't match a value of type {show(t)}"
                    )
            return
        if isinstance(pattern, BindPat):
            self._declare(self._sym(pattern.position), t, pattern.position, f"'{pattern.name}'")
            return
        if isinstance(pattern, StructPat):
            info = self.structs.get(pattern.type_name)
            if info is None:
                for _name, sub in pattern.fields:
                    self._check_pattern(sub, _unchecked())
                return
            instance = TCon(info.name, info.fresh_args(self.level))
            if not self._try_unify(instance, t):
                self._error(pattern.position, f"Pattern '{info.name}' can't match a value of type {show(t)}")
            mapping = dict(zip(info.params, instance.args))
            for name, sub in pattern.fields:
                self._check_pattern(sub, subst(info.fields.get(name, _unchecked()), mapping))
            return
        if isinstance(pattern, EnumPat):
            if pattern.type_name == "Option" and pattern.variant == "none":
                return  # `none` can be anything's value
            info = self.enums.get(pattern.type_name)
            fields = info.variants.get(pattern.variant) if info is not None else None
            if fields is None:
                for _name, sub in pattern.fields:
                    self._check_pattern(sub, _unchecked())
                return
            instance = TCon(info.name, info.fresh_args(self.level))
            if not self._try_unify(instance, t):
                self._error(
                    pattern.position,
                    f"Pattern '{info.name}.{pattern.variant}' can't match a value of type {show(t)}",
                )
            mapping = dict(zip(info.params, instance.args))
            for name, sub in pattern.fields:
                self._check_pattern(sub, subst(fields.get(name, _unchecked()), mapping))
            return
        if isinstance(pattern, TypePat):
            # M25: binds `name` to the named type's instance type (not
            # unified against `t` -- a catch arm's scrutinee type is the
            # implicit Unknown, see `_check_try`).
            if pattern.name is None:
                return
            info = self.structs.get(pattern.type_name) or self.enums.get(pattern.type_name)
            instance = TCon(pattern.type_name, info.fresh_args(self.level)) if info is not None else _unchecked()
            self._declare(self._sym(pattern.position), instance, pattern.position, f"'{pattern.name}'")
            return

    # -- errors (M26, docs/ERRORS.md) -----------------------------------------

    def _accs(self) -> list:
        return self.fn_stack[-1].accs if self.fn_stack else self.main_accs

    def _raise(self, node, position: int, excluded: frozenset = EMPTY) -> None:
        """The errors in `node` (minus `excluded`) can be thrown here."""
        if node is None:
            return
        accs = self._accs()
        self.u.flow(node, accs[-1], excluded)
        self.u.commit()
        if accs is self.main_accs and len(accs) == 1:
            self.sites.append((position, node, excluded))

    def _raise_names(self, names, position: int) -> None:
        self._raise(ESet("acc", self.level, names), position)

    def _raise_unknown(self, position: int) -> None:
        """Something the checker can't see through may throw here."""
        self._raise_names({UNKNOWN_ERROR}, position)
        self.unknown_sites.append((position, [n.id for n in self._accs()[1:]]))

    def _is_error_type(self, name: str) -> bool:
        entry = self.impl_methods.get(name)
        return entry is not None and "Error" in entry["traits"]

    def _sealed(self, texprs: list, position: int):
        """A written `throws A | B` (or `throws never`, `[]`) list; None when
        it names a type parameter (`throws E` in a generic function), which
        the checker infers instead, like an unwritten clause."""
        names = []
        for texpr in texprs:
            name = getattr(texpr, "name", None)
            if name is None:
                continue
            if any(name in scope for scope in self.tparams):
                return None
            if name != "Unknown" and name != "RuntimeError":
                names.append(name)
                self.throws_names.append((name, getattr(texpr, "position", position)))
        return ESet("sealed", self.level, declared=names, position=position)

    def _check_throw(self, expr: ThrowExpr) -> None:
        value = expr.value
        if isinstance(value, Ident):
            symbol = self._sym(value.position)
            if symbol in self.rethrow:
                # Re-throwing what a catch-all arm caught.
                self._check_expr(value)
                self._raise(self.rethrow[symbol], expr.position)
                return
        t = prune(self._check_expr(value))
        if isinstance(t, TCon):
            if t.name == "RuntimeError":
                return  # runtime errors are catchable but never tracked
            if t.name in ("Never",):
                return
            if not self._is_error_type(t.name):
                self._error(value.position, f"{show(t)} doesn't implement Error, so it can't be thrown")
                return
            self._raise_names({t.name}, expr.position)
        elif isinstance(t, TFn):
            self._error(value.position, f"{show(t)} doesn't implement Error, so it can't be thrown")
        elif isinstance(t, TUnknown) and t.kind == "unchecked":
            return
        else:
            self._raise_unknown(expr.position)

    @staticmethod
    def _irrefutable(pattern) -> bool:
        return isinstance(pattern, (WildcardPat, BindPat))

    def _arm_type_name(self, pattern):
        """The error type a catch arm's pattern names, if any."""
        if isinstance(pattern, (TypePat, StructPat, EnumPat)):
            return pattern.type_name
        return None

    def _handled(self, arms: list) -> frozenset:
        """The error types a `try`'s arms fully handle: unguarded arms
        covering a whole type (`e: T`, `T { .. }` with only bindings, or
        every variant of an enum), or everything (`_`/`e`)."""
        handled = set()
        variants: dict = {}
        for arm in arms:
            if arm.guard is not None:
                continue
            p = arm.pattern
            if self._irrefutable(p):
                return ALL
            if isinstance(p, TypePat):
                handled.add(p.type_name)
            elif isinstance(p, StructPat) and all(self._irrefutable(sub) for _n, sub in p.fields):
                handled.add(p.type_name)
            elif isinstance(p, EnumPat) and all(self._irrefutable(sub) for _n, sub in p.fields):
                variants.setdefault(p.type_name, set()).add(p.variant)
        for name, covered in variants.items():
            info = self.enums.get(name)
            if info is not None and covered >= set(info.variants):
                handled.add(name)
        return frozenset(handled)

    def _report_errors(self) -> None:
        roots = [self.main_accs[0]]
        roots += [node for _p, node, _e in self.sites]
        roots += [n for check in self.throw_checks for n in check[:2]]
        roots += [node for node, _n, _p in self.arm_checks]
        for table in (self.decl_types, self.method_types):
            for t in table.values():
                roots += esets(t)
        for obj, field in self.field_types.values():
            roots += esets(obj) + esets(field)
        for t in self.env.values():
            roots += esets(t.type if isinstance(t, Scheme) else t)
        solve(roots)

        for position, node, excluded in self.sites:
            names = sorted(_minus_ex(node.solved, excluded) - {UNKNOWN_ERROR})
            if names:
                self.diagnostics.append(
                    TypeDiagnostic("unhandled", f"Unhandled error: {', '.join(names)}", position)
                )
        for source, sealed, position, whose in self.throw_checks:
            extra = sorted(source.solved - sealed.declared - {UNKNOWN_ERROR})
            if not extra:
                continue
            listed = ", ".join(extra)
            if whose is not None:
                message = f"{whose} can throw {listed}, which isn't in its throws list"
            else:
                allowed = " | ".join(sorted(sealed.declared)) or "never"
                message = f"This function can throw {listed}, but its expected type only allows throws {allowed}"
            self.diagnostics.append(TypeDiagnostic("unhandled", message, position))
        for name, position in self.throws_names:
            if not self._is_error_type(name):
                self._error(position, f"{name} doesn't implement Error, so it can't be in a throws list")
        for node, name, position in self.arm_checks:
            if name == "RuntimeError" or UNKNOWN_ERROR in node.solved or name in node.solved:
                continue
            self.diagnostics.append(TypeDiagnostic("warning", f"{name} is never thrown here", position))
        for position, enclosing in self.unknown_sites:
            if any(i in self.catch_all for i in enclosing):
                continue
            self.diagnostics.append(
                TypeDiagnostic("implicit", "Can't infer what this throws; annotate it", position)
            )

    # -- the explicit level ------------------------------------------------

    def _report_implicit(self) -> None:
        """Every declaration whose type ended up containing something the
        checker couldn't infer: an unbound variable (that never even saw
        `none`) or an implicit Unknown. Each such hole is reported once, at
        the first declaration (by position) it appears in."""
        seen_vars = set()
        seen_unknowns = set()
        for position, what, t in sorted(self.decls, key=lambda d: d[0]):
            if self._in_prelude(position):
                continue
            bad = False
            for var in free_vars(t):
                if var.none_seen or var.id in seen_vars:
                    continue
                seen_vars.add(var.id)
                bad = True
            for unknown in unknowns(t):
                if unknown.kind != "implicit" or id(unknown) in seen_unknowns:
                    continue
                seen_unknowns.add(id(unknown))
                bad = True
            if bad:
                self.diagnostics.append(
                    TypeDiagnostic("implicit", f"Can't infer the type of {what}; annotate it", position)
                )
