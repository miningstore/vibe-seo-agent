"""Build a Claude CLI prompt, run it, parse the JSON envelope.

The generator hands Claude the slot definition, current champion copy,
recent GSC data, the house voice + best-practices rubric, and the
analytics-mcp tool. Claude proposes 2–4 candidate treatments and emits
them in a final ```json``` block. We parse that block, validate each
treatment against the slot's hard rules, and return only the survivors.

This module DOES NOT write to D1. The loop runner owns persistence so
--dry-run can exercise the prompt and validator without side effects.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config as cfg


@dataclass
class GeneratedVariant:
    treatment: dict       # the raw treatment object (e.g. {"text": "..."} or full schema fragment)
    hypothesis: str       # 1-2 sentence rationale from the LLM
    validation_errors: list[str]  # populated if the variant failed validation; empty if OK


@dataclass
class GenerationResult:
    slot_name: str
    page_match: dict
    variants: list[GeneratedVariant]
    raw_stdout: str
    spent_usd: float


def generate(
    slot: cfg.Slot,
    page_match: dict,
    champion_treatment: dict | None,
    context_block: str,
    dry_run: bool = False,
) -> GenerationResult:
    prompt = _build_prompt(slot, page_match, champion_treatment, context_block)

    if dry_run:
        # In dry-run, exercise everything except the actual subprocess.
        # Emit a stub envelope so the validator path can still run.
        stub = {
            "slot": slot.name,
            "page_match": page_match,
            "variants": [
                {
                    "treatment": _stub_treatment(slot),
                    "hypothesis": "DRY-RUN STUB: real Claude would propose something here.",
                },
            ],
        }
        stub_json = "```json\n" + json.dumps(stub, indent=2) + "\n```"
        return _parse_and_validate(slot, page_match, stub_json, spent_usd=0.0)

    stdout, spent = _invoke_claude(prompt)
    return _parse_and_validate(slot, page_match, stdout, spent_usd=spent)


# --- Prompt construction ------------------------------------------------

def _build_prompt(
    slot: cfg.Slot,
    page_match: dict,
    champion_treatment: dict | None,
    context_block: str,
) -> str:
    rubric = (cfg.PROMPTS_DIR / "seo_best_practices.md").read_text()
    template = (cfg.PROMPTS_DIR / "variant_generator.md").read_text()
    champion_repr = (
        json.dumps(champion_treatment, indent=2)
        if champion_treatment is not None
        else "(no champion yet — this slot is brand new; propose strong starting variants)"
    )
    return f"""{template}

---

# Slot to vary

- **slot**: `{slot.name}`
- **page-pattern**: `{slot.pattern}`
- **kind**: `{slot.kind}`
- **length bounds**: {slot.min_len}–{slot.max_len} characters
- **description**: {slot.description}
- **page-match predicate**: `{json.dumps(page_match)}`
- **allowed placeholders**: {('{' + '}, {'.join(slot.template_vars) + '}') if slot.template_vars else 'NONE — any {placeholder} in your output will be rejected'}{f' — these will be substituted at render time, so use them.' if slot.template_vars else ''}

# Current champion copy

```json
{champion_repr}
```

# Recent context (GSC / GA4 / on-page data)

{context_block}

---

# Best-practices rubric

{rubric}

---

# Output requirement

Emit a single fenced ```json block at the END of your reply with the
shape described in the generator template above. No other code blocks.
"""


def _stub_treatment(slot: cfg.Slot) -> dict:
    if slot.kind == "text":
        return {"text": "Average Rent Listings, Real Prices, Refreshed Daily"}
    if slot.kind == "faq":
        return {"items": [{"question": "Stub?", "answer": "Stub."}]}
    return {"@context": "https://schema.org", "@type": "Thing", "name": "stub"}


# --- Claude CLI invocation ----------------------------------------------

def _invoke_claude(prompt: str) -> tuple[str, float]:
    """Run `claude -p --bare` with the MCP config, return (stdout, est_cost_usd).

    The cost estimate is rough — we read the `cost_usd` line if Claude
    emits it, otherwise fall back to a token-count heuristic. The loop
    runner is the source of truth for spend; this is just a hint.
    """
    if not shutil.which(cfg.CLAUDE_BIN) and not Path(cfg.CLAUDE_BIN).exists():
        raise RuntimeError(f"claude CLI not found at {cfg.CLAUDE_BIN}")

    # NOTE: do NOT pass --bare. That flag disables OAuth/keychain and
    # forces ANTHROPIC_API_KEY auth, which we don't use — we authenticate
    # via the Claude Code plan credentials at ~/.claude/.credentials.json
    # (same as autoresearch.service). The -p (--print) flag is what gives
    # us non-interactive single-shot output without disabling plan auth.
    args = [
        cfg.CLAUDE_BIN,
        "-p", prompt,
        "--output-format", "text",
        "--allowedTools", "Read,Glob,Grep",
        "--model", cfg.CLAUDE_MODEL,
        "--max-turns", str(cfg.CLAUDE_TURN_CAP),
    ]
    if cfg.MCP_CONFIG.exists() and cfg.MCP_CONFIG.stat().st_size > 50:
        # Only wire MCPs if .mcp.json declares any servers. An empty
        # `{"mcpServers": {}}` shouldn't be passed — claude treats it
        # as a hard requirement and errors if it can't reach the server.
        try:
            import json as _json
            with open(cfg.MCP_CONFIG) as _f:
                _mcp_cfg = _json.load(_f)
            if _mcp_cfg.get("mcpServers"):
                args += [
                    "--mcp-config", str(cfg.MCP_CONFIG),
                    "--allowedTools",
                    "Read,Glob,Grep," + ",".join(
                        f"mcp__{name}__*" for name in _mcp_cfg["mcpServers"].keys()
                    ),
                ]
        except Exception:
            pass

    proc = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=cfg.CLAUDE_TIMEOUT_S,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"claude exit {proc.returncode}: {(proc.stderr or '')[:500]}"
        )
    stdout = proc.stdout or ""
    spent = _extract_cost(stdout)
    return stdout, spent


def _extract_cost(stdout: str) -> float:
    m = re.search(r"cost_usd[\"']?\s*[:=]\s*([0-9]+\.?[0-9]*)", stdout)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return 0.0


# --- Parse + validate ---------------------------------------------------

def _parse_and_validate(
    slot: cfg.Slot,
    page_match: dict,
    raw_stdout: str,
    spent_usd: float,
) -> GenerationResult:
    envelope = _extract_json_envelope(raw_stdout)
    variants: list[GeneratedVariant] = []
    if envelope is None:
        return GenerationResult(slot.name, page_match, [], raw_stdout, spent_usd)

    for entry in envelope.get("variants", []) or []:
        treatment = entry.get("treatment")
        hypothesis = (entry.get("hypothesis") or "").strip()
        errors = validate_treatment(slot, treatment)
        variants.append(GeneratedVariant(
            treatment=treatment if isinstance(treatment, dict) else {},
            hypothesis=hypothesis,
            validation_errors=errors,
        ))
    return GenerationResult(slot.name, page_match, variants, raw_stdout, spent_usd)


def _extract_json_envelope(stdout: str) -> dict | None:
    # Prefer fenced ```json``` block at end of reply.
    fences = re.findall(r"```json\s*(.*?)\s*```", stdout, re.DOTALL)
    if fences:
        try:
            return json.loads(fences[-1])
        except json.JSONDecodeError:
            pass
    # Fallback: find the last JSON object that contains a "variants" key.
    for match in reversed(list(re.finditer(r"\{[^{}]*\"variants\"[^{}]*\}", stdout, re.DOTALL))):
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            continue
    return None


def validate_treatment(slot: cfg.Slot, treatment: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(treatment, dict):
        return ["treatment is not a JSON object"]

    if slot.kind == "text":
        text = treatment.get("text")
        if not isinstance(text, str):
            errors.append("missing or non-string `text` field")
            return errors
        n = len(text)
        if slot.min_len and n < slot.min_len:
            errors.append(f"text too short ({n} chars; min {slot.min_len})")
        if slot.max_len and n > slot.max_len:
            errors.append(f"text too long ({n} chars; max {slot.max_len})")
        low = text.lower()
        for token in slot.banned_tokens:
            if token.lower() in low:
                errors.append(f"contains banned token '{token}'")
        # Placeholders: allow only the ones whitelisted on the slot. Any
        # other {name} pattern is a sign the LLM emitted a templating
        # artefact that won't get substituted at render time.
        found = set(re.findall(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", text))
        allowed = set(slot.template_vars)
        unknown = found - allowed
        if unknown:
            errors.append(
                f"text contains unknown placeholders: {sorted(unknown)} "
                f"(slot allows: {sorted(allowed) or 'none'})"
            )
        if "\n" in text:
            errors.append("text contains newline")

    elif slot.kind == "faq":
        items = treatment.get("items")
        if not isinstance(items, list) or not (3 <= len(items) <= 8):
            errors.append("faq items must be a list of 3–8 entries")
        else:
            for i, it in enumerate(items):
                if not isinstance(it, dict):
                    errors.append(f"faq[{i}] not an object")
                    continue
                if not isinstance(it.get("question"), str) or not isinstance(it.get("answer"), str):
                    errors.append(f"faq[{i}] missing question/answer string")

    elif slot.kind == "schema":
        if treatment.get("@context") != "https://schema.org":
            errors.append("@context must be 'https://schema.org'")
        if not isinstance(treatment.get("@type"), str):
            errors.append("@type missing or non-string")

    return errors
