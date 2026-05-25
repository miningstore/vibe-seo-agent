/**
 * vibe-seo-worker — edge A/B for static sites on Cloudflare Pages.
 *
 * Sits in front of your Pages origin. On every request:
 *
 *   1. Reads (or mints) the `_arx` session cookie.
 *   2. Detects bots via UA pattern.
 *   3. Fetches the static HTML from your Pages origin.
 *   4. Looks up active variants for the requested path from D1.
 *   5. Thompson-samples one variant per slot (deterministic by
 *      session_id + slot).
 *   6. Uses Cloudflare's HTMLRewriter to swap <title>, <meta>, <h1>
 *      etc. INLINE in the streaming response — no parse-and-rebuild,
 *      sub-ms latency.
 *   7. Writes the assignment row to D1 fire-and-forget.
 *   8. Returns the rewritten HTML + the cookie if it was minted.
 *
 * Bots see the LITERAL fallback (the static HTML as built) until a
 * champion is named. No cloaking risk.
 *
 * Why a Worker not a Pages middleware function?
 *
 * - Pages middleware also works on static builds, but tying every
 *   request through a Worker route gives finer control (you can
 *   selectively skip /api/* paths, attach KV/D1, override headers,
 *   etc) and works on ANY static host you can route through CF.
 * - This pattern is fully portable. Drop it in front of Pages,
 *   Netlify, Vercel, even an S3 bucket — anywhere CF Workers can route.
 */

export interface Env {
  // D1 binding for the seo_variants / seo_assignments tables. Same schema
  // as the Astro/middleware example — see ../astro-cloudflare/migrations/.
  DB: D1Database;

  // The origin to fetch from. Usually your Pages deployment URL.
  // Set in wrangler.toml [vars]:
  //   ORIGIN = "miningstore-pages-xxx.pages.dev"
  ORIGIN: string;

  // Optional killswitch. Set to "false" to disable variant assignment
  // for all visitors and just transparently proxy the origin.
  SEO_OPTIMIZER_ENABLED?: string;
}

// Crawler UA pattern — substring match (NOT word-bounded — Googlebot/2.1
// has no word boundary between e and b). Bots see the static fallback.
const BOT_UA =
  /(bot|spider|crawler|slurp|bingpreview|duckduckbot|baiduspider|yandex|sogou|exabot|facebot|ia_archiver|ahrefs|semrush|petalbot|applebot|mj12|dotbot|rogerbot|headlesschrome|python-requests)/i;

// Variant resolution result.
interface VariantResolution {
  id: number;
  treatment: Record<string, any>;
}
type VariantMap = Record<string, VariantResolution>; // keyed by slot name

export default {
  async fetch(
    request: Request,
    env: Env,
    ctx: ExecutionContext,
  ): Promise<Response> {
    const url = new URL(request.url);
    const ua = request.headers.get("user-agent") || "";
    const isBot = BOT_UA.test(ua);
    const enabled =
      (env.SEO_OPTIMIZER_ENABLED ?? "true").toLowerCase() !== "false";

    // Always fetch the origin first — HTMLRewriter streams it.
    const originUrl = new URL(
      url.pathname + url.search,
      `https://${env.ORIGIN}`,
    );
    const originResponse = await fetch(originUrl.toString(), {
      method: request.method,
      headers: passthroughHeaders(request.headers),
      body: request.method === "GET" || request.method === "HEAD"
        ? undefined
        : request.body,
      redirect: "manual",
    });

    // Don't rewrite non-HTML.
    const contentType = originResponse.headers.get("content-type") || "";
    if (!contentType.includes("text/html") || !enabled) {
      return originResponse;
    }

    // Cookie minting + read.
    const cookieHeader = request.headers.get("cookie") || "";
    let sessionId = isBot ? null : readCookie(cookieHeader, "_arx");
    let mintedCookie: string | null = null;
    if (!sessionId && !isBot) {
      sessionId = crypto.randomUUID();
      // Secure flag required even though both target sites are HTTPS-only;
      // without it the cookie can leak over a forced-HTTP downgrade. SameSite=Lax
      // alone doesn't prevent that. The Worker only runs for HTTPS requests so
      // setting Secure here never blocks a legitimate first set.
      mintedCookie = `_arx=${sessionId}; Path=/; Max-Age=7776000; SameSite=Lax; Secure`;
    }

    // Resolve variants for this path.
    let variants: VariantMap = {};
    try {
      variants = await resolveVariants({
        db: env.DB,
        path: url.pathname,
        sessionId,
        isBot,
      });
    } catch (e) {
      // Fail open — never break the page because the optimizer is down.
      console.error("variant resolution failed:", e);
    }

    // Build the rewriter. Each slot maps to a CSS selector + a handler
    // that swaps the element's text or attribute.
    const rewriter = new HTMLRewriter()
      .on("title", new TextSwap(variants, "title"))
      .on('meta[name="description"]', new MetaContentSwap(variants, "meta_description"))
      .on('meta[property="og:title"]', new MetaContentSwap(variants, "og_title"))
      .on('meta[property="og:description"]', new MetaContentSwap(variants, "og_description"))
      .on("h1", new TextSwap(variants, "h1"));

    let rewritten = rewriter.transform(originResponse);

    // Persist the assignment fire-and-forget so we don't add latency.
    if (sessionId && Object.keys(variants).length > 0) {
      ctx.waitUntil(
        persistAssignments(env.DB, sessionId, url.pathname, variants),
      );
    }

    // Add the Set-Cookie if we minted one. Have to rebuild Response
    // since headers are immutable on the original.
    if (mintedCookie) {
      const headers = new Headers(rewritten.headers);
      headers.append("Set-Cookie", mintedCookie);
      rewritten = new Response(rewritten.body, {
        status: rewritten.status,
        statusText: rewritten.statusText,
        headers,
      });
    }

    return rewritten;
  },
};

// === Helpers =================================================================

function passthroughHeaders(h: Headers): Headers {
  // Strip hop-by-hop + host so the origin gets a clean request.
  const out = new Headers();
  for (const [k, v] of h) {
    if (["host", "cf-connecting-ip", "cf-ipcountry", "cf-ray"].includes(k.toLowerCase()))
      continue;
    out.set(k, v);
  }
  return out;
}

function readCookie(cookieHeader: string, name: string): string | null {
  for (const part of cookieHeader.split(";")) {
    const [k, ...v] = part.trim().split("=");
    if (k === name) return v.join("=");
  }
  return null;
}

// === Variant resolution ======================================================

interface ResolveArgs {
  db: D1Database;
  path: string;
  sessionId: string | null;
  isBot: boolean;
}

async function resolveVariants(args: ResolveArgs): Promise<VariantMap> {
  // Look up active + champion variants whose page_match JSON either
  // is empty {} (match any page under their pattern) or has a "path"
  // field equal to our path. The middleware DOES NOT understand
  // arbitrary page_match shapes — extend this lookup when you need
  // richer predicates (prefix matching, regex, etc).
  const rows = await args.db
    .prepare(
      `SELECT id, slot, status, treatment, page_match, alpha, beta
         FROM seo_variants
        WHERE status IN ('active', 'champion')
          AND (page_match = '{}' OR json_extract(page_match, '$.path') = ?1)`,
    )
    .bind(args.path)
    .all<VariantRow>();

  const grouped = groupBySlot(rows.results || []);
  const out: VariantMap = {};

  for (const [slot, candidates] of Object.entries(grouped)) {
    const champion = candidates.find((c) => c.status === "champion");
    const active = candidates.filter((c) => c.status === "active");

    let chosen: VariantRow | null = null;
    if (args.isBot || !args.sessionId) {
      // Bots and session-less: champion-only. If no champion exists,
      // return null so the HTMLRewriter passes through the original
      // (literal) text. NEVER pick a "first active" — that's a
      // cloaking risk across crawls.
      chosen = champion ?? null;
    } else {
      const pool = champion ? [champion, ...active] : active;
      if (pool.length === 0) continue;
      const seed = await hashSeed(`${args.sessionId}::${slot}`);
      chosen = thompsonSample(pool, seed);
    }

    if (chosen) {
      let treatmentJson: Record<string, any> = {};
      try {
        treatmentJson = JSON.parse(chosen.treatment);
      } catch {
        continue;
      }
      out[slot] = { id: chosen.id, treatment: treatmentJson };
    }
  }
  return out;
}

interface VariantRow {
  id: number;
  slot: string;
  status: string;
  treatment: string;
  page_match: string;
  alpha: number;
  beta: number;
}

function groupBySlot(rows: VariantRow[]): Record<string, VariantRow[]> {
  const map: Record<string, VariantRow[]> = {};
  for (const r of rows) {
    (map[r.slot] ??= []).push(r);
  }
  return map;
}

async function hashSeed(s: string): Promise<number> {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  const view = new DataView(buf);
  // 32-bit unsigned, deterministic per (sessionId, slot)
  return view.getUint32(0, false);
}

// Approximate Thompson sampling, deterministic per (visitor, variant)
// pair so the same visitor always sees the same variant on every
// request.
//
// For each variant we draw a Beta(α,β) sample. We don't have a real
// Beta sampler in Workers, so we approximate: per-variant uniform draw
// in [0,1) seeded by (sessionSeed XOR variantId), then mapped to a
// sample whose location matches the Beta mean with a spread matching
// its stddev. Crude but: (a) gives the right ordering when one variant
// is decisively winning, (b) distributes uniformly across the pool
// when α=β=1 (fresh variants), which the prior naive `closest-to-mean`
// approach failed at — all means equal 0.5, all distances equal,
// pool[0] always won.
//
// The production loop on the VPS uses scipy.stats.beta proper; this
// is only the arm picker at request time.
function thompsonSample(pool: VariantRow[], seed: number): VariantRow {
  let best = pool[0];
  let bestSample = -Infinity;
  for (const v of pool) {
    const mixed = (seed ^ v.id) >>> 0;
    const u = (mixed % 1_000_000) / 1_000_000;            // uniform [0,1)
    const mean = v.alpha / (v.alpha + v.beta);
    const variance =
      (v.alpha * v.beta) /
      ((v.alpha + v.beta) ** 2 * (v.alpha + v.beta + 1));
    const stddev = Math.sqrt(variance);
    // Sample = mean + 2 * stddev * (u - 0.5). For Beta(1,1) this gives
    // a range of roughly [-0.08, 1.08] clamped to [0,1] downstream;
    // for Beta(50, 5) — a near-certain winner — the sample is tightly
    // clustered around its mean. That's enough to drive arm selection.
    const sample = mean + 2 * stddev * (u - 0.5);
    if (sample > bestSample) {
      bestSample = sample;
      best = v;
    }
  }
  return best;
}

// === HTMLRewriter handlers ===================================================

class TextSwap {
  constructor(
    private variants: VariantMap,
    private slotSuffix: "title" | "h1",
  ) {}
  element(el: Element) {
    // Find the variant whose slot ends with the requested suffix
    // (e.g. "home.title" or "bm_hosting.title" for slotSuffix="title").
    for (const [slot, v] of Object.entries(this.variants)) {
      if (!slot.endsWith("." + this.slotSuffix)) continue;
      const text = v.treatment?.text;
      if (typeof text !== "string" || !text) continue;
      el.setInnerContent(text);
      return; // First match wins (slots are uniquely keyed by path).
    }
  }
}

class MetaContentSwap {
  constructor(
    private variants: VariantMap,
    private slotSuffix: "meta_description" | "og_title" | "og_description",
  ) {}
  element(el: Element) {
    for (const [slot, v] of Object.entries(this.variants)) {
      if (!slot.endsWith("." + this.slotSuffix)) continue;
      const text = v.treatment?.text;
      if (typeof text !== "string" || !text) continue;
      el.setAttribute("content", text);
      return;
    }
  }
}

// === D1 writes ===============================================================

async function persistAssignments(
  db: D1Database,
  sessionId: string,
  pagePath: string,
  variants: VariantMap,
): Promise<void> {
  const now = new Date().toISOString();
  const stmts: D1PreparedStatement[] = [];
  for (const [slot, v] of Object.entries(variants)) {
    stmts.push(
      db
        .prepare(
          `INSERT OR IGNORE INTO seo_assignments
             (session_id, slot, variant_id, page_path, assigned_at)
           VALUES (?1, ?2, ?3, ?4, ?5)`,
        )
        .bind(sessionId, slot, v.id, pagePath, now),
    );
  }
  if (stmts.length > 0) await db.batch(stmts);
}
