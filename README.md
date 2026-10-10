# IranDoc News Generation

![Python 3.12](https://img.shields.io/badge/python-3.12-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.141-009688)
![PostgreSQL 16](https://img.shields.io/badge/PostgreSQL-16-336791)
![Docker](https://img.shields.io/badge/docker-compose-ready-2496ED)
![License](https://img.shields.io/badge/license-internal-lightgrey)

Automatic journalistic news generation from IranDoc thesis metadata, with a
human review panel in front of publishing. A thesis is submitted as JSON,
stored in PostgreSQL as a job, processed by a persistent Worker that generates
a Persian news article (title, lead, body) with an LLM — then a reviewer
approves it in a server-rendered panel before a separate sender Worker
publishes it to Bale/Telegram channels.

```text
Thesis JSON ──POST /submit──▶ FastAPI ──INSERT──▶ PostgreSQL (status='ready')
                                                      │  FOR UPDATE SKIP LOCKED, FIFO
Generation Worker ◀──claim────────────────────────────┘
  │  normalize + rule-based classify (deterministic, never the LLM)
  │  PydanticAI Agent ──▶ AvalAI ──▶ {title, lead, body}
  ▼
PostgreSQL (status='review', output_json) ──▶ sender queues awaiting_confirmation
                                                      │  human gate (default ON)
Review panel ◀── reviewer edits + approves ───────────┘──▶ status='pending'
                                                      │
Sender Worker ◀──claim── render ──▶ Bale / Telegram ──▶ status='sent'
```

- **PostgreSQL is both storage and queue.** No Redis, Celery, Kafka,
  Kubernetes, or microservices — one modular monolith, four runtime
  components (`postgres`, `api`, `worker`, `sender`) plus the review panel
  (`web`, same image, different command).
- **The LLM is used ONLY for news generation.** Field/category classification
  is deterministic and rule-based; the LLM never classifies.
- **Nothing publishes without a human.** Deliveries queue as
  `awaiting_confirmation` and flip to `pending` only on approval
  (`REQUIRE_HUMAN_CONFIRMATION`, default `true`).
- **One system, not two apps.** The panel shares the same database, ORM
  models, settings, and domain helpers as the workers
  (see `docs/admin-panel.md` → *Wiring*).

## Contents

- [Features](#features)
- [Project structure](#project-structure)
- [Requirements](#requirements)
- [Configuration](#configuration)
- [Quickstart](#quickstart)
- [Machine API reference](#machine-api-reference)
- [Review panel](#review-panel)
- [Database & migrations](#database--migrations)
- [Workers](#workers)
- [Security](#security)
- [Demo](#demo)
- [Testing](#testing)
- [Troubleshooting](#troubleshooting)
- [Docs](#docs)
- [License](#license)

## Features

**Machine API** (shared-secret auth, interactive Swagger at `/docs`):

- `POST /submit` — validate thesis JSON, store a `ready` job (optional
  per-record `model_id`/`prompt_id` overrides); auth is checked *before*
  validation, so rejected requests never create rows.
- `GET /records` — paginated, filterable listing (`page`/`page_size≤100`,
  `status`/`field`/`university`/`q`/`order`) with `LIMIT/OFFSET + COUNT`.
- `GET /records/{id}` — status, generated output, thesis metadata, audit FKs,
  timestamps, and per-channel delivery state.
- `PATCH /records/{id}` — human decisions (`approve`/`reject`/`save_draft`,
  only from `review`, else `409`), with optional title/lead/body edits.
- `GET /deliveries` + `POST /deliveries/{id}/retry` — paginated delivery
  listing and manual re-queue of `failed` rows (attempts reset).
- `GET /stats` — record/delivery counts per status plus Worker heartbeat
  freshness (no per-record timings).
- `/models` + `/prompts` — list/create/detail, atomic single-active
  `POST /{id}/activate`, immutable prompt versions (new version = new row),
  and a real 1-token `POST /models/{id}/test-connection` that never
  returns or logs a key.

**Review panel** (Persian RTL UI, Jinja2 + locally vendored HTMX, no build
step, no CDN — see [Review panel](#review-panel)):

- Login with per-IP lockout, argon2 passwords, signed cookies, full audit.
- Dashboard (KPIs, 7-day chart, health, timeline, channels), records list
  with live HTMX tables, tabbed review page (edit, raw JSON, metadata,
  history, delivery preview of exactly what approval will send), deliveries
  with retry, submit form, models/prompts/channels/fields/categories
  management, users + roles, and a global audit trail.
- Three roles: **viewer** (read-only), **editor** (review), **admin** (full).

## Project structure

```text
project/
├── app/
│   ├── main.py               # FastAPI app factory, CORS, OpenAPI body for /submit
│   ├── config.py             # Typed settings from env/.env (the ONLY config source)
│   ├── submissions.py        # Shared thesis intake used by API and panel alike
│   ├── db/
│   │   ├── connection.py     # Shared async engine + session factory
│   │   └── models.py         # ORM models mirroring the migrated schema
│   ├── schemas/news.py       # Pydantic contracts for every API shape
│   ├── api/
│   │   ├── routes.py         # All machine endpoints (never calls the LLM)
│   │   └── limits.py         # Rate limiter (10/min per client on /submit)
│   ├── auth/secret.py        # Constant-time shared-secret check (machines only)
│   ├── fields/
│   │   ├── normalization.py  # NFC, zero-width removal, whitespace collapse
│   │   └── classification.py # Priority-ordered keyword rules → category
│   ├── news/
│   │   ├── generation.py     # PydanticAI Agent + shared resolve/probe helpers
│   │   └── prompts.py        # Packaged fallback system prompt (injection-safe)
│   ├── worker/worker.py      # Persistent sequential generation Worker + recovery
│   ├── messaging/
│   │   ├── formatting.py     # Article → channel message text
│   │   ├── platforms.py      # Bale + Telegram HTTP clients
│   │   ├── dispatcher.py     # Delivery queue: enqueue / claim / retry policy
│   │   └── worker.py         # Persistent sender Worker + recovery
│   └── panel/                # Server-rendered review UI (same image, `web` service)
│       ├── app.py            # Panel routes (cookie auth, never the shared secret)
│       ├── auth.py           # argon2, signed cookies, login lockout, proxy IP
│       ├── templates/        # Jinja2 pages ported from the approved design
│       └── static/           # panel.css (byte-identical design), HTMX, Vazirmatn
├── migrations/               # SQL migrations 001–015 (authoritative schema)
├── scripts/
│   ├── apply_migrations.py   # Ordered, once-only runner (`_migrations` table)
│   ├── seed_fields.py        # Loads data/fields.xlsx (idempotent)
│   └── manage_channels.py    # Channels + field → channel routes CLI
├── tests/                    # pytest suite, PostgreSQL-backed (no SQLite)
├── data/fields.xlsx          # 533 source field names
├── docs/                     # architecture, codebase-guide, erd, demo, admin-panel
├── demo/                     # Static machine-API demo UI (:8080) + fonts
├── Dockerfile                # python:3.12-slim, non-root user
├── docker-compose.yml        # postgres, migrate, api, demo, worker, sender, web
├── requirements.txt
└── .env.example              # Placeholders only — no real secrets
```

## Requirements

- Python 3.12
- PostgreSQL 16
- Docker + Docker Compose (for the containerized stack)

## Configuration

Copy `.env.example` to `.env` and fill in real values. `.env` is git-ignored;
nothing secret is ever baked into images or committed.

| Variable | Description | Required |
|----------|-------------|----------|
| `DATABASE_URL` | PostgreSQL connection URL | Yes |
| `SHARED_SECRET` | Machine-API authentication secret | Yes |
| `AVALAI_API_KEY` | AvalAI API key | Yes |
| `AVALAI_BASE_URL` | AvalAI base URL | Yes |
| `LLM_MODEL` | Model runtime identifier (`gpt-5.6-luna`) | Yes |
| `LLM_TIMEOUT_SECONDS` | LLM call timeout | No (default: 120) |
| `WORKER_POLL_INTERVAL_SECONDS` | Generation queue poll interval | No (default: 1) |
| `WORKER_HEARTBEAT_INTERVAL_SECONDS` | Heartbeat interval (must stay ≪ lease) | No (default: 30) |
| `WORKER_LEASE_MINUTES` | Stale-job recovery timeout | No (default: 5) |
| `BALE_BOT_TOKEN` | Bale bot token used by the `sender` service | Yes for sending |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token (until a Telegram channel is routed) | No |
| `SENDER_SEND_DELAY_SECONDS` | Delay between two consecutive channel sends | No (default: 5) |
| `SENDER_POLL_INTERVAL_SECONDS` | Sender idle poll interval | No (default: 2) |
| `SENDER_MAX_ATTEMPTS` | Send attempts before parking as `failed` | No (default: 5) |
| `SENDER_BACKOFF_BASE_SECONDS` | Base for exponential send backoff | No (default: 10) |
| `SENDER_REQUEST_TIMEOUT_SECONDS` | Per-send HTTP timeout | No (default: 20) |
| `SENDER_LEASE_MINUTES` | Abandoned-send recovery timeout | No (default: 5) |
| `REQUIRE_HUMAN_CONFIRMATION` | Gate deliveries as `awaiting_confirmation` until approval | No (default: true) |
| `PANEL_ADMIN_USERNAME` | Review panel first admin username | No (default: admin) |
| `PANEL_ADMIN_PASSWORD` | Review panel first admin password (env only, argon2-hashed) | Yes for panel login |
| `PANEL_SECRET_KEY` | Review panel cookie signing key | Yes for panel |
| `PANEL_PORT` | Published panel port (loopback-only by default) | No (default: 127.0.0.1:8081) |
| `PANEL_COOKIE_SECURE` | Send session cookie with `Secure` (enable behind TLS proxy) | No (default: false) |
| `PANEL_LOGIN_MAX_ATTEMPTS` / `PANEL_LOGIN_WINDOW_MINUTES` / `PANEL_LOGIN_LOCKOUT_MINUTES` | Per-IP login failures in window before lockout | No (defaults: 10/10/15) |
| `PANEL_SESSION_HOURS` | Session lifetime in hours (cookie + server check agree) | No (default: 12) |
| `PANEL_TRUSTED_PROXIES` | Extra trusted proxy peers/networks for `X-Forwarded-*` | No (default: loopback only) |
| `POSTGRES_PASSWORD` | PostgreSQL admin password (no default — compose refuses without it) | Yes |
| `POSTGRES_DB` | PostgreSQL database name | No (default: irandoc_news) |
| `API_HOST` / `API_PORT` | API listen address inside its container | No (defaults: 0.0.0.0/8000) |
| `DEMO_CORS_ORIGINS` | Browser origins allowed to call the API cross-origin | No (default: local :8080) |

## Quickstart

```bash
cp .env.example .env          # edit with real credentials
docker compose up -d
docker compose ps
curl.exe http://localhost:8000/health
# → {"status":"ok","database":"connected"}
```

First run only — seed the 533 fields (idempotent):

```bash
docker compose exec api python scripts/seed_fields.py
```

Submit a thesis (all nine fields required; `model_id`/`prompt_id` optional):

```bash
curl.exe -X POST http://localhost:8000/submit -H "Content-Type: application/json" -d "{
  \"shared_Secret\": \"REPLACE_WITH_REAL_SECRET\",
  \"username\": \"researcher1\",
  \"title\": \"عنوان پایان‌نامه\",
  \"abstract\": \"چکیده پایان‌نامه\",
  \"field\": \"پزشکی\",
  \"university\": \"دانشگاه تهران\",
  \"table_of_contents\": \"فهرست مطالب\",
  \"author\": \"نام نویسنده\",
  \"supervisor\": \"نام استاد راهنما\"
}"
# → {"id": 1, "status": "ready"}
```

Watch the Worker claim it, generate the article, and park it for review;
approve it in the panel (below) and watch the sender deliver it:

```bash
docker compose logs --tail=50 worker
curl.exe "http://localhost:8000/records/1?shared_Secret=REPLACE_WITH_REAL_SECRET"
```

Panel: set `PANEL_ADMIN_PASSWORD` + `PANEL_SECRET_KEY`, `docker compose up -d
web`, and open it through your HTTPS reverse proxy (it forwards to loopback
`127.0.0.1:8081`; plain HTTP on `:8081` is never exposed). Log in, give the
client their own account from the Users page — never share the admin login.

## Machine API reference

Interactive docs: **http://localhost:8000/docs** (Swagger UI sends real
requests — no mocks).

| Method & path | Auth | Purpose |
|---|---|---|
| `POST /submit` | `shared_Secret` in body (checked before validation) | Store thesis → `{"id","status":"ready"}`; `400` bad JSON, `401` bad secret, `422` validation/unknown `model_id`/`prompt_id` |
| `GET /health` | none | `{"status":"ok","database":"connected"}` iff `SELECT 1` succeeds |
| `GET /records` | `shared_Secret` query | Paginated listing: `page`, `page_size≤100`, `status`, `field`, `university`, `q`, `order=newest\|oldest` → `{items,total,page,page_size}` |
| `GET /records/{id}` | `shared_Secret` query | Full detail: status, `output_json`, thesis fields, audit FKs, timestamps, deliveries |
| `PATCH /records/{id}` | `shared_Secret` in body | `{action: approve\|reject\|save_draft, title?,lead?,body?}`; review-only, else `409` |
| `GET /deliveries` | `shared_Secret` query | Paginated; filters `status`, `platform`, `main_id` |
| `POST /deliveries/{id}/retry` | `shared_Secret` in body | Re-queue `failed` as `pending` (attempts reset); else `409` |
| `GET /stats` | `shared_Secret` query | Counts per status + Worker heartbeat freshness |
| `GET/POST /models`, `GET/PATCH /models/{id}` | `shared_Secret` | Model CRUD (`is_active` only via activate) |
| `POST /models/{id}/activate` | `shared_Secret` in body | Exactly one active model, atomically |
| `POST /models/{id}/test-connection` | `shared_Secret` in body | Real 1-token probe → `{ok,model,error}`; key never returned |
| `GET/POST /prompts`, `GET /prompts/{id}` | `shared_Secret` | Immutable versions: new version = new row, never edited |
| `POST /prompts/{id}/activate` | `shared_Secret` in body | Exactly one active prompt, atomically |

Contracts are frozen: `POST /submit` keeps its exact field names/casing
(including `shared_Secret`); `/health` and `/records/{id}` keep their fields.
New endpoints only add.

## Review panel

Persian RTL UI at the reverse-proxy address → loopback `:8081` → `web`
service (same image as `api`, different command). Jinja2 + locally vendored
HTMX (tables re-poll every 15s — no websockets), plain CSS ported
byte-identical from the approved design, vendored Vazirmatn. Full page/role
reference: `docs/admin-panel.md`.

| Page | What reviewers do there |
|---|---|
| Dashboard | KPIs, 7-day chart, Worker/sender health, recent events, channels |
| Records | Filterable queue; per-row approve shortcut and review links |
| Record review | Tabbed: editable text, raw model JSON, input metadata, history, delivery preview of exactly what approval will send; approve / save draft / reject / regenerate-with-model |
| Deliveries | Filterable send states; retry failed rows |
| Submit | Same intake as `POST /submit`, with model/prompt override selects |
| Models / Prompts / Channels | Activate + connection test; immutable prompt versions; routes matrix |
| Fields / Categories | Reference data with routing and usage |
| Users / Audit | Admin-only accounts (create, role, activate, password reset), own-password change on Profile, immutable event trail |

Roles: **viewer** (read-only — every mutation is `403`), **editor**
(review, retry, submit), **admin** (everything, incl. config and users).
First admin comes from `PANEL_ADMIN_USERNAME`/`PANEL_ADMIN_PASSWORD` at
startup; every login (including failures, with visitor IP), review, retry,
and config change is audited.

**Public-exposure hardening:** per-IP login lockout
(`PANEL_LOGIN_MAX_ATTEMPTS/WINDOW/LOCKOUT`), argon2 passwords, signed
`HttpOnly` + `SameSite=Lax` cookies (`Secure` when `PANEL_COOKIE_SECURE` or the
trusted proxy reports `https`), 12h agreed session expiry
(`PANEL_SESSION_HOURS`), and `X-Forwarded-For/Proto` honored only from the
local proxy plus `PANEL_TRUSTED_PROXIES` (direct clients' headers are
ignored). Plain HTTP transmits passwords unencrypted — TLS at the reverse
proxy is required, not optional.

## Database & migrations

PostgreSQL is the persistent store *and* the job queue. `migrations/*.sql`
are the authoritative schema, applied in filename order and tracked in
`_migrations` (each runs exactly once; the `migrate` one-shot service
no-ops afterwards; `api`/`worker`/`sender`/`web` all wait on it):

1. `001_initial_schema.sql` — `categories`, `fields`, `models`, `prompts`, `main`
2. `002_seed_categories.sql` — 13 categories + `other`
3. `003_seed_prompt.sql` — the v1 system prompt
4. `004_seed_model.sql` — the active model configuration
5. `005_channel_delivery.sql` — `channels`, `field_channel_routes`, `deliveries` + initial Bale channel and routes
6. `006_add_rejected_status.sql` — allow `rejected` on `main.status`
7. `007_model_config_columns.sql` — `models.base_url/api_key_env/temperature/max_output_tokens/description`
8. `008_panel_auth_audit.sql` — `roles`, `panel_users`, `audit_logs`
9. `009_seed_viewer_role.sql` — read-only `viewer` role
10. `010_fields_matched_keyword.sql` — `fields.matched_keyword`
11. `011_category_channel_routes.sql` — primary category→channel mapping
12. `012_delivery_routing_provenance.sql` — `deliveries.category_id`/`route_source`
13. `013_record_publish_timestamps.sql` — `main.approved_at`/`published_at`
14. `014_channel_bot_token_env.sql` — `channels.bot_token_env`
15. `015_field_category_source.sql` — `fields.category_source` (`rules`/`curated`)

Migrations are additive-only and never edited after applying — schema changes
always add the next numbered file. Full ER diagram: `docs/erd.md`.

## Workers

**Generation Worker** (`worker`): recovers stale `processing` jobs past
`WORKER_LEASE_MINUTES`, claims the oldest `ready` row with
`FOR UPDATE SKIP LOCKED`, resolves field → category deterministically,
links the effective model/prompt (per-record override → active row →
config fallback), keeps a heartbeat alive across the bounded LLM call, stores
`output_json` as `review` — or `failed` with the error text. Generation is
*not* retried; crashes recover to `ready` (at-least-once).

**Sender Worker** (`sender`): turns `review` rows into one `deliveries` row
per routed channel (`awaiting_confirmation` under the human gate, `pending`
otherwise, `pending` directly with the gate off), claims the oldest due
delivery, renders the message, sends via the platform client, waits
`SENDER_SEND_DELAY_SECONDS`, and retries retryable failures with exponential
backoff up to `SENDER_MAX_ATTEMPTS` (permanent errors fail immediately).
Sending *is* retried — a lost article is worse than a duplicate post.

## Security

- Machine secret from env only — never stored, logged, or sent to the LLM;
  checked with `hmac.compare_digest` **before** validation.
- LLM/bot credentials from env only; `models.api_key_env` stores an env *var
  name*, never a key; connection probes never return keys; channel rows are
  destinations, never credentials.
- Panel passwords are argon2 hashes; sessions are signed, `HttpOnly`,
  time-boxed cookies; login is lockout-guarded and audited.
- `.env` is git-ignored; `.env.example` holds placeholders only; no real
  secrets exist in source or Git history.
- `postgres` listens on loopback only; the panel binds loopback and is
  published solely through the TLS reverse proxy.

## Demo

A reproducible end-to-end demo lives in `docs/demo-guide.md` (health →
Swagger → submit → watch Worker → read record → unknown-field and duplicate
behavior → failure/recovery → SQL inspection), ending with the supervisor
Q&A (data retention, auth-before-validation, unknown fields, FIFO order,
statuses, recovery, at-least-once, secrets, Docker networking). The
junior-developer walkthrough is `docs/codebase-guide.md`.

## Testing

Against a dedicated PostgreSQL test database built from the same migrations
— no SQLite, no database mocks; platform HTTP is stubbed so the suite is
offline (one test file covers the live end-to-end flow with a stubbed LLM):

```bash
python -m pytest -q
```

159 tests cover auth order and contracts, normalization/classification
(including `معماری داخلی` vs `معماری`), Worker claim/process/fail/recovery,
model/prompt resolution priority, the human gate, review actions, delivery
retry policy, stats, panel pages/roles/lockout, and the full
panel→worker→approve→sender chain.

## Troubleshooting

| Symptom | Check |
|---|---|
| `/health` reports `disconnected` | `postgres` container, `DATABASE_URL`, compose network |
| Jobs stuck in `processing` | Worker logs; `WORKER_LEASE_MINUTES` vs heartbeat interval |
| Jobs in `failed` | `main.error_message` (LLM/provider errors) |
| Article never delivered | Still `awaiting_confirmation`? It needs panel approval; `pending` with future `next_attempt_at` is backing off; `failed` has `error_message` |
| Sends fail immediately | `deliveries.error_message`; `no bot token configured` means the platform token is missing from `sender` |
| Panel login loops back to `/login` | Cookie has `Secure` but you are on plain HTTP — go through the TLS proxy (or unset `PANEL_COOKIE_SECURE` for loopback-only use) |
| Panel login `429` | Per-IP lockout tripped — wait out `PANEL_LOGIN_LOCKOUT_MINUTES`, then check the audit trail |
| Container can't reach DB | Use `postgres:5432`, never `localhost`, inside containers |

## Docs

- `docs/architecture.md` — components, queues, recovery, security model
- `docs/codebase-guide.md` — junior-developer walkthrough of the whole lifecycle
- `docs/erd.md` — entity-relationship diagram (Mermaid)
- `docs/demo-guide.md` — reproducible supervisor demo
- `docs/admin-panel.md` — panel pages, endpoints used, roles, wiring proof

## License

Internal project — TETRA Company.
