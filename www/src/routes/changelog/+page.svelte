<script lang="ts">
	import { changelogPosts } from '$lib/content';

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
	<title>Changelog · Mah</title>
	<meta name="description" content="Version-by-version changelog for the Mah language." />
</svelte:head>

<div class="container changelog">
	<header>
		<h1>Changelog</h1>
		<p class="lede">
			Every Mah release, newest first. These are the same posts as on the <a href="/blog"
				>blog</a
			>, tagged <code>changelog</code>.
		</p>
	</header>

	<ol class="releases">
		{#each changelogPosts as post (post.slug)}
			<li>
				<a href="/blog/{post.slug}" class="release-link">
					<div class="release-head">
						{#if post.meta.version}
							<span class="version">v{post.meta.version}</span>
						{/if}
						<time datetime={post.meta.date}>{formatDate(post.meta.date)}</time>
					</div>
					<h2>{post.meta.title}</h2>
					<p>{post.meta.description}</p>
				</a>
			</li>
		{/each}
	</ol>
</div>

<style>
	.changelog {
		padding: 2.5rem 1.5rem 4rem;
		max-width: 46rem;
	}

	.lede {
		color: var(--color-text-muted);
	}

	.lede code {
		font-size: 0.85em;
	}

	.releases {
		list-style: none;
		margin: 2rem 0 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 1.75rem;
	}

	.release-link {
		display: block;
		color: var(--color-text);
		border-left: 3px solid var(--color-border);
		padding-left: 1rem;
	}

	.release-link:hover {
		text-decoration: none;
		border-left-color: var(--color-accent);
		transform: translateX(3px);
	}

	.release-link:hover h2 {
		color: var(--color-accent);
	}

	.release-head {
		display: flex;
		align-items: center;
		gap: 0.6rem;
		font-size: 0.85rem;
		color: var(--color-text-muted);
	}

	.version {
		font-family: var(--font-mono);
		font-weight: 700;
		color: var(--color-text);
	}

	.release-link h2 {
		margin: 0.2em 0 0.2em;
	}

	.release-link p {
		margin: 0;
		color: var(--color-text-muted);
	}
</style>
