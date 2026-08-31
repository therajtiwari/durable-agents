-- Backs PostgresRefundBackend, used specifically where the fake payments
-- API needs to survive a real process restart (the chaos test suite) —
-- an in-memory Python object cannot. Two tables, mirroring the same
-- separation as spec's own FakeRefundAPI: every physical call attempt
-- (refund_attempts, insert-only, never deduplicated) versus what actually
-- got created (refund_ledger, one row per idempotency_key, ever).

CREATE TABLE refund_attempts (
    id              SERIAL      PRIMARY KEY,
    order_id        TEXT        NOT NULL,
    idempotency_key TEXT        NOT NULL,
    attempted_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE refund_ledger (
    idempotency_key TEXT        PRIMARY KEY,
    refund_id       TEXT        NOT NULL,
    order_id        TEXT        NOT NULL,
    amount_inr      INT         NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
