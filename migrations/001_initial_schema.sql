CREATE TABLE categories (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) UNIQUE NOT NULL,
    description TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT now()
);

CREATE TABLE fields (
    id SERIAL PRIMARY KEY,
    name VARCHAR(500) UNIQUE NOT NULL,
    normalized_name VARCHAR(500) UNIQUE NOT NULL,
    category_id INTEGER NOT NULL REFERENCES categories(id),
    source VARCHAR(20) NOT NULL DEFAULT 'spreadsheet',
    created_at TIMESTAMP NOT NULL DEFAULT now()
);

CREATE TABLE models (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) UNIQUE NOT NULL,
    provider VARCHAR(50) NOT NULL,
    version VARCHAR(50) NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMP NOT NULL DEFAULT now()
);

CREATE TABLE prompts (
    id SERIAL PRIMARY KEY,
    version VARCHAR(50) UNIQUE NOT NULL,
    system_prompt TEXT NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMP NOT NULL DEFAULT now()
);

CREATE TABLE main (
    id SERIAL PRIMARY KEY,
    username VARCHAR(255) NOT NULL,
    title TEXT NOT NULL,
    abstract TEXT NOT NULL,
    field VARCHAR(500) NOT NULL,
    university VARCHAR(255) NOT NULL,
    table_of_contents TEXT,
    author VARCHAR(255) NOT NULL,
    supervisor VARCHAR(255) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'ready',
    field_id INTEGER REFERENCES fields(id),
    category_id INTEGER REFERENCES categories(id),
    model_id INTEGER REFERENCES models(id),
    prompt_id INTEGER REFERENCES prompts(id),
    output_json JSONB,
    error_message TEXT,
    claimed_at TIMESTAMP,
    heartbeat_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    updated_at TIMESTAMP NOT NULL DEFAULT now(),
    CONSTRAINT ck_main_status CHECK (status IN ('ready', 'processing', 'review', 'failed'))
);

CREATE INDEX ix_main_status_created_at ON main (status, created_at);
CREATE INDEX ix_main_field_id ON main (field_id);
CREATE INDEX ix_main_category_id ON main (category_id);
