# Public Preview Onboarding and Deployment Plan

**Status:** Proposed  
**Purpose:** Reduce the friction required for someone other than the original developer to install, run, evaluate, and give useful feedback on Context Memory Fabric (CMF), while preserving a path toward a future hosted product.

## Goals

CMF should become easy enough for technically capable outside testers to evaluate without requiring knowledge of the project's internal development history.

The near-term distribution target is a **public preview**, not a multi-tenant SaaS product.

This plan separates three problems that should not be solved at the same time:

1. **Move the primary CMF deployment off the developer laptop.**
2. **Make a single-user CMF deployment easy for another person to install.**
3. **Later, decide whether to build a multi-user hosted CMF service.**

The first two are appropriate for the public preview. The third requires additional identity, tenancy, storage-isolation, and operational work.

---

## Current onboarding friction

The current setup assumes that a tester can:

- install Python 3.12+;
- install and understand `uv`;
- install and operate Docker;
- start and persist FalkorDB;
- obtain and configure an LLM/embedding API key;
- provide an existing durable knowledge corpus through `LLM_WIKI_PATH`;
- explicitly choose a FalkorDB graph through `FALKORDB_DATABASE`;
- run the CMF MCP server;
- expose it over a supported transport when using cloud-hosted AI clients;
- configure OAuth correctly for network access;
- configure each AI harness separately;
- understand which CMF tools to call and how to evaluate whether they helped.

This is reasonable for development, but it is too much prerequisite knowledge for useful public-preview feedback.

The public preview should reduce the tester's mental model to:

> Install CMF, connect one or more AI clients, add/import some history, and ask questions that were difficult to answer across those clients before.

---

## Recommended deployment model for the public preview

### 1. Move the primary personal CMF deployment to a small cloud VM

For the current architecture, a conventional Linux VM is simpler than decomposing CMF across multiple managed services.

Run a single Docker Compose stack containing:

- `cmf-mcp`
- `falkordb`
- a TLS reverse proxy such as Caddy
- persistent volumes for:
  - FalkorDB data;
  - the SQLite journal/state database;
  - proposal/import state;
  - the durable knowledge corpus, or a checked-out/synchronized copy of it.

Use a stable hostname such as `cmf.<domain>` as the OAuth issuer and MCP endpoint.

This deployment remains **single-user** and becomes the canonical personal CMF instance. It removes laptop uptime, networking, and tunnel availability from the normal path.

### 2. Keep SQLite for the single-user hosted deployment

SQLite is not inherently a problem for the first remote deployment. The server and database can remain on the same VM and persistent disk.

Do not migrate to PostgreSQL merely to move CMF into the cloud.

A relational database migration becomes worthwhile when CMF needs one or more of:

- multiple application replicas;
- concurrent multi-user workloads;
- per-user tenancy;
- managed database failover;
- remote database access from several services.

### 3. Keep FalkorDB colocated initially

The simplest first deployment is to run FalkorDB beside CMF in Docker with persistent storage.

Managed FalkorDB can be evaluated later if operational burden, backup requirements, scale, or availability justify the additional cost and external dependency.

### 4. Add backups before treating the VM as canonical

Back up at least:

- FalkorDB persistence data or exports;
- `journal.db` and related CMF state;
- the durable knowledge corpus;
- configuration required to reconstruct the instance, excluding plaintext secrets from the repository.

Backups should leave the VM and land in separate object storage or another independent location.

Document and test restore, not only backup creation.

---

## Do not make the first public preview multi-tenant

A single hosted CMF server shared by unrelated testers would currently create unnecessary privacy and architecture risk.

Before a shared hosted service, CMF would need explicit decisions and implementation for:

- user identity;
- per-user authorization;
- graph isolation;
- journal/evidence isolation;
- durable-knowledge isolation;
- per-user API keys and secrets;
- OAuth client ownership;
- deletion/export of one user's data;
- quotas and abuse controls;
- backup and restore at user scope;
- observability that does not leak user content.

The current `FALKORDB_DATABASE` selection, filesystem-backed wiki, SQLite journal/state, and single-user OAuth model make **one isolated CMF deployment per user** a safer preview architecture.

For early testers, choose one of two paths:

1. **Self-hosted preview:** the tester runs a local single-user CMF instance.
2. **Managed alpha instance:** create a separate isolated cloud instance for a small number of high-value testers.

Do not put several testers into the primary personal CMF instance.

---

## Public-preview onboarding target

### Desired installer experience

The first serious onboarding milestone should require only:

- Docker Desktop or Docker Engine;
- one supported model-provider/API credential, unless a fully local/default path is provided;
- an AI client that supports MCP.

Everything else should be bootstrapped by CMF.

A reasonable target flow:

```bash
git clone https://github.com/tmargolis/context-memory-fabric.git
cd context-memory-fabric
cp .env.example .env
docker compose up -d
docker compose exec cmf cmf doctor
```

The exact commands can change. The important property is that the user should not have to install Python or `uv` on the host.

---

## Changes that reduce onboarding friction

### A. Containerize the CMF server itself

The repository already provides FalkorDB through Docker Compose, but the preview should ship an application image for CMF as well.

The Compose stack should:

- build or pull the CMF image;
- start FalkorDB;
- wait for dependencies to become healthy;
- mount persistent volumes;
- expose the MCP endpoint;
- inject configuration through environment variables;
- restart cleanly without losing state.

This removes Python and `uv` from the tester's host prerequisites.

### B. Make durable knowledge optional

A new user should be able to experience episodic memory and cross-harness context without first creating an LLM Wiki.

Support at least:

- **memory-only mode**, no `LLM_WIKI_PATH`;
- **full mode**, episodic memory plus durable knowledge.

If no wiki is configured, `get_context()` should still operate and clearly identify which providers are active.

### C. Add an initialization path

Provide one command or guided script that:

- creates required state directories;
- validates required configuration;
- creates/selects the target graph;
- verifies FalkorDB connectivity;
- verifies the embedding/extraction provider;
- reports whether the wiki provider is enabled;
- prints the MCP endpoint;
- prints the next connection step.

Avoid requiring a new user to understand graph names, internal state directories, or the journal schema.

### D. Add `cmf doctor`

A diagnostic command should answer:

- Is the server reachable?
- Is FalkorDB reachable?
- Is the configured graph valid?
- Can the configured LLM/embedding provider be called?
- Is the wiki path valid, if configured?
- Is the journal writable?
- Is OAuth configured safely for a network deployment?
- Are required persistent paths mounted?
- Which CMF version is running?

The command should end with a concise pass/fail summary and actionable fixes.

### E. Provide synthetic starter data

Include a small fictional corpus and a handful of synthetic episodes.

This should let a tester validate:

- `remember()`;
- `recall_mem()`;
- `search_wiki()`, when the demo corpus is enabled;
- `get_context()`;
- temporal supersession/provenance behavior.

No personal data from the original CMF deployment should be required to demonstrate value.

### F. Add a first-run evaluation

Give the tester 5-10 questions that demonstrate what CMF is designed to improve.

Examples should cover:

- a fact that changed over time;
- a decision made in an earlier harness;
- a durable reference document;
- a question requiring both episodic and durable context;
- provenance: "why does CMF believe this?"

The preview should make the value observable within the first session.

### G. Provide client-specific quick starts

For each confirmed client, maintain a minimal copy/paste section with:

- local `stdio` configuration when supported;
- remote OAuth/MCP configuration when supported;
- one verification prompt;
- known limitations.

Keep the detailed material in `CLIENTS.md`, but add a shorter "first successful connection" path.

### H. Add an importer workflow intended for normal users

Historical import is central to the value proposition. It should not feel like an internal administrative pipeline.

A preview-quality import flow should:

- accept supported export formats;
- run dry-run/classification first;
- summarize what will become episodic memory versus durable candidates;
- make ambiguous items reviewable;
- be idempotent;
- preserve provenance;
- clearly warn when content is sent to an external model/embedding provider.

### I. Add release artifacts

For a public preview, prefer versioned releases over asking testers to track `main`.

Each preview release should include:

- release notes;
- known limitations;
- upgrade instructions;
- migration notes for persisted state;
- a tested Compose configuration;
- a versioned container image if practical.

### J. Add a feedback path

The repository should make it obvious how to provide:

- installation failures;
- retrieval failures;
- incorrect/stale memories;
- privacy/security concerns;
- client compatibility reports;
- feature requests.

A structured issue template is more useful than asking testers for general feedback.

For the earliest cohort, provide a short test protocol so feedback answers concrete questions rather than "what do you think?"

---

## Hosting options

### Option 1: Single VPS with Docker Compose

**Recommended for the first remote CMF deployment.**

Advantages:

- closely matches the existing local architecture;
- one persistent filesystem for SQLite, wiki files, and CMF state;
- FalkorDB can remain containerized;
- easy to reason about backups;
- stable domain and OAuth issuer;
- low architectural churn.

Tradeoffs:

- CMF remains responsible for VM patching, backups, service monitoring, and recovery;
- one VM is a single availability domain.

For the public-preview stage, simplicity is more valuable than high availability.

### Option 2: Application PaaS plus persistent volumes

Platforms that run containers and provide persistent volumes can host CMF, but the stack has multiple stateful components.

Advantages:

- easier deployments;
- managed TLS and domains;
- less OS administration.

Tradeoffs:

- persistent-volume semantics differ by platform;
- SQLite and FalkorDB constrain horizontal scaling;
- multi-service networking and backups can become more platform-specific than a small VM.

Evaluate this after the Dockerized stack works cleanly on a conventional VM.

### Option 3: Managed FalkorDB plus hosted CMF application

This can reduce graph-database operations.

Advantages:

- database operations and some backup/availability concerns move to the provider;
- a clearer path to larger deployments.

Tradeoffs:

- additional recurring cost;
- still leaves the SQLite/journal and durable corpus to host;
- does not solve multi-user tenancy;
- creates an external dependency for highly personal context data.

This is an optimization, not a prerequisite for the preview.

### Option 4: Multi-tenant hosted CMF

Treat this as a separate product milestone.

Do not use infrastructure selection as a substitute for designing tenancy. A hosted platform does not automatically make the current data model safe for multiple users.

---

## Suggested implementation sequence

### Phase 1: Canonical remote personal deployment

- [ ] Add a CMF application Dockerfile/image.
- [ ] Extend Docker Compose to run CMF + FalkorDB.
- [ ] Define persistent-volume layout.
- [ ] Deploy to a small Linux VM.
- [ ] Configure a stable HTTPS hostname.
- [ ] Use CMF OAuth for remote MCP clients.
- [ ] Move/copy the durable corpus to the VM securely.
- [ ] Move the current FalkorDB graph and SQLite state.
- [ ] Add automated off-host backups.
- [ ] Test restore.
- [ ] Reconnect ChatGPT/Claude/other harnesses to the canonical remote instance.
- [ ] Confirm the laptop can be offline without breaking normal CMF use.

### Phase 2: Single-user public preview

- [ ] Make `LLM_WIKI_PATH` optional.
- [ ] Remove host Python/`uv` from the normal installation path.
- [ ] Add initialization/bootstrap command.
- [ ] Add `cmf doctor`.
- [ ] Add synthetic starter corpus and episodes.
- [ ] Add a short first-run evaluation.
- [ ] Simplify client quick starts.
- [ ] Add normal-user import workflow/documentation.
- [ ] Produce a versioned preview release.
- [ ] Add structured feedback templates.

### Phase 3: Managed alpha testers

- [ ] Select a small cohort that actively uses multiple AI harnesses.
- [ ] Prefer self-hosting for technically capable testers.
- [ ] Provision isolated single-user instances for a few testers when necessary.
- [ ] Measure setup completion rate and time-to-first-use.
- [ ] Track which parts of setup require direct help.
- [ ] Use those failures to determine what to automate next.

### Phase 4: Hosted product decision

Only after public-preview feedback, decide whether CMF needs:

- multi-user hosted service;
- open-core model;
- self-hosted product;
- managed single-tenant offering;
- fully open-source distribution.

If a hosted multi-user service is selected, design tenancy and identity before replacing SQLite or decomposing the stack.

---

## Public-preview acceptance criteria

The preview is ready for broader external testing when:

1. A new user can start CMF without an existing LLM Wiki.
2. Docker is the only substantial local runtime prerequisite.
3. A clean machine can reach a working CMF instance through documented commands.
4. `cmf doctor` can identify the most common configuration failures.
5. The tester can connect at least one supported AI client without project-author assistance.
6. Synthetic data can demonstrate memory, temporal change, provenance, and unified context.
7. Historical import can be run without editing source code.
8. Restarting the stack does not lose memory, journal state, or configuration.
9. Backup and restore have been tested.
10. No original developer secrets or personal corpus data are present in the public repository or container image.

The stronger target is that a technically capable tester can reach a meaningful CMF result in one sitting without needing a live walkthrough.
