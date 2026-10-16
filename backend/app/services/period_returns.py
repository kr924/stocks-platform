"""
5d / 10d / 15d / 1m / 3m / 6m / 1y returns for the earnings calendar.

Each period is measured from the close N trading sessions before today, read
from Upstox daily candles: 5, 10, 15, 21, 63, 126 and 252 sessions, the same
trading-day counts the index board uses. Those closes ("bases") are fetched
once per instrument per IST day and stored in period_bases. A published close
never changes, so nothing is fetched twice in a day, restarts included.

The return itself is worked out where the live price is: the calendar
divides the current price by each base. So the figures move with the market
during the session, and the bases cost one candle request per stock per day.

Fetching runs in one background thread, paced at about one request a second
(Upstox meters historical calls too), soonest meetings first so today's and
tomorrow's companies fill in within a minute. Only the pipe form of an
instrument key is a request key, and a key the feed cannot resolve is
skipped, never synthesised.
"""
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

logger = logging.getLogger("app.period_returns")

IST = timezone(timedelta(hours=5, minutes=30))

# Trading sessions per period. Approximate on purpose: a holiday shifts the
# comparison by a day rather than losing it.
PERIODS = {"5d": 5, "10d": 10, "15d": 15, "1m": 21, "3m": 63, "6m": 126, "1y": 252}
PACE_SECONDS = 1.0

_lock = threading.Lock()
_running = False
_failed: Dict[str, str] = {}      # key -> IST day it failed; not retried that day


def _today() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d")


def bases_from_candles(rows: List[list], today: str) -> Optional[dict]:
    """
    rows: daily candles oldest first, [timestamp, open, high, low, close, ...].
    Today's own candle (if Upstox has published a partial one) is left out:
    "N sessions ago" counts back from today, and the live price stands for
    today. A stock younger than a period gets no base for it rather than its
    first close under the wrong label.
    """
    done = [r for r in rows if str(r[0])[:10] < today and r[4]]
    if not done:
        return None
    out = {"last_close": float(done[-1][4]), "last_date": str(done[-1][0])[:10],
           "bases": {}, "from": {}}
    for name, n in PERIODS.items():
        if len(done) >= n:
            r = done[-n]
            out["bases"][name] = float(r[4])
            out["from"][name] = str(r[0])[:10]
    return out


def stored(db, keys: List[str], day: Optional[str] = None) -> Dict[str, dict]:
    """Today's bases for these instrument keys, from the table."""
    from app.database import PeriodBase
    day = day or _today()
    keys = [k for k in dict.fromkeys(keys) if k]
    if not keys:
        return {}
    out = {}
    for i in range(0, len(keys), 400):
        for row in db.query(PeriodBase).filter(PeriodBase.day == day,
                                               PeriodBase.instrument_key.in_(keys[i:i + 400])).all():
            try:
                out[row.instrument_key] = json.loads(row.data_json or "null")
            except Exception:
                pass
    return out


def _fetch_one(feed, key: str, today: str) -> Optional[dict]:
    start = (datetime.now(IST) - timedelta(days=400)).strftime("%Y-%m-%d")
    rows = feed.get_historical_candles(key, "day", today, start) or []
    rows = sorted(rows, key=lambda c: str(c[0]))
    return bases_from_candles(rows, today)


def _fill(keys: List[str]):
    global _running
    from app.database import SessionLocal, PeriodBase
    today = _today()
    db = SessionLocal()
    try:
        try:
            purge_old(db)
        except Exception:
            db.rollback()
        from app.main import get_active_feed
        feed = get_active_feed()
        have = set(stored(db, keys, today))
        todo = [k for k in keys if k not in have and _failed.get(k) != today]
        if todo:
            logger.info(f"[RETURNS] fetching bases for {len(todo)} instrument(s)")
        for key in todo:
            try:
                data = _fetch_one(feed, key, today)
            except Exception as e:
                _failed[key] = today
                logger.info(f"[RETURNS] {key}: {str(e)[:120]}")
                time.sleep(PACE_SECONDS)
                continue
            try:
                db.merge(PeriodBase(instrument_key=key, day=today,
                                    data_json=json.dumps(data), fetched_at=datetime.utcnow()))
                db.commit()
            except Exception as e:
                db.rollback()
                logger.warning(f"[RETURNS] could not store {key}: {e}")
            if data is None:
                _failed[key] = today
            time.sleep(PACE_SECONDS)
    except Exception as e:
        logger.warning(f"[RETURNS] fill stopped: {e}")
    finally:
        db.close()
        with _lock:
            _running = False


def ensure(keys: List[str]) -> bool:
    """
    Start fetching today's bases for any of these keys not yet stored, in
    the order given (soonest meetings first). Returns at once; a fill already
    running is not doubled.
    """
    global _running
    keys = [k for k in dict.fromkeys(keys) if k and "|" in k]
    if not keys:
        return False
    with _lock:
        if _running:
            return False
        _running = True
    threading.Thread(target=_fill, args=(keys,), daemon=True, name="period-returns").start()
    return True


def failed_today(key: str) -> bool:
    """The fetch for this key failed or returned no candles today; not retried until tomorrow."""
    return _failed.get(key) == _today()


def purge_old(db, keep_days: int = 3) -> int:
    from app.database import PeriodBase
    cutoff = (datetime.now(IST) - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    n = db.query(PeriodBase).filter(PeriodBase.day < cutoff).delete(synchronize_session=False)
    db.commit()
    return n
