-- The one table this runtime needs. Ships inside the package (rather
-- than only in db/migrations/) so that a `pip install` is sufficient to
-- create it — see storage/schema.py's create_schema().
--
-- IF NOT EXISTS throughout: create_schema() is safe to call on every
-- application boot, which is the usual way a library like this gets
-- wired in.

CREATE TABLE IF NOT EXISTS events (
    run_id      UUID        NOT NULL,
    seq         INT         NOT NULL,
    type        TEXT        NOT NULL,
    payload     JSONB       NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, seq)
);

-- No separate index on (run_id, seq): the PRIMARY KEY above already creates
-- one automatically. A duplicate index here would only cost write throughput
-- and disk space for zero query benefit.

-- Partial index for the three lookups a supervising process actually
-- runs across all runs: find runs awaiting approval, and find runs that
-- have finished. Partial because those three types are a small fraction
-- of the log.
CREATE INDEX IF NOT EXISTS idx_events_type ON events (type) WHERE type IN
    ('ApprovalRequested', 'RunCompleted', 'RunFailed');
