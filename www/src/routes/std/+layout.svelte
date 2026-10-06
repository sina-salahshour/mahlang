<script lang="ts">
	import SidebarLayout from '$lib/components/SidebarLayout.svelte';
	import { stdSections } from '$lib/content';

	let { children } = $props();

	const sections = stdSections();
</script>

<SidebarLayout {sections} base="/std" label="Stdlib">
	{@render children()}
</SidebarLayout>
