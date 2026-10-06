<script lang="ts">
	import QuickstartTabs from '$lib/components/QuickstartTabs.svelte';
	import LangIcon from '$lib/components/LangIcon.svelte';
	import { highlighter } from '$lib/markdown/highlight-mah';

	const heroCode = `struct Point { x, y }
fn add(a, b) {
    return Point { x: a.x + b.x, y: a.y + b.y }
}
let p = add(Point { x: 1, y: 2 }, Point { x: 3, y: 4 })
print(p)        # Point { x: 4, y: 6 }

enum Shape {
    Circle { r },
    Square { s },
    Empty
}
fn area(s) {
    match s {
        Shape.Circle { r } => { 3 * r * r }
        Shape.Square { s } => { s * s }
        Shape.Empty => { 0 }
    }
}
print(area(Shape.Circle { r: 5 }))   # 75`;

	const closureCode = `let counter = fn() {
    let count = 0
    return fn() {
        count = count + 1
        return count
    }
}
let next = counter()
print(next())   # 1
print(next())   # 2`;

	const asyncCode = `fn slow(n) { sleep_async(10); n * 2 }
let a = detach slow(21)
let b = detach { sleep_async(5); "from a block" }
print(a.await, b.await)   # 42 from a block`;

	const heroHtml = highlighter(heroCode, 'mah');
	const closureHtml = highlighter(closureCode, 'mah');
	const asyncHtml = highlighter(asyncCode, 'mah');

	const features: { title: string; body: string }[] = [
		{
			title: 'Closures that capture by reference',
			body: 'Heap-allocated Frames linked by a static chain (the classic SCP/DCP technique) mean a closure that outlives its call still sees later mutations of the variables it captured -- like JavaScript, not Python.'
		},
		{
			title: 'Structs, enums, and pattern matching',
			body: 'struct fields have no types; enum variants can be unit or struct-shaped. match handles literals, some/none, struct/enum destructuring, range patterns, guards, and wildcards.'
		},
		{
			title: 'Rust-style traits',
			body: 'trait declares required and default methods; impl Trait for Type implements them, including for your own types and (orphan-rule-respecting) built-ins like Number.'
		},
		{
			title: 'Lazy iterators and ranges',
			body: 'map/filter/skip/take/reduce work lazily over ranges, Strings, Vectors, Maps, and any type that implements Iterable + Iterator -- plus for loops and range patterns.'
		},
		{
			title: 'Cooperative async',
			body: 'detach any call or expression to get a Promise back, then .await it. Single-threaded and cooperative -- no hidden threads.'
		},
		{
			title: 'Default params and kwargs',
			body: 'fn greet(name, greeting = "Hello") and greet("Mah", greeting: "Salam") -- defaults and keyword arguments work for functions, methods, and static calls alike.'
		},
		{
			title: 'Modules with export/import',
			body: "export fn / export let mark what's visible to importers; import \"lib.mh\" or import m from \"lib\" bring it in, flat or namespaced. Diamond imports and cycles are safe."
		},
		{
			title: 'mah format',
			body: 'A whitespace-only formatter that verifies its own output: same tokens, same comments, same AST before it ever writes a file.'
		},
		{
			title: 'A real LSP + VS Code / Neovim',
			body: 'Live diagnostics, hover, completion, go-to-definition, cross-file rename, and format-on-save -- built on the same resolver the compiler uses, not a second analysis.'
		},
		{
			title: 'Portable bytecode, two VMs',
			body: 'mah build compiles to a portable .mahc format (docs/MAHC_FORMAT.md) with both a Python reference VM and a from-scratch Rust runtime (mah-vm) -- plus self-contained, dependency-free executables.'
		},
		{
			title: 'Project manifests',
			body: 'mah init scaffolds mah-project.toml, src/main.mh, and docs written for coding agents. mah run / mah build / mah check work from any subdirectory.'
		},
		{
			title: 'Optional type annotations',
			body: 'fn add(a: Number, b: Number) -> Number { a + b } -- syntax and generics exist today; a static type checker with inference is the next milestone (see /docs/types).'
		}
	];
</script>

<svelte:head>
	<title>Mah — a small language built from scratch</title>
	<meta
		name="description"
		content="Mah is a small programming language with closures, structs, enums, pattern matching, traits, async, and its own bytecode VMs -- hand-written, pure Python plus a Rust runtime, zero third-party dependencies."
	/>
</svelte:head>

<section class="hero">
	<div class="container hero-grid">
		<div class="hero-copy">
			<p class="eyebrow"><LangIcon size={30} /> ماه — "moon" in Persian</p>
			<h1>A small language, built entirely from scratch.</h1>
			<p class="lede">
				Mah is a hand-written lexer, parser, resolver, bytecode compiler, and VM for its own
				portable bytecode format -- plus a formatter, a real language server, and editor
				integrations. Every one of them pure Python, standard library only, with zero
				third-party runtime dependencies anywhere in the toolchain. A native Rust runtime runs
				the exact same bytecode.
			</p>
			<div class="cta-row">
				<a class="btn primary" href="/docs">Read the docs</a>
				<a class="btn" href="/blog">Blog</a>
				<a class="btn" href="https://github.com/sina-salahshour/mahlang" target="_blank" rel="noreferrer"
					>Source</a
				>
			</div>
		</div>
		<div class="hero-code">
			{@html heroHtml}
		</div>
	</div>
</section>

<section class="quickstart">
	<div class="container">
		<h2>Get started</h2>
		<p class="section-lede">
			Clone the repo and run programs immediately -- nothing to <code>pip install</code>. Once
			<code>mah</code> is on your <code>PATH</code> (<code>make install-mah</code>), every command
			below works from anywhere.
		</p>
		<QuickstartTabs />
	</div>
</section>

<section class="features">
	<div class="container">
		<h2>What's in the language</h2>
		<div class="feature-grid">
			{#each features as f (f.title)}
				<div class="feature">
					<h3>{f.title}</h3>
					<p>{f.body}</p>
				</div>
			{/each}
		</div>
	</div>
</section>

<section class="snippets">
	<div class="container snippet-grid">
		<div>
			<h3>Closures capture by reference</h3>
			<div class="snippet-code">{@html closureHtml}</div>
		</div>
		<div>
			<h3>Async: detach, then .await</h3>
			<div class="snippet-code">{@html asyncHtml}</div>
		</div>
	</div>
</section>

<section class="tooling">
	<div class="container tooling-grid">
		<div>
			<h2>Tooling that isn't an afterthought</h2>
			<p>
				<code>mah lsp</code> is a dependency-free LSP built directly on the same resolver the compiler
				uses: live diagnostics, hover, completion, go-to-definition, cross-file rename, and
				document formatting. It ships with a VS Code extension and Neovim integration
				(tree-sitter grammar + ftplugin).
			</p>
			<p>
				<code>mah build</code> compiles to a portable <code>.mahc</code> bytecode file that runs on
				the Python VM <em>or</em> the from-scratch Rust runtime (<code>--vm rust</code>), and
				<code>mah build --self-contained</code> bundles the runtime and bytecode into one executable
				file that needs neither Python nor <code>mah</code> installed to run.
			</p>
			<div class="cta-row">
				<a class="btn" href="/docs/tooling">LSP & editors</a>
				<a class="btn" href="/docs/projects">Projects & mah init</a>
			</div>
		</div>
	</div>
</section>

<style>
	section {
		padding: 3.5rem 0;
	}

	.hero {
		background: linear-gradient(
			180deg,
			var(--color-bg-raised) 0%,
			var(--color-bg) 100%
		);
		border-bottom: 1px solid var(--color-border);
		padding-top: 4rem;
	}

	.hero-grid {
		display: grid;
		grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
		gap: 3rem;
		align-items: center;
	}

	.eyebrow {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		color: var(--color-accent);
		font-weight: 700;
		letter-spacing: 0.02em;
		margin-bottom: 0.4rem;
	}

	.hero-copy h1 {
		font-size: clamp(2rem, 4vw, 2.9rem);
		margin-top: 0;
	}

	.lede {
		color: var(--color-text-muted);
		font-size: 1.05rem;
	}

	.cta-row {
		display: flex;
		flex-wrap: wrap;
		gap: 0.75rem;
		margin-top: 1.5rem;
	}

	.btn {
		display: inline-block;
		padding: 0.6rem 1.1rem;
		border-radius: var(--radius);
		border: 1px solid var(--color-border);
		background: var(--color-bg);
		color: var(--color-text);
		font-weight: 600;
		font-size: 0.92rem;
	}

	.btn:hover {
		text-decoration: none;
		border-color: var(--color-accent);
		color: var(--color-accent);
		transform: translateY(-1px);
		box-shadow: 0 6px 16px -10px rgb(0 0 0 / 35%);
	}

	.btn.primary {
		background: var(--color-accent);
		border-color: var(--color-accent);
		color: var(--color-accent-contrast);
	}

	.btn.primary:hover {
		color: var(--color-accent-contrast);
		opacity: 0.92;
	}

	.hero-code :global(pre.mah-code) {
		margin: 0;
		font-size: 0.82rem;
	}

	.section-lede {
		color: var(--color-text-muted);
		max-width: 46rem;
		margin-bottom: 1.75rem;
	}

	.feature-grid {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(min(15.5rem, 100%), 1fr));
		gap: 1.25rem;
		margin-top: 1.5rem;
	}

	.feature {
		border: 1px solid var(--color-border);
		border-radius: var(--radius);
		padding: 1.1rem 1.2rem;
		background: var(--color-bg-raised);
	}

	.feature:hover {
		transform: translateY(-3px);
		border-color: var(--color-accent);
		box-shadow: 0 8px 20px -12px rgb(0 0 0 / 25%);
	}

	.feature h3 {
		margin: 0 0 0.4rem;
		font-size: 1rem;
	}

	.feature p {
		margin: 0;
		font-size: 0.9rem;
		color: var(--color-text-muted);
	}

	.snippets {
		background: var(--color-bg-raised);
		border-top: 1px solid var(--color-border);
		border-bottom: 1px solid var(--color-border);
	}

	.snippet-grid {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(min(18rem, 100%), 1fr));
		gap: 2rem;
	}

	.snippet-grid h3 {
		margin-top: 0;
	}

	.snippet-code :global(pre.mah-code) {
		font-size: 0.82rem;
	}

	.tooling-grid p {
		color: var(--color-text-muted);
		max-width: 42rem;
	}

	.snippet-grid > :global(*),
	.hero-grid > :global(*) {
		min-width: 0;
	}

	@media (max-width: 860px) {
		.hero-grid {
			/* minmax(0, ...) rather than plain 1fr: a 1fr track's minimum
			   is its content's min-content width, so the hero's code block
			   used to widen the whole page past the viewport on phones. */
			grid-template-columns: minmax(0, 1fr);
			gap: 2rem;
		}
	}

	@media (max-width: 600px) {
		section {
			padding: 2.5rem 0;
		}

		.hero {
			padding-top: 2.5rem;
		}

		.cta-row .btn {
			flex: 1 1 auto;
			text-align: center;
		}
	}
</style>
