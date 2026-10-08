# Supervisor Demo Guide (Assignment 4)

A reproducible end-to-end demonstration of the IranDoc News Generation service.
All commands below are valid for the current project.

## 1. Start the services

```bash
docker compose up -d
docker compose ps
```

Expected: `postgres`, `api`, `worker`, `sender`, and `web` containers running.
The review panel (`web`, same image, `PANEL_PORT`, default `8081`) is at
**http://YOUR_HOST:8081/login** — see `docs/admin-panel.md`. It shows counts;
the client demo below must not show processing times or record counts.

## 2. Check API/database health

```bash
curl.exe http://localhost:8000/health
```

Expected: `{"status":"ok","database":"connected"}`.

## 3. Open Swagger

Open **http://localhost:8000/docs** in a browser. Swagger UI is the real
interactive API client: click **Try it out**, edit the request, click
**Execute**, and the browser sends a real HTTP request to the running server and
shows the real response.

## 4. Submit a valid thesis

Via `POST /submit` (Swagger or curl), with all nine required fields:

```json
{
  "shared_Secret": "REPLACE_WITH_REAL_SECRET",
  "username": "demo_user",
  "title": "یادگیری ماشین برای تشخیص پزشکی",
  "abstract": "این پایان‌نامه روش‌های یادگیری ماشین برای تشخیص زودهن بیماری را بررسی می‌کند.",
  "field": "هوش مصنوعی",
  "university": "دانشگاه صنعتی شریف",
  "table_of_contents": "1. مقدمه\n2. روش\n3. نتایج",
  "author": "نام نویسنده",
  "supervisor": "نام استاد راهنما"
}
```

Expected response: `{"id": 1, "status": "ready"}` — the record enters the
PostgreSQL-backed queue.

## 5. Watch the Worker process the job

```bash
docker compose logs --tail=100 worker
```

The Worker claims the job (`processing`), normalizes and classifies the field,
calls the LLM, and stores the result.

## 6. See the generated news

```bash
curl.exe "http://localhost:8000/records/1?shared_Secret=REPLACE_WITH_REAL_SECRET"
```

Expected: `status: "review"` with `output_json` containing the generated
`title`, `lead`, and `body`.

## 7. Demonstrate unknown-field behavior

Submit a thesis with a field that is **not** in the Excel dataset (e.g. a made-up
field). The Worker inserts it into the `fields` table with `source='runtime'`
and a stable normalized identity, classified into a category (or `other`).

## 8. Demonstrate duplicate prevention

Submit the **same** unknown field again. It is reused — no duplicate `fields` row
is created — because matching is on the unique `normalized_name`.

## 9. Demonstrate failure and recovery

- A job that fails (e.g. LLM error) is marked `failed` with the error message.
- A job left in `processing` by a crashed Worker is reset to `ready` after
  `WORKER_LEASE_MINUTES` and reprocessed (at-least-once).

## 10. Inspect the database

```bash
docker compose exec postgres psql -U postgres -d irandoc_news
```

Example queries (real columns from the current schema):

```sql
-- Recent job records
SELECT id, status, field, created_at FROM main ORDER BY id DESC LIMIT 10;

-- Count jobs by status
SELECT status, count(*) FROM main GROUP BY status;

-- Recently registered fields
SELECT id, name, source FROM fields ORDER BY id DESC LIMIT 10;

-- A finished record with its generated news
SELECT id, status, field, output_json FROM main
WHERE status = 'review' ORDER BY id DESC LIMIT 5;
```

## Assignment 4 demo questions

- **What data is accepted and retained?** The nine input fields are validated
  and stored in `main`; the shared secret is checked but never stored.
- **Why is the Shared Secret checked before content validation?** So an
  unauthenticated request never reaches thesis validation and never creates a
  record.
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
  the fix must be systematic.
- **Why one persistent sequential Worker?** At this scale PostgreSQL is the
  queue; a single Worker keeps processing ordered and the architecture simple.
- **How does queue order work?** `ORDER BY created_at` (oldest first) with
  `FOR UPDATE SKIP LOCKED`.
- **What happens when the queue is empty?** The Worker waits
  `WORKER_POLL_INTERVAL_SECONDS` and keeps looping; it stays alive.
- **What do the statuses mean?** `ready` → `processing` → `review` | `failed`.
- **How is stale processing recovered?** Jobs with `heartbeat_at` older than
  `WORKER_LEASE_MINUTES` are reset to `ready`.
- **Model failure vs Worker crash.** A model/LLM error → `failed`. A Worker
  crash mid-job → the job stays `processing` → recovered to `ready`.
- **What if the model response arrives but the DB save fails?** The job stays
  `processing`; recovery resets it to `ready` and it is reprocessed.
- **Why at-least-once?** Crash recovery reprocesses the job, so a duplicate LLM
  call is possible.
- **Why local Docker success ≠ VPS success.** Ports, volumes, environment
  variables, and networking must be verified on the VPS.
- **Docker image/container/volume/network.** Images are built from
  `Dockerfile`; containers run from images; `pg_data` is a named volume for
  persistence; services communicate on the default bridge network.
- **Why `localhost` differs inside a container.** Inside a container,
  `localhost` is that container; services reach each other by service name
  (`postgres`).
- **Why PostgreSQL does not need a public port internally.** The API/Worker
  reach it at `postgres:5432` on the internal network.
- **How do secrets reach the app?** Via environment variables from `.env`
  (git-ignored), never baked into images.
- **Health vs logs vs metrics.** `/health` checks API + database connectivity;
  logs give operational visibility; Prometheus/Grafana are not required.
