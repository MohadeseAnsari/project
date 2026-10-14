# Admin / Review Panel

Server-rendered human review UI for the IranDoc News Generation service.
Same Docker image as the API with a different command (`web` compose service,
published port `PANEL_PORT`, default `8081`). Jinja2 + locally vendored HTMX
(`app/panel/static/htmx.min.js`, no CDN), plain CSS
(`app/panel/static/panel.css`, byte-identical port of the approved design),
Vazirmatn fonts vendored from `demo/fonts/`.
All pages are Persian, `dir="rtl"`. No Node, no build step.

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
| `admin` (ادمین) | + `MANAGE_CONFIG` (models/prompts/channels/routing/categories, model test), `MANAGE_USERS`, `VIEW_AUDIT` |

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
| Dashboard | `GET /` | Same `GROUP BY` counts as `GET /stats` (records/deliveries per status) + `MAX(main.heartbeat_at)` + 10 newest records. Shows no timings. |
| Records list | `GET /records`, `GET /records/table` (HTMX) | SQL `LIMIT/OFFSET + COUNT` with `status/field/university/q/order` filters (same semantics as `GET /records` API) |
| Record review | `GET /records/{id}`, `POST /records/{id}/review`, `POST /records/bulk-approve` | Editable `title/lead/body` (`output_json`); `approve` moves the record `review -> approved` (status + `approved_at`, first wins) and flips `awaiting_confirmation -> pending` via `confirm_deliveries_for_article`; `reject` -> `rejected`; `save_draft` edits only. Review actions are only legal from `review` (else 409). The review list defaults to undecided rows (status filter, `approved=no`), so an approved record leaves it immediately; detail shows stored vs current category, approver/when, and for a sent article the `published` status. Bulk approves eligible rows, skips the rest. Audited. |
| Record regenerate | `POST /records/{id}/regenerate` | `record_service.regenerate` — same resolution + generation path as the Worker; success returns the record to `review`, provider errors park it `failed` with the error text. Audited either way. |
| Record category fix | `POST /records/{id}/category` | `record_service.reassign_category` — recovery for a record classified into the wrong category (the sender routes by `main.category_id`, so until this is fixed the article publishes to the wrong channel). Picks real category names, affects only that record, audited; unknown category is 422. |
| Record flow timeline | part of `GET /records/{id}` | `record_service.record_timeline` builds submitted → field/category (with the matched keyword) → generated with `<model>/<prompt>` → reviewed by `<user>` → delivered to `<channel>` with the message id, straight from columns and audit rows. Failures and `deleted_from_channel` appear as such, and it renders for every state. |
| Deliveries | `GET /deliveries`, `GET /deliveries/table` (HTMX), `POST /deliveries/{id}/retry`, `POST /deliveries/{id}/release`, `POST /deliveries/{id}/cancel`, `POST /deliveries/{id}/delete-from-channel`, `POST /deliveries/{id}/mark-deleted` | Filters `status/platform/main_id` with per-row route provenance. Recovery action per state: retry only `failed` (else 409, resets `attempts=0`); release only `awaiting_confirmation` (else 409, attempts kept); cancel any not-yet-sent row (`awaiting_confirmation`/`pending`, else 409 — an in-flight or sent message cannot be cancelled) which parks it `failed` with an operator note so it stays visible and retryable; delete-from-channel only for `sent` (else 409) and calls the platform's `deleteMessage` **first** — the row moves to `deleted_from_channel` keeping the message id only after the platform confirms, and a refusal (409) or a transport failure (502) leaves the row `sent` because the message is still in the channel. `mark-deleted` is the companion for a message already removed by hand: it records the state without calling the platform. Gate counts explain why nothing was sent yet. All audited. |
| Submit form | `GET /submit`, `POST /submit` | Creates `main(status='ready')` with validated optional `model_id`/`prompt_id` (same rules as `POST /submit` API). Audited. |
| Models | `GET /models`, `POST /models`, `POST /models/{id}`, `POST /models/{id}/activate`, `POST /models/{id}/deactivate`, `POST /models/{id}/delete`, `POST /models/{id}/test`, `POST /models/{id}/clear-key` | Create/edit (api_model, base_url, temperature, max_output_tokens, description, key), test connection, activate, deactivate, delete. **Key handling (PART D):** the field is a masked write-only input — an empty submit keeps the stored key and "پاک کردن کلید" removes it; the value is encrypted before storage and never rendered, returned or logged. A row with its own `base_url` requires its own key and fails loudly rather than sending the global key. **Guards:** the active model cannot be deleted (deactivate first), a model in use by records cannot be deleted, and neither deactivate nor delete may leave zero usable models. The status column reads the recorded probe (`last_tested_at`/`last_test_ok`/`last_test_error`/latency): آزمون نشده / ✓ موفق / ! ناموفق + time + reason. Admin only. |
| Prompts | `GET /prompts`, `POST /prompts`, `POST /prompts/{id}/activate` | Immutable versions: new version = new row + activate previous off; never edited in place. Admin only for mutations. |
| Channels + routing | `GET /channels`, `POST /channels`, `POST /channels/{id}`, `POST /channels/{id}/toggle`, `POST /channels/{id}/delete`, `POST /channels/route`, `POST /channels/unroute`, `POST /channels/category-route`, `POST /channels/category-unroute` | **Channels are a list, not a single row**: `POST /channels` always inserts a second destination beside the existing ones and refuses only an identical `(platform, chat_id)`. `GET /channels?channel_id=N` picks which one the settings card edits, so saving changes that channel alone. Each card has its own on/off switch (`toggle`) and delete action (`delete`, requires `confirm=yes`). Delete removes the row and its field/category routes — but a channel that already has deliveries is **disabled instead of deleted**, so published history keeps resolving. Creating accepts a numeric id or @username (validated live via getChat) and an optional **per-channel bot token**, stored Fernet-encrypted, write-only, and used to validate the target and to send/delete on that channel; a blank token on edit keeps the stored one, and `clear_token=yes` drops it explicitly. Category→channel mapping is primary (name pickers), field routes stay as named exceptions. Admin only for mutations. |
| Fields | `GET /fields`, `POST /fields/{id}/confirm` | Read-only table with category, matched keyword (the WHY), human-confirm badge, source, routed channels, plus top-field bars from real record counts. Confirm freezes the category (`curated`, skipped by reclassification forever). Re-classify spreadsheet rows with `scripts/reclassify_fields.py` (dry-run diff first, `--apply` to persist; `main` rows keep their snapshots). |
| Categories | `GET /categories` | Cards per category with real field names as chips and field counts; server-side name search. |
| Users | `GET /users`, `POST /users`, `POST /users/{id}/role`, `POST /users/{id}/toggle`, `POST /users/{id}/password` | Console accounts (argon2 hashes, never plaintext); role changes, activate/deactivate, and password resets; access matrix. Admin only. Own password via `/profile`. |
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
   test-connection and the panel test button. Route resolution and message
   preview use `routing_service.preview` / `render_article`, the same code the
   sender runs. Password hashing lives in `services/passwords.py`, called by
   the users service. Nothing is copied between worker and panel.
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
