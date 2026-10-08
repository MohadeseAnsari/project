# IranDoc News Generation

Automatic journalistic news generation from IranDoc thesis metadata. A thesis
is submitted as JSON, stored in PostgreSQL as a job, and processed by a
single persistent Worker that generates a Persian news article (title, lead,
body) using an LLM.

## Architecture

```
Input JSON
    ↓
FastAPI  (authenticate → validate → store)
    ↓
PostgreSQL  (main.status = 'ready')   ← PostgreSQL is both storage and queue
    ↓
Persistent sequential Worker  (claim → normalize → classify → generate)
    ↓
AvalAI / PydanticAI  (LLM news generation — the ONLY use of the LLM)
    ↓
PostgreSQL  (main.status = 'review', output_json = {title, lead, body})
    ↓
Persistent sender Worker  (processed-but-unsent → render → send → mark sent)
    ↓
Bale / Telegram channels  (deliveries.status = 'sent')
```

- **PostgreSQL** is both the persistent store and the job queue.
- **One persistent sequential Worker** processes jobs one at a time, oldest
  first (`ORDER BY created_at`, `FOR UPDATE SKIP LOCKED`).
- **One persistent sender Worker** publishes finished articles to channels,
  with a configurable delay between sends. It is a separate process so channel
  rate limits and platform outages never affect generation.
- **The LLM is used ONLY for news generation.** Field/category classification
  is deterministic and rule-based — the LLM never classifies.
- **No Redis, Celery, Kafka, Kubernetes, or microservices.**

## Project Structure

```
project/
├── app/
│   ├── main.py               # FastAPI app, lifespan, OpenAPI customization
│   ├── config.py             # Settings from environment / .env
│   ├── db/
│   │   ├── connection.py     # Async engine + session factory
│   │   └── models.py         # SQLAlchemy ORM models
│   ├── schemas/news.py       # Pydantic request/response contracts
│   ├── api/
│   │   ├── routes.py         # /submit, /health, /records/{id}
│   │   └── limits.py         # Rate limiter (10/min per client)
│   ├── auth/secret.py        # Constant-time shared-secret verification
│   ├── fields/
│   │   ├── normalization.py  # Unicode NFC, zero-width removal, whitespace collapse
│   │   └── classification.py # Deterministic keyword rules → category
│   ├── news/
│   │   ├── generation.py     # PydanticAI Agent + AvalAI client
│   │   └── prompts.py        # Versioned system prompt (injection-safe)
│   ├── worker/worker.py      # Persistent sequential Worker + recovery
│   └── messaging/
│       ├── formatting.py     # Article → channel message text
│       ├── platforms.py      # Bale + Telegram HTTP clients
│       ├── dispatcher.py     # Delivery queue: enqueue / claim / retry
│       └── worker.py         # Persistent sender Worker + recovery
├── migrations/               # SQL migrations (authoritative schema)
├── scripts/
│   ├── apply_migrations.py    # Ordered, once-only migration runner
│   ├── seed_fields.py        # Loads data/fields.xlsx into the fields table
│   └── manage_channels.py    # Channels + field → channel routes CLI
├── tests/                    # pytest suite (PostgreSQL-backed)
├── data/fields.xlsx          # 533 source field names
├── docs/                     # architecture.md, erd.md, codebase-guide.md
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── .env.example              # Placeholder values only — no real secrets
```

## Requirements

- Python 3.12
- PostgreSQL 16
- Docker + Docker Compose (for the containerized stack)

## Environment Variables

Copy `.env.example` to `.env` and fill in real values. `.env` is git-ignored.

| Variable | Description | Required |
|----------|-------------|----------|
| `DATABASE_URL` | PostgreSQL connection URL | Yes |
| `SHARED_SECRET` | Authentication secret | Yes |
| `AVALAI_API_KEY` | AvalAI API key | Yes |
| `AVALAI_BASE_URL` | AvalAI base URL | Yes |
| `LLM_MODEL` | Model runtime identifier (`gpt-5.6-luna`) | Yes |
| `LLM_TIMEOUT_SECONDS` | LLM call timeout | No (default: 120) |
| `WORKER_POLL_INTERVAL_SECONDS` | Queue poll interval | No (default: 1) |
| `WORKER_HEARTBEAT_INTERVAL_SECONDS` | Heartbeat interval | No (default: 30) |
| `WORKER_LEASE_MINUTES` | Stale-job recovery timeout | No (default: 5) |
| `BALE_BOT_TOKEN` | Bale bot token used by the `sender` service | Yes for sending |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token (unused until a Telegram channel is routed) | No |
| `SENDER_SEND_DELAY_SECONDS` | Delay between two consecutive channel sends | No (default: 5) |
| `SENDER_POLL_INTERVAL_SECONDS` | Sender idle poll interval | No (default: 2) |
| `SENDER_MAX_ATTEMPTS` | Send attempts before a delivery is parked as `failed` | No (default: 5) |
| `SENDER_BACKOFF_BASE_SECONDS` | Base for exponential send backoff | No (default: 10) |
| `SENDER_REQUEST_TIMEOUT_SECONDS` | Per-send HTTP timeout | No (default: 20) |
| `SENDER_LEASE_MINUTES` | Abandoned-send recovery timeout | No (default: 5) |
| `REQUIRE_HUMAN_CONFIRMATION` | Gate deliveries as awaiting_confirmation until approval | No (default: true) |
| `PANEL_ADMIN_USERNAME` | Review panel first admin username | No (default: admin) |
| `PANEL_ADMIN_PASSWORD` | Review panel first admin password (env only, hashed with argon2) | Yes for panel login |
| `PANEL_SECRET_KEY` | Review panel cookie signing key | Yes for panel |
| `POSTGRES_PASSWORD` | PostgreSQL admin password (postgres service) | No (default: postgres) |
| `POSTGRES_DB` | PostgreSQL database name (postgres service) | No (default: irandoc_news) |

## Running Locally (Docker Compose)

```bash
cp .env.example .env          # edit with real credentials
docker compose up -d
docker compose ps
curl.exe http://localhost:8000/health
```

Review panel (same image, `web` service): set `PANEL_ADMIN_PASSWORD` +
`PANEL_SECRET_KEY` in `.env`, then open **http://YOUR_HOST:8081/login**
(`PANEL_PORT`, default `8081`, binds all interfaces). Plain HTTP transmits
the password unencrypted — put TLS in front (reverse proxy) before exposing
it; to revert to loopback-only access set `PANEL_PORT=127.0.0.1:8081`. See
`docs/admin-panel.md` for pages, roles, and security notes.

Apply migrations and seed fields (first run only):

```bash
docker compose exec api python scripts/seed_fields.py
```

## Database Migrations

SQL migrations in `migrations/` are the authoritative schema. They are applied
in filename order and tracked in the `_migrations` table, so each runs exactly
once:

1. `001_initial_schema.sql` — tables, constraints, indexes
2. `002_seed_categories.sql` — 13 categories + `other`
3. `003_seed_prompt.sql` — the v1 system prompt
4. `004_seed_model.sql` — the active model configuration
5. `005_channel_delivery.sql` — `channels`, `field_channel_routes`,
   `deliveries`, plus the initial Bale channel and its routes for every field
6. `006_add_rejected_status.sql` — allow `rejected` on `main.status`
7. `007_model_config_columns.sql` — `models.base_url/api_key_env/temperature/max_output_tokens/description`
8. `008_panel_auth_audit.sql` — `roles`, `panel_users`, `audit_logs`
9. `009_seed_viewer_role.sql` — read-only `viewer` role

Migrations run automatically: the one-shot `migrate` service executes
`scripts/apply_migrations.py` on every `docker compose up`, and both `api` and
`worker` wait on it via `condition: service_completed_successfully`. Because the
runner skips already-recorded filenames, it is a no-op on subsequent runs, and a
failed migration stops `api`/`worker` from starting rather than letting them run
against a missing schema. No job or field rows are inserted by this step.

To run it by hand:

```bash
docker compose exec api python scripts/apply_migrations.py
```

## Field Seed

Loads the 533 field names from `data/fields.xlsx` into the `fields` table,
normalized and deterministically classified. Idempotent
(`ON CONFLICT (normalized_name) DO NOTHING`):

```bash
docker compose exec api python scripts/seed_fields.py
```

## Channel Delivery

Once an article reaches `status = 'review'` it is *processed*. The `sender`
service publishes processed articles to channels:

```
main.status = 'review'  →  deliveries row per routed channel  →  send  →  deliveries.status = 'sent'
```

- **What counts as "processed but not sent"** is not a flag — it is the absence
  of a delivery for a routed channel. Once a field fans out to several
  channels, an article has one independent send state per destination, each with
  its own attempt count and error.
- **Message format** (per channel, `📢` footer names the channel):

  ```
  📰 *Title*

  📰 Lead

  📄 Body

  📢 @newschanneltest123
  ```

- **Throttle**: `SENDER_SEND_DELAY_SECONDS` is applied after every send attempt,
  so a channel never sees a burst.
- **Retries**: timeouts, rate limits and 5xx responses are retried with
  exponential backoff up to `SENDER_MAX_ATTEMPTS`, then the delivery is parked
  in `failed` with its error for manual inspection. A permanent error (unknown
  chat, revoked token, bad request) fails immediately instead of burning
  attempts that could never succeed.
- **Crash recovery**: a `sending` row older than `SENDER_LEASE_MINUTES` is
  re-queued, trading a possible duplicate post for never losing an article.
- **Human confirmation** gates publishing when `REQUIRE_HUMAN_CONFIRMATION=true`
  (default): queueing writes `awaiting_confirmation`, and approval flips it to
  `pending`. With the flag off, queueing writes `pending` directly.

Inspect delivery state:

```sql
SELECT d.id, d.status, d.attempts, d.error_message,
       c.platform, c.chat_id, d.sent_at
FROM deliveries d JOIN channels c ON c.id = d.channel_id
ORDER BY d.id DESC LIMIT 20;
```

### Channels and field → channel routes

Every field currently routes to the one Bale channel. Routes are rows, so
splitting fields across channels or adding Telegram is a data change:

```bash
docker compose exec api python scripts/manage_channels.py list
docker compose exec api python scripts/manage_channels.py add-channel \
    --platform telegram --chat-id -1001234567890 --username @mychannel
docker compose exec api python scripts/manage_channels.py route --field-id 12 --channel-id 2
docker compose exec api python scripts/manage_channels.py unroute --field-id 12 --channel-id 1
```

A field with no route falls back to all active channels, so fields discovered
at runtime (unknown fields created by the generation Worker) still publish.

## API / Swagger

Open **http://localhost:8000/docs** — Swagger UI is the real interactive API
client. For every endpoint you can click **Try it out**, edit the request,
click **Execute**, and the browser sends a real HTTP request to the running
server and shows the real response. No mocks, no static responses.

### POST /submit

Request body (all nine fields required):

```json
{
  "shared_Secret": "REPLACE_WITH_REAL_SECRET",
  "username": "researcher1",
  "title": "عنوان پایان‌نامه",
  "abstract": "چکیده پایان‌نامه",
  "field": "پزشکی",
  "university": "دانشگاه تهران",
  "table_of_contents": "فهرست مطالب",
  "author": "نام نویسنده",
  "supervisor": "نام استاد راهنما"
}
```

Response: `{"id": 1, "status": "ready"}`

### GET /health

```bash
curl.exe http://localhost:8000/health
```

Returns `{"status": "ok", "database": "connected"}` when the API can reach
PostgreSQL. It checks API + database connectivity; it does **not** claim to
monitor the Worker (the Worker is a batch process with no HTTP surface).

### GET /records/{id}

```bash
curl.exe "http://localhost:8000/records/1?shared_Secret=REPLACE_WITH_REAL_SECRET"
```

Returns the record's status and, when finished, its generated output.

## Logs

```bash
docker compose logs --tail=100 api
docker compose logs --tail=100 worker
docker compose logs --tail=100 postgres
```

## PostgreSQL

- **Service / host inside Docker:** `postgres`
- **Port:** `5432`
- **Database:** `irandoc_news`
- **User:** `postgres`
- **Data volume:** `pg_data` (persists across container recreation)

Inside the containers, the API and Worker reach PostgreSQL at
`postgres:5432` — never `localhost:5432` (inside a container, `localhost` is the
container itself). Port 5432 is published to the host only for tools like
pgAdmin; internal communication does not need it.

## Security

- `shared_Secret` comes from the environment only; it is never stored in the
  database, never logged, and never sent to the LLM.
- The secret is checked with `hmac.compare_digest` (constant time) **before**
  thesis-content validation, so an invalid secret never creates a record.
- `.env` is git-ignored; `.env.example` contains placeholders only.
- No real API keys, passwords, or secrets are in source code or Git.

## Demo / Supervisor Presentation

A reproducible end-to-end demo (Assignment 4):

1. **Start services**
   ```bash
   docker compose up -d
   docker compose ps
   ```
2. **Check health** — `curl.exe http://localhost:8000/health` →
   `{"status":"ok","database":"connected"}`.
3. **Open Swagger** — http://localhost:8000/docs.
4. **Submit a valid thesis** (all 9 fields) via `POST /submit` →
   `{"id": 1, "status": "ready"}`. The record enters the `ready` queue.
5. **Watch the Worker** — `docker compose logs -f worker`. It claims the job
   (`processing`), normalizes/classifies the field, calls the LLM, and stores
   the result.
6. **See the result** — `GET /records/1?shared_Secret=...` →
   `status: "review"` with `output_json` containing the generated
   `title`/`lead`/`body`.
7. **Unknown field** — submit a thesis with a field not in the Excel dataset.
   The Worker inserts it into `fields` with `source='runtime'` and a stable
   normalized identity, classified into a category (or `other`).
8. **Duplicate unknown field** — submit the same unknown field again. It is
   reused (no duplicate `fields` row) because matching is on `normalized_name`.
9. **Failure/recovery** — a job that fails (e.g. LLM error) is marked `failed`
   with the error message. A job left in `processing` by a crashed Worker is
   reset to `ready` after `WORKER_LEASE_MINUTES` and reprocessed
   (at-least-once).

## SQL Demo Commands

The main application table is **`main`** (not "records"). Real columns from the
current schema:

```sql
-- Recent job records
SELECT id, status, field, created_at
FROM main
ORDER BY id DESC
LIMIT 10;

-- Count jobs by status
SELECT status, count(*)
FROM main
GROUP BY status;

-- Recently seeded/registered fields
SELECT id, name, source
FROM fields
ORDER BY id DESC
LIMIT 10;

-- Categories
SELECT id, name
FROM categories
ORDER BY id;

-- A finished record with its generated news
SELECT id, status, field, output_json
FROM main
WHERE status = 'review'
ORDER BY id DESC
LIMIT 5;
```

## Assignment 4 Demo Questions

- **What data is accepted and retained?** The nine input fields are validated
  and stored in `main`; the shared secret is checked but never stored.
- **Why is the Shared Secret checked before content validation?** So an
  unauthenticated request never reaches thesis validation and never creates a
  record — authentication precedes processing protected data.
- **How are unknown fields registered?** Normalized → exact match → if absent,
  classified and inserted into `fields` with `source='runtime'`.
- **How are duplicate unknown fields prevented?** Matching is on the unique
  `normalized_name`; repeated values reuse the same row.
- **How are spacing/letter differences handled?** Unicode NFC normalization,
  zero-width removal, trim, and whitespace collapsing produce a stable
  identity.
- **What is the `معماری داخلی` issue?** A broad `معماری` rule must not
  swallow the specific `معماری داخلی`. Specific keywords are listed before
  broad ones, so `معماری داخلی` maps to its own rule first.
- **Why one mapping fix may not be enough.** Fixing a single keyword without
  checking rule order and substring precedence can misclassify other fields;
  the fix must be systematic (specific-before-broad across all rules).
- **Why one persistent sequential Worker?** At this scale PostgreSQL is the
  queue; a single Worker keeps processing ordered and the architecture simple.
- **How does queue order work?** `ORDER BY created_at` (oldest first) with
  `FOR UPDATE SKIP LOCKED` for safe claiming.
- **What happens when the queue is empty?** The Worker waits
  `WORKER_POLL_INTERVAL_SECONDS` and keeps looping; it stays alive.
- **What do the statuses mean?** `ready` (queued) → `processing` (claimed) →
  `review` (generated) or `failed` (error recorded).
- **How is stale processing recovered?** Jobs with `heartbeat_at` older than
  `WORKER_LEASE_MINUTES` are reset to `ready` and reprocessed.
- **Model failure vs Worker crash.** A model/LLM error → `failed`. A Worker
  crash mid-job → the job stays `processing` with a stale heartbeat → recovered
  to `ready`.
- **What if the model response arrives but the DB save fails?** The job stays
  `processing`; recovery resets it to `ready` and it is reprocessed.
- **Why at-least-once?** Crash recovery reprocesses the job, so a duplicate LLM
  call is possible. Exactly-once would require distributed transactions.
- **Why local Docker success ≠ VPS success.** Ports, volumes, environment
  variables, and networking must be verified on the VPS; service names and
  `localhost` behave differently.
- **Docker image/container/volume/network.** Images are built from
  `Dockerfile`; containers run from images; `pg_data` is a named volume for
  persistence; services communicate on the default bridge network.
- **Why `localhost` differs inside a container.** Inside a container,
  `localhost` is that container; services reach each other by service name
  (`postgres`).
- **Why PostgreSQL does not need a public port internally.** The API/Worker
  reach it at `postgres:5432` on the internal network; the published port is
  only for host tools.
- **How do secrets reach the app?** Via environment variables from `.env`
  (git-ignored), never baked into images.
- **Health vs logs vs metrics.** `/health` checks API + database connectivity;
  logs give operational visibility; Prometheus/Grafana are not required at
  this stage.

## Testing

Tests run against a dedicated PostgreSQL test database (`irandoc_news_test`)
built from the same migrations — no SQLite, no mocks of the database:

```bash
python -m pytest -q
```

## License

Internal project — TETRA Company.
