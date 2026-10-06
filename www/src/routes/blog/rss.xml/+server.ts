import { posts } from '$lib/content';

export const prerender = true;

const SITE_URL = 'https://mahlang.dev';

function escapeXml(s: string): string {
	return s
		.replace(/&/g, '&amp;')
		.replace(/</g, '&lt;')
		.replace(/>/g, '&gt;')
		.replace(/"/g, '&quot;')
		.replace(/'/g, '&apos;');
}

export function GET() {
	const items = posts
		.map((post) => {
			const url = `${SITE_URL}/blog/${post.slug}`;
			const pubDate = new Date(`${post.meta.date}T00:00:00Z`).toUTCString();
			return `		<item>
			<title>${escapeXml(post.meta.title)}</title>
			<link>${url}</link>
			<guid>${url}</guid>
			<pubDate>${pubDate}</pubDate>
			<description>${escapeXml(post.meta.description)}</description>
			${(post.meta.tags ?? []).map((t) => `<category>${escapeXml(t)}</category>`).join('\n\t\t\t')}
		</item>`;
		})
		.join('\n');

	const body = `<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
	<channel>
		<title>Mah blog</title>
		<link>${SITE_URL}/blog</link>
		<description>Updates, design notes, and changelogs for the Mah language.</description>
		<language>en-us</language>
${items}
	</channel>
</rss>
`;

	return new Response(body, {
		headers: { 'Content-Type': 'application/rss+xml; charset=utf-8' }
	});
}
