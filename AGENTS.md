# AGENTS.md

## Project

Context Memory Fabric (CMF) is a shared personal-context layer for AI clients. It keeps four concerns distinct:

1. source evidence
2. derived episodic memory
3. durable knowledge
4. assembled runtime context

Preserve these boundaries. Do not collapse evidence, memory, knowledge, and context into a single store or abstraction without explicit direction.

## Relevant documentation

Read documentation according to the task rather than loading all project docs automatically.

- `docs/adr/` — architectural decisions and invariants
- `docs/IMPLEMENTATION-PLAN.md` — milestone definitions, sequencing, and exit gates
- `docs/plan-active.md` — current work and outstanding tasks
- project README/setup documentation — operational and environment details

For architecture changes, consult the relevant ADRs first.
For milestone work, consult the implementation plan and active plan.

Documentation may lag the implementation. When code and documentation disagree, investigate the discrepancy rather than assuming either is correct.

## Working style

For non-trivial changes:

1. Inspect the relevant implementation and tests.
2. State the proposed approach before making broad or architectural changes.
3. Make the smallest coherent change that satisfies the task.
4. Run focused tests first.
5. Run broader relevant tests before declaring the work complete.
6. Report what changed, what was tested, and any unresolved issues.

Work on one milestone or clearly bounded task at a time.

Do not opportunistically refactor unrelated code.

## Safety and operational state

This repository contains real local operational state in addition to Git-tracked source.

Do not, without explicit approval:

- reset, recreate, migrate, or delete FalkorDB graphs
- modify production graph data
- delete or rewrite SQLite journal data
- approve, reject, promote, or reconcile queued memories or document proposals
- apply changes to the external LLM_Wiki repository
- start, stop, restart, or reconfigure persistent services
- modify launchd jobs
- change model or embedding configuration
- install or remove dependencies
- perform bulk historical imports or backfills

Treat ignored files, databases, proposal queues, import state, evaluation fixtures, and taxonomy files as potentially important data even though Git does not track them.

Reads and writes through CMF interfaces may themselves be captured as evidence. Consider side effects before using project services merely for inspection.

## Graph and model invariants

The current local deployment uses 768-dimensional vectors.

Do not change embedding dimensions on an existing graph. A dimension change requires a compatible fresh graph and an explicit migration/rebuild plan.

Model, embedding, extraction-policy, and graph-schema changes can affect persisted data. Assess each change for persisted-data compatibility, versioning, and whether replay, migration, or a fresh graph is required before applying it.

## Testing

Prefer the smallest relevant test set during development.

Do not run tests marked `live` unless explicitly requested. Live tests may invoke real inference or create persistent test data.

Before running any test or acceptance script that can access FalkorDB, SQLite state, external providers, or other persistent services, verify its target and isolation behavior. Do not assume that an unmarked pytest test is side-effect free.

Tests should use the designated test graph rather than production state. Existing `conftest.py` safeguards are useful but are not a substitute for verifying that a test cannot mutate production data.

Standalone acceptance scripts may bypass pytest fixtures and isolation. Inspect them before execution and do not run them against operational state without explicit approval.

Do not claim tests passed unless they were actually executed.

## Git

Do not commit, push, merge, rebase, reset, delete branches, or modify remote state unless explicitly requested.

Do not overwrite or discard user changes.

Before substantial work, inspect the current branch and working-tree state.

When parallel agents are working on the project, prefer isolated branches or worktrees rather than allowing multiple agents to modify the same checkout.

## Documentation

Update documentation when the implementation changes an architectural decision, operational procedure, milestone state, or documented behavior. Update the applicable planning item to reflect verified progress; mark it complete only when its acceptance criteria and required approvals are satisfied

Do not rewrite planning documents merely to make them match an implementation without first determining whether the implementation or the plan is authoritative.

Keep historical decisions and completed milestones intact unless the task explicitly calls for revising them.