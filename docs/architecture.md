# Architecture

## Overview

IranDoc News Generation is a modular monolithic service that receives thesis metadata via HTTP, stores it in PostgreSQL, and processes it sequentially through a single Worker that generates journalistic news using an LLM.

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

## Security

- `shared_Secret` from environment variable only
- Constant-time comparison via `hmac.compare_digest()`
- Never stored in database, logged, or sent to LLM
- AvalAI API key from environment variable only

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
│   └── worker/
│       └── worker.py        # Sequential worker
├── migrations/              # SQL migrations
├── scripts/                 # Seed/migration scripts
├── tests/                   # Automated tests
├── data/                    # fields.xlsx
├── docs/                    # Documentation
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
└── README.md
```
