/*
 * Reference implementation — adapt the PAGE_PATTERNS array below for
 * your site's URL structure. The middleware shape (cookie minting,
 * bot detection, Thompson sampling, D1 lookup, fire-and-forget
 * assignment write, fallback for bots when no champion exists) is
 * generic and reusable as-is.
 *
 * Copy this into your Astro site's `src/middleware.ts` (and the
 * helper into `src/lib/seo-variants.ts`), then `pickSlotText` from
 * your page templates.
 *
 * Bot safety: when no champion has been promoted yet, bots see the
 * literal fallback string from the template (NOT the "first active"
 * variant). This avoids Google flagging cloaking when variants get
 * added/killed across crawls. Once a champion is named, bots see it
 * consistently from then on.
 *
 * Originally extracted from average-rent.com — see project history.
 */

/**
 * Edge-time variant resolution for the SEO optimizer.
 *
 * Read by middleware.ts before each render of an experiment-bearing
 * page. Looks up the active variants for the current page, samples one
 * per slot via Thompson sampling, and (for real visitors) writes the
 * assignment to D1 fire-and-forget.
 *
 * The variant store is cached in `caches.default` for 60s with a path-
 * scoped cache key, so the D1 read fires at most once per minute per
 * page bucket. Sampling is deterministic by (session_id, slot) so the
 * same visitor sees the same variant for the life of the cookie.
 *
 * Bots (Googlebot etc.) are passed `isBot=true` by the caller and are
 * always handed the current champion variant — never under-tested arms.
 * The optimizer's promotion path keeps a slot's champion stable until
 * the next decisive winner, so crawled HTML for a given path stays
 * deterministic.
 */

type D1Database = import('@cloudflare/workers-types').D1Database;

// Matches common crawler / non-browser-client tokens as substrings (NOT
// word-bounded) because strings like `Googlebot/2.1` have no word boundary
// between `e` and `b`. Single source of truth for UA-based bot detection —
// import it everywhere (middleware + events endpoint) so the assignment side
// and the outcome side can't drift apart. The CF bot-score gate in the
// middleware covers the harder case of scrapers that spoof a browser UA.
//
// AI/AEO crawlers are included even when their UA carries no bot/spider/crawler
// token (meta-externalagent, ChatGPT-User, OAI-SearchBot, PerplexityBot, ...).
// Matching them matters for two reasons: (1) they must see the CHAMPION, not an
// under-tested variant — otherwise your AI-answer snippet is built from noise;
// (2) they must be excluded from the bandit test pool, or their impressions
// pollute the Beta posteriors and skew which variant "wins". If a downstream
// integration also blocks/bot-gates on this regex, matching here is what keeps
// those crawlers from being wrongly blocked (SEO/AEO safety).
export const BOT_UA = /(bot|spider|crawler|slurp|bingpreview|duckduckbot|baiduspider|yandex|sogou|exabot|facebot|ia_archiver|ahrefs|semrush|petalbot|applebot|mj12|dotbot|rogerbot|headlesschrome|python-requests|httpclient|curl|wget|go-http-client|node-fetch|axios|okhttp|libwww|scrapy|externalagent|chatgpt-user|oai-searchbot|perplexitybot|claudebot|bytespider|amazonbot|google-extended|cohere-ai|timpibot)/i;

// Variants are tied to "page patterns" — coarse families like 'city_index'
// or 'apartment'. Each pattern maps to a path regex and a slot prefix the
// templates know to read.
type Pattern = {
  name: string;
  regex: RegExp;
  matchCity?: (path: string) => string | null;
  matchSlug?: (path: string) => string | null;
};

const PATTERNS: Pattern[] = [
  // /city/austin-tx/apartments/  → listing
  {
    name: 'apartments_listing',
    regex: /^\/city\/([^/]+)\/apartments\/?$/,
    matchCity: (p) => p.match(/^\/city\/([^/]+)\/apartments\/?$/)?.[1] ?? null,
  },
  // /city/austin-tx/some-slug/   → apartment detail
  // (must come before city_index to avoid matching '/city/x/' as a slug)
  {
    name: 'apartment',
    regex: /^\/city\/([^/]+)\/([^/]+)\/?$/,
    matchCity: (p) => p.match(/^\/city\/([^/]+)\/([^/]+)\/?$/)?.[1] ?? null,
    matchSlug: (p) => p.match(/^\/city\/([^/]+)\/([^/]+)\/?$/)?.[2] ?? null,
  },
  // /city/austin-tx/             → city landing
  {
    name: 'city_index',
    regex: /^\/city\/([^/]+)\/?$/,
    matchCity: (p) => p.match(/^\/city\/([^/]+)\/?$/)?.[1] ?? null,
  },
];

// Excluded sub-paths under /city/*/ that are NOT under experiment control.
// /city/$city/trends/, /city/$city/floor-plans/, etc. all live alongside
// the experiment-bearing pages but are not in any pattern. The pattern
// regexes above only match exact known shapes so these are skipped
// automatically — listed here so a future reader knows it's intentional.
// (No code needed; documentation only.)

export function isExperimentPath(path: string): boolean {
  return matchPattern(path) !== null;
}

/**
 * Page-template helper. Read a slot's treatment from `locals.seoVariants`
 * with a fallback. The treatment is a free-form JSON value owned by the
 * optimizer; the `field` arg picks one key out of it. For simple string
 * slots (title, meta_description, h1) the treatment is just `{ text: "..." }`
 * and the template passes `field='text'`. For schema slots the treatment
 * is the full JSON-LD object and the caller reads it whole.
 */
export function pickSlotText(
  variants: Record<string, { id: number; treatment: any }> | undefined,
  slot: string,
  fallback: string,
  field: string = 'text',
  vars: Record<string, string | number> = {},
): string {
  const v = variants?.[slot];
  if (!v || !v.treatment) return fallback;
  const candidate = v.treatment[field];
  if (typeof candidate !== 'string' || candidate.length === 0) return fallback;
  // Substitute {name} placeholders against `vars`. Unknown placeholders
  // are left in place — the validator on the agent side rejects unknown
  // ones, so anything that lands here is a name the agent was told it
  // could use. Leaving unknowns visible makes their absence diagnosable.
  if (!candidate.includes('{')) return candidate;
  return candidate.replace(/\{([a-zA-Z_][a-zA-Z0-9_]*)\}/g, (m, key) =>
    Object.prototype.hasOwnProperty.call(vars, key) ? String(vars[key]) : m,
  );
}

/** Pick a JSON treatment object (e.g. schema fragment) with fallback. */
export function pickSlotJson<T>(
  variants: Record<string, { id: number; treatment: any }> | undefined,
  slot: string,
  fallback: T,
): T {
  const v = variants?.[slot];
  if (!v || !v.treatment) return fallback;
  return v.treatment as T;
}

function matchPattern(path: string): { pattern: Pattern; city: string | null; slug: string | null } | null {
  for (const pattern of PATTERNS) {
    if (pattern.regex.test(path)) {
      return {
        pattern,
        city: pattern.matchCity ? pattern.matchCity(path) : null,
        slug: pattern.matchSlug ? pattern.matchSlug(path) : null,
      };
    }
  }
  return null;
}

interface VariantRow {
  id: number;
  slot: string;
  page_match: string;
  treatment: string;
  status: string;
  alpha: number;
  beta: number;
}

export type VariantMap = Record<string, { id: number; treatment: any }>;

interface ResolveArgs {
  db: D1Database;
  path: string;
  sessionId: string | null;       // null = treat as bot / champion-only
  isBot: boolean;
  // Real navigation source (host of the page request's Referer header):
  // 'www.google.com' for an organic click, null for direct. Recorded on the
  // assignment so the optimizer can score variants on organic-search humans
  // (SEO_IMPRESSION_MODE='organic'). Distinct from the same-origin event
  // referrer on seo_outcomes, which always resolves to your own host.
  referrerHost?: string | null;
  // Cloudflare Bot Management score at render time (1 = bot .. 99 = human),
  // or null when the signal is unavailable. Stored for post-hoc filtering.
  botScore?: number | null;
  waitUntil: (p: Promise<any>) => void;
}

interface CachedBucket {
  fetchedAt: number;
  // grouped by slot
  bySlot: Record<string, VariantRow[]>;
}

const BUCKET_TTL_MS = 60_000;

// In-Worker process-local cache (per-isolate). Survives within a request and
// across requests handled by the same isolate. The `caches.default` API would
// also work but isolate-local Map keyed on the bucket name is simpler and avoids
// serialization. It's a Cloudflare best practice for sub-minute lookups like
// this where staleness is fine and the dataset is tiny.
const bucketCache = new Map<string, CachedBucket>();

export async function resolveVariants(args: ResolveArgs): Promise<{ variants: VariantMap }> {
  const matched = matchPattern(args.path);
  if (!matched) return { variants: {} };

  const { pattern, city, slug } = matched;
  // Bucket key: one slug-pattern + city is enough granularity to keep the
  // active-variant list small and matching cheap. Slug-level personalization
  // (e.g. one apartment vs another) is handled via page_match JSON.
  const bucketKey = `${pattern.name}|${city ?? ''}`;
  const now = Date.now();
  let bucket = bucketCache.get(bucketKey);
  if (!bucket || now - bucket.fetchedAt > BUCKET_TTL_MS) {
    bucket = await loadBucket(args.db, pattern.name, city);
    bucketCache.set(bucketKey, bucket);
  }

  const variants: VariantMap = {};
  const writes: { slot: string; variantId: number }[] = [];

  for (const slot of Object.keys(bucket.bySlot)) {
    const rows = bucket.bySlot[slot];
    if (!rows || rows.length === 0) continue;

    // Filter to rows whose page_match predicate matches the current page.
    const eligible = rows.filter((r) => pageMatchHits(r.page_match, { city, slug }));
    if (eligible.length === 0) continue;

    const champion = eligible.find((r) => r.status === 'champion');
    const active = eligible.filter((r) => r.status === 'active');

    let chosen: VariantRow | null = null;
    if (args.isBot || !args.sessionId) {
      // Bots and any session-less request: champion-only. If there's no
      // champion yet (early in a slot's life), we DELIBERATELY return
      // null so pickSlotText falls back to the literal fallback string
      // in the template. Two reasons:
      //   1. Otherwise the "first active" can drift across crawls as
      //      variants get added/killed, which Google may flag as cloaking.
      //   2. The literal fallback is deterministic across deploys, so
      //      every crawl sees the same HTML for a given path.
      chosen = champion ?? null;
    } else {
      // Deterministic per (session_id, slot): the same visitor sees the
      // same variant on every request, even if the bucket is replenished.
      // Mix in champion + active, weighted ~50/50 by giving the champion
      // its own Beta(α,β) draw alongside the active arms (Thompson sampling).
      const pool = champion ? [champion, ...active] : active;
      const seed = await hashSeed(`${args.sessionId}::${slot}`);
      chosen = thompsonSample(pool, seed);
    }

    if (chosen) {
      let treatmentJson: any = null;
      try {
        treatmentJson = JSON.parse(chosen.treatment);
      } catch {
        treatmentJson = null;
      }
      variants[slot] = { id: chosen.id, treatment: treatmentJson };
      if (!args.isBot && args.sessionId) {
        writes.push({ slot, variantId: chosen.id });
      }
    }
  }

  if (writes.length > 0 && args.sessionId) {
    args.waitUntil(
      writeAssignments(
        args.db,
        args.sessionId,
        args.path,
        writes,
        args.referrerHost ?? null,
        args.botScore ?? null,
      ),
    );
  }

  return { variants };
}

async function loadBucket(db: D1Database, patternName: string, city: string | null): Promise<CachedBucket> {
  // We pull all active and champion variants for the pattern, then filter
  // page_match at request time. The dataset per pattern stays small (≤
  // active_cap * slots * cities) so the in-memory filter is cheap.
  const slotPrefix = `${patternName}.`;
  const rows = await db
    .prepare(
      `SELECT id, slot, page_match, treatment, status, alpha, beta
       FROM seo_variants
       WHERE status IN ('active', 'champion')
         AND slot LIKE ?1
       ORDER BY id ASC`,
    )
    .bind(`${slotPrefix}%`)
    .all<VariantRow>();

  const bySlot: Record<string, VariantRow[]> = {};
  for (const row of rows.results || []) {
    if (!bySlot[row.slot]) bySlot[row.slot] = [];
    bySlot[row.slot].push(row);
  }
  // city is not used to scope the DB query — page_match is the source of
  // truth — but it shapes the cache bucket key so per-city buckets evict
  // independently as their variants change.
  void city;
  return { fetchedAt: Date.now(), bySlot };
}

function pageMatchHits(pageMatchJson: string, ctx: { city: string | null; slug: string | null }): boolean {
  if (!pageMatchJson || pageMatchJson === '{}') return true;
  let obj: Record<string, string>;
  try {
    obj = JSON.parse(pageMatchJson);
  } catch {
    return false;
  }
  for (const [k, v] of Object.entries(obj)) {
    if (v === '*') continue;
    if (k === 'city' && v !== ctx.city) return false;
    if (k === 'slug' && v !== ctx.slug) return false;
  }
  return true;
}

async function writeAssignments(
  db: D1Database,
  sessionId: string,
  pagePath: string,
  writes: { slot: string; variantId: number }[],
  referrerHost: string | null = null,
  botScore: number | null = null,
) {
  const now = new Date().toISOString();
  const stmts = writes.map((w) =>
    db
      .prepare(
        `INSERT OR IGNORE INTO seo_assignments (session_id, slot, variant_id, page_path, assigned_at, referrer_host, bot_score)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)`,
      )
      .bind(sessionId, w.slot, w.variantId, pagePath, now, referrerHost, botScore),
  );
  try {
    await db.batch(stmts);
  } catch (e: any) {
    console.error('seo_assignments write failed:', e?.message || e);
  }
}

// --- Thompson sampling ---------------------------------------------------
//
// For each candidate variant draw a sample from Beta(α, β), pick the variant
// with the highest draw. Beta(α, β) is sampled via two Gamma draws:
//   X ~ Gamma(α, 1), Y ~ Gamma(β, 1), Beta = X/(X+Y).
// We use Marsaglia & Tsang's method for Gamma(shape, 1) with shape ≥ 1,
// boosting any shape < 1 via the standard Beta(shape,1) trick. The RNG is
// seeded deterministically from (session_id, slot) so the same visitor sees
// the same variant across requests.

function thompsonSample(pool: VariantRow[], seed: number): VariantRow | null {
  if (pool.length === 0) return null;
  if (pool.length === 1) return pool[0];
  const rng = mulberry32(seed);
  let bestIdx = 0;
  let bestDraw = -Infinity;
  for (let i = 0; i < pool.length; i++) {
    const a = Math.max(1e-6, pool[i].alpha);
    const b = Math.max(1e-6, pool[i].beta);
    const draw = betaSample(rng, a, b);
    if (draw > bestDraw) {
      bestDraw = draw;
      bestIdx = i;
    }
  }
  return pool[bestIdx];
}

function betaSample(rng: () => number, alpha: number, beta: number): number {
  const x = gammaSample(rng, alpha);
  const y = gammaSample(rng, beta);
  if (x + y === 0) return 0.5;
  return x / (x + y);
}

function gammaSample(rng: () => number, shape: number): number {
  // Boost shape < 1 to shape + 1 via Beta(shape, 1) reduction.
  if (shape < 1) {
    const u = rng();
    return gammaSample(rng, shape + 1) * Math.pow(u, 1 / shape);
  }
  const d = shape - 1 / 3;
  const c = 1 / Math.sqrt(9 * d);
  // Marsaglia & Tsang
  while (true) {
    let x: number, v: number;
    do {
      x = normalSample(rng);
      v = 1 + c * x;
    } while (v <= 0);
    v = v * v * v;
    const u = rng();
    if (u < 1 - 0.0331 * x * x * x * x) return d * v;
    if (Math.log(u) < 0.5 * x * x + d * (1 - v + Math.log(v))) return d * v;
  }
}

function normalSample(rng: () => number): number {
  // Box-Muller
  const u1 = Math.max(rng(), 1e-12);
  const u2 = rng();
  return Math.sqrt(-2 * Math.log(u1)) * Math.cos(2 * Math.PI * u2);
}

function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return function () {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

async function hashSeed(s: string): Promise<number> {
  // FNV-1a 32-bit hash; deterministic without needing SubtleCrypto in dev.
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}
