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
- The app trusts `X-Forwarded-For` / `X-Forwarded-Proto` only from the local
  proxy: loopback peers always, plus networks in `PANEL_TRUSTED_PROXIES`
  (set to the compose bridge subnet, since inside Docker the host proxy
  arrives as the bridge gateway, not 127.0.0.1). Direct clients' headers are
  ignored, so the lockout, audit IPs, and Secure-cookie logic always see the
  real visitor. The `Secure` cookie flag is sent when `PANEL_COOKIE_SECURE`
  is on or the trusted proxy reports `https`.

## Roles

| Role | Capabilities |
|---|---|
| `viewer` (بیننده) | Read-only: views every page, every mutation returns 403 |
| `editor` (بازبین) | Review: records list/detail (approve/reject/save_draft), delivery retry, submit form |
| `admin` (ادمین) | Everything above plus models/prompts/channels/users management and the audit trail |

Every review/retry/config change is written to `audit_logs` (who, action,
record, detail) and shown on the record detail page as history and on the
global `/audit` page.

## Pages and the endpoints/logic they use

| Page | Route | Uses |
|---|---|---|
| Login | `GET /login`, `POST /login` | `panel_users` + argon2 verify; sets signed cookie; audits `login` |
| Dashboard | `GET /` | Same `GROUP BY` counts as `GET /stats` (records/deliveries per status) + `MAX(main.heartbeat_at)` + 10 newest records. Shows no timings. |
| Records list | `GET /records`, `GET /records/table` (HTMX) | SQL `LIMIT/OFFSET + COUNT` with `status/field/university/q/order` filters (same semantics as `GET /records` API) |
| Record review | `GET /records/{id}`, `POST /records/{id}/review` | Editable `title/lead/body` (`output_json`); `approve` flips `awaiting_confirmation→pending` via `confirm_deliveries_for_article`; `reject` → `rejected`; `save_draft` edits only. Only from `review` (else 409 page). Audited. |
| Deliveries | `GET /deliveries`, `GET /deliveries/table` (HTMX), `POST /deliveries/{id}/retry` | Filters `status/platform/main_id`; retry only `failed` (else 409), resets `attempts=0`. Audited. |
| Submit form | `GET /submit`, `POST /submit` | Creates `main(status='ready')` with validated optional `model_id`/`prompt_id` (same rules as `POST /submit` API). Audited. |
| Models | `GET /models`, `POST /models`, `POST /models/{id}/activate`, `POST /models/{id}/test` | List/create/detail; single-active activate (atomic); 1-token real connection probe (key never returned/logged). Admin only for mutations. |
| Prompts | `GET /prompts`, `POST /prompts`, `POST /prompts/{id}/activate` | Immutable versions: new version = new row + activate previous off; never edited in place. Admin only for mutations. |
| Channels + routing | `GET /channels`, `POST /channels`, `POST /channels/route`, `POST /channels/unroute` | Destinations (tokens stay in env, never on rows); field→channel routes as row inserts. Admin only for mutations. |
| Fields | `GET /fields` | Read-only table with category, source, routed channels, plus top-field bars from real record counts. |
| Categories | `GET /categories` | Cards per category with real field names as chips and field counts; server-side name search. |
| Users | `GET /users`, `POST /users`, `POST /users/{id}/role`, `POST /users/{id}/toggle` | Console accounts (argon2 hashes, never plaintext); role changes and activate/deactivate; access matrix. Admin only. |
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

1. **One database.** The panel uses the same PostgreSQL instance and tables
   through `app/db/connection.py` (`async_session`) and `app/db/models.py`.
   No second database, schema copy, model set, or cache exists.
2. **Shared domain logic.** Intake lives in `app/submissions.py`
   (`create_thesis_record`), used by both `POST /submit` and the panel form.
   Generation resolution lives in `app/news/generation.py`
   (`resolve_generation_config`), used by both the Worker and the panel
   regenerate action; the probe (`probe_model_connection`) is shared by the
   API test-connection and the panel test button. Route resolution and message
   preview use the sender's own `channels_for_article` / `render_article`.
   Nothing is copied between worker and panel.
3. **Same state machine.** `main`: `ready|processing|review|failed` plus
   `rejected` (added *with* migration `006`, human reject only); `deliveries`:
   `pending|sending|sent|failed|awaiting_confirmation`. No other values exist.
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
9. **End-to-end proof.** `tests/test_panel_e2e.py` drives the whole chain
   (panel submit → worker → gated delivery → panel approve → sender → sent,
   LLM + platform stubbed). Live proof on 2026-10-08: panel record 41 went
   `ready → review` (real LLM output), approval released the delivery
   `awaiting_confirmation → pending`, and the sender delivered it
   (`message_id=174486`, audit `submit,approve`).
10. **Machine API untouched.** `POST /submit`, `/health`, `GET /records/{id}`
    keep their exact contracts (covered by the existing suite).
