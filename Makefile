build:
	python compiler-generator/compiler.py mah.lang

nvim-install: build
	@mkdir -p ~/.local/share/nvim/site/queries/mahlang
	@cp compiler-generator/tree-sitter/queries/highlights.scm ~/.local/share/nvim/site/queries/mahlang/highlights.scm
	@echo "MahLang Neovim syntax highlighting installed."

.PHONY: build nvim-install
