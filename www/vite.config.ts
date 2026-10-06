import { mdsvex } from 'mdsvex';
import adapter from '@sveltejs/adapter-vercel';
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';
import { highlighter } from './src/lib/markdown/highlight-mah.ts';
import { headingIds } from './src/lib/markdown/heading-ids.ts';

export default defineConfig({
	plugins: [
		sveltekit({
			compilerOptions: {
				// Force runes mode for the project, except for libraries. Can be removed in svelte 6.
				runes: ({ filename }) => filename.split(/[/\\]/).includes('node_modules') ? undefined : true
			},
			adapter: adapter({
				split: true
			}),
			preprocess: [
				mdsvex({
					extensions: ['.svx', '.md'],
					highlight: { highlighter },
					rehypePlugins: [headingIds]
				})
			],
			extensions: ['.svelte', '.svx', '.md']
		})
	]
});
