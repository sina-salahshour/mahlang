CC ?= cc
CFLAGS ?= -O3 -shared -fPIC -I ./syntax-highlight/src
NVIM_DIR ?= $(if $(XDG_CONFIG_HOME),$(XDG_CONFIG_HOME)/nvim,$(HOME)/.config/nvim)

lang:
	python ./compiler-generator/generate.py ./mah.lang

install-nvim:
	mkdir -p $(NVIM_DIR)/parser $(NVIM_DIR)/queries/mah $(NVIM_DIR)/ftdetect
	$(CC) $(CFLAGS) ./syntax-highlight/src/parser.c -o $(NVIM_DIR)/parser/mah.so
	cp ./syntax-highlight/queries/mah/highlights.scm $(NVIM_DIR)/queries/mah/highlights.scm
	printf 'au BufRead,BufNewFile *.mh set filetype=mah\n' > $(NVIM_DIR)/ftdetect/mah.vim

uninstall-nvim:
	rm -f $(NVIM_DIR)/parser/mah.so
	rm -f $(NVIM_DIR)/queries/mah/highlights.scm
	-rmdir $(NVIM_DIR)/queries/mah 2>/dev/null || true
	-rmdir $(NVIM_DIR)/queries 2>/dev/null || true
	rm -f $(NVIM_DIR)/ftdetect/mah.vim
	-rmdir $(NVIM_DIR)/ftdetect 2>/dev/null || true
	-rmdir $(NVIM_DIR)/parser 2>/dev/null || true

install: install-nvim
install-syntax: install-nvim
nvim-install: install-nvim

uninstall: uninstall-nvim
remove-nvim: uninstall-nvim
remove-syntax: uninstall-nvim
uninstall-syntax: uninstall-nvim
nvim-uninstall: uninstall-nvim
nvim-remove: uninstall-nvim

.PHONY: lang install install-nvim install-syntax nvim-install uninstall uninstall-nvim remove-nvim remove-syntax uninstall-syntax nvim-uninstall nvim-remove

