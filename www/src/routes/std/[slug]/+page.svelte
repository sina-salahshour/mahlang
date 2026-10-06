<script lang="ts">
	let { data } = $props();
	const Doc = $derived(data.doc.component);
</script>

<svelte:head>
	<title>{data.doc.meta.title} · Mah standard library</title>
	<meta
		name="description"
		content={data.doc.meta.summary ?? `The ${data.doc.meta.title} module of Mah's standard library.`}
	/>
</svelte:head>

<article>
	<p class="crumbs"><a href="/std">Standard library</a> / {data.doc.meta.section}</p>
	<Doc />
</article>

<nav class="pager" aria-label="Modules">
	{#if data.prev}
		<a class="prev" href="/std/{data.prev.slug}">
			<span>Previous</span>
			<code>{data.prev.title}</code>
		</a>
	{/if}
	{#if data.next}
		<a class="next" href="/std/{data.next.slug}">
			<span>Next</span>
			<code>{data.next.title}</code>
		</a>
	{/if}
</nav>

<style>
	.crumbs {
		margin: 0 0 0.5rem;
		font-size: 0.85rem;
		color: var(--color-text-muted);
	}

	.pager {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		gap: 1rem;
		margin-top: 3rem;
		padding-top: 1.5rem;
		border-top: 1px solid var(--color-border);
	}

	.pager a {
		display: flex;
		flex-direction: column;
		gap: 0.2rem;
		padding: 0.75rem 1rem;
		border: 1px solid var(--color-border);
		border-radius: var(--radius);
		color: var(--color-text);
	}

	.pager a:hover {
		text-decoration: none;
		border-color: var(--color-accent);
	}

	.pager span {
		font-size: 0.8rem;
		color: var(--color-text-muted);
	}

	.pager code {
		background: none;
		padding: 0;
		color: var(--color-accent);
	}

	.next {
		grid-column: 2;
		text-align: right;
	}
</style>
