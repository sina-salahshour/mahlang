import { error } from '@sveltejs/kit';
import { stdDocs, getStdDoc } from '$lib/content';
import type { EntryGenerator } from './$types';

export const prerender = true;

export const entries: EntryGenerator = () => stdDocs.map((d) => ({ slug: d.slug }));

export function load({ params }) {
	const doc = getStdDoc(params.slug);
	if (!doc) error(404, `No standard library module "${params.slug}"`);
	const i = stdDocs.indexOf(doc);
	const neighbor = (d: (typeof stdDocs)[number] | undefined) =>
		d ? { slug: d.slug, title: d.meta.title } : undefined;
	return { doc, prev: neighbor(stdDocs[i - 1]), next: neighbor(stdDocs[i + 1]) };
}
