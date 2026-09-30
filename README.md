# IranDoc News Generation

Automatic journalistic news generation from IranDoc thesis metadata.

## Architecture

```
Client → FastAPI → PostgreSQL (ready) → Worker → LLM → PostgreSQL (review)
```

- **PostgreSQL** is both persistent storage and job queue
- **One sequential Worker** processes jobs one at a time
- **LLM** (PydanticAI + AvalAI) generates news only — never classifies fields
- **No Redis, Celery, Kafka, or Kubernetes**

## Quick Start (Docker Compose)

```bash
cp .env.example .env
# edit .env with your credentials

docker compose up -d
docker compose exec api python scripts/apply_migrations.py
docker compose exec api python scripts/seed_fields.py
```

## API

### POST /submit

```json
{
  "shared_Secret": "your-secret",
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

Returns `{"status": "ok", "database": "connected"}` when healthy.

## Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `DATABASE_URL` | PostgreSQL connection URL | Yes |
| `SHARED_SECRET` | Authentication secret | Yes |
| `AVALAI_API_KEY` | AvalAI API key | Yes |
| `AVALAI_BASE_URL` | AvalAI base URL | Yes |
| `LLM_MODEL` | Model name | Yes |

## Migrations

SQL migrations are in `migrations/`:

1. `001_initial_schema.sql` — creates all tables, constraints, indexes
2. `002_seed_categories.sql` — inserts 13 categories + `other`
3. `003_seed_prompt.sql` — inserts the v1 system prompt
4. `004_seed_model.sql` — inserts the active model configuration

Apply with: `python scripts/apply_migrations.py`

## License

Internal project — TETRA Company.
