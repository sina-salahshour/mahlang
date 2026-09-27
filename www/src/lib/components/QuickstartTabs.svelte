<script lang="ts">
	import { Tabs } from 'melt/builders';

	type Tab = { id: string; label: string; lines: string[] };

	const items: Tab[] = [
		{
			id: 'project',
			label: 'New project',
			lines: [
				'mah init my-app        # scaffolds mah-project.toml, src/main.mh, docs/',
				'cd my-app',
				'mah run                 # runs the entry point from mah-project.toml',
				'mah build                # writes every [[target]], e.g. build/my-app.mahc'
			]
		},
		{
			id: 'file',
			label: 'Run a file',
			lines: [
				'mah run ./examples/prime_numbers.mh   # compile and run directly',
				'mah build ./examples/structs.mh       # -> structs.mahc',
				'mah runc ./examples/structs.mahc      # run compiled bytecode',
				'mah format ./examples                  # rewrite .mh files in the standard layout'
			]
		},
		{
			id: 'vm',
			label: 'Rust VM',
			lines: [
				'make vm                                       # build mah-vm (needs cargo)',
				'mah run --vm rust ./examples/structs.mh       # run on the native runtime',
				'mah build --self-contained ./examples/structs.mh -o structs',
				'./structs                                      # runs with no mah install needed'
			]
		}
	];

	const tabs = new Tabs<string>({ value: items[0].id });
</script>

<div class="quickstart-tabs">
	<div {...tabs.triggerList} class="tab-list">
		{#each items as item (item.id)}
			<button {...tabs.getTrigger(item.id)} class="tab-trigger">{item.label}</button>
		{/each}
	</div>
	{#each items as item (item.id)}
		<div {...tabs.getContent(item.id)} class="tab-panel">
			<pre class="mah-code language-sh"><code>{item.lines.join('\n')}</code></pre>
		</div>
	{/each}
</div>

<style>
	.quickstart-tabs {
		border: 1px solid var(--color-border);
		border-radius: var(--radius);
		overflow: hidden;
		background: var(--color-bg);
	}

	.tab-list {
		display: flex;
		gap: 0.25rem;
		padding: 0.4rem;
		background: var(--color-bg-raised);
		border-bottom: 1px solid var(--color-border);
	}

	.tab-trigger {
		border: none;
		background: transparent;
		color: var(--color-text-muted);
		padding: 0.45rem 0.9rem;
		border-radius: 6px;
		font-size: 0.88rem;
		font-weight: 600;
		cursor: pointer;
	}

	.tab-trigger:hover {
		color: var(--color-text);
	}

	.tab-trigger[data-active] {
		background: var(--color-bg);
		color: var(--color-accent);
		box-shadow: 0 0 0 1px var(--color-border);
	}

	.tab-panel .mah-code {
		margin: 0;
		border: none;
		border-radius: 0;
	}
</style>
