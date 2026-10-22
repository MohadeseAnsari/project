# Architecture

## Overview

IranDoc News Generation is a modular monolithic service that receives thesis metadata via HTTP, stores it in PostgreSQL, processes it sequentially through a single Worker that makes ONE LLM call per thesis — returning the journalistic article AND its ISCED-F 2013 classification together — and publishes finished articles to messaging channels through a second, separate Worker.

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
- GET `/records/{id}/classification` — the record's stored category matches (category, score, rank, reason, primary flag) as the combined call / reviewers wrote them
- POST `/records/{id}/category-matches` — reviewer override: replaces ALL stored matches with the submitted list (unknown category or invalid score → 422; an empty list re-stores the `other` fallback so a record is never left unclassified)

### Worker (Sequential, Persistent)
- Polls PostgreSQL for `ready` jobs
- Claims job with `FOR UPDATE SKIP LOCKED`
- Updates heartbeat during processing
- Registers the field name for dedup (`fields`, WITHOUT classifying it), then makes ONE structured LLM call: news + ISCED-F 2013 `categories` in the same response
- Post-processes the classification (hallucination filter against the live non-deprecated `categories` rows, score clamp to [0,1], dedupe keep-highest, threshold `MIN_CATEGORY_RELEVANCE`, trim to `TOP_N_CATEGORIES`) and stores every qualifying match in `main_category_matches` (ranked 1..N); sets `main.category_id` to the top match — or to the single `other` fallback row (score 0, rank 1, reason `no auto-match`) when nothing qualifies
- Stores the article as JSONB, sets status to `review`
- On failure (including LLM/schema errors): sets status to `failed` with error message
- Recovers stale `processing` jobs on startup and each loop iteration

### Sender Worker (Channel Delivery)
- Polls for processed articles (`main.status = 'review'`) that have no delivery for an active channel
- Creates one `deliveries` row per (article, channel) — the unit of work
- Resolves channels for an article: **MULTI-category** — every qualifying
  row in `main_category_matches` (score >= `MIN_CATEGORY_RELEVANCE`, live
  category, never the `other` fallback) fans the article out to that
  category's channels (`category_channel_routes`), deduped per channel. The
  union is the whole answer: a channel reached by two of the record's
  categories gets ONE delivery (source recorded as `multi`); a record whose
  only answer is `other` qualifies for nothing and queues **no deliveries at
  all** — it waits for a reviewer (`other` is excluded from qualifying even
  when the threshold is set to 0). A category with no channels selected goes
  **nowhere** — there is no fallback to "every active channel", because a
  fallback would make the panel's matrix a decoration the owner watches
  change while nothing happens. The matrix is edited on `/categories`. Each
  delivery snapshots the TOP-1 match's `category_id` (audit-only limitation
  in the multi-category case) and records `route_source='category'`.
- **A channel only receives news from the moment it was switched on.** The
  filter is `channels.activated_at <= main.created_at`, and `activated_at` moves
  forward every time a channel is switched ON. Without it, adding a channel made
  the sender enqueue the entire archive to it — a brand new channel has no
  delivery rows for anything, so every historical record looked like pending
  work, forever. The same floor is expressed twice on purpose: in
  `channels_for_article_multi()` for the rows that get created, and in
  `_has_undelivered_channel()` for the batch query that finds the articles.
  They must agree or news is silently dropped or silently flooded.
- Renders the message (`app/messaging/formatting.py`): emoji-headed title in bold, lead, body, and the destination channel handle
- Claims one due delivery at a time with `FOR UPDATE SKIP LOCKED`
- Sends through the platform client for the channel (`Bale`, `Telegram`) using the bot token from configuration
- Waits `SENDER_SEND_DELAY_SECONDS` after every attempt so a channel never sees a burst
- Retries retryable failures with exponential backoff up to `SENDER_MAX_ATTEMPTS`, then parks the delivery as `failed`; permanent errors (unknown chat, revoked token) fail immediately
- Recovers abandoned `sending` deliveries after `SENDER_LEASE_MINUTES`
- `awaiting_confirmation` is the gated initial status when
  `REQUIRE_HUMAN_CONFIRMATION=true` AND `AUTO_PUBLISH_ON_CLASSIFICATION=false`
  (both defaults): the sender never claims it, and approval flips it to
  `pending`. With the flag off, or auto-publish on classification enabled,
  queueing writes `pending` directly — except that a record whose only
  classification is the `other` fallback never auto-publishes under any
  setting, because nothing qualified.
- Runs as its own compose service so channel rate limits and platform outages never affect generation

### Classification (LLM, combined with generation)

One call, one answer, one point of trust:

- Classification of a thesis comes from the SAME LLM call that writes the news
  (v3-compact): the model answers with up to 3
  `{code 0-29, score 0-100, reason}` entries next to the article; the worker
  maps codes back to ISCED-F 2013 category rows (compact output, ~75 % fewer
  classification tokens).
- The vocabulary is the 29 ISCED-F 2013 intermediate fields + the `other`
  fallback, defined once in `app/news/isced.py` and pinned char-for-char
  (ZWNJ included) against the live `categories` rows.
- The Worker post-processes the answer and is the only point of trust: names
  that are not live (`deprecated = FALSE`) `categories` rows are dropped as
  hallucinations, scores are clamped to [0,1], duplicates collapse to the
  highest score, and only matches at/above `MIN_CATEGORY_RELEVANCE` — never
  `other` — are stored in `main_category_matches`, ranked 1..N by score
  (`main.category_id` becomes the display primary).
- An empty answer stores exactly one `other` row (score 0.0, rank 1, reason
  `no auto-match`) so "unclassified" is always distinguishable from "not yet
  generated", and such a record queues no deliveries.
- Regeneration and the reviewer's manual assignment (panel «تعیین دستی دسته»,
  API `POST /records/{id}/category-matches`) REPLACE the stored rows wholesale
  (manual rows score 1.0, reason «تعیین دستی توسط بازبین»), so the routing
  answer is always exactly one generation or one human decision old.
- Classification is queried per record via `GET /records/{id}/classification`
  and counted by primary category in `GET /stats` (`records_by_category`).
- `fields.category_id` / `matched_keyword` are legacy metadata: new field rows
  are inserted with NULLs (dedup identity only), and
  `app/fields/classification.py` is retired to reference status (kept for
  `scripts/reclassify_fields.py` audits of historical rows).
- No deterministic rules anywhere in the live path.

### LLM Generation (PydanticAI + AvalAI)
- Receives thesis metadata as structured data
- Returns `GeneratedNews(title, lead, body, categories)` — the article AND its
  ISCED-F 2013 classification in one structured reply
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
- **Required output shape**: strict JSON `{title, lead, body, categories}`
  validated by Pydantic (`GeneratedNews`), where `categories` is a list of
  at most 3 `{code, score, reason}` (code 0-29, score 0-100, reason <= 5
  words); codes not present in the live `categories` table are dropped as
  hallucinations and anything else fails the job with the error.
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
(record, which rewrites the classification with the new answer), cancel /
retry / release / delete-from-channel (delivery), reassign category (record —
replaces the stored matches with one manual match, score 1.0). The record
page's دسته‌بندی‌های خودکار tab shows every stored match (score, rank, reason,
primary badge) plus the fallback warning when a record classified only as
`other`. `records.record_timeline` renders the resulting flow per record —
submitted → classified (primary category) → generated with
`<model>/<prompt>` → reviewed by `<user>` → delivered to `<channel>` with the
message id — from real columns and audit rows only.

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

## Credential policy

Provider keys and bot tokens are managed **in the panel** and stored in the
database **encrypted**; no secret lives in an environment variable or a
plaintext column any more.

- `models.api_key_encrypted` and `channels.bot_token_encrypted` hold Fernet
  ciphertext (`app/credentials.py`). `models.api_model` holds the provider's
  real model id when it differs from the display name.
- **The master key is never in the database.** It comes from
  `CREDENTIALS_KEY`; when unset it is generated once on first boot into
  `data/.credentials_key`, mode `0600`, gitignored. All four services mount the
  same `credentials_data` volume — one key the panel cannot read would break
  credential management. A warning names the file so the operator backs it up:
  without it every stored credential is undecryptable.
- **Write-only in the panel and the API.** The key field is a masked password
  input: an empty submit keeps the stored value, and a separate
  "پاک کردن کلید" action removes it. No response, page, audit row or log
  contains a key value — `tests/test_credentials.py` asserts each surface.
- **One resolution order** (`app/credentials_resolve.py`), used by the Worker,
  the panel regenerate action, the connection probe and the machine API:
  `base_url` row → global; `api_model` row → row name → global; key = row's
  decrypted key → `api_key_env` → global.
- **A row that defines its own `base_url` with no usable key FAILS LOUDLY**
  (`MissingModelKeyError`) instead of borrowing the global key. This is the
  fix for the HTTP 401 seen when a second provider was added: the row now
  gets its own credential, and until it does, generation fails with a clear
  message instead of sending the wrong provider's key to the new endpoint.
- Every probe records `last_tested_at`, `last_test_ok`, `last_test_error`
  (endpoint + HTTP status, never the key) and latency. The panel's status
  column reads those columns, so it is recorded data rather than a claim.

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
- Bot tokens: the platform-wide `BALE_BOT_TOKEN` / `TELEGRAM_BOT_TOKEN` from
  the environment, **or** a per-channel `channels.bot_token_encrypted`
  (Fernet, written from the panel form, write-only and never rendered back).
  A channel's own token wins over the platform-wide one, so a channel can be
  added and used with no environment change. Deleting a published message uses
  the same token that sent it.

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
│   │   ├── normalization.py # Field normalization (dedup identity)
│   │   └── classification.py# RETIRED rule engine (reference only)
│   ├── news/
│   │   ├── generation.py    # Combined LLM call (news + classification)
│   │   ├── prompts.py       # v3-compact prompt template
│   │   └── isced.py         # Canonical 29 ISCED-F 2013 names + fallback
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

## Auto-publish mode

When `REQUIRE_HUMAN_CONFIRMATION=false`, news is auto-published to the matched
categories' channels without human review. The LLM's ISCED-F 2013 classification
drives routing (multi-category union), so an article reaches every channel whose
category was matched.

### Auto-routing helper

`scripts/auto_route_channels.py` maps existing channels to categories by matching
the channel's title (or username as fallback) to `categories.name` (normalized).
Run once after creating channels:

    python scripts/auto_route_channels.py           # preview
    python scripts/auto_route_channels.py --apply   # write

It is idempotent (ON CONFLICT DO NOTHING) and exits 1 if any channel has no
matching category, so CI can flag it. The "other" category is never auto-routed.

### Unmatched articles

`unmatched_category_behavior` (review | fallback_other | failed) controls what
happens when no category reaches `MIN_CATEGORY_RELEVANCE`:
- review (default): article stays in review, visible via the "needs-classification" filter.
- fallback_other: enqueue deliveries for the "other" category's channels.
- failed: move to failed with a clear error message.

### Recovery (mis-classification)

With auto-publish on, mis-classification causes immediate wrong publication.
Recovery: delete the message from the wrong channel (explicit operator action),
reassign the category in the panel (which replaces `main_category_matches`),
then click "Re-publish to correct channels" to queue deliveries for the correct
channels. Re-publish never deletes messages.

### Trade-offs

No pre-publish review; every article is either published, in review (visible
via filter), or failed. Setting `REQUIRE_HUMAN_CONFIRMATION=true` fully restores
the manual gate.
