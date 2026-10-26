# Entity Relationship Diagram

```mermaid
erDiagram
    categories ||--o{ main : "categorized_as"
    fields ||--o{ main : "selected_for"
    models ||--o{ main : "generated_by"
    prompts ||--o{ main : "generated_with"
    main ||--o{ main_category_matches : "classified_as"
    categories ||--o{ main_category_matches : "vocabulary"
    categories ||--o{ category_channel_routes : "routed_to"
    channels ||--o{ category_channel_routes : "receives"
    channels ||--o{ deliveries : "target_of"
    main ||--o{ deliveries : "published_as"
    categories ||--o{ deliveries : "explains"
    roles ||--o{ panel_users : "grants"
    panel_users ||--o{ audit_logs : "performed_by"
    main ||--o{ audit_logs : "acted_on"

    categories {
        SERIAL id PK
        VARCHAR name UK
        TEXT description
        BOOLEAN deprecated
        VARCHAR isc_code
        INTEGER short_code
        TIMESTAMP created_at
    }
    # deprecated is retained for retiring a row without breaking its FKs, but
    # the pre-ISCED rule vocabulary is gone: migration 024 remapped its fields,
    # records and deliveries onto the closest live rows, then deleted it. The
    # 29 ISCED-F 2013 intermediate fields + other stay live; isc_code is
    # indicative metadata only (not used for routing, nullable by design).

    fields {
        SERIAL id PK
        VARCHAR name UK
        VARCHAR normalized_name UK
        VARCHAR source
        TIMESTAMP created_at
    }
    # fields is only a dedup identity for a discipline name. It has no
    # category, keyword, or channel route. main.field_id references it, but
    # neither classification nor routing reads the field row.

    models {
        SERIAL id PK
        VARCHAR name UK
        VARCHAR provider
        VARCHAR version
        BOOLEAN is_active
        VARCHAR base_url
        VARCHAR api_key_env
        FLOAT temperature
        INTEGER max_output_tokens
        TEXT description
        TIMESTAMP created_at
    }

    prompts {
        SERIAL id PK
        VARCHAR version UK
        TEXT system_prompt
        BOOLEAN is_active
        TIMESTAMP created_at
    }

    main {
        SERIAL id PK
        VARCHAR username
        TEXT title
        TEXT abstract
        VARCHAR field
        VARCHAR university
        TEXT table_of_contents
        VARCHAR author
        VARCHAR supervisor
        VARCHAR status
        INTEGER field_id FK
        INTEGER category_id FK
        INTEGER model_id FK
        INTEGER prompt_id FK
        JSONB output_json
        TEXT error_message
        TIMESTAMP claimed_at
        TIMESTAMP heartbeat_at
        TIMESTAMP approved_at
        TIMESTAMP published_at
        TIMESTAMP created_at
        TIMESTAMP updated_at
    }
    # main.category_id is the DISPLAY primary (the rank-1 match's category);
    # the full multi-category answer lives in main_category_matches and is
    # what routing actually reads.

    main_category_matches {
        SERIAL id PK
        INTEGER main_id FK
        INTEGER category_id FK
        FLOAT score
        INTEGER rank
        TEXT reason
        INTEGER model_id FK
        TIMESTAMP created_at
    }
    # One row per (category, score, rank) the combined generation call — or a
    # reviewer — classified a record into. Promotion rules:
    #   - rank 1 is the primary (highest score; main.category_id mirrors it);
    #   - ck_main_cat_matches_score pins score to 0..1;
    #   - a review override ('POST /records/{id}/category-matches' or the
    #     panel's «تعیین دستی دسته») REPLACES all rows: one manual match with
    #     score 1.0 and reason «تعیین دستی توسط بازبین», or the single other
    #     fallback (score 0.0, reason 'no auto-match') when cleared;
    #   - model_id is NULL on manual/override rows, so an LLM answer is never
    #     mistaken for a human decision;
    #   - routing reads ONLY qualifying rows: score >=
    #     settings.min_category_relevance, category NOT deprecated, category
    #     != 'other'. An other-only record has one row and sends nothing.
    # Indexes: (main_id) for the record page, (score) for threshold queries.

    roles {
        SERIAL id PK
        VARCHAR name UK
        TEXT description
        TIMESTAMP created_at
    }

    panel_users {
        SERIAL id PK
        VARCHAR username UK
        TEXT password_hash
        INTEGER role_id FK
        BOOLEAN is_active
        TIMESTAMP deleted_at
        TIMESTAMP created_at
    }
    # deleted_at tombstones a removed console account (migration 020): the row
    # is kept so audit_logs.user_id keeps resolving the author, but every
    # panel listing and the login path filter it out.

    audit_logs {
        SERIAL id PK
        INTEGER user_id FK
        VARCHAR action
        INTEGER main_id FK
        TEXT detail
        TIMESTAMP created_at
    }

    channels {
        SERIAL id PK
        VARCHAR platform
        BIGINT chat_id
        VARCHAR username
        VARCHAR title
        VARCHAR bot_token_env
        TEXT bot_token_encrypted
        BOOLEAN is_active
        TIMESTAMP created_at
        TIMESTAMP activated_at
    }
    # Many channels coexist; the pair (platform, chat_id) is unique
    # (uq_channels_platform_chat) so one destination is never registered
    # twice. bot_token_encrypted is a per-channel Fernet token, write-only.
    # Deleting is not unconditional: a channel with deliveries is disabled.
    #
    # activated_at is the channel's start line: an article is delivered here
    # only if main.created_at >= channels.activated_at. It is seeded from
    # created_at for existing rows and refreshed every time the channel is
    # switched ON, so neither a new channel nor a re-enabled one inherits the
    # archive that predates it.

    category_channel_routes {
        INTEGER category_id PK,FK
        INTEGER channel_id PK,FK
        TIMESTAMP created_at
    }
    # THE routing matrix, edited one category at a time on the panel's
    # /categories page: a category reaches exactly the channels ticked here —
    # and nothing more. It is consulted through main_category_matches now:
    # every qualifying match of a record contributes the channels of its
    # category, and the per-channel union (source 'multi' when two categories
    # hit the same channel) is what gets queued. A category with no rows here
    # publishes nothing — no fallback to "every active channel", because a
    # fallback would make the matrix a control that looks live and is not.
    # Legacy note: deliveries written before v2-combined were remapped to the
    # closest ISCED category by migration 024. Routing still reads only the
    # current matches of a record.

    deliveries {
        SERIAL id PK
        INTEGER main_id FK
        INTEGER channel_id FK
        INTEGER category_id FK
        VARCHAR route_source
        VARCHAR status
        INTEGER attempts
        BIGINT platform_message_id
        TEXT error_message
        TIMESTAMP claimed_at
        TIMESTAMP next_attempt_at
        TIMESTAMP sent_at
        TIMESTAMP created_at
        TIMESTAMP updated_at
    }
```

`main.status` is the generation lifecycle: `ready`, `processing`, `review`,
`approved`, `published`, `failed`, `rejected` (explicit human rejection from
`review`). The allowed transitions live in exactly one place,
`app/services/transitions.py` (`RECORD_TRANSITIONS` / `DELIVERY_TRANSITIONS`),
and no route sets a status by hand:

```
ready --claim--> processing --generated--> review --approve--> approved --sent--> published
                     |                          |                                 (terminal)
                     | error                    | reject -> rejected (terminal)
                     v                          +-- regenerate fails -> failed --regenerate--> review
                   failed
```

`review -> published` also exists for deployments running with
`REQUIRE_HUMAN_CONFIRMATION=false`: there the first successful send is the
decision point. `approved_at`/`published_at` stamp the decision and the first
publication (first-wins), but the statuses now carry the same facts, so the
panel never infers "approved" from a timestamp — this is what fixed the
records that stayed in "نیازمند تأیید" after their deliveries were sent.

Two migrations repair the existing rows (both additive; neither edits an
applied file):

- `016` widens `ck_main_status` and backfills from the timestamps. That alone
  left 43 rows still reading `review`, because their deliveries were sent
  *before* `published_at` existed (added in migration 013).
- `017` derives the truth from `deliveries`: an article whose deliveries are
  **all** `sent` becomes `published`, with `published_at` set to the earliest
  real send time and `approved_at` back-filled from the earliest `approve`
  audit row. Articles with any delivery still unsent are untouched — for those
  the state genuinely is not published yet — and `rejected` is never rewritten
  (a human rejection stays the final word even if a send had happened).

After 017 the invariant holds: no row is `approved`/`review` while all of its
deliveries are `sent`.

`deliveries.status` is the separate publishing lifecycle:
`awaiting_confirmation`, `pending`, `sending`, `sent`, `failed`
(and `deleted_from_channel` when an operator deletes the published message):
`awaiting_confirmation` is the gated initial status while
`REQUIRE_HUMAN_CONFIRMATION` is on; approving the record flips it to `pending`.

An article reaches **the channels its qualifying category matches name**:
every row in `main_category_matches` with `score >= MIN_CATEGORY_RELEVANCE`
(in a live category, never `other`) contributes that category's
`category_channel_routes`, and the per-channel union is what gets queued —
a channel reached through two of the record's categories gets ONE delivery
(`route_source` = the category name, or `multi` when the union coincides).
Each delivery snapshots the top-1 match's `category_id` at queue time (an
audit-only simplification in the multi-category case), so a send stays
explainable after later edits. Excluded channels are `is_active = false` and
`channels.activated_at > main.created_at` — a channel never receives news
that predates the moment it was switched on, which is what stops a newly
routed channel from being flooded with the archive. A record whose only
match is the `other` fallback reaches **no channel at all** and waits for a
reviewer. There is no field-level routing fallback.
