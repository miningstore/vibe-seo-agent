# Astro + Cloudflare Pages reference integration

How average-rent.com hooks the agent into a Cloudflare-hosted Astro
site. Adapt for your stack.

## What's here

| File | What |
|---|---|
| `migrations/0001_seo_experiments.sql` | The 4 D1 tables. Apply via `wrangler d1 migrations apply <db> --remote`. |
| `middleware.ts` | Reads `_arx` cookie, samples variants, populates `Astro.locals.seoVariants`. |
| `lib-seo-variants.ts` | Helper used by middleware + page templates. `pickSlotText()` for reads. |

## Wiring

```bash
# Drop the migration into your site
cp migrations/0001_seo_experiments.sql ../../your-site/migrations/
cd ../../your-site
wrangler d1 migrations apply your-db --remote

# Drop the middleware + helper
cp middleware.ts your-site/src/middleware.ts
mkdir -p your-site/src/lib
cp lib-seo-variants.ts your-site/src/lib/seo-variants.ts
```

Then in any page template:

```astro
---
import { pickSlotText } from '../lib/seo-variants';
const title = pickSlotText(Astro.locals, 'home.title', 'Default title');
---
<title>{title}</title>
```

## Adapting `PAGE_PATTERNS`

In `lib-seo-variants.ts`, edit the `PAGE_PATTERNS` array. Each entry
maps a path regex to a slot prefix the templates read. Example for a
generic blog:

```ts
const PAGE_PATTERNS: Pattern[] = [
  { name: 'home',     regex: /^\/$/,                slotPrefix: 'home' },
  { name: 'category', regex: /^\/category\/[^/]+\/?$/, slotPrefix: 'category' },
  { name: 'post',     regex: /^\/blog\/[^/]+\/?$/,    slotPrefix: 'post' },
];
```

Then in `seo_agent/config.py`, define slots with matching `pattern`:

```python
SLOTS = [
    Slot(name="home.title",     pattern="home",     ...),
    Slot(name="category.title", pattern="category", ...),
    Slot(name="post.h1",        pattern="post",     ...),
]
```
