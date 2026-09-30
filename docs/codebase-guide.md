# Codebase Guide

A junior-developer guide to the IranDoc News Generation service.

## 1. Overall architecture

The service is a **modular monolith**: one Python codebase, one deployable
application, three runtime components (PostgreSQL, API, Worker).

```
Client ──POST /submit──▶ FastAPI ──INSERT──▶ PostgreSQL (main.status='ready')
                                                     │
Worker ◀──claim (FOR UPDATE SKIP LOCKED, FIFO)──────┘
  │  1. normalize + rule-based classify field (deterministic, no LLM)
  │  2. PydanticAI Agent ──▶ AvalAI (OpenAI-compatible) ──▶ {title, lead, body}
  ▼
PostgreSQL (main.status='review', output_json=..., model_id, prompt_id)
```

## 2. Project structure

```
project/
├── app/
│   ├── main.py               # FastAPI app, lifespan
│   ├── config.py             # Settings from env
│   ├── db/                   # connection, models
│   ├── schemas/news.py       # Pydantic models
│   ├── api/                  # routes, limits
│   ├── auth/secret.py        # Secret verification
│   ├── fields/               # normalization, classification
│   ├── news/                 # generation, prompts
│   └── worker/worker.py      # Sequential worker
├── migrations/               # SQL migrations
├── scripts/                  # Seed/migration scripts
├── tests/                    # Automated tests
├── data/fields.xlsx          # 533 source field names
├── docs/                     # Documentation
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── .env.example
```

## 3. Request lifecycle (API)

`POST /submit` in `app/api/routes.py`:

1. **Parse JSON** — malformed bodies get `400`.
2. **Authenticate** — `shared_Secret` is compared with the environment
   configured secret using `hmac.compare_digest` (constant time). Missing or
   wrong secret → `401`. **Authentication happens before validation**.
3. **Validate** — Pydantic `ThesisInput` enforces the nine-field contract.
4. **Store** — a `main` row is inserted with `status='ready'`.
5. **No LLM** — the request path never calls the LLM.

## 4. Worker lifecycle

`app/worker/worker.py` runs a persistent loop (`run_worker`):

1. **Recover** — `recover_stale_jobs` resets `processing` jobs whose
   `heartbeat_at` is older than `WORKER_LEASE_MINUTES` back to `ready`.
2. **Claim** — `claim_next_job` selects the oldest `ready` job
   (`ORDER BY created_at`, FIFO) with `FOR UPDATE SKIP LOCKED`.
3. **Process** — `process_job`: classify, generate news, store the result.
4. **Fail** — any exception → `fail_job` sets `failed` with the error text.
5. **Empty queue** — the Worker waits `WORKER_POLL_INTERVAL_SECONDS` and loops.

## 5. Database

PostgreSQL is both the persistent store and the queue. See [erd.md](erd.md).

## 6. Field normalization

`app/fields/normalization.py` — Unicode NFC, zero-width removal, trim,
whitespace collapse. The result is the field's stable identity.

## 7. Classification

`app/fields/classification.py` — deterministic, rule-based, never the LLM.
Specific keywords before broad ones; unknown fields fall back to `other`.

## 8. LLM generation

`app/news/generation.py` — PydanticAI Agent with `output_type=GeneratedNews`
over AvalAI's OpenAI-compatible API. The LLM is never used for classification.
