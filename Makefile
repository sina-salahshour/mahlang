build: lang

lang:
	python ./compiler-generator/generate.py ./mah.lang

nvim-install:
	@mkdir -p ~/.local/share/nvim/site/queries/mah
	@cp syntax-highlight/queries/mah/highlights.scm ~/.local/share/nvim/site/queries/mah/highlights.scm
	@echo "MahLang Neovim syntax highlighting installed."

.PHONY: build lang nvim-install
