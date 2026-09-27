# mahlang.dev — the Mah website

A SvelteKit site for the [Mah language](../README.md): a landing page,
user-facing docs, and a blog (including version changelogs). Built with
Svelte 5 (runes), mdsvex for Markdown, and `@sveltejs/adapter-static` so
the whole thing builds to static HTML.

## Developing

```sh
pnpm install
pnpm run dev -- --open
```

(`npm`/`yarn` work too — there's no dependency on pnpm specifically,
though this project's own `pnpm-lock.yaml` is committed. Delete it and
`node_modules/` if you switch package managers.)

## Building

```sh
pnpm run build      # -> build/, a static site (adapter-static, everything prerendered)
pnpm run preview    # serve that build locally
pnpm run check      # svelte-check (types + Svelte diagnostics)
```

`pnpm run build` must succeed and `pnpm run check` must pass before
shipping a change here.

## Content layout

Everything under `src/content/` is plain Markdown, loaded with
`import.meta.glob` (see `src/lib/content.ts`) — **adding a new `.md` file
needs no code change**, as long as its frontmatter is right.

```
src/content/docs/*.md     one page per file, rendered at /docs/<filename-without-.md>
src/content/blog/*.md     one post per file, rendered at /blog/<filename-without-.md>
```

### Docs (`src/content/docs/*.md`)

Frontmatter:

```yaml
---
title: Traits          # shown in the sidebar and <title>
order: 6                # position within its section (lower = earlier)
section: Language        # sidebar group: "Start", "Language", or "Tooling" today
---
```

The sidebar (`src/lib/components/DocsSidebar.svelte`) groups pages by
`section` and sorts within a section by `order` — there's no separate
navigation config to keep in sync.

### Blog (`src/content/blog/*.md`)

Frontmatter:

```yaml
---
title: "v0.1.0: runnable bytecode and the Rust VM"
date: 2026-09-26           # YYYY-MM-DD; the index and RSS feed sort by this
description: "One sentence, used in listings and the <meta description>."
tags: [changelog]           # any tags; the literal tag "changelog" is special, see below
version: "0.1.0"             # only for changelog posts
---
```

**Changelog posts are blog posts.** A post tagged `changelog` (with a
`version`) shows up at `/blog` like any other post, *and* at
`/changelog` (filtered to just that tag, via
`src/lib/content.ts`'s `changelogPosts`). There's no separate changelog
content format — see `.claude/skills/update-docs/SKILL.md` for the
workflow to add one.

`/blog/rss.xml` (`src/routes/blog/rss.xml/+server.ts`) is generated from
the same post list.

## Mah syntax highlighting

`src/lib/markdown/highlight-mah.ts` is a small, dependency-free regex
tokenizer (no Shiki/highlight.js) used as mdsvex's `highlight` hook for
` ```mah ` code fences (see `vite.config.ts`). Its keyword list mirrors
`mah/compiler/lexer.py`'s `KEYWORDS`. Every other fenced language (`sh`,
`toml`, ...) just gets escaped and wrapped, no tokenizing.

If you add a new Mah keyword, update `KEYWORDS`/`CONSTANTS`/`BUILTIN_TYPES`
in that file to match `mah/compiler/lexer.py`.

**Important**: that highlighter's HTML gets spliced directly into a
Svelte component's markup by mdsvex, so it escapes `{`/`}` (as well as
the usual `<`/`>`/`&`/quotes) — Mah source is full of braces, and an
unescaped `{`/`}` there makes Svelte try to parse it as a mustache
expression and fail the build. Keep that escaping if you touch the file.

## Structure

```
src/routes/
  +page.svelte              landing page
  docs/
    +layout.svelte           sidebar + prose layout
    +page.ts                 redirects /docs -> the first doc (by `order`)
    [slug]/+page.{ts,svelte} renders one docs page; +page.ts declares
                              prerender entries from src/lib/content.ts
  blog/
    +page.svelte              post index
    [slug]/+page.{ts,svelte} renders one post
    rss.xml/+server.ts        RSS feed
  changelog/+page.svelte      changelog-tagged posts only
src/lib/
  content.ts                  loads docs/ and blog/ via import.meta.glob
  markdown/highlight-mah.ts   the Mah syntax highlighter
  components/                 Header, Footer, ThemeToggle, DocsSidebar,
                               QuickstartTabs (melt-ui builders: Dialog,
                               Toggle, Accordion, Tabs) — theme persistence
                               and small reactive bits use `runed`
  styles/app.css               CSS variables, light/dark theme, typography
```

Everything is prerendered (`export const prerender = true` in the root
`+layout.ts`); dynamic routes (`docs/[slug]`, `blog/[slug]`) declare
`entries()` from the same content list so `adapter-static` knows every
page to emit.

## Keeping the language reference in sync

This site's docs are *not* the only place the language is documented for
readers: `mah/project/templates/docs/mah-language.md` is copied into
every new Mah project by `mah init` and is the reference an LLM working
inside a Mah project reads. Updating one doesn't update the other
automatically — see `.claude/skills/update-docs/SKILL.md`, which covers
both.
