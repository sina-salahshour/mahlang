<script lang="ts">
	let { data } = $props();
	const Post = $derived(data.post.component);

	function formatDate(d: string) {
		return new Date(d + 'T00:00:00Z').toLocaleDateString('en-US', {
			year: 'numeric',
			month: 'long',
			day: 'numeric',
			timeZone: 'UTC'
		});
	}
</script>

<svelte:head>
	<title>{data.post.meta.title} · Mah blog</title>
	<meta name="description" content={data.post.meta.description} />
</svelte:head>

<div class="container post">
	<a href="/blog" class="back">← All posts</a>
	<header>
		<div class="post-meta">
			<time datetime={data.post.meta.date}>{formatDate(data.post.meta.date)}</time>
			{#if data.post.meta.version}
				<span class="badge accent">v{data.post.meta.version}</span>
			{/if}
			{#each data.post.meta.tags ?? [] as tag (tag)}
				<span class="badge">{tag}</span>
			{/each}
		</div>
		<h1>{data.post.meta.title}</h1>
	</header>
	<div class="prose">
		<Post />
	</div>
</div>

<style>
	.post {
		padding: 2.5rem 1.5rem 4rem;
		max-width: 44rem;
	}

	.back {
		font-size: 0.9rem;
		color: var(--color-text-muted);
	}

	.post-meta {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		font-size: 0.85rem;
		color: var(--color-text-muted);
		margin: 1.2rem 0 0.3rem;
	}

	header h1 {
		margin-top: 0.1em;
	}
</style>
