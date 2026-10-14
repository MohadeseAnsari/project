# Entity Relationship Diagram

```mermaid
erDiagram
    categories ||--o{ fields : "contains"
    categories ||--o{ main : "categorized_as"
    fields ||--o{ main : "selected_for"
    models ||--o{ main : "generated_by"
    prompts ||--o{ main : "generated_with"
    fields ||--o{ field_channel_routes : "routed_by"
    channels ||--o{ field_channel_routes : "receives"
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
        TIMESTAMP created_at
    }

    fields {
        SERIAL id PK
        VARCHAR name UK
        VARCHAR normalized_name UK
        INTEGER category_id FK
        VARCHAR source
        VARCHAR matched_keyword
        VARCHAR category_source
        TIMESTAMP created_at
    }
    # fields.category_id is the ONE place a field's category is decided. It is
    # assigned once — by hand from the panel, or by the classification rules
    # when the field is first seen — and read from then on, never re-derived
    # per submission (item 15). category_source records which: 'manual' or
    # 'rules'.

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

    field_channel_routes {
        INTEGER field_id PK,FK
        INTEGER channel_id PK,FK
        TIMESTAMP created_at
    }
    # Retired from the delivery decision (item 6) and no longer written by the
    # panel. Kept so existing rows are not lost; dropping the table would be a
    # destructive migration for no gain.

    category_channel_routes {
        INTEGER category_id PK,FK
        INTEGER channel_id PK,FK
        TIMESTAMP created_at
    }
    # Retired from the delivery decision and no longer written by the panel
    # (item 6, final rule: news goes to every ACTIVE channel, nothing is
    # configurable). Kept so existing rows are not lost; the panel has no
    # screen for them any more.

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

An article reaches **every active channel**; there is no route table in the
decision any more. The only exclusions are `is_active = false` and
`channels.activated_at > main.created_at` — a channel never receives news that
predates the moment it was switched on. Each delivery snapshots `category_id` +
`route_source` (`all`) at queue time, so a send stays explainable after later
edits. `fields.matched_keyword` records which keyword produced the field's
category, for the same reason.
