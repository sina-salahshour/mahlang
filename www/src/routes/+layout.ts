// Everything on this site is static content known at build time, so
// prerender the whole app for the static adapter (see svelte config in
// vite.config.ts). Dynamic routes (docs/blog slugs) declare their own
// `entries()` in their +page.ts.
export const prerender = true;
