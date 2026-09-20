import * as vscode from "vscode";
import {
  LanguageClient,
  LanguageClientOptions,
  ServerOptions,
} from "vscode-languageclient/node";

let client: LanguageClient | undefined;

export function activate(context: vscode.ExtensionContext): void {
  const config = vscode.workspace.getConfiguration("mah");
  const command = config.get<string>("serverPath", "mah");

  // `mah lsp` is the Mah CLI's own language-server subcommand (see
  // mah/cli/main.py / mah/lsp/server.py in the mahlang repo) -- a plain
  // external process that ALWAYS speaks LSP over stdio unconditionally,
  // with no flag of its own to request that. Deliberately omit `transport`
  // here: for an `Executable`, vscode-languageclient only uses stdio for
  // the actual reader/writer BY DEFAULT when `transport` is left unset --
  // setting it explicitly to `TransportKind.stdio` instead tells the
  // client to also APPEND a `--stdio` argument to the child process (the
  // convention servers that support multiple transports use to be told
  // which one to switch into), which `mah lsp`'s argparse-based CLI
  // rejects outright since it has no such flag.
  const run = { command, args: ["lsp"] };
  const serverOptions: ServerOptions = { run, debug: run };

  const clientOptions: LanguageClientOptions = {
    documentSelector: [{ scheme: "file", language: "mah" }],
    synchronize: {
      fileEvents: vscode.workspace.createFileSystemWatcher("**/*.mh"),
    },
  };

  client = new LanguageClient(
    "mahLanguageServer",
    "Mah Language Server",
    serverOptions,
    clientOptions,
  );

  client.start().then(
    () => undefined,
    (error: unknown) => {
      const message = error instanceof Error ? error.message : String(error);
      vscode.window.showErrorMessage(
        `Mah: failed to start the language server ('${command} lsp'). ` +
          `Is Mah installed and on PATH (see 'make install-mah'), or is ` +
          `'mah.serverPath' set correctly? ${message}`,
      );
    },
  );

  context.subscriptions.push({ dispose: () => void client?.stop() });
}

export function deactivate(): Thenable<void> | undefined {
  return client?.stop();
}
