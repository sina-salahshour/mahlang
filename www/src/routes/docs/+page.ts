import { redirect } from '@sveltejs/kit';
import { docs } from '$lib/content';

export const prerender = true;

export function load() {
	// /docs lands on the first doc page (by `order`), e.g. Getting started.
	redirect(307, `/docs/${docs[0].slug}`);
}
