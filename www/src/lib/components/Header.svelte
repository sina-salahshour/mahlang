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

	// melt's outside-tap close listens on `document`, which iOS Safari
	// doesn't reliably fire for taps on non-interactive areas. A tap on the
	// ::backdrop is delivered to the <dialog> itself, so handle it here too:
	// anything outside the sheet's box is the backdrop.
	function onSheetClick(e: MouseEvent) {
		const sheet = e.currentTarget as HTMLDialogElement;
		if (e.target !== sheet) return;
		const r = sheet.getBoundingClientRect();
		const inside =
			e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom;
		if (!inside) mobile.open = false;
	}
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

	<dialog {...mobile.content} class="mobile-sheet" aria-label="Menu" onclick={onSheetClick}>
		<div class="sheet-head">
			<span class="sheet-title">Menu</span>
			<!-- The header's hamburger sits under the modal sheet, so the sheet
			     needs its own close button. -->
			<button class="sheet-close" type="button" aria-label="Close menu" onclick={() => (mobile.open = false)}>
				<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true">
					<path
						d="M6 6l12 12M18 6L6 18"
						stroke="currentColor"
						stroke-width="2"
						stroke-linecap="round"
					/>
				</svg>
			</button>
		</div>
		<nav aria-label="Mobile">
			{#each nav as item (item.href)}
				<a
					href={item.href}
					class:active={isActive(item.href)}
					aria-current={isActive(item.href) ? 'page' : undefined}
					onclick={() => (mobile.open = false)}>{item.label}</a
				>
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
	   sheet instead of the browser's default centered dialog.

	   The open/closed look must hang off melt's `data-open`, NOT the native
	   `[open]` attribute: to close, melt clears `data-open` and waits for
	   `transitionend` before calling close(), so `[open]` stays set the
	   whole time. Styling on `[open]` meant nothing changed, no transition
	   ran, close() never came, and the sheet stuck open (invisibly
	   blocking the page) after Escape, an outside click or a link tap.
	   Opening works the same way in reverse: showModal() first (closed
	   look), then `data-open` on the next tick, so the slide-in transitions
	   without needing @starting-style. */
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
		padding: 0 1rem 1.5rem;
		box-shadow: -12px 0 32px -16px rgb(0 0 0 / 35%);
		transform: translateX(100%);
		opacity: 0;
		transition:
			transform 0.25s cubic-bezier(0.4, 0, 0.2, 1),
			opacity 0.2s ease;
	}

	.mobile-sheet[data-open] {
		transform: translateX(0);
		opacity: 1;
	}

	.mobile-sheet::backdrop {
		background: var(--sheet-backdrop, rgb(0 0 0 / 40%));
		opacity: 0;
		transition: opacity 0.25s ease;
	}

	.mobile-sheet[data-open]::backdrop {
		opacity: 1;
	}

	.mobile-sheet::backdrop {
		cursor: pointer;
	}

	/* Same height as the site header, so the X lands where the hamburger
	   was. */
	.sheet-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		height: 3.75rem;
		margin-bottom: 0.75rem;
		padding-left: 0.5rem;
		border-bottom: 1px solid var(--color-border);
	}

	.sheet-title {
		font-size: 0.78rem;
		font-weight: 700;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--color-text-muted);
	}

	.sheet-close {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 2.5rem;
		height: 2.5rem;
		border-radius: 8px;
		border: 1px solid var(--color-border);
		background: var(--color-bg-raised);
		color: var(--color-text);
		cursor: pointer;
	}

	.sheet-close:hover {
		border-color: var(--color-accent);
		color: var(--color-accent);
	}

	.mobile-sheet nav {
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
	}

	/* Full-width rows with comfortable tap targets (>= 44px). */
	.mobile-sheet nav a {
		display: block;
		padding: 0.7rem 0.5rem;
		border-radius: 8px;
		font-size: 1.05rem;
		color: var(--color-text);
		font-weight: 500;
	}

	.mobile-sheet nav a:hover {
		background: var(--color-bg-raised);
		text-decoration: none;
	}

	.mobile-sheet nav a.active {
		background: var(--color-bg-inset);
		color: var(--color-accent);
		font-weight: 600;
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
