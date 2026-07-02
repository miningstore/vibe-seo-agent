# 03 - Customizing for your site

The Google connection layer is universal. Everything else needs a few
choices that depend on your stack.

## What you'll edit

| File | What |
|---|---|
| `.env` | site URL, GA4 property, CF credentials |
| `seo_agent/site_config.py` | SLOTS, HOUSE_STYLE_BANNED, PAGE_MATCH for YOUR project (overlay — see below) |
| `seo_agent/prompts/variant_generator.md` | rules the LLM follows when proposing variants |
| `seo_agent/prompts/seo_best_practices.md` | your SEO rubric (mine from your audits / Ahrefs / etc.) |
| `<your-site>/middleware.*` | hook the variant lookup into render (see Astro example) |
| `<your-site>/migrations/*.sql` | add the 4 seo_* tables to your DB |

## The overlay pattern (don't fight template updates)

DON'T edit `seo_agent/config.py` directly. That file ships with the
template and you'll lose your edits the next time you `git pull`.

INSTEAD, create `seo_agent/site_config.py` from the example:

```bash
cp seo_agent/site_config.example.py seo_agent/site_config.py
$EDITOR seo_agent/site_config.py
```

`site_config.py` is gitignored. Whatever module-level constants you
define there (`SLOTS`, `HOUSE_STYLE_BANNED`, `PAGE_MATCH`) override the
defaults from the template's `config.py`. You can pull template
updates from upstream without merging your per-project knobs.

`PAGE_MATCH` is a dict mapping slot name to a page-match predicate.
Useful when one slot targets one specific URL (common on static sites
where you're optimizing a known page):

```python
PAGE_MATCH = {
    "home.title":       {"path": "/"},
    "pricing.title":    {"path": "/pricing/"},
    "blog_post.title":  {"path_prefix": "/blog/"},   # if your middleware supports prefix matching
}
```

## Template placeholders (the cross-page trick)

A slot's `page_match` predicate determines which pages a variant
applies to:

- `page_match={}`  → the variant applies to **every page** matching
  the slot's `pattern` (e.g. every city listing page).
- `page_match={"city":"austin-tx"}` → the variant applies to ONE city.

For `page_match={}` slots, you need **template placeholders** like
`{city}` or `{unit_count}` in the variant text — otherwise the same
literal string renders on every city's page, which is bad for SEO.

Whitelist the allowed placeholders in `template_vars`:

```python
Slot(
    name="apartments_listing.h1",
    pattern="apartments_listing",
    kind="text",
    enabled=True,
    description="H1 on /city/{city}/apartments/. Variants may use {city}, "
                "{property_count}, {unit_count}.",
    min_len=15, max_len=80,
    banned_tokens=HOUSE_STYLE_BANNED,
    template_vars=("city", "property_count", "unit_count"),
)
```

Then in your page template:

```astro
{pickSlotText(
  Astro.locals.seoVariants,
  'apartments_listing.h1',
  `${cityDisplay} Apartments`,  // fallback if no variant
  'text',
  { city: cityDisplay, property_count: String(n), unit_count: String(units) },
)}
```

The validator allows the Claude-generated text to use any
whitelisted placeholder; the helper substitutes them at render time.
**Unknown placeholders are rejected** — if Claude proposes
`{foo}` and `foo` isn't in `template_vars`, the variant is rejected
with a clear error message in the agent's log.

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


## SERP-visible slots: sequential evaluation, not parallel A/B

`<title>` and meta-description slots need special handling, and the
agent now enforces it. Two facts make the parallel bandit meaningless
for them:

1. **Googlebot always renders the champion.** The snippet searchers see
   never varies with the on-site assignment split, so a parallel test
   can't observe SERP CTR differences between arms.
2. **A meta description renders nowhere on-page** (and a title only in
   the browser tab), so the on-site engagement "reward" for those arms
   is statistical noise. Left alone, the bandit will promote *something*
   from that noise -- in the reference deployment it crowned
   paywall-scented copy that held search CTR near 0.1% at page-one
   positions.

Mark such slots `serp_visible=True`. The loop then routes their
kill/promote decisions to `seo_agent/serp_evaluator.py`, which runs a
**sequential champion-tenure test**:

- Each arm holds the champion seat for `SEO_SERP_TENURE_DAYS`
  (default 14).
- A tenure is scored from the GSC API directly:
  `adjusted_ctr = ctr / expected_ctr(avg_position)` over the slot's
  page family, so position drift between tenures doesn't masquerade as
  snippet quality.
- The static template fallback participates as a first-class arm
  (a tenure with `variant_id NULL` measures it).
- After every arm has been measured, the best conclusive arm is
  crowned; decisively worse arms (below `SEO_SERP_LOSER_KILL_RATIO`
  of the best, default 0.8) are killed.

Setup checklist:

1. Set `serp_visible=True` on the slot (title/meta only; H1s stay on
   the parallel bandit -- they render on-page, so engagement
   measurement is causally valid there).
2. Add the slot's pattern to `PATTERN_PATH_REGEX` in
   `site_config.py` (e.g. `{"category": r"^/c/[^/]+/$"}`).
3. Add `SERP_WITHHOLD_BANNED` to the slot's `banned_tokens` so the
   generator can't propose paywall-scented snippet copy at all.
4. Apply `examples/astro-cloudflare/migrations/0004_seo_serp_tenures.sql`
   (or let the evaluator create the table on its first `--commit` run).

Operate / inspect:

```bash
python -m seo_agent.serp_evaluator            # dry-run all serp slots
python -m seo_agent.serp_evaluator --commit   # apply (loop does this automatically)
```

Expect one full rotation to take `arms x SEO_SERP_TENURE_DAYS` days.
That is slow by design: one tenure is one *causal* read of what Google
actually does with that snippet, which noisy parallel data can never
give you. Keep the arm pool small for SERP slots (3-5 well-motivated
candidates beat 8 spaghetti arms).
