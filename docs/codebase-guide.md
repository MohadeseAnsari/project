# Codebase Guide

A junior-developer guide to the IranDoc News Generation service. It explains
what each part exists for and how a thesis becomes a news article. The
authoritative requirements live in `docs/irandoc_four_assignments_english_only.txt`
(one directory above the project root); this guide explains how the code
satisfies them.

## 1. Overall architecture

The service is a **modular monolith**: one Python codebase, one deployable
application, four runtime components (PostgreSQL, API, Worker, Sender). There
are no microservices, no Redis/Celery/Kafka, and exactly one Worker of each kind.

```
Client ──POST /submit──▶ FastAPI ──INSERT──▶ PostgreSQL (main.status='ready')
                                                     │
Worker ◀──claim (FOR UPDATE SKIP LOCKED, FIFO)──────┘
  │  1. normalize + rule-based classify field (deterministic, no LLM)
  │  2. PydanticAI Agent ──▶ AvalAI (OpenAI-compatible) ──▶ {title, lead, body}
  ▼
PostgreSQL (main.status='review', output_json=..., model_id, prompt_id)
  │
Sender ◀──claim deliveries (processed, not yet sent for that channel)
  │  render message ──▶ Bale / Telegram ──▶ deliveries.status='sent'
  ▼
Channel (throttled: SENDER_SEND_DELAY_SECONDS between sends)
```

Separation of responsibilities (Assignment 2):

| Concern | Owner | Rule |
|---|---|---|
| Authenticate / validate / create job | API | Never calls the LLM |
| Normalize + classify field | `app/fields/` | Deterministic rules, never the LLM |
| Generate news | `app/news/` (PydanticAI + AvalAI) | Only journalistic generation |
| Job state / queue / results | PostgreSQL | Source of truth |
| Sequential processing + recovery | `app/worker/` | One Worker, persistent |
| Publishing to channels + send retry | `app/messaging/` | Separate Worker, never blocks generation |

## 2. Project structure

```
project/
├── app/
│   ├── main.py               # FastAPI app, lifespan (logging + engine disposal)
│   ├── config.py             # pydantic-settings: all config from env/.env
│   ├── db/
│   │   ├── connection.py     # shared async engine, session factory, get_session
│   │   └── models.py         # SQLAlchemy ORM models (schema via migrations)
│   ├── schemas/news.py       # Pydantic contracts: ThesisInput, GeneratedNews, ...
│   ├── api/
│   │   ├── routes.py         # /submit, /health, /records/{id}
│   │   └── limits.py         # slowapi rate limiter (10/min per client)
│   ├── auth/secret.py        # constant-time shared-secret verification
│   ├── fields/
│   │   ├── normalization.py  # Unicode NFC, zero-width removal, whitespace collapse
│   │   └── classification.py # priority-ordered keyword rules -> category
│   ├── news/
│   │   ├── generation.py     # PydanticAI Agent + AvalAI client
│   │   └── prompts.py        # versioned system prompt (injection-safe)
│   ├── worker/worker.py      # persistent sequential Worker + recovery
│   └── messaging/
│       ├── formatting.py     # article -> channel message (emoji sections, bold title)
│       ├── platforms.py      # Bale + Telegram sendMessage clients
│       ├── dispatcher.py     # delivery queue: enqueue, claim, retry policy
│       └── worker.py         # persistent sender Worker + recovery
├── migrations/               # SQL migrations (applied by scripts/apply_migrations.py)
├── scripts/
│   ├── apply_migrations.py    # ordered, once-only migration runner (_migrations table);
│   │                          # run automatically by the one-shot `migrate` compose service
│   ├── seed_fields.py        # loads data/fields.xlsx into the fields table
│   └── manage_channels.py    # channels + field -> channel routes CLI
├── tests/                    # pytest suite (PostgreSQL-backed unit + E2E tests)
├── data/fields.xlsx          # 533 source field names
├── docs/                     # this guide, architecture.md, erd.md, llm-prompt.md
├── Dockerfile                # python:3.12-slim, non-root user
├── docker-compose.yml        # postgres + api + worker + sender, named volume
├── requirements.txt
└── .env.example              # documented variables, no real secrets
```

## 3. Request lifecycle (API)

`POST /submit` in `app/api/routes.py`:

1. **Parse JSON** — malformed bodies get `400`.
2. **Authenticate** — `shared_Secret` is compared with the environment
   configured secret using `hmac.compare_digest` (constant time). Missing or
   wrong secret → `401`. **Authentication happens before validation**, so a
   failed authentication never creates a database record.
3. **Validate** — Pydantic `ThesisInput` enforces the nine-field contract
   (all nine fields are required, including `table_of_contents`;
   `shared_Secret` is checked separately, before this model is applied).
   Empty/whitespace-only strings → `422`. Unknown extra fields are ignored.
4. **Store** — a `main` row is inserted with `status='ready'` and returned as
   `{"id": <int>, "status": "ready"}` with `200`.
5. **No LLM** — the request path never calls the LLM; generation is
   asynchronous in the Worker.

`GET /health` runs `SELECT 1` and reports `ok/connected` vs
`unhealthy/disconnected` — it distinguishes "process alive" from
"application actually healthy". `GET /records/{id}?shared_Secret=...`
returns a record's status and (when finished) its output.

## 4. Worker lifecycle

`app/worker/worker.py` runs a persistent loop (`run_worker`):

1. **Recover** — `recover_stale_jobs` resets `processing` jobs whose
   `heartbeat_at` is older than `WORKER_LEASE_MINUTES` back to `ready`.
2. **Claim** — `claim_next_job` selects the oldest `ready` job
   (`ORDER BY created_at`, FIFO) with `FOR UPDATE SKIP LOCKED`, sets
   `processing`, and stamps `claimed_at`/`heartbeat_at`.
3. **Process** — `process_job`:
   - resolves the field (exact normalized match, else classify + insert as
     `source='runtime'`) and links `field_id`/`category_id`;
   - links the active `model_id`/`prompt_id` (auditability);
   - starts a heartbeat task, calls the LLM, stores
     `output_json={title, lead, body}`, sets `review`.
   - `category_id` is a snapshot: re-classifying the field later (new
     keywords, `scripts/reclassify_fields.py --apply`) never rewrites
     `main` rows, so history stays stable. The record page shows both the
     stored category and the field's current one when they differ.
4. **Fail** — any exception → `fail_job` sets `failed` with the error text
   (truncated to 2000 chars). There is **no automatic retry counter**; failed
   jobs stay failed.
5. **Empty queue** — the Worker waits `WORKER_POLL_INTERVAL_SECONDS` and
   loops; it stays alive. SIGTERM/SIGINT trigger graceful shutdown after the
   current job.

## 5. Database

PostgreSQL is both the persistent store and the queue. Tables (see
`migrations/001_initial_schema.sql` and `docs/erd.md`):

- **categories** — 13 seeded categories + `other` (migration 002).
- **fields** — 533 fields seeded from `data/fields.xlsx` (migration-free,
  via `scripts/seed_fields.py`) plus runtime-discovered fields;
  `normalized_name` is the unique stable identity, `source` distinguishes
  `spreadsheet` vs `runtime`.
- **models** — LLM model configuration; `is_active` selects the one in use.
- **prompts** — versioned system prompts; `is_active` selects the one in use.
- **main** — the thesis/news record and queue entry: thesis fields, `status`
  (`ready|processing|review|failed|rejected`, CHECK constraint), `field_id` /
  `category_id` / `model_id` / `prompt_id` audit FKs, `output_json` (JSONB),
  `error_message`, `claimed_at` / `heartbeat_at` (recovery), `created_at` /
  `updated_at`. Indexed on `(status, created_at)`, `field_id`, `category_id`.
- **channels** — a publishing destination: `platform` (`bale|telegram`,
  CHECK), `chat_id` (BIGINT, because chat ids exceed 2³¹), `username` (rendered
  as the message footer), `title`, `is_active`. Unique per
  `(platform, chat_id)`. Migration 005 seeds the one Bale channel in use.
  **Many rows coexist**: the panel adds a channel beside the existing ones and
  edits, switches on/off and deletes each one independently; a channel with
  deliveries is disabled rather than deleted, so published history keeps
  resolving. `bot_token_encrypted` (Fernet, migration 019) is an *optional*
  per-channel token written from the panel form — it wins over the
  platform-wide one, is never rendered back, and is what lets a channel be
  added with no environment change.
- **category_channel_routes** — retired. No longer written, no longer read, and
  the panel has no screen for it: the owner removed the category → channel
  configuration because news goes to every active channel regardless. Kept so
  existing rows are not lost.
- **field_channel_routes** — retired from the delivery decision (item 6) and no
  longer written by the panel. The table is kept so existing rows and the
  history they explain are not lost.
- **deliveries** — one row per (article, channel): `status`
  (`pending|sending|sent|failed|awaiting_confirmation`, CHECK), `attempts`,
  `platform_message_id`, `error_message`, `claimed_at`, `next_attempt_at`,
  `sent_at`, plus `category_id`/`route_source` snapshots of which category
  and which route level produced the row. Unique per `(main_id, channel_id)`,
  which is what makes queueing idempotent. Indexed on
  `(status, next_attempt_at)`.

The schema is created **only** by SQL migrations tracked in the `_migrations`
table. The test suite runs against a dedicated PostgreSQL test database
(`irandoc_news_test`) built from the same migrations — no SQLite, no
`create_all()`.

## 6. Queue

The generation queue **is** the `main` table. Claiming uses
`SELECT ... WHERE status='ready' ORDER BY created_at LIMIT 1 FOR UPDATE
SKIP LOCKED`, which gives FIFO creation-order processing and safe claiming.
No external queue system is used (Assignment 2/4).

Publishing has its own queue, the `deliveries` table, because "processed but
not sent" is per *channel*: once a field fans out to several channels one
article has several independent send states. The sender claims the oldest **due**
delivery (`status='pending' AND next_attempt_at <= now()`) with the same
`FOR UPDATE SKIP LOCKED`, then waits `SENDER_SEND_DELAY_SECONDS`.

`main.status='review'` means *processed*; a review row with no delivery for a
routed channel means *not yet sent*. That is why there is no `sent` flag on
`main`.

## 7. Field normalization

`app/fields/normalization.py` — pure, deterministic:

1. Unicode NFC normalization,
2. zero-width character removal (`U+200B`, `U+200D`, `U+200E`, `U+200F`,
   `U+FEFF`),
3. trim + collapse internal whitespace.

The result is the field's **stable identity**: exact matching, duplicate
prevention (unique index on `normalized_name`), and runtime insertion all use
it. Re-running the seed script is idempotent
(`ON CONFLICT (normalized_name) DO NOTHING`).

## 8. Classification

`app/fields/classification.py` — deterministic, rule-based, **never the LLM**.
Token-aware longest-match rules map a normalized field to a category:
multi-token phrases match exactly, ≤4-char keywords match whole tokens only,
longer keywords may substring-match at lower score, and the longest match
wins (category order breaks ties only). `معماری داخلی` matches its specific
rule and is **not** swallowed by the broad `معماری` rule; `مو` can never fire
inside `موزه`. `classify_with_reason` returns `(category, matched_keyword,
rule)` so every decision is explainable. Unmatched fields fall back to the
`other` category. Unknown incoming fields are inserted with
`source='runtime'` and the deterministically classified category.

## 9. LLM generation

`app/news/generation.py` — PydanticAI `Agent` with
`output_type=GeneratedNews` (`title`, `lead`, `body`) over AvalAI's
OpenAI-compatible API (`AsyncOpenAI` client). The system prompt
(`app/news/prompts.py`) frames thesis metadata as **data, not instructions**
(to prevent prompt injection from thesis content) and forbids unsupported
additions, promotional language, and unnecessary repetition. The client uses
a bounded timeout (`LLM_TIMEOUT_SECONDS`, default 120s — see Engineering
decisions). The LLM is never used for classification, and no secrets are
included in its input.

## 10. Error handling

| Layer | Behavior |
|---|---|
| API | 400 malformed JSON, 401 bad secret, 422 validation, 500 DB failure (no record created) |
| Worker claim | row-level lock; crash-safe |
| LLM failure | job → `failed` with error text; Worker continues with the next job |
| Worker loop | per-iteration try/except; logs and keeps polling |
| Heartbeat | best-effort; failures logged, do not fail the job |
| Send timeout / 429 / 5xx | retryable → delivery back to `pending` with backoff, then `failed` at `SENDER_MAX_ATTEMPTS` |
| Send 4xx / `ok:false` | permanent → delivery `failed` immediately (an unknown chat stays unknown) |
| No bot token for a platform | delivery `failed` with a clear error rather than being silently skipped |
| Sender loop | per-iteration try/except; logs and keeps polling |

## 11. Recovery

If the Worker dies mid-job (crash, container kill, host restart), the job
stays `processing` with a stale heartbeat. On the next loop iteration,
`recover_stale_jobs` resets it to `ready` and it is processed again. This
covers both a crash **before** the LLM call and a crash **after** the LLM
response but before the DB commit — the trade-off is at-least-once
processing (a possible duplicate LLM call), which is the accepted engineering
decision for this design. `WORKER_LEASE_MINUTES` (default 5) controls how
quickly abandoned jobs are recovered; the heartbeat interval
(`WORKER_HEARTBEAT_INTERVAL_SECONDS`, default 30) must stay well below it.

The sender has the analogous mechanism: `recover_stale_deliveries` returns
`sending` deliveries older than `SENDER_LEASE_MINUTES` to `pending`. Because a
send is short, a send interrupted by shutdown is deliberately **not** recorded
as failed — `asyncio.CancelledError` is re-raised and the row is recovered, so a
graceful stop never invents a failure that did not happen.

Sending is retried (unlike generation, which is not): a transient provider
error losing a published article is worse than a duplicate post.

## 12. Docker

`docker-compose.yml` defines four services on one implicit bridge network:

- **postgres** — `postgres:16`, named volume `pg_data` (data survives
  container recreation), healthcheck via `pg_isready`, port 5432 published
  to localhost (required for pgAdmin; not exposed beyond localhost).
- **migrate** — one-shot `scripts/apply_migrations.py`; `api`, `worker` and
  `sender` wait on it with `condition: service_completed_successfully`.
- **api** — built from `Dockerfile`, port 8000, healthcheck against
  `/health`, `depends_on: postgres (service_healthy)`.
- **worker** — same image, runs `python -m app.worker.worker`,
  `restart: unless-stopped`, `depends_on: postgres (service_healthy)`.
- **sender** — same image, runs `python -m app.messaging.worker`,
  `restart: unless-stopped`. Needs `BALE_BOT_TOKEN`; it is a separate service
  so a channel outage or a slow send delay cannot slow down generation.

All configuration comes from environment variables (see `.env.example`);
nothing secret is baked into images. Inside the containers, services reach
PostgreSQL at `postgres:5432` — never `localhost`.

## 13. Testing

`tests/` (run with `python -m pytest -q`):

- **Unit tests** run against a real PostgreSQL test database
  (`irandoc_news_test`) built from the same migrations — no SQLite, no
  `create_all()`: auth, API contract, normalization, classification
  (including `معماری داخلی` vs `معماری`), Worker claim/process/fail/
  recovery, health, and channel delivery (`tests/test_messaging.py`).
- Platform HTTP calls in the delivery tests are always stubbed, so no test
  contacts Bale or Telegram and the suite is offline.
- **Integration/E2E** (`tests/test_e2e.py`) runs against real PostgreSQL:
  submit via the API, then either claim-and-process with a mocked LLM or
  observe the live Worker finish the job. It skips automatically when
  PostgreSQL is unavailable.
- An autouse fixture disposes the shared engine pool after each test
  because pytest-asyncio uses a fresh event loop per test.

## 14. VPS deployment

The same Compose stack runs on an Ubuntu VPS:

```bash
ssh user@your-vps
git clone <repo> && cd project
cp .env.example .env          # edit with real credentials
docker compose up -d
docker compose exec api python scripts/seed_fields.py
```

Local Docker success does not automatically guarantee VPS success: verify
the ports (8000, 5432) are reachable, the volume is mounted, environment
variables are set, and the containers are on the expected network. Use safe
test data only — never real/sensitive thesis data (Assignment 4 security
boundary).

## 15. SSH

Access the VPS with `ssh user@your-vps`. For routine operations prefer a
dedicated, non-root user, key-based authentication, and a firewall that
exposes only the required ports.

## 16. tmux

Use tmux on the VPS so sessions survive disconnection:

```bash
tmux new -s irandoc        # create session
# detach: Ctrl-b d        # reattach: tmux attach -t irandoc
```

Run the API and Worker (or `docker compose logs -f`) in separate tmux
windows/panes instead of background shell jobs you cannot reattach to.

## 17. Troubleshooting

| Symptom | Check |
|---|---|
| `/health` reports `disconnected` | postgres container status, `DATABASE_URL`, network |
| Jobs stuck in `processing` | Worker logs; `WORKER_LEASE_MINUTES` vs heartbeat interval |
| Jobs in `failed` | `main.error_message` (LLM/provider errors) |
| Worker not claiming | queue empty? `docker compose logs worker` |
| Field classified as `other` | check `app/fields/classification.py` keyword rules |
| Container can't reach DB | use `postgres` (not `localhost`) as host inside containers |
| Nothing sent to the channel | is the article `review`? `SELECT * FROM deliveries ORDER BY id DESC` — `pending` with a future `next_attempt_at` is waiting out backoff, `failed` has `error_message` |
| Sends fail immediately | `deliveries.error_message`; a `failed` row with `no bot token configured` means `BALE_BOT_TOKEN` is missing from the `sender` service |
| Article never delivered | `main.field_id IS NULL` → the field has no route and delivery fell back to active channels; check `manage_channels.py list` |

## Engineering decisions (not explicitly required by the assignments)

- `LLM_TIMEOUT_SECONDS` (default 120s): a hung LLM call would otherwise
  block the single Worker forever; the timeout lets the job fail and the
  Worker continue.
- `main.id` is an auto-incrementing Integer (matching the approved ERD sketch).
- The schema extends the approved ERD with `normalized_name`, `source`,
  `is_active` (models/prompts), and `claimed_at`/`heartbeat_at` — all
  required by Assignments 3–4 (stable field identity, auditability, crash
  recovery).
- `table_of_contents` is required by the API input contract (all nine
  fields), while the database column remains nullable to preserve any
  pre-existing rows.
- `/health` intentionally does not report Worker state: the Worker is a
  batch process with no HTTP surface, and the assignments do not require it.
- At-least-once processing on crash (possible duplicate LLM call) instead
  of exactly-once, which would require distributed transactions.
- A dedicated `deliveries` table instead of a `sent` flag on `main`: once a
  field fans out to several channels, one article has several independent send
  states, each needing its own attempts, error and recovery.
- A separate sender service instead of a second loop inside the Worker: the
  inter-send delay and platform outages would otherwise sit on generation's
  critical path.
- Bounded send retries with backoff instead of unlimited retries: a permanently
  broken channel must not stall every article queued behind it. Permanent
  errors (unknown chat, revoked token) fail immediately rather than burning
  attempts that could never succeed.
- Bot tokens in configuration by default, with an optional encrypted per-channel
  token: a channel is a destination, not a credential, so channel rows stay safe
  to read and audit; the exception exists so a second channel can be added from
  the panel without editing the environment.
- The panel records `deleted_from_channel` only after the platform confirms the
  delete, so the row can never claim a message is gone while it is still in the
  channel; a refusal leaves it `sent` and retryable.
- Fan-out delete is per channel, not all-or-nothing: removing one article from
  several channels calls each platform separately and reports the ones that
  refused, because a single uncooperative channel must not strand the rest.
- A platform "message not found" counts as a successful delete, not a refusal.
  The postcondition ("this message is no longer published") already holds, and
  treating it as a failure would leave the row `sent` forever for a message
  that is gone. It is told apart from a genuine refusal (no rights, wrong
  chat) by the provider's own description, since the HTTP status differs
  between Bale (404) and Telegram (400).
- Delivery to EVERY active channel rather than a routed subset: the owner
  redefined the core rule, and the previous chain meant that adding one
  category mapping silently stopped delivery to all the other channels. A
  channel is excluded only by switching it off, which is explicit and
  reversible.
- `awaiting_confirmation` is the gated initial delivery status when
  `REQUIRE_HUMAN_CONFIRMATION=true` (default): queued by the sender but never
  claimed until approval flips it to `pending`. With the flag off, queueing
  writes `pending` directly (pre-gate behaviour).
- At-least-once sending on crash (possible duplicate post in the channel) —
  the same trade-off as generation, and for the same reason.
