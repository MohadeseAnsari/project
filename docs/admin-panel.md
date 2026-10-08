# Admin / Review Panel

Server-rendered human review UI for the IranDoc News Generation service.
Same Docker image as the API with a different command (`web` compose service,
loopback-only `127.0.0.1:8081`). Jinja2 + locally vendored HTMX
(`app/panel/static/htmx.min.js`, no CDN), plain CSS
(`app/panel/static/style.css`), Vazirmatn fonts vendored from `demo/fonts/`.
All pages are Persian, `dir="rtl"`. No Node, no build step.

## Access

```bash
cp .env.example .env   # set PANEL_ADMIN_PASSWORD + PANEL_SECRET_KEY
docker compose up -d web
```

Open **http://127.0.0.1:8081/login**. The first admin
(`PANEL_ADMIN_USERNAME`, default `admin`) is created at startup from env vars
when no users exist; the password is stored as an argon2 hash, never plaintext.
A signed `panel_session` cookie (12h, HttpOnly, Lax) keeps the login.

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
