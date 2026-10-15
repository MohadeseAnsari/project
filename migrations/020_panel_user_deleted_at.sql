-- Deleted console users leave the panel entirely (item 7/16).
--
-- Before this, deleting a user either hard-deleted the row (when they had no
-- audit history) or left them in the table as a deactivated account. Either
-- way a "deleted" person was still visible in the panel.
--
-- audit_logs.user_id is ON DELETE SET NULL, so a hard delete would erase the
-- author of past events. This adds an explicit tombstone instead: the row
-- stays, is filtered out of every panel listing and out of login, and the
-- audit trail keeps resolving the name.
ALTER TABLE panel_users ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP NULL;

-- The panel only ever lists live accounts, and login resolves by id.
CREATE INDEX IF NOT EXISTS ix_panel_users_live ON panel_users (id) WHERE deleted_at IS NULL;
