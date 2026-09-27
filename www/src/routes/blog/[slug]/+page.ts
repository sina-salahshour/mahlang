import { error } from '@sveltejs/kit';
import { posts, getPost } from '$lib/content';
import type { EntryGenerator } from './$types';

export const prerender = true;

export const entries: EntryGenerator = () => posts.map((p) => ({ slug: p.slug }));

export function load({ params }) {
	const post = getPost(params.slug);
	if (!post) error(404, `No blog post for "${params.slug}"`);
	return { post };
}
