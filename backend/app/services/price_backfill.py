"""
Fill in prices for a day's order prompts, on demand.

Two different jobs behind one button, because what can honestly be recovered
depends entirely on whether the day has finished.

**Today** — the baseline is still capturable. A filing that landed this morning
with no `price_at_announcement` (the quote feed was down, or the symbol had not
resolved yet) can be given the current price. It is late, but it is the same
session and the same tape, so "since result" measured from it means something.

**A past day** — the baseline is gone for good. The price at the moment a
filing landed three days ago existed for one instant and nobody wrote it down;
putting today's price, or that day's close, in a field labelled "at result"
would be a fabrication that looks exactly like data. So it is left empty.

What a past day *can* have is its actual close, which Upstox will still serve
from the daily candles. That is enough for the day's change, and it is a real
measurement of the day being looked at rather than a number borrowed from now.

Note the candle range: asking for one day with from_date == to_date routes to
the intraday endpoint, which has nothing to say about a finished day and errors
with UDAPI1076. A range ending on the day wanted returns it, and the candle
before it is the previous close the change is measured against.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger("app.price_backfill")

IST = timezone(timedelta(hours=5, minutes=30))

# Enough calendar days back to clear a long weekend and find the previous
# trading session's close.
_LOOKBACK_DAYS = 8


def _store(db, symbol: str, day: str, last: Optional[float],
           prev_close: Optional[float], source: str) -> bool:
    """Record one day's price, if it is not already on file."""
    from app.database import DailyPrice

    sym = (symbol or "").strip().upper()
    if not sym or last is None:
        return False
    if db.query(DailyPrice).filter(DailyPrice.symbol == sym,
                                   DailyPrice.trade_date == day).first():
        return False
    change = (round((last - prev_close) / prev_close * 100, 2)
              if prev_close else None)
    db.add(DailyPrice(symbol=sym, trade_date=day, last_price=last,
                      prev_close=prev_close, change_pct=change, source=source))
    return True


def backfill(db, trade_date: str) -> dict:
    """
    Fetch what is still fetchable for one day's prompts.

    Returns a summary the panel can show. Never raises: a price that cannot be
    had is a fact about the day, not an error in asking.
    """
    from app.database import DailyPrice, PendingResultOrder

    today = datetime.now(IST).strftime("%Y-%m-%d")
    is_today = trade_date == today

    rows = (db.query(PendingResultOrder)
            .filter(PendingResultOrder.trade_date == trade_date).all())
    if not rows:
        return {"ok": True, "checked": 0, "baselines_filled": 0,
                "days_stored": 0, "message": f"No prompts on {trade_date}."}

    have = {s for (s,) in db.query(DailyPrice.symbol)
            .filter(DailyPrice.trade_date == trade_date).all()}

    baselines, stored, failed = 0, 0, 0

    if is_today:
        # One live read serves both jobs: the missing baselines and today's
        # price. Batched through the shared cache, so this is not a new call
        # per row.
        from app.services.results_router import get_ltp
        for r in rows:
            need_base = r.price_at_announcement is None
            need_day = r.symbol.upper() not in have
            if not (need_base or need_day):
                continue
            ltp = get_ltp(r.instrument_key, r.symbol)
            if ltp is None:
                failed += 1
                continue
            if need_base:
                r.price_at_announcement = ltp
                baselines += 1
            if need_day and _store(db, r.symbol, trade_date, ltp, None, "upstox"):
                stored += 1
                have.add(r.symbol.upper())
    else:
        # A finished day: its own close, from the daily candles.
        try:
            from app.main import get_active_feed
            feed = get_active_feed()
        except Exception as e:
            return {"ok": False, "checked": len(rows), "baselines_filled": 0,
                    "days_stored": 0, "message": f"Price feed unavailable: {e}"}

        end = trade_date
        start = (datetime.strptime(trade_date, "%Y-%m-%d")
                 - timedelta(days=_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
        for r in rows:
            if r.symbol.upper() in have:
                continue
            key = (r.instrument_key or "").replace(":", "|")
            if "|" not in key:
                failed += 1
                continue
            try:
                candles = feed.get_historical_candles(key, "day", end, start) or []
            except Exception as e:
                logger.debug(f"No candles for {r.symbol} to {end}: {e}")
                failed += 1
                continue
            # Upstox returns newest first or oldest first depending on the
            # endpoint; sorting on the timestamp makes the order ours.
            candles = sorted(candles, key=lambda c: str(c[0]))
            on_day = [c for c in candles if str(c[0])[:10] == trade_date]
            if not on_day:
                failed += 1
                continue
            close = on_day[-1][4]
            before = [c for c in candles if str(c[0])[:10] < trade_date]
            prev = before[-1][4] if before else None
            if _store(db, r.symbol, trade_date, close, prev, "upstox candles"):
                stored += 1
                have.add(r.symbol.upper())

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        return {"ok": False, "checked": len(rows), "baselines_filled": 0,
                "days_stored": 0, "message": f"Could not save: {e}"}

    # Report the state, not just the delta. "Nothing new to fetch" on a day
    # that already has 42 closes on file reads as a failure, which is the
    # opposite of what happened.
    on_file = (db.query(DailyPrice)
               .filter(DailyPrice.trade_date == trade_date).count())
    total = len({(r.symbol or "").upper() for r in rows if r.symbol})

    if is_today:
        if baselines or stored:
            msg = f"Filled {baselines} baseline(s) and recorded {stored} price(s)."
        else:
            msg = "Everything on this day already had a price."
    else:
        msg = (f"Recorded {stored} closing price(s)."
               if stored else "Nothing further to fetch.")
        msg += f" {on_file} of {total} companies on {trade_date} now have a close on file."
        if on_file:
            msg += (" The price at the moment each filing landed was never recorded and "
                    "cannot be recovered, so 'since result' stays blank for this day.")
    if failed:
        msg += (f" {failed} symbol(s) have no price available — they do not resolve "
                "to a listing on either exchange.")

    logger.info(f"[BACKFILL] {trade_date}: {baselines} baselines, {stored} days, {failed} failed.")
    return {"ok": True, "checked": len(rows), "baselines_filled": baselines,
            "days_stored": stored, "failed": failed, "is_today": is_today,
            "message": msg}
