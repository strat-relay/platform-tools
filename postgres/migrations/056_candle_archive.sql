-- Durable candle archive for completed market bars and historical backfill.
--
-- The market-data collector currently stores bars only in Redis under
-- md:bars:<provider_symbol>:<timeframe> with a rolling KEEP_BARS=400 window.
-- Bars evicted from that window are lost permanently.  The outcome resolver
-- requires historical bars to evaluate signals whose evaluation window predates
-- the Redis window; without them it fires CANDLE_GAP and advances the cursor
-- past unevaluated history.
--
-- This migration adds a durable, append-only store with:
--   - explicit conflict detection (conflicting prior observations logged, not
--     silently replaced)
--   - bounded, restartable backfill tracking
--   - no TTL or rolling eviction
--
-- This migration is additive and idempotent (CREATE IF NOT EXISTS throughout).
-- It does not modify any existing schema objects.
-- Approval is required before applying to production (D3-A gate).

CREATE SCHEMA IF NOT EXISTS market_data;

-- ---------------------------------------------------------------------------
-- Primary archive
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS market_data.candle_archive (
    provider         text          NOT NULL,
    provider_symbol  text          NOT NULL,
    timeframe        text          NOT NULL,   -- 'M5', 'M15', 'H1', 'H4'
    candle_time      bigint        NOT NULL,   -- Unix seconds, candle open timestamp
    open_price       numeric(20,8) NOT NULL,
    high_price       numeric(20,8) NOT NULL,
    low_price        numeric(20,8) NOT NULL,
    close_price      numeric(20,8) NOT NULL,
    tick_volume      bigint,
    real_volume      bigint,
    spread           integer,
    is_closed        boolean       NOT NULL DEFAULT true,
    fetched_at       timestamptz   NOT NULL DEFAULT now(),
    source_run_id    text,
    PRIMARY KEY (provider, provider_symbol, timeframe, candle_time),
    CONSTRAINT candle_archive_geometry CHECK (
        high_price >= low_price
        AND high_price >= open_price
        AND high_price >= close_price
        AND low_price  <= open_price
        AND low_price  <= close_price
    ),
    CONSTRAINT candle_archive_positive_prices CHECK (
        open_price > 0 AND high_price > 0 AND low_price > 0 AND close_price > 0
    ),
    CONSTRAINT candle_archive_closed_only CHECK (is_closed = true)
);

-- Range query: resolver fetches all bars for a symbol/timeframe ordered by time
CREATE INDEX IF NOT EXISTS candle_archive_range_idx
    ON market_data.candle_archive (provider, provider_symbol, timeframe, candle_time ASC);

-- Coverage audit: "which bars were stored before/after a given backfill run?"
CREATE INDEX IF NOT EXISTS candle_archive_run_idx
    ON market_data.candle_archive (source_run_id, provider_symbol, timeframe)
    WHERE source_run_id IS NOT NULL;

COMMENT ON TABLE market_data.candle_archive IS
    'Durable completed-bar store. Eviction-free; primary key rejects duplicate candle identities. '
    'Conflicting re-fetches are logged to market_data.candle_conflicts instead of overwriting.';

-- ---------------------------------------------------------------------------
-- Conflict log: re-fetched bars whose OHLC differs from the stored observation
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS market_data.candle_conflicts (
    id               bigserial     PRIMARY KEY,
    provider         text          NOT NULL,
    provider_symbol  text          NOT NULL,
    timeframe        text          NOT NULL,
    candle_time      bigint        NOT NULL,
    stored_open      numeric(20,8),
    stored_high      numeric(20,8),
    stored_low       numeric(20,8),
    stored_close     numeric(20,8),
    incoming_open    numeric(20,8) NOT NULL,
    incoming_high    numeric(20,8) NOT NULL,
    incoming_low     numeric(20,8) NOT NULL,
    incoming_close   numeric(20,8) NOT NULL,
    detected_at      timestamptz   NOT NULL DEFAULT now(),
    source_run_id    text
);

CREATE INDEX IF NOT EXISTS candle_conflicts_identity_idx
    ON market_data.candle_conflicts (provider, provider_symbol, timeframe, candle_time);

COMMENT ON TABLE market_data.candle_conflicts IS
    'Audit log of conflicting bar re-fetches (same identity, different OHLC). '
    'Requires manual review; neither value is authoritative.';

-- ---------------------------------------------------------------------------
-- Backfill run tracking: durable cursor for restartable range fetches
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS market_data.backfill_runs (
    run_id              text        PRIMARY KEY,
    provider            text        NOT NULL,
    provider_symbol     text        NOT NULL,
    timeframe           text        NOT NULL,
    requested_start     bigint      NOT NULL,   -- inclusive, Unix seconds
    requested_end       bigint      NOT NULL,   -- exclusive, Unix seconds
    checkpoint_cursor   bigint,                 -- last fully committed page end
    status              text        NOT NULL    DEFAULT 'IN_PROGRESS'
                            CHECK (status IN ('IN_PROGRESS', 'COMPLETE', 'FAILED', 'PARTIAL')),
    bars_fetched        integer     NOT NULL    DEFAULT 0,
    gaps_detected       integer     NOT NULL    DEFAULT 0,
    conflicts_detected  integer     NOT NULL    DEFAULT 0,
    started_at          timestamptz NOT NULL    DEFAULT now(),
    completed_at        timestamptz,
    last_error          text
);

COMMENT ON TABLE market_data.backfill_runs IS
    'Per-run backfill cursor and health state. checkpoint_cursor allows restartable fetches '
    'without re-issuing completed pages. A run may be restarted from checkpoint_cursor.';
