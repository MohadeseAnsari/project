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
  │  1. register the field name as a dedup identity (no category, no keyword)
  │  2. ONE PydanticAI Agent call ──▶ AvalAI (OpenAI-compatible)
  │     → GeneratedNews{title, lead, body, categories[≤3]}  (news + ISCED-F 2013 together)
  │  3. post-process categories → main_category_matches (hallucination filter,
  │     clamp 0..1, dedupe, MIN_CATEGORY_RELEVANCE, top TOP_N_CATEGORIES;
  │     empty answer → single 'other' fallback row that never delivers)
  ▼
PostgreSQL (main.status='review', output_json=..., model_id, prompt_id)
  │
Sender ◀──claim deliveries (processed, not yet sent for that channel)
  │  union of qualifying matches' channels (multi-category) ──▶ Bale / Telegram
  │     ──▶ deliveries.status='sent'
  ▼
Channel (throttled: SENDER_SEND_DELAY_SECONDS between sends)
```

Separation of responsibilities (Assignment 2):

| Concern | Owner | Rule |
|---|---|---|
| Authenticate / validate / create job | API | Never calls the LLM |
| Classify thesis | `app/news/` (same combined call) | LLM names, Worker validates + stores |
| Generate news | `app/news/` (PydanticAI + AvalAI) | ONE call returns article + classification |
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
│   │   └── normalization.py  # Unicode NFC, zero-width removal, whitespace collapse
│   ├── news/
│   │   ├── generation.py     # PydanticAI Agent + AvalAI client
│   │   ├── prompts.py        # versioned v2-combined prompt (injection-safe)
│   │   └── isced.py          # canonical 29 ISCED-F 2013 names + fallback
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
│   ├── seed_fields.py        # loads data/fields.xlsx as dedup identities
│   └── auto_route_channels.py # one-time category-channel title matching
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
   - upserts the field (exact normalized match, else insert as
     `source='runtime'` with `category_id = NULL` — dedup identity only) and
     links `field_id`;
   - links the active `model_id`/`prompt_id` (auditability);
   - starts a heartbeat task, makes the ONE combined LLM call, post-processes
     the classification into `main_category_matches`, stores
     `output_json={title, lead, body, categories}`, sets `review`;
   - the classification is stored per record: regenerating replaces the
     matches, a reviewer override replaces them too — history moves only
     through deliberate actions, never a silent re-derivation. The record
     page's دسته‌بندی‌های خودکار tab always shows what is stored, and each
     row records whether the LLM (`model_id` set) or a person (NULL) wrote it.
4. **Fail** — any exception → `fail_job` sets `failed` with the error text
   (truncated to 2000 chars). There is **no automatic retry counter**; failed
   jobs stay failed.
5. **Empty queue** — the Worker waits `WORKER_POLL_INTERVAL_SECONDS` and
   loops; it stays alive. SIGTERM/SIGINT trigger graceful shutdown after the
   current job.

## 5. Database

PostgreSQL is both the persistent store and the queue. Tables (see
`migrations/023_isced_categories.sql` and
`migrations/024_drop_legacy_categories.sql` for the v2-combined vocabulary
changes and `docs/erd.md` for the full picture):

- **categories** — exactly the 29 ISCED-F 2013 intermediate fields + `other`
  are live (`deprecated=FALSE`, with indicative `isc_code`). The retired rule
  vocabulary was remapped onto these rows and deleted by migration 024, so
  no dead vocabulary is left for the model or the router to trip over. The
  `deprecated` flag remains for retiring a category without breaking its FKs.
- **fields** — 533 field names seeded from `data/fields.xlsx` plus
  runtime-discovered names; `normalized_name` is the unique stable identity and
  `source` distinguishes `spreadsheet` vs `runtime`. The table has no category,
  keyword, or channel route; it exists only so repeated discipline names share
  one dedup identity. `main.field_id` points at it, and neither classification
  nor routing reads it.
- **models** — LLM model configuration; `is_active` selects the one in use.
- **prompts** — versioned system prompts; `is_active` selects the one in use
  (migration 023 seeds and activates `v2-combined`).
- **main** — the thesis/news record and queue entry: thesis fields, `status`
  (`ready|processing|review|failed|rejected`, CHECK constraint), `field_id` /
  `category_id` (display primary — the rank-1 match) / `model_id` /
  `prompt_id` audit FKs, `output_json` (JSONB, incl. the model's
  `categories` answer), `error_message`, `claimed_at` / `heartbeat_at`
  (recovery), `created_at` / `updated_at`. Indexed on `(status, created_at)`,
  `field_id`, `category_id`.
- **main_category_matches** — the per-record classification: one row per
  `{category, score 0..1, rank 1..N, reason}` the combined call stored, with
  `model_id = NULL` marking manual overrides. Routing and the panel read this
  table, not `main.category_id` alone.
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
  added with no environment change. `activated_at` (migration 022) is the
  channel's start line: an article is only delivered there if it was created at
  or after that moment, and the value is refreshed every time the channel is
  switched ON. Without it, adding a channel made the sender enqueue the whole
  archive to it — a new channel has no delivery rows for anything, so every
  historical record looked like pending work.
- **category_channel_routes** — **the routing matrix.** A record publishes to
  the union of the channels its qualifying matches name: every qualifying
  `main_category_matches` row contributes that category's rows here. Written
  from the per-category checkbox picker on `/categories`, which replaces the
  whole set rather than appending, and read by the sender, the preview
  endpoint and the panel so every surface agrees.
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

## 8. Classification (LLM, combined)

Since v2-combined there is **no rule-based classifier in the live path**.
Classification of a thesis comes from the SAME LLM call that writes the news:
the prompt embeds the 29 ISCED-F 2013 intermediate fields (canonical list in
`app/news/isced.py`, with exact ZWNJ spelling) plus an `other` fallback, and
the model answers with up to three `{category, score, reason}` entries. The
Worker validates the answer (`app/services/records.py`): names that are not
live `categories` rows are dropped as hallucinations, scores are clamped to
[0,1], duplicates collapse to the highest score, only matches at/above
`MIN_CATEGORY_RELEVANCE` are stored in `main_category_matches` (rank 1..N),
and an empty answer becomes a single `other` row (`score=0.0`, reason
`no auto-match`) that never auto-delivers.

There is no field-level classifier and no field-level routing path. The
`fields` table is only a dedup identity; classification is stored per record
in `main_category_matches` and a reviewer can replace it manually.

## 9. LLM generation

`app/news/generation.py` — PydanticAI `Agent` with
`output_type=GeneratedNews` (`title`, `lead`, `body`, `categories`) over
AvalAI's OpenAI-compatible API (`AsyncOpenAI` client). The system prompt
(`app/news/prompts.py`) frames thesis metadata as **data, not instructions**
(to prevent prompt injection from thesis content), forbids unsupported
additions, promotional language, and unnecessary repetition, and embeds the
numbered ISCED-F 2013 category list with per-category guidance. The client
uses a bounded timeout (`LLM_TIMEOUT_SECONDS`, default 120s — see Engineering
decisions). No secrets are included in its input. The full prompt, its
placeholders and the post-processing contract are documented verbatim in
`docs/llm-prompt.md` and pinned by `tests/test_prompt_contract.py` +
`tests/test_combined_classification.py`.

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
# optional: preload the starter discipline names as dedup identities
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
| Field classified as `other` | `main_category_matches` has a single `other` row (score 0, `no auto-match`) — correct it via the record page's دسته‌بندی‌های خودکار tab or `POST /records/{id}/category-matches` |
| Container can't reach DB | use `postgres` (not `localhost`) as host inside containers |
| Nothing sent to the channel | is the article `review`? `SELECT * FROM deliveries ORDER BY id DESC` — `pending` with a future `next_attempt_at` is waiting out backoff, `failed` has `error_message` |
| Sends fail immediately | `deliveries.error_message`; a `failed` row with `no bot token configured` means `BALE_BOT_TOKEN` is missing from the `sender` service |
| Article never delivered | check that the record has a qualifying `main_category_matches` row and that its category has channel routes on `/categories`; an `other`-only or unrouted record publishes nowhere by design |

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
- Strict, matches-based routing rather than "every active channel": the
  broadcast rule (item 6) was tried and reversed — adding one route silently
  stopped delivery to every other channel, and the archive would flood any new
  channel. The current core rule: a record is delivered to the per-channel
  union of the channels its QUALIFYING `main_category_matches` name (score ≥
  `MIN_CATEGORY_RELEVANCE`, never the `other` fallback), a category with no
  selected channels publishes nothing, and a fallback-only record waits for a
  reviewer. Exclusion is explicit (channel switched off, score under
  threshold, or no route), which is reversible where broadcast silence was
  not.
- `awaiting_confirmation` is the gated initial delivery status when
  `REQUIRE_HUMAN_CONFIRMATION=true`: queued by the sender but never claimed
  until approval flips it to `pending`. With the gate off (the default), queueing
  writes `pending` directly; a record whose only classification is the `other`
  fallback never auto-publishes under any setting.
- At-least-once sending on crash (possible duplicate post in the channel) —
  the same trade-off as generation, and for the same reason.
