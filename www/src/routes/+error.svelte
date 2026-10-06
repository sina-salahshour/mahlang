<script lang="ts">
	import { page } from '$app/state';

	const notFound = $derived(page.status === 404);
</script>

<svelte:head>
	<title>{notFound ? 'Page not found' : 'Something went wrong'} · Mah</title>
</svelte:head>

<div class="container error-page">
	<p class="code">{page.status}</p>
	<h1>{notFound ? 'Page not found' : 'Something went wrong'}</h1>
	<p class="message">
		{#if notFound}
			There's nothing at <code>{page.url.pathname}</code>. It may have moved, or the link may be
			mistyped.
		{:else}
			{page.error?.message ?? 'An unexpected error occurred.'}
		{/if}
	</p>
	<nav class="ways" aria-label="Where to go">
		<a class="btn primary" href="/">Home</a>
		<a class="btn" href="/docs">Docs</a>
		<a class="btn" href="/std">Standard library</a>
		<a class="btn" href="/blog">Blog</a>
	</nav>
</div>

<style>
	.error-page {
		padding-top: 4rem;
		padding-bottom: 4rem;
		max-width: 40rem;
	}

	.code {
		margin: 0;
		font-family: var(--font-mono);
		font-weight: 700;
		font-size: 0.95rem;
		color: var(--color-accent);
	}

	h1 {
		margin-top: 0.25rem;
	}

	.message {
		color: var(--color-text-muted);
		overflow-wrap: anywhere;
	}

	.ways {
		display: flex;
		flex-wrap: wrap;
		gap: 0.75rem;
		margin-top: 1.75rem;
	}

	.btn {
		display: inline-block;
		padding: 0.55rem 1rem;
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
	}

	.btn.primary {
		background: var(--color-accent);
		border-color: var(--color-accent);
		color: var(--color-accent-contrast);
	}
</style>
