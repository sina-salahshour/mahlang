import { error } from '@sveltejs/kit';
import { docs, getDoc } from '$lib/content';
import type { EntryGenerator } from './$types';

export const prerender = true;

export const entries: EntryGenerator = () => docs.map((d) => ({ slug: d.slug }));

export function load({ params }) {
	const doc = getDoc(params.slug);
	if (!doc) error(404, `No doc page for "${params.slug}"`);
	return { doc };
}
