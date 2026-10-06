// A tiny rehype plugin (no dependencies, like the rest of this site's
// Markdown pipeline): give every h2-h4 in a docs page or blog post an `id`,
// slugged the way GitHub does it, so `/docs/standard-library#stdfs` links
// and in-page anchors work. Duplicate slugs on one page get `-1`, `-2`, ...

type HastNode = {
	type: string;
	tagName?: string;
	value?: string;
	properties?: Record<string, unknown>;
	children?: HastNode[];
};

function textOf(node: HastNode): string {
	if (node.type === 'text') return node.value ?? '';
	return (node.children ?? []).map(textOf).join('');
}

export function slugify(text: string): string {
	return text
		.trim()
		.toLowerCase()
		.replace(/[^\p{L}\p{N}\s_-]/gu, '')
		.replace(/\s/g, '-');
}

export function headingIds() {
	return (tree: HastNode) => {
		const seen = new Map<string, number>();
		const walk = (node: HastNode) => {
			if (node.type === 'element' && /^h[2-4]$/.test(node.tagName ?? '')) {
				node.properties ??= {};
				if (!node.properties.id) {
					const base = slugify(textOf(node));
					const n = seen.get(base) ?? 0;
					seen.set(base, n + 1);
					node.properties.id = n ? `${base}-${n}` : base;
				}
			}
			node.children?.forEach(walk);
		};
		walk(tree);
	};
}
