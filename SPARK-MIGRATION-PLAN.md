# Migrating CMF off Gemini onto Spark-local models

**Status:** Phase 0 substantially verified; Phases 1-7 not yet executed. **Written:** 2026-09-08. **Revised:** 2026-09-08 (rev 3 — decisions D1-D5, probing, and Phase 0 verification).
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
FalkorDB graph `memory-fabric` (local Docker, 127.0.0.1:6379)
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

**D2 — New graph.** ✅ *Resolved: fresh graph, full re-promotion, not more writes into `memory-fabric`.*

Target `mem-fabric`. `FALKORDB_DATABASE` has no default and refuses to guess ([`resolve_target_database`](server/providers/memory_graphiti.py)), so switching is a one-line `.env` change and the Gemini-era graph survives untouched for the §9 comparison and for rollback.

**Confirmed split:** the graph lives on Todd's machine; only the *inference* runs on the Spark.

```
Mac                                    Spark (nanospark)
  CMF server                             LM Studio :1234
  FalkorDB (Docker, 127.0.0.1:6379)  <-- SSH tunnel -->  GLM-4.7-Flash
    graph `memory-fabric`      (kept, untouched)         nomic-embed-text
    graph `mem-fabric` (new, 768-dim)
  BGE reranker (sentence-transformers)
```

So FalkorDB is not moved, no second tunnel is needed, and the graph never leaves your hardware. What crosses the tunnel is extraction prompts and embedding requests. The Gemini-era `memory-fabric` graph stays in place for the Phase 7 A/B and for rollback.

**D3 — Reranking: `BGERerankerClient`.** ✅ *Resolved: option 1.*

`uv add sentence-transformers`, then `BGERerankerClient()`. First construction downloads `BAAI/bge-reranker-v2-m3` (~2.2 GB) and it runs on the Mac. Note `OpenAIRerankerClient` pointed at LM Studio **will not work** — it scores via `logit_bias={'6432': 1, '7983': 1}`, hardcoded OpenAI BPE token ids meaningless under GLM's tokenizer. Verified by reading the source; do not attempt it.

A ~10-line passthrough `CrossEncoderClient` (returns Graphiti's existing RRF order) is worth keeping in the tree as a dev stub so a torch install never blocks a test run, but BGE is the target.

**D4 — Gemini escape hatch.** ✅ *Resolved: keep it.*

`CMF_LLM_PROVIDER` (`gemini` | `local`) selects between client sets inside `create_graphiti`. Split it into `CMF_LLM_PROVIDER` + `CMF_EMBED_PROVIDER` from the start — §9 may well land on the hybrid (local embeddings, Gemini extraction), and retrofitting that split later means redoing Phase 1.

**D5 — MS4a cost/privacy gate.** ✅ *Resolved: no concern about Alex having access.*

The recorded MS4a decision was "Gemini-only for now, no filtering, hard free-tier rate cap." The cost half is simply superseded — local inference has no per-call cost, so the ledger stops being a spend control and becomes a throughput control (§7). No privacy action needed; noting only that the decision record should be updated so it doesn't read as still-current.

---

## Phase 0 — Network path — **3 of 4 done**

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

- [ ] **Make the tunnel survive sleep — the one item still open.** `Host spark` in `~/.ssh/config` is currently bare (`HostName` + `User` only) with no keepalives set anywhere in the file. Four days of uptime is encouraging but it is luck on a quiet network: without `ServerAliveInterval` a half-open connection stays *listening locally* while silently failing to forward, so CMF sees connection-refused or a hang mid-consolidation rather than a clean error. A 12–24 h promotion run is exactly the workload that exposes this.
```
Host spark
    HostName 100.123.43.85
    User todd
    ServerAliveInterval 30
    ServerAliveCountMax 3
    ExitOnForwardFailure yes
```
Note `ExitOnForwardFailure yes` changes behaviour deliberately: ssh will refuse to connect at all if it cannot bind 12345, rather than silently giving you a session with no forward. For auto-restart, `autossh -M 0 -N -L 12345:127.0.0.1:1234 spark` or a launchd agent with `KeepAlive`. Restart the existing tunnel after editing the config — the running process keeps its old settings.

Port `12345` is the one already in use. Nothing is listening on the Mac's `11434` (no local Ollama), so there is no collision either way; `12345` keeps the Spark endpoint visibly distinct from anything local.

---

## Phase 1 — Config plumbing

- [ ] Add to `.env.example` and `.env`:
```bash
# Provider selection, split so a hybrid (local embeddings + Gemini extraction)
# is expressible without re-plumbing — see D4 and Phase 6.
CMF_LLM_PROVIDER=local            # "gemini" | "local"
CMF_EMBED_PROVIDER=local          # "gemini" | "local"

# Spark LM Studio, reached over the Phase 0 tunnel. Never a public IP.
CMF_LOCAL_BASE_URL=http://127.0.0.1:12345/v1
CMF_LOCAL_API_KEY=lm-studio       # LM Studio ignores it; the OpenAI SDK requires non-empty
CMF_LOCAL_LLM_MODEL=zai-org/glm-4.7-flash
CMF_LOCAL_EMBED_MODEL=text-embedding-nomic-embed-text-v1.5

# MUST match the embedder's true output width. Graphiti reads this at import
# time and OpenAIEmbedder *silently truncates* to it — a wrong value here
# corrupts every vector with no error.
EMBEDDING_DIM=768

# New graph. The 1024-dim Gemini graph stays intact under `memory-fabric`.
FALKORDB_DATABASE=mem-fabric
```

- [ ] Extend `CMFConfig` in [`server/core/config.py`](server/core/config.py) with the local fields, and widen `memory_enabled` so it doesn't demand `GEMINI_API_KEY` when the provider is `local`. As written it returns `False` without a Gemini key, which would disable memory tooling at startup on an otherwise correctly configured local box.

**Gotcha:** `EMBEDDING_DIM` is read at *module import* in graphiti_core (`EMBEDDING_DIM = int(os.getenv('EMBEDDING_DIM', 1024))`). It must be in the environment before `graphiti_core` is first imported — a `load_dotenv()` inside a function that runs after the import is too late. Set it in the shell/MCP server env, or call `load_dotenv()` before the graphiti import in `server/mcp.py`.

---

## Phase 2 — Swap the Graphiti clients

Branch `create_graphiti()` (L94) on `CMF_LLM_PROVIDER` / `CMF_EMBED_PROVIDER`. The Gemini branch stays exactly as-is.

- [ ] Local branch:
```python
from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
from graphiti_core.cross_encoder.bge_reranker_client import BGERerankerClient

llm_client = OpenAIGenericClient(
    config=LLMConfig(
        api_key=local_api_key,
        base_url=local_base_url,
        model=resolved_model,
        small_model=resolved_model,
    ),
    client=LMStudioCompatClient(local_base_url, local_api_key),   # Phase 3
)

embedder = OpenAIEmbedder(
    config=OpenAIEmbedderConfig(
        api_key=local_api_key,
        base_url=local_base_url,
        embedding_model=local_embed_model,
        embedding_dim=768,
    )
)

cross_encoder = BGERerankerClient()      # D3; PassthroughReranker() as a dev stub
```

Use `OpenAIGenericClient`, **not** `OpenAIClient` — the generic one targets arbitrary OpenAI-compatible endpoints.

- [ ] Set `structured_output_mode='json_object'` (Mode B, §3.4). That makes graphiti inject the schema into the prompt; the Phase 3 proxy then rewrites the wire format to `text`, which is what LM Studio accepts. Driving this from a `CMF_LOCAL_STRUCTURED_MODE` env var keeps Mode A one variable away if a model turns out to need the grammar.

- [ ] Assert the first embedding's width rather than trusting config:
```python
assert len(vec) == 768, f"embedder returned {len(vec)}d, EMBEDDING_DIM says 768"
```
`OpenAIEmbedder.create` does `embedding[: self.config.embedding_dim]` — a mismatch truncates silently and corrupts the index with no error.

---

## Phase 3 — The LM Studio compatibility proxy

This is the piece that makes D1 work. Implement as a **wrapper around `AsyncOpenAI`** exposing `.chat.completions.create(...)`, injected via `OpenAIGenericClient(client=...)` — not a subclass, so it survives graphiti upgrades.

- [ ] **Request side (does the real work in Mode B):** if `response_format.type == "json_object"`, rewrite to `{"type": "text"}` (§3.2). Graphiti has already appended the schema to the final user message, so nothing is lost.
- [ ] **Response side (Mode A fallback):** if `choices[0].message.content` is empty/whitespace and `reasoning_content` is non-empty, promote `reasoning_content` into `content` (§3.1). Graphiti's own `_strip_code_fences` handles fenced output downstream.
- [ ] **Guard the promotion.** Only substitute when the reasoning payload actually parses as JSON; otherwise leave `content` empty and let `EmptyResponseError` fire (which graphiti retries). This guard is what makes it safe to keep both modes in one code path — in Mode B `reasoning_content` holds genuine chain-of-thought prose (verified on Qwen: `'Thinking Process:\n\n1. **Analyze the Request:**…'`), and feeding that to the JSON parser would be worse than failing.
- [ ] **Don't lower `max_tokens`.** Reasoning tokens come out of the same budget, and extraction calls measured at 2,900–5,800 completion tokens. Graphiti's `OpenAIGenericClient` defaults to 16,384, which is ample; the failure mode if it's too low is a truncated JSON body (`finish_reason: "length"`), which graphiti will retry three more times before failing — expensive at ~48s a call.
- [ ] Unit-test against **recorded** LM Studio responses (empty-content + populated-reasoning; a `json_object` request) so the test suite needs no live Spark.

---

## Phase 4 — Neutralize the rate limiter

The ledger is correct and worth keeping for the Gemini path. For local models it must become a no-op without changing any call signature — `promote_reviewed`, `pipeline.py` and `ReasoningEpisodePolicyV1` all take a `GeminiRateLimiter` and catch `GeminiQuotaExhaustedError`.

- [ ] Add a `LOCAL_MODEL_BUDGETS` mechanism populating budgets on demand for whatever `CMF_LOCAL_LLM_MODEL` names, with `rpm`/`rpd` sentinels large enough never to bind (so `seconds_until_headroom` is always `0.0`).
- [ ] Have `get_default_rate_limiter()` (L340) merge those when `CMF_LLM_PROVIDER=local`, so the constructor's unknown-model `ValueError` doesn't fire.
- [ ] Leave `KNOWN_MODEL_BUDGETS` (L52) and its dashboard-provenance comment untouched — those numbers stay accurate for the Gemini path and shouldn't be diluted with fake entries.
- [ ] **Raise `DEFAULT_CALLS_PER_OPERATION` from 3 to 6** and correct its docstring. It is documented as a deliberate over-estimate of graphiti's real per-episode call count, but that count is 4-6 (Phase 7), so today it under-reserves and the local ledger can drift ahead of Google's actual counters. This is a fix to the *Gemini* path and is worth landing independently of the migration.
- [ ] Pass `inter_call_delay=0.2` for local runs ([`promotion.py:145` and `:305`](server/consolidation/promotion.py)) — it's a parameter, so pass it rather than editing the default.

Keep the *shape*: local inference still fails transiently — model swapping under Auto-Evict (§1, a live concern here), or Alex taking the baseline offline. `classify_transient_error`'s `503`/`unavailable`/`overloaded` markers still apply, so the retry machinery stays useful.

---

## Phase 5 — Reasoning-episode policy

[`_default_generate()`](server/policies/reasoning_episode_v1.py) at L106 calls `google.genai` directly with `response_mime_type: application/json`.

- [ ] Write `_local_generate(model, prompt)` against `/v1/chat/completions`, going through the **same Phase 3 proxy** so the reasoning-channel and `json_object` handling live in one place. Use `json_schema` with the episode schema, temperature 0.2.
- [ ] Select on `CMF_LLM_PROVIDER` at `ReasoningEpisodePolicyV1.__init__` (L158) — `generate_fn` is already injectable, so no structural change and the existing fakes keep working.
- [ ] Retune `_TRANSIENT_MARKERS` for LM Studio's error strings (it returns `{"error": "..."}` shapes, not Google's). **Add `"model unloaded"`** — observed live when Auto-Evict swapped a model out mid-generation. It is not an HTTP 5xx and matches none of the existing markers, so without this it surfaces as a hard failure and aborts the run rather than retrying. Same addition belongs in [`server/core/rate_limiter.py`](server/core/rate_limiter.py)'s `TRANSIENT_ERROR_MARKERS` for the Graphiti path.
- [ ] **Bump `version` from `"0.2"` to `"0.3"`.** A different extraction model is a different policy. The pipeline's `supersedes` lineage re-derives cleanly, and leaving it at 0.2 would silently mix Gemini-derived and GLM-derived episodes in one version bucket — wrecking the §9 comparison.

---

## Phase 6 — Fresh graph and rebuild

**Back up before anything here.**

- [ ] Journal + ledgers:
```bash
cp imports/journal/journal.db imports/journal/journal.db.pre-spark-20260908
```
- [ ] FalkorDB:
```bash
docker exec context-memory-fabric-falkordb redis-cli --rdb /data/pre-spark-20260908.rdb
```

- [ ] Point `.env` at `mem-fabric`. Graphiti builds indexes at the new dimension on first use; the old graph is untouched.

- [ ] **Reset the promotion ledger.** `promote_reviewed` skips any row where `promotion_store.is_promoted(memory_id)` is true, and `promotions.memory_id` is the primary key with **no graph column in it** — so all 285 prior promotions would be skipped and the new graph would come up empty. After confirming the backup exists:
```bash
sqlite3 imports/journal/journal.db "DELETE FROM promotions;"
```
Better: add `graph_name` to that primary key so both graphs coexist in one ledger, making the §9 A/B repeatable rather than one-shot.

- [ ] Re-promote, small first:
```bash
python -m server.review.cli promote --limit 5 --apply
```
Inspect before going further:
```bash
docker exec context-memory-fabric-falkordb redis-cli GRAPH.RO_QUERY mem-fabric "MATCH (n) RETURN labels(n)[0], count(*)"
```
- [ ] Then the full run. No `--no-wait` needed — after Phase 4 there is nothing to wait for.

Watch for Auto-Evict swapping GLM out mid-run (§1 shows this happening already). If throughput collapses, check residency first — `lms ps` as `nano`.

---

## Phase 7 — Verification

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

- [ ] Take **20 statements already promoted under Gemini**, re-promote into `mem-fabric`, compare extracted entities and edges side by side. The old graph exists precisely for this.
- [ ] Run the same recall queries against both graphs and compare.
- [ ] **The gate:** does GLM extraction produce entity/edge structure comparable to Gemini's? A local model that extracts noticeably worse converts a quota problem into a data-quality problem, which is the worse trade. If quality drops, fall back to the hybrid — local embeddings (high-volume, low-judgment) with Gemini extraction (low-volume, high-judgment). Phase 1's split provider vars make that a config change.

Now cheap to measure, and worth measuring: whether `gemini-3.8-flash`-class quality was ever needed, or whether its 20 RPD ceiling was the only reason CMF settled on flash-lite.

---

## Rollback

Four env vars and one restore:

```bash
CMF_LLM_PROVIDER=gemini
CMF_EMBED_PROVIDER=gemini
FALKORDB_DATABASE=memory-fabric
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
| GLM extraction quality below Gemini | **High** — the real gate | Phase 7 A/B against the retained graph; hybrid fallback via the split provider vars |
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
