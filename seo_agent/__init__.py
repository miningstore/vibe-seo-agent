"""Continuous SEO optimizer.

Parallel edge A/B experimentation system: proposes copy variants for
title / meta-description / H1 / FAQ / schema slots on the city, listing,
and apartment-detail pages; samples real visitors across variants via
Thompson sampling in the Astro middleware; reads engagement events from
D1 and Search-Console rank data from GSC; updates Beta posteriors and
promotes winners back into the static template via PR.

Lives alongside autoresearch/ on the same VPS, runs as
seo_agent.service. See ROADMAP in CLAUDE.md for re-enable
sequencing and the killswitch in wrangler.toml.
"""
