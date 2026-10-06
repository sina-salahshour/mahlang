<script lang="ts">
	import SidebarLayout from '$lib/components/SidebarLayout.svelte';
	import { docSections } from '$lib/content';

	let { children } = $props();

	const sections = docSections();
</script>

<SidebarLayout {sections} base="/docs" label="Docs">
	{@render children()}
</SidebarLayout>
