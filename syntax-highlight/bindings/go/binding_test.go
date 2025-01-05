package tree_sitter_mah_test

import (
	"testing"

	tree_sitter "github.com/tree-sitter/go-tree-sitter"
	tree_sitter_mah "github.com/tree-sitter/tree-sitter-mah/bindings/go"
)

func TestCanLoadGrammar(t *testing.T) {
	language := tree_sitter.NewLanguage(tree_sitter_mah.Language())
	if language == nil {
		t.Errorf("Error loading Mah grammar")
	}
}
