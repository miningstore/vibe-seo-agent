# 04 - Operations

What to watch, how to interpret D1 rows, how to debug.

## The four tables

| Table | Owner | Lifecycle |
|---|---|---|
| `seo_variants` | agent loop | One row per variant proposal. `status` ∈ `active`/`paused`/`killed`/`champion`. `alpha`/`beta` are Beta posteriors updated by the eval phase. |
| `seo_assignments` | middleware | One row per `(session_id, slot, page_path)`. Written fire-and-forget when the middleware first picks a variant for a visitor. |
| `seo_outcomes` | tracker island | One row per engagement event (view, scroll_50/90, lead_click, cta_click, dwell_30s). Posted from in-page JS to `/api/seo-events`. |
| `seo_gsc_daily` | poller | One row per `(variant_id, date, page_path)`. Written by `gsc_poller` nightly. Per-variant rank/CTR/impressions split. |

## Daily heartbeat — what "healthy" looks like

```bash
# On the VPS (or laptop with CF creds in env)
python -m seo_agent.health_check
```

| Check | Healthy |
|---|---|
| GSC | OK; rows > 0; impressions > 0 |
| GA4 | OK; M accounts visible; sample path has traffic |
| D1 | OK; `seo_variants` > 0 once at least one slot is enabled |
| Anthropic | SKIP (plan auth — no API key needed) |

After the first day, also expect:

| Table | Healthy after 24h | After 7d | After 14d |
|---|---|---|---|
| `seo_variants` | 4–8 active per enabled slot | same (cap at `MAX_ACTIVE_VARIANTS_PER_SLOT`) | First `killed` rows appear |
| `seo_assignments` | hundreds to thousands per active variant (depends on traffic) | growing | stable rate |
| `seo_outcomes` | ~5–10× `seo_assignments` (one view + scroll + dwell per session) | growing | stable |
| `seo_gsc_daily` | 0 (no GSC data attributed yet) | rows for variants that have GSC impressions | rows for ALL active variants on indexed pages |

## What to watch over the first 7 days

### Hour 0 — launch checks

```bash
# Variants are actually inserted
sqlite> SELECT id, slot, status, treatment FROM seo_variants;

# Real visitors get assignments (hit the page in your browser, then:)
sqlite> SELECT COUNT(*) FROM seo_assignments;

# Bots see the fallback (no cloaking risk)
curl -sA "Googlebot/2.1" https://yoursite.com/path/ | grep -E '<h1>'
# Should match your template's literal fallback, NOT a variant
```

### Hour 24 — engagement events

```bash
sqlite> SELECT event, COUNT(*) FROM seo_outcomes GROUP BY event;
```

Should see view, scroll_50, scroll_90, dwell_30s and (if your CTAs
are wired with `data-seo-event`) cta_click / lead_click. If only
`view` rows exist, the tracker island isn't firing — check the
browser console on a city page for errors from the inline script in
`Base.astro` (or your framework's equivalent).

### Hour 48 — first GSC attribution

After the second nightly poll, `seo_gsc_daily` should have rows for
variants that were assigned to sessions whose page-path Google has
impressed.

```bash
sqlite> SELECT variant_id, SUM(impressions), SUM(clicks)
        FROM seo_gsc_daily GROUP BY variant_id;
```

If `seo_gsc_daily` is still empty after 48 hours, check:
- Were any pages impressed in GSC? `python -m seo_agent.gsc_poller --days 7 --no-attribute`
- Are there assignments to attribute to? `SELECT COUNT(*) FROM seo_assignments`
- Are the assignments' `page_path` values matching what GSC returned? Look at the `page_path` columns side-by-side.

### Day 7 — kill phase

The loop's eval logic will start marking under-performers `killed`
once they reach `KILL_MIN_IMPRESSIONS=2000` and `P(beat champion) < 5%`.

```bash
sqlite> SELECT id, status, alpha, beta FROM seo_variants WHERE status IN ('killed', 'paused');
```

If nothing's been killed by day 10 across thousands of impressions,
your variants are all too similar. Tune `seo_agent/prompts/variant_generator.md`
to push for more divergent proposals.

### Day 14 — promotion

```bash
sqlite> SELECT id, status, alpha, beta FROM seo_variants WHERE status = 'champion';
```

When a variant has `P(beat champion) > 95%` over `>= 5000 impressions`
and `>= 14 days`, the promoter:

1. Marks the variant `champion`, demotes the old champion to `paused`.
2. Opens a PR baking the winning treatment into the static template
   fallback (so the win survives even if D1 is wiped).
3. Bots start being served the new champion from then on.

## Cost monitoring

The agent only spends money when it invokes Claude. If you use plan
auth (recommended), there's no per-token cost — your plan covers it
up to the rate limits. If you use `ANTHROPIC_API_KEY`, the loop
tracks spend in `seo_agent/metrics/spending.json`.

`SEO_DAILY_SPEND_CAP_USD` in `.env` (default 15.0) hard-stops the
loop if cumulative API spend exceeds it in 24h. On plan auth this is
informational only.

## Common failures

### `claude exit 1` with no stderr

Most likely: you have `ANTHROPIC_API_KEY` set in `.env` but it's invalid
or expired, OR you re-introduced `--bare` to the claude args. The
`--bare` flag explicitly disables OAuth/keychain — drop it.

Test from the systemd-equivalent env:

```bash
sudo -u ubuntu bash -c 'cd ~/seo-agent && source seo_agent/.venv/bin/activate && set -a && source .env && set +a && ~/.local/bin/claude -p "hi" --print 2>&1 | head -5'
```

### `No module named seo_agent.gsc_poller` from systemd

Missing `Environment=PYTHONPATH=/home/ubuntu/seo-agent` in the unit
file. CWD-on-sys.path doesn't always work from systemd-spawned python.

### Worktree files keep disappearing

Sibling git op clobbered untracked files. Either commit the missing
files to `main` (so they get pulled), or move the worktree to a path
the sibling never touches.

### Bots seeing variant text (cloaking risk)

If `curl -A Googlebot` returns variant text on a slot that has no
`champion`, your `seo-variants.ts` is using an old version. Pull
the latest from this repo — the fix is to return `null` for bots
when no champion exists, so `pickSlotText` falls back to the literal.

### Variants all rejected with "unknown placeholder"

Add the placeholder name to the slot's `template_vars` in `config.py`.
Then update the page template to pass the substitution value:
`pickSlotText(slot, fallback, 'text', { city: cityDisplay, ... })`.

### Variants all rejected with "contains banned token"

Look at the rejection log line — it names the token. Decide: keep
the ban (good — the LLM was wandering into competitor territory) or
loosen it by removing the entry from `HOUSE_STYLE_BANNED` in `config.py`.

## Useful one-liners

```bash
# Watch agent loop in real time
sudo journalctl -u seo-agent.service -f

# Watch poller (will be quiet until 03:00)
sudo journalctl -u seo-gsc-poller.service -f

# Trigger a poll right now (not waiting for the timer)
sudo systemctl start seo-gsc-poller.service && \
  sudo journalctl -u seo-gsc-poller.service -n 20

# Restart the loop after config.py edits
git fetch origin main && git reset --hard origin/main
sudo systemctl restart seo-agent.service

# Pause the agent without killing it (e.g. for a deploy)
sudo systemctl stop seo-agent.service
# ... do thing ...
sudo systemctl start seo-agent.service
```

## When to pull the lever

The variant pool naturally trends toward 8 active per enabled slot
(the cap). Once that's reached, the loop just maintains: replenishes
killed slots with new proposals from Claude, updates Beta posteriors
on every eval pass.

**Add a slot** to widen the experimentation surface. Edit `config.py`
to flip an `enabled=False` to `True`, commit + push + VPS pulls.

**Add a page-pattern variant** for a specific city / category by
creating a new slot with `page_match={"city": "austin-tx"}` (the slot
will only apply to that city). The Thompson sampler reads
`(slot, page_match)` as the bucket.

**Promote a winner manually** if you're confident before the 14-day
gate: edit the row's `status` to `champion` in D1 and `status='paused'`
on the old champion. The promotion bot will eventually do this for
you but you can shortcut.

**Kill a runaway** if a variant is hurting business metrics in a way
the agent's reward signal can't see: edit `status='killed'` in D1
directly. The loop won't propose anything to fill the slot if the
total active count is still ≥ MAX.
