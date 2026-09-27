<script lang="ts">
	// The Mah language mark (the same icon used for the VS Code extension
	// and the .mh file icon, from editors/vscode/icons/, and as the site
	// favicon). Always the dark badge (a self-contained dark-navy square) --
	// it reads cleanly at small sizes on both the light and dark site
	// themes, which the previous light/dark-swapped version didn't.
	let { size = 32 }: { size?: number } = $props();
</script>

<img
	src="/icons/mah-lang-dark.svg"
	alt=""
	class="lang-icon"
	width={size}
	height={size}
	style="width: {size}px; height: {size}px;"
/>

<style>
	.lang-icon {
		display: inline-block;
		flex: none;
		border-radius: 22%;
	}
</style>
