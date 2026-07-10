# Account Profile Registry

Account Profile Registry, called "账号画像登记表" in Chinese, records the
operator-approved account identities that Trawler may bind to a site. It is
metadata, not a password manager.

## What It Stores

- `domain`: normalized site domain.
- `account_id`: stable local id, such as `default`, `work`, or `personal`.
- `label`: human-readable label.
- `status`: `active`, `expired`, `needs_login`, or `blocked`.
- `login_method`: `manual_qr`, `manual_password`, or `imported_state`.
- `profile_dir`, `storage_state_path`, `cookie_jar_path`: vault paths.
- `last_verified_at`, `expires_at`: lifecycle timestamps.
- `notes`, `risk_flags`: operator notes for agents and audits.
- `is_default`: the default account profile for that domain.

It does not store plaintext passwords, OTP seeds, recovery codes, or raw secret
values. Notes must not contain passwords.

## Vault Layout

The default account keeps the historical domain-level layout:

```text
data/account_vault/<domain>/profile/
data/account_vault/<domain>/storage_state.json.enc
data/account_vault/<domain>/auto_cookies.json.enc
```

Named accounts are isolated:

```text
data/account_vault/<domain>/accounts/<account_id>/profile/
data/account_vault/<domain>/accounts/<account_id>/storage_state.json.enc
data/account_vault/<domain>/accounts/<account_id>/auto_cookies.json.enc
```

`storage_state` and cookie jars remain encrypted by `account_vault` with
`TRAWLER_VAULT_KEY`.

## MCP Workflow

1. Call `register_account_profile(domain, account_id, ...)` to create metadata.
2. Call `open_browser_session(url, account_id=...)` for a visible browser.
3. The human logs in, solves verification, or operates the page.
4. Call `extract_browser_session(...)` or `close_browser_session(...)`.
5. Trawler persists encrypted browser state and marks the profile verified.

For direct single-page retrieval, `retrieve_page` and `crawl_url` also accept
`account_id` when using user-authorized access.

## Status Semantics

- `active`: last login state is expected to work.
- `expired`: session likely expired; prefer visible browser login.
- `needs_login`: no valid state exists yet or the user must refresh login.
- `blocked`: do not use this profile automatically until an operator changes it.

Status affects automatic selection. Only active, unexpired profiles are selected
automatically. Expired or needs-login profiles route callers toward visible
browser recovery. Blocked profiles are not selected as defaults; an explicit
user request can still open a browser for manual recovery.
