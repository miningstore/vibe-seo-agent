/*
 * Reference implementation — adapt the PAGE_PATTERNS array below for
 * your site's URL structure. The middleware shape (cookie minting,
 * bot detection, Thompson sampling, D1 lookup, fire-and-forget
 * assignment write) is generic and reusable as-is.
 *
 * Copy this into your Astro site's `src/middleware.ts` (and the
 * helper into `src/lib/seo-variants.ts`), then `pickSlotText` from
 * your page templates.
 *
 * Originally extracted from average-rent.com — see project history.
 */

import { defineMiddleware } from 'astro:middleware';
import { readCookie } from './lib/auth';
import { resolveVariants, isExperimentPath, BOT_UA } from './lib/seo-variants';

// Cloudflare Bot Management score at or below this is treated as automated
// (scores run 1 = definitely bot .. 99 = definitely human). 30 is CF's
// commonly-cited "likely automated" cutoff — conservative enough that real
// humans (who score 80-99) are never excluded, while spoofed-UA scrapers
// (which score low) stop entering the variant test pool.
const BOT_SCORE_THRESHOLD = 30;

/**
 * Two responsibilities:
 *
 *   1. SEO variant assignment (edge A/B). On experiment-bearing paths
 *      (currently /city/*), assign each real visitor a stable variant
 *      per slot via Thompson sampling, write the assignment fire-and-
 *      forget to D1, and expose the resolved map on `locals.seoVariants`
 *      so page templates can render variant-specific copy. Crawlers
 *      always see the current champion — never an under-tested variant.
 *
 *   2. Cache-Control headers for SSR HTML so Cloudflare's edge can cache
 *      crawler responses. Experiment-bearing pages for real visitors
 *      bypass edge cache (variant content is per-cookie); crawler
 *      responses on the same paths stay cacheable because they're
 *      deterministic (always the champion).
 */
export const onRequest = defineMiddleware(async (context, next) => {
  const path = context.url.pathname;
  const ua = context.request.headers.get('user-agent') || '';
  const cookieHeader = context.request.headers.get('cookie') || '';

  // Bot detection OR's two signals:
  //   1. User-Agent regex (BOT_UA) — self-identified crawlers.
  //   2. Cloudflare Bot Management — `cf.botManagement.score` (1 = automated
  //      .. 99 = human) plus `verifiedBot`. Catches scrapers that spoof a real
  //      browser UA, which the regex alone cannot. Present only when Bot
  //      Management / Bot Fight Mode evaluated the request; absent → fall back
  //      to the UA check (purely additive, never a regression).
  // Bots never get a cookie or a variant assignment — they see the champion.
  const cf = (context.locals as any).runtime?.cf;
  const rawScore = cf?.botManagement?.score;
  const botScore: number | null =
    typeof rawScore === 'number' && rawScore > 0 ? rawScore : null;
  const scoreSaysBot = botScore !== null && botScore <= BOT_SCORE_THRESHOLD;
  const isBot = BOT_UA.test(ua) || cf?.botManagement?.verifiedBot === true || scoreSaysBot;

  // Real navigation source for THIS page request: the host of the Referer
  // header ('www.google.com' on an organic-search click, null on a direct
  // visit). Recorded on the assignment so the optimizer can score variants on
  // organic-search humans (SEO_IMPRESSION_MODE='organic'); the same-origin
  // event referrer on seo_outcomes always resolves to your own host and can't.
  let referrerHost: string | null = null;
  try {
    const ref = context.request.headers.get('referer') || '';
    if (ref) referrerHost = new URL(ref).host || null;
  } catch {
    referrerHost = null;
  }

  // Resolve SEO variants before next() so the page template can read them.
  // Killswitch: SEO_OPTIMIZER_ENABLED=false disables variant assignment and
  // forces champion-only rendering for everyone.
  const env = context.locals.runtime?.env;
  const killswitch = env?.SEO_OPTIMIZER_ENABLED === 'false';
  const experimentPath = isExperimentPath(path);

  let mintedSessionId: string | null = null;
  let didAssignVariants = false;

  if (experimentPath && env?.DB && !killswitch) {
    // Bots always see the champion; we do not mint cookies for them.
    let sessionId = isBot ? null : readCookie(cookieHeader, '_arx');
    if (!sessionId && !isBot) {
      sessionId = crypto.randomUUID();
      mintedSessionId = sessionId;
    }

    try {
      const { variants } = await resolveVariants({
        db: env.DB,
        path,
        sessionId,
        isBot,
        referrerHost,
        botScore,
        waitUntil: (p) => {
          try {
            context.locals.runtime?.ctx?.waitUntil?.(p);
          } catch {
            /* dev runtime may not have ctx; let the promise settle */
            p.catch(() => {});
          }
        },
      });
      // Always attach (possibly empty) so templates can rely on the shape.
      context.locals.seoVariants = variants;
      didAssignVariants = !isBot && Object.keys(variants).length > 0;
    } catch (e: any) {
      console.error('seoVariants resolve failed:', e?.message || e);
      context.locals.seoVariants = {};
    }
  } else if (experimentPath) {
    // Always set the field so the templates' optional read is consistent.
    context.locals.seoVariants = {};
  }

  const response = await next();

  // Mint the cookie on the response when we created a fresh session id.
  // 90 days, SameSite=Lax, no HttpOnly (the tracker reads it via a JSON
  // island, not via document.cookie, but keeping it readable lets us pass
  // it through to client-side analytics if we ever need to).
  if (mintedSessionId) {
    response.headers.append(
      'Set-Cookie',
      // Secure flag required: the middleware only runs over HTTPS in
      // production, and without Secure the cookie could leak under a
      // forced-HTTP downgrade. SameSite=Lax alone doesn't prevent that.
      `_arx=${mintedSessionId}; Path=/; Max-Age=7776000; SameSite=Lax; Secure`,
    );
  }

  // Only touch HTML responses for the cache-control logic.
  const contentType = response.headers.get('content-type') || '';
  if (!contentType.includes('text/html')) return response;

  // Skip if the route already set its own Cache-Control.
  if (response.headers.get('cache-control')) return response;

  // Skip API, admin, account, and auth-sensitive paths.
  const skipPrefixes = ['/api/', '/admin/', '/preferences/', '/account/', '/login/', '/logout/'];
  if (skipPrefixes.some((p) => path.startsWith(p))) return response;

  // Authenticated requests: never cache at the shared edge — per-subscriber
  // HTML can vary on Pro entitlements.
  if (cookieHeader && (readCookie(cookieHeader, 'ar_session') || readCookie(cookieHeader, 'ar_admin'))) {
    response.headers.set('Cache-Control', 'private, no-store');
    return response;
  }

  // Skip if the response set a personalized cookie (fresh login, etc.).
  // _arx is fine to cache around for bots (we never set _arx for bots);
  // for real visitors with _arx on experiment paths we explicitly bypass
  // edge cache below.
  const setCookie = response.headers.get('set-cookie') || '';
  if (setCookie && !setCookie.startsWith('_arx=')) return response;

  // Experiment-bearing real-visitor responses are per-cookie variant copy.
  // They must not be cached at the shared edge.
  if (experimentPath && !isBot && (mintedSessionId || readCookie(cookieHeader, '_arx') || didAssignVariants)) {
    response.headers.set('Cache-Control', 'private, max-age=0');
    return response;
  }

  // Default: cache for 1 min in browser, 5 min at the edge. Short enough
  // that a bad response evicts within minutes; long enough to absorb
  // crawler traffic spikes and keep Googlebot off D1.
  response.headers.set('Cache-Control', 'public, max-age=60, s-maxage=300');
  return response;
});
