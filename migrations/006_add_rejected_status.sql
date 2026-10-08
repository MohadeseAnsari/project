-- Allow human rejection of review records.
--
-- Purpose: PATCH /records/{id} with action=reject moves a `review` record to
-- `rejected` so a human decision is distinguishable from an LLM/worker failure
-- (`failed`). Additive only: widens the allowed set, touches no existing rows.
-- `approve` and `save_draft` keep `review`; only `reject` writes the new value.

ALTER TABLE main DROP CONSTRAINT ck_main_status;
ALTER TABLE main ADD CONSTRAINT ck_main_status CHECK (
    status IN ('ready', 'processing', 'review', 'failed', 'rejected')
);
