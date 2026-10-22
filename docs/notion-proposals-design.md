# Notion change proposals

**Status:** Design approved 2026-10-21. Envelope/inspection and staging/review
implemented and live fixture acceptance passed. Guarded apply is implemented off
by default for review; live writes and credential/service changes remain unauthorized.
**Date:** 2026-10-21
**Prerequisite:** Read-only retrieval is complete ([record](plan-history.md#ms11--live-notion-knowledge-provider-closed-2026-10-21)).

## Recommended first scope

Support an explicit replacement of **one existing plain-text paragraph block**,
including a paragraph nested inside a toggle. Stage locally, review separately,
then apply only the exact reviewed operation. Retain the current read-only
retrieval configuration and token.

Do not convert a flattened search excerpt back into a page: it omits formatting,
block identity and potentially capped content. Exclude page creation, append,
delete, move, titles/properties, attachments, rich-text formatting/links/mentions,
synced blocks and multi-block operations from the first version. These need
separate designs, not inferred fallbacks. A single-block PATCH avoids a
multi-request page replacement with no rollback transaction.

Notion documents `GET /v1/blocks/{id}` and `PATCH /v1/blocks/{id}`; an update
replaces a supplied field, leaves omitted fields unchanged, and cannot update
children directly. Application requires Update content capability.
[Retrieve](https://developers.notion.com/reference/retrieve-a-block),
[update](https://developers.notion.com/reference/update-a-block)

## Existing seams and necessary changes

`server.knowledge.propose_knowledge_change()` already delegates to a provider's
`propose_change()`. Its routing needs no new Notion branch. The existing MCP
signature accepts provider-specific addressing in `target_path`.

However, `DocProposal`, diff rendering and `apply_proposal()` still assume wiki
files; generic names do not make application provider-neutral. Add a small
provider-dispatch seam before filesystem resolution, with the existing wiki
implementation preserved. Unknown providers must fail closed. This is a new
scope amendment for review, beyond the completed retrieval/alias work; do not
silently route Notion through `get_corpus_root()` or Git commits in LLM_Wiki.

Add defaulted provider/target/payload fields to the existing proposal envelope;
legacy records without a provider remain wiki records. No bulk migration or
rewrite of existing proposals. New Notion records use a non-file target URI,
so even an older wiki-only reader cannot treat them as supported text files.
Read-only retrieval remains independently usable without proposals or writes.
This preserves ADRs [0001](adr/0001-four-layer-model.md) and
[0002](adr/0002-provider-boundaries.md).

## Workflow

1. **Inspect.** Add a provider-neutral read tool,
   `inspect_knowledge_document(provider, document_id)`, delegating to optional
   source inspection support; unsupported providers say so. For Notion it
   returns a bounded live block tree with page/block UUIDs, exact text, source
   timestamps, links, editability reasons and opaque target URIs such as
   `notion://pages/<page-uuid>/blocks/<block-uuid>`. Use the existing depth,
   request and byte caps and explicitly mark omissions. Only fully returned,
   individually validated plain paragraphs are eligible; a partial page never
   licenses whole-page replacement. No block IDs need to be copied manually.
2. **Propose.** Call `propose_knowledge_change(provider="notion", target_path=<URI>,
   proposed_content=<replacement paragraph>, rationale=..., source_context=...)`.
   Freshly GET the page, target block and up to four intermediate ancestors;
   verify the block actually belongs to that page and is not trashed, synced,
   moved out of scope or unsupported. Reject unresolvable ancestry and any
   formatted/non-text rich-text fragment. Preserve children and omitted fields.
   Accept 1–2,000 characters with no markup interpretation. Reject no-op changes.
3. **Stage.** Persist a pending proposal under the existing `CMF_STATE_DIR` doc
   proposal store using an atomic write. Record provider, workspace label, page
   and block IDs, target URI, base page/block edit times, canonical original
   block payload/hash, exact PATCH payload, proposed text/hash, rationale,
   evidence context, and readable before/after diff. Never store credentials or
   use capped cached text as the base. Return the existing proposed-result shape
   (`status`, `provider`, `proposal_id`, `target_path`). No Notion mutation.
4. **Review.** Existing list/get/review tools display provider, page title/link,
   exact block, operation, base version, diff and payload digest. Approval and
   rejection remain separate from application. Freeze a canonical `review_digest`
   binding workspace, target IDs, operation, base hashes and replacement payload.
   Add optional `expected_review_digest` parameters to review/apply MCP tools;
   require them for Notion and preserve legacy wiki callers. Editing a proposal
   requires a fresh proposal and review, not an in-place rebase.
5. **Dry run.** Default `apply_doc_proposal(..., dry_run=True)` dispatches to
   Notion, verifies approval plus content/review digests, rechecks ancestry,
   page/block edit times and exact base payload hash without using the cache,
   and returns the exact planned single PATCH. No source write or applied status.
   Dry run can use the read token; enabling writes is not needed to inspect a plan.
6. **Apply (a later bounded phase).** With explicit operator write enablement,
   acquire a cross-process proposal lock, repeat the fresh checks using the
   write credential, and persist an attempt record **before** sending the PATCH.
   Require a nonempty matching `expected_sha256` as well as the review digest;
   do not inherit the wiki helper's optional internal hash check. Send only
   `paragraph.rich_text`; never send children, trash state or properties.
   Re-read the block to verify exact expected text/preserved fields. Record
   verification, timestamps and response IDs before marking applied. There is
   no Notion Git commit. Repeat application of an applied proposal is a no-op.

## Compatibility and failure handling

- Extend `DocProposal` with defaulted fields and reuse the common review store
  and decisions. Preserve old JSON and all existing wiki proposal tests. Keep
  provider-specific validation/snapshot/PATCH code under `server/providers/notion/`;
  the shared layer only owns the envelope, review states and dispatch. Update
  MCP tool descriptions, annotations and contract fixtures deliberately.
- Notion staging may be approved/rejected when application is disabled. The
  dispatcher must return an explicit unsupported/disabled result until the
  apply phase is implemented; never fall through to the wiki writer. Pause
  Notion proposals in automatic review/apply paths until they support the same
  target-bound digest and explicit approval requirements.
- Reject stale, moved, inaccessible, changed-type, formatted or trashed targets.
  Compare canonical raw writable fields and ancestry, not just flattened text
  or minute-granularity page timestamps. Refuse conflicts; no automatic merge.
- The update API reference does **not document** conditional `If-Match` or a
  revision compare-and-swap. A GET/check/PATCH sequence therefore has a race
  with external edits; local locks cannot exclude a Notion user. This is an
  explicit limitation, not atomic conflict prevention. In-flight edits can
  still be overwritten. Recommend applying while the target is not being edited;
  if strict no-lost-update semantics is required, do not enable this writer
  without a verified server-side conditional-write mechanism. Post-write
  verification detects some races but cannot undo overwritten external text.
- A timeout/disconnect/5xx after PATCH may mean the write succeeded. Persist
  `apply_state=unknown` inside the proposal's provider data, keep it visible as
  outcome-unknown and disable another PATCH. A later explicit reconciliation
  reads the block: desired payload present means verified content (with the
  original attempt's uncertain attribution recorded), unchanged base permits
  a newly authorized retry, anything else is a conflict. Do not auto-rollback.
  Recover an interrupted process's started attempt the same way.
- Do not blindly retry mutations or reuse retrieval's retry loop for them.
  At most one PATCH attempt per apply call. A 429/529 records Retry-After in
  the shared credential cooldown. Before any later apply, inspect the live
  block and repeat conflict checks; treat a 529 or any uncertain mutation
  outcome as unknown rather than assuming the PATCH was never executed.
  [Rate limits](https://developers.notion.com/reference/request-limits)
- Invalidate all local cached versions of the affected page before/after a write
  attempt, including unknown outcomes. Add a per-page cache generation so an
  overlapping reader cannot republish a pre-write body after invalidation.
  Other CMF processes still rely on fresh Notion timestamps; do not claim
  cross-process cache coherence beyond that existing boundary.

## Credentials and bounds

Staging/inspection/review use the current `CMF_NOTION_TOKEN`, Read content only.
Real apply is off by default: propose `CMF_NOTION_ENABLE_WRITES=false` and a
separate `CMF_NOTION_WRITE_TOKEN` with Read + Update content, shared only with
intended pages. No Insert content is required for this scope. Verify target
access with the writer itself; a user-supplied workspace label is not proof of
identity. Do not silently use the read token for writes or widen its permissions.
Keep all real values in gitignored `.env`/secret injection. No new dependency.

Use the existing cancellable deadline/pacing machinery, with a distinct
mutation method that has no implicit retries. Each propose/dry-run performs at
most six GETs (page + block + four ancestors), plus at most two read retries.
Apply performs at most six preflight GETs, one PATCH and one verification GET,
with at most two retries for GETs only: **ten HTTP attempts maximum**, under a
10-second overall deadline. Before PATCH, ensure the deadline has time left;
if verification cannot finish, mark unknown. Queue/lock waiting and cooldowns
count toward that deadline. Do not start more requests after expiry; cancelling
the client cannot guarantee that an in-flight remote PATCH stops, so that
outcome must remain unknown until reconciled.
Inspection uses at most one page GET, four scope-ancestor GETs, three block-list
GETs and two read retries (ten HTTP attempts), under the configured deadline.
The four-ancestor allowance for staging is shared across block and root-scope
verification, keeping its six-GET cap. Local proposal publication follows a
successful bounded read on the caller thread; it cannot occur in a timed-out
HTTP worker. The HTTP deadline does not promise to interrupt a stalled local
filesystem commit. Rate limits remain based
on the configured plan, with cooldown taking precedence.

## Implementation sequence and acceptance

1. **Envelope/dispatch and inspection:** defaulted schema, provider-aware review
   rendering, bounded inspection and explicit unsupported apply. Add focused
   tests; legacy wiki proposal creation/review/apply and old JSON stay unchanged.
2. **Staging/review:** Notion `propose_change`, fresh base capture, immutable
   digests, atomic local records, no source writes. Test through actual MCP on
   a synthetic fixture only after authorization to create a real proposal.
   **Review this bounded phase before starting remote application.**
3. **Optional guarded apply:** separate write credential, dry run, proposal lock,
   durable attempt state, conflict checks, single PATCH, verification, cache
   generation and unknown-outcome reconciliation. Enable and live-test only
   with explicit authorization for fixture writes and token/service changes.

Offline tests use fixture transports and temporary queues only. Cover namespace
and ancestry validation, unsupported/annotated blocks, exact payload preservation,
size caps, digest/approval refusal, stale bases, moved/deleted pages, forged
proposal targets, 401/403/429/529, deadline before/after PATCH, crash recovery,
concurrent applies, post-read races, idempotent repeated apply, overlapping cache
fills and rollback/legacy-reader refusal. Never auto-retry an ambiguous write.

Run focused proposal tests, existing knowledge/wiki proposal and MCP contract
checks, then the broader suite with verified isolation; report existing failures
separately. Explicit live acceptance is propose → inspect diff → approve → dry
run → one authorized fixture edit → verify through `search_knowledge` and
`get_context`, followed by an external-edit conflict refusal. Initial staging
acceptance must work with a read-only token and produce zero Notion PATCH calls.

This plan changes no existing queues, schemas on disk, Notion content, model
configuration or services. API field-replacement/capability/size behavior above
is documented; conditional writes, timing of remote revision propagation and
exact transient-error mutation outcomes are not assumed guaranteed. Staging/review implementation was approved 2026-10-21. Remote application still
requires the separate bounded-phase review described above.


## Staging/review implementation checkpoint — 2026-10-21

Implemented the first two phases for review. `inspect_knowledge_document` returns
bounded live block identities and exact eligible paragraph text. Notion staging
uses fresh GET evidence, never a cached excerpt, and publishes an atomic local
record only after the bounded reads succeed. Manual review requires the recorded
canonical digest; legacy wiki JSON and calls retain their defaults. Apply and
apply dry run return unsupported before any wiki filesystem resolution. Automatic
review/application and wiki consolidation exclude these proposals.

Offline verification used synthetic HTTP transports, temporary queues/journals,
and a process guard refusing operational service/database access. No live-marked
tests ran. The initial Notion retrieval/proposal run passed **81 tests**; the
legacy/provider/review/MCP regression run passed **159 tests**, including the
32-tool schema fixture and offline MCP adapter workflow. After adding explicit
legacy-reader and inspection-depth checks, the proposal-only run passed
**25 tests**. Final inspection completeness handling also marks unsupported
child subtrees as omitted. The final targeted run passed **31 checks**: 26
proposal tests, the MCP adapter workflow, and four schema-contract tests. Counts
overlap. `git diff --check` passed.

The full guarded suite run finished with **1,183 passed, nine skipped, 23 failed,
two errors and 13 live tests deselected**. One failure was an older provider
registration assertion still expecting 31 tools; updated to 32 and rechecked.
The other 22 failures match the recorded read-only baseline categories: 18
unavailable graph tests, three protected operational state/journal paths, and
the known unchanged-extraction-prompt assertion. The two probe setup errors
are sandbox socket-binding restrictions. This is **not** a passing full suite.
The broader run preceded the final save-path refinement below.

A final crash-path review changed publication to replace the existing record
in place, then atomically relocate that same file. An interruption between
those steps leaves one authoritative JSON decision. The timeout test now accepts
both valid deadline mechanisms' messages while retaining its elapsed-time and
no-publication checks. Final affected regression run: **76 passed**, covering
27 Notion proposal tests, legacy review/apply/presentation, provider interfaces,
the MCP adapter workflow and schema contracts. This includes the corrected
32-tool assertion and interruption fault injection. As with the existing wiki
store, review status checks are not serialized across processes; the guarded
write phase must extend its proposal lock to review/status transitions before
remote application is enabled.

A real MCP fixture proposal has not been staged: it requires operator authorization for
that new operational record and deployment/tool refresh for the new inspection
tool. No service restart, token/permission change, operational Notion proposal,
remote source mutation or remote application implementation was performed.
The guarded-write phase remains separately reviewable and unimplemented.


## Live staging acceptance and guarded apply — 2026-10-21

The operator reported restarting CMF and refreshing the tools, then explicitly
authorized the fixture staging/review workflow and subsequent guarded apply
implementation. The refreshed inspection tool returned the selected paragraph
at depth two under the toggleable “Installation notes” heading on “CMF charger
test” (`400944a6-5cf6-8038-9495-dfdd33d8a997`). Target block:
`400944a6-5cf6-80ed-b4a3-f94e406e0b69`.

CMF retrieved `WIKI/Household/EV-Electrician-Selection.md`, confirming Arnold
Electrical Services primary, Amp Electric second quote, and Kapital Electric
third. Proposal `notion-a4f30f46-6a65-440a-82b6-440d41ca6749` replaced only the
selected paragraph to include that shortlist. `get_doc_proposal` verified the
target, base text, ancestry, replacement and diff. Approval used review digest
`d84c3929119a366ec76fe1265503b13a86094362b08fa30c486874114c465102`.
The then-deployed apply dry run returned “Application for provider 'notion' is
not implemented; no source write occurred.” The approved fixture remains
unapplied. No additional service restart was performed.

Guarded apply is now implemented locally in `server/providers/notion/apply.py`.
It supports read-token dry runs and explicit read-only reconciliation through
`apply_doc_proposal(..., reconcile=True)`, retaining 32 tools. Real apply needs
both matching digests, approval, `CMF_NOTION_ENABLE_WRITES=true`, and a separate
`CMF_NOTION_WRITE_TOKEN`. Writer preflight proves access to the exact reviewed
target; the configured workspace label alone is never used to establish access.
Shared Unix file locks serialize review and Notion apply; attempts are fsynced
before sending at most one PATCH. A total ten-second deadline includes lock
and credential waits; preflight and verification share two GET retries.

Application audit is separate from the immutable review envelope. A started or
unknown attempt refuses another PATCH. Reconciliation observes desired content
and preserved fields (mark applied, attribution uncertain), exact unchanged
base (retry_ready for a later explicit apply), or conflict (new proposal).
Application state appears in list/get results. Cache invalidation drops every
credential/version variant for that page and per-page generations prevent an
overlapping old fill from publishing after invalidation.

The live test exposed per-request `request_id` fields in Notion GET responses.
Fresh comparisons exclude only those diagnostic fields; content, timestamps,
parent identities and ancestor evidence remain checked. Existing staged evidence
and its review digest are retained unchanged, with no queue migration/rewrite.
Verification permits Notion to change last-edit metadata and rich-text response
representation while requiring exact plain text and other block fields.

No token, service, dependency, graph, journal or external wiki changes were
made for implementation. No Notion PATCH was sent to a live source. Live
write acceptance and external-edit refusal remain pending separate explicit
operator authorization. The documented GET/check/PATCH race still applies.


Final guarded-apply verification: **214 passed**, four live tests deselected,
75 subtests passed. The run covered 21 guarded-apply tests, Notion staging and
retrieval, MCP schemas/adapters, legacy wiki staging/review/apply/presentation,
provider interfaces, knowledge routing, nightly review, and consolidation.
The process guard denied external service connections, non-temporary SQLite,
and operational queue writes; fixture transports and temporary stores handled
all source mutations. Fault tests include failed attempt persistence, 401/403/
429/500/529, pre/post-PATCH deadlines, verification failure, crash reconciliation,
concurrent applies, a separate process denied the same file lock, unchanged-base
retry authorization, and cache fill generation refusal. An initial test-only run
had four fixture/assertion failures; these were corrected before the final
passing regression run. `git diff --check` passed. No full-suite claim is made;
the earlier full-suite baseline limitations remain recorded above.

A final live read-only inspection confirmed the fixture paragraph still reads
“I plan to hire an electrician after the agreement is approved.” with the same
nested block identity and edit timestamp. The approved proposal remains staged
for future explicitly authorized write acceptance. Deployment/tool refresh for
the new apply contract, write credential provisioning, and the fixture PATCH
have not been performed.
