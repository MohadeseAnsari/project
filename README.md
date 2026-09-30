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
```

- **PostgreSQL** is both the persistent store and the job queue.
- **One persistent sequential Worker** processes jobs one at a time, oldest
  first (`ORDER BY created_at`, `FOR UPDATE SKIP LOCKED`).
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
│   └── worker/worker.py      # Persistent sequential Worker + recovery
├── migrations/               # SQL migrations (authoritative schema)
├── scripts/
│   ├── apply_migrations.py    # Ordered, once-only migration runner
│   └── seed_fields.py        # Loads data/fields.xlsx into the fields table
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
| `POSTGRES_PASSWORD` | PostgreSQL admin password (postgres service) | No (default: postgres) |
| `POSTGRES_DB` | PostgreSQL database name (postgres service) | No (default: irandoc_news) |

## Running Locally (Docker Compose)

```bash
cp .env.example .env          # edit with real credentials
docker compose up -d
docker compose ps
curl.exe http://localhost:8000/health
```

Apply migrations and seed fields (first run only):

```bash
docker compose exec api python scripts/apply_migrations.py
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
