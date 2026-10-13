# Architecture

## Overview

IranDoc News Generation is a modular monolithic service that receives thesis metadata via HTTP, stores it in PostgreSQL, processes it sequentially through a single Worker that generates journalistic news using an LLM, and publishes finished articles to messaging channels through a second, separate Worker.

See [codebase-guide.md](codebase-guide.md) for the full guide (request/Worker lifecycle, recovery, Docker, VPS, troubleshooting).

## Components

### API (FastAPI)
- Receives POST `/submit` with thesis metadata + shared_Secret (plus optional `model_id`/`prompt_id` per-record overrides)
- Authenticates via constant-time secret comparison BEFORE Pydantic validation
- Validates input with Pydantic
- Creates database record with status `ready`
- Returns record ID and status
- GET `/health` — database connectivity check
- GET `/records/{id}` — retrieve record status/output plus review metadata and delivery state
- GET `/records` — paginated listing (page/page_size ≤100, filters status/field/university/q/order) for the review panel
- PATCH `/records/{id}` — review decision (approve/reject/save_draft, only from review, 409 otherwise)
- GET `/deliveries` — paginated deliveries (filters status/platform/main_id) plus `POST /deliveries/{id}/retry` (failed only, resets attempts)
- GET `/stats` — record/delivery counts per status plus worker heartbeat freshness (no per-record timings)

### Worker (Sequential, Persistent)
- Polls PostgreSQL for `ready` jobs
- Claims job with `FOR UPDATE SKIP LOCKED`
- Updates heartbeat during processing
- Calls preprocessing → classification → LLM generation
- Stores result as JSONB, sets status to `review`
- On failure: sets status to `failed` with error message
- Recovers stale `processing` jobs on startup and each loop iteration

### Sender Worker (Channel Delivery)
- Polls for processed articles (`main.status = 'review'`) that have no delivery for a routed channel
- Creates one `deliveries` row per (article, channel) — the unit of work
- Resolves channels through the category chain: `category_channel_routes`
  first, then `field_channel_routes` (field-level exceptions), then all
  active channels as fallback; each delivery snapshots `category_id` +
  `route_source` so sends stay explainable
- Renders the message (`app/messaging/formatting.py`): emoji-headed title in bold, lead, body, and the destination channel handle
- Claims one due delivery at a time with `FOR UPDATE SKIP LOCKED`
- Sends through the platform client for the channel (`Bale`, `Telegram`) using the bot token from configuration
- Waits `SENDER_SEND_DELAY_SECONDS` after every attempt so a channel never sees a burst
- Retries retryable failures with exponential backoff up to `SENDER_MAX_ATTEMPTS`, then parks the delivery as `failed`; permanent errors (unknown chat, revoked token) fail immediately
- Recovers abandoned `sending` deliveries after `SENDER_LEASE_MINUTES`
- `awaiting_confirmation` is the gated initial status when
  `REQUIRE_HUMAN_CONFIRMATION=true` (default): the sender never claims it, and
  approval flips it to `pending`. With the flag off, queueing writes `pending`
  directly.
- Runs as its own compose service so channel rate limits and platform outages never affect generation
- Human gate (`REQUIRE_HUMAN_CONFIRMATION`, default true): new deliveries queue as
  `awaiting_confirmation` and only become `pending` on explicit approval; when
  false they queue as `pending` directly (pre-gate behaviour)

### Field Classification (Deterministic)
- Normalizes field names (Unicode NFC, trim, collapse whitespace)
- Looks up exact normalized match in database
- Falls back to token-aware longest-match keyword rules: multi-token
  phrases match exactly, ≤4-char keywords match whole tokens only (so `مو`
  never fires inside `موزه`), longer keywords may substring-match at lower
  score; the longest match wins and category order breaks ties only
- Negative keywords veto genuine collisions (documented in code)
- Every decision returns `(category, matched_keyword, rule)` — the panel
  shows WHY, and `fields.matched_keyword` stores it
- Unknown fields → `other` category (reviewed names only; see report)
- No LLM involvement

### LLM Generation (PydanticAI + AvalAI)
- Receives thesis metadata as structured data
- Returns GeneratedNews(title, lead, body)
- Resolves model+prompt per job: per-record override (`main.model_id`/`prompt_id` from `POST /submit`) → active row (`is_active`) → config fallback (`settings.llm_model`, packaged prompt)
- Per-model `base_url`/`api_key_env` (env var name, never the key) override the provider endpoint/key
- Models/prompts managed via `/models` + `/prompts` (list/create/detail, single-active `POST /{id}/activate`); prompts immutable (new version = new row, never edit); `POST /models/{id}/test-connection` probes with a 1-token call without returning the key
- Model and prompt references recorded on main record
- Model configuration lives in the database, secrets live only in the
  environment: a row stores the variable NAME (`api_key_env`), never a key.
  A row with its own `base_url` but no usable key fails loudly
  (`MissingModelKeyError` → `main.error_message`) instead of borrowing the
  global key; the global key applies only to rows sharing the configured
  endpoint

### What is sent to the LLM (exact structure)
  the active `prompts` row, else the packaged fallback in
  `app/news/prompts.py`. Thesis content is framed as DATA inside it, never
  instructions.
- **User message** (built by `build_user_message`, shared with the panel
  preview so both are byte-identical): the literal template
  `Thesis metadata:\ntitle: {title}\nabstract: {abstract}\nfield: {field}\n
  university: {university}\ntable_of_contents: {table_of_contents}\n
  author: {author}\n supervisor: {supervisor}` with the record's seven
  metadata values; missing values render empty.
- **Model / endpoint**: the resolved model name and `base_url` (row override
  else configuration). `temperature` / `max_output_tokens` are stored per
  model row and shown in the panel, but the provider call currently uses
  provider defaults for them — see the panel note where they are displayed.
- **Required output shape**: strict JSON `{title, lead, body}` validated by
  Pydantic (`GeneratedNews`); anything else fails the job with the error.
- **Preview**: the record page's `پرامپت نهایی` tab renders the exact system
  prompt + user message + resolved model name for that record. No secret is
  ever included in either message or shown in the preview.
- **Resolution order (implemented, tested per step)**: per-record override
  (`main.model_id`/`prompt_id`) → active DB row (`is_active`) → packaged
  fallback (`settings.llm_model`, `SYSTEM_PROMPT`); `base_url` from the
  endpoint-owning row else configuration; API key from that row's
  `api_key_env`, else the platform-wide key — except a row with its own
  `base_url` and no usable key fails loudly instead of substituting.
- Bounded timeout (`LLM_TIMEOUT_SECONDS`, default 120s) so a hung call fails the job instead of blocking the Worker forever

## Layering (browser → routes → services → ORM → PostgreSQL)

- The browser never talks to PostgreSQL directly: every page is rendered
  server-side, and HTMX partials hit panel routes that query through services.
- `app/services/` holds one module per domain (`records`, `deliveries`,
  `models`, `prompts`, `channels`, `routing`, `fields`, `users`, `audit`,
  `passwords`, `transitions`): every shared query and state transition lives
  there exactly once. Both the panel routes and the machine API routes call
  the same functions; no route contains ad-hoc SQL.
- **The panel is a client of the backend, enforced by tests.** No module under
  `app/panel/` may import `app/db/connection.py` (`async_session`, `engine`)
  or any ORM model; routes receive one session per request from
  `app.services.session_dep` and hand it to services.
  `tests/test_panel_layering.py` fails on a single violation (AST-based, so
  even a function-local import is caught) and additionally asserts that every
  registered panel route calls at least one service function.
- Services raise plain `ValueError`s with stable codes (`not-in-review`,
  `duplicate-username`, `not-retryable`, `weak-password`, …); routes map them
  to HTTP statuses and Persian messages. Nothing reaches the browser as a
  stack trace. The mapping lives in `app/services/errors.py`
  (`ERROR_MESSAGES`, `status_for`, `message_for`, `from_value_error`), so the
  same code always yields the same status and the same Persian text.
- Authorization is one module: `app/panel/authz.py` holds the `Action` enum
  and the `ROLE_ACTIONS` table; routes call `_require_role(user, Action.X)`
  and map `PermissionDenied` to 403. `tests/test_authz_matrix.py` pins the
  role × action matrix, fails when a POST route has no guard, and exercises
  the whole thing over HTTP.
- Password hashing lives in `app/services/passwords.py` and is called by the
  users service, not by routes: plaintext enters `create_user_with_password`,
  `reset_password` or `change_own_password`, and only the argon2 hash is
  stored.
- Services never render and never audit with a UI identity: routes pass the
  acting `user_id` to `audit_service.record_event`.

## State machine (one place, always reflected in the UI)

`app/services/transitions.py` defines the allowed transitions for both
lifecycles and raises `InvalidTransition` for anything else; the Workers, the
machine API and the panel all go through it, and **no route sets a status by
hand** (diagram in [erd.md](erd.md)).

Records: `ready → processing → review → approved → published`, plus `rejected`
and `failed`. Deliveries: `awaiting_confirmation → pending → sending → sent`,
plus `failed` and `deleted_from_channel`.

Every stuck state has an explicit recovery action in the panel: regenerate
(record), cancel / retry / release / delete-from-channel (delivery),
reassign category (record). `records.record_timeline` renders the resulting
flow per record — submitted → field/category (with the matched keyword) →
generated with `<model>/<prompt>` → reviewed by `<user>` → delivered to
`<channel>` with the message id — from real columns and audit rows only.

What the operator sees follows from those states, not from timestamps:

- approving moves the record to `approved`, so it leaves the "needs review"
  list and KPI immediately and shows who approved it and when;
- the first successful send moves it to `published` (the fix for records that
  sat in "نیازمند تأیید" while their deliveries were already sent);
- rejecting shows `rejected`; the needs-review KPI is a plain count of
  `status='review'`, so it is exact by construction;
- `review`, `approved` and `published` records still queue a delivery for a
  channel routed *later*, created directly as `pending` because the human
  decision already happened.
- The sender re-reads the record status **at insert time** (not only in its
  queueing `SELECT`): if a reviewer approves in between, the new delivery is
  created `pending` instead of `awaiting_confirmation`. Without this the row
  would be parked forever — the record had left `review`, so no second
  approval could ever release it.

## CSRF

Every panel POST form embeds a session-bound CSRF token
(`app/panel/csrf.py`), validated by the `verify_csrf` FastAPI dependency on
`panel_app`: Origin/Host always, plus the token outside the documented
`/login` + `/logout` exemptions. Failures render a Persian 400 page. It is a
dependency rather than `@app.middleware("http")` on purpose — a middleware
reads the form on a *different* `Request` object and consumes the ASGI
stream, after which the endpoint's `Form(...)` parameters arrive empty.
Session cookies are `HttpOnly` + `SameSite=Lax` (`Secure` behind TLS).

## Database

PostgreSQL serves as both persistent storage and job queue. See [erd.md](erd.md) for the full schema.

## Worker Recovery

- `claimed_at`: timestamp when worker claimed the job
- `heartbeat_at`: updated before LLM call
- Recovery: jobs with `status='processing'` and `heartbeat_at < now() - WORKER_LEASE_MINUTES` are reset to `ready`
- Configurable via `WORKER_LEASE_MINUTES` environment variable
- No automatic retry count

Generation is *not* retried, but sending *is*: a transient provider failure
losing a published article is worse than a duplicate post.

## Sender Recovery

- `deliveries.claimed_at`: timestamp when the sender claimed the delivery
- Recovery: deliveries with `status='sending'` and `claimed_at < now() - SENDER_LEASE_MINUTES` return to `pending`
- `deliveries.attempts` bounds retries: `SENDER_MAX_ATTEMPTS` with exponential backoff from `SENDER_BACKOFF_BASE_SECONDS`, then `failed`
- `deliveries.next_attempt_at` is what makes backoff work — a failed delivery is parked in the future instead of retried immediately

## Security

- `shared_Secret` from environment variable only
- Constant-time comparison via `hmac.compare_digest()`
- Never stored in database, logged, or sent to LLM
- AvalAI API key from environment variable only
- Bot tokens (`BALE_BOT_TOKEN`, `TELEGRAM_BOT_TOKEN`) from environment only; a `channels` row is a destination, never a credential

## Project Structure

```
project/
├── app/
│   ├── main.py              # FastAPI app factory
│   ├── config.py            # Settings from env
│   ├── db/
│   │   ├── connection.py    # Async engine + session
│   │   └── models.py        # SQLAlchemy ORM models
│   ├── schemas/
│   │   └── news.py          # Pydantic models
│   ├── api/
│   │   └── routes.py        # HTTP endpoints
│   ├── auth/
│   │   └── secret.py        # Secret verification
│   ├── fields/
│   │   ├── normalization.py # Field normalization
│   │   └── classification.py# Deterministic classification
│   ├── news/
│   │   ├── generation.py    # LLM client
│   │   └── prompts.py       # Prompt template
│   ├── worker/
│   │   └── worker.py        # Sequential worker
│   └── messaging/
│       ├── formatting.py    # Article → channel message
│       ├── platforms.py     # Bale + Telegram clients
│       ├── dispatcher.py    # Delivery queue + retry policy
│       └── worker.py        # Sender worker
├── migrations/              # SQL migrations
├── scripts/                 # Seed/migration/channel scripts
├── tests/                   # Automated tests
├── data/                    # fields.xlsx
├── docs/                    # Documentation
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
└── README.md
```
