<script lang="ts">
	let { data } = $props();
	const Doc = $derived(data.doc.component);
</script>

<svelte:head>
	<title>{data.doc.meta.title} · Mah docs</title>
	<meta name="description" content="Mah language docs: {data.doc.meta.title}" />
</svelte:head>

<article>
	<!-- The Markdown files carry their title in frontmatter only; render it
	     here so every page starts with a heading. -->
	<h1>{data.doc.meta.title}</h1>
	<Doc />
</article>
