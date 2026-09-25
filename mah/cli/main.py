#!/usr/bin/env python3
import argparse
import os
import re
import shlex
import sys

from ..bytecode.decode import decode
from ..bytecode.disasm import disassemble
from ..bytecode.format import MahcFormatError
from ..bytecode.lower import line_col
from ..code_interpreter import run_bytes
from ..runtime_values import MahRuntimeError
from ..compiler.codegen import Codegen, CodeBuffer
from ..compiler.driver import _format_parser_errors as _driver_format_parser_errors
from ..compiler.driver import compile_to_bytes
from ..compiler.lexer import Lexer
from ..compiler.parser import Parser
from ..compiler.resolve import Resolver
from ..preprocessor import demangle_message, preprocess
from ..project.init import init_project
from ..project.manifest import MANIFEST_NAME, MahProjectError, find_manifest, load_project

sys.tracebacklimit = 0

_SUBCOMMANDS = {"run", "build", "runc", "dis", "lsp", "init", "format"}


def read_file(file_name):
    try:
        with open(file_name) as f:
            input_str = f.read()
    except FileNotFoundError:
        print(f"Error: file not found '{file_name}'", file=sys.stderr)
        sys.exit(2)
    return input_str


def generate_code(input_str: str, pp) -> CodeBuffer:
    """Front end only (lex -> parse -> resolve -> codegen), stopping short
    of `mah/bytecode/lower.py` -- kept for `tests/test_error_recovery.py`,
    which inspects the raw `CodeBuffer` IR directly. `mah build`/`mah run`
    go through `mah.compiler.driver.compile_to_program`/`compile_to_bytes`
    instead (which do lower all the way to a `Program`)."""
    lexer = Lexer(input_str)
    parser = Parser(lexer)
    program = parser.parse_program()

    if parser.errors:
        # M6: the parser is forgiving (it recovers and keeps going so a
        # tool -- the LSP -- can report every syntax mistake in one pass),
        # but `mah build`/`run` still refuse outright to build/execute a
        # program with any parse error, collected or not -- see
        # docs/V2_DESIGN.md's M6 milestone. Fold every collected error into
        # one SyntaxError message (each position already substituted with
        # its real file#line:col label) so it composes with the single-
        # exception handling `main()`'s outer `except` block already does.
        raise SyntaxError(_format_parser_errors(pp, parser.errors))

    resolver = Resolver(prelude_start=pp.prelude_start)
    resolver.resolve_program(program)

    codegen = Codegen(resolver.global_frame)
    return codegen.generate(program)


def find_error_line(input_str: str, pos: int):
    """1-based `(line, col)` of `pos` in `input_str`. M14: delegates to
    `mah.bytecode.lower.line_col`, which fixes this function's old
    off-by-one column (a token at the very start of a line used to report
    column 2) -- see that function's docstring; DEBUG-section positions and
    compile-time error labels must agree exactly."""
    if pos is None or pos > len(input_str):
        return None
    return line_col(input_str, pos)


def _format_parser_errors(pp, errors: list) -> str:
    """M6: render every collected `parser.errors` entry as one combined
    message with each position resolved to a `#line:col`/`file#line:col`
    label. Delegates to the compile driver's implementation so `run`,
    `build`, and `generate_code` can never format these differently."""
    return _driver_format_parser_errors(pp, errors)

def _location_label(pp, entry_path: str, combined_offset: int) -> str:
    """Render a ``#line:row`` (entry file) or ``file#line:row`` location."""
    path, src_offset = pp.map_to_source(combined_offset)
    line_info = find_error_line(pp.files.get(path, ""), src_offset)
    if not line_info:
        return None
    line, row = line_info
    if path == entry_path:
        return f"#{line}:{row}"
    return f"{os.path.basename(path)}#{line}:{row}"


def _default_mahc_path(source_path: str) -> str:
    root, _ext = os.path.splitext(source_path)
    return root + ".mahc"


def _resolve_project(file_arg: str | None):
    """M15: shared project-mode lookup for `run`/`build` -- `file_arg` is
    `None` (search upward from the current directory) or a directory
    (scoped lookup, no upward search). Loads and validates the manifest,
    then checks the entry file exists (same error either command reports).
    Returns `(project, None)` on success, or `(None, exit_code)` once the
    error has already been printed to stderr."""
    if file_arg is None:
        manifest_path = find_manifest(os.getcwd())
        if manifest_path is None:
            print(
                f"error: no FILE given and no {MANIFEST_NAME} found in {os.getcwd()} or its parents",
                file=sys.stderr,
            )
            return None, 2
    else:
        manifest_path = os.path.join(file_arg, MANIFEST_NAME)
        if not os.path.isfile(manifest_path):
            print(f"error: {file_arg} has no {MANIFEST_NAME}", file=sys.stderr)
            return None, 2

    try:
        project = load_project(manifest_path)
    except MahProjectError as e:
        print(f"error: {e}", file=sys.stderr)
        return None, 2

    if not os.path.isfile(project.entry):
        rel_entry = os.path.relpath(project.entry, project.root)
        print(
            f"error: entry file {rel_entry} (from {manifest_path}) does not exist",
            file=sys.stderr,
        )
        return None, 2

    return project, None


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mah",
        description="The Mah programming language toolchain.",
    )
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser(
        "run", help="compile and run a .mh file, or the current project's entry point"
    )
    run_parser.add_argument(
        "file", nargs="?", default=None,
        help="path to the .mh file to run, or a project directory (defaults to the "
             "current project, found by searching upward from the current directory)",
    )

    build_parser = subparsers.add_parser(
        "build", help="compile a .mh file, or the current project's targets, to .mahc bytecode"
    )
    build_parser.add_argument(
        "file", nargs="?", default=None,
        help="path to the .mh file to compile, or a project directory (defaults to the "
             "current project, found by searching upward from the current directory)",
    )
    build_parser.add_argument(
        "-o", "--out", metavar="PATH",
        help="write the .mahc file to PATH instead of alongside the source file "
             "(single-file mode only)",
    )
    build_parser.add_argument(
        # M15: no `choices=` -- in project mode `--target` names a
        # `[[target]]` entry, not just "debug"/"release"; single-file mode
        # validates the value manually (see main()) so it still only
        # accepts "debug"/"release" there.
        "--target", default=None,
        help="single-file mode: 'debug' (default) includes source positions for runtime "
             "error messages, 'release' omits them; project mode: the name of one "
             "[[target]] entry to build (default: build all of them)",
    )

    init_parser = subparsers.add_parser(
        "init", help="create a new Mah project (in DIR, or the current directory)"
    )
    init_parser.add_argument("directory", nargs="?", default=".", help="where to create the project")

    runc_parser = subparsers.add_parser("runc", help="run a compiled .mahc bytecode file")
    runc_parser.add_argument("file", help="path to the .mahc file to run")

    dis_parser = subparsers.add_parser("dis", help="disassemble a .mahc bytecode file")
    dis_parser.add_argument("file", help="path to the .mahc file to disassemble")
    dis_parser.add_argument(
        "-o", "--out", metavar="PATH",
        help="write the disassembly to PATH instead of printing it",
    )

    format_parser = subparsers.add_parser(
        "format", help="rewrite .mh files in the standard layout (only whitespace changes)"
    )
    format_parser.add_argument(
        "paths", nargs="*",
        help="files or directories to format ('-' formats stdin to stdout); defaults to the "
             "current project, or the current directory outside a project",
    )
    format_parser.add_argument(
        "--check", action="store_true",
        help="change nothing; list the files that would change and exit 1 if any would",
    )

    lsp_parser = subparsers.add_parser("lsp", help="start the Mah language server (speaks LSP over stdio)")
    lsp_parser.add_argument(
        "--version", action="store_true",
        help="print the language server's version and exit",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point -- see bin/mah (installed) and mah/__main__.py
    (`python -m mah`, e.g. from a repo checkout) for how this gets
    invoked.

    Preserves the old CLI's UX: a bare file argument with no subcommand
    word is shorthand -- `mah foo.mh` for `mah run foo.mh`, `mah foo.mahc`
    for `mah runc foo.mahc` -- only kicking in when the first token isn't a
    known subcommand name or a help/version flag, so it never shadows
    `mah run ...` etc. (Known, accepted limitation: a real file literally
    named `run`/`build`/`runc`/`dis`/`lsp` can't be run via this shorthand
    -- use `mah run ./run` instead.)
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] not in _SUBCOMMANDS and argv[0] not in ("-h", "--help", "--version"):
        shorthand = "runc" if argv[0].endswith(".mahc") else "run"
        argv = [shorthand, *argv]

    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 1

    if args.command == "lsp":
        from ..lsp.server import main as lsp_main
        return lsp_main(["--version"] if args.version else [])

    if args.command == "init":
        try:
            name, created, skipped = init_project(args.directory)
        except MahProjectError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print(f"Created Mah project '{name}' in {args.directory}")
        for rel in created:
            print(f"  {rel}")
        if skipped:
            print(f"skipped (already exist): {', '.join(skipped)}")
        cd_prefix = "" if args.directory == "." else f"cd {shlex.quote(args.directory)} && "
        print(f"Next: {cd_prefix}mah run")
        return 0

    if args.command == "runc":
        data = read_file_bytes(args.file)
        try:
            run_bytes(data)
        except MahcFormatError as e:
            print(f"error: invalid .mahc file: {e}", file=sys.stderr)
            return 2
        except MahRuntimeError as e:
            _report_runtime_error(e)
            return 1
        return 0

    if args.command == "format":
        from ..format.cli import run_format
        return run_format(args.paths, args.check)

    if args.command == "dis":
        data = read_file_bytes(args.file)
        try:
            program = decode(data)
        except MahcFormatError as e:
            print(f"error: invalid .mahc file: {e}", file=sys.stderr)
            return 2
        text = disassemble(program)
        if args.out is None:
            print(text, end="")
        else:
            with open(args.out, "w") as f:
                f.write(text)
        return 0

    # M15: `run`/`build` are project-aware -- a missing or directory FILE
    # means "operate on the project found by (a directory-scoped or
    # upward-searching) manifest lookup" instead of a single .mh file.
    # `project` stays `None` for ordinary single-file mode; `targets` is
    # only ever set for a project `build` (the list of `[[target]]`
    # entries to actually write, already resolved from `--target`).
    project = None
    targets = None

    if args.command == "run":
        if args.file is None or os.path.isdir(args.file):
            project, err = _resolve_project(args.file)
            if err is not None:
                return err
            args.file = project.entry

    elif args.command == "build":
        if args.file is None or os.path.isdir(args.file):
            if args.out is not None:
                print(
                    "error: --out can't be used when building a project; "
                    "set 'out' in its [[target]] entries",
                    file=sys.stderr,
                )
                return 2
            project, err = _resolve_project(args.file)
            if err is not None:
                return err
            args.file = project.entry
            if not project.targets:
                print(f"error: {project.manifest_path} has no [[target]] entries", file=sys.stderr)
                return 2
            if args.target is None:
                targets = list(project.targets)
            else:
                targets = [t for t in project.targets if t.name == args.target]
                if not targets:
                    available = ", ".join(t.name for t in project.targets)
                    print(
                        f"error: no target named '{args.target}' in {project.manifest_path} "
                        f"(available: {available})",
                        file=sys.stderr,
                    )
                    return 2
        elif args.target is not None and args.target not in ("debug", "release"):
            print(
                "error: --target must be 'debug' or 'release' when building a single file",
                file=sys.stderr,
            )
            return 2

    entry_str = read_file(args.file)
    pp = preprocess(args.file, entry_str)

    if pp.errors:
        message, offset, _length = pp.errors[0]
        line_info = find_error_line(entry_str, offset)
        if line_info:
            line, row = line_info
            message = f"{message} at position #{line}:{row}"
        raise SyntaxError(message)

    file_str = pp.text

    # Runtime errors (`MahRuntimeError`) are already located by the VM
    # itself (a `#L:C`/`file#L:C` suffix baked into the message at raise
    # time -- see code_interpreter.py's `step_task`) -- unlike compile-time
    # errors (`SyntaxError`, still using the old raw-combined-offset
    # convention resolved here), they must NOT be re-mapped: `main()`'s
    # position-substitution regex below only ever matches a bare run of
    # digits (`at position 123`), which a `#L:C` label never is, so it's a
    # no-op on an already-located runtime error message -- this comment
    # exists so that stays true on purpose, not by accident.
    try:
        if args.command == "build" and project is not None:
            # Compile the entry once per distinct profile among the
            # targets being built (usually just "debug" and/or "release"),
            # then reuse each compiled result for every target that shares
            # that profile.
            compiled_by_profile = {}
            for t in targets:
                if t.profile not in compiled_by_profile:
                    compiled_by_profile[t.profile] = compile_to_bytes(
                        path=project.entry, text=entry_str, target=t.profile
                    )
            for t in targets:
                os.makedirs(os.path.dirname(t.out), exist_ok=True)
                with open(t.out, "wb") as f:
                    f.write(compiled_by_profile[t.profile])
                rel_out = os.path.relpath(t.out, os.getcwd())
                print(f"built {t.name} ({t.profile}) -> {rel_out}")
        elif args.command == "build":
            build_target = args.target if args.target is not None else "debug"
            data = compile_to_bytes(path=args.file, text=entry_str, target=build_target)
            out_path = args.out if args.out is not None else _default_mahc_path(args.file)
            with open(out_path, "wb") as f:
                f.write(data)
        elif args.command == "run":
            data = compile_to_bytes(path=args.file, text=entry_str, target="debug")
            run_bytes(data)
    except MahRuntimeError as e:
        _report_runtime_error(e)
        return 1
    except Exception as e:
        (message, *_) = e.args
        message = demangle_message(message)
        pos = re.findall(r"at position '?(\d+)'?", message)
        if not len(pos):
            e.args = (message,)
            raise e
        [pos] = pos
        label = _location_label(pp, pp.entry_path, int(pos))
        if not label:
            e.args = (message,)
            raise e
        message = message.replace(pos, label)
        e.args = (message,)
        raise e

    return 0


def _report_runtime_error(e: MahRuntimeError) -> None:
    """M14: a runtime error ends the program (Mah has no catchable errors
    yet -- see docs/MAHC_FORMAT.md §6.8). Print it as `RuntimeError: ...`
    on stderr rather than letting Python print the exception class's full
    module path (`mah.runtime_values.MahRuntimeError: ...`)."""
    print(f"RuntimeError: {e}", file=sys.stderr)


def read_file_bytes(file_name: str) -> bytes:
    try:
        with open(file_name, "rb") as f:
            return f.read()
    except FileNotFoundError:
        print(f"Error: file not found '{file_name}'", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    raise SystemExit(main())
