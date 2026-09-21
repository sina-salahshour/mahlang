#!/usr/bin/env python3
"""Mah language server.

A dependency-free (standard library only) implementation of the Language
Server Protocol over stdio for the Mah language. It provides:

  * live diagnostics (compile errors, including imported files) on open/change
  * hover information for keywords, builtins and identifiers (with doc comments)
  * go to definition (scope-aware, and across ``import``ed files)
  * completion for keywords, builtins and document/imported symbols
  * document symbols (functions and variables)
  * a comment/uncomment code action

The server speaks LSP `3.x` framing (``Content-Length`` headers followed by a
JSON-RPC 2.0 body) on stdin/stdout. All logging goes to stderr so it never
corrupts the protocol stream.

Run it directly for a quick sanity check:

    python -m mah lsp --version
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

from . import analysis

SERVER_NAME = "mah-lsp"
SERVER_VERSION = "0.1.0"


def uri_to_path(uri: str) -> str | None:
    """Convert a ``file://`` URI to a local filesystem path."""
    if not uri:
        return None
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return None
    return unquote(parsed.path)


def path_to_uri(path: str) -> str:
    """Convert a local filesystem path to a ``file://`` URI.

    Uses `pathlib.Path.as_uri()` rather than the old
    `"file://" + urllib.request.pathname2url(path)` idiom: a CPython
    stdlib behavior change (`pathname2url` now itself prepends an
    authority-separating `//` before an absolute POSIX path's leading `/`,
    to stop an absolute path from ever being misread as a URL authority)
    means that old idiom now produces a malformed URI with two extra
    slashes (`file://///tmp/...` instead of `file:///tmp/...`) on current
    Python versions. This broke cross-file go-to-definition specifically
    -- the only place this function's return value ever reaches a client
    -- while same-file jumps (which never call this function, see
    `_on_textDocument_definition` below) kept working, which is why this
    was easy to miss. `Path.as_uri()` is the stable, version-independent
    way to do this conversion."""
    return Path(path).resolve().as_uri()


def _log(message: str) -> None:
    sys.stderr.write(f"[{SERVER_NAME}] {message}\n")
    sys.stderr.flush()


class Server:
    def __init__(self, stdin, stdout) -> None:
        self._stdin = stdin
        self._stdout = stdout
        self._documents: dict[str, str] = {}
        self._shutdown = False
        self._running = True

    # -- transport --------------------------------------------------------
    def _read_message(self) -> dict | None:
        headers: dict[str, str] = {}
        while True:
            line = self._stdin.readline()
            if not line:
                return None  # EOF
            line = line.decode("ascii", errors="replace")
            if line in ("\r\n", "\n"):
                break
            if ":" in line:
                key, _, value = line.partition(":")
                headers[key.strip().lower()] = value.strip()

        length = int(headers.get("content-length", 0))
        if length <= 0:
            return None
        body = self._stdin.read(length)
        if not body:
            return None
        return json.loads(body.decode("utf-8"))

    def _write_message(self, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        header = f"Content-Length: {len(data)}\r\n\r\n".encode("ascii")
        self._stdout.write(header)
        self._stdout.write(data)
        self._stdout.flush()

    def _respond(self, request_id, result) -> None:
        self._write_message({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _respond_error(self, request_id, code: int, message: str) -> None:
        self._write_message(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": code, "message": message},
            }
        )

    def _notify(self, method: str, params: dict) -> None:
        self._write_message({"jsonrpc": "2.0", "method": method, "params": params})

    # -- main loop --------------------------------------------------------
    def serve_forever(self) -> int:
        while self._running:
            try:
                message = self._read_message()
            except Exception as error:  # noqa: BLE001
                _log(f"failed to read message: {error!r}")
                continue

            if message is None:
                break

            method = message.get("method")
            if method is None:
                continue  # a response to a server->client request; ignore

            request_id = message.get("id")
            params = message.get("params") or {}
            try:
                self._dispatch(method, request_id, params)
            except Exception as error:  # noqa: BLE001 - never crash the server
                _log(f"error handling {method!r}: {error!r}")
                if request_id is not None:
                    self._respond_error(request_id, -32603, f"internal error: {error}")

        return 0 if self._shutdown else 1

    def _dispatch(self, method: str, request_id, params: dict) -> None:
        handler = getattr(self, "_on_" + method.replace("/", "_").replace("$", ""), None)
        if handler is None:
            if request_id is not None:
                # Unknown request: reply with MethodNotFound.
                self._respond_error(request_id, -32601, f"method not found: {method}")
            return
        handler(request_id, params)

    # -- lifecycle --------------------------------------------------------
    def _on_initialize(self, request_id, params: dict) -> None:
        # M7: go-to-definition and rename are now advertised -- both are
        # built on `Resolver`'s real symbol table (see
        # `lsp/analysis.py`'s `get_definition`/`get_rename_edits`), not the
        # retired independent token-scope model (`_build_scopes` and
        # friends), which M7 deleted. hover is advertised too, likewise
        # built on the resolver (plus `type_position_index` for struct/enum/
        # variant names). completion is now advertised as well -- revived on
        # top of the same resolver foundation (`analysis.py`'s
        # `get_completions`), replacing the dead, pre-M0 `TokenType`-based
        # scanner it used to call. document-symbols/code-actions remain
        # deliberately NOT advertised: they're unrelated to this work and
        # still built on that old dead token-scanner (a focused follow-up,
        # not part of this milestone). Advertising them now would invite a
        # well-behaved client to call handlers that raise. Diagnostics need
        # no capability flag: they're pushed via
        # `textDocument/publishDiagnostics` notifications on open/change/
        # save, unconditionally.
        capabilities = {
            "textDocumentSync": {
                "openClose": True,
                "change": 1,  # full document sync
            },
            "definitionProvider": True,
            "renameProvider": True,
            "hoverProvider": True,
            "completionProvider": {"triggerCharacters": [".", "\""]},
        }
        self._respond(
            request_id,
            {
                "capabilities": capabilities,
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )

    def _on_initialized(self, request_id, params: dict) -> None:
        _log("client initialized")

    def _on_shutdown(self, request_id, params: dict) -> None:
        self._shutdown = True
        self._respond(request_id, None)

    def _on_exit(self, request_id, params: dict) -> None:
        self._running = False

    # -- document sync ----------------------------------------------------
    def _on_textDocument_didOpen(self, request_id, params: dict) -> None:
        doc = params.get("textDocument", {})
        uri = doc.get("uri")
        text = doc.get("text", "")
        if uri is None:
            return
        self._documents[uri] = text
        self._publish_diagnostics(uri, text)

    def _on_textDocument_didChange(self, request_id, params: dict) -> None:
        uri = params.get("textDocument", {}).get("uri")
        changes = params.get("contentChanges") or []
        if uri is None or not changes:
            return
        # We advertise full sync, so the last change carries the whole document.
        text = changes[-1].get("text", "")
        self._documents[uri] = text
        self._publish_diagnostics(uri, text)

    def _on_textDocument_didSave(self, request_id, params: dict) -> None:
        uri = params.get("textDocument", {}).get("uri")
        if uri is None:
            return
        text = params.get("text")
        if text is None:
            text = self._documents.get(uri, "")
        else:
            self._documents[uri] = text
        self._publish_diagnostics(uri, text)

    def _on_textDocument_didClose(self, request_id, params: dict) -> None:
        uri = params.get("textDocument", {}).get("uri")
        if uri is None:
            return
        self._documents.pop(uri, None)
        # Clear diagnostics for the closed document.
        self._notify(
            "textDocument/publishDiagnostics",
            {"uri": uri, "diagnostics": []},
        )

    # -- language features ------------------------------------------------
    def _on_textDocument_hover(self, request_id, params: dict) -> None:
        uri = params.get("textDocument", {}).get("uri")
        position = params.get("position", {})
        text = self._documents.get(uri, "")
        hover = analysis.get_hover(
            text,
            position.get("line", 0),
            position.get("character", 0),
            uri_to_path(uri),
        )
        self._respond(request_id, hover)

    def _on_textDocument_completion(self, request_id, params: dict) -> None:
        uri = params.get("textDocument", {}).get("uri")
        position = params.get("position", {})
        text = self._documents.get(uri, "")
        items = analysis.get_completions(
            text,
            uri_to_path(uri),
            position.get("line"),
            position.get("character"),
        )
        self._respond(request_id, {"isIncomplete": False, "items": items})

    def _on_textDocument_documentSymbol(self, request_id, params: dict) -> None:
        uri = params.get("textDocument", {}).get("uri")
        text = self._documents.get(uri, "")
        self._respond(request_id, analysis.get_document_symbols(text))

    def _on_textDocument_definition(self, request_id, params: dict) -> None:
        uri = params.get("textDocument", {}).get("uri")
        position = params.get("position", {})
        text = self._documents.get(uri, "")
        target = analysis.get_definition(
            text,
            position.get("line", 0),
            position.get("character", 0),
            uri_to_path(uri),
        )
        if target is None:
            self._respond(request_id, None)
            return
        # A ``path`` of None means the definition is in the current document.
        target_path = target.get("path")
        target_uri = path_to_uri(target_path) if target_path else uri
        self._respond(request_id, {"uri": target_uri, "range": target["range"]})

    def _on_textDocument_rename(self, request_id, params: dict) -> None:
        uri = params.get("textDocument", {}).get("uri")
        position = params.get("position", {})
        new_name = params.get("newName", "")
        text = self._documents.get(uri, "")
        result = analysis.get_rename_edits(
            text,
            position.get("line", 0),
            position.get("character", 0),
            new_name,
            uri_to_path(uri),
        )
        if result is None:
            self._respond(request_id, None)  # None is a valid "can't rename this" response
            return
        # `analysis.get_rename_edits` returns its edits keyed by the entry
        # file's own filesystem path (or BUFFER_PATH for a path-less
        # buffer) -- rename is single-file only (see analysis.py), so
        # there's always exactly one key here. Translate it to the real
        # document URI the same way `_on_textDocument_definition` above
        # translates `get_definition`'s ``path`` key.
        edits = next(iter(result["changes"].values()), [])
        self._respond(request_id, {"changes": {uri: edits}})

    def _on_textDocument_codeAction(self, request_id, params: dict) -> None:
        uri = params.get("textDocument", {}).get("uri")
        text = self._documents.get(uri, "")
        rng = params.get("range", {})
        start_line = rng.get("start", {}).get("line", 0)
        end_line = rng.get("end", {}).get("line", start_line)
        actions = analysis.get_code_actions(uri, text, start_line, end_line)
        self._respond(request_id, actions)

    # -- diagnostics ------------------------------------------------------
    def _publish_diagnostics(self, uri: str, text: str) -> None:
        try:
            diagnostics = analysis.get_diagnostics(text, uri_to_path(uri))
        except Exception as error:  # noqa: BLE001
            _log(f"diagnostics failed for {uri}: {error!r}")
            diagnostics = []
        self._notify(
            "textDocument/publishDiagnostics",
            {"uri": uri, "diagnostics": diagnostics},
        )


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if "--version" in argv or "-v" in argv:
        sys.stdout.write(f"{SERVER_NAME} {SERVER_VERSION}\n")
        return 0
    if "--help" in argv or "-h" in argv:
        sys.stdout.write(
            "Mah language server (LSP over stdio).\n"
            "Start it from your editor; it reads JSON-RPC on stdin/stdout.\n"
        )
        return 0

    _log(f"starting {SERVER_VERSION} (python {sys.version.split()[0]})")
    server = Server(sys.stdin.buffer, sys.stdout.buffer)
    return server.serve_forever()


if __name__ == "__main__":
    raise SystemExit(main())
