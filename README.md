# IranDoc News Generation

![Python 3.12](https://img.shields.io/badge/python-3.12-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.141-009688)
![PostgreSQL 16](https://img.shields.io/badge/PostgreSQL-16-336791)
![Docker Compose](https://img.shields.io/badge/docker-compose-ready-2496ED)
![Tests](https://img.shields.io/badge/tests-697%20passed-brightgreen)

**A thesis-to-channel news pipeline for IranDoc.** A thesis is submitted as
structured metadata; one LLM call writes the Persian news article and classifies
the thesis into ISCED-F 2013 categories; the article is routed to the selected
channels of those categories and published automatically through the sender
Worker.

The project also ships a server-rendered Persian review panel for operators:
records, classification, manual overrides, deliveries, channels, models,
prompts, users, and the complete audit trail.

---

## Overview

```text
Thesis metadata
      │
      ▼
POST /submit ──▶ PostgreSQL (main.status = ready)
      │
      ▼
Generation Worker ── claim FIFO ──▶ ONE LLM call
      │                               ├─ news: title / lead / body
      │                               └─ categories: up to 3 × {name, score, reason}
      ▼
main_category_matches + output_json (status = review)
      │
      ▼
Sender Worker ── qualifying categories ──▶ category_channel_routes
      │                                      └─ Bale / Telegram
      ▼
deliveries (per article × channel) ──▶ published
```

The pipeline is **automatic by default**. A human gate can be enabled with
`REQUIRE_HUMAN_CONFIRMATION=true` when a deployment wants a reviewer to approve
each article before delivery.

### Design principles

- **PostgreSQL is both storage and queue.** No Redis, Celery, Kafka, or
  microservices. The system is a modular monolith with five runtime components:
  `postgres`, `api`, `worker`, `sender`, and `web` (the panel).
- **One LLM call does two jobs.** The model returns the article and its
  classification together; there is no separate rule-based classifier.
- **Classification is per record.** The result lives in
  `main_category_matches`. A reviewer can replace it manually at any time.
- **Routing is per category.** A category reaches exactly the channels selected
  on `/categories`; there is no field-level routing and no broadcast fallback.
- **Operational history is durable.** Deliveries, classifications, audit events,
  and channel routes are retained as history rather than overwritten.

---

## Features

### Machine API

- Shared-secret authentication.
- Swagger UI at `/docs` for interactive testing.
- Submission, record listing/detail, review actions, classification override,
  delivery listing and recovery, stats, models, prompts, and routing preview.

### Generation Worker

- Claims `ready` jobs with `FOR UPDATE SKIP LOCKED` in FIFO order.
- Makes one PydanticAI call per thesis against an OpenAI-compatible endpoint.
- Validates the model answer against the live ISCED-F 2013 vocabulary:
  hallucination filtering, score clamping, deduplication, thresholding, and
  top-N selection.
- Stores the article and category matches, then moves the record to `review`.
- Recovers abandoned `processing` jobs after a stale heartbeat.
- Failure handling rolls back the aborted transaction before recording the
  failure, so a database error cannot leave a job stuck forever.

### Sender Worker

- Resolves the union of channels from all qualifying category matches.
- Deduplicates channels reached by more than one category.
- Respects `channels.activated_at`: a newly enabled channel never receives the
  archive that predates it.
- Sends through Bale or Telegram, records the platform message id, retries
  transient failures with exponential backoff, and parks permanent failures for
  operator action.
- Supports retry, release, cancel, delete-from-channel, and manual mark-deleted
  actions from the panel.

### Review panel

- Persian RTL UI, server-rendered with Jinja2 and locally vendored HTMX.
- Dashboard with KPIs, 7-day chart, system health, event timeline, and channel
  status.
- Records list with SQL-backed filtering, pagination, multi-select, and bulk
  approval.
- Record detail tabs: news text, automatic classification, raw model output,
  metadata, history, delivery, and final prompt.
- Manual category override, regenerate, republish, and delivery recovery.
- Category and channel routing management.
- Models, prompts, users, roles, profile, and audit trail.
- Responsive layout and accessible focus states.

---

## Tech stack

| Layer | Technology |
|---|---|
| API | FastAPI + Pydantic |
| Database | PostgreSQL 16, SQLAlchemy 2 async, asyncpg |
| LLM | PydanticAI + OpenAI-compatible endpoint |
| Workers | asyncio, persistent polling loops |
| Panel | Jinja2, plain CSS, vendored HTMX, Vazirmatn |
| Auth | Shared secret (API), argon2 + signed cookies (panel) |
| Deployment | Docker Compose |

---

## Project structure

```text
.
├── app/
│   ├── api/                 # Machine API routes and rate limiting
│   ├── auth/                # Shared-secret verification
│   ├── db/                  # Async engine and ORM models
│   ├── fields/              # Field-name normalization (dedup identity only)
│   ├── messaging/           # Delivery queue, platform clients, sender Worker
│   ├── news/                # LLM generation, prompts, ISCED vocabulary
│   ├── panel/               # Server-rendered review panel
│   │   ├── app.py           # Panel routes and application wiring
│   │   ├── auth.py          # Passwords, sessions, login lockout
│   │   ├── authz.py         # Role/permission policy in one place
│   │   ├── csrf.py          # Session-bound CSRF protection
│   │   ├── static/          # panel.css, HTMX, Vazirmatn
│   │   └── templates/       # Jinja2 pages
│   ├── schemas/             # API and LLM contracts
│   ├── services/            # Domain services; all database access lives here
│   └── worker/              # Generation Worker
├── migrations/              # Ordered, immutable SQL migrations
├── scripts/                 # Migration runner and optional operational helpers
├── tests/                   # PostgreSQL-backed pytest suite
├── data/                    # Optional starter field names
├── docs/                    # Architecture, ERD, panel, prompt, and guide docs
├── demo/                    # Static machine-API demo UI
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── .env.example
```

### Layering rule

```text
HTTP routes (panel/API)
        │
        ▼
app/services/*  ── all database queries and state changes
        │
        ▼
SQLAlchemy ORM ──▶ PostgreSQL
```

Panel routes and API routes never query the ORM directly. This is enforced by
`tests/test_panel_layering.py`, so the two interfaces cannot drift apart.

---

## Quickstart

### 1. Requirements

- Docker and Docker Compose
- An OpenAI-compatible LLM endpoint and API key
- A Bale or Telegram bot token if news should be published

### 2. Configure

```bash
cp .env.example .env
```

Edit `.env` and set at least:

```dotenv
POSTGRES_PASSWORD=...
PANEL_ADMIN_PASSWORD=...
PANEL_SECRET_KEY=...
SHARED_SECRET=...
AVALAI_API_KEY=...
```

`LLM_MODEL` and `AVALAI_BASE_URL` are fallback values. The active model created
in the panel (`/models`) takes precedence.

### 3. Start the stack

```bash
docker compose up -d --build
docker compose ps
```

The migration service runs first and applies every migration exactly once.

Check the API:

```bash
curl http://localhost:8000/health
```

### 4. Open the panel

The panel listens on loopback and is intended to sit behind a TLS reverse proxy:

```text
http://127.0.0.1:8081
```

For local development without HTTPS, set `PANEL_COOKIE_SECURE=false`. If the
panel is behind a TLS proxy, keep it `true`. Log in with the credentials from
`.env` and create the first channels, models, and routes.

### 5. Optional: preload field names

The worker discovers field names automatically. To preload a starter list as
dedup identities:

```bash
docker compose exec api python scripts/seed_fields.py
```

---

## Configuration

Environment variables are read from the process environment, then `.env`.
`.env` is ignored by Git.

### Required for a working deployment

| Variable | Purpose |
|---|---|
| `POSTGRES_PASSWORD` | PostgreSQL password; Compose refuses to start without it |
| `PANEL_ADMIN_PASSWORD` | Initial panel admin password |
| `PANEL_SECRET_KEY` | Signs panel session cookies and CSRF tokens |
| `SHARED_SECRET` | Machine API authentication secret |
| `AVALAI_API_KEY` | LLM provider key (or set a model-specific key in the panel) |

### Generation and routing

| Variable | Default | Purpose |
|---|---|---|
| `LLM_MODEL` | `gpt-5.6-luna` | Fallback model id; the active panel model wins |
| `AVALAI_BASE_URL` | `https://api.avalai.ir/v1` | Fallback OpenAI-compatible endpoint |
| `LLM_TIMEOUT_SECONDS` | `300` | LLM call timeout |
| `LLM_MAX_OUTPUT_TOKENS` | `1000` | Output token cap |
| `TOP_N_CATEGORIES` | `3` | Maximum stored category matches per record |
| `MIN_CATEGORY_RELEVANCE` | `0.5` | Minimum score for a match to route |
| `SKIP_REASON_ABOVE_SCORE` | `90` | Optional reason threshold for compact output |
| `STORE_LLM_RAW_OUTPUT` | `false` | Store the raw LLM response for debugging |
| `REQUIRE_HUMAN_CONFIRMATION` | `false` | Gate deliveries behind manual approval |
| `UNMATCHED_CATEGORY_BEHAVIOR` | `review` | `review`, `fallback_other`, or `failed` |

### Workers and delivery

| Variable | Default | Purpose |
|---|---|---|
| `WORKER_POLL_INTERVAL_SECONDS` | `1` | Generation queue poll interval |
| `WORKER_HEARTBEAT_INTERVAL_SECONDS` | `30` | Worker liveness heartbeat |
| `WORKER_LEASE_MINUTES` | `5` | Stale generation job recovery |
| `SENDER_POLL_INTERVAL_SECONDS` | `2` | Sender queue poll interval |
| `SENDER_SEND_DELAY_SECONDS` | `5` | Delay between channel sends |
| `SENDER_MAX_ATTEMPTS` | `5` | Delivery retry cap |
| `SENDER_BACKOFF_BASE_SECONDS` | `10` | Exponential backoff base |
| `SENDER_LEASE_MINUTES` | `5` | Stale sending row recovery |
| `BALE_BOT_TOKEN` | empty | Bale bot token |
| `TELEGRAM_BOT_TOKEN` | empty | Telegram bot token |

### Panel

| Variable | Default | Purpose |
|---|---|---|
| `PANEL_ADMIN_USERNAME` | `admin` | Initial admin username |
| `PANEL_COOKIE_SECURE` | `false` | Enable behind HTTPS |
| `PANEL_TRUSTED_PROXIES` | empty | Trusted proxy CIDRs for forwarded headers |
| `PANEL_LOGIN_MAX_ATTEMPTS` | `10` | Login attempts before lockout |
| `PANEL_LOGIN_WINDOW_MINUTES` | `10` | Lockout window |
| `PANEL_LOGIN_LOCKOUT_MINUTES` | `15` | Lockout duration |
| `PANEL_SESSION_HOURS` | `12` | Session lifetime |

---

## Machine API

All endpoints except `/health` require the shared secret. The API also supports
query-string authentication where documented.

| Method | Endpoint | Purpose |
|---|---|---|
| `POST` | `/submit` | Validate a thesis and create a `ready` job |
| `GET` | `/health` | API and database health |
| `GET` | `/records` | Paginated records with filters |
| `GET` | `/records/{id}` | Record detail, output, deliveries, matches |
| `GET` | `/records/{id}/classification` | Stored category matches |
| `POST` | `/records/{id}/category-matches` | Replace classification manually |
| `POST` | `/records/{id}/republish` | Queue corrected categories |
| `PATCH` | `/records/{id}` | Approve, reject, or save a draft |
| `GET` | `/deliveries` | Paginated deliveries and status |
| `POST` | `/deliveries/{id}/retry` | Retry a failed delivery |
| `POST` | `/deliveries/{id}/release` | Release a gated delivery |
| `GET` | `/stats` | Status and classification counters |
| `GET` | `/routing/preview?record_id=` | Inspect a record's routes |
| `GET` | `/models` | List models |
| `POST` | `/models` | Create a model |
| `PATCH` | `/models/{id}` | Update model connection settings |
| `POST` | `/models/{id}/activate` | Activate a model |
| `POST` | `/models/{id}/test-connection` | Probe a model |
| `GET` | `/prompts` | List prompt versions |
| `POST` | `/prompts` | Create a prompt version |
| `POST` | `/prompts/{id}/activate` | Activate a prompt |

Example submission:

```bash
curl -X POST http://localhost:8000/submit \
  -H "Content-Type: application/json" \
  -d '{
    "shared_Secret": "YOUR_SHARED_SECRET",
    "username": "researcher1",
    "title": "عنوان پایان‌نامه",
    "abstract": "چکیده پایان‌نامه",
    "field": "پزشکی",
    "university": "دانشگاه تهران",
    "table_of_contents": "فهرست مطالب",
    "author": "نام نویسنده",
    "supervisor": "نام استاد راهنما"
  }'
```

The response is `{"id": 1, "status": "ready"}`. The Worker picks the job up
asynchronously.

---

## Review panel

| Page | Purpose |
|---|---|
| `/` | Dashboard, health, chart, timeline, channel status |
| `/records` | Record list and review/approval |
| `/records?tab=deliveries` | Delivery queue and recovery |
| `/records/{id}` | News, classification, metadata, history, delivery |
| `/submit` | Submit a new thesis from the panel |
| `/categories` | Classification categories and category→channel routing |
| `/channels` | Channel destinations, tokens, activation, deletion |
| `/models` | Model connection settings and connection tests |
| `/prompts` | Versioned prompt management |
| `/users` | Console users and roles |
| `/audit` | Immutable audit trail |
| `/profile` | Own account and password |

`/fields` is retired and redirects to `/categories`. Field names are only an
internal dedup identity, not a classification or routing surface.

### Roles

| Role | Capabilities |
|---|---|
| `viewer` | Read-only access |
| `editor` | Review, approve/reject, submit, delivery recovery |
| `admin` | Everything, including models, prompts, channels, categories, users |

The policy lives in `app/panel/authz.py`; tests assert the matrix and route
coverage.

### Classification and manual override

The LLM stores up to `TOP_N_CATEGORIES` matches in `main_category_matches`.
Every match at or above `MIN_CATEGORY_RELEVANCE` routes the article to that
category's channels. `other` is a fallback and never routes automatically.

To correct a classification, use the record's **دسته‌بندی‌های خودکار** tab or the
**اصلاح دستهٔ رکورد** card. The correction replaces the stored matches with one
manual match and updates the display primary category.

---

## Database

Migrations are immutable and applied in order by `scripts/apply_migrations.py`.
The `_migrations` table records every applied file.

Key tables:

| Table | Purpose |
|---|---|
| `main` | Thesis metadata, job state, generated output |
| `main_category_matches` | Per-record LLM/manual classification |
| `categories` | ISCED-F 2013 vocabulary + `other` |
| `category_channel_routes` | Category → channel routing matrix |
| `channels` | Bale/Telegram destinations and activation state |
| `deliveries` | One row per article × channel send |
| `fields` | Dedup identity for discipline names only |
| `models` | LLM connection configuration |
| `prompts` | Versioned system prompts |
| `panel_users`, `roles` | Panel authentication and authorization |
| `audit_logs` | Immutable operator/action history |

Schema changes are additive migrations, never edits to an already-applied file.
The latest migrations remove the retired field-level classification columns and
field routes, and localize category descriptions.

---

## Workers and automatic publishing

### Generation Worker

1. Recover stale `processing` jobs whose heartbeat exceeded the lease.
2. Claim the oldest `ready` job with `FOR UPDATE SKIP LOCKED`.
3. Register the field name as a dedup identity.
4. Resolve the model and prompt: per-record override → active row → config
   fallback.
5. Make one LLM call for news + classification.
6. Store `output_json` and `main_category_matches`; move to `review`.
7. On failure, roll back and mark the job `failed` safely.

### Sender Worker

1. Find processed articles with at least one qualifying category channel that
   has no delivery row.
2. Create deliveries as `pending` (or `awaiting_confirmation` when the human
   gate is enabled).
3. Claim due deliveries with `FOR UPDATE SKIP LOCKED`.
4. Render, send, record the platform message id, and update status.
5. Retry transient failures; park permanent failures for the operator.

### Automatic mode vs human gate

```dotenv
# Default: generate and publish without manual approval
REQUIRE_HUMAN_CONFIRMATION=false

# Optional: hold deliveries until an editor approves the record
REQUIRE_HUMAN_CONFIRMATION=true
```

Even in automatic mode, an `other`-only record publishes nowhere and remains
available for manual correction.

---

## Testing

The test suite uses a real PostgreSQL test database created from the migrations
— no SQLite, no mocked schema.

Local setup:

```bash
python3.12 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
pytest -q
```

The test harness creates `irandoc_news_test` from `DATABASE_URL`, applies every
migration, and truncates tables between tests.

Coverage highlights:

- API contract and authentication
- LLM response validation and classification golden cases
- Category routing, channel activation, multi-category unions
- Delivery retries, human gate, delete/republish flows
- Panel routing, permissions, CSRF, templates, and layering
- Worker recovery and failure handling

Latest run: **697 passed, 2 skipped**.

Lint the application, scripts, and tests:

```bash
pip install -r requirements-dev.txt
ruff check app scripts tests
```

---

## Operations

### Logs

```bash
docker compose logs -f worker
docker compose logs -f sender
docker compose logs -f web
```

### Health

```bash
curl http://localhost:8000/health
```

The dashboard health card reports the active model, last probe, Worker
heartbeat, and pending deliveries.

### Common issues

| Symptom | Check |
|---|---|
| Thesis stays `ready` | Is the worker running? `docker compose logs worker` |
| Thesis failed immediately | `main.error_message`; model key/base URL; provider status |
| Category missing | Check `main_category_matches`; use manual override if needed |
| No delivery rows | Does the record have a qualifying non-`other` match? Does its category have channel routes? |
| Delivery pending forever | `next_attempt_at`, sender logs, bot token |
| Delivery failed | `deliveries.error_message`; retry from the panel |
| 500 after schema change | Rebuild **all** services, not only `web`: `docker compose up -d --build` |
| Panel login loops | `PANEL_COOKIE_SECURE` must match HTTP/HTTPS; check proxy headers |

---

## Security

- Never commit `.env`, `auth.json`, bot tokens, or API keys.
- The panel is intended for loopback or a trusted TLS reverse proxy.
- Project settings/extensions run with the Pi process permissions; review
  untrusted repos before granting trust.
- API authentication uses a constant-time shared-secret check.
- Panel passwords use argon2; sessions are signed cookies; CSRF is
  session-bound.
- Login attempts are rate-limited per IP.
- Provider keys and bot tokens are encrypted at rest when stored in the panel;
  they are never rendered back or logged.
- Use a container/sandbox for untrusted or unattended execution.

---

## Documentation

| Document | Contents |
|---|---|
| [`docs/architecture.md`](docs/architecture.md) | System architecture and design decisions |
| [`docs/admin-panel.md`](docs/admin-panel.md) | Panel routes and operator behavior |
| [`docs/codebase-guide.md`](docs/codebase-guide.md) | Code layout and extension guide |
| [`docs/erd.md`](docs/erd.md) | Entity relationship diagram |
| [`docs/llm-prompt.md`](docs/llm-prompt.md) | Prompt and classification contract |
| [`docs/demo-guide.md`](docs/demo-guide.md) | Machine API demo |

---

## License

Internal IranDoc project. Not published under an open-source license.
