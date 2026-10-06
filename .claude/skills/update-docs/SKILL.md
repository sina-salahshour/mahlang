---
name: update-docs
description: Use when the user asks to update the docs/website after a language feature or change, or to write a changelog/release post.
---

# Updating the Mah website (`www/`)

The website (`www/`, a SvelteKit + mdsvex static site) has its own docs
and blog, separate from — but meant to stay consistent with —
`mah/project/templates/docs/mah-language.md` (the reference `mah init`
copies into every new project). **This skill covers the website. Keep
`mah/project/templates/docs/mah-language.md` in sync too** — that's
`.claude/skills/mah-add-feature/SKILL.md` step 9's job, not this skill's,
but do both for a real language change. `www/README.md` has the fuller
structural writeup this skill summarizes.

## Layout

```
www/src/content/docs/*.md    one page per file -> /docs/<filename>
www/src/content/std/*.md     one page per std module -> /std/<module>
www/src/content/blog/*.md    one post per file -> /blog/<filename>, and
                              also /changelog if tagged `changelog`
```

Both are loaded via `import.meta.glob` (`www/src/lib/content.ts`) —
**dropping in a new `.md` file is enough, no code change needed.** The
docs sidebar groups by frontmatter `section` and sorts by `order`; there
is no separate nav config to edit.

## Updating a docs page for a language change

1. Find the right existing page under `www/src/content/docs/` — one per
   topic (`structs.md`, `traits.md`, `iterators-ranges.md`, `async.md`,
   `types.md`, `projects.md`, `formatter.md`, `tooling.md`, ...). Most
   changes extend an existing page rather than needing a new one. Add a
   new page only for a genuinely new topic area, with frontmatter:

   ```yaml
   ---
   title: My New Topic
   order: 16   # next unused number in its section
   section: Language   # "Start" | "Language" | "Tooling" today
   ---
   ```

2. Write the addition the same way the existing pages read: short prose,
   then a ```` ```mah ```` fenced example. Base examples on
   `examples/*.mh` or `mah/project/templates/docs/mah-language.md` —
   don't invent syntax.

3. **Verify every Mah code sample actually compiles/runs** before
   committing it:

   ```sh
   python3 -m mah build /tmp/check.mh     # or:
   python3 -m mah run /tmp/check.mh
   ```

   Paste the snippet into a scratch `.mh` file and run one of the above.
   A snippet that only *looks* right is not good enough — this is the
   same bar `docs/mah-language.md` code blocks are held to (see
   `docs/FORMAT.md`'s note that every template doc code block is tested).

4. If the change affects Mah's keywords (a new keyword, or a builtin
   name), update `www/src/lib/markdown/highlight-mah.ts`'s `KEYWORDS`/
   `CONSTANTS`/`BUILTIN_TYPES` sets to match `mah/compiler/lexer.py`'s
   `KEYWORDS`, so the new syntax highlights correctly in fenced ```mah
   blocks.

5. Build and check before considering it done:

   ```sh
   cd www && pnpm run build && pnpm run check
   ```

## Updating the standard library reference

Each `std:` module has a page in `www/src/content/std/<module>.md`
(frontmatter: `title: std:<module>`, `order`, `section`, and a one-line
`summary` for the `/std` index). A page is a guide (what the module is for,
then common tasks with examples), then a **Reference** section listing
every exported function and type. When a module gains, loses or changes
an export (check `mah/std/<module>.mh`'s `export`s), update its Reference
tables and add an example if the change is notable. A brand-new module
gets a new page; `docs/standard-library.md` (the short tour) gets a
section linking to it.

Verify the examples on **both** runtimes: `python3 -m mah run x.mh`, and
`python3 -m mah build x.mh -o x.mahc && runtime/target/release/mah-vm run x.mahc`
(`make vm` first). Examples that need the internet go inside a `fn` that
isn't called, so they still compile-check. `std:test` examples are test
files: check them with `mah test` in a scratch project.

## Adding a blog post

Create `www/src/content/blog/<slug>.md`:

```yaml
---
title: "A human-readable title"
date: 2026-10-01              # YYYY-MM-DD, unquoted is fine
description: "One sentence, used in listings and <meta description>."
tags: [design]                  # any tags; "changelog" is special, see below
---
```

Then the post body as plain Markdown (``` ```mah ``` fences work the
same as in docs pages, and are verified the same way — see step 3
above). It appears at `/blog/<slug>` and in the `/blog` index (sorted by
`date`, newest first) automatically — no other file to touch.

## Adding or extending a changelog entry

A changelog entry **is a blog post** — there's no separate format.
Tag it `changelog` and give it a `version`:

```yaml
---
title: "vX.Y.Z: short summary of what changed"
date: 2026-10-01
description: "One sentence summarizing the release."
tags: [changelog]
version: "X.Y.Z"     # quoted, so "0.10.0" doesn't become a float
---
```

It will then show up at `/blog` (like any post) *and* at `/changelog`
(filtered to `tags: [changelog]`, sorted by `date`) — both read from
`www/src/lib/content.ts`'s `posts`/`changelogPosts`, so nothing else
needs updating.

**Which version does a change belong to?**

- If there's an unreleased/upcoming version's changelog post already
  (check `www/src/content/blog/` for the highest `version` tagged
  `changelog` and compare to the version strings in
  `mah/lsp/server.py`'s `SERVER_VERSION`, `mah/project/templates/mah-project.toml`,
  and `runtime/Cargo.toml` — they move together), **add a section to
  that existing post** rather than creating a new one for every single
  change.
- Only create a new changelog post when you're actually cutting a new
  version (those version strings above are being bumped together as
  part of this change). Follow the existing posts' style: a short intro
  paragraph, then one `##` subsection per notable change with a real,
  verified ```mah example where it helps.
- Keep the writing style consistent with the existing changelog posts in
  `www/src/content/blog/v0-*.md` — they're written as short, dense
  release notes with runnable examples, not marketing copy.

## After any of the above

```sh
cd www
pnpm run build   # must succeed -- prerenders every page, including new ones
pnpm run check   # svelte-check must pass
```

Then remind whoever's reviewing (or yourself, if you're about to call
the change done) that `mah/project/templates/docs/mah-language.md`
needs the same update if this was a language change — that file is the
authoritative reference `mah init` ships into new projects, and it
drifting from the website is exactly the kind of inconsistency this
skill exists to prevent.
