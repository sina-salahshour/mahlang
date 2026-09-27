<script lang="ts">
	import { Toggle } from 'melt/builders';
	import { PersistedState } from 'runed';

	// Persisted across reloads/tabs (runed) and driving a headless toggle
	// button (melt) -- see src/app.html for the matching pre-paint script
	// that avoids a light/dark flash on load.
	const stored = new PersistedState<'light' | 'dark' | null>('mah-theme', null);

	function prefersDark() {
		return (
			typeof window !== 'undefined' &&
			window.matchMedia?.('(prefers-color-scheme: dark)').matches
		);
	}

	const toggle = new Toggle({
		value: () => (stored.current ?? (prefersDark() ? 'dark' : 'light')) === 'dark',
		onValueChange: (isDark) => {
			stored.current = isDark ? 'dark' : 'light';
			document.documentElement.setAttribute('data-theme', isDark ? 'dark' : 'light');
		}
	});
</script>

<button {...toggle.trigger} class="theme-toggle" title="Toggle color theme" type="button">
	{#if toggle.value}
		<!-- sun (currently dark -> switch to light) -->
		<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
			<circle cx="12" cy="12" r="4.2" fill="currentColor" />
			<g stroke="currentColor" stroke-width="1.6" stroke-linecap="round">
				<line x1="12" y1="1.5" x2="12" y2="4.2" />
				<line x1="12" y1="19.8" x2="12" y2="22.5" />
				<line x1="1.5" y1="12" x2="4.2" y2="12" />
				<line x1="19.8" y1="12" x2="22.5" y2="12" />
				<line x1="4.4" y1="4.4" x2="6.3" y2="6.3" />
				<line x1="17.7" y1="17.7" x2="19.6" y2="19.6" />
				<line x1="4.4" y1="19.6" x2="6.3" y2="17.7" />
				<line x1="17.7" y1="6.3" x2="19.6" y2="4.4" />
			</g>
		</svg>
	{:else}
		<!-- moon (currently light -> switch to dark) -->
		<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
			<path
				fill="currentColor"
				d="M20.5 14.7A8.5 8.5 0 0 1 9.3 3.5a8.5 8.5 0 1 0 11.2 11.2Z"
			/>
		</svg>
	{/if}
	<span class="visually-hidden">Toggle color theme</span>
</button>

<style>
	.theme-toggle {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 2.1rem;
		height: 2.1rem;
		border-radius: 999px;
		border: 1px solid var(--color-border);
		background: var(--color-bg-raised);
		color: var(--color-text);
		cursor: pointer;
		transition:
			border-color 0.15s ease,
			color 0.15s ease,
			transform 0.25s ease;
	}

	.theme-toggle:hover {
		border-color: var(--color-accent);
		color: var(--color-accent);
		transform: rotate(-12deg) scale(1.06);
	}

	.theme-toggle svg {
		transition: transform 0.2s ease;
	}
</style>
