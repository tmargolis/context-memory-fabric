# Live Notion knowledge provider

**Status:** Approved by User, 2026-10-21. Implemented; offline verification recorded below. Live freshness checks remain outstanding.
**Date:** 2026-10-21

## Boundary and decisions

Add `server.providers.notion.provider:NotionKnowledgeProvider`, named `notion`,
implementing `KnowledgeSource.query()`. The older `KnowledgeProvider.search()`
interface returns wiki-specific `SearchResult` objects (`relative_path`,
`filename`, extraction status) and includes a filesystem rescan switch. The
multi-source loader and conformance contract call `query()` and expect
`KnowledgeResult` with provider, UUID, scope, version, and URI. Implementing only
`search()` would not participate; implementing both adds an unused adapter.

Retrieval, interleaving, error isolation, and context rendering need no core
changes (ADR 0002). **Scope amendment authorized by User, 2026-10-21:** permit a
small loader change in `server/knowledge.py` for built-in aliases `wiki`, `gmail`,
and `notion`, while retaining `module:Class` for external providers and existing
configurations. Default becomes `wiki`; aliases resolve before the existing
protocol and duplicate-name checks. No provider-specific retrieval enters core.
Update loader documentation/tests to describe the alias registry accurately.
Adding external providers still requires only a module and its config path. Notion remains external
durable knowledge: retrieval neither captures evidence nor writes memories,
journal entries, proposals, or wiki files. No sync job or snapshot.

Proposed files: `provider.py` (configuration, ranking, result mapping, shared
cache), `client.py` (HTTP, deadline, pacing), `blocks.py` (bounded traversal and
text rendering), and package `__init__.py`. New Python files use future
annotations and type hints. Docstrings explain the tradeoffs in the same style
as the Gmail provider. No `propose_change`: the existing proposal router reports
read-only/unsupported. A later proposal implementation would be a separate,
explicitly reviewed capability, never a write inside retrieval.

**Dependency approved by User, 2026-10-21:** `httpx==0.28.1` is now declared
directly and locked. It was already present transitively, but relying on that
is fragile. Its async client provides cancellable I/O and an injectable transport.

## Request flow and bounds

Pin `Notion-Version: 2026-03-11`. This version searches pages and **data sources**,
rather than the database objects used in older API examples. Search matches
titles, not arbitrary page bodies. Local ranking improves the returned
candidates; it cannot recover pages absent from those candidates. This v1 is
bounded title/property discovery followed by body retrieval, not workspace
full-text search. The property-query branch below broadens discovery for notes
organized in databases. [Search reference](https://developers.notion.com/reference/post-search)

For a single `query(text, max_results=10)`:

1. Blank, punctuation-only, stopword-only queries or nonpositive result limits
   return `[]` without I/O. No token also returns `[]`. Otherwise validate tuning
   and scope at query time and start one monotonic deadline, default **10 s**.
   `is_configured()` checks only nonblank token presence; it never accesses the
   network or filesystem. Construction performs no requests.
2. Enter the credential's process-local request gate; queue time counts against
   the deadline. If `CMF_NOTION_ROOT_PAGE_ID` is set, `GET /v1/pages/{id}` verifies
   that this expected knowledge root remains accessible and untrashed. This is
   a visibility sentinel, not an instruction to enumerate its descendants.
3. `POST /v1/search` with the user's text, page-only filter, explicit descending
   `last_edited_time` sort, and `page_size=C` (**C=5** by default, independent of
   requested result count). Follow `start_cursor` only while fewer than C usable
   unique pages have been collected, with at most **two search requests** and
   **2C inspected hits**. Reject repeated/missing cursors when `has_more` is true.
   Drop archived/trashed and non-page hits. Also query up to **D=2 configured
   data sources**: `GET /v1/data_sources/{id}` reads each schema, followed by
   `POST /v1/data_sources/{id}/query` with an OR of `contains` conditions over
   title/rich-text properties and query terms. Use at most eight properties
   (title first, others by stable property ID), six distinct terms (first
   occurrence order), and two query requests per data source including cursor
   pagination. Each request uses `page_size=C`; inspect at most `(2 + 2D)C`
   raw hits across both branches. Reject repeated/missing cursors. The source
   IDs are explicitly configured, not discovered by enumerating the workspace.
   Deduplicate the merged pool by UUID, then select at most C pages by weighted
   lexical overlap over title and returned text properties, edit time
   descending, and UUID ascending. Finish this selection before any body fetch.
   Property-only matches are eligible even with no title-search hit.
4. For every candidate, `GET /v1/pages/{id}` refreshes edit time, visibility,
   title, and parent. This costs C additional calls, deliberately avoiding cache
   validation against an eventually consistent search index. A now-trashed page
   is removed; 403/404 invalidates its cache entry and fails the source with a
   sharing/access diagnostic. For each remaining page, reuse its body only if
   `(page UUID, last_edited_time)` matches the cache; otherwise fetch
   `GET /v1/blocks/{page_id}/children?page_size=100`, recursively following child
   IDs and pagination cursors. Limit each page to **three block-list requests**,
   depth **four** (top-level blocks are depth one), and **64 KiB UTF-8 text**.
   Traversal is depth-first in API sibling order. Caps yield explicitly marked
   truncated text; a transport failure does not yield a partial success.
5. Resolve ambiguous/untitled titles using known parent data, then at most
   **four ancestor requests total**, using `GET /v1/pages/{id}`,
   `/v1/data_sources/{id}`, or `/v1/databases/{id}` as the parent type requires.
   Limit ancestry to four levels and detect cycles. Reuse ancestor data within
   this query only. If the allocation is exhausted, include the parent type and
   UUID as the unresolved ancestry segment, plus page UUID if still ambiguous.
   An actual HTTP failure follows the same source-error policy.
6. Rank all fetched/cached candidate text locally and return at most
   `min(max_results, C)` positive-score results. Check the deadline through
   parsing, flattening, scoring, and immediately before returning/publishing
   cache entries. Never serve a body for a page that was not discovered and
   freshly validated in this query.

Let U be cache misses, A ancestor requests, V the optional sentinel (0 or 1),
and R retries. HTTP attempts are bounded by **2 + 3D + C + 3U + A + V + R**,
where D≤2 (one schema + up to two query calls each), A≤4, and R≤2 globally.
At default C=5 with no configured data sources, the cold maximum is **29
attempts**; enabling two data sources raises that maximum to **35**.
Ordinary flat pages without the property branch use **11** (one search + five metadata + five body calls),
and a warm query uses **6**, absent ancestor/sentinel calls. Property discovery
adds D schema calls plus up to 2D query calls on both cold and warm paths. Larger result
requests do not expand these limits. A full traversal is not promised: both
request caps and the deadline can stop it.

All calls share plan-aware pacing with no bursts, keyed by credential across
provider instances: `CMF_NOTION_PLAN=free|plus|business|enterprise`, default
`free`. Space request starts by at least 1/3 s for Free/Plus and 1/10 s for
Business/Enterprise. Also enforce the corresponding rolling 60-second budget
(180/600 starts); a plan label is an operator assertion, not verified licensing.
If instances sharing a token disagree, apply the lower budget. Twenty-nine
starts span at least 9.33 s at 3/s or 2.8 s at 10/s; 35 span 11.33 s or 3.4 s.
Network latency and the deadline can prevent reaching the attempt maximum.
The plan setting never overrides server cooldowns or increases the candidate,
retry, traversal, or overall time budgets. Worst-case caller latency is the configured
deadline plus normal scheduling overhead, not attempt-count × socket timeout.

The synchronous protocol runs a cancellable async HTTP operation in a bounded
daemon worker, so it also works when invoked from an existing event loop
(as `get_context` currently does). The caller waits only for its remaining
deadline; expiry signals cancellation and raises without joining cleanup.
Per-credential admission permits one active query worker, preventing abandoned
workers from accumulating; an occupied slot consumes the next query's budget.
HTTP connect/read/write/pool limits are each capped by remaining time, with an
enclosing async deadline as well. Cancellation forbids later requests or cache
publication, closes the client, and releases admission after cleanup. Tests
must demonstrate the caller bound with stalled and trickling transports, not
just a fake clock. No additional core changes or persistent worker service are required beyond
the approved alias-loader amendment.

## Throttling and failures

On 429 (and documented 529 overload), read integer-seconds `Retry-After`, extend
the credential's shared cooldown, and retry only after that delay fits within
the deadline. At most **two retries across the entire query**. Never shorten a
server wait to squeeze in a retry; if it cannot fit, fail immediately while
retaining the cooldown for subsequent calls. Missing/malformed headers use
bounded 1 s / 2 s backoff. A documented blocked-access rate-limit reason is
terminal. No retries for other HTTP statuses or network errors in v1.

Current docs describe 180 requests/minute on most plans, 600 on Business and
Enterprise, plus a shared workspace limit; `Retry-After` can exceed a minute.
Plan-aware pacing is not a guarantee against 429s, especially when other
processes use the same integration or workspace.
[Rate-limit reference](https://developers.notion.com/reference/request-limits)

Bad token/401, 403/404, network failure, malformed response, exhausted retries,
or deadline expiry raises a sanitized `ProviderConfigurationError` from
`query()`. This follows the requested provider convention even though the
shared exception's docstring describes configuration errors more narrowly.
`search_knowledge.errors['notion']` and the existing context error text expose
the failure while other sources still answer. No stale-cache fallback and no
timing-dependent subset on timeout. Errors mention status/category and a safe
action, never raw response bodies, headers, credentials, or request objects;
suppress exception chaining containing those objects.

## Credentials, scope, and visibility

Store the token and all deployment-specific settings in the gitignored `.env`
(or injected environment/secret store); commit only empty/generic examples in
`.env.example`. Existing `.gitignore` already excludes `.env` and `.env.*` while
allowing `.env.example`. Do not read, rewrite, or print the operational `.env`
while developing the provider.

Use an internal integration token in `CMF_NOTION_TOKEN`. This is CMF's **first
third-party knowledge-source credential held by the server itself**; Gmail
deliberately holds none. CMF already holds model-provider API keys, so this is
not literally its first third-party credential overall. Compromise of the CMF
process/environment would now expose the integration's accessible notes.

Create a dedicated integration with **Read content only**, no update/insert,
comments, or user-information capabilities, and share only the intended notes
or knowledge root with it. Tokens stay out of logs, exceptions, metadata, test
fixtures, and committed configuration. Send authorization only to the fixed
HTTPS Notion API origin; disable redirects, ambient proxy configuration, and
HTTP debug logging in this client. Validate UUIDs before constructing paths.
[Capabilities](https://developers.notion.com/reference/capabilities)

`CMF_NOTION_WORKSPACE` is a required operator-supplied workspace UUID or stable
label, yielding `private:notion:<workspace>`. It is provenance, not a verified
workspace identity or authorization mechanism. Token present with missing scope
is configured-but-broken at query time.

Notion only exposes shared content. The API cannot distinguish an unshared
missing page from a nonexistent one; a successful search cannot prove coverage.
Search also has indexing delay and no completeness guarantee.
[Search limitations](https://developers.notion.com/reference/search-optimizations-and-limitations)

The setup instructions must tell users to share the intended root, search for
a known note, and configure the optional root sentinel for an actionable error
when that root loses access. Emit a once-per-credential, token-free warning:
“Notion searches accessible titles and configured database text properties;
page bodies outside those candidates are not searched. Missing notes may need
sharing or indexing time.” Repeat an actionable diagnostic on empty searches; an empty
result means no matches in the bounded accessible discovery scope, not an empty
workspace. Include
`coverage='accessible_title_property_candidates'` in every result's metadata. Existing
rendering does not display arbitrary metadata or successful-empty warnings;
without a core change, logs and setup documentation are the available warning
surfaces for an empty result. This visibility limitation remains explicit;
even a working sentinel cannot certify access to every intended note.

Database containers/data-source schemas are not documents in v1. Database row
pages are retrieved like ordinary pages after either discovery branch.
Child-page/database blocks contribute their labels only, not embedded copies of
independent documents.

### Property filters and broader content discovery

`POST /v1/data_sources/{id}/query` supports compound `and`/`or` property filters,
including text, numeric, date, select/status, people, and relation conditions.
A `Summary` rich-text property is searchable there; paragraphs inside the page
body are blocks and are not covered by those conditions. Use the current
endpoint, not the deprecated database-query route.
[Query](https://developers.notion.com/reference/query-a-data-source),
[filters](https://developers.notion.com/reference/filter-data-source-entries)

V1 automatically translates query terms only to title/rich-text `contains`
filters; include those returned text properties at normal body weight in local
BM25 and excerpt selection. For operator-defined restrictions such as status,
date, number, people UUID, or relation UUID, allow an optional
`CMF_NOTION_DATA_SOURCE_FILTERS` JSON object mapping configured data-source UUIDs
to native Notion filter objects. AND each restriction with the generated text
OR; validate the shape and schema properties/types before requesting. Limit
JSON to 16 KiB, 48 leaf conditions and two compound levels, and reject unknown
source IDs. Combining restrictions with generated text conditions normalizes
the restriction to bounded AND-of-OR clauses so the final request stays within
two compound levels; reject expansions exceeding 48 restriction conditions. Filters apply to that branch only, not workspace title search;
this is retrieval tuning, not an access control boundary. Do not infer typed
conditions from natural language or invoke an LLM to do so. Supporting the
native filter shape avoids inventing a second filter language.

Notion offers broader content search through its **hosted MCP** (`notion-search`,
plus plan-dependent AI search). This is distinct from REST `/v1/search`, so
“title-only is the best Notion offers” would be incorrect. Hosted MCP uses
OAuth or a separate workspace-owned MCP token (Business/Enterprise), rather
than assuming a REST internal integration token works there. Moving v1 to that
backend would change credentials, permissions, response mapping, and API
contract; keep it an explicit design decision if discovery across arbitrary
page bodies is required. Do not silently substitute it or scrape Notion.
[Tools](https://developers.notion.com/guides/mcp/mcp-supported-tools),
[authorization](https://developers.notion.com/guides/mcp/get-started-with-mcp#faqs)

## Text, ranking, cache, and provenance

Flatten rich text to plain text, preserving Unicode. Index headings, paragraphs,
bulleted/numbered items, checked/unchecked to-dos, toggles, callouts, quotes,
code (including language), table rows (cells separated by ` | `), equations,
and available captions. Newlines separate blocks; two spaces per depth retain
nesting. Columns/tables are traversal containers. Do not download images,
files, video, audio, embeds, link previews, or external URLs; retain their
captions where supplied. Drop decorative icons, dividers, breadcrumbs, generated
TOCs, unsupported payloads, and duplicate synced blocks (`synced_from != null`).
Traverse original synced blocks only, with visited-ID protection. Mark depth,
request, and byte truncation in metadata and the visible excerpt suffix.
These rendering and exclusion rules are product choices based on the
[block schema](https://developers.notion.com/reference/block), not API behavior.

BM25 follows Gmail's scoring ideas (k1=1.4, b=0.75, title tokens weighted ×3),
implemented locally without importing Gmail internals or refactoring it. Use
Unicode-aware casefolded tokens and stable sorted unique query terms to avoid
hash-iteration floating-point differences. Build statistics over the selected
candidate set. Sort by descending rounded score, descending edit timestamp,
then canonical page UUID. Excerpts use the best matching 280-character window
with the earliest window winning ties; append truncation markers outside that
limit. Same corpus and candidate set produce identical IDs/order even when API
response order changes. Notion does not document stable candidate membership
or tie ordering at a search cutoff; local ranking cannot promise determinism
across changes to that set. Conformance uses a fixed offline corpus.

The process-level, thread-safe LRU holds at most 128 page bodies, each ≤64 KiB,
namespaced by credential fingerprint, workspace, API version, and flattening
policy; inside that namespace keys are `(page UUID, last_edited_time)`. The
fingerprint is never emitted. Replace older versions on admission of a changed
timestamp. Coordinate overlapping misses through the per-credential gate;
waiting queries reuse a completed fill. Fixture transports get isolated cache
namespaces. Provider reconstruction retains the cache, necessary because
`load_knowledge_sources()` constructs fresh instances. **The cache does not
survive process restarts:** cold fetches are acceptable, persisted private notes
would expand the storage/security boundary, and no snapshot needs maintenance.
Never cache query errors; cache deterministic capped text with its truncation
flags. No cached search results, persisted files, or background refresh.

| KnowledgeResult field | Mapping |
| --- | --- |
| `provider`, `document_id` | `notion`; canonical page UUID, stable through renames/moves |
| `title` | Title property selected by type, independent of property name; ancestry for duplicates/untitled pages; UUID fallback as above |
| `excerpt` | Matching flattened text, falling back to title; explicit truncation suffix |
| `uri` | Validated Notion page URL, otherwise `https://www.notion.so/<UUID-without-dashes>` |
| `source_timestamp` | Aware UTC `last_edited_time`: currency of the retrieved note, not when it was originally created |
| `source_version` | Original ISO `last_edited_time`; creation time cannot version mutable content |
| `retrieval_score`, `retrieval_method` | Rounded BM25 score; `bm25` (scores comparable only within Notion) |
| `scope` | `private:notion:<CMF_NOTION_WORKSPACE>` |
| `metadata` | Original created/edit times, parent page/data-source/database IDs, resolved ancestry, coverage, truncation reasons; normalized returned title/rich-text, select/multi-select/status, number, checkbox, date, URL, formula/rollup scalar properties |

Drop people/contact details, file URLs, relation expansion, and schema objects
from metadata; retain relation IDs and note incomplete property representations
without extra property-fetch fan-out. Returned title/rich-text properties join the selected page body for ranking;
other property values remain provenance/filter data. Page timestamps and parent types are documented in the
[page reference](https://developers.notion.com/reference/page).

## Proposed .env.example addition

Place after the existing Gmail entries, leaving the active provider list and
real `.env` untouched. Users without a vault may select Notion alone and leave
`LLM_WIKI_PATH` empty; alongside Wiki, use the combined example below.

```dotenv
# Notion queries the live API; no local snapshot or sync job. Enabling it puts
# note excerpts into get_context for EVERY connected client.
# CMF_KNOWLEDGE_PROVIDERS=wiki,notion
# Internal integration: Read content only; share only intended knowledge pages.
# Token is held by the CMF server. Never commit it or include it in logs.
# CMF_NOTION_TOKEN=
# Required stable workspace UUID/label for private:notion:<workspace> provenance.
# CMF_NOTION_WORKSPACE=
# Optional expected shared root: inaccessible/trashed root fails visibly.
# CMF_NOTION_ROOT_PAGE_ID=
# Workspace plan controls pacing (free/plus: 3/s; business/enterprise: 10/s).
# CMF_NOTION_PLAN=free
# Optional data-source UUIDs, at most two; searches title/rich-text properties.
# CMF_NOTION_DATA_SOURCE_IDS=
# Optional native filters keyed by those UUIDs (JSON; example uses a generic ID).
# CMF_NOTION_DATA_SOURCE_FILTERS={"00000000-0000-0000-0000-000000000001":{"property":"Status","status":{"equals":"Published"}}}
# Discovery is access-limited; verify a known note after sharing.
# Bounds include pagination, retries, pacing, and all HTTP calls per query.
# CMF_NOTION_CANDIDATE_LIMIT=5
# CMF_NOTION_TIMEOUT_SECONDS=10
```

Candidate limit accepts integers 1–20; timeout accepts finite seconds 1–30.
Reject invalid values, unknown plan names, and invalid source/filter config at
query time. Other caps remain code constants in v1.

## Verification and remaining assumptions

`tests/test_notion_provider.py` mixes `KnowledgeSourceConformance` into a
`unittest.TestCase` using an injected offline fixture transport (search, page
metadata, ancestors, and block children). Fingerprint fixture content, not its
request log. No real token or network is needed; forbid unexpected outbound
traffic. Cover Unicode/odd queries, zero results, read-only proposal routing,
alias/path loading (including mixed specs, unknown aliases, and duplicates),
and errors isolated beside a fake source.

Focused unit cases cover every text type/exclusion, nested tables/lists, caps
and truncation, reordered search hits and tied scores, bounded search/block
pagination and bad cursors, schema-driven property discovery, property-only
matches, compound typed restrictions and invalid filters, candidate caps across
both branches, archived/trashed filtering, duplicate ancestry,
401/403/404 and redaction, 429/529 cooldown and Retry-After (including over-budget
and malformed values), all four plan names, request spacing/minute budgets,
conflicting settings for the same token, global retry/request caps, hard
deadlines and cleanup,
cache hits across fresh provider instances/overlapping queries, changed edit
timestamps, failed fetches, and credential/fixture namespace isolation.

Add a few opt-in `@pytest.mark.live` read-only checks: real search/body retrieval,
known nested content/pagination and provenance, and timestamp invalidation after
an operator edits a dedicated fixture page manually. Do not edit workspace
content from tests. Require explicit dedicated test credentials/page IDs;
otherwise skip. Existing pytest `-m 'not live'` excludes these. Generalize the
marker's Gemini-only description to external APIs when implementing.

After implementation, run `uv run pytest tests/test_notion_provider.py -v`, then
`uv run pytest` only after auditing broader tests' targets/isolation, including
the existing `cmf_test` graph safeguards and temporary state paths. Do not run
live tests, operational acceptance scripts, or touch persistent data merely to
validate this provider. Expected nine off-disk Gemini fixture skips must be
reported against actual output, not assumed. See the implementation verification record below.

**Confirmed from current docs:** REST title-only search, typed/compound data-source
property filters (not page-body filters), hosted MCP content search with distinct
authorization, plan-dependent request budgets, data-source objects for the
pinned version, incomplete/eventually consistent discovery, single-level block
responses and cursor pagination (possibly short pages), permission-related
404s, read-content capabilities, and integer-seconds Retry-After on 429/529.
Search ranking stability is **not guaranteed** by the docs; deterministic local
ranking is our policy, conditional on candidate membership.

**Unconfirmed assumptions / live acceptance gates:** (1) edits to all nested
blocks advance the containing page's `last_edited_time`, with enough precision
for cache invalidation; the page docs define update time but do not guarantee
that propagation or atomic reads. (2) Fresh page metadata and subsequently
fetched blocks describe the same revision during concurrent editing; Notion
offers no pinned-revision read here. (3) Actual property payload completeness
for the pinned version needs fixture-workspace verification; retain only values
returned, never invent them. No live API was called in preparing this design.
If nested edit propagation fails, timestamp-only caching cannot promise fresh
unchanged-page reuse: stop and revise this design before claiming that behavior.
Search indexing delay also means a newly shared/renamed page may be temporarily
undiscoverable even though its body would be fresh if selected.


## Implementation verification (2026-10-21)

The provider modules, aliases, direct dependency/lock entry, `.env.example`, and
setup instructions are implemented. No deployment settings were changed, no
live tests were run, and no operational services or data were modified.

- Focused command: `uv run pytest tests/test_notion_provider.py -v` — **58 passed,
  3 live tests deselected** (plus 9 passing unittest subtests). UV used the
  existing environment (`UV_NO_SYNC=1`) and a temporary cache directory;
  FalkorDB was directed at an unused loopback port, not operational services.
- Related command: `uv run pytest tests/test_notion_provider.py
  tests/test_ms5_knowledge_core.py tests/test_ms5_gmail_provider.py -v` —
  **100 passed, 3 live tests deselected**, plus 12 passing unittest subtests.
- Repository-wide `uv run pytest` was attempted with an external, temporary
  isolation plugin and audit guard: **1,158 passed, 9 skipped, 13 live tests
  deselected, 22 failed, 2 errors**. This is not a passing full-suite result.
  The plugin redirects the import-time default journal to temporary SQLite;
  the guard blocks external-service connections and writes to operational files.
  Eighteen failures require FalkorDB, three require an operational journal/path
  (including a child-process OAuth setup), and two errors require local socket
  binding unavailable in this sandbox. One additional extraction-prompt assertion
  fails outside the changed files. These tests were not altered to hide failures.
  The final two extra Notion regression tests were added after that full run's
  collection and are included in the focused/related results above.
- `git diff --check` and Python compilation of new modules/tests passed.
  The full-suite log is `/private/tmp/cmf-notion-full-pytest.log`; the final
  related-test log is `/private/tmp/cmf-notion-final-pytest.log`.
- Live checks use dedicated test credentials and manually maintained fixtures.
  The timestamp-propagation, revision-race, and property-completeness assumptions
  above remain unverified against a real workspace; offline fixtures do not
  establish those API guarantees.


### Live step-6 checks (2026-10-21)

At User's request, the Notion credentials/workspace/plan were read from the
existing `.env` without modifying or printing it. A bounded API search found
the uniquely named `CMF charger test` page and verified that it had at least
two top-level blocks. Query and canonical page IDs were passed in process
memory to the existing live tests; no additional fixture variables were
required in `.env`.

`test_live_known_page` and `test_live_block_cursor_pagination`: **2 passed in
6.65 seconds**. One benign anyio assertion-rewrite warning came from invoking
pytest after the discovery client had already imported anyio. The sandboxed
network attempt failed; the authorized network-enabled retry succeeded.
FalkorDB checks were directed at an unused loopback port. No Notion content,
operational journal, provider configuration, or service state was changed.

This confirms live title discovery, provider retrieval/provenance, and block
cursor pagination for the shared fixture. The manually edited nested-page
version test remains unrun; parent timestamp propagation, concurrent revision
consistency, and full property completeness are still unverified. The optional
data-source/property-filter settings were not needed for these page tests.


### Actual CMF MCP checks after server restart (2026-10-21)

The first MCP checks failed because the old running loader did not recognize
`notion`. After User restarted the server, both connected-app and native
`search_knowledge` returned the shared fixture with its paragraph, nested note,
and stable provenance. `get_context` also returned an attributed Notion section
beside wiki and episodic results, with no provider error. This chat's tool
schemas needed no manual refresh.

Server request logs confirm unchanged-page body-cache reuse across MCP calls:
the first search read metadata and two block-child lists, and the later search
read search results/metadata only. Nested-edit invalidation remains untested.
The first context call spent roughly 71 seconds rebuilding the cold wiki index
before episodic retrieval; this was outside Notion's query deadline. MS11 retains
the outstanding full-suite and live freshness checks rather than treating basic
MCP retrieval as completion of every acceptance gate.


### Manual edit and cache invalidation via MCP (2026-10-21)

The same running server had cached fixture version `2026-10-21T16:15:00.000Z`.
After User's manual edit, actual MCP `search_knowledge` returned version
`2026-10-21T16:44:00.000Z` and the added electrician/agreement sentence. Logs
showed page and toggle children refetched on the changed-version query (1.4 s),
then only search + fresh metadata on a repeat query (1.1 s), with identical
updated text/version. No restart, config change, content write by the assistant,
or snapshot refresh occurred between those queries.

This closes the manual-edit timestamp/cache check for this fixture. It does
not prove atomic reads during concurrent edits, complete property responses,
or edit-time propagation for every supported block type. Those limitations
and the previously recorded broader-suite results remain explicit in MS11.
