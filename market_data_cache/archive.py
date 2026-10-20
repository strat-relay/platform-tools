"""Append-only completed-candle persistence before Redis window advancement."""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from .store import TIMEFRAME_SECONDS


class ArchiveUnavailable(RuntimeError):
    """Candle publication must wait for durable archive verification."""


class ArchiveConflict(ArchiveUnavailable):
    """A closed candle differs from another observation of the same identity."""


def normalise(batches: dict[str, list[dict]], observed_at: float) -> list[dict]:
    by_key = {}
    if sum(map(len, batches.values())) > 50000:
        raise ArchiveUnavailable('archive batch exceeds 50000 observations')
    for timeframe, bars in batches.items():
        if timeframe not in TIMEFRAME_SECONDS:
            raise ArchiveUnavailable('unsupported archive timeframe')
        step = TIMEFRAME_SECONDS[timeframe]
        for bar in bars:
            stamp = bar['time']
            if type(stamp) is not int or stamp < 0 or stamp % step or stamp+step > observed_at:
                raise ArchiveUnavailable('archive requires aligned completed candles')
            if bar.get('is_closed') is False or bar.get('complete') is False:
                raise ArchiveUnavailable('archive refuses forming candles')
            prices = tuple(Decimal(str(bar[k])) for k in ('open','high','low','close'))
            if any(not v.is_finite() or v<=0 or v>=Decimal('1e12') or
                   v != v.quantize(Decimal('.00000001')) for v in prices):
                raise ArchiveUnavailable('archive price is invalid or loses database precision')
            op, hi, lo, cl = prices
            if hi < max(op,cl) or lo > min(op,cl) or lo > hi:
                raise ArchiveUnavailable('archive OHLC geometry is invalid')
            key = (timeframe,stamp)
            if key in by_key:
                if tuple(Decimal(by_key[key][k]) for k in ('open_price','high_price','low_price','close_price')) != prices:
                    raise ArchiveConflict('conflicting closed candles in archive input')
                continue
            record = dict(timeframe=timeframe,candle_time=stamp,
                          **dict(zip(('open_price','high_price','low_price','close_price'),map(str,prices))))
            for name in ('tick_volume','real_volume','spread'):
                value = bar.get(name)
                limit = 2**31 if name == 'spread' else 2**63
                record[name] = value if type(value) is int and 0<=value<limit else None
            by_key[key] = record
    return [by_key[k] for k in sorted(by_key)]


RECORD = '''timeframe text, candle_time bigint, open_price numeric,
    high_price numeric, low_price numeric, close_price numeric,
    tick_volume bigint, real_volume bigint, spread integer'''


class PostgresCandleArchive:
    def __init__(self, connection: Callable[[], Any], *, source_run_id: str,
                 expected_database: str = 'trading_platform'):
        self.connection = connection
        self.source_run_id = source_run_id
        self.expected_database = expected_database

    def __call__(self, symbol: str, batches: dict[str,list[dict]], *, observed_at: float) -> dict:
        try:
            records = normalise(batches,observed_at)
            if not records:
                raise ArchiveUnavailable('no completed candles to archive')
            payload = json.dumps(records,sort_keys=True,separators=(',',':'))
            with self.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SET LOCAL statement_timeout = '10s'")
                    cur.execute("SET LOCAL lock_timeout = '2s'")
                    cur.execute("""SELECT current_database(), pg_is_in_recovery(),
                        current_setting('transaction_read_only'),
                        has_table_privilege(current_user,'market_data.candle_archive','SELECT'),
                        has_table_privilege(current_user,'market_data.candle_archive','INSERT')""")
                    if tuple(cur.fetchone()) != (self.expected_database,False,'off',True,True):
                        raise ArchiveUnavailable('archive database target or privileges invalid')
                    # Batch insert and conflict check share one transaction. Concurrent
                    # inserts are compared after ON CONFLICT has waited for their commit.
                    cur.execute(f"""WITH incoming AS (
                        SELECT * FROM jsonb_to_recordset(%s::jsonb) AS x({RECORD})
                    ), inserted AS (
                        INSERT INTO market_data.candle_archive
                         (provider,provider_symbol,timeframe,candle_time,
                          open_price,high_price,low_price,close_price,
                          tick_volume,real_volume,spread,source_run_id)
                        SELECT 'MT5',%s,timeframe,candle_time,open_price,high_price,low_price,close_price,
                               tick_volume,real_volume,spread,%s FROM incoming
                        ORDER BY timeframe,candle_time
                        ON CONFLICT (provider,provider_symbol,timeframe,candle_time) DO NOTHING
                        RETURNING 1
                    ) SELECT count(*) FROM inserted""",(payload,symbol,self.source_run_id))
                    inserted = int(cur.fetchone()[0])
                    cur.execute(f"""SELECT x.timeframe,x.candle_time
                        FROM jsonb_to_recordset(%s::jsonb) AS x({RECORD})
                        JOIN market_data.candle_archive a
                          ON a.provider='MT5' AND a.provider_symbol=%s
                         AND a.timeframe=x.timeframe AND a.candle_time=x.candle_time
                        WHERE (a.open_price,a.high_price,a.low_price,a.close_price)
                          IS DISTINCT FROM (x.open_price,x.high_price,x.low_price,x.close_price)
                        LIMIT 1""",(payload,symbol))
                    if cur.fetchone():
                        raise ArchiveConflict('stored closed candle differs; archive transaction rolled back')
                conn.commit()
            streams = {}
            for tf in batches:
                stamps = [r['candle_time'] for r in records if r['timeframe']==tf]
                streams[tf] = dict(bars=len(stamps),first_open=min(stamps) if stamps else None,
                                   last_open=max(stamps) if stamps else None)
            return dict(status='VERIFIED',checked_at=observed_at,verified_bars=len(records),streams=streams,
                        inserted_bars=inserted,source_run_id=self.source_run_id,
                        batch_sha256=hashlib.sha256(payload.encode()).hexdigest())
        except ArchiveUnavailable:
            raise
        except (ValueError,KeyError,TypeError,InvalidOperation) as exc:
            raise ArchiveUnavailable('archive input validation failed') from exc
        except Exception as exc:
            # Driver errors may include row contents or connection details.
            raise ArchiveUnavailable('archive persistence failed: '+type(exc).__name__) from exc
