<script lang="ts">
	import LangIcon from './LangIcon.svelte';
</script>

<footer class="site-footer">
	<div class="container bar">
		<p class="colophon">
			<LangIcon size={20} /> Mah — ماه, "moon" in Persian. Built from scratch, pure Python (plus a
			Rust runtime).
		</p>
		<nav aria-label="Footer">
			<a href="/docs">Docs</a>
			<a href="/blog">Blog</a>
			<a href="/changelog">Changelog</a>
			<a href="/blog/rss.xml">RSS</a>
			<a href="https://github.com/sina-salahshour/mahlang" target="_blank" rel="noreferrer"
				>GitHub</a
			>
		</nav>
	</div>
</footer>

<style>
	.site-footer {
		border-top: 1px solid var(--color-border);
		margin-top: 4rem;
		padding: 2rem 0;
		color: var(--color-text-muted);
		font-size: 0.9rem;
	}

	.bar {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		justify-content: space-between;
		gap: 1rem;
	}

	.colophon {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		margin: 0;
	}

	nav {
		display: flex;
		gap: 1.1rem;
	}

	nav a {
		color: var(--color-text-muted);
	}

	nav a:hover {
		color: var(--color-text);
	}
</style>
