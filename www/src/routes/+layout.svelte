<script lang="ts">
	import '$lib/styles/app.css';
	import { onNavigate } from '$app/navigation';
	import Header from '$lib/components/Header.svelte';
	import Footer from '$lib/components/Footer.svelte';

	let { children } = $props();

	// View Transitions between routes (progressive enhancement: browsers
	// without support just navigate normally, no-op here). Respects
	// prefers-reduced-motion via the CSS in app.css.
	onNavigate((navigation) => {
		if (!document.startViewTransition) return;
		if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;

		return new Promise((resolve) => {
			document.startViewTransition(async () => {
				resolve();
				await navigation.complete;
			});
		});
	});
</script>

<div class="page">
	<Header />
	<main>
		{@render children()}
	</main>
	<Footer />
</div>

<style>
	.page {
		display: flex;
		flex-direction: column;
		min-height: 100vh;
	}

	main {
		flex: 1;
		/* Only the page content cross-fades between routes -- the header
		   (and its sliding nav indicator) and footer stay put. See the
		   ::view-transition-*(main-content) rules in app.css. */
		view-transition-name: main-content;
	}
</style>
