# Admin / Review Panel

Server-rendered human review UI for the IranDoc News Generation service.
Same Docker image as the API with a different command (`web` compose service,
published port `PANEL_PORT`, default `8081`). Jinja2 + locally vendored HTMX
(`app/panel/static/htmx.min.js`, no CDN), plain CSS
(`app/panel/static/panel.css`, byte-identical port of the approved design),
Vazirmatn fonts vendored from `demo/fonts/`.
All pages are Persian, `dir="rtl"`. No Node, no build step.

Related: [`llm-prompt.md`](llm-prompt.md) documents exactly what is sent to the
LLM (message order, placeholders, resolution order) and which parts live in
the database versus in code.

## Access

```bash
cp .env.example .env   # set PANEL_ADMIN_PASSWORD + PANEL_SECRET_KEY
docker compose up -d web
```

Open the panel through your HTTPS reverse proxy, which forwards to loopback
`127.0.0.1:8081` (`PANEL_PORT`, default `127.0.0.1:8081`). The first admin
(`PANEL_ADMIN_USERNAME`, default `admin`) is
created at startup from env vars when no users exist; the password is stored
as an argon2 hash, never plaintext. A signed `panel_session` cookie (12h,
HttpOnly, Lax, Secure behind the proxy) keeps the login.

## Security notes (public exposure)

- The deployment binds the panel to loopback only: the ONLY public door is
  the HTTPS reverse proxy in front of it. Plain HTTP on `:8081` is never
  exposed to the internet, and plain HTTP transmits the panel password
  unencrypted — TLS at the proxy is required, not optional. Emergency public
  exposure only with `PANEL_PORT=8081` and only behind a TLS proxy.
- Login is rate-limited per IP with lockout after repeated failures
  (`PANEL_LOGIN_MAX_ATTEMPTS/WINDOW/LOCKOUT`, defaults 10/10min/15min), and
  failed logins are written to the audit trail with the visitor IP. Sessions
  last `PANEL_SESSION_HOURS` (default 12), enforced on the cookie and
  server-side alike. Every other panel route requires login (anonymous
  browsers redirect to `/login`); there are no debug routes and no
  unauthenticated listing endpoints.
- **CSRF.** Every POST form embeds a session-bound token
  (`app/panel/csrf.py`) validated by the `verify_csrf` dependency before the
  endpoint runs; a missing/invalid token or a foreign `Origin` returns a
  Persian 400. The session cookie is `HttpOnly` + `SameSite=Lax` (`Secure`
  behind the TLS proxy). `/login` is exempt from the token (pre-session, the
  Origin check still applies); `/logout` only clears the signed cookie.
- The app trusts `X-Forwarded-For` / `X-Forwarded-Proto` only from the local
  proxy: loopback peers always, plus networks in `PANEL_TRUSTED_PROXIES`
  (set to the compose bridge subnet, since inside Docker the host proxy
  arrives as the bridge gateway, not 127.0.0.1). Direct clients' headers are
  ignored, so the lockout, audit IPs, and Secure-cookie logic always see the
  real visitor. The `Secure` cookie flag is sent when `PANEL_COOKIE_SECURE`
  is on or the trusted proxy reports `https`.

## Roles

The whole policy lives in `app/panel/authz.py`: an `Action` enum, one
`ROLE_ACTIONS` table, and `can()`/`require()`. Routes never re-implement a
role check — they call `_require_role(user, Action.X)` and map the raised
`PermissionDenied` to the 403 page — so "who may do what" has a single
answer. `tests/test_authz_matrix.py` pins the matrix, fails if any POST
route lacks a guard, and exercises it over HTTP.

| Role | Actions |
|---|---|
| `viewer` (بیننده) | `VIEW_RECORDS` only — every page is reachable, every mutation returns 403 |
| `editor` (بازبین) | + `REVIEW_RECORD`, `RETRY_DELIVERY` (retry/release/cancel/delete-from-channel), `SUBMIT_RECORD`, `FIELD_CURATE` |
| `admin` (ادمین) | + `MANAGE_CONFIG` (models/prompts/channels/categories, model test), `MANAGE_USERS`, `VIEW_AUDIT` |

Unknown roles fail closed (no permissions). `/profile/password` is
self-service for any signed-in user (the current password is the real check),
and `/login` is pre-session; both are listed explicitly in the module rather
than silently unguarded.

Every review/retry/config change is written to `audit_logs` (who, action,
record, detail) and shown on the record detail page as history and on the
global `/audit` page.

## Accounts and passwords

- Never share the admin login: each human gets their own account from the
  Users page (`editor` for reviewers, `viewer` for read-only guests).
- **Provider keys and bot tokens.** Set them on the Models page (per model,
  with its own endpoint and optional provider model id) or on the Channels
  page (per channel). They are encrypted with a master key that is **not** in
  the database: `CREDENTIALS_KEY` in the environment, or the generated
  `data/.credentials_key` file (mode 0600, gitignored, shared by all
  services through the `credentials_data` volume). The panel shows only a
  masked hint; there is no way to read a key back. If you ever need to see a
  key, re-enter it — the stored copy is write-only by design.
- **Rotation.** Replacing a key in the panel immediately changes what the next
  generation uses (each job resolves at claim time). After rotation, run
  "تست اتصال" to see the recorded outcome.
- **The row name is a label, not a model id.** `name` identifies the row for
  you; the provider only understands its own id. If the two differ, put the
  real id in **شناسهٔ مدل در ارائه‌دهنده** (`api_model`) — that is what goes in
  the request. Activating a model that has never passed "تست اتصال" is
  refused (409): an id the provider does not know otherwise surfaces much
  later as `404 ... does not exist` on a record, instead of as a fixable
  settings problem.
- Handover flow: admin creates the account, the user logs in and changes
  their password immediately on `/profile`. Deactivate (`toggle`) accounts
  instead of deleting them, so the audit trail keeps its author names.

## Pages and the endpoints/logic they use

| Page | Route | Uses |
|---|---|---|
| Login | `GET /login`, `POST /login` | `panel_users` + argon2 verify; sets signed cookie; audits `login` |
| Dashboard | `GET /` | Same `GROUP BY` counts as `GET /stats` (records/deliveries per status) + `MAX(main.heartbeat_at)` + 10 newest records + `model_service.active_model_health()`. The health card shows the **active model's real connection state** (`Connected` / `Checking` / `Disconnected` / `Not configured`) computed in the service layer, with its last probe time and latency; the state is never guessed at the template and never implies success when the model was never probed. Worker liveness uses `Active` / `Idle`, and when it is not `Active` the card and the header pill both say **when** the last heartbeat was — "Idle" alone does not distinguish a blip from an outage. The header pill itself expands a `<details>` listing each unhealthy component and its short reason (the active model's state plus `model_health.error`, and the worker heartbeat age), so "نیازمند بررسی" is actionable without opening the card below. Shows no timings. |
| Dashboard — live poll | `GET /dashboard/live` | The volatile region only: the four counters, the two worker leases and the model health. The dashboard re-fetches it on `settings.panel_dashboard_refresh_seconds` (default 15s) via HTMX, so an operator watching a generate→approve→publish run sees it move without reloading. The 7-day chart, the event timeline and the channel table are deliberately excluded — they do not change over that window and re-rendering them would only add flicker. Polling (not a websocket) because the figures are cheap counts and the service otherwise holds no long-lived connection. Requires a session. |
| Records & review | `GET /records[?status&field&university&q&order&page&approved&tab]`, `GET /records/table` (HTMX), `POST /records/bulk-approve` | **One page, two tabs** (the owner asked for records and deliveries merged back). `?tab=records` (the default) lists records with its own filters and RECORD KPIs — awaiting review, queued, published, generation failed — because that tab answers "what needs my decision". `?tab=deliveries` lists deliveries with its own filters and DELIVERY KPIs. The tab is a query string, not JS, so both halves are bookmarkable; filter state is deliberately **not** carried across the tab bar, because `status` means a record state on one tab and a delivery state on the other. Multi-select of reviewable rows survives the table's 15s auto-refresh (selection held in a JS Set, re-applied after each htmx swap) and its script renders on the records tab only. Admin/reviewer. |
| Record review | `GET /records/{id}`, `POST /records/{id}/review`, `POST /records/bulk-approve` | Editable `title/lead/body` (`output_json`); `approve` moves the record `review -> approved` (status + `approved_at`, first wins) and flips `awaiting_confirmation -> pending` via `confirm_deliveries_for_article`; `reject` -> `rejected`; `save_draft` edits only. Review actions are only legal from `review` (else 409). The review list defaults to undecided rows (status filter, `approved=no`), so an approved record leaves it immediately; detail shows stored vs current category, approver/when, and for a sent article the `published` status. Bulk approves eligible rows, skips the rest. Audited. |
| Record regenerate | `POST /records/{id}/regenerate` | `record_service.regenerate` — same resolution + generation path as the Worker; success returns the record to `review`, provider errors park it `failed` with the error text. Audited either way. |
| Record category fix | `POST /records/{id}/category` | `record_service.reassign_category` — recovery for a record classified into the wrong category (the sender routes by `main.category_id`, so until this is fixed the article publishes to the wrong channel). Picks real category names, affects only that record, audited; unknown category is 422. |
| Record flow timeline | part of `GET /records/{id}` | `record_service.record_timeline` builds submitted → field/category (with the matched keyword) → generated with `<model>/<prompt>` → reviewed by `<user>` → delivered to `<channel>` with the message id, straight from columns and audit rows. Failures and `deleted_from_channel` appear as such, and it renders for every state. |
| Delivery actions | `GET /deliveries/table` (HTMX), `POST /deliveries/{id}/retry`, `POST /deliveries/{id}/release`, `POST /deliveries/{id}/cancel`, `POST /deliveries/{id}/delete-from-channel`, `POST /deliveries/{id}/mark-deleted` | Filters `status/platform/main_id` with per-row route provenance. Recovery action per state: retry only `failed` (else 409, resets `attempts=0`); release only `awaiting_confirmation` (else 409, attempts kept); cancel any not-yet-sent row (`awaiting_confirmation`/`pending`, else 409 — an in-flight or sent message cannot be cancelled) which parks it `failed` with an operator note so it stays visible and retryable; delete-from-channel only for `sent` (else 409) and calls the platform's `deleteMessage` **first** — the row moves to `deleted_from_channel` keeping the message id only after the platform confirms, and a refusal (409) or a transport failure (502) leaves the row `sent` because the message is still in the channel. `mark-deleted` is the companion for a message already removed by hand: it records the state without calling the platform. Gate counts explain why nothing was sent yet. All audited. |
| Records — bulk delete | `POST /records/{id}/delete-everywhere` | An article fans out to one delivery row per routed channel, so removing "the news" is a bulk action: **حذف پیام از همهٔ کانال‌ها** on the record's delivery tab calls `deleteMessage` on **every** channel the article reached. Channels are independent — a real refusal on one does not stop the others; the successes are recorded `deleted_from_channel` and the refusal stays `sent` with the platform's reason in the audit log, so it can be retried alone. A platform "message not found" is treated as **done** (the message is not published, which is the goal) and counted separately as `already_gone`, so a message removed by hand or in an earlier run can still be cleared. Requires an explicit confirmation; the button only appears while something is still published. The page then says whether the delete was complete or partial. |
| Deliveries & hand-off | `GET /records?tab=deliveries[&status&platform&main_id]`, `GET /deliveries/table` (HTMX), `POST /deliveries/{id}/retry`, `/release`, `/cancel`, `/delete-from-channel`, `/mark-deleted` | **The deliveries TAB of `/records`** (`/deliveries` 301s there so old bookmarks survive). The KPIs count DELIVERIES — delivered, queued, awaiting your approval, failed — because this tab answers "did it reach the channel". Filters are delivery axes (platform, outcome), not record axes. Carries the **Retry Policy** and human-gate cards. Each row's buttons depend on its status: retry (failed), release (awaiting), cancel (queued), delete-from-channel (sent). `RETRY_DELIVERY` for every action. |
| Submit form | `GET /submit`, `POST /submit` | Creates `main(status='ready')` with validated optional `model_id`/`prompt_id` (same rules as `POST /submit` API). Audited. |
| Models | `GET /models`, `POST /models`, `POST /models/{id}`, `POST /models/{id}/activate`, `POST /models/{id}/deactivate`, `POST /models/{id}/delete`, `POST /models/{id}/test`, `POST /models/{id}/clear-key` | Create/edit (api_model, base_url, temperature, max_output_tokens, description, key), test connection, activate, deactivate, delete. **Key handling (PART D):** the field is a masked write-only input — an empty submit keeps the stored key and "پاک کردن کلید" removes it; the value is encrypted before storage and never rendered, returned or logged. A row with its own `base_url` requires its own key and fails loudly rather than sending the global key. **Guards:** the active model cannot be deleted (deactivate first), a model in use by records cannot be deleted, and neither deactivate nor delete may leave zero usable models. The status column reads the recorded probe (`last_tested_at`/`last_test_ok`/`last_test_error`/latency): آزمون نشده / ✓ موفق / ! ناموفق + time + reason. Admin only. |
| Prompts | `GET /prompts`, `POST /prompts`, `POST /prompts/{id}/activate` | Immutable versions: new version = new row + activate previous off; never edited in place. Admin only for mutations. |
| Channels | `GET /channels[?channel_id&new]`, `POST /channels`, `POST /channels/{id}`, `POST /channels/{id}/toggle`, `POST /channels/{id}/delete` | **Destinations.** A channel is a *destination*, not a rule: which categories name it is decided on `/categories`. Switching it OFF therefore removes it from every category at once. Adding always inserts beside the existing ones; `?channel_id=N` picks the one a settings card edits; each row has its own switch; delete removes an unused channel and **disables** one that has deliveries so its history keeps resolving. A row shows a red **ارسال ناموفق** badge with the provider's reason when it is ON but rejecting messages. A per-channel bot token is stored Fernet-encrypted and never rendered back. A channel only receives news generated from the moment it was switched ON, and it shows its **start line** so a re-enabled channel is not flooded with the archive. Admin only. |
| Fields | `GET /fields[?q&category_id&source&unconfirmed&page&page_size&assigned&skipped]`, `POST /fields`, `POST /fields/bulk-category`, `POST /fields/{id}/category`, `POST /fields/{id}/confirm`, `POST /fields/{id}/delete` | **Independent page, and where fields are curated.** One row per field with its category editable in place. **Searched, filtered and paginated in SQL** at 25/50/100/200 per page (it once called `list_fields()`, whose default `limit=100` left 449 of 549 fields unrendered *and* unsearchable). Rows and count come from the SAME predicate and every filter rides into the pager. **Bulk assignment**: tick rows, pick one category, apply — the only tractable way to move hundreds of fields out of the catch-all. Fields already in the target are **skipped, not rewritten**, so the «دستی» badge keeps meaning "a person chose this". The outcome is reported after redirect (`?assigned=&skipped=`), because a batch that silently did nothing looks identical to one that worked. **Create a field** by name and category: a discipline missing from the spreadsheet could otherwise only reach `other`, which under strict routing publishes nowhere; it is stored `source='manual'`, `category_source='manual'` so the next classification pass cannot move it. The name is normalized the way the classifier normalizes, so a near-duplicate is refused in Persian rather than blowing up on the unique index. **Delete a field** is **refused while any record points at it** (`field-in-use`): `main.field_id` is nullable, so detaching would be easy, but it would silently orphan real history from the discipline it was filed under. `FIELD_CURATE` for all of these. |
| Categories & keywords | `GET /categories[?q=]`, `POST /categories`, `POST /categories/{id}/delete`, `POST /categories/{id}/channels` | **Independent page, and where routing is configured.** A **create-category** form exists because buckets previously could only be deleted or reused, which made a missing bucket unfixable under strict routing; it is idempotent by name ("you already have that" beats a uniqueness error on a one-line text box). A card per category: its field names as chips, the field count, and its description -- plus how many records are filed under it, so a delete that will be refused says why before the click. Server-side name search. Each card carries a `<details>` channel picker: **the checkbox list is the complete truth** about where that bucket's news goes, so the POST REPLACES the route set (`channel_service.set_category_channels`, delete-then-insert) rather than appending. Empty selection means the category publishes nothing. Admin only (`MANAGE_CONFIG`), audited as `category_channels`. **Routing is LIVE** (see Dispatch): a category with no channels sends no news, so the page flags every unrouted category that still holds fields and says how many -- otherwise "no news arrived" would be the only symptom. The warning is scoped to the categories on screen, so it never defeats the search filter. `/channels` shows the mirror: read-only chips of which categories name the selected channel. **Delete a category** is refused while it still holds fields (`category-has-fields`) or records `category-in-use`): `fields.category_id` is NOT NULL and `main.category_id` is a plain FK, so a category holding either cannot go without deciding where those rows end up — and sweeping its fields into a fallback bucket automatically would move news to channels the operator never chose. Each card states which of the two is in the way, a refused delete leaves the category and its fields intact, and its channel routes go with it (`ON DELETE CASCADE`). The path out is in the message: filter `/fields` by the category, bulk-assign, come back. Admin only, audited as `category_delete`. |
| Users | `GET /users[?user_id=N]`, `POST /users`, `POST /users/{id}/role`, `POST /users/{id}/toggle`, `POST /users/{id}/password`, `POST /users/{id}/delete` | Console accounts (argon2 hashes, never plaintext). **One settings block for every user**: `?user_id=N` selects the account the block edits, so the page never grows a pair of sections per account. **Delete means gone from the panel**: it tombstones the row (`panel_users.deleted_at`), which every listing and the login path filter out, while `audit_logs.user_id` keeps resolving the author's name (that FK is `ON DELETE SET NULL`, so erasing the row would lose it). Safeguards unchanged: no self-delete, no deleting the last active admin; deactivation remains available. Admin only. Own password via `/profile`. |
| Audit trail | `GET /audit` | Immutable `audit_logs` table with action/user/record filters and pagination. Admin only. |

Machine API (`POST /submit`, `/health`, `/records/{id}`, `/records`,
`/deliveries`, `/stats`, `/models`, `/prompts`) keeps the shared-secret
dependency unchanged; the panel uses its own cookie auth and never accepts the
shared secret.

## Visual design

The panel ports the approved design reference (`panel.css` plus one reference
page per screen) with the same structure, class names, and component hierarchy
(sidebar, topbar, KPIs, cards, tables, badges, filters, tabs, timeline, matrix,
chart, switch, notes, login box). `app/panel/static/panel.css` is a byte-identical
copy (only served from `/static/`); fonts are the repo's own Vazirmatn files.
Mock numbers/texts from the reference are replaced with real data; pages show
Persian labels, `dir="rtl"`, and HTMX powers the filter/table partials (the
script tag is invisible and changes no pixels).

## Worker heartbeat

Freshness is `MAX(main.heartbeat_at)` — the last time the generation Worker
touched a job. The demo page must not show processing times or record counts;
this panel is the only place counts appear.

## Wiring: one system, not a parallel app

1. **One database, one access path.** The panel reaches PostgreSQL only
   through `app/services/*`: no module under `app/panel/` may import
   `app/db/connection.py` or an ORM model, and every panel route must call a
   service. `tests/test_panel_layering.py` enforces both by parsing the
   modules (AST, so local imports count).
2. **Shared domain logic.** Intake lives in `records.create_thesis_record`,
   used by both `POST /submit` and the panel form. Generation resolution and
   regeneration live in `records.regenerate` +
   `app/news/generation.resolve_generation_config`, used by the Worker and the
   panel; the probe (`models.test_model_connection`) is shared by the API
   test-connection and the panel test button. Message preview uses
   `render_article`, the same code the sender runs, and the machine API's
   `/routing/preview` uses `dispatcher.preview_routing` — the panel's routing
   preview PAGE was removed (item 5) and `/routing` now redirects to
   `/channels`, but the auditable API endpoint stays. Password hashing lives
   in `services/passwords.py`, called by the users service. Nothing is copied
   between worker and panel.
3. **Same state machine.** `app/services/transitions.py` defines record
   (`ready|processing|review|approved|published|failed|rejected`) and delivery
   (`awaiting_confirmation|pending|sending|sent|failed|deleted_from_channel`)
   transitions; the panel, the API and both Workers all go through it.
4. **Panel submit = machine submit.** Both insert `main(status='ready')`; the
   Worker claims it on its next poll with no glue.
5. **One writer per concern.** The panel never calls Bale/Telegram and never
   sends inline: approve flips gated rows to `pending`, retry re-queues
   `failed` rows; the sender Worker performs every send.
6. **Live data only.** Every page renders real rows; empty states (no models,
   no channels, no output yet) replace mock content.
7. **HTMX polling.** The records/deliveries tables re-poll their own partials
   (`hx-trigger="load, every 15s"`) — no websockets, no threads, and the
   machine shared secret never reaches the browser.
8. **Same config.** The panel reads `app/config.py` and the same `.env`,
   adding only `PANEL_SECRET_KEY` (+ panel login credentials).
9. **CSRF.** Every POST form carries a session-bound token validated by the
   `verify_csrf` dependency; the session cookie is `SameSite=Lax` and origin
   is checked. Covered by `tests/test_panel_csrf.py`.
9. **End-to-end proof.** `tests/test_panel_e2e.py` drives the whole chain
   (panel submit → worker → gated delivery → panel approve → sender → sent,
   LLM + platform stubbed). Live proof on 2026-10-08: panel record 41 went
   `ready → review` (real LLM output), approval released the delivery
   `awaiting_confirmation → pending`, and the sender delivered it
   (`message_id=174486`, audit `submit,approve`).
10. **Machine API untouched.** `POST /submit`, `/health`, `GET /records/{id}`
    keep their exact contracts (covered by the existing suite).
