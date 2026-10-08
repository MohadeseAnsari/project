-- Panel authentication and audit trail (owner-approved).
--
-- Purpose: server-rendered review panel needs human accounts, not the shared
-- machine secret. `roles` seeds admin/editor; `panel_users` stores argon2
-- password hashes (never plaintext); `audit_logs` records who approved,
-- rejected, edited, retried, or changed config. Additive only: new tables,
-- no existing rows touched. The first admin is created at runtime from
-- PANEL_ADMIN_USERNAME/PANEL_ADMIN_PASSWORD env vars, never seeded here.

CREATE TABLE roles (
    id SERIAL PRIMARY KEY,
    name VARCHAR(20) UNIQUE NOT NULL,
    description TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT now()
);
INSERT INTO roles (name, description) VALUES
    ('admin', 'Full access: review, retry, models/prompts/channels/users'),
    ('editor', 'Review only: approve/reject/save_draft and retry')
ON CONFLICT (name) DO NOTHING;

CREATE TABLE panel_users (
    id SERIAL PRIMARY KEY,
    username VARCHAR(100) UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role_id INTEGER NOT NULL REFERENCES roles(id),
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMP NOT NULL DEFAULT now()
);

CREATE TABLE audit_logs (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES panel_users(id) ON DELETE SET NULL,
    action VARCHAR(50) NOT NULL,
    main_id INTEGER REFERENCES main(id) ON DELETE SET NULL,
    detail TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT now()
);
CREATE INDEX ix_audit_logs_main_id ON audit_logs (main_id);
