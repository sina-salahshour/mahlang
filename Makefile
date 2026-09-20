CC ?= cc
CFLAGS ?= -O3 -shared -fPIC -I ./syntax-highlight/src
NVIM_DIR ?= $(if $(XDG_CONFIG_HOME),$(XDG_CONFIG_HOME)/nvim,$(HOME)/.config/nvim)
PYTHON ?= python3

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

# ---------------------------------------------------------------------------
# mah -- the CLI (which also ships the language server as `mah lsp`)
# ---------------------------------------------------------------------------
#
# Install the interpreter into $(LIB_DIR) and link the executable as `mah`.
# bin/mah resolves its own real path explicitly and does
# `from mah.cli.main import main`, so this works from any invoking directory
# regardless of how sys.path[0] gets set for a script run through a symlink.
install-mah:
	mkdir -p $(LIB_DIR) $(BIN_DIR)
	rm -rf $(LIB_DIR)/mah
	cp -r mah $(LIB_DIR)/mah
	cp bin/mah $(LIB_DIR)/mah-launcher
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
# vscode -- editor integration (not written yet)
# ---------------------------------------------------------------------------
#
# Placeholder so there's already a dedicated build step to grow into once
# editors/vscode/ exists. Packaging a .vsix (npm install && npm run compile
# && npx vsce package) is a fundamentally different kind of step -- building
# a distributable artifact -- than copying files into a config directory, so
# it gets its own command rather than folding into install-mah/install-nvim.
build-vscode:
	@echo "No VSCode extension yet -- editors/vscode/ doesn't exist. Nothing to build."

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

.PHONY: lang test install install-all uninstall uninstall-all \
	install-mah uninstall-mah install-nvim uninstall-nvim build-vscode \
	install-cli install-cli-mah cli-install uninstall-cli cli-uninstall remove-cli \
	install-syntax nvim-install install-lsp install-lsp-nvim lsp-install nvim-lsp \
	uninstall-syntax remove-syntax nvim-uninstall remove-nvim \
	uninstall-lsp uninstall-lsp-nvim lsp-uninstall lsp-remove
