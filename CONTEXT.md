# Trawler Domain Context

## AuthorizedBrowserRetrieval

AuthorizedBrowserRetrieval is a single-page retrieval task directed by a user who
can access the target page in a real browser. It is not multi-page crawling and
not stealth bypass. The system tries automated browser retrieval first, keeps
SSRF protections, skips the robots precheck for that explicit page task, and
hands control to the human when login, verification, or page interaction is
needed.

## HumanAssist

HumanAssist is the visible-browser handoff. In `auto` mode Trawler opens it when
the ladder detects login or verification. In `required` mode Trawler opens it as
the first step. In `off` mode Trawler must not open a human window and should
return a clear error instead.

## AccountState

AccountState is encrypted browser storage and cookies saved after a human or
browser session succeeds. It lets later requests use normal browser state
without asking the user to log in again. AccountState requires
`TRAWLER_VAULT_KEY`.

## AccountProfileRegistry

AccountProfileRegistry, called "账号画像登记表" in Chinese, is the metadata
registry for user-approved site accounts. It records domain, account_id, label,
status, login method, vault paths, verification time, expiry time, notes, risk
flags, and default-account selection. It does not store plaintext passwords.
AccountState remains encrypted in account_vault; the registry only tells agents
which account identity and vault paths belong together.

## BrowserSession

BrowserSession binds domain, account identity, proxy, storage-state presence,
and fingerprint identity. Fetchers should keep these together so one logical
user/browser identity is not accidentally mixed with another.

## LiveBrowserSession

LiveBrowserSession is a visible browser kept open across MCP tool calls. A human
can operate the page directly, click an element picker, drag a region picker,
then an agent can extract the current state by selector, visible text, full-page
markdown, screenshot, bounded HTML, picked element, picked region, page clone,
an accessibility snapshot, visible content blocks, an extraction bundle, fit
markdown with citations, or an element snapshot with key computed CSS. It is the right interface when
the page state cannot be reached by a single fetch. A LiveBrowserSession may be
opened by Trawler as a local persistent browser or connected to an existing
local CDP browser endpoint when the user explicitly wants to use an already
open browser profile.

## BrowserAction

BrowserAction is a user-directed operation performed inside a LiveBrowserSession
before extraction: click, fill, type, press, scroll, wait, wait_for_selector,
goto, check, uncheck, or select_option. Actions are not stealth automation; they
are the MCP equivalent of operating the visible browser state the user asked for.
Navigating actions still pass URL canonicalization and SSRF checks.

## ExtractionBundle

ExtractionBundle is a bounded, multi-view output from the current browser state:
fit markdown, citations, visible-text excerpt, visible content blocks, DOM links,
accessibility snapshot, page clone with key CSS, optional screenshot image bytes,
and an artifact id when artifact capture is enabled. It gives an AI caller enough
perspectives to answer or reconstruct UI content without embedding unbounded HTML
in structured output.

## SiteIntelligenceProfile

SiteIntelligenceProfile, abbreviated SIP and called "站点智能画像" in Chinese, is
the operational memory for a domain. It records observed page traits, extraction
strategy, human-assist expectations, validation time, review time, and known
limits so agents can choose the right retrieval path before touching the page.
It is not a whitepaper; it is a compact, executable site profile.

## RetrievalReadiness

RetrievalReadiness is the preflight report for a URL or domain. It combines the
SiteIntelligenceProfile, AccountProfileRegistry, encrypted vault presence,
account usability, and a recommended next MCP call. Agents should use it before
hard retrieval so they choose a browser, account, human-assist mode, and
extraction mode deliberately.

## PageExtraction

PageExtraction is the final output step after a page is available. The current
modes are whole-page markdown, visible-text markdown, CSS-selector extraction,
current DOM HTML, element snapshot, picked element, picked region, page clone,
accessibility snapshot, visible content blocks, fit markdown with citations,
extraction bundle, and screenshot artifact/image output.
