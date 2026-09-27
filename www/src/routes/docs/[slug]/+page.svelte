<script lang="ts">
	let { data } = $props();
	const Doc = $derived(data.doc.component);
</script>

<svelte:head>
	<title>{data.doc.meta.title} · Mah docs</title>
	<meta name="description" content="Mah language docs: {data.doc.meta.title}" />
</svelte:head>

<article>
	<Doc />
</article>
