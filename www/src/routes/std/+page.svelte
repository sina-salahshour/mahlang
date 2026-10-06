<script lang="ts">
	import { stdSections } from '$lib/content';
	import { highlighter } from '$lib/markdown/highlight-mah';

	const sections = stdSections();

	const importHtml = highlighter(
		`import json from "std:json"           # namespaced: json.parse(...), json.JsonError
import "std:collections"              # flat: Set, Deque, PriorityQueue

let config = try json.parse("{\\"port\\": 8080}") else [:]
let seen = Set.of(["a", "b", "a"])
print(config["port"], seen.len())     # 8080 2`,
		'mah'
	);
</script>

<svelte:head>
	<title>Standard library · Mah</title>
	<meta
		name="description"
		content="A guide to every module in Mah's standard library: what it's for, how to use it, and its full API."
	/>
</svelte:head>

<h1>Standard library</h1>

<p class="lede">
	Mah ships with a standard library of {sections.reduce((n, s) => n + s.docs.length, 0)} modules,
	written in Mah itself (with a few natives underneath) so the Python VM and the Rust
	<code>mah-vm</code> behave identically. Each page below explains what a module is for, walks through
	using it, and lists its full API.
</p>

<h2>Importing a module</h2>

<p>
	Standard library modules are imported with a <code>std:</code> path, either
	<strong>namespaced</strong> (everything is reached through the name you give it) or
	<strong>flat</strong> (its exports become plain names in your file):
</p>

{@html importHtml}

<ul>
	<li>
		<code>std:</code> names are reserved: they never refer to a file of yours, and an unknown one
		(<code>import "std:nope"</code>) is a compile error.
	</li>
	<li>
		A module's types are reached like its functions (<code>json.JsonError</code>,
		<code>fs.File</code>) and are module-scoped, so your own <code>struct Match</code> never clashes
		with <code>regex.Match</code>.
	</li>
	<li>
		Errors a module throws are ordinary structs or enums you <code>catch</code> by type; the type
		checker knows what each function throws (see <a href="/docs/errors">Errors</a>).
	</li>
	<li>
		In your editor, hover, completion and go-to-definition work on standard library functions like
		on your own.
	</li>
</ul>

{#each sections as section (section.name)}
	<h2>{section.name}</h2>
	<div class="module-grid">
		{#each section.docs as mod (mod.slug)}
			<a class="module" href="/std/{mod.slug}">
				<code class="module-name">{mod.meta.title}</code>
				<span class="module-summary">{mod.meta.summary}</span>
			</a>
		{/each}
	</div>
{/each}

<h2>How it's built</h2>

<p>
	Each module is a Mah file inside the <code>mah</code> package (<code>mah/std/math.mh</code>). The
	functions that need the machine (<code>math.tan</code>, file and socket I/O, TLS) are declared
	with <code>extern fn</code>, which binds a Mah function to a native the VM provides. Only standard
	library modules may use <code>extern fn</code>. New natives come with a new bytecode minor version:
	a compiled program is marked with the lowest version it needs, and an older runtime refuses it,
	naming the natives it lacks.
</p>

<style>
	.lede {
		font-size: 1.05rem;
		color: var(--color-text-muted);
	}

	.module-grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(min(15rem, 100%), 1fr));
		gap: 0.9rem;
		margin: 1rem 0 1.5rem;
	}

	.module {
		display: flex;
		flex-direction: column;
		gap: 0.35rem;
		padding: 0.9rem 1rem;
		border: 1px solid var(--color-border);
		border-radius: var(--radius);
		background: var(--color-bg-raised);
		color: var(--color-text);
	}

	.module:hover {
		text-decoration: none;
		border-color: var(--color-accent);
		transform: translateY(-2px);
		box-shadow: 0 8px 20px -12px rgb(0 0 0 / 25%);
	}

	.module-name {
		align-self: flex-start;
		font-weight: 700;
		color: var(--color-accent);
		background: none;
		padding: 0;
		font-size: 0.95rem;
	}

	.module-summary {
		font-size: 0.88rem;
		color: var(--color-text-muted);
		line-height: 1.5;
	}
</style>
