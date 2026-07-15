# Site Intelligence Profile

Site Intelligence Profile, abbreviated SIP, is Trawler's internal site knowledge
format. In Chinese, use "站点智能画像".

It is not a whitepaper. A whitepaper is usually an external strategy document;
SIP is an operational profile that agents can read before retrieval.

Each SIP records:

- `observed_at`: when the behavior was observed.
- `review_after`: when the profile should be checked again.
- `page_traits`: page behavior such as SPA, waterfall feed, login wall, virtual list.
- `recommended_extract_modes`: modes that work well for this site.
- `extraction_strategy`: practical retrieval steps.
- `human_assist`: when a visible browser or user login is likely needed.
- `validation`: what was tested and what worked.
- `known_limits`: risks the caller should keep in mind.

Agents should call `get_site_profile(domain)` before hard browser retrieval. If
the profile says the site is a rendered feed or SPA, prefer `bundle`,
`visible_blocks`, screenshot, and picker flows over article-style markdown alone.
