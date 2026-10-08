-- Panel viewer role (read-only).
--
-- Purpose: the approved users design has three roles — viewer (read-only),
-- reviewer/editor (review actions), admin (full access). The panel enforces:
-- viewer sees pages but every mutation returns 403. Additive only: one row,
-- no existing rows touched.

INSERT INTO roles (name, description) VALUES
    ('viewer', 'Read-only: view pages, no review or config changes')
ON CONFLICT (name) DO NOTHING;
