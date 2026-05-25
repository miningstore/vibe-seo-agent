# 03 - Customizing for your site

The Google connection layer is universal. Everything else needs a few
choices that depend on your stack.

## What you'll edit

| File | What |
|---|---|
| `.env` | site URL, GA4 property, CF credentials |
| `seo_agent/config.py` | SLOTS (what copy to vary), HOUSE_STYLE_BANNED |
| `seo_agent/prompts/variant_generator.md` | rules the LLM follows when proposing variants |
| `seo_agent/prompts/seo_best_practices.md` | your SEO rubric (mine from your audits / Ahrefs / etc.) |
| `<your-site>/middleware.*` | hook the variant lookup into render (see Astro example) |
| `<your-site>/migrations/*.sql` | add the 4 seo_* tables to your DB |

## Choosing slots

A **slot** is one copy surface you want the agent to vary on one
page-pattern. Examples:

- `home.title` — the homepage's `<title>` tag
- `category.h1` — the H1 on every category page
- `product.meta_description` — meta description on detail pages

Best practice: start with `home.h1` enabled, the rest disabled. H1 is
below the SERP fold so weak variants can't tank rank. Once you see
real telemetry flowing, flip `home.meta_description` (drives SERP CTR,
medium risk), then `home.title` (highest leverage, highest risk).

Edit `seo_agent/config.py` → the `SLOTS = [...]` list. The structure:

```python
Slot(
    name="home.title",                # unique
    pattern="home",                   # which pages it applies to
    kind="text",                      # text | faq | schema
    enabled=False,                    # gate
    description="HTML <title> on /",
    min_len=30, max_len=60,           # validator enforces
    banned_tokens=HOUSE_STYLE_BANNED, # competitor names, em-dashes, etc.
)
```

## Banned tokens

`HOUSE_STYLE_BANNED` is the deny-list for proposed variants. Common
additions:

- Competitor brand names you don't want to rank for
- Phrases that imply data-source mechanics (`scraped`, `crawled`,
  `vendor names`)
- Em/en dashes (look LLM-y in copy)
- Regulatory red flags (`guaranteed`, `safe investment`, etc.)
- Internal jargon a customer wouldn't recognize

## Plugging into your site

The agent stores variants in D1; your site reads them per-request and
swaps copy. Reference implementation lives at `examples/astro-cloudflare/`:

```
examples/astro-cloudflare/
├── middleware/
│   └── seo-variants.ts       # cookie assignment + variant lookup
├── migrations/
│   └── 0001_seo_experiments.sql
└── lib/
    └── seo-variants.ts       # template helper: pickSlotText(locals, slot, fallback)
```

### For Astro on Cloudflare Pages (recommended)

```ts
// site/src/middleware.ts
import { seoVariantMiddleware } from './middleware/seo-variants';
export const onRequest = seoVariantMiddleware;
```

```astro
---
// site/src/pages/index.astro
import { pickSlotText } from './lib/seo-variants';
const title = pickSlotText(Astro.locals, 'home.title', 'Your default title');
const h1    = pickSlotText(Astro.locals, 'home.h1',    'Your default H1');
---
<title>{title}</title>
<h1>{h1}</h1>
```

The middleware:
1. Reads the `_arx` cookie (mints one for new visitors, skips bots).
2. Looks up active variants for matching slots from D1 (60s cache).
3. Thompson-samples a variant per slot, writes assignment to D1
   fire-and-forget.
4. Populates `Astro.locals.seoVariants` for the page template.

Crawlers (Googlebot, etc.) always see the current `champion` variant.

### For other frameworks

The pattern is identical, just rewrite the middleware in your stack:

- **Next.js (App Router)**: middleware.ts at the project root,
  variants on a cookie, swap copy in server components.
- **Remix**: loader-side variant lookup; pass via loader data.
- **Plain HTML on a CDN**: less natural — the bandit needs server-side
  variant assignment, which static hosting doesn't have. Consider
  Cloudflare Workers as a thin edge proxy.

### If you're not on Cloudflare

D1 isn't required — it's just what apartment-pricer uses. Swap
`seo_agent/d1_client.py` for a Postgres/SQLite/whatever client.
The schema in `examples/astro-cloudflare/migrations/0001_seo_experiments.sql`
is plain SQL with no D1-isms; should drop straight into Postgres
with minor type tweaks.

## Customizing variant generation

`seo_agent/prompts/variant_generator.md` is the master prompt the
Claude CLI session sees when proposing variants. Edit it to:

- Inject your brand voice / style guide
- Reference your competitive positioning
- Add no-go words / regulatory constraints
- Bias toward specific keyword themes from your SEO research

`seo_agent/prompts/seo_best_practices.md` is the SEO rubric the LLM
scores against. Default content is mined from public SEO best
practices. Replace with your own audit findings for stronger results.

## Adapting the eval signal

`seo_agent/loop.py` evaluates variants on a composite of GSC rank/CTR
+ GA4 engagement + D1 events. If you don't have on-site engagement
tracking yet, set the GA4 / D1 weights to 0 in `_score_variant()` and
rely on GSC alone. Slower (7-day eval window) but honest.

## Multiple sites under one agent

You can run one VPS, one OAuth token, and N agents — one per site.
Recommended layout: separate `.env` per site, separate systemd unit
per site, all reading from the same `credentials/` directory. The
OAuth user can see all GSC properties they own, and the GA4 properties
they're Editor on, with the single shared token.
