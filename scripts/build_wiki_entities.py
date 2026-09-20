"""MS7b Phase 2b -- decompose wiki sections into entities.

Consumes scripts/build_wiki_sections.py's registry and, for every
non-boilerplate section, asks a model to decompose `heading + section
lede` into the concrete named entities it names (Todd's correction,
2026-09-13: a full heading like "Why Gemma 4 12B is especially suitable
artistically" can't name-match an episode; decomposed into "Gemma 4 12B"
+ "Art" it can). Model choice (openai/gpt-oss-20b, local) and the
batching approach were validated in the working session: batches of
~25-30 headings parse reliably and run at ~0.6-0.9s/heading, against
~4s/heading unbatched and qwen3.5-122b's ~130s/heading with a 30%
truncation rate on the same enriched-episode task.

Two providers (--provider local|gemini). This step is pure text
generation -- no embeddings -- so it is NOT provider-locked the way
Phase 3's seeding/replay is: the entity names/types it emits are reusable
regardless of which model embeds them later. That is what makes Gemini a
safe *fallback* here specifically (Todd, 2026-09-13, when the Spark
tunnel went down mid-run): the --provider gemini path draws on the same
shared free-tier ledger (server.core.rate_limiter) production capture
uses, reserving one call at a time and stopping clean (checkpointed,
resumable) on GeminiQuotaExhaustedError rather than competing with live
capture past what the ledger allows. Requires Todd's go each time, since
it spends shared quota -- never invoked without --provider gemini
explicitly on the command line.

Output is a reviewable JSON registry -- inspect it before Phase 3 seeds
anything into a graph. Deterministic given the same input file and model
(temperature 0), but the model call itself makes this NOT a pure function
of the input the way build_wiki_sections.py is.

Usage:
    uv run python scripts/build_wiki_entities.py [--sections PATH] [--out PATH]
        [--batch-size 25] [--limit N] [--model NAME]
        [--provider local|gemini] [--resume]
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from server.core.config import load_config

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

DEFAULT_SECTIONS = Path("imports/state/wiki_sections.json")
DEFAULT_OUT = Path("imports/state/wiki_entities.json")
DEFAULT_BATCH_SIZE = 25
DEFAULT_MODEL = "openai/gpt-oss-20b"  # --provider local default; ignored for --provider gemini
LEDE_CHARS = 220  # section lede is already word-capped upstream; this is a char safety net

SYSTEM_PROMPT = (
    "Decompose each numbered heading into the concrete named entities it is about: short "
    "canonical nouns (product, tool, model, person, org, project, place, technique, domain), "
    "as said in isolation. Strip framing words, numbering, dates. No propositions. 0-4 per "
    "heading; empty if nothing concrete is named. Use the section-opens text only as context "
    "for the heading, not as its own heading.\n"
    "Reply with ONLY this JSON, one entry per heading, same order and count as the input:\n"
    '{"results":[{"n":1,"entities":[{"name":"...","type":"..."}]}]}'
)

# Generic abstractions the model reaches for when a heading names a process
# rather than a thing ("Recommendations", "Next Steps") or when it pads out
# a thin heading with the section's own structural vocabulary. Filtered
# post-hoc rather than prompted away entirely -- the prompt already asks
# for concrete nouns, and a static stoplist catches what slips through
# without trying to out-guess the model in the instructions. This is a
# safety net, not the real filter -- Phase 4's wiki-registry + recurrence
# sweep is what actually separates signal from noise.
STOPLIST = {
    "decision", "recommendation", "recommendations", "summary", "overview",
    "background", "context", "status", "approach", "next steps", "notes",
    "questions", "open questions", "connections", "description", "activities",
    "rumors", "vendor", "wall", "tabs", "planet",
}


def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


_TRANSIENT_NETWORK_ERRORS = (urllib.error.URLError, TimeoutError, ConnectionError, OSError)


def _call_model(
    base_url: str, api_key: str, model: str, prompt_user: str, max_tokens: int, retries: int = 3,
) -> tuple[Optional[list[dict]], str]:
    """One chat-completions call. Returns (parsed results-or-None, raw text for diagnostics).

    text mode + schema-in-prompt, not response_format=json_schema: LM
    Studio's reasoning models put the entire answer in `reasoning_content`
    and leave `content` empty under json_schema constrained decoding (see
    server/providers/lmstudio_client.py's docstring for the measured
    per-model breakdown) -- text mode is the one that reliably fills
    `content`.

    Retries a few times with backoff on a transient network error (the
    SSH tunnel to the Spark dropping mid-run is a real, observed failure
    mode, not a hypothetical one -- see docs/plan-active.md's MS7b Phase 2
    notes). A sustained outage still propagates after `retries` attempts;
    the caller (build()) checkpoints after every batch specifically so
    that case loses at most one in-flight batch, not the whole run.
    """
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": max_tokens,
        "response_format": {"type": "text"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt_user},
        ],
    }
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    last_exc: Optional[Exception] = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                data = json.loads(resp.read())
            break
        except _TRANSIENT_NETWORK_ERRORS as e:
            last_exc = e
            if attempt < retries - 1:
                backoff = 5.0 * (attempt + 1)
                logger.warning(f"    transient network error ({e!r}); retrying in {backoff:.0f}s "
                                f"(attempt {attempt+1}/{retries})")
                time.sleep(backoff)
    else:
        raise last_exc
    msg = data["choices"][0]["message"]
    raw = ((msg.get("content") or "") + "\n" + (msg.get("reasoning_content") or "")).strip()
    m = re.search(r'\{\s*"results"\s*:\s*\[.*\]\s*\}', raw, re.DOTALL)
    if not m:
        return None, raw
    try:
        return json.loads(m.group(0))["results"], raw
    except (json.JSONDecodeError, KeyError, TypeError):
        return None, raw


def _call_gemini(rate_limiter, prompt_user: str, max_tokens: int, retries: int = 3) -> tuple[Optional[list[dict]], str]:
    """Same contract as _call_model, against Gemini instead of the local
    LM Studio endpoint. Reserves one call against the shared free-tier
    ledger (server.core.rate_limiter) before every request -- the same
    ledger production capture draws from -- so this competes for quota
    honestly rather than around the accounting. On GeminiQuotaExhaustedError
    the caller's checkpoint-then-raise handles a clean stop, same as a
    sustained network outage on the local path.

    Unlike the local path, no reasoning_content/content-split quirk to
    work around (see server/providers/lmstudio_client.py's docstring for
    why that workaround exists only for LM Studio's reasoning models) --
    response_mime_type=application/json is requested directly, matching
    the existing Gemini-calling precedent in
    server/policies/reasoning_episode.py's _default_generate.
    """
    from dotenv import load_dotenv
    from google import genai

    from server.core.rate_limiter import (
        GeminiQuotaExhaustedError,
        classify_transient_error,
    )

    load_dotenv()
    import os
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set (project-root .env). --provider gemini needs it.")
    client = genai.Client(api_key=api_key)

    model = rate_limiter.reserve(estimated_calls=1)  # raises GeminiQuotaExhaustedError if none has headroom

    last_exc: Optional[Exception] = None
    for attempt in range(retries):
        try:
            resp = client.models.generate_content(
                model=model,
                contents=f"{SYSTEM_PROMPT}\n\n{prompt_user}",
                config={"response_mime_type": "application/json", "temperature": 0},
            )
            raw = resp.text or "{}"
            break
        except GeminiQuotaExhaustedError:
            raise
        except Exception as e:  # noqa: BLE001
            last_exc = e
            if classify_transient_error(e) == "unavailable" and attempt < retries - 1:
                backoff = 5.0 * (attempt + 1)
                logger.warning(f"    transient Gemini error ({e!r}); retrying in {backoff:.0f}s "
                                f"(attempt {attempt+1}/{retries})")
                time.sleep(backoff)
            else:
                raise
    else:
        raise last_exc

    m = re.search(r'\{\s*"results"\s*:\s*\[.*\]\s*\}', raw, re.DOTALL)
    if not m:
        return None, raw
    try:
        return json.loads(m.group(0))["results"], raw
    except (json.JSONDecodeError, KeyError, TypeError):
        return None, raw


def decompose_batch(call_fn, batch: list[dict]) -> list[list[dict]]:
    """One batch of sections -> list of entity-lists (same order), via
    whichever provider's call_fn the caller built. On any parse failure,
    falls back to per-section singleton calls for just this batch rather
    than losing it -- singleton calls are the same code path at batch
    size 1, just slower."""
    lines = [f'{i}. {s["heading_clean"]} || opens: {s["lede"][:LEDE_CHARS]}' for i, s in enumerate(batch, 1)]
    max_tokens = 200 + 90 * len(batch)
    results, raw = call_fn("\n".join(lines), max_tokens)

    if results is not None and len(results) == len(batch) and all(isinstance(r, dict) for r in results):
        by_n = {r.get("n"): (r.get("entities") or []) for r in results}
        return [by_n.get(i, []) for i in range(1, len(batch) + 1)]

    logger.warning(f"  batch of {len(batch)} failed to parse cleanly; falling back to per-section calls")
    out = []
    for s in batch:
        r, _ = call_fn(f'1. {s["heading_clean"]} || opens: {s["lede"][:LEDE_CHARS]}', 400)
        entry = r[0] if r and isinstance(r[0], dict) else None
        out.append((entry.get("entities") or []) if entry else [])
    return out


def _load_checkpoint(out_path: Path) -> tuple[dict[str, dict[str, Any]], set[str], int]:
    """Resume state from a previous (possibly interrupted) run's output
    file: (entities so far, section_ids already processed, stoplist hits
    so far). Empty state if no checkpoint exists yet."""
    if not out_path.exists():
        return {}, set(), 0
    try:
        prior = json.loads(out_path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}, set(), 0
    entities = prior.get("entities", {})
    done_ids: set[str] = set()
    for rec in entities.values():
        done_ids.update(rec.get("sections", []))
    done_ids.update(prior.get("processed_section_ids_no_entities", []))
    return entities, done_ids, prior.get("stoplist_filtered_mentions", 0)


def build(sections_path: Path, model: str, batch_size: int, limit: Optional[int],
          out_path: Path, resume: bool, provider: str = "local") -> dict:
    registry = json.loads(sections_path.read_text())
    all_sections = registry["sections"]
    candidates = [s for s in all_sections if not s["is_boilerplate"]]
    if limit is not None:
        candidates = candidates[:limit]

    MAX_RPM_WAIT_SECONDS = 90.0  # an RPM wall clears within a minute; an RPD wall does not -- don't block on that one

    if provider == "gemini":
        from server.core.rate_limiter import (
            DEFAULT_MODEL_CHAIN, GeminiQuotaExhaustedError, GeminiRateLimiter,
            KNOWN_MODEL_BUDGETS, _default_state_path,
        )
        # NOT get_default_rate_limiter(): that singleton is provider-aware and
        # deliberately returns an *unmetered* stand-in (chain=[local model],
        # no budgets) whenever CMF_LLM_PROVIDER=local -- correct for
        # production, useless here. This script needs a real Gemini-budgeted
        # limiter regardless of the live provider setting, so it builds one
        # directly -- pointed at the SAME persisted state file
        # (_default_state_path()) so it shares the real ledger with any
        # other Gemini usage rather than keeping a separate count.
        rate_limiter = GeminiRateLimiter(
            chain=list(DEFAULT_MODEL_CHAIN), budgets=KNOWN_MODEL_BUDGETS, state_path=_default_state_path(),
        )

        def call_fn(prompt, max_tok):
            while True:
                try:
                    return _call_gemini(rate_limiter, prompt, max_tok)
                except GeminiQuotaExhaustedError:
                    wait = rate_limiter.seconds_until_headroom()
                    if wait > MAX_RPM_WAIT_SECONDS:
                        raise  # RPD-bound (hours) -- let the caller checkpoint and stop clean
                    logger.info(f"    RPM wall hit; waiting {wait:.0f}s for headroom to clear")
                    time.sleep(wait)

        transient_errors: tuple = (GeminiQuotaExhaustedError,) + _TRANSIENT_NETWORK_ERRORS
        model = f"gemini:{rate_limiter.chain}"  # informational only -- reserve() picks the real one per call
        logger.info(f"Provider: gemini (fallback). Shared free-tier chain: {rate_limiter.chain}. "
                    f"This spends the same quota ledger production capture uses.")
    else:
        cfg = load_config()
        base_url, api_key = cfg.local_base_url, cfg.local_api_key
        call_fn = lambda prompt, max_tok: _call_model(base_url, api_key, model, prompt, max_tok)  # noqa: E731
        transient_errors = _TRANSIENT_NETWORK_ERRORS

    entities: dict[str, dict[str, Any]] = {}
    done_ids: set[str] = set()
    stoplist_hits = 0
    if resume:
        entities, done_ids, stoplist_hits = _load_checkpoint(out_path)
        if done_ids:
            logger.info(f"Resuming: {len(done_ids)} sections already done in {out_path}, "
                        f"{len(entities)} entities carried over")

    remaining = [s for s in candidates if s["section_id"] not in done_ids]
    no_entity_ids = {sid for sid in done_ids if sid not in {i for rec in entities.values() for i in rec["sections"]}}

    def _write_checkpoint():
        for rec in entities.values():
            rec["mention_count"] = len(rec["sections"])
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps({
            "schema_version": "wiki_entities_v1",
            "model": model,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "sections_processed": len(done_ids),
            "sections_total": len(candidates),
            "sections_skipped_boilerplate": len(all_sections) - len(candidates),
            "stoplist_filtered_mentions": stoplist_hits,
            "distinct_entities": len(entities),
            "processed_section_ids_no_entities": sorted(no_entity_ids),
            "entities": entities,
        }, indent=1))

    t0 = time.monotonic()
    for start in range(0, len(remaining), batch_size):
        batch = remaining[start:start + batch_size]
        try:
            per_section = decompose_batch(call_fn, batch)
        except transient_errors as e:
            logger.error(f"  batch at offset {start} failed outright after retries ({e!r}); "
                         f"checkpoint saved through the last successful batch -- rerun with --resume")
            _write_checkpoint()
            raise

        for section, ents in zip(batch, per_section):
            done_ids.add(section["section_id"])
            if not ents:
                no_entity_ids.add(section["section_id"])
            for e in ents:
                # The model occasionally returns a bare string instead of
                # {"name":..,"type":..} despite the schema instruction;
                # tolerate it rather than losing the whole batch to a crash.
                if isinstance(e, str):
                    name, etype = e, ""
                elif isinstance(e, dict):
                    name, etype = (e.get("name") or "").strip(), e.get("type", "")
                else:
                    continue
                name = name.strip()
                if not name:
                    continue
                if _norm(name) in STOPLIST:
                    stoplist_hits += 1
                    continue
                key = _norm(name)
                rec = entities.setdefault(key, {"name": name, "type": etype, "sections": []})
                rec["sections"].append(section["section_id"])

        _write_checkpoint()  # after every batch -- a mid-run failure loses at most one batch

        done = min(start + batch_size, len(remaining))
        elapsed = time.monotonic() - t0
        rate = elapsed / done if done else 0
        logger.info(f"  {len(done_ids)}/{len(candidates)} sections  ({elapsed:.0f}s this run, "
                    f"{rate:.2f}s/section, ~{rate*(len(remaining)-done)/60:.1f} min remaining)")

    for rec in entities.values():
        rec["mention_count"] = len(rec["sections"])

    return {
        "schema_version": "wiki_entities_v1",
        "model": model,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sections_processed": len(done_ids),
        "sections_total": len(candidates),
        "sections_skipped_boilerplate": len(all_sections) - len(candidates),
        "stoplist_filtered_mentions": stoplist_hits,
        "distinct_entities": len(entities),
        "processed_section_ids_no_entities": sorted(no_entity_ids),
        "entities": entities,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sections", type=Path, default=DEFAULT_SECTIONS)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--limit", type=int, default=None, help="cap on sections processed (calibration runs)")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--resume", action="store_true",
                         help="continue from --out's checkpoint, skipping sections already processed")
    parser.add_argument("--provider", choices=["local", "gemini"], default="local",
                         help="gemini is a fallback for when the local model is unreachable (e.g. Spark tunnel "
                              "down) -- spends the same shared free-tier ledger production capture uses, so it "
                              "is never the default; pass it explicitly each time.")
    args = parser.parse_args()

    result = build(args.sections, args.model, args.batch_size, args.limit, args.out, args.resume, args.provider)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=1))
    logger.info(f"\n{result['distinct_entities']} distinct entities from {result['sections_processed']} sections "
                f"({result['stoplist_filtered_mentions']} stoplist hits filtered)")
    logger.info(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
