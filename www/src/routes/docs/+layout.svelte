<script lang="ts">
	import DocsSidebar from '$lib/components/DocsSidebar.svelte';

	let { children } = $props();
</script>

<div class="container docs-layout">
	<aside class="sidebar">
		<DocsSidebar />
	</aside>
	<div class="content prose">
		{@render children()}
	</div>
</div>

<style>
	.docs-layout {
		display: grid;
		grid-template-columns: 15rem minmax(0, 1fr);
		gap: 2.5rem;
		padding-top: 2rem;
		padding-bottom: 4rem;
		align-items: start;
	}

	.sidebar {
		position: sticky;
		top: 5rem;
		max-height: calc(100vh - 6rem);
		overflow-y: auto;
		/* Excluded from the page cross-fade (muted in app.css) -- only the
		   active-doc indicator inside it should move between doc pages,
		   the sidebar itself shouldn't fade out and back in. */
		view-transition-name: docs-sidebar;
	}

	.content {
		min-width: 0;
	}

	@media (max-width: 860px) {
		.docs-layout {
			grid-template-columns: 1fr;
		}

		.sidebar {
			position: static;
			max-height: none;
			border-bottom: 1px solid var(--color-border);
			padding-bottom: 1rem;
			margin-bottom: 1rem;
		}
	}
</style>
