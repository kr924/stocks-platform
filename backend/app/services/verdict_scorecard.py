"""
Does the AI's verdict on a result predict the stock's move?

Two filings called BUY on 23 Oct 2026 both fell ~7%, which says little on its
own: two calls prove nothing either way. Before a BUY is trusted with money
the question is how BUY calls have done as a group, against SELL, NA and the
BUYs the checks withheld (services/verdict_checks.py).

For every results prompt with an AI verdict and a price at announcement:

  d0   close of the first session after the result: the same day for a
       filing during the session (09:15-15:30 IST), the next session for
       one before the open or after the close
  d1   the session after d0
  d5   five sessions after d0

each against the price when the filing landed (price_at_announcement, the
same baseline as the panel's "since result"). Closes come from Upstox daily
candles, one request per filing, paced at ~1/s, and are stored in
verdict_outcomes, so a filing is fetched until its d5 exists and never again.
A session still open (today before 15:35) is not used as a close.

A hit is the stock moving the way the verdict said: up for BUY and BEATS
ESTIMATES, down for SELL and MISSES ESTIMATES. NA and withheld BUYs have no
direction; their average move is the yardstick a BUY has to beat.
"""
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

logger = logging.getLogger("app.verdict_scorecard")

IST = timezone(timedelta(hours=5, minutes=30))
PACE_SECONDS = 1.0
MAX_DAYS = 120

UP_IS_HIT = {"BUY", "BEATS ESTIMATES"}
DOWN_IS_HIT = {"SELL", "MISSES ESTIMATES"}

_lock = threading.Lock()
_running = False
_last_fill: Dict[str, object] = {}


def _ist(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


def first_session(event_time: datetime) -> tuple:
    """(IST date string, inclusive): the date from which d0 is the first candle."""
    t = _ist(event_time)
    d = t.strftime("%Y-%m-%d")
    after_close = (t.hour, t.minute) >= (15, 30)
    return d, not after_close


def pick_closes(candles: List[list], event_time: datetime, now: Optional[datetime] = None) -> dict:
    """
    candles: daily [timestamp, open, high, low, close, ...], any order.
    Returns {d0_date, d0_close, d1_close, d5_close} with None for sessions
    not yet closed.
    """
    now_ist = _ist(now or datetime.utcnow())
    today = now_ist.strftime("%Y-%m-%d")
    session_open = now_ist.weekday() < 5 and (now_ist.hour, now_ist.minute) < (15, 35)
    rows = sorted(((str(c[0])[:10], c[4]) for c in candles if c and len(c) > 4 and c[4]),
                  key=lambda r: r[0])
    rows = [r for r in rows if not (r[0] == today and session_open) and r[0] <= today]
    start, inclusive = first_session(event_time)
    after = [r for r in rows if (r[0] >= start if inclusive else r[0] > start)]
    out = {"d0_date": None, "d0_close": None, "d1_close": None, "d5_close": None}
    if after:
        out["d0_date"], out["d0_close"] = after[0][0], float(after[0][1])
    if len(after) > 1:
        out["d1_close"] = float(after[1][1])
    if len(after) > 5:
        out["d5_close"] = float(after[5][1])
    return out


def _verdict_of(db, pending) -> Optional[dict]:
    from app.database import TradeAILog
    if not pending.ai_log_id:
        return None
    log = db.query(TradeAILog).filter(TradeAILog.id == pending.ai_log_id).first()
    if not log or not log.ai_suggestion:
        return None
    try:
        h = json.loads(log.highlights_json or "{}")
    except Exception:
        h = {}
    checks = h.get("checks") or {}
    return {
        "verdict": (log.ai_suggestion or "NA").upper(),
        "withheld": bool(checks.get("gate")),
        "trend": (checks.get("trend") or {}).get("verdict"),
        "provider": (log.provider or "")[:200],
    }


def _record(db, pending) -> Optional[object]:
    """The outcome row for a prompt, created or refreshed from its AI log."""
    from app.database import VerdictOutcome
    v = _verdict_of(db, pending)
    row = db.query(VerdictOutcome).filter(VerdictOutcome.pending_id == pending.id).first()
    if v is None:
        return row
    if row is None:
        row = VerdictOutcome(pending_id=pending.id)
        db.add(row)
    row.symbol = pending.symbol
    row.trade_date = pending.trade_date or _ist(pending.event_time).strftime("%Y-%m-%d")
    row.event_time = pending.event_time
    row.verdict = v["verdict"]
    row.withheld = v["withheld"]
    row.trend = v["trend"]
    row.provider = v["provider"]
    if pending.price_at_announcement:
        row.base_price = pending.price_at_announcement
    row.updated_at = datetime.utcnow()
    return row


def _fill(days: int):
    global _running
    from app.database import SessionLocal, PendingResultOrder, VerdictOutcome
    db = SessionLocal()
    fetched = 0
    try:
        since = datetime.utcnow() - timedelta(days=days)
        pendings = (db.query(PendingResultOrder)
                    .filter(PendingResultOrder.event_time >= since,
                            PendingResultOrder.ai_log_id.isnot(None))
                    .order_by(PendingResultOrder.event_time.desc()).all())
        pendings = [p for p in pendings if (getattr(p, "kind", "result") or "result") == "result"]
        for p in pendings:
            try:
                _record(db, p)
                db.commit()
            except Exception as e:
                db.rollback()
                logger.debug(f"[SCORECARD] record {p.id}: {e}")

        todo = (db.query(VerdictOutcome)
                .filter(VerdictOutcome.complete == False,  # noqa: E712
                        VerdictOutcome.base_price.isnot(None))
                .all())
        if not todo:
            return
        from app.main import get_active_feed, resolve_instrument_keys
        feed = get_active_feed()
        keys_by_pending = {p.id: (p.instrument_key or "").replace(":", "|") for p in pendings}
        today = datetime.now(IST).strftime("%Y-%m-%d")
        logger.info(f"[SCORECARD] fetching closes for {len(todo)} verdict(s)")
        for row in todo:
            key = keys_by_pending.get(row.pending_id) or ""
            if "|" not in key:
                ks = resolve_instrument_keys(row.symbol or "")
                key = ks[0] if ks else ""
            if not key or not row.event_time:
                row.note = "no instrument key"
                continue
            start = (_ist(row.event_time) - timedelta(days=4)).strftime("%Y-%m-%d")
            try:
                # A range, never from == to: a single day routes to the
                # intraday endpoint and fails (UDAPI1076).
                candles = feed.get_historical_candles(key, "day", today, start) or []
            except Exception as e:
                row.note = str(e)[:200]
                time.sleep(PACE_SECONDS)
                continue
            fetched += 1
            closes = pick_closes(candles, row.event_time)
            for k, val in closes.items():
                if val is not None:
                    setattr(row, k, val)
            row.complete = row.d5_close is not None
            row.note = None
            row.updated_at = datetime.utcnow()
            try:
                db.commit()
            except Exception:
                db.rollback()
            time.sleep(PACE_SECONDS)
        cutoff = (datetime.now(IST) - timedelta(days=365)).strftime("%Y-%m-%d")
        db.query(VerdictOutcome).filter(VerdictOutcome.trade_date < cutoff).delete(synchronize_session=False)
        db.commit()
    except Exception as e:
        logger.warning(f"[SCORECARD] fill stopped: {e}")
        db.rollback()
    finally:
        db.close()
        _last_fill.update({"at": datetime.utcnow().isoformat() + "Z", "fetched": fetched,
                           "mono": time.monotonic()})
        with _lock:
            _running = False


# Opening the scorecard starts a fill at most this often; the button forces one.
AUTO_REFRESH_MINUTES = 30


def refresh(days: int = 30, wait: bool = False, force: bool = True) -> bool:
    """
    Fill missing closes in the background. False if a fill is already running,
    or (force=False) one ran within AUTO_REFRESH_MINUTES: during the session
    every recent row is incomplete, and re-reading their candles each time the
    panel opens would spend the Upstox allowance for nothing.
    """
    global _running
    with _lock:
        if _running:
            return False
        last = _last_fill.get("mono")
        if not force and last and time.monotonic() - last < AUTO_REFRESH_MINUTES * 60:
            return False
        _running = True
        _last_fill["mono"] = time.monotonic()
    days = max(1, min(int(days or 30), MAX_DAYS))
    if wait:
        _fill(days)
    else:
        threading.Thread(target=_fill, args=(days,), daemon=True, name="verdict-scorecard").start()
    return True


def is_running() -> bool:
    return _running


def _ret(close, base) -> Optional[float]:
    if close is None or not base:
        return None
    return round((close / base - 1) * 100, 2)


def group_of(row) -> str:
    if row.withheld:
        return "BUY withheld"
    v = (row.verdict or "NA").upper()
    return v if v in UP_IS_HIT | DOWN_IS_HIT else "NA"


def summary(db, days: int = 30) -> dict:
    """Per verdict: count, average move and hit rate at d0, d1 and d5."""
    from app.database import VerdictOutcome
    days = max(1, min(int(days or 30), MAX_DAYS))
    cutoff = (datetime.now(IST) - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = (db.query(VerdictOutcome).filter(VerdictOutcome.trade_date >= cutoff)
            .order_by(VerdictOutcome.event_time.desc()).all())
    order = ["BUY", "BEATS ESTIMATES", "BUY withheld", "NA", "MISSES ESTIMATES", "SELL"]
    groups: Dict[str, dict] = {}
    items = []
    for r in rows:
        g = group_of(r)
        rets = {h: _ret(getattr(r, f"{h}_close"), r.base_price) for h in ("d0", "d1", "d5")}
        items.append({"pending_id": r.pending_id, "symbol": r.symbol, "trade_date": r.trade_date,
                      "group": g, "verdict": r.verdict, "trend": r.trend, "provider": r.provider,
                      "base_price": r.base_price, "d0_date": r.d0_date, **{f"ret_{h}": v for h, v in rets.items()},
                      "note": r.note})
        s = groups.setdefault(g, {"group": g, "n": 0, **{f"{h}": [] for h in ("d0", "d1", "d5")}})
        s["n"] += 1
        for h, v in rets.items():
            if v is not None:
                s[h].append(v)
    out_groups = []
    for g in order:
        if g not in groups:
            continue
        s = groups[g]
        entry = {"group": g, "n": s["n"], "direction": "up" if g in UP_IS_HIT else "down" if g in DOWN_IS_HIT else ""}
        for h in ("d0", "d1", "d5"):
            vals = s[h]
            entry[f"{h}_n"] = len(vals)
            entry[f"{h}_avg"] = round(sum(vals) / len(vals), 2) if vals else None
            entry[f"{h}_median"] = round(sorted(vals)[len(vals) // 2], 2) if vals else None
            if g in UP_IS_HIT:
                entry[f"{h}_hit"] = round(sum(1 for v in vals if v > 0) / len(vals) * 100) if vals else None
            elif g in DOWN_IS_HIT:
                entry[f"{h}_hit"] = round(sum(1 for v in vals if v < 0) / len(vals) * 100) if vals else None
            else:
                entry[f"{h}_hit"] = None
                entry[f"{h}_up"] = round(sum(1 for v in vals if v > 0) / len(vals) * 100) if vals else None
        out_groups.append(entry)
    return {"days": days, "groups": out_groups, "rows": items[:300],
            "filling": _running,
            "last_fill": {k: v for k, v in _last_fill.items() if k != "mono"},
            "missing_price": sum(1 for r in rows if not r.base_price)}
