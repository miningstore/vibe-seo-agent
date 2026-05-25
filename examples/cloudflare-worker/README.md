# Cloudflare Worker — static-site A/B reference

When your site is **static** (Astro `output: 'static'`, Next.js export,
Jekyll, plain HTML on CF Pages, etc.), you can't run middleware
per-request to swap variants. Instead, put a Worker in front of your
Pages origin. The Worker uses Cloudflare's `HTMLRewriter` API to stream-
transform the static HTML, swapping `<title>` / `<meta>` / `<h1>` based
on D1-stored variants. Sub-ms added latency. No site rebuild.

When to use this vs the Astro/SSR middleware:

| If your site is... | Use |
|---|---|
| Astro hybrid SSR / SSR | `examples/astro-cloudflare/` middleware (simpler) |
| Astro `output: 'static'` / any static build | This Worker (don't touch the build) |
| Next.js static export | This Worker |
| Plain HTML on CF Pages | This Worker |
| Not on Cloudflare | Use the Astro middleware pattern, translated to your stack |

## What's here

| File | Purpose |
|---|---|
| `src/index.ts` | The Worker. Reads `_arx` cookie, fetches origin, HTMLRewriter swaps `<title>`/`<meta>`/`<h1>`, persists assignment to D1 fire-and-forget. |
| `wrangler.toml` | Config template — set `ORIGIN`, `database_id`, and routes. |
| `package.json` | `wrangler deploy` etc. |

## Setup

```bash
# 1. Apply the D1 migration to the database for THIS site.
#    Use the same schema as examples/astro-cloudflare/migrations/0001_seo_experiments.sql
#    Create a fresh DB if you don't have one:
npx wrangler d1 create yoursite-seo-db
# Save the printed database_id into wrangler.toml.

# 2. Set ORIGIN to your Pages deployment hostname.
#    Find it via: `wrangler pages deployment list --project-name yoursite`.
#    Use the *.pages.dev hostname, NOT your apex domain (would loop).
$EDITOR wrangler.toml   # set ORIGIN = "yoursite-pages-xxx.pages.dev"

# 3. Deploy WITHOUT a route binding first — so it's testable but not live.
npm install
npx wrangler deploy

# 4. Test against the Worker's *.workers.dev URL. Variant lookup will
#    return empty (no variants yet in D1), Worker should transparently
#    proxy your static site.
curl -I https://vibe-seo-worker.<your-subdomain>.workers.dev/

# 5. Once you have a few variants in seo_variants (the agent on your
#    VPS will be inserting them), test that the rewriter swaps. Insert
#    a test variant via wrangler d1 execute:
npx wrangler d1 execute yoursite-seo-db --remote --command "
  INSERT INTO seo_variants (slot, page_match, treatment, status, created_at, created_by, hypothesis, alpha, beta)
  VALUES ('home.title', '{\"path\":\"/\"}', '{\"text\":\"TEST VARIANT TITLE\"}', 'active', datetime('now'), 'manual', 'smoke test', 1.0, 1.0)
"

# 6. Hit the Worker URL again. View source — <title>TEST VARIANT TITLE</title>
#    should appear instead of your static title.

# 7. When confident, add the route in wrangler.toml:
#      [[routes]]
#      pattern = "yoursite.com/*"
#      zone_name = "yoursite.com"
#    Then redeploy.

# 8. Verify the production domain still works and shows variants.
#    Verify Googlebot sees the static fallback (no cloaking):
curl -sA "Googlebot/2.1" https://yoursite.com/ | grep -E '<title>|<h1'
```

## Bot safety

Bots (UA matches `BOT_UA` regex in `src/index.ts`) see the **original
HTML** — the Worker passes through the literal static content without
rewriting. Once you have a `champion` variant (status='champion'),
bots see the champion consistently from then on. Never the
"first active" — that's the cloaking-risk antipattern.

## Killswitch

Set `SEO_OPTIMIZER_ENABLED=false` in the Worker env to disable variant
assignment globally — the Worker becomes a transparent proxy. Useful
during a launch / debugging.

```bash
npx wrangler secret put SEO_OPTIMIZER_ENABLED
# enter "false"
```

(Or update `wrangler.toml [vars]` and redeploy.)

## Performance

`HTMLRewriter` is a Cloudflare-native streaming HTML parser. It
processes bytes as they arrive from the origin and emits to the
client incrementally. Typical added latency: < 5ms for typical pages.
The D1 lookup is the slowest part — first hit ~50-100ms; cached
subsequent requests near-zero (D1 has its own internal cache).
Wrap the D1 query in `caches.default` if you need to optimize
further.

## What it doesn't do (yet)

- **Engagement events** (`scroll_50`, `dwell_30s`, etc): not collected
  from the Worker. You need an in-page tracker script that POSTs to
  an event-ingest endpoint (see `examples/astro-cloudflare/` for the
  shape). Static sites typically pull in a tiny inline `<script>` from
  the worker via HTMLRewriter — extend `src/index.ts` if you want this.
- **GA4 reconciliation**: handled by the optimizer's `ga4_client.py`
  on the VPS, not the Worker.
