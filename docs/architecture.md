# Architecture

## Overview

IranDoc News Generation is a modular monolithic service that receives thesis metadata via HTTP, stores it in PostgreSQL, and processes it sequentially through a single Worker that generates journalistic news using an LLM.

## Components

### API (FastAPI)
- Receives POST `/submit` with thesis metadata + shared_Secret
- Authenticates via constant-time secret comparison BEFORE Pydantic validation
- Validates input with Pydantic
- Creates database record with status `ready`
- Returns record ID and status

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

## Database

PostgreSQL serves as both persistent storage and job queue. See [erd.md](erd.md) for the full schema.
