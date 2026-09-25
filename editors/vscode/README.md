# Mah Language for VS Code

Editor support for [Mah](https://github.com/sina-salahshour/mahlang) (`.mh` files):

- Syntax highlighting (a TextMate grammar covering Mah's keywords, structs/enums,
  async (`detach`/`.await`/`sleep_async`), strings, numbers, and comments)
- Full language server support via `mah lsp`:
  - diagnostics
  - hover — including doc comments, and struct/enum/variant shapes
  - go-to-definition — variables, functions, struct/enum/variant names, and
    jumping straight into an `import`ed file
  - rename — including across files (renaming a function/variable updates
    every file in the workspace that imports it), and struct/enum type,
    variant, and field names
  - completion — keywords, builtins, in-scope symbols, namespaced imports,
    and `.mh` file paths while typing inside an `import "..."` string
- A custom file icon for `.mh` files in the Explorer (VS Code's built-in icon themes
  only — see **File icon note** below if you use a third-party icon theme)

## Requirements

The `mah` CLI must be installed and on `PATH`. From the root of the `mahlang` repo:

```sh
make install-mah
```

This installs `mah` (see `../../Makefile`), which the extension spawns as
`mah lsp` to talk LSP over stdio — the same server used by the Neovim
integration (`editors/nvim`), just launched by VS Code instead.

If `mah` isn't on `PATH`, or you want to point at a specific build, set:

```json
{ "mah.serverPath": "/absolute/path/to/mah" }
```

## Building and running locally (not yet published to the Marketplace)

From this directory:

```sh
npm install
npm run compile
```

Then either:

- Press F5 in VS Code (with this folder open) to launch an Extension
  Development Host with the extension loaded, or
- Package a `.vsix` and install it manually:

  ```sh
  npx @vscode/vsce package
  code --install-extension mah-language-0.3.0.vsix
  ```

  (Also available as `make build-vscode` from the repo root.)

## File icon note

VS Code lets a language extension register a per-language file icon
(`contributes.languages[].icon`), which this extension does. It's shown by
VS Code's own built-in icon themes (e.g. "Seti (Visual Studio Code)"), but
popular third-party icon themes maintain their own language→icon tables and
don't read this field automatically:

- **vscode-icons**: no built-in Mah icon yet; a PR to
  [vscode-icons/vscode-icons](https://github.com/vscode-icons/vscode-icons)
  adding the `mah` language ID would be the proper way to get one upstream.
- **Material Icon Theme**: you can self-configure a mapping in your settings:

  ```json
  "material-icon-theme.languages.associations": { "mah": "python" }
  ```

  (swap `"python"` for whichever bundled icon you'd like used in the meantime).
