# Architecture

## Overview

IranDoc News Generation is a modular monolithic service that receives thesis metadata via HTTP, stores it in PostgreSQL, processes it sequentially through a single Worker that generates journalistic news using an LLM, and publishes finished articles to messaging channels through a second, separate Worker.

See [codebase-guide.md](codebase-guide.md) for the full guide (request/Worker lifecycle, recovery, Docker, VPS, troubleshooting).

## Components

### API (FastAPI)
- Receives POST `/submit` with thesis metadata + shared_Secret
- Authenticates via constant-time secret comparison BEFORE Pydantic validation
- Validates input with Pydantic
- Creates database record with status `ready`
- Returns record ID and status
- GET `/health` — database connectivity check
- GET `/records/{id}` — retrieve record status/output

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
- Resolves channels via `field_channel_routes` (field → channel, one-to-many); falls back to all active channels when a field has no route
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
- Falls back to priority-ordered keyword rules (specific before broad)
- Unknown fields → `other` category
- No LLM involvement

### LLM Generation (PydanticAI + AvalAI)
- Receives thesis metadata as structured data
- Returns GeneratedNews(title, lead, body)
- Prompt stored in database with version tracking
- Model and prompt references recorded on main record
- Bounded timeout (`LLM_TIMEOUT_SECONDS`, default 120s) so a hung call fails the job instead of blocking the Worker forever

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
