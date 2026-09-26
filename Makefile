CC ?= cc
CFLAGS ?= -O3 -shared -fPIC -I ./syntax-highlight/src
NVIM_DIR ?= $(if $(XDG_CONFIG_HOME),$(XDG_CONFIG_HOME)/nvim,$(HOME)/.config/nvim)
PYTHON ?= python3
CARGO ?= cargo

# Installed locations inside the Neovim config directory.
FTDETECT := $(NVIM_DIR)/ftdetect/mah.vim
TS_PARSER := $(NVIM_DIR)/parser/mah.so
LSP_FTPLUGIN := $(NVIM_DIR)/ftplugin/mah.lua

# CLI install layout (defaults to a user-local prefix, e.g. ~/.local).
PREFIX ?= $(HOME)/.local
LIB_DIR := $(PREFIX)/lib/mah
BIN_DIR := $(PREFIX)/bin
BIN_LINK := $(BIN_DIR)/mah

# `compiler/` is hand-written as of docs/V2_DESIGN.md's M0 milestone --
# mah.lang/compiler-generator are kept only as a historical reference to
# v1's grammar-DSL pipeline (see docs/GRAMMAR_DSL.md) and are no longer
# regenerated. This target is intentionally a no-op so a stray `make lang`
# can't clobber the hand-written compiler/ files.
lang:
	@echo "compiler/ is hand-written now (see docs/V2_DESIGN.md) -- 'make lang' does nothing."

# Run the automated test suite (stdlib unittest, no extra dependencies --
# see docs/TESTING.md for the testing policy this enforces).
test:
	$(PYTHON) -m unittest discover -s tests -t . -v

# The same suite with every program run on the Rust VM instead (see
# tests/support.py's `_run_rust`), plus the Rust crate's own tests.
test-rust: vm
	cd runtime && $(CARGO) test --release
	MAH_TEST_VM=rust $(PYTHON) -m unittest discover -s tests -t .

# ---------------------------------------------------------------------------
# mah-vm -- the native Rust runtime (runtime/, std-only; see docs/RUST_VM.md)
# ---------------------------------------------------------------------------
vm:
	cd runtime && $(CARGO) build --release

# ---------------------------------------------------------------------------
# mah -- the CLI (which also ships the language server as `mah lsp`)
# ---------------------------------------------------------------------------
#
# Install the interpreter into $(LIB_DIR) and link the executable as `mah`.
# bin/mah resolves its own real path explicitly and does
# `from mah.cli.main import main`, so this works from any invoking directory
# regardless of how sys.path[0] gets set for a script run through a symlink.
# The Rust runtime is copied too if it's been built (`make vm`): `--vm rust`
# and `build --self-contained` look for it next to the package.
install-mah:
	mkdir -p $(LIB_DIR) $(BIN_DIR)
	rm -rf $(LIB_DIR)/mah $(LIB_DIR)/mah-vm
	cp -r mah $(LIB_DIR)/mah
	cp bin/mah $(LIB_DIR)/mah-launcher
	if [ -x runtime/target/release/mah-vm ]; then cp runtime/target/release/mah-vm $(LIB_DIR)/mah-vm; fi
	chmod +x $(LIB_DIR)/mah-launcher
	ln -sf $(LIB_DIR)/mah-launcher $(BIN_LINK)

uninstall-mah:
	rm -f $(BIN_LINK)
	rm -rf $(LIB_DIR)
	-rmdir $(BIN_DIR) 2>/dev/null || true

# ---------------------------------------------------------------------------
# nvim -- editor integration: tree-sitter highlighting + the LSP ftplugin
# (which just shells out to the already-installed `mah lsp` -- see
# install-mah above). Both pieces share one filetype-detection file and are
# installed/removed together now that neither is independently useful
# without the other for a real editing setup.
# ---------------------------------------------------------------------------
$(FTDETECT):
	mkdir -p $(NVIM_DIR)/ftdetect
	printf 'au BufRead,BufNewFile *.mh set filetype=mah\n' > $(FTDETECT)

install-nvim: $(FTDETECT)
	mkdir -p $(NVIM_DIR)/parser $(NVIM_DIR)/queries/mah $(NVIM_DIR)/ftplugin
	$(CC) $(CFLAGS) ./syntax-highlight/src/parser.c -o $(TS_PARSER)
	cp ./syntax-highlight/queries/mah/highlights.scm $(NVIM_DIR)/queries/mah/highlights.scm
	cp editors/nvim/ftplugin/mah.lua $(LSP_FTPLUGIN)

uninstall-nvim:
	rm -f $(TS_PARSER)
	rm -f $(NVIM_DIR)/queries/mah/highlights.scm
	rm -f $(LSP_FTPLUGIN)
	-rmdir $(NVIM_DIR)/queries/mah 2>/dev/null || true
	-rmdir $(NVIM_DIR)/queries 2>/dev/null || true
	-rmdir $(NVIM_DIR)/parser 2>/dev/null || true
	-rmdir $(NVIM_DIR)/ftplugin 2>/dev/null || true
	rm -f $(FTDETECT)
	-rmdir $(NVIM_DIR)/ftdetect 2>/dev/null || true

# ---------------------------------------------------------------------------
# vscode -- editor integration (TextMate grammar + LSP client wired to
# `mah lsp` + a per-language file icon; see editors/vscode/README.md)
# ---------------------------------------------------------------------------
#
# Packaging a .vsix is a fundamentally different kind of step -- building a
# distributable artifact via npm/vsce -- than copying files into a config
# directory, so it gets its own command rather than folding into
# install-mah/install-nvim. Produces editors/vscode/mah-language-*.vsix;
# install it yourself with `code --install-extension <path>` (deliberately
# not done automatically here -- that would modify your live VS Code setup).
build-vscode:
	cd editors/vscode && npm install && npm run compile && npx @vscode/vsce package

install: install-mah install-nvim
install-all: install
uninstall: uninstall-mah uninstall-nvim
uninstall-all: uninstall

# ---------------------------------------------------------------------------
# Back-compat aliases (old target names from before mah/nvim were split out
# this way) -- kept so nobody's muscle memory or scripts break.
# ---------------------------------------------------------------------------
install-cli: install-mah
install-cli-mah: install-mah
cli-install: install-mah
uninstall-cli: uninstall-mah
cli-uninstall: uninstall-mah
remove-cli: uninstall-mah

install-syntax: install-nvim
nvim-install: install-nvim
install-lsp: install-nvim
install-lsp-nvim: install-nvim
lsp-install: install-nvim
nvim-lsp: install-nvim

uninstall-syntax: uninstall-nvim
remove-syntax: uninstall-nvim
nvim-uninstall: uninstall-nvim
remove-nvim: uninstall-nvim
uninstall-lsp: uninstall-nvim
uninstall-lsp-nvim: uninstall-nvim
lsp-uninstall: uninstall-nvim
lsp-remove: uninstall-nvim

.PHONY: lang test test-rust vm install install-all uninstall uninstall-all \
	install-mah uninstall-mah install-nvim uninstall-nvim build-vscode \
	install-cli install-cli-mah cli-install uninstall-cli cli-uninstall remove-cli \
	install-syntax nvim-install install-lsp install-lsp-nvim lsp-install nvim-lsp \
	uninstall-syntax remove-syntax nvim-uninstall remove-nvim \
	uninstall-lsp uninstall-lsp-nvim lsp-uninstall lsp-remove
