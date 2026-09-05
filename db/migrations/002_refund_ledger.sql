-- Backs PostgresRefundBackend, for the case where the fake payments API
-- has to survive a real process restart (the chaos suite) and an
-- in-memory Python object cannot.
--
-- Two tables on purpose, so exactly-once is measurable rather than
-- assumed: refund_attempts records every physical call, insert-only and
-- never deduplicated, while refund_ledger holds one row per
-- idempotency_key, ever. The gap between the two counts is the proof.

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
