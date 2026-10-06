<script lang="ts">
	import { page } from '$app/state';
	import { Accordion } from 'melt/builders';
	import { docSections, type DocSection } from '$lib/content';

	// Defaults are the /docs sidebar; the standard library reference
	// (/std) passes its own sections and base path.
	let {
		sections = docSections(),
		base = '/docs',
		label = 'Docs',
		onnavigate
	}: {
		sections?: DocSection[];
		base?: string;
		label?: string;
		onnavigate?: () => void;
	} = $props();

	const accordion = new Accordion({
		multiple: true,
		// svelte-ignore state_referenced_locally
		value: sections.map((s) => s.name)
	});

	function isActive(slug: string) {
		return page.params.slug === slug;
	}

	// Sliding highlight behind the active doc link -- one element that
	// moves between link positions (plain CSS transition) instead of the
	// active class just jumping, and instead of the whole sidebar
	// re-rendering/fading when you go from one doc page to another.
	let docRefs: Record<string, HTMLAnchorElement> = $state({});
	// Bumped whenever the nav changes size -- e.g. the mobile sidebar going
	// from display:none (every offset 0) to shown, or a section expanding --
	// so the indicator re-measures instead of keeping stale offsets.
	let navEl: HTMLElement | undefined = $state();
	let layoutTick = $state(0);
	$effect(() => {
		if (!navEl) return;
		const ro = new ResizeObserver(() => layoutTick++);
		ro.observe(navEl);
		return () => ro.disconnect();
	});
	let indicatorStyle = $derived.by(() => {
		void layoutTick;
		const slug = page.params.slug;
		const el = slug ? docRefs[slug] : undefined;
		if (!el || !el.offsetWidth) return 'opacity: 0';
		return `top: ${el.offsetTop}px; left: ${el.offsetLeft}px; width: ${el.offsetWidth}px; height: ${el.offsetHeight}px; opacity: 1;`;
	});
</script>

<nav class="docs-sidebar" bind:this={navEl} aria-label={label}>
	<span class="side-indicator" style={indicatorStyle}></span>
	{#each sections as section (section.name)}
		{@const item = accordion.getItem({ id: section.name })}
		<div class="section">
			<h2 {...item.heading}>
				<button {...item.trigger} class="section-trigger">
					<span>{section.name}</span>
					<svg
						class="chevron"
						class:open={item.isExpanded}
						viewBox="0 0 24 24"
						width="14"
						height="14"
						aria-hidden="true"
					>
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
			</h2>
			{#if item.isExpanded}
				<ul {...item.content}>
					{#each section.docs as doc (doc.slug)}
						<li>
							<a
								href="{base}/{doc.slug}"
								onclick={() => onnavigate?.()}
								bind:this={docRefs[doc.slug]}
								class:active={isActive(doc.slug)}>{doc.meta.title}</a
							>
						</li>
					{/each}
				</ul>
			{/if}
		</div>
	{/each}
</nav>

<style>
	.docs-sidebar {
		position: relative;
		font-size: 0.92rem;
	}

	.side-indicator {
		position: absolute;
		background: var(--color-bg-inset);
		border-radius: 6px;
		transition:
			top 0.2s cubic-bezier(0.4, 0, 0.2, 1),
			left 0.2s cubic-bezier(0.4, 0, 0.2, 1),
			width 0.2s cubic-bezier(0.4, 0, 0.2, 1),
			height 0.2s cubic-bezier(0.4, 0, 0.2, 1),
			opacity 0.15s ease;
		z-index: 0;
		pointer-events: none;
	}

	.section {
		margin-bottom: 0.25rem;
	}

	.section h2 {
		margin: 0;
		font-size: inherit;
	}

	.section-trigger {
		width: 100%;
		display: flex;
		align-items: center;
		justify-content: space-between;
		background: none;
		border: none;
		padding: 0.5rem 0.25rem;
		font-weight: 700;
		font-size: 0.78rem;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--color-text-muted);
		cursor: pointer;
	}

	.chevron {
		transition: transform 0.15s ease;
	}

	.chevron.open {
		transform: rotate(180deg);
	}

	ul {
		list-style: none;
		margin: 0 0 0.5rem;
		padding: 0;
		display: flex;
		flex-direction: column;
	}

	li a {
		position: relative;
		z-index: 1;
		display: block;
		padding: 0.35rem 0.6rem;
		border-radius: 6px;
		color: var(--color-text);
	}

	li a:hover {
		background: var(--color-bg-raised);
		text-decoration: none;
	}

	li a.active {
		color: var(--color-accent);
		font-weight: 600;
	}
</style>
