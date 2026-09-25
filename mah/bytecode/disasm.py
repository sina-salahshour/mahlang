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

from .format import BUILTIN_TYPES, TAG_DEC, TAG_FALSE, TAG_INT, TAG_NONE, TAG_STR, TAG_TRUE
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
        if t_idx in (0, 1):
            return BUILTIN_TYPES[t_idx][0]
        decl = self.p.types[t_idx - 2]
        return self.p.strings[decl.name]

    def variant_name(self, t_idx: int, variant_idx: int) -> str:
        if t_idx in (0, 1):
            return BUILTIN_TYPES[t_idx][1][variant_idx][0]
        decl = self.p.types[t_idx - 2]
        return self.p.strings[decl.variants[variant_idx][0]]

    def func(self, f_idx: int) -> str:
        fn = self.p.functions[f_idx]
        name = self.p.strings[fn.name] if fn.name is not None else ""
        return f"fn#{f_idx}<{name}>"


def _instr_line(r: _Renderer, i: int, instr) -> str:
    op = instr.op
    a = instr.args
    if op == "halt" or op == "matchfail" or op == "deferpush" or op == "deferscopepop":
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
    elif op == "deferadd":
        rendered = f"closure={_addr(a[0])}"
    elif op in ("deferpeek", "deferpop"):
        rendered = f"dest={_addr(a[0])}"
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
        for i, t in enumerate(program.types):
            idx = i + 2
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
            params_text = ", ".join(
                f"{program.strings[pname]}=" if has_default else program.strings[pname]
                for pname, has_default in fn.params
            )
        else:
            params_text = str(fn.param_count)  # 1.0 file: no names/defaults
        lines.append(
            f"  {i}: entry={fn.entry} slots={fn.slot_count} params=({params_text}) name={name}"
        )

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
