CC ?= cc
CFLAGS ?= -O3 -shared -fPIC -I ./syntax-highlight/src
NVIM_DIR ?= $(if $(XDG_CONFIG_HOME),$(XDG_CONFIG_HOME)/nvim,$(HOME)/.config/nvim)
PYTHON ?= python3

# Installed locations inside the Neovim config directory.
FTDETECT := $(NVIM_DIR)/ftdetect/mah.vim
TS_PARSER := $(NVIM_DIR)/parser/mah.so
LSP_FTPLUGIN := $(NVIM_DIR)/ftplugin/mah.lua
LSP_DIR := $(NVIM_DIR)/mah-lsp

# CLI install layout (defaults to a user-local prefix, e.g. ~/.local).
PREFIX ?= $(HOME)/.local
LIB_DIR := $(PREFIX)/lib/mah
BIN_DIR := $(PREFIX)/bin
BIN_LINK := $(BIN_DIR)/mah

lang:
	$(PYTHON) ./compiler-generator/generate.py ./mah.lang

# Install the interpreter into $(LIB_DIR) and link the executable as `mah`.
# Running a script through the symlink makes Python resolve sys.path[0] to the
# real $(LIB_DIR), so the bundled modules import correctly from any directory.
install-cli: lang
	mkdir -p $(LIB_DIR)/compiler $(BIN_DIR)
	cp mah.py actions.py code_interpreter.py preprocessor.py $(LIB_DIR)/
	cp compiler/__init__.py compiler/lexer.py compiler/parser.py compiler/ir_generator.py $(LIB_DIR)/compiler/
	chmod +x $(LIB_DIR)/mah.py
	ln -sf $(LIB_DIR)/mah.py $(BIN_LINK)

uninstall-cli:
	rm -f $(BIN_LINK)
	rm -rf $(LIB_DIR)
	-rmdir $(BIN_DIR) 2>/dev/null || true

# Filetype detection is shared by syntax highlighting and the LSP.
$(FTDETECT):
	mkdir -p $(NVIM_DIR)/ftdetect
	printf 'au BufRead,BufNewFile *.mh set filetype=mah\n' > $(FTDETECT)

install-nvim: $(FTDETECT)
	mkdir -p $(NVIM_DIR)/parser $(NVIM_DIR)/queries/mah
	$(CC) $(CFLAGS) ./syntax-highlight/src/parser.c -o $(TS_PARSER)
	cp ./syntax-highlight/queries/mah/highlights.scm $(NVIM_DIR)/queries/mah/highlights.scm

uninstall-nvim:
	rm -f $(TS_PARSER)
	rm -f $(NVIM_DIR)/queries/mah/highlights.scm
	-rmdir $(NVIM_DIR)/queries/mah 2>/dev/null || true
	-rmdir $(NVIM_DIR)/queries 2>/dev/null || true
	-rmdir $(NVIM_DIR)/parser 2>/dev/null || true
	@# remove shared filetype detection only if the LSP is not installed
	@if [ ! -f "$(LSP_FTPLUGIN)" ]; then \
		rm -f "$(FTDETECT)"; \
		rmdir $(NVIM_DIR)/ftdetect 2>/dev/null || true; \
	fi

# Bundle the language server (and the compiler modules it needs) next to the
# Neovim config, then drop in the ftplugin that starts it for *.mh buffers.
install-lsp: lang $(FTDETECT)
	mkdir -p $(LSP_DIR)/lsp $(LSP_DIR)/compiler $(NVIM_DIR)/ftplugin
	cp lsp/__init__.py lsp/analysis.py lsp/server.py $(LSP_DIR)/lsp/
	cp compiler/__init__.py compiler/lexer.py compiler/parser.py compiler/ir_generator.py $(LSP_DIR)/compiler/
	cp actions.py preprocessor.py $(LSP_DIR)/
	cp editors/nvim/ftplugin/mah.lua $(LSP_FTPLUGIN)

uninstall-lsp:
	rm -f $(LSP_FTPLUGIN)
	rm -rf $(LSP_DIR)
	-rmdir $(NVIM_DIR)/ftplugin 2>/dev/null || true
	@# remove shared filetype detection only if highlighting is not installed
	@if [ ! -f "$(TS_PARSER)" ]; then \
		rm -f "$(FTDETECT)"; \
		rmdir $(NVIM_DIR)/ftdetect 2>/dev/null || true; \
	fi

install: install-cli install-nvim install-lsp
install-all: install
install-syntax: install-nvim
nvim-install: install-nvim
install-lsp-nvim: install-lsp
lsp-install: install-lsp
nvim-lsp: install-lsp
install-cli-mah: install-cli
cli-install: install-cli
install-mah: install-cli

uninstall: uninstall-cli uninstall-lsp uninstall-nvim
uninstall-all: uninstall
remove-nvim: uninstall-nvim
remove-syntax: uninstall-nvim
uninstall-syntax: uninstall-nvim
nvim-uninstall: uninstall-nvim
nvim-remove: uninstall-nvim
uninstall-lsp-nvim: uninstall-lsp
lsp-uninstall: uninstall-lsp
lsp-remove: uninstall-lsp
cli-uninstall: uninstall-cli
remove-cli: uninstall-cli
uninstall-mah: uninstall-cli

.PHONY: lang install install-all install-cli install-cli-mah cli-install install-mah \
	install-nvim install-syntax nvim-install install-lsp install-lsp-nvim lsp-install nvim-lsp \
	uninstall uninstall-all uninstall-cli cli-uninstall remove-cli uninstall-mah \
	uninstall-nvim remove-nvim remove-syntax uninstall-syntax nvim-uninstall nvim-remove \
	uninstall-lsp uninstall-lsp-nvim lsp-uninstall lsp-remove
