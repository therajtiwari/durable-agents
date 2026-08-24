CREATE TABLE events (
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

CREATE INDEX idx_events_type ON events (type) WHERE type IN
    ('ApprovalRequested', 'RunCompleted', 'RunFailed');
