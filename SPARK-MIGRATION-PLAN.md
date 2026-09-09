# Migrating CMF off Gemini onto Spark-local models

**Status:** Phases 0-6 complete. Only Phase 7 (quality A/B) remains. **Written:** 2026-09-08. **Revised:** 2026-09-09 (rev 5 — Phases 0-6 built and verified; graph rebuilt into `mem-fabric-local`; first-look inspection findings folded into Phase 7).
**Scope:** replace the Google Gemini Developer API as CMF's LLM + embedding + reranking backend with models served from the DGX Spark (`nanospark`) over LM Studio's OpenAI-compatible endpoint.

Related: [docs/plan-active.md](docs/plan-active.md) · [docs/adr/0002-provider-boundaries.md](docs/adr/0002-provider-boundaries.md) · shared Spark Google Doc (Proposed Spec / Services tabs).

---

## Corrections to rev 1

Two claims in the first draft were wrong, and both corrections loosen constraints rather than tighten them:

1. **`gemma-4-26b-a4b` is not a broken quant, and is not disqualified.** Rev 1 called its degenerate output a bad quantization. It is not — given a plain prompt it answers correctly, and in `text` mode it returns valid extraction JSON. The garbage appears *only* under `json_schema` constrained decoding, from a control-token leak in the serving stack. Root cause in §3.3.
2. **`qwen3-coder-30b` is no longer the recommendation.** Per D1 the target is GLM-4.7-Flash, now verified on a realistic extraction prompt — 17 entities and 14 edges, the richest of the three models tested.

Two further items are new rather than corrected:

3. LM Studio **rejects `response_format: {"type":"json_object"}`** outright. This does *not* break graphiti's default path (every call site in the `add_episode` path passes a `response_model`, so the default mode only ever emits `json_schema`) — but it does mean graphiti's schema-in-prompt mode needs a request rewrite. See §3.2.
4. `text` mode changes the recommendation again, and is the headline result: with the schema in the prompt and no grammar, **all three preferred models — GLM, Qwen3.5 and Gemma — return clean JSON in `content`** and their actual thinking in `reasoning_content`. This is simpler than rev 2's proxy design: the response-side reasoning shim is no longer on the critical path. See §3.4.

---

## 0. Why this is worth doing, and what it costs

Today every `remember()`, `recall()`, reasoning-episode extraction and promotion goes through Gemini's free tier, gated by a hard local ledger in [`server/core/rate_limiter.py`](server/core/rate_limiter.py). That ledger exists because the free tier's ceilings are low — `gemini-3.5-flash-lite` allows 500 requests/day, and CMF conservatively reserves 3 calls per operation, so a promotion run realistically gets ~160 episodes/day before it stalls until midnight Pacific.

There are **1,243 `reasoning-episode@0.2` rows queued for review** and 285 already promoted. At free-tier throughput a full re-promotion is a multi-day affair paced by a rate limiter. On the Spark it is bounded only by inference speed.

The cost is that the vector space changes, which means a graph rebuild. That is the whole difficulty of this migration; everything else is plumbing.

---

## 1. Verified facts (measured 2026-09-08)

All Spark facts below come from `ssh spark` over Tailscale using the existing `~/.ssh/config` entry, with `curl` issued against `127.0.0.1:1234` **on the Spark itself** — nothing was sent to the public interface.

### Host

```
HOST=nanospark
id -nG  ->  todd sudo users docker          # both already granted; no usermod needed
df -h /  ->  3.7T total, 3.0T available
GPU      ->  NVIDIA GB10
listening -> 0.0.0.0:1234 (LM Studio), 0.0.0.0:3000 (OpenWebUI), *:3389 (xrdp)
`lms` is not on todd's PATH (it lives under the `nano` user's install)
```

**This answers two of the three open verification items from the last email:** Docker group membership is already in place, and Qwen3.5-35B-A3B is downloaded.

### Model inventory, residency and context (`/api/v0/models`)

```
model                                   state        ctx_loaded   ctx_max
qwen/qwen3.5-35b-a3b                    loaded             8192    262144
unsloth/qwen3.5-122b-a10b               not-loaded         None    262144
qwen/qwen3.8-27b                        not-loaded         None    262144
qwen/qwen3-coder-30b                    not-loaded         None    262144
zai-org/glm-4.7-flash                   not-loaded         None    202752
openai/gpt-oss-20b                      not-loaded         None    131072
google/gemma-4-26b-a4b                  not-loaded         None    262144
nvidia/nemotron-3-nano-omni             not-loaded         None    262144
google/gemma-4-e4b                      not-loaded         None    131072
text-embedding-nomic-embed-text-v1.5    not-loaded         None      2048
```

All chat models are `Q4_K_M` GGUF. Three operational findings here, none of them in the Google Doc:

- **The always-resident baseline does not exist yet.** Only one model is loaded at any moment, and which one changes as requests arrive — Auto-Evict is swapping aggressively. GLM-4.7-Flash and Qwen3.5-35B-A3B are *downloaded*, not *pinned*.
- **Models load at 8192 context, not 128K.** The agreed 128K cap isn't in effect; LM Studio is loading at its default. Graphiti's extraction prompt carries previous-episode context and can plausibly exceed 8192, which would truncate silently or error. This needs raising before a real run.
- **`glm-4.7-flash` reports `arch: deepseek2`**, which is why it behaves like a DeepSeek-family reasoning model under llama.cpp — relevant to §3.

### Embeddings

```
POST /v1/embeddings  model=text-embedding-nomic-embed-text-v1.5
single input -> 768 dims
batch input  -> 3 vectors, 768 dims each   (batching works; Graphiti needs it)
```

**It silently truncates at its 2048-token window.** Verified: embedding a ~4,200-token text and the same text with a completely distinct tail appended returns `cosine = 1.000000` — the tail is never seen, and no error or warning is raised.

Low practical risk: Graphiti only embeds entity **names** (`name_embedding`) and edge **facts** (`fact_embedding`), both short — `Episodic` nodes carry no embedding at all (confirmed against the live graph's property keys). `gemini-embedding-001` has the same 2048-token input limit, so this is parity, not a regression. Worth an assertion anyway, since the failure is invisible.

### The current CMF graph

```
FalkorDB graph `mem-fabric-gemini` (local Docker, 127.0.0.1:6379)
  Episodic nodes   337
  Entity nodes     337
  RELATES_TO edges 187
  stored vector dimension: 1024
Journal (imports/journal/journal.db)
  events                     19,012
  reasoning-episode@0.2      1,243 queued_for_review
  promotions                 285 succeeded, 24 failed
```

**1024 → 768 is the crux.** Two independent problems:

1. **The FalkorDB vector index is dimension-typed.** 768-dim vectors cannot be written into an index built for 1024.
2. **Even at matching dimensions it would still be wrong.** Nomic and Gemini embeddings occupy unrelated vector spaces; cosine similarity between them is noise. Any embedder change requires re-embedding everything regardless of dimension.

Graphiti has no re-embed API. Re-embedding means re-adding episodes, which means re-running extraction. Hence: **fresh graph, full re-promotion.** At 337 nodes this is cheap — the reason to do it now rather than after MS7 grows the graph.

---

## 2. The four seams in CMF

Every Gemini dependency lands in one of four places. Nothing else in the codebase talks to a model.

| # | Location | What it does | Change |
|---|---|---|---|
| 1 | [`server/providers/memory_graphiti.py:94`](server/providers/memory_graphiti.py) `create_graphiti()` | builds `GeminiClient` (L124), `GeminiEmbedder` (L132), `GeminiRerankerClient` (L139) | swap for OpenAI-compatible clients + the §4 proxy |
| 2 | [`server/core/rate_limiter.py:52`](server/core/rate_limiter.py) `KNOWN_MODEL_BUDGETS` | hard free-tier ledger; **rejects any model not in the dict** (constructor raises `ValueError`) | give local models effectively-infinite budgets |
| 3 | [`server/policies/reasoning_episode_v1.py:106`](server/policies/reasoning_episode_v1.py) `_default_generate()` | direct `google.genai` call, bypasses Graphiti entirely | second implementation against the OpenAI endpoint |
| 4 | [`server/consolidation/promotion.py:145`](server/consolidation/promotion.py) `inter_call_delay=3.5` | polite spacing for Gemini quota | drop to ~0.2 for local |

Seam 2 fails loudly and immediately: `GeminiRateLimiter.__init__` raises `ValueError` for any chain member missing from `KNOWN_MODEL_BUDGETS`, so setting `CMF_GEMINI_MODEL_CHAIN=zai-org/glm-4.7-flash` without touching the budgets dict crashes at startup.

---

## 3. How the local models behave under Graphiti — full diagnosis

Rev 1 reported these as model defects. They are not: every one is an interaction between LM Studio's serving stack and the *structured-output mode* Graphiti requests. Choose the right mode and all three preferred models work. §3.4 is the conclusion; §3.1–3.3 are the evidence.

### 3.1 The reasoning channel swallows the answer

LM Studio parses a reasoning model's thinking channel server-side and returns it in a **separate `reasoning_content` field**. When the model wraps its whole answer in that channel, `content` comes back empty. Graphiti does:

```python
result = response.choices[0].message.content or ''
if not result:
    raise EmptyResponseError('LLM returned an empty response')
```
— [`openai_generic_client.py:162`](.venv/lib/python3.12/site-packages/graphiti_core/llm_client/openai_generic_client.py)

Measured on a realistic extraction prompt (long text, nested entity+edge schema, `json_schema` response format):

| Model | `content` | `reasoning_content` | Verdict |
|---|---|---|---|
| `zai-org/glm-4.7-flash` | empty | **valid JSON — 12 entities, 10 edges** | ✅ works via proxy |
| `qwen/qwen3.5-35b-a3b` | empty | valid JSON (short prompt; long-prompt run pending) | ✅ works via proxy |
| `qwen/qwen3-coder-30b` | clean JSON | — (non-reasoning) | ✅ works unmodified |

The important result is GLM: under `json_schema`, constrained decoding applies to the reasoning channel too, so `reasoning_content` is **entirely** the JSON object — no chain-of-thought prose mixed in that a parser would choke on. That is what makes the proxy safe rather than a heuristic.

`chat_template_kwargs: {"enable_thinking": false}` **does not suppress this** — tested on GLM, identical empty-content result. LM Studio's reasoning parser runs regardless.

### 3.2 LM Studio rejects `json_object`

```
POST /v1/chat/completions  response_format={"type":"json_object"}
-> {"error": "'response_format.type' must be 'json_schema' or 'text'"}
```

Graphiti emits `json_object` in two situations:

```python
if response_model is None or self.structured_output_mode == 'json_object':
    return {'type': 'json_object'}
```

**Scope check** (this corrects an overstatement in an earlier draft of this section): every `generate_response` call site in graphiti 0.29.3's `add_episode` path passes `response_model` — verified across `node_operations`, `edge_operations`, `combined_extraction` and `community_operations`. So on the **default `json_schema` mode, LM Studio never sees a `json_object` request**, and this is not an independent breakage of the default path.

It matters for a different reason: it means `structured_output_mode='json_object'` — graphiti's built-in "put the schema in the prompt instead of enforcing it" mode — cannot be selected as-is, because every request it produces gets a 400. Since §3.4 makes that mode the *preferred* configuration, the proxy has to rewrite the request format. That is a request-side rewrite, not a workaround for a broken default.

### 3.3 Gemma: control-token leakage under constrained decoding

Gemma's `json_schema` output began correctly and then derailed:

```
{"entities": [{"name": "2026-09-04", "entity_type": "DATE",
 "summary": "The date of the<|channel>66666666666666666666...
```

The `<|channel>` token is the tell. Gemma 4's chat template uses special channel-control tokens to switch into and out of its thinking section. Under GBNF constrained decoding the grammar constrains the *text* shape — inside a JSON string it permits arbitrary characters — but **does not mask the model's special/control tokens**. So when the model tries to emit a channel transition mid-summary, the grammar happily accepts it as string content. Once that control token sits in-context in a structurally invalid position, the distribution collapses into `6666…` degeneracy until the token cap (`finish=length`, 4000 tokens burned).

This is a grammar/special-token interaction in the serving stack, not a defect in the weights — which is exactly why the same model answers a plain prompt correctly. Consequences:

- It is **not fixable from CMF's side** by prompt or parameter changes. The fix lives in llama.cpp/LM Studio (masking special tokens during constrained decoding) or in the model's GGUF metadata. Worth reporting to Alex as a serving-stack bug independent of CMF.
- **It does not disqualify Gemma.** Avoiding `json_schema` avoids the bug entirely: in `text` mode Gemma returns valid JSON (12 entities, 10 edges — §3.4). The constraint is on the *mode*, not the model.
- It does mean **Mode A is off the table as a universal default**, since it is the mode that triggers this.

### 3.4 Two workable modes — and why `text` mode is the better one

Testing `text` mode (no grammar, schema injected into the prompt — exactly what graphiti's `json_object` mode does) changes the recommendation:

Same realistic prompt, both modes, all three preferred models:

| Model | Mode A — `json_schema` (grammar) | Mode B — `text` + schema in prompt |
|---|---|---|
| `zai-org/glm-4.7-flash` | `reasoning_content` = valid JSON, `content` empty (12 ent / 10 edges) | ✅ **`content` = valid JSON — 17 entities, 14 edges** (4,155 completion tokens) |
| `qwen/qwen3.5-35b-a3b` | `reasoning_content` = valid JSON, `content` empty | ✅ `content` = valid JSON — 14 entities, 10 edges (5,254 tokens) |
| `google/gemma-4-26b-a4b` | ❌ control-token corruption (§3.3) | ✅ `content` = valid JSON — 12 entities, 10 edges (5,779 tokens) |

**Mode B works on every model, including Gemma.** In all three cases `reasoning_content` holds genuine thinking prose (`'1. **Analyze the Request:** …'`) and `content` holds clean JSON — the models behaving as designed, with the reasoning channel doing its job instead of being force-fed a grammar.

- **Mode A — `json_schema` + response-side shim.** Constrained decoding guarantees parseable output, but on GLM/Qwen it all lands in `reasoning_content`, and it is what corrupts Gemma.
- **Mode B — `text` + schema in prompt.** Universal across the family, and needs only a *request-side* rewrite — the response-side shim becomes unnecessary. No parse guarantee.

**Mode B is the recommendation.** The missing guarantee is already covered upstream: graphiti retries `JSONDecodeError` and `EmptyResponseError` up to 4 times with exponential backoff (`is_server_or_retry_error`, `stop_after_attempt(4)` — `llm_client/client.py:62`). An occasional unparseable reply self-heals; a persistently unparseable one fails with a clear error. Keep the Mode A response-side shim in the proxy as a guarded fallback so both configurations work from one code path.

**GLM extracts the richest structure of the three** on this sample (17/14 vs 14/10 and 12/10) at the lowest token cost, which makes D1's preference the right call on evidence rather than just intent.

One caution: all three burn **4,000–5,800 completion tokens per extraction call**, most of it reasoning. That is the throughput risk quantified — see Phase 7.

---

## 4. Decisions — resolved

**D1 — Extraction model: GLM-4.7-Flash, via the client proxy, in Mode B.** ✅ *Resolved: durable route preferred over switching to `qwen3-coder-30b`.*

GLM is verified working on a realistic extraction prompt (12 entities, 10 edges). The proxy carries both behaviours so the mode is a config flag rather than a rewrite:

1. **Request side (Mode B, primary):** rewrite `response_format: {"type":"json_object"}` → `{"type":"text"}` (§3.2). Graphiti has already appended the schema to the prompt in that mode, so nothing is lost.
2. **Response side (Mode A fallback, guarded):** when `content` is empty and `reasoning_content` parses as JSON, promote it into `content` (§3.1).

In Mode B the response-side shim is not actually exercised — it is retained only so Mode A remains selectable. **All three of your preferred models are verified working**, so if GLM disappoints on the Phase 7 quality gate, switching to Qwen3.5-35B-A3B or Gemma-4-26B-A4B is a one-variable change. `qwen3-coder-30b` is no longer merely an escape hatch: it is 3.4x cheaper in output tokens on the identical prompt (Phase 7), which is the difference between ~15 h and ~50 h on the full 1,243-row backlog. It joins the quality A/B rather than sitting in reserve.

**D2 — New graph.** ✅ *Resolved: fresh graph, full re-promotion, not more writes into `mem-fabric-gemini`.*

Target `mem-fabric-local`. `FALKORDB_DATABASE` has no default and refuses to guess ([`resolve_target_database`](server/providers/memory_graphiti.py)), so switching is a one-line `.env` change and the Gemini-era graph survives untouched for the §9 comparison and for rollback.

**Confirmed split:** the graph lives on Todd's machine; only the *inference* runs on the Spark.

```
Mac                                    Spark (nanospark)
  CMF server                             LM Studio :1234
  FalkorDB (Docker, 127.0.0.1:6379)  <-- SSH tunnel -->  GLM-4.7-Flash
    graph `mem-fabric-gemini`      (kept, untouched)         nomic-embed-text
    graph `mem-fabric-local` (new, 768-dim)
  BGE reranker (sentence-transformers)
```

So FalkorDB is not moved, no second tunnel is needed, and the graph never leaves your hardware. What crosses the tunnel is extraction prompts and embedding requests. The Gemini-era `mem-fabric-gemini` graph stays in place for the Phase 7 A/B and for rollback.

**D3 — Reranking: `BGERerankerClient`.** ✅ *Resolved: option 1 — but see below, it is not currently reachable.*

**Measured after Phases 2-3: CMF never invokes a cross-encoder at all.** Instrumenting `rank()` across a real search returned **zero invocations**, and the code path explains why:

- CMF has exactly one retrieval call site: `graphiti.search(query)` in `memory_graphiti.py:467`.
- That method resolves to the `EDGE_HYBRID_SEARCH_RRF` recipe (or `EDGE_HYBRID_SEARCH_NODE_DISTANCE` when given a center node). Neither uses `EdgeReranker.cross_encoder`.
- Nothing in CMF calls `graphiti.search_()`, which is the entry point whose default *is* `COMBINED_HYBRID_SEARCH_CROSS_ENCODER`.

**What a cross-encoder would do.** Retrieval here is two-stage. Stage one is recall-oriented and cheap: BM25 keyword matching plus cosine similarity over embeddings. Those use a *bi-encoder* — query and document are embedded separately and compared by vector distance, which is fast because document vectors are precomputed, but the model never sees the query and the document together. Stage two would be precision-oriented: a *cross-encoder* takes `(query, passage)` as one joint input and scores relevance directly, so it can register negation, qualifiers, and which entity a question is actually about. It is markedly more accurate and markedly more expensive — one forward pass per candidate, nothing precomputable.

Graphiti's `reranker` enum blurs this: `rrf`, `node_distance`, `episode_mentions` and `mmr` are cheap structural or statistical reorderings, while `cross_encoder` is the only one that runs a model. CMF uses `rrf` — reciprocal rank fusion, which merges the BM25 and cosine rankings by rank position with no model involved.

**So is passthrough a problem?** Not today — it is a true no-op, not a degradation, because the only ranking CMF performs is RRF and that happens before the cross-encoder would be consulted. It also means **D3 is off the critical path**: no need to pull torch and ~2.2 GB of BGE weights to complete this migration.

It becomes a real decision when reranking is switched on, which MS7's context-assembly work is the natural occasion for. Two things follow:

- `PassthroughReranker` now **logs a warning the first time it is actually invoked**. Without it, flipping a search config to `cross_encoder` would silently return the input order with plausible-looking scores, and the only symptom would be retrieval quality that never improved — indistinguishable from a bad model or a bad query set, which is exactly the confusion MS7's evaluation cannot afford.
- When that day comes, `CMF_RERANKER=bge` is the switch, and `uv add sentence-transformers` the prerequisite.

**D4 — Gemini escape hatch.** ✅ *Resolved: keep it.*

`CMF_LLM_PROVIDER` (`gemini` | `local`) selects between client sets inside `create_graphiti`. Split it into `CMF_LLM_PROVIDER` + `CMF_EMBED_PROVIDER` from the start — §9 may well land on the hybrid (local embeddings, Gemini extraction), and retrofitting that split later means redoing Phase 1.

**D5 — MS4a cost/privacy gate.** ✅ *Resolved: no concern about Alex having access.*

The recorded MS4a decision was "Gemini-only for now, no filtering, hard free-tier rate cap." The cost half is simply superseded — local inference has no per-call cost, so the ledger stops being a spend control and becomes a throughput control (§7). No privacy action needed; noting only that the decision record should be updated so it doesn't read as still-current.

---

## Phase 0 — Network path — **complete**

CMF runs on the Mac; LM Studio runs on the Spark. LM Studio is bound to `0.0.0.0:1234` on a **directly-routable public IP with no firewall you control** — do not point CMF at `128.171.121.85`. Everything goes through the Tailscale-backed SSH tunnel.

- [x] **Tunnel open** — already running, and has been for over four days:
```
PID 1398   ELAPSED 04-03:40:11   ssh -N -L 12345:127.0.0.1:1234 spark
```
`lsof -nP -iTCP:12345 -sTCP:LISTEN` shows this ssh process as the *only* thing bound to 12345 (both `127.0.0.1` and `[::1]`). Nothing else was ever on that port — had anything been, ssh would have failed to bind and the forward would be dead.

- [x] **`/v1/models` reachable through the tunnel** — 10 models returned, matching the inventory in §1.
```bash
curl -sS http://127.0.0.1:12345/v1/models | python3 -m json.tool
```
- [x] **Embeddings verified through the tunnel → `768`**, which fixes `EMBEDDING_DIM`:
```bash
curl -sS http://127.0.0.1:12345/v1/embeddings -H 'Content-Type: application/json' -d '{"model":"text-embedding-nomic-embed-text-v1.5","input":"dimension probe"}' | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["data"][0]["embedding"]))'
```

- [x] **Tunnel hardened against sleep/half-open.** `~/.ssh/config` now carries the keepalives, confirmed effective via `ssh -G spark` (`serveraliveinterval 30`, `serveralivecountmax 3`, `exitonforwardfailure yes`, `tcpkeepalive yes`):
```
Host spark
    HostName 100.123.43.85
    User todd
    ServerAliveInterval 30
    ServerAliveCountMax 3
    ExitOnForwardFailure yes
```
Tunnel restarted to pick them up (PID 13398), re-verified end to end: 10 models, 768-dim embeddings. `autossh -M 0 -N -L 12345:127.0.0.1:1234 spark` or a launchd `KeepAlive` agent remains optional for auto-restart — the keepalives make a dead tunnel *fail fast* rather than hang, but they do not respawn it.

Port `12345` is the one already in use. Nothing is listening on the Mac's `11434` (no local Ollama), so there is no collision either way; `12345` keeps the Spark endpoint visibly distinct from anything local.

---

## Phase 1 — Config plumbing — **complete**

Landed with **zero behaviour change**: both provider switches stay on `gemini`, the graph stays `mem-fabric-gemini`, and `EMBEDDING_DIM` stays 1024. The new variables are inert until Phase 2 supplies the code that reads them.

### The import-order hazard, found and fixed

`EMBEDDING_DIM` in `.env` **would have done nothing.** graphiti_core freezes it into a module constant at import time:

```python
# graphiti_core/embedder/client.py
EMBEDDING_DIM = int(os.getenv('EMBEDDING_DIM', 1024))
```

and `server/mcp.py` imports `server.memory` (→ graphiti_core) at line 29, while `load_config()` does not run until line 45. `memory_graphiti.py`'s own `load_dotenv()` sits *after* its graphiti imports. Demonstrated before the fix:

```
graphiti EMBEDDING_DIM at import = 1024
after load_dotenv, os.environ EMBEDDING_DIM = None
graphiti constant is now still = 1024 (constants do not re-read)
```

The consequence would have been silent: `graphiti_core.search` falls back to `[0.0] * EMBEDDING_DIM` for a zero query vector, which on a 768-dimension graph is the wrong width, with nothing raised to say so.

- [x] **`server/__init__.py` created** with `load_dotenv(override=False)`. It is the only chokepoint that runs before any `server.*` submodule, and therefore before graphiti_core. This turns `server` from a namespace package into a regular one — intentional, and consistent with pyproject's `packages = ["server"]`.
- [x] `override=False` is load-bearing, not stylistic: `tests/conftest.py` sets `FALKORDB_DATABASE=cmf_test` before importing any `server` module to keep test writes out of the production graph. `override=True` would have silently clobbered that and pointed the suite at the real graph. Verified both directions — preset survives, absent preset still reads `.env`.

### Config

- [x] **`CMFConfig` extended** with `llm_provider`, `embed_provider`, `local_base_url`, `local_api_key`, `local_llm_model`, `local_embed_model`, `local_structured_mode`, `embedding_dim`, plus `llm_is_local` / `embed_is_local` helpers.
- [x] **Unknown provider values raise** rather than defaulting. A typo like `lcoal` silently falling back to `gemini` would send extraction to a metered API the operator believed they had left, and the only symptom would be quota burn.
- [x] **`memory_enabled` widened.** It previously required `GEMINI_API_KEY` unconditionally, so a fully local deployment would have reported memory disabled and unregistered the memory tools at startup. It now follows the two switches, and consults *both* — the hybrid needs the credentials of both halves.
- [x] **`.env.example` and `.env` documented**, with the flip-together warning on `EMBEDDING_DIM` / `FALKORDB_DATABASE`.

### Tests

- [x] **`tests/test_spark_config.py` — 17 tests.** `CMFConfig` had no coverage at all before this.
- [x] **Suite result: 293 passed, 6 skipped, 23 subtests passed, 0 regressions.** The 7 remaining failures all require live Gemini and fail today on exhausted daily embedding quota (above), not on anything Phase 1 touched. Verified by running the suite with the credential removed so those paths fail instantly: **27.5s** versus **15+ minutes** with live calls.
- [x] **Live-Gemini tests should be marked and skippable.** Seven tests call the real API: `test_step6_mcp_tools` (2), `test_step6b_proposals` (1), `test_step7_import_memories` (1), `test_step8_edit_memory` (3). They make the suite ~35x slower, cost real quota, cannot run in CI without a key, and today fail for quota reasons rather than code reasons. `conftest.py` already isolates FalkorDB writes into `cmf_test`; nothing isolates Gemini. A `live` marker plus `-m "not live"` by default is the cheap fix, and it should land before Phase 7's A/B work, which needs many repeated runs.
- [x] One fixture bug worth recording: `load_config()` calls `load_dotenv()`, and python-dotenv only declines to override variables that are *present* — so a `monkeypatch.delenv` of `GEMINI_API_KEY` was immediately repopulated from the real `.env`, and the "missing credential" cases were testing the opposite of what they claimed. The fixture now stubs `load_dotenv`, and `test_fixture_actually_isolates_from_dotenv` guards against the stub being dropped.

### Found while verifying: the rate limiter does not govern embeddings

Running the seven live-Gemini tests to close out Phase 1 surfaced this:

```
429 RESOURCE_EXHAUSTED
Quota exceeded for metric: generativelanguage.googleapis.com/embed_content_free_tier_requests
limit: 1000, model: gemini-embedding-1.0
```

`server/core/rate_limiter.py` exists to "guarantee CMF never places a Gemini call that would exceed the free tier's per-model RPM/RPD ceilings". It does not do that for embeddings:

```
chain actually reserved against : ['gemini-3.5-flash-lite', 'gemini-3.1-flash-lite']
embedder in KNOWN_MODEL_BUDGETS : True   (rpm=100, tpm=30000, rpd=1000)
embedder in DEFAULT_MODEL_CHAIN : False
models tracked in today's ledger: ['gemini-3.5-flash-lite', 'gemini-3.1-flash-lite']
```

The budget for `gemini-embedding-001` is *defined* but never *reserved against*, because `reserve()` only walks `DEFAULT_MODEL_CHAIN`. Every `add_episode` and `search` embeds, entirely unmetered, and today that quietly exceeded 1,000/day — the local ledger showed plenty of headroom the whole time because it was tracking the wrong models.

`create_graphiti`'s docstring states the assumption explicitly: *"the embedder always uses gemini-embedding-001, which has ample free-tier headroom and isn't part of the rate-limited fallback chain."* The headroom claim is what failed. 1,000 embedding requests/day is roughly 200 episodes at 4-6 calls each — the same order as the 500 RPD generation ceiling, not comfortably above it.

Two consequences:

- **Independent of this migration**, the Gemini path needs the embedder metered — either added to the reservation chain or given its own ledger, since it has a different ceiling and no fallback sibling to fall through to.
- **It strengthens the migration's case.** Embedding is the highest-volume, lowest-judgment call CMF makes, and it is the one that hit the wall first. `nomic-embed-text-v1.5` on the Spark has no quota at all, which is why `CMF_EMBED_PROVIDER=local` is worth landing even if extraction stays on Gemini.

### Note for Phase 6

`EMBEDDING_DIM`, `FALKORDB_DATABASE` and the two provider switches must move **together**. Setting `EMBEDDING_DIM=768` while still pointed at `mem-fabric-gemini` would write 768-dimension vectors into a 1024-dimension graph.

---

## Phases 2 & 3 — Client swap and LM Studio proxy — **complete, verified end to end**

A full `add_episode` + `search` round-trip ran against GLM-4.7-Flash and nomic-embed on the Spark, writing 768-dimension vectors into a throwaway FalkorDB graph. Search returned sensible facts:

```
add_episode OK in 44.5s
search OK in 0.2s -> 5 edges
   - Alex Nano agreed to keep GLM-4.7-Flash resident in LM Studio on the DGX Spark
   - Alex Nano agreed to ... leave JIT loading enabled for experimental models
   - Todd reviewed the Context Memory Fabric rate limiter against his AI Studio dashboard
```

### Correction: Mode A, not Mode B

Rev 3 recommended Mode B (`json_object` → `text`, schema in the prompt) on the strength of hand-written probe prompts. **End-to-end testing against graphiti's actual prompts proved that wrong.** Given `extract_nodes`, GLM returns the *schema itself*:

```
pydantic_core.ValidationError: 1 validation error for ExtractedEntities
extracted_entities  Field required
  input_value={'$defs': {'ExtractedEnti...ties', 'type': 'object'}
```

Constrained decoding cannot fail that way — the grammar makes echoing the schema structurally impossible. `DEFAULT_LOCAL_STRUCTURED_MODE` is therefore `json_schema`.

**This makes the proxy required, not a convenience.** Mode A's own failure is GLM leaving `content` empty, and it fires on every call:

```
DEBUG server.providers.lmstudio_client: Promoting 43 chars of reasoning_content into an empty content field.
content          : '{"entities": ["Todd", "Alex", "DGX Spark"]}'
reasoning_content: '{"entities": ["Todd", "Alex", "DGX Spark"]}'
```

The cost is Gemma-4-26B-A4B: its control-token leak is triggered by constrained decoding specifically, so it is out as an extraction model. Mode B remains selectable via `CMF_LOCAL_STRUCTURED_MODE` for a non-reasoning model that might prefer it.

### Measured: the per-episode call mix, and why the earlier estimate was wrong

One `add_episode` on this episode issued:

| | count |
|---|---|
| `chat/completions` (extraction) | **3** |
| `embeddings` | **~20** |

Both numbers correct earlier guesses, in opposite directions. The §"×3 multiplier" section estimated 4-6 LLM calls from a source trace — the real figure is **3**, so the rate limiter's `DEFAULT_CALLS_PER_OPERATION = 3` was accurate after all and the proposed raise to 6 is unnecessary. More importantly, **nobody was counting embeddings**, and they outnumber LLM calls roughly 7:1 because every entity name and edge fact is embedded individually.

**This is what exhausted the Gemini quota, and it reframes the migration's economics.** At ~20 embeddings per episode against a 1,000/day free-tier embedding ceiling, the Gemini path sustains roughly **50 episodes/day** — not the ~160/day implied by the 500 RPD generation ceiling. The binding constraint was never extraction; it was embedding, unmetered and uncounted.

Consequences:

- `CMF_EMBED_PROVIDER=local` is the single highest-value part of this migration and is worth landing **on its own**, ahead of any extraction change. nomic-embed has no quota.
- The full 1,243-row backlog would need ~25,000 embedding calls. On Gemini that is 25 days. On the Spark it is free.
- Phase 4's "raise `DEFAULT_CALLS_PER_OPERATION` to 6" is **withdrawn** — measurement says 3 is right. Metering the embedder stands, and is now clearly the urgent half.

### Built

- **`server/providers/lmstudio_client.py`** — `LMStudioCompatClient`. Request side rewrites `json_object` → `text` (LM Studio rejects `json_object` outright). Response side promotes `reasoning_content` into an empty `content`, but **only when it parses as JSON**, so Mode B's genuine chain-of-thought prose is never fed to graphiti's `json.loads`. Duck-types `AsyncOpenAI` rather than subclassing it, since graphiti only ever touches `.chat.completions.create`.
- **`server/providers/reranker.py`** — `PassthroughReranker` (default) and BGE selection via `CMF_RERANKER`, with a clear error naming the install when `sentence-transformers` is absent. Documents why `OpenAIRerankerClient` is unusable here: it scores via `logit_bias` on hardcoded OpenAI BPE ids that mean unrelated tokens under GLM's tokenizer.
- **`create_graphiti` split** into `_build_llm_client` / `_build_embedder` / `_build_cross_encoder`, each branching on its own switch. `resolve_llm_model` picks the model from the right source — the rate limiter's chain is a list of *Gemini* ids and is meaningless on the local path.
- **`assert_embedding_width()`** — every layer here fails silently on a width mismatch (OpenAIEmbedder slices rather than raises, FalkorDB accepts anything, graphiti falls back to a frozen module constant), so a graph can be built entirely from wrong-width vectors with nothing raised and only poor recall as a symptom. Verified live: returns 768, raises when told to expect 1024.
- **`tests/test_lmstudio_client.py` — 17 offline tests**, running on recorded response shapes so the suite needs no Spark.
- **Verified stored width is 768** in the probe graph, and the Gemini path constructs byte-identically to before.

### Also landed: `live` test marker

Seven tests call the real Gemini API. They are now marked and excluded by default via `pyproject.toml`'s `addopts = -m "not live"`.

- Default: **310 passed, 6 skipped, 7 deselected, 26s**, zero quota.
- Opt in with `uv run pytest -m live`; everything with `-m ""`.
- Markers verified to work on the `unittest.IsolatedAsyncioTestCase` classes in both directions.

Before this, a full run took **15+ minutes**, almost entirely blocked on network and retry backoff, and could not run at all on an exhausted-quota day.

---

## Phase 4 — Rate limiter — **complete**

Two halves: switch the ledger off where it protects nothing, and switch it on where it was missing.

### Local path: unmetered, not fake-unlimited

- [x] `get_default_rate_limiter()` returns a limiter built with `unmetered=True` when `CMF_LLM_PROVIDER=local`, chained to `CMF_LOCAL_LLM_MODEL`.
- [x] `reserve()` returns immediately, `seconds_until_headroom()` returns `0.0`, `reserve_model()` is a no-op, and **no ledger file is touched at all**.

Implemented as a flag rather than as ceilings large enough never to bind, because the latter still loads, mutates and re-saves the JSON ledger on every reservation — a 1,243-episode backfill issues thousands. Verified by a test asserting the state file is never created.

The object still exists and still raises the same exception type, so every caller's `GeminiQuotaExhaustedError` handling stays wired for a switch back to Gemini. The constructor's unknown-model `ValueError` is skipped only in unmetered mode; the metered path still rejects a model with no known budget.

### Gemini path: the embedder is now metered

The gap that caused today's failures. `gemini-embedding-001` had a budget in `KNOWN_MODEL_BUDGETS` but was absent from `DEFAULT_MODEL_CHAIN`, so `reserve()` never debited it.

- [x] **`reserve_model(model, calls)`** — reserves against one *named* model with no chain fallback. `reserve()`'s "which model should I use" question is meaningless for the embedder: there is one embedding model and nothing to fall through to, so the only useful answers are yes and `GeminiQuotaExhaustedError`.
- [x] **`MeteredEmbedder`** (`server/providers/metered_embedder.py`) wraps the Gemini embedder. Metering has to happen there because CMF has no call site to guard — graphiti calls `embedder.create()` from inside `add_episode` and `search`.
- [x] **Debits per input, not per batch.** `GeminiEmbedder` forces `batch_size = 1` for `gemini-embedding-001`, so a batch of N is N HTTP requests and N quota debits. Counting a batch as one call is precisely the undercount that hid this.
- [x] **Reserves before delegating**, so a refused caller has made zero API requests — the same contract `get_graphiti_for_operation()` already gives generation.
- [x] **Not applied to the local embedder.** nomic-embed has no quota; a layer that only ever says yes is noise.

### Found while testing: `status()` had the same blind spot

The operator-facing `status()` reported only models **in the chain**. Since the embedder is deliberately not in the chain, an operator checking quota saw no embedding usage whatsoever — right up to a live 429 on a ceiling nothing was reporting. It now reports the union of the chain and everything the ledger has recorded, with an `in_chain` flag, and returns `{"unmetered": true}` on the local path.

### Also

- [x] **`"model unloaded"` added to `TRANSIENT_ERROR_MARKERS`.** LM Studio's Auto-Evict kills in-flight requests with `{"error": "Model unloaded."}` — observed live. It is not an HTTP 5xx and matched no existing marker, so it would abort a batch run that one retry would have carried through.
- [x] **`inter_call_delay` is provider-aware.** `default_inter_call_delay()` resolves to 3.5s on Gemini (politeness toward a shared 15 RPM ceiling) and 0.2s locally. Resolved at call time rather than baked into three signatures, so flipping the provider takes effect without edits and an explicit caller value — including the `0` the tests pass — still wins. Across 1,243 episodes the old default would have added over an hour of pure sleeping.
- [x] **`DEFAULT_CALLS_PER_OPERATION` left at 3**, per the measurement that withdrew the proposed raise.
- [x] **`tests/test_spark_rate_limiter.py` — 18 tests.** Suite: **330 passed, 24s**.

### Caveat for the first day back on Gemini

The ledger is CMF's own count, not Google's. It currently records **zero** embedding usage today because metering did not exist while today's ~1,000 embedding calls were being made. If anything runs on the Gemini path before the Pacific-midnight reset, the ledger will believe there is a full 1,000-call budget available while Google's counter is already exhausted, and calls will 429 despite a clean local reservation. From tomorrow the two agree.

---

## Phase 5 — Reasoning-episode policy — **complete**

`ReasoningEpisodePolicyV1` bypassed Graphiti entirely, calling `google.genai` directly, so Phases 2-3 did not touch it.

- [x] **`_local_generate(model, prompt)`** — same `GenerateFn` contract, routed through the Phase 3 `LMStudioCompatClient` rather than a bare `AsyncOpenAI`, so the `json_object`→`text` rewrite and the `reasoning_content` rescue apply here too. In `json_schema` mode it supplies `_EPISODES_SCHEMA`, a permissive schema mirroring the shape the prompt already describes and `_to_episode` already reads.
- [x] **`_select_generate_fn()`** picks it at construction from `CMF_LLM_PROVIDER`. An injected `generate_fn` still wins, so every existing fake keeps working.
- [x] **Transient handling consolidated.** The policy's private `_TRANSIENT_MARKERS` tuple is gone; it now defers to `server.core.rate_limiter.classify_transient_error`, which means LM Studio's `{"error": "Model unloaded."}` is retried here too. Gemini's `"try again later"` phrasing was carried into the shared set so nothing was lost. Quota errors stay deliberately non-retryable — `evaluate_window` re-raises them as `GeminiQuotaExhaustedError` so the pipeline stops clean.
- [x] **Version bumped 0.2 → 0.3.** A different extraction model is a different policy: GLM-derived and Gemini-derived episodes must not share a version bucket or the Phase 7 comparison has nothing to compare.

### The version bump was a trap, and it needed three more fixes

`"0.2"` was a hardcoded **default argument** in three production call sites — `review_queue()`, `tier1_review_queue()` and `mark_superseded_by_reasoning()` — and the review CLI had **no `--policy-version` flag at all**. Bumping the policy alone would have silently emptied the review queue with no way to reach the 1,243 rows sitting at 0.2.

- [x] **`REASONING_POLICY_VERSION` is now the single source of truth**, imported by all three (verified free of import cycles). A literal repeated across four modules is precisely how a bump stops matching rows.
- [x] **`--policy-version` added** to `stats`, `queue` and `export`, defaulting to the current version.
- [x] **An empty queue now explains itself.** It distinguishes "no rows at all at this version" (prints the versions that do have rows, and the flag to use) from "rows exist but the backlog is worked through" — which is a finished queue, not a missing one. The first draft conflated them and reported the 1,243-row version as empty.

Verified against the real journal:

```
$ python -m server.review.cli queue --tier 1
0 pending across 0 buckets
Note: no rows at all at policy version 0.3; other versions: 0.2 (1243 queued), 0.1 (66 queued)
      Review them with:  --policy-version 0.2

$ python -m server.review.cli queue --tier 1 --policy-version 0.2
0 pending across 0 buckets (301 already reviewed, 942 tier-2 not in scope)
```

- [x] **Nine existing tests failed on the bump** — fixtures built rows at `"0.2"` and queried with the new default. They were the canary for exactly this coupling. Their fixtures now use `REASONING_POLICY_VERSION`, so they are version-agnostic and will not break on the next bump.
### Verified live, and it caught a regression

A real window through GLM-4.7-Flash extracted correct episodes with correct evidence linking:

```
[finding]  conf=0.9  evidence=['e1','e2','e3']
[decision] conf=0.9  evidence=['e3']
```

But both came back with **`thread_key=None`**. Checked against the real corpus, that is a regression, not a quirk: Gemini populated `thread_key` on **1,242 of 1,243** rows. The field is load-bearing — referenced 58 times across 10 modules, it is what `consolidation/threads.py` matches conversations on and what `review/projects.py` buckets by — so nulls there quietly degrade thread continuity and project grouping.

Cause: the prompt describes `thread_key` as nullable, so the permissive schema allowed null, and GLM took the option wherever the grammar permitted it. **`thread_key` is now required and non-nullable in `_EPISODES_SCHEMA`**; the genuinely optional fields (`status`, `rationale`, `alternatives`, `driving_question`, `thread_title`) stay nullable, since requiring everything would push the model to invent values. Re-run:

```
[finding] conf=1.0  thread=gemini-free-tier-consolidation  status=resolved
```

**Note for Phase 7:** the same window yielded 2 episodes on one run and 1 on the next, at temperature 0.2. Extraction is non-deterministic, so the quality A/B needs several samples per input rather than one — a single-sample comparison would mostly measure variance.

- [x] **`tests/test_spark_reasoning_policy.py` — 22 tests**, including a guard that `thread_key` stays required. Suite: **352 passed, 24s.**

---

## Phase 6 — Fresh graph and rebuild — **complete**

**295 of 295 promoted, 0 failed**, into `mem-fabric-local` on Spark-local models. Roughly 3.2 hours at 38.6s/episode.

The 295 are **the MS6a tier-1-approved set** — reviewer `todd`, 2026-09-08, 295 approved / 6 rejected of the ~301 episodes `reasoning_kind` routed to tier 1 (`reviews` table). The same 295 were promoted into `mem-fabric-gemini` earlier (285 succeeded + 24 transient-failed). So `mem-fabric-local` is not raw output — it is the human-reviewed tier-1 corpus re-extracted and re-embedded on GLM-4.7-Flash + `nomic-embed`, which is exactly what makes it comparable to `mem-fabric-gemini` in Phase 7.

```
mem-fabric-local    295 Episodic · 559 Entity · 567 RELATES_TO · 768-dim vectors
mem-fabric-gemini   337 Episodic · 337 Entity · 187 RELATES_TO · 1024-dim  (untouched)

promotions ledger
  mem-fabric-gemini   285 succeeded, 24 failed
  mem-fabric-local    295 succeeded
```

Both graphs are live in the same ledger — the composite primary key from earlier makes the A/B repeatable rather than one-shot.

### What was done differently from the written plan

- **The graph was renamed, not replaced.** `memory-fabric` → `mem-fabric-gemini` via a Redis `RENAME` (verified on a throwaway graph first; FalkorDB moves the companion `telemetry{...}` key itself). Data intact: 337/337/187, embeddings still 1024-wide.
- **The `promotions` ledger was never cleared.** The plan called for `DELETE FROM promotions` because the old single-column key would otherwise skip every previously-promoted row. Widening the key to `(memory_id, graph_name)` made that unnecessary — nothing was destroyed, and the Gemini promotion history is fully intact.
- **The rename left 22 rows pointing at `memory-fabric`.** Those would have been re-promoted into the Gemini graph as duplicates. Checked for collisions (none), then re-pointed.
- **`.env` was left on `mem-fabric-gemini`.** The run took its settings from its own process environment, so the MCP server and concurrent sessions kept working against Gemini throughout. Flipping is a separate, deliberate step.

### Health

- **0 failures.** 4 empty responses across 295 episodes, all recovered by graphiti's retry.
- **0 model evictions.** The `"model unloaded"` marker added in Phase 4 never had to fire — GLM stayed resident for the full run.
- **Every entity has a 768-dimension embedding**; none missing, none truncated.
- Recall works against the new graph and returns relevant facts.

### Two observations for Phase 7

1. **The local graph is much denser.** 559 entities and 567 edges from 295 episodes, versus 337 and 187 from 337 episodes on Gemini — roughly 2x the entities and 3x the edges per episode. This is *not* self-evidently better. It could be richer extraction or it could be over-extraction that dilutes retrieval, and only a graded query set can say which.
2. **155 edges were discarded** with `"Source/Target entity not found in nodes for edge relation"` — GLM proposed relationships whose endpoints it had not extracted as entities, so graphiti dropped them. That is roughly one dropped edge every two episodes, silently. Worth measuring against Gemini's rate; it may be a real quality gap or simply a different extraction style.

**Rate observations were noisy.** Per-episode time swung between 24s and 58s depending on episode content, not graph size — cumulative-average ETAs swung with it and repeatedly suggested trends that were not there.

---

## Phase 7 — Verification

### First-look inspection of `mem-fabric-local` (2026-09-09)

A visual pass over the rebuilt graph in the FalkorDB browser, before any graded scoring. Impression: **GLM's entities and edges look noticeably lower-quality than Gemini's** — the density observation from Phase 6 now has specific symptoms attached. Each item below is either an **A/B scoring input** (the graded query set has to measure it) or a **concrete fix** with a location. None of it is yet quantified against the retained Gemini graph — that is the Phase 7 job.

Counts below are from `GRAPH.RO_QUERY mem-fabric-local` on 2026-09-09; the Gemini-graph comparison is the Phase 7 job and is not yet done.

1. **Generic-pronoun entities.** `user` and `The user` exist as two separate `Entity` nodes (2 nodes total). Where either refers to **Todd as the actual actor** — a real decision or action he took — they should resolve to a single canonical `Todd` node. **But not always:** where the text is generic or hypothetical ("if a user does X, then Y"), the pronoun is not Todd and arguably should not be a personal entity at all. So this is context-sensitive resolution, not a string replace — the fix has to read the surrounding claim. *Fix candidates:* an extraction-prompt instruction to name the first-person participant explicitly when it is the corpus owner and to leave generic/hypothetical actors unpromoted; or a promotion-time canonicalization that keys off whether the episode records a concrete action. Measure first: how many pronoun nodes exist, how many are Todd-as-actor vs. generic, and Gemini's rate (Gemini appears to have named people directly).
2. **Self-referential `RELATES_TO` edges.** 8 edges whose source and target are the same node (`Interlock`→`Interlock` among them). A node related to itself carries no information and pollutes neighbourhood expansion. *Fix:* drop edges where `source == target` at the promotion/extraction boundary, before the write. Check whether Gemini's extraction produced any at all.
3. **Redundant parallel edges — both relation types.** `RELATES_TO`: 50 ordered node pairs carry 140 edges between them (the `Mac`↔`Windows 11` / `Mac`↔`VMware` / `Mac`↔`virtual machine` cluster). `MENTIONS`: 7 episode↔entity pairs carry 18 edges where 7 would do. Distinct facts on parallel `RELATES_TO` edges are legitimate; exact- or near-duplicate ones are not, and duplicate `MENTIONS` never are. *Fix:* dedupe `MENTIONS` on `(episode, entity)`; dedupe/merge `RELATES_TO` on `(source, target, relation, fact)` with near-duplicate fact collapsing. Quantify both rates on the Gemini graph.
4. **Paraphrase-spam and template-token leakage in `RELATES_TO` facts.** Worse than item 3 and not mechanical. The single pair `legal strategy`→`board` carries **9** edges, and the fact strings show GLM (or graphiti's edge-dedupe step) misbehaving: near-identical paraphrases, several *self-describing the defect* — `"...a duplicate legal strategy to force the condo board..."`, `"...a typoed legal strategy..."`, `"...the prevous condo board..."` — and two with prompt-template placeholders leaked into the stored fact: `"...approve an SOURCE_ENTITY_0"` and `"The_CURRENT_MESSAGE is evaluating a legal strategy..."`. Needs a root-cause pass in Phase 7: is this GLM emitting the same fact N times with perturbations, graphiti's dedup prompt echoing its own scaffold, or test residue written into the live graph? The `SOURCE_ENTITY_0` / `The_CURRENT_MESSAGE` tokens are definitely not real content. This one alone is a strong signal that GLM extraction is not yet at parity.
5. **Opaque episode names.** Episodes are named `promoted_<YYYYMMDD>_<digits>` (e.g. `promoted_20260830_29234680`). That string is unusable for browsing or citing. A semantic slug — **`<source>-<topic>-<NNN>`**, e.g. `chatgpt-astrophotography-001` — would make the graph navigable and give `recall()` provenance a human-readable handle. *Fix:* the `name=` argument in the promotion path (`promote_reviewed` → `remember()`); derive `topic` from the reasoning episode's `thread_key` or `thread_title`, `source` from the originating harness, `NNN` a per-(source,topic) counter. Low risk, high readability payoff — worth doing before the full 1,243-row backlog run.
6. **Entity granularity — fragments instead of real-world entities.** For `promoted_20260830_29234680` ("remove the separate Morocco zones toggle layer and integrate microclimate guidance into the assumptions section…"), GLM extracted `Morocco zones toggle layer`, `assumptions section`, `microclimate guidance`, `text overlays`, `map layers`, `HTML` — UI-implementation noun phrases lifted from the sentence. The salient entities are **`Spain`** and **`Morocco`** (the astrophotography trip's actual locations), which GLM did not extract at all. This is the core of the "entities aren't as good as Gemini" impression: GLM chunks the surface text rather than identifying the durable referents. *This is an extraction-prompt problem*, and the most important one to fix if GLM stays the extraction model — the A/B scoring should weight "extracted the real entities" heavily.

**Sequencing:** items 2, 3 and 5 are mechanical and can be fixed independently of the model choice. Items 1, 4 and 6 are extraction-quality and feed directly into the D1 model decision — if the prompt can't be made to fix them on GLM, that is the argument for the hybrid (Gemini extraction, local embeddings) or for scoring Qwen3.5 / `qwen3-coder-30b` on the same inputs.

Functional:

- [ ] `remember()` round-trips — write an episode, confirm entity + edge extraction landed.
- [ ] `recall()` returns it with a sane similarity score (not the near-uniform scores that indicate a broken vector space).
- [ ] `search_wiki` unaffected — file-based, shares no code with this.
- [ ] `get_context` returns both memory and knowledge hits.
- [ ] Full suite green: `uv run pytest`. Land or stash the 11 already-modified working-tree files first so failures are attributable.

Throughput — **measured, same prompt, cold and warm:**

```
model                        wall    completion   tok/s    285 eps x3    1,243 eps x3
zai-org/glm-4.7-flash        48.4s      2,916     60.2       ~11.5 h        ~50 h
zai-org/glm-4.7-flash warm   53.5s      3,578     66.8       ~12.7 h        ~55 h
qwen/qwen3-coder-30b         18.7s      1,040     55.8        ~4.4 h        ~19 h
qwen/qwen3-coder-30b warm    14.5s      1,060     73.0        ~3.4 h        ~15 h
```

Three things fall out of this:

1. **The Spark generates at ~55–73 tok/s regardless of model.** That is the hardware ceiling for these Q4 GGUFs at 8192 context. Wall-clock is therefore almost purely a function of *how many tokens the model emits* — not of model size.
2. **The reasoning tax is ~3.4x.** GLM spends ~3,000–3,600 tokens where `qwen3-coder-30b` spends ~1,050 on the identical prompt, and nearly all of the difference is thinking. Per-call variance is real too: GLM's warm run emitted 23% more tokens than its cold run, so treat these as within a factor, not to the minute.
3. **Model load time is not the dominant cost.** Warm ≈ cold for GLM (53.5s vs 48.4s — the warm run was *slower*, because it emitted more). This softens the Auto-Evict concern for throughput: an eviction costs a reload, not a catastrophe. The eviction risk that matters is the hard `"Model unloaded."` mid-request failure, not latency.

**Against the Gemini free tier's ~160 episodes/day** (285 → ~2 days, 1,243 → ~8 days), both models win. But the choice is no longer free:

| | GLM-4.7-Flash | qwen3-coder-30b |
|---|---|---|
| 285-episode re-promotion | ~12 h (overnight) | ~3.5 h (an afternoon) |
| 1,243-row backlog | **~50 h — over two days of continuous inference** | ~15 h |
| Extraction richness on the test prompt | 17 entities / 14 edges | not yet scored |

**Recommendation:** keep GLM for the Phase 6 re-promotion — 285 episodes overnight is acceptable and extraction quality is what that run is for. But **add `qwen3-coder-30b` to the Phase 7 quality A/B**, not just as an escape hatch. If it scores close to GLM, it is the right model for bulk backlog work, and the 1,243-row difference (15 h vs 50 h) is large enough to matter.

### The ×3 multiplier — what it is, and where it applies

Worth separating two things the plan had been conflating:

- **The ×3 is a quota *reservation*, not a cap on calls.** `DEFAULT_CALLS_PER_OPERATION = 3` tells the rate limiter to debit 3 units of Gemini free-tier budget per operation. It never limits, batches or delays anything.
- **It does apply during ingest.** Phase 6's promotion run calls `remember()` → `get_graphiti_for_operation()` → `reserve()`, so under Gemini every promoted episode debits 3. That is why the free tier yields ~160 episodes/day.
- **After Phase 4 it is inert.** With local models on effectively-infinite budgets, `reserve()` always succeeds and `seconds_until_headroom()` is always 0. The reservation has **zero effect on the local Phase 6 wall-clock.**

What *does* drive Phase 6 wall-clock is the **real** number of LLM calls per `add_episode`, which is not 3. Tracing graphiti 0.29.3 for CMF's exact call shape (`source=EpisodeType.text`, no `entity_types`, no community update):

| Stage | LLM calls |
|---|---|
| node extraction | 1 |
| node dedupe / resolution | 1 |
| edge extraction | 1 |
| edge dedupe (`resolve_extracted_edges`) | **1 per extracted edge**, fanned out concurrently |
| `_extract_entity_attributes` | **0** — returns `{}` immediately when `entity_type is None`, which is CMF's case |
| `_extract_entity_summaries_batch` | 1 per flight of `MAX_NODES = 30` → 1 for typical episodes |
| community update | 0 — CMF doesn't request it |

So **4–6 calls in the common case**, with the edge-dedupe fan-out as the variable term. Two consequences:

1. **The throughput table above is optimistic by roughly 1.3–2x.** GLM's 285-episode run is more realistically 16–24 h than 11.5 h, and the 1,243-row backlog 70–100 h. This strengthens the case for scoring `qwen3-coder-30b` as a bulk-work candidate rather than a reserve.
2. **The Gemini ledger has been under-reserving.** `DEFAULT_CALLS_PER_OPERATION`'s docstring calls 3 "a deliberate over-estimate," but the real count is 4–6. On the Gemini path the local ledger can therefore drift *ahead* of Google's real counters — the opposite of the intended safety margin. Worth raising to 6 regardless of this migration.

### Deferred measurements

Not blocking the plan; do these before or during Phase 7 rather than now.

- [ ] **Concurrency behaviour — the biggest open variable.** Graphiti fans the edge-dedupe calls out through `semaphore_gather` at `SEMAPHORE_LIMIT = 20`, assuming a backend that serves requests in parallel. **LM Studio serving a single model instance may serialize them.** If it does, 14 concurrent edge-dedupe calls cost 14× wall-clock rather than ~1×, and every estimate here is badly low. Test cheaply: fire N concurrent completions at the endpoint and check whether total wall-clock is flat or linear in N.
- [ ] **Warm vs cold, properly.** The single warm/cold pair here is not enough to separate load time from generation variance — GLM's "warm" run was slower because it emitted 23% more tokens. Needs repeated runs.
- [ ] Score `qwen3-coder-30b` extraction quality against GLM and Gemini on the same 20 statements.
- [ ] Re-time after Alex pins the models and raises the context — both change these numbers.
- [ ] Time a real `remember()` end to end, which settles the 4–6 estimate empirically.
- [ ] If throughput needs improving, the levers in order: the non-reasoning model above, then `SEMAPHORE_LIMIT` for concurrency. A smaller model is *not* an obvious lever — token throughput was flat at 55-73 tok/s across both models benchmarked, so on this hardware output volume is what costs time.

Quality — where the decision actually gets made:

- [ ] Take **20 statements already promoted under Gemini**, re-promote into `mem-fabric-local`, compare extracted entities and edges side by side. The old graph exists precisely for this.
- [ ] Run the same recall queries against both graphs and compare.
- [ ] **Score the six first-look symptoms** (above) on both graphs, not just impressionistically: pronoun-entity count (and Todd-as-actor vs. generic split), self-referential edge count, parallel-edge rate for `RELATES_TO` *and* `MENTIONS`, paraphrase-spam / template-token leakage in facts, and — the weighted one — "did extraction name the real-world entities" on a hand-graded sample (the `Spain`/`Morocco` failure mode).
- [ ] Root-cause item 4 (the `SOURCE_ENTITY_0` / `The_CURRENT_MESSAGE` / "duplicate legal strategy" facts) — GLM paraphrase-spam, graphiti dedup-prompt echo, or test residue in the live graph.
- [ ] Decide whether items 2, 3 and 5 (self-edges, parallel-edge dedup, semantic episode names) land as promotion-path fixes now, independent of the model decision.
- [ ] **The gate:** does GLM extraction produce entity/edge structure comparable to Gemini's? A local model that extracts noticeably worse converts a quota problem into a data-quality problem, which is the worse trade. If quality drops, fall back to the hybrid — local embeddings (high-volume, low-judgment) with Gemini extraction (low-volume, high-judgment). Phase 1's split provider vars make that a config change.

Now cheap to measure, and worth measuring: whether `gemini-3.8-flash`-class quality was ever needed, or whether its 20 RPD ceiling was the only reason CMF settled on flash-lite.

---

## Rollback

Four env vars and one restore:

```bash
CMF_LLM_PROVIDER=gemini
CMF_EMBED_PROVIDER=gemini
FALKORDB_DATABASE=mem-fabric-gemini
EMBEDDING_DIM=1024
```
```bash
cp imports/journal/journal.db.pre-spark-20260908 imports/journal/journal.db
```

The Gemini graph is never mutated by any step above. That is the reason for D2.

---

## What needs Alex

**Nothing blocks Phases 0–5.** You have sudo and docker, the tunnel needs no cooperation, and every model needed is downloaded.

Worth raising, in rough priority:

1. **Pin the resident models.** The Google Doc's "always-resident GLM + Qwen" is not in effect — `/api/v0/models` shows one model loaded at a time and Auto-Evict swapping on each request to a different model. A long CMF promotion run is exactly the workload this hurts. Ask for GLM-4.7-Flash and the Nomic embedder to be pinned; the embedder is tiny and being evicted between calls would be pure overhead.
2. **Raise the loaded context from 8192.** Every model is loading at LM Studio's 8192 default, not the agreed 128K cap. Graphiti's extraction prompt carries previous-episode context and may exceed it. 32K is probably plenty for CMF and far cheaper in KV-cache than 128K.
3. **The `0.0.0.0:1234` binding.** Confirmed by probe, not speculation: LM Studio, OpenWebUI (`:3000`) and xrdp (`:3389`) are all listening on all interfaces on a public IP with no firewall you control, and LM Studio's API is unauthenticated. Independent of this migration — CMF only ever uses the tunnel. Concrete suggestion: bind LM Studio to the Tailscale address (`100.123.43.85`) instead of `0.0.0.0`, which keeps it reachable for OpenWebUI on the same host via loopback. Check first whether OpenWebUI points at `localhost:1234` or the public address.
4. **The Gemma `<|channel>` bug** (§3.3) — a genuine LM Studio/llama.cpp constrained-decoding issue worth reporting upstream, unrelated to CMF. Also worth asking whether LM Studio exposes a per-model switch to disable server-side reasoning parsing; if so, it may remove the need for half the Phase 3 proxy.
5. **`lms` on your PATH** — a nicety. Every LM Studio inspection currently needs `sudo -iu nano` (note: `sudo nano` opens the editor).
6. **Heads-up before the Phase 6 run** — sustained load, per the agreement to flag big experiments in advance.

---

## Open risks

| Risk | Severity | Mitigation |
|---|---|---|
| GLM extraction quality below Gemini | **High** — the real gate; 2026-09-09 first-look inspection (Phase 7) found specific symptoms — generic-pronoun entities, self-referential (8) and duplicate parallel edges (`RELATES_TO` 50 pairs / `MENTIONS` 7 pairs), paraphrase-spam facts with leaked template tokens (`SOURCE_ENTITY_0`, `The_CURRENT_MESSAGE`), and surface-fragment entities missing the real referents (`Spain`/`Morocco`) | Phase 7 A/B against the retained graph, now with those symptoms as explicit scored metrics; mechanical fixes (self-edges, edge dedup, episode naming) separable from the model choice; hybrid fallback via the split provider vars if the extraction prompt can't recover items 1, 4 and 6 |
| Reasoning-model throughput | **Medium** — measured; passes for the 285-row run, marginal for the 1,243-row backlog | GLM ~12 h / 285 episodes (vs ~2 days on the free tier) but **~50 h for the full backlog**. `qwen3-coder-30b` is 3.4x cheaper in output tokens (~15 h) — promoted from escape hatch to a Phase 7 quality candidate |
| Auto-Evict swaps the model out mid-run | **High** — observed live: two probe requests died with `{"error": "Model unloaded."}` mid-generation | Ask Alex to pin (see "What needs Alex" #1). Note `"Model unloaded."` matches none of `classify_transient_error`'s markers and is not an HTTP 5xx, so **graphiti will not retry it** — add it to `TRANSIENT_ERROR_MARKERS` in Phase 4 |
| 8192 loaded context truncates extraction prompts | Medium | Raise to 32K; until then, watch for truncated/empty extractions on long episodes |
| Reasoning payload isn't pure JSON in some path | Medium | Phase 3 guard: only promote `reasoning_content` when it parses |
| `EMBEDDING_DIM` mismatch corrupts vectors silently | Medium | Assert `len(vector) == 768` on first embed rather than trusting config |
| Nomic silently truncates >2048-token input | Low | Graphiti only embeds names and facts, both short; parity with `gemini-embedding-001` |
| Tunnel drops on sleep | Medium | `autossh` / launchd + `ServerAliveInterval` |
| Spark unavailable (Alex's box, Alex's experiments) | Medium | `CMF_LLM_PROVIDER=gemini` escape hatch stays live |
| `promotions` ledger reset loses provenance | Low | Backup first; better, add `graph_name` to the primary key |

---

## Suggested sequencing

Phases 0–5 are one session's work and fully reversible. Phase 6 is the irreversible-feeling one (it isn't, given backups) and Phase 7 is where the decision gets made. Per the milestone review gate, treat **Phase 5 → Phase 6 as the checkpoint**: get the plumbing green and a 5-row promotion working, then stop and look at the output before committing to a full rebuild.

This does not displace MS6a's outstanding review pass — if anything it argues for doing the tier-1 pass **first**, on the existing Gemini graph, so verdicts exist before the rebuild. Verdicts live in `reviews` / `derived_memories`, not in the graph, so they survive it and make the re-promotion smaller and better targeted.
