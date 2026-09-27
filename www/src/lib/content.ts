// Loads Markdown content (docs + blog) via `import.meta.glob`, so dropping a
// new `.md` file into src/content/docs/ or src/content/blog/ shows up on the
// site with no code change (see .claude/skills/update-docs/SKILL.md).
//
// Each `.md` file is compiled by mdsvex into a Svelte component; the module
// also exports `metadata`, the file's frontmatter.

import type { Component } from 'svelte';

export interface DocMeta {
	title: string;
	order: number;
	section: string;
}

export interface DocEntry {
	slug: string;
	meta: DocMeta;
	component: Component;
}

export interface PostMeta {
	title: string;
	date: string;
	description: string;
	tags: string[];
	version?: string;
}

export interface PostEntry {
	slug: string;
	meta: PostMeta;
	component: Component;
}

type MdModule = { default: Component; metadata: Record<string, unknown> };

function slugFromPath(path: string): string {
	const file = path.split('/').pop() ?? path;
	return file.replace(/\.md$/, '');
}

// mdsvex's frontmatter parser (js-yaml under the hood) turns an unquoted
// `date: 2026-09-26` into a real JS `Date` at build time, not a string --
// normalize it back to a plain "YYYY-MM-DD" string so every consumer
// (formatting, sorting, the RSS feed) can treat `date` as a string.
function normalizeDate(value: unknown): string {
	if (value instanceof Date) return value.toISOString().slice(0, 10);
	if (typeof value === 'string') return value.slice(0, 10);
	return String(value);
}

const docModules = import.meta.glob<MdModule>('/src/content/docs/*.md', { eager: true });

export const docs: DocEntry[] = Object.entries(docModules)
	.map(([path, mod]) => ({
		slug: slugFromPath(path),
		meta: mod.metadata as unknown as DocMeta,
		component: mod.default
	}))
	.sort((a, b) => a.meta.order - b.meta.order);

export function getDoc(slug: string): DocEntry | undefined {
	return docs.find((d) => d.slug === slug);
}

export interface DocSection {
	name: string;
	docs: DocEntry[];
}

/** Docs grouped by `section`, in first-appearance order (which follows `order`). */
export function docSections(): DocSection[] {
	const sections: DocSection[] = [];
	const bySectionName = new Map<string, DocSection>();
	for (const doc of docs) {
		let section = bySectionName.get(doc.meta.section);
		if (!section) {
			section = { name: doc.meta.section, docs: [] };
			bySectionName.set(doc.meta.section, section);
			sections.push(section);
		}
		section.docs.push(doc);
	}
	return sections;
}

const postModules = import.meta.glob<MdModule>('/src/content/blog/*.md', { eager: true });

export const posts: PostEntry[] = Object.entries(postModules)
	.map(([path, mod]) => {
		const meta = mod.metadata as unknown as PostMeta;
		return {
			slug: slugFromPath(path),
			meta: { ...meta, date: normalizeDate(meta.date) },
			component: mod.default
		};
	})
	.sort((a, b) => (a.meta.date < b.meta.date ? 1 : a.meta.date > b.meta.date ? -1 : 0));

export function getPost(slug: string): PostEntry | undefined {
	return posts.find((p) => p.slug === slug);
}

export const changelogPosts: PostEntry[] = posts.filter((p) => p.meta.tags?.includes('changelog'));
