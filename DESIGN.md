# Trawler Design Constitution

Trawler is a crawler and user-authorized browser retrieval MCP server for AI agents.

## Why MCP

MCP is the right shape for Trawler because the caller is an agent, not a human
operator. The server exposes narrow, auditable tools that fetch data, save raw
provenance, and return bounded markdown or JSON summaries. Browser control,
cookies, proxies, robots policy, and SSRF checks stay behind the tool interface
instead of being reimplemented by every agent prompt.

## Boundaries

- Fetch web pages and return clean Markdown.
- Retrieve a single user-directed page through AuthorizedBrowserRetrieval when
  the user can access that page in a real browser.
- Map links from raw DOM when an agent needs a frontier preview.
- Save fetched output as raw Markdown files for downstream modules.
- Do not call LLMs.
- Do not implement retrieval, article writing, PDF parsing, or UI.

## Tool Contract

`retrieve_page` is the primary single-page task interface. It exposes product
intent directly: `access_mode`, `human_assist`, `extract_mode`, and optional
`selector`. `access_mode="user_authorized"` is for a single explicit page task:
it skips the robots precheck for that page, prefers a real browser path, does
not use external Jina reader fallback, reuses encrypted AccountState, and opens
HumanAssist when login or verification is needed.

`open_browser_session` / `extract_browser_session` / `close_browser_session`
are the live browser workflow. Use them when a human must operate the page
before extraction. The browser remains visible and alive between MCP calls; the
agent later extracts the current page state as markdown, visible text, selector
content, picked element, picked region, screenshot image content, bounded HTML,
an accessibility snapshot, visible content blocks, fit markdown with citations,
an extraction bundle, or a page clone snapshot with key computed CSS. `start_element_picker` and
`start_region_picker` inject temporary browser overlays so the human can select
the target directly in the page.

`run_browser_actions` executes explicit BrowserActions inside a LiveBrowserSession
before extraction. `extract_browser_session` can also receive an `actions` list
so agents can do "click, wait, extract" as one auditable MCP call. Navigating
actions still pass URL canonicalization and SSRF checks. BrowserAction audit
records store action type and status, not typed text.

`connect_browser_session` connects to an existing Chromium/Chrome CDP endpoint
when the user already has the right browser profile open. CDP endpoints are
localhost-only by default; remote CDP requires `TRAWLER_ALLOW_REMOTE_CDP=1`.
Trawler does not own or persist that external browser profile, but still checks
web URLs and installs a request route guard. Browser sessions fail closed if the
route guard cannot be installed unless `TRAWLER_ALLOW_UNGUARDED_BROWSER=1` is
set for a trusted environment.

The legacy public tools return strings.

- Success: `__TRAWLER_OK__:\n\n<content>`
- Failure: `__TRAWLER_ERROR__:{json}`

The JSON error payload must include `errorType`, `message`, `retryable`, and `suggestedAction`.

New structured variants keep that same text in the MCP content channel and add
machine-readable `structuredContent` for clients that support it. This is a
compatibility layer, not a replacement for the legacy string contract.

`get_site_profile` returns the Site Intelligence Profile (SIP, "站点智能画像") for
a domain. Agents should consult it before difficult retrieval so they know
whether to use browser actions, visible blocks, screenshots, picker flows, or
standard article markdown.

`get_retrieval_readiness` is the preflight tool for hard pages. It combines SIP,
AccountProfileRegistry, encrypted vault presence, account status, and a
recommended next MCP call so agents do not blindly try retrieval paths.

`register_account_profile`, `list_account_profiles`, and `mark_account_profile`
manage the Account Profile Registry ("账号画像登记表"). These tools store account
metadata and vault paths only. They never store plaintext passwords. A caller
binds browser retrieval to an account with `account_id`; the visible browser is
where the human logs in or refreshes verification.

## Safety Defaults

- SSRF protection is on by default.
- `robots.txt` is respected by default.
- Account state is encrypted when persisted.
- Browser-returned cookies are encrypted when persisted.
- Account profiles store metadata only; secrets stay out of the registry.
- Scraped page content is untrusted input.
- Runtime data stays under `data/` and must not be committed.

AuthorizedBrowserRetrieval does not disable SSRF protection or crawl scope
checks. It is single-page by design; multi-page crawl jobs remain policy-bound
frontier crawls.

## Session Identity

Browser-facing fetches use a BrowserSession. A session binds domain, account
state, proxy URL, encrypted cookie jar, storage-state presence, and fingerprint
identity. Callers should not mix cookies or fingerprints across sessions.

An AccountProfileRegistry row names the account side of that identity. The
default account keeps the historical domain-level vault layout, while named
accounts use isolated `accounts/<account_id>/` vault paths. This preserves
backwards compatibility while allowing multiple real logins for the same site.
Only active, unexpired profiles are selected automatically; expired, needs-login,
or blocked profiles route the caller toward visible-browser recovery instead of
silently reusing stale browser state.

Session health is tracked by success count, error score, last error, and
retirement status. Fetchers should mark sessions good on successful extraction
and mark or retire them on bot blocks or expired account state.

Proxy selection is session-aware. When `TRAWLER_PROXY_POOL` is configured, a
stable proxy is selected from the pool for a domain/account identity and then
stored on the BrowserSession so browser, curl, Jina, and HITL fetches agree on
the same network identity.

LiveBrowserSession reuse is intentionally narrower: a local persistent session
is reused only when domain, account_id, access mode, adapter type, and proxy
binding match. This avoids mixing account state or network identity just because
two tasks share the same domain.

## Crawl Frontier

Multi-page crawl jobs use a persistent SQLite frontier. The frontier owns URL
discovery, de-duplication, per-URL status, errors, and paginated results. The
in-process worker only leases frontier work; callers inspect jobs through
status, errors, results, and cancellation tools.

`discover_site_index` is a bounded pre-crawl discovery tool. It reads robots
Sitemap declarations, common sitemap locations, sitemap indexes, RSS/Atom feeds,
and HTML feed hints to produce seed URLs. It does not crawl every discovered URL.
Redirects are followed manually so each hop is checked by the SSRF guard before
the next request is sent.

`crawl_site_indexed` uses that discovery step as a bounded seed source for a
frontier job. Retryable per-URL errors are returned to the frontier with
exponential backoff up to `TRAWLER_FRONTIER_MAX_RETRIES`; non-retryable failures
become terminal error rows. Aggregated crawl output includes fetched page bodies
without raw frontmatter and a compact error summary.

Frontier crawls use one crawl policy across discovery seeds, page-discovered
links, and final redirect targets: `max_depth`, `include_paths`,
`exclude_paths`, `include_subdomains`, and `ignore_query_parameters`. URLs that
fall outside that policy are not enqueued; fetched pages whose `final_url`
escapes the policy return `blocked-scope` before parsing or raw success storage.
The lightweight HTTP fetcher also rebuilds DNS pinning on every redirect hop so
SSRF checks apply to the actual host being requested.

## Cache Policy

Single-page crawls expose explicit cache modes:

- `enabled`: read cached raw content first, then fetch and record fresh results.
- `read_only`: return cached raw content or a `cache-miss` error; never fetch.
- `write_only`: fetch fresh content and record it; do not read old cache entries.
- `bypass` / `disabled`: fetch fresh content without recording a seen-url cache entry.

Refreshing cache is separate from robots policy. `bypass_robots` must be explicit.

## Debug Artifacts

Failures can produce out-of-band debug artifacts under `data/artifacts/`.
Artifacts keep MCP responses small while preserving inspectable evidence:
metadata, bounded HTML snapshots, browser console messages, request failures,
and screenshots when a browser page is still available.

Error payloads may include `artifact_id`. Callers can inspect artifacts through
`list_artifacts`, `get_artifact`, or `artifact://{artifact_id}` without embedding
binary data or large HTML directly in tool responses.

Artifact retention is bounded by age and total bytes. Operators can use
`cleanup_artifacts` in dry-run mode first, then delete only safe direct child
artifact directories with valid metadata.

## Extraction Signals

Site rules may define CSS `selectors`. When a selector matches, Trawler crops the
parser input to those DOM fragments while retaining full-DOM link discovery.
Selector matches and selector errors are recorded in raw metadata.

Raw metadata also records deterministic quality signals: character count, line
count, heading count, markdown link count, table count, code block count, and
whether parser input was truncated. These are provenance hints for agents, not a
semantic quality score.

Non-HTML text responses are adapted before the HTML parser: JSON becomes fenced
pretty JSON, RSS/Atom becomes a link list, and plain text is returned bounded.

Fit markdown is a bounded post-processing view for agent context. It normalizes
blank lines, truncates at readable boundaries, and extracts HTTP(S) markdown
links into a citations list. It does not invent citations or call an LLM.

Visible content blocks are a rendered-page fallback for SPA feeds, waterfalls,
marketplaces, and dashboards where article-style markdown extraction is the
wrong shape. The browser inspects visible DOM elements, records text plus rects,
deduplicates repeated containers, and includes the result in bundles without
requiring site-specific code.
