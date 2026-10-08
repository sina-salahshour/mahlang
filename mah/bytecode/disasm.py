"""`Program` -> human-readable disassembly text -- `mah dis` (replaces the
old `mah build`'s raw IR-tuple dump). Exact layout isn't normative (only
the file format and VM semantics are, docs/MAHC_FORMAT.md's own concern);
this just needs to be readable: a header (version/counts), the
types/natives/functions tables, then one line per instruction with
resolved operands (strings quoted, constants rendered as Mah literals,
addresses as `(depth,slot)`, functions as `fn#N<name>`, types by name), and
a trailing `; file:line:col` comment when DEBUG info covers that
instruction.
"""

from __future__ import annotations

from ..runtime_values import PRIMITIVE_TYPE_NAMES
from .format import TAG_DEC, TAG_FALSE, TAG_INT, TAG_NONE, TAG_STR, TAG_TRUE, builtin_types_for
from .program import Program


def _quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _addr(a) -> str:
    if a is None:
        return "-"
    depth, slot = a
    return f"({depth},{slot})"


def _addr_list(addrs) -> str:
    return "[" + ", ".join(_addr(a) for a in addrs) + "]"


def _str_list(r: "_Renderer", indices) -> str:
    return "[" + ", ".join(r.s(i) for i in indices) + "]"


class _Renderer:
    def __init__(self, program: Program):
        self.p = program
        # M25: how many built-in types (and where user types start
        # numbering from) depends on the file's own minor version.
        self.builtin_types = builtin_types_for(program.minor)

    def s(self, idx: int) -> str:
        return _quote(self.p.strings[idx])

    def s_opt(self, idx) -> str:
        return "-" if idx is None else self.s(idx)

    def const(self, idx: int) -> str:
        c = self.p.constants[idx]
        if c.tag == TAG_NONE:
            return "none"
        if c.tag == TAG_FALSE:
            return "false"
        if c.tag == TAG_TRUE:
            return "true"
        if c.tag == TAG_INT:
            return str(c.value)
        if c.tag == TAG_DEC:
            return self.p.strings[c.value]
        if c.tag == TAG_STR:
            return _quote(self.p.strings[c.value])
        return f"<const?{c.tag}>"

    def type_name(self, t_idx: int) -> str:
        if t_idx < len(self.builtin_types):
            return self.builtin_types[t_idx][0]
        decl = self.p.types[t_idx - len(self.builtin_types)]
        return self.p.strings[decl.name]

    def variant_name(self, t_idx: int, variant_idx: int) -> str:
        if t_idx < len(self.builtin_types):
            return self.builtin_types[t_idx][1][variant_idx][0]
        decl = self.p.types[t_idx - len(self.builtin_types)]
        return self.p.strings[decl.variants[variant_idx][0]]

    def typeref(self, ref) -> str:
        """M41a: a META type annotation, as Mah would write it."""
        if ref.tag == 0:
            return "Unknown"
        if ref.tag == 1:
            name = self.type_name(ref.index) if ref.kind == 0 else PRIMITIVE_TYPE_NAMES[ref.index]
            return name + self._args(ref.args)
        if ref.tag == 2:
            text = f"fn({', '.join(self.typeref(p) for p in ref.args)}) -> {self.typeref(ref.ret)}"
            return text + self.throws(ref.throws)
        if ref.tag == 3:
            return self.p.strings[ref.name]
        if ref.tag == 4:
            return "Self"
        if ref.tag == 5:
            return "Never"
        return self.p.strings[ref.name] + self._args(ref.args)

    def _args(self, args) -> str:
        return "<" + ", ".join(self.typeref(a) for a in args) + ">" if args else ""

    def throws(self, throws) -> str:
        if throws is None:
            return ""
        return " throws " + (" | ".join(self.typeref(t) for t in throws) if throws else "never")

    def func(self, f_idx: int) -> str:
        fn = self.p.functions[f_idx]
        name = self.p.strings[fn.name] if fn.name is not None else ""
        return f"fn#{f_idx}<{name}>"


def _instr_line(r: _Renderer, i: int, instr) -> str:
    op = instr.op
    a = instr.args
    if op in ("halt", "matchfail", "deferpush", "deferscopepop", "atomicbegin", "atomicend", "atomicabort", "retry"):
        rendered = ""
    elif op == "move":
        rendered = f"src={_addr(a[0])} dest={_addr(a[1])}"
    elif op == "loadk":
        rendered = f"const={r.const(a[0])} dest={_addr(a[1])}"
    elif op == "jmp":
        rendered = f"target={a[0]}"
    elif op == "jmpf":
        rendered = f"cond={_addr(a[0])} target={a[1]}"
    elif op == "jmpset":
        rendered = f"param={_addr(a[0])} target={a[1]}"
    elif op in ("add", "sub", "mul", "div", "idiv", "mod", "pow", "eq", "neq", "lt", "gt", "le", "ge", "and", "or"):
        rendered = f"a={_addr(a[0])} b={_addr(a[1])} dest={_addr(a[2])}"
    elif op in ("neg", "not"):
        rendered = f"a={_addr(a[0])} dest={_addr(a[1])}"
    elif op == "closure":
        rendered = f"fn={r.func(a[0])} dest={_addr(a[1])}"
    elif op == "call":
        rendered = f"callee={_addr(a[0])} args={_addr_list(a[1])}"
    elif op == "callkw":
        rendered = f"callee={_addr(a[0])} args={_addr_list(a[1])} kwnames={_str_list(r, a[2])}"
    elif op == "callspread":
        rendered = f"callee={_addr(a[0])} args={_addr(a[1])} kwargs={_addr(a[2])}"
    elif op == "callmethodspread":
        rendered = (
            f"recv={_addr(a[0])} name={r.s(a[1])} args={_addr(a[2])} kwargs={_addr(a[3])} trait={r.s_opt(a[4])}"
        )
    elif op == "spread":
        rendered = f"target={_addr(a[0])} source={_addr(a[1])} keyword={a[2]}"
    elif op == "loadtype":
        what = r.type_name(a[1]) if a[0] == 0 else PRIMITIVE_TYPE_NAMES[a[1]]
        rendered = f"type={what} dest={_addr(a[2])}"
    elif op == "decorate":
        kind, x, y, values = a
        if kind == 0:
            target = f"fn {r.func(x)}"
        elif kind == 1:
            target = f"param {y} of fn {r.func(x)}"
        elif kind == 2:
            target = f"type {r.type_name(x)}"
        elif kind == 3:
            target = f"field {y} of {r.type_name(x)}"
        else:
            target = f"variant {y} of {r.type_name(x)}"
        rendered = f"target={target} values={_addr_list(values)}"
    elif op == "paramhooks":
        rendered = f"fn={r.func(a[0])} param={a[1]} dest={_addr(a[2])}"
    elif op == "ret":
        rendered = f"value={_addr(a[0])}"
    elif op == "retval":
        rendered = f"dest={_addr(a[0])}"
    elif op == "callmethod":
        rendered = f"recv={_addr(a[0])} name={r.s(a[1])} args={_addr_list(a[2])} trait={r.s_opt(a[3])}"
    elif op == "callmethodkw":
        rendered = (
            f"recv={_addr(a[0])} name={r.s(a[1])} args={_addr_list(a[2])} "
            f"kwnames={_str_list(r, a[3])} trait={r.s_opt(a[4])}"
        )
    elif op == "defmethod":
        rendered = f"closure={_addr(a[0])} type={r.s(a[1])} trait={r.s_opt(a[2])} name={r.s(a[3])} is_method={a[4]}"
    elif op == "detach":
        rendered = f"callee={_addr(a[0])} args={_addr_list(a[1])} dest={_addr(a[2])}"
    elif op == "detachkw":
        rendered = f"callee={_addr(a[0])} args={_addr_list(a[1])} kwnames={_str_list(r, a[2])} dest={_addr(a[3])}"
    elif op == "detachmethod":
        rendered = (
            f"recv={_addr(a[0])} name={r.s(a[1])} args={_addr_list(a[2])} trait={r.s_opt(a[3])} dest={_addr(a[4])}"
        )
    elif op == "detachmethodkw":
        rendered = (
            f"recv={_addr(a[0])} name={r.s(a[1])} args={_addr_list(a[2])} kwnames={_str_list(r, a[3])} "
            f"trait={r.s_opt(a[4])} dest={_addr(a[5])}"
        )
    elif op == "await":
        rendered = f"promise={_addr(a[0])} dest={_addr(a[1])}"
    elif op == "struct":
        rendered = f"type={r.type_name(a[0])} values={_addr_list(a[1])} dest={_addr(a[2])}"
    elif op == "enum":
        rendered = (
            f"type={r.type_name(a[0])} variant={r.variant_name(a[0], a[1])} "
            f"values={_addr_list(a[2])} dest={_addr(a[3])}"
        )
    elif op == "vector":
        rendered = f"items={_addr_list(a[0])} dest={_addr(a[1])}"
    elif op == "map":
        rendered = f"pairs={_addr_list(a[0])} dest={_addr(a[1])}"
    elif op == "getfield":
        rendered = f"obj={_addr(a[0])} field={r.s(a[1])} dest={_addr(a[2])}"
    elif op == "setfield":
        rendered = f"obj={_addr(a[0])} field={r.s(a[1])} src={_addr(a[2])}"
    elif op == "matchstruct":
        rendered = f"value={_addr(a[0])} type={r.type_name(a[1])} dest={_addr(a[2])}"
    elif op == "matchenum":
        rendered = f"value={_addr(a[0])} type={r.type_name(a[1])} variant={r.variant_name(a[1], a[2])} dest={_addr(a[3])}"
    elif op == "matchrange":
        rendered = f"value={_addr(a[0])} lo={_addr(a[1])} hi={_addr(a[2])} inclusive={a[3]} dest={_addr(a[4])}"
    elif op == "matchtype":
        rendered = f"value={_addr(a[0])} type={r.type_name(a[1])} dest={_addr(a[2])}"
    elif op == "deferadd":
        rendered = f"closure={_addr(a[0])}"
    elif op in ("deferpeek", "deferpop", "deferdepth"):
        rendered = f"dest={_addr(a[0])}"
    elif op == "deferabove":
        rendered = f"depth={_addr(a[0])} dest={_addr(a[1])}"
    elif op == "throw":
        rendered = f"value={_addr(a[0])}"
    elif op == "sharedget":  # M44 (1.21)
        mode = {0: "copy", 1: "working"}.get(a[2], str(a[2]))
        rendered = f"index={a[0]} name={r.s(a[1])} mode={mode} dest={_addr(a[3])}"
    elif op == "sharedset":
        rendered = f"index={a[0]} name={r.s(a[1])} src={_addr(a[2])}"
    elif op == "native":
        native = r.p.natives[a[0]]
        rendered = f"fn={r.s(native.name)} args={_addr_list(a[1])} dest={_addr(a[2])}"
    else:
        rendered = " ".join(repr(x) for x in a)
    return f"  {i:>5}  {op:<14}{rendered}"


def disassemble(program: Program) -> str:
    lines = []
    lines.append(f"MAHC version 1.{program.minor}")
    lines.append(
        f"strings={len(program.strings)} constants={len(program.constants)} types={len(program.types)} "
        f"natives={len(program.natives)} functions={len(program.functions)} code={len(program.code)} "
        f"debug={'yes' if program.debug is not None else 'no'}"
    )
    r = _Renderer(program)

    if program.types:
        lines.append("")
        lines.append("TYPES:")
        base = len(builtin_types_for(program.minor))
        for i, t in enumerate(program.types):
            idx = i + base
            name = program.strings[t.name]
            if t.kind == 0:
                fields = ", ".join(program.strings[f] for f in t.fields)
                lines.append(f"  {idx}: struct {name} {{ {fields} }}")
            else:
                variants = "; ".join(
                    f"{program.strings[vn]}({', '.join(program.strings[f] for f in vf)})"
                    for vn, vf in t.variants
                )
                lines.append(f"  {idx}: enum {name} {{ {variants} }}")

    if program.natives:
        lines.append("")
        lines.append("NATIVES:")
        for i, n in enumerate(program.natives):
            lines.append(f"  {i}: {program.strings[n.name]}/{n.arity}")

    lines.append("")
    lines.append("FUNCTIONS:")
    for i, fn in enumerate(program.functions):
        name = program.strings[fn.name] if fn.name is not None else "<anon>"
        if fn.params is not None:
            nparams = len(fn.params)
            kw_at = nparams - 1 if fn.rest & 2 else -1
            pos_at = nparams - 1 - (1 if fn.rest & 2 else 0) if fn.rest & 1 else -1
            params_text = ", ".join(
                ("..." if i == pos_at else "**" if i == kw_at else "")
                + (f"{program.strings[pname]}=" if has_default else program.strings[pname])
                for i, (pname, has_default) in enumerate(fn.params)
            )
        else:
            params_text = str(fn.param_count)  # 1.0 file: no names/defaults
        lines.append(
            f"  {i}: entry={fn.entry} slots={fn.slot_count} params=({params_text}) name={name}"
        )

    if program.minor >= 4:
        lines.append("")
        lines.append("HANDLERS:")
        if not program.handlers:
            lines.append("  (none)")
        for start, end, handler, slot in program.handlers:
            lines.append(f"  [{start}, {end}) -> {handler} slot={slot}")

    if program.tests:
        # M28 (docs/MAHC_FORMAT.md #4.9): a `mah test` build's test table.
        lines.append("")
        lines.append("TESTS:")
        for t in program.tests:
            lines.append(f"  {program.strings[t.name]!r} slot={t.slot} line={t.line}")

    if program.meta is not None:
        # M41a (docs/MAHC_FORMAT.md #4.10): the written annotations, docs
        # and constant defaults.
        lines.append("")
        lines.append("META:")
        for i, m in enumerate(program.meta.functions):
            if not m.has_meta:
                continue
            fn = program.functions[i]
            name = program.strings[fn.name] if fn.name is not None else "<anon>"
            tparams = f"<{', '.join(program.strings[t] for t in m.type_params)}>" if m.type_params else ""
            params = []
            for (pname, _has_default), pm in zip(fn.params or [], m.params):
                text = f"{program.strings[pname]}: {r.typeref(pm.type)}"
                if pm.default == 2:
                    text += f" = {r.const(pm.const)}"
                elif pm.default == 1:
                    text += " = ..."
                params.append(text)
            lines.append(f"  fn#{i} {name}{tparams}({', '.join(params)}) -> {r.typeref(m.returns)}{r.throws(m.throws)}")
            if m.doc is not None:
                lines.append(f"      doc: {_quote(program.strings[m.doc]).replace(chr(10), chr(92) + chr(110))}")
        for i, tm in enumerate(program.meta.types):
            decl = program.types[i]
            name = program.strings[decl.name]
            tparams = f"<{', '.join(program.strings[t] for t in tm.type_params)}>" if tm.type_params else ""
            if decl.kind == 0:
                fields = ", ".join(
                    f"{program.strings[f]}: {r.typeref(ref)}" for f, (ref, _doc) in zip(decl.fields, tm.body)
                )
                lines.append(f"  type#{i + len(r.builtin_types)} struct {name}{tparams} {{ {fields} }}")
            else:
                variants = "; ".join(
                    f"{program.strings[vn]}({', '.join(r.typeref(ref) for ref in refs)})"
                    for (vn, _vf), (_vdoc, refs) in zip(decl.variants, tm.body)
                )
                lines.append(f"  type#{i + len(r.builtin_types)} enum {name}{tparams} {{ {variants} }}")
            if tm.doc is not None:
                lines.append(f"      doc: {_quote(program.strings[tm.doc]).replace(chr(10), chr(92) + chr(110))}")

    debug_by_pc: dict[int, tuple[int, int, int]] = {}
    if program.debug is not None:
        prev = None
        for pc, file_idx, line, col in program.debug.runs:
            debug_by_pc[pc] = (file_idx, line, col)

    lines.append("")
    lines.append("CODE:")
    file_names = (
        [program.strings[i] for i in program.debug.files] if program.debug is not None else []
    )
    current_debug = None
    for i, instr in enumerate(program.code):
        line = _instr_line(r, i, instr)
        if i in debug_by_pc:
            current_debug = debug_by_pc[i]
        if current_debug is not None and current_debug[1] != 0:
            file_idx, ln, col = current_debug
            fname = file_names[file_idx] if file_idx < len(file_names) else f"file{file_idx}"
            line += f"  ; {fname}:{ln}:{col}"
        lines.append(line)

    return "\n".join(lines) + "\n"
