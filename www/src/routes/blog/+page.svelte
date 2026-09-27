<script lang="ts">
	import { posts } from '$lib/content';

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
	<title>Blog · Mah</title>
	<meta name="description" content="Updates, design notes, and changelogs for the Mah language." />
</svelte:head>

<div class="container blog-index">
	<header>
		<h1>Blog</h1>
		<p class="lede">
			Design notes and release history for Mah. Version changelogs live here too, and also at
			<a href="/changelog">/changelog</a>.
		</p>
	</header>

	<ul class="posts">
		{#each posts as post (post.slug)}
			<li>
				<a href="/blog/{post.slug}" class="post-link">
					<div class="post-meta">
						<time datetime={post.meta.date}>{formatDate(post.meta.date)}</time>
						{#if post.meta.version}
							<span class="badge accent">v{post.meta.version}</span>
						{/if}
						{#each post.meta.tags ?? [] as tag (tag)}
							<span class="badge">{tag}</span>
						{/each}
					</div>
					<h2>{post.meta.title}</h2>
					<p>{post.meta.description}</p>
				</a>
			</li>
		{/each}
	</ul>
</div>

<style>
	.blog-index {
		padding: 2.5rem 1.5rem 4rem;
		max-width: 46rem;
	}

	.lede {
		color: var(--color-text-muted);
	}

	.posts {
		list-style: none;
		margin: 2rem 0 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 1.75rem;
	}

	.post-link {
		display: block;
		color: var(--color-text);
	}

	.post-link:hover {
		text-decoration: none;
		transform: translateX(3px);
	}

	.post-link:hover h2 {
		color: var(--color-accent);
	}

	.post-meta {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		font-size: 0.85rem;
		color: var(--color-text-muted);
		margin-bottom: 0.3rem;
	}

	.post-link h2 {
		margin: 0.1em 0 0.2em;
	}

	.post-link p {
		margin: 0;
		color: var(--color-text-muted);
	}
</style>
