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
    channels ||--o{ deliveries : "target_of"
    main ||--o{ deliveries : "published_as"
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
        TIMESTAMP created_at
    }

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
        TIMESTAMP created_at
    }

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
        VARCHAR platform UK
        BIGINT chat_id UK
        VARCHAR username
        VARCHAR title
        BOOLEAN is_active
        TIMESTAMP created_at
    }

    field_channel_routes {
        INTEGER field_id PK,FK
        INTEGER channel_id PK,FK
        TIMESTAMP created_at
    }

    deliveries {
        SERIAL id PK
        INTEGER main_id FK
        INTEGER channel_id FK
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
`failed`, plus `rejected` (explicit human rejection from `review` via
`PATCH /records/{id}`). `deliveries.status` is the separate publishing lifecycle: `pending`,
`sending`, `sent`, `failed`, `awaiting_confirmation` (reserved for the planned
human confirmation step, never written today).
