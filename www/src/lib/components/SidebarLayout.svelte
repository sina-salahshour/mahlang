<script lang="ts">
	import type { Snippet } from 'svelte';
	import { page } from '$app/state';
	import { afterNavigate } from '$app/navigation';
	import DocsSidebar from './DocsSidebar.svelte';
	import type { DocSection } from '$lib/content';

	// Sidebar + content shell shared by /docs and /std. On wide screens the
	// sidebar sits sticky on the left; at 860px and below it collapses
	// behind a toggle bar (showing the current page's title) so phones see
	// the content first instead of scrolling past the whole page list.
	let {
		sections,
		base,
		label,
		children
	}: {
		sections: DocSection[];
		base: string;
		label: string;
		children: Snippet;
	} = $props();

	let open = $state(false);

	const current = $derived(
		sections.flatMap((s) => s.docs).find((d) => d.slug === page.params.slug)?.meta.title
	);

	// Collapse again after following a link (including back/forward).
	afterNavigate(() => {
		open = false;
	});
</script>

<div class="container sidebar-layout">
	<aside class="sidebar" class:open>
		<button
			class="sidebar-toggle"
			aria-expanded={open}
			aria-controls="sidebar-nav"
			onclick={() => (open = !open)}
		>
			<span class="toggle-label">{label}</span>
			{#if current}<span class="toggle-current">{current}</span>{/if}
			<svg class="chevron" viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
				<path
					d="M6 9l6 6 6-6"
					stroke="currentColor"
					stroke-width="2"
					fill="none"
					stroke-linecap="round"
					stroke-linejoin="round"
				/>
			</svg>
		</button>
		<div class="sidebar-nav" id="sidebar-nav">
			<DocsSidebar {sections} {base} {label} onnavigate={() => (open = false)} />
		</div>
	</aside>
	<div class="content prose">
		{@render children()}
	</div>
</div>

<style>
	.sidebar-layout {
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

	.sidebar-toggle {
		display: none;
	}

	.content {
		min-width: 0;
	}

	@media (max-width: 860px) {
		.sidebar-layout {
			grid-template-columns: minmax(0, 1fr);
			gap: 1.25rem;
			padding-top: 0;
		}

		/* Sticks just under the site header so the page list is one tap
		   away while reading. */
		.sidebar {
			top: 3.75rem;
			z-index: 10;
			max-height: none;
			overflow: visible;
			margin: 0 -1.5rem;
			padding: 0 1.5rem;
			background: var(--color-bg);
			border-bottom: 1px solid var(--color-border);
		}

		.sidebar-toggle {
			display: flex;
			align-items: center;
			gap: 0.6rem;
			width: 100%;
			padding: 0.75rem 0;
			background: none;
			border: none;
			color: var(--color-text);
			font: inherit;
			font-size: 0.92rem;
			cursor: pointer;
			text-align: left;
		}

		.toggle-label {
			font-weight: 700;
			font-size: 0.75rem;
			text-transform: uppercase;
			letter-spacing: 0.04em;
			color: var(--color-text-muted);
			flex: none;
		}

		.toggle-current {
			flex: 1;
			min-width: 0;
			overflow: hidden;
			text-overflow: ellipsis;
			white-space: nowrap;
			font-weight: 600;
		}

		.chevron {
			margin-left: auto;
			flex: none;
			transition: transform 0.15s ease;
		}

		.sidebar.open .chevron {
			transform: rotate(180deg);
		}

		.sidebar-nav {
			display: none;
			max-height: calc(100vh - 8.5rem);
			max-height: calc(100dvh - 8.5rem);
			overflow-y: auto;
			padding-bottom: 0.75rem;
		}

		.sidebar.open .sidebar-nav {
			display: block;
		}
	}

	@media (max-width: 600px) {
		.sidebar {
			margin: 0 -1rem;
			padding: 0 1rem;
		}
	}
</style>
