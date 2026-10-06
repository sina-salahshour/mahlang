<script lang="ts">
	import { page } from '$app/state';
	import { Dialog } from 'melt/builders';
	import ThemeToggle from './ThemeToggle.svelte';

	const nav = [
		{ href: '/docs', label: 'Docs' },
		{ href: '/std', label: 'Stdlib' },
		{ href: '/blog', label: 'Blog' },
		{ href: '/changelog', label: 'Changelog' }
	];

	function isActive(href: string) {
		return page.url.pathname === href || page.url.pathname.startsWith(href + '/');
	}

	// Sliding active-link indicator: a single element whose left/width
	// animate (plain CSS transition -- the nav persists across client-side
	// navigations, so this just works) to the newly active link instead of
	// the whole navbar re-animating on every route change.
	let navRefs: Record<string, HTMLAnchorElement> = $state({});
	let indicatorStyle = $derived.by(() => {
		const active = nav.find((item) => isActive(item.href));
		const el = active ? navRefs[active.href] : undefined;
		if (!el) return 'opacity: 0';
		return `left: ${el.offsetLeft}px; width: ${el.offsetWidth}px; opacity: 1;`;
	});

	// Off-canvas nav for small screens, built on a real <dialog> -- melt's
	// Dialog builder only drives native showModal()/close() (and the
	// ::backdrop below) when the content ref is an HTMLDialogElement, which
	// is also what gives us focus trap, Escape-to-close, and
	// outside-click-to-close for free.
	const mobile = new Dialog();
</script>

<header class="site-header">
	<div class="container bar">
		<a href="/" class="brand">
			<img src="/icons/mah-lang-dark.svg" alt="" class="brand-icon" width="40" height="40" />
			<span class="brand-name">Mah</span>
		</a>

		<nav class="primary-nav" aria-label="Primary">
			{#each nav as item (item.href)}
				<a
					href={item.href}
					bind:this={navRefs[item.href]}
					class:active={isActive(item.href)}>{item.label}</a
				>
			{/each}
			<span class="nav-indicator" style={indicatorStyle}></span>
		</nav>

		<div class="actions">
			<a
				class="gh-link"
				href="https://github.com/sina-salahshour/mahlang"
				target="_blank"
				rel="noreferrer"
			>
				GitHub
			</a>
			<ThemeToggle />
			<button
				{...mobile.trigger}
				class="menu-btn"
				aria-label={mobile.open ? 'Close menu' : 'Open menu'}
				aria-expanded={mobile.open}
			>
				<svg
					class="menu-icon"
					class:open={mobile.open}
					viewBox="0 0 24 24"
					width="20"
					height="20"
					aria-hidden="true"
				>
					<path
						class="menu-bar menu-bar-top"
						stroke="currentColor"
						stroke-width="2"
						stroke-linecap="round"
						d="M3 6h18"
					/>
					<path
						class="menu-bar menu-bar-mid"
						stroke="currentColor"
						stroke-width="2"
						stroke-linecap="round"
						d="M3 12h18"
					/>
					<path
						class="menu-bar menu-bar-bot"
						stroke="currentColor"
						stroke-width="2"
						stroke-linecap="round"
						d="M3 18h18"
					/>
				</svg>
			</button>
		</div>
	</div>

	<dialog {...mobile.content} class="mobile-sheet">
		<nav aria-label="Mobile">
			{#each nav as item (item.href)}
				<a href={item.href} onclick={() => (mobile.open = false)}>{item.label}</a>
			{/each}
			<!-- .gh-link is hidden on small screens, so GitHub lives here instead. -->
			<a href="https://github.com/sina-salahshour/mahlang" target="_blank" rel="noreferrer">GitHub</a>
		</nav>
	</dialog>
</header>

<style>
	.site-header {
		border-bottom: 1px solid var(--color-border);
		background: var(--color-bg);
		position: sticky;
		top: 0;
		z-index: 20;
	}

	.bar {
		display: flex;
		align-items: center;
		gap: 1.5rem;
		height: 3.75rem;
	}

	.brand {
		display: flex;
		align-items: center;
		gap: 0.55rem;
		font-weight: 700;
		font-size: 1.15rem;
		color: var(--color-text);
	}

	.brand:hover {
		text-decoration: none;
		color: var(--color-text);
	}

	.brand-icon {
		width: 2.5rem;
		height: 2.5rem;
		border-radius: 22%;
		transition: transform 0.2s ease;
	}

	.brand:hover .brand-icon {
		transform: rotate(-6deg) scale(1.05);
	}

	.primary-nav {
		position: relative;
		display: flex;
		gap: 1.25rem;
	}

	.primary-nav a {
		color: var(--color-text-muted);
		font-weight: 500;
		font-size: 0.95rem;
		padding: 0.4rem 0;
	}

	.primary-nav a:hover {
		color: var(--color-text);
		text-decoration: none;
	}

	.primary-nav a.active {
		color: var(--color-text);
	}

	/* Sliding indicator under the active link -- position/size are set
	   inline (indicatorStyle), only the motion between values is CSS. */
	.nav-indicator {
		position: absolute;
		bottom: 0;
		height: 2px;
		background: var(--color-accent);
		border-radius: 2px;
		transition:
			left 0.22s cubic-bezier(0.4, 0, 0.2, 1),
			width 0.22s cubic-bezier(0.4, 0, 0.2, 1),
			opacity 0.15s ease;
		pointer-events: none;
	}

	.actions {
		display: flex;
		align-items: center;
		gap: 0.75rem;
		/* Always pin to the right edge of the bar -- previously this relied
		   on .primary-nav's margin-right: auto, which stopped working once
		   that nav is display:none on mobile (a hidden element's margin
		   doesn't push anything), leaving the hamburger button stranded
		   right next to the logo instead of at the edge. */
		margin-left: auto;
	}

	.gh-link {
		font-size: 0.9rem;
		color: var(--color-text-muted);
		font-weight: 500;
	}

	.menu-btn {
		display: none;
		align-items: center;
		justify-content: center;
		width: 2.1rem;
		height: 2.1rem;
		border-radius: 8px;
		border: 1px solid var(--color-border);
		background: var(--color-bg-raised);
		color: var(--color-text);
		cursor: pointer;
		flex: none;
	}

	.menu-btn:hover {
		border-color: var(--color-accent);
		color: var(--color-accent);
	}

	/* Hamburger -> X, driven by mobile.open via the `open` class. */
	.menu-bar {
		transform-box: fill-box;
		transform-origin: center;
		transition:
			transform 0.2s ease,
			opacity 0.2s ease;
	}

	.menu-icon.open .menu-bar-top {
		transform: translateY(6px) rotate(45deg);
	}

	.menu-icon.open .menu-bar-mid {
		opacity: 0;
	}

	.menu-icon.open .menu-bar-bot {
		transform: translateY(-6px) rotate(-45deg);
	}

	/* The off-canvas nav is a real <dialog> (see the script for why: melt's
	   Dialog builder drives native showModal()/close() only when the
	   content ref is an HTMLDialogElement), positioned as a right-side
	   sheet instead of the browser's default centered dialog, animated with
	   a plain CSS transition -- melt itself waits for `transitionend`
	   before actually closing it, so this "just works" with the builder. */
	.mobile-sheet {
		position: fixed;
		inset: 0 0 0 auto;
		margin: 0;
		height: 100%;
		width: min(80vw, 18rem);
		max-width: none;
		max-height: none;
		border: none;
		border-left: 1px solid var(--color-border);
		background: var(--color-bg);
		color: var(--color-text);
		padding: 1.5rem;
		box-shadow: -12px 0 32px -16px rgb(0 0 0 / 35%);
		transform: translateX(100%);
		opacity: 0;
		transition:
			transform 0.25s cubic-bezier(0.4, 0, 0.2, 1),
			opacity 0.2s ease,
			overlay 0.25s allow-discrete,
			display 0.25s allow-discrete;
	}

	.mobile-sheet[open] {
		transform: translateX(0);
		opacity: 1;
	}

	@starting-style {
		.mobile-sheet[open] {
			transform: translateX(100%);
			opacity: 0;
		}
	}

	.mobile-sheet::backdrop {
		background: rgb(0 0 0 / 40%);
		opacity: 0;
		transition:
			opacity 0.25s ease,
			overlay 0.25s allow-discrete,
			display 0.25s allow-discrete;
	}

	.mobile-sheet[open]::backdrop {
		opacity: 1;
	}

	@starting-style {
		.mobile-sheet[open]::backdrop {
			opacity: 0;
		}
	}

	.mobile-sheet nav {
		display: flex;
		flex-direction: column;
		gap: 1rem;
	}

	.mobile-sheet nav a {
		font-size: 1.05rem;
		color: var(--color-text);
		font-weight: 500;
	}

	@media (max-width: 400px) {
		.bar {
			gap: 0.75rem;
		}

		.brand-icon {
			width: 2.1rem;
			height: 2.1rem;
		}
	}

	@media (max-width: 720px) {
		.primary-nav,
		.gh-link {
			display: none;
		}

		.menu-btn {
			display: inline-flex;
		}
	}
</style>
