# Context Memory Fabric (CMF)

[![License: PolyForm Perimeter 1.0.1](https://img.shields.io/badge/License-PolyForm%20Perimeter%201.0.1-blue.svg)](LICENSE.md)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![MCP Compliant](https://img.shields.io/badge/MCP-2.1+-green.svg)](https://modelcontextprotocol.io/)

**Context Memory Fabric (CMF)** is an experimental, single-user, self-hosted personal context and memory layer shared across AI clients (Claude Desktop, IDE agents, chat interfaces, and CLI tools). It bridges the gap between siloed chat windows by uniting **curated durable knowledge** with **temporal episodic memory** through standard **Model Context Protocol (MCP)** tools.

> [!NOTE]
> **Developer Public Preview:** CMF is currently released as a developer preview under the [PolyForm Perimeter License 1.0.1](LICENSE.md). It is designed for individual developers and power users looking to experiment with personal context continuity across their own local AI tools.

---

## Architectural Separation

CMF preserves strict boundaries between four distinct concerns:

```text
                                         Context Memory Fabric MCP Server
                                                         │
   ┌──────────────┬──────────────┬──────────────┬────────┴───────┬──────────────┬──────────────────────┬──────────────────┐
   │              │              │              │                │              │                      │                  │
remember()   recall_mem()   edit_memory()  search_wiki()    get_context()   propose_doc_update()     import_memories()
   │              │              │              │                │              │                      │
 direct        episodic       episodic       canonical         unified       reviewable             administrative
episodic       factual       mutation /       durable          context        proposal                historical
 write           read         re-date          read          assembly          write                 bulk import
   │              │              │              │                │              │                      │
   ▼              ▼              ▼              ▼                ▼              ▼                      ▼
  Graphiti / FalkorDB (Episodic)             Markdown Wiki    Both Stores   doc-proposals/      Graphiti / FalkorDB
  temporal graph validity tracking          (Wiki UNTOUCHED)  synthesized   (staging diffs)     (imports/ registry)
```

1. **Source Evidence:**
   - Raw conversational evidence and tool invocations are journaled to an append-only SQLite store (`imports/journal/journal.db`) in the background without blocking client execution.
2. **Derived Episodic Memory (Graphiti + FalkorDB):**
   - Temporal knowledge graph tracking past decisions, milestones, evolving preferences, and project facts.
   - Preserves temporal validity (`valid_at`, `invalid_at`) so newer decisions supersede older ones without silently erasing history.
3. **Durable Knowledge (`LLM_Wiki`):**
   - Curated Markdown notes, research reports, PDFs, and system blueprints.
   - Proposed updates never overwrite your canonical documents directly; they are staged as unified diffs in `doc-proposals/` for human review.
4. **Assembled Runtime Context (`get_context`):**
   - The primary retrieval interface. Concurrently queries both durable wiki notes and recent episodic decisions, returning synthesized Markdown with clear source attribution and temporal conflict resolution guidance.

---

## Quickstart (Docker Compose Preview)

The fastest way to experience CMF is using the self-contained Compose preview, which includes a pre-configured FalkorDB instance and a fictional starter knowledge wiki:

```bash
# 1. Clone repository
git clone https://github.com/tmargolis/context-memory-fabric.git
cd context-memory-fabric

# 2. Copy the configuration template
cp .env.example .env

# 3. Add your Gemini API key to .env
# GEMINI_API_KEY=AIzaSy...

# 4. Start the stack
docker compose -f docker-compose.preview.yml up -d
```

Your CMF MCP server is now running at `http://localhost:8000/mcp`.

For client connection instructions (Claude Desktop, Cursor, and local Python installation), see the **[Installation & Setup Guide](docs/SETUP.md)**.

---

## Start Building Context Now

You do not need to perform complex historical imports or data migrations to start using CMF:

1. **Connect your favorite client** (e.g. Claude Desktop or Cursor) via the [Setup Guide](docs/SETUP.md#connecting-your-mcp-client).
2. **Retrieve context:** Ask questions like `"What is the current architecture of Project Aether?"` or `"What decisions did we make regarding battery specs?"` — your assistant calls `get_context` or `search_wiki`.
3. **Store milestones:** At natural checkpoints, tell your assistant: `"Remember that we finalized the LoRa transmit interval to 5 minutes."` — it will invoke `remember()`.
4. **Stage wiki changes:** When an assistant drafts a new architectural guideline or updates a specification, it calls `propose_doc_update()` to stage a reviewable diff in `doc-proposals/` without modifying your files.

*(Historical memory imports from ChatGPT or Claude are supported via `import_memories` and `import_chatgpt_exports`, but are labeled **experimental** in this preview.)*

---

## Evaluation: Does Shared Context Help?

These are small evaluations on the author's own corpus, not a general benchmark of the models. Answers were graded against hand-written criteria: 0 = missing or wrong, 1 = partial, 2 = complete.

### 1. Personal Answer Quality (30 Questions)

Questions about ongoing projects and past decisions. The native-memory baselines were asked **inside projects**, so they could draw on local project context — a head start the CMF comparison did not need.

| Assistant / Environment | Completeness (% of max score) |
|---|---|
| Claude (isolated baseline, no context) | 3% |
| Gemini 3.8 Flash (native memory) | 10% |
| GPT-5.6 (native memory) | 18% |
| Claude Sonnet 5 (native memory, in projects) | 18% |
| **Claude + CMF `get_context()` (episodic memory + Wiki)** | **80%** (1.60 / 2) |

With both sources combined, CMF scored about **4.4×** the strongest native-memory baseline.

### 2. Cross-Harness Retrieval (36 Questions)

Can context be recovered from wherever you are working, without first finding the original conversation? These questions were asked **outside projects**, with no local context to help. The 36 questions span six source groups (6 each): Claude Cowork, Claude Desktop Code, ChatGPT, Gemini, Antigravity/Codex, and durable Wiki notes. Each assistant answered the same set first with native memory and no tools, then with CMF available over MCP.

| Assistant | Without CMF | With CMF | Gain |
|---|---|---|---|
| ChatGPT | 1/36 (2.8%) | **36/36 (100%)** | +97.2 pp |
| Claude | 0/36 (0%) | **36/36 (100%)** | +100 pp |
| Gemini | 0/36 (0%) | **36/36 (100%)** | +100 pp |

*Complete answers only. pp = percentage points.*

Without project context, native baselines were lower and the gain from CMF was larger: context that was unavailable through native recall became retrievable through CMF.

### Hallucination & Adversarial Resistance

On a 12-question control suite of non-existent events, superseded decisions, and false-premise questions, assistants using CMF said "I don't know" to pure negatives, returned the current decision rather than outdated state, and refuted or declined false premises instead of inventing details.

---

## Tested Clients & Capabilities

CMF has been verified across several AI development environments:
- **Claude Desktop (macOS / Windows):** Verified with stdio and Streamable HTTP via `mcp-proxy`.
- **OpenAI Codex & Antigravity IDE:** Dedicated transcript capture adapters that journal session evidence and extract episodic proposals.
- **ChatGPT & Gemini:** Connector integration tested via OAuth 2.1 and Streamable HTTP.
- **Cursor / General MCP Clients:** Standard MCP tool discovery and execution.

---

## Privacy, Security & Operational Limits

- **Single-User, Self-Hosted:** Every deployment runs on your own hardware or local Docker containers against your own isolated graph. Your data is never shared with a central CMF service or multi-tenant database.
- **Model Provider Data Flow:** When using Gemini (`CMF_LLM_PROVIDER=gemini`), memory extraction and embeddings are processed by Google's API under your own API key. Local inference via OpenAI-compatible endpoints (`CMF_LLM_PROVIDER=local`) is supported for offline workflows.
- **Mutation & Deletion Limits:** Calling `edit_memory()` updates temporal validity and entity node properties in the graph. Complete retroactive pruning of all downstream graph associations is not guaranteed.
- **Security Disclosures:** If you discover a potential vulnerability, please review our [Security Policy](SECURITY.md) and report it privately.

---

## License

Context Memory Fabric is available under the **PolyForm Perimeter License 1.0.1**. See [LICENSE.md](LICENSE.md) and [COPYRIGHT.md](COPYRIGHT.md) for full terms.

---

## Feedback & Community

We welcome questions, setup issues, and retrieval feedback!
- **Issues & Discussions:** Please open a report using our [Setup or Retrieval Problem template](https://github.com/tmargolis/context-memory-fabric/issues).
- **Setup Questions:** Check [docs/SETUP.md](docs/SETUP.md) or open an issue with your environment details.
