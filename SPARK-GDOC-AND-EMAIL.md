# Google Doc updates + email to Alex

Companion to [SPARK-MIGRATION-PLAN.md](SPARK-MIGRATION-PLAN.md). Nothing here has been sent or edited — copy from it as you like.

---

## Part 1 — What to update in the shared Google Doc

### A. "Services" tab — corrections to what's recorded there

These are measured, not inferred, from `/api/v0/models` and `ss -lnt` on 2026-09-08:

| Recorded / assumed | Actually observed |
|---|---|
| GLM + Qwen always resident | **Only one model loaded at a time.** Auto-Evict swaps on each request to a different model. Both are downloaded, neither is pinned |
| 128K context cap | **All models load at 8192** — LM Studio's default. The 128K decision isn't in effect |
| `lms server start --bind 0.0.0.0` | Confirmed: `:1234` (LM Studio, unauthenticated), `:3000` (OpenWebUI) and `:3389` (xrdp) are all listening on all interfaces |
| Qwen3.5-35B-A3B pending download | **Complete** — present in `lms ls` output |

Also worth adding to that tab, since neither of us had it written down:
- All chat models are `Q4_K_M` GGUF.
- `zai-org/glm-4.7-flash` reports `arch: deepseek2`.
- `text-embedding-nomic-embed-text-v1.5` → **768 dimensions**, 2048-token context, batch input supported.
- Disk: 3.0 T free of 3.7 T.

### B. "Proposed Spec" tab — a new "CMF on the Spark" section

The short version of what CMF will actually use:

```
Mac                                    Spark (nanospark)
  CMF server                             LM Studio :1234
  FalkorDB (Docker, localhost:6379)  <-- SSH tunnel -->  GLM-4.7-Flash  (extraction)
    graph `memory-fabric`       (Gemini-era, kept)       nomic-embed-text-v1.5 (embeddings)
    graph `mem-fabric` (new, 768-dim)
  BGE reranker (local, sentence-transformers)
```

- **The graph stays on my machine.** Only inference crosses the tunnel — extraction prompts and embedding requests. No FalkorDB on the Spark, no second tunnel.
- **Access is via the Tailscale SSH tunnel only**, never the public `128.171.121.85`.
- **Reranking is local to my Mac** (`sentence-transformers` + `BAAI/bge-reranker-v2-m3`), so it needs nothing from the Spark. This replaces the earlier "small BGE container on the Spark" proposal — simpler, and it drops a shared dependency.
- **Steady-state load is modest** — CMF is not an interactive consumer. The heavy event is a one-off backfill (below).

### C. "Proposed Spec" tab — model-behaviour findings worth recording

This is the part most useful to Alex independent of CMF, since it affects anything doing structured extraction:

> **Structured output on LM Studio: use `text` mode, not `json_schema`.**
>
> - LM Studio accepts only `response_format: "json_schema"` or `"text"`. **`json_object` is rejected** with `'response_format.type' must be 'json_schema' or 'text'`.
> - Under `json_schema`, GLM-4.7-Flash and Qwen3.5-35B-A3B return **empty `content`** — the whole JSON answer lands in `reasoning_content`. Anything reading `message.content` gets nothing. Setting `chat_template_kwargs: {"enable_thinking": false}` does not help.
> - Under `json_schema`, **Gemma-4-26B-A4B corrupts**: a `<|channel>` control token leaks into a JSON string and generation collapses into repeated `6666…` until the token cap. The grammar constrains text shape but doesn't mask special tokens. Probably worth reporting upstream to LM Studio / llama.cpp.
> - **In `text` mode with the schema in the prompt, all three work correctly** — clean JSON in `content`, thinking in `reasoning_content`.
>
> | Model | `json_schema` | `text` + schema in prompt |
> |---|---|---|
> | GLM-4.7-Flash | JSON stuck in `reasoning_content` | ✅ 17 entities / 14 edges |
> | Qwen3.5-35B-A3B | JSON stuck in `reasoning_content` | ✅ 14 entities / 10 edges |
> | Gemma-4-26B-A4B | ❌ control-token corruption | ✅ 12 entities / 10 edges |

### D. "Proposed Spec" tab — throughput baseline

Same prompt, one sample each, 8192 context, nothing else on the GPU:

```
model                        wall    completion tokens   tok/s
zai-org/glm-4.7-flash        48.4s         2,916         60.2
zai-org/glm-4.7-flash warm   53.5s         3,578         66.8
qwen/qwen3-coder-30b         18.7s         1,040         55.8
qwen/qwen3-coder-30b warm    14.5s         1,060         73.0
```

Two things worth having on record for both of us:
- **~55–73 tok/s regardless of model.** That looks like the hardware ceiling for Q4 GGUFs at this context size, so wall-clock tracks output volume, not parameter count.
- **Reasoning models cost ~3.4x in output tokens** for the same task. Relevant to anyone picking a model for batch work.

*(Caveat to note in the doc: single samples, and warm-vs-cold isn't cleanly separated yet — GLM's "warm" run was slower only because it emitted 23% more tokens. More runs needed.)*

### E. Flag as a heads-up, not a decision

One-off backfill: **285 episodes now, ~1,243 later**, at 4–6 LLM calls each. Overnight on GLM. I'll give notice before starting it.

---

## Part 2 — Draft email to Alex

> Subject: Spark — CMF plan, plus some LM Studio findings you'll want

hey alex—

thanks for the sudo/password reset and for clearing out the ollama stuff. i changed the temp password with `passwd`, and confirmed i'm already in both `sudo` and `docker` — so that one's done, nothing needed. i also confirmed Qwen3.5-35B-A3B finished downloading and shows up in the model list. so of your four checkboxes, the top ones are all verified from my side.

i've written up a plan to move Context Memory Fabric off the Gemini free tier and onto the Spark, and added it to the doc. the short version: CMF's graph database stays on my machine — only inference crosses the tunnel. GLM-4.7-Flash for entity extraction, nomic-embed-text for embeddings, reranking runs locally on my laptop. that last bit changes what i proposed earlier: no BGE container on the Spark after all, which means one less shared service for us to maintain.

**some findings that are probably more useful to you than the CMF plan itself.** i spent a while probing structured output and hit three things worth knowing:

1. **LM Studio rejects `response_format: json_object`** — it only accepts `json_schema` or `text`. worth knowing if you point anything at it that assumes the OpenAI default.
2. **GLM-4.7-Flash and Qwen3.5 return empty `content` under `json_schema`.** the entire JSON answer goes into `reasoning_content` instead. anything reading `message.content` — which is most libraries — gets an empty string and usually raises. setting `enable_thinking: false` doesn't help; LM Studio's reasoning parser runs regardless.
3. **Gemma-4-26B-A4B actively corrupts under `json_schema`.** a `<|channel>` control token leaks into a JSON string mid-generation and the output collapses into repeated `6666...` until it hits the token cap. i think the grammar constrains the text shape but doesn't mask the model's special tokens. this looks like a genuine LM Studio/llama.cpp bug rather than a bad quant — the same model answers a plain prompt fine. might be worth reporting upstream.

**the fix for all three is the same:** use `text` mode with the schema in the prompt instead of `json_schema`. i re-ran all three models that way and they all return clean JSON in `content`, with actual thinking in `reasoning_content`. GLM gave the richest extraction (17 entities / 14 edges vs 14/10 and 12/10).

i also got a rough throughput baseline: ~55–73 tok/s across every model i tried, which looks like the hardware ceiling at 8192 context rather than anything model-specific. so wall-clock is really about how many tokens a model emits — and reasoning models emit ~3.4x more for the same task. GLM 48s/call vs qwen3-coder 15s/call, almost entirely explained by output volume. i'll do more careful warm/cold runs later; these are single samples.

**two things i'd like your help with:**

- **pinning the resident models.** right now `/api/v0/models` shows only one model loaded at any moment, with Auto-Evict swapping as requests come in — a couple of my test calls actually died mid-generation with `"Model unloaded."`. that's fine for interactive use but it'll break a long batch job. could we pin GLM-4.7-Flash and the nomic embedder? the embedder is tiny and evicting it between calls is pure overhead.
- **the context length.** everything is loading at 8192, not the 128K we'd talked about — looks like LM Studio's default rather than a deliberate setting. 32K would be plenty for what i'm doing and much cheaper in KV cache than 128K.

on the network binding — i realise i keep coming back to this, but i did confirm it rather than just worrying about it: `1234`, `3000` and `3389` are all listening on `0.0.0.0` and LM Studio's API has no auth. CMF only ever reaches it through the Tailscale tunnel, so this doesn't block me. but one option that keeps everything working: bind LM Studio to the Tailscale address (`100.123.43.85`) instead of `0.0.0.0` — OpenWebUI on the same host can still reach it over loopback. worth checking first whether OpenWebUI is configured against `localhost:1234` or the public address.

last thing: at some point i'll need to do a one-off backfill — 285 episodes now, maybe 1,243 later, at 4–6 LLM calls each. that's an overnight job on GLM. i'll give you a heads-up before i kick it off rather than just saturating the box.

no rush on any of this. the plan doesn't block on you — i can build and test the whole pipeline against what's there now.

cheers,
-t

---

## Notes before you send

- The email assumes you're comfortable saying the BGE-container proposal is withdrawn. If you'd rather keep that option open with Alex, cut that sentence.
- The `<|channel>` finding is the most genuinely useful thing in here for him — it's his box and his models, and it would bite anyone doing structured extraction. Worth leading with if you want the reply to be substantive.
- The throughput numbers are single samples. The email says so; keep that caveat if you trim.
- Nothing in here commits you to a date.
