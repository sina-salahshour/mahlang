// A small, dependency-free syntax highlighter for Mah source code, used as
// mdsvex's `highlight` function for ```mah fenced code blocks (and a much
// simpler pass for other languages used in the docs, like sh/toml/text).
//
// Mah's own tooling is "pure Python, standard library only" by design (see
// README.md) -- in that spirit, this highlighter is a plain regex tokenizer
// instead of pulling in Shiki/highlight.js. The keyword list mirrors
// mah/compiler/lexer.py's KEYWORDS table and mah/preprocessor.py's
// import/export/from handling.

const KEYWORDS = new Set([
	'let',
	'print',
	'input',
	'sin',
	'cos',
	'if',
	'elif',
	'else',
	'while',
	'break',
	'continue',
	'return',
	'defer',
	'detach',
	'sleep_async',
	'fn',
	'struct',
	'enum',
	'match',
	'trait',
	'impl',
	'for',
	'in',
	'import',
	'export',
	'from',
	// M25/M26 errors: `throw`/`try` are reserved; `catch`/`throws`/`never`
	// are contextual in the real lexer, but in doc samples they only ever
	// appear as keywords.
	'throw',
	'try',
	'catch',
	'throws',
	'never'
]);

// Constant-like keywords get their own token class.
const CONSTANTS = new Set(['true', 'false', 'none', 'some']);

// `self`/`Self` are plain identifiers to the lexer but carry special meaning
// (the receiver / the impl's own type), so they get their own highlight.
const SELF_WORDS = new Set(['self', 'Self']);

// Built-in type names usable in type annotations (see docs/mah-language.md).
const BUILTIN_TYPES = new Set([
	'Number',
	'String',
	'Bool',
	'Vector',
	'Map',
	'Option',
	'Promise',
	'Function',
	'None',
	'Never',
	'Unknown',
	'Printable',
	'Index',
	'IndexAssign',
	'Iterable',
	'Iterator',
	'Error',
	'RuntimeError'
]);

function escapeHtml(s: string): string {
	// mdsvex splices this HTML directly into a Svelte component's markup, so
	// `{`/`}` must be escaped too -- otherwise Svelte's template compiler
	// tries to parse curly braces from Mah source (`fn f() { ... }`) as
	// mustache expressions and fails with "Unexpected token".
	return s
		.replace(/&/g, '&amp;')
		.replace(/</g, '&lt;')
		.replace(/>/g, '&gt;')
		.replace(/"/g, '&quot;')
		.replace(/'/g, '&#39;')
		.replace(/\{/g, '&#123;')
		.replace(/\}/g, '&#125;');
}

// Ordered token matchers. Order matters: comments/strings/numbers are
// matched before the generic identifier rule so keywords inside strings
// don't get relexed.
const TOKEN_RE =
	/(#[^\n]*)|("(?:\\.|[^"\\])*")|(\b\d+(?:\.\d+)?\b)|([A-Za-z_][A-Za-z0-9_]*)|(\.\.=|\.\.|\*\*|==|!=|<=|>=|=>|->|[-+*/%<>=!&|.,:;(){}\[\]])/g;

function highlightMah(code: string): string {
	let out = '';
	let last = 0;
	TOKEN_RE.lastIndex = 0;
	let m: RegExpExecArray | null;
	while ((m = TOKEN_RE.exec(code))) {
		if (m.index > last) out += escapeHtml(code.slice(last, m.index));
		const [full, comment, string, number, word, punct] = m;
		if (comment) {
			out += `<span class="tok-comment">${escapeHtml(comment)}</span>`;
		} else if (string) {
			out += `<span class="tok-string">${escapeHtml(string)}</span>`;
		} else if (number) {
			out += `<span class="tok-number">${escapeHtml(number)}</span>`;
		} else if (word) {
			if (KEYWORDS.has(word)) {
				out += `<span class="tok-keyword">${word}</span>`;
			} else if (CONSTANTS.has(word)) {
				out += `<span class="tok-constant">${word}</span>`;
			} else if (SELF_WORDS.has(word)) {
				out += `<span class="tok-self">${word}</span>`;
			} else if (BUILTIN_TYPES.has(word)) {
				out += `<span class="tok-type">${word}</span>`;
			} else {
				out += escapeHtml(word);
			}
		} else if (punct) {
			out += `<span class="tok-punct">${escapeHtml(punct)}</span>`;
		} else {
			out += escapeHtml(full);
		}
		last = TOKEN_RE.lastIndex;
	}
	if (last < code.length) out += escapeHtml(code.slice(last));
	return out;
}

/**
 * mdsvex `highlight` hook: given a fenced code block's contents and
 * language tag, returns the HTML to embed. Mah gets real (if simple) token
 * highlighting; every other language just gets escaped and wrapped so it
 * still looks like code.
 */
export function highlighter(code: string, lang: string | null | undefined): string {
	const language = (lang || 'text').toLowerCase();
	const body = language === 'mah' ? highlightMah(code) : escapeHtml(code);
	const langClass = `language-${language}`;
	return `<pre class="mah-code ${langClass}"><code class="${langClass}">${body}</code></pre>`;
}
