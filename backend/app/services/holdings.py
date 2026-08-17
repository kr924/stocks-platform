"""
Keep the tracker's Holdings tab in step with what has actually been bought.

A position exists in two places that never spoke to each other: TradeConfig
knows a buy filled, and the Watchlist's `is_holding` flag is what the Stocks
Tracker draws. Buying through the app left the tracker unaware, so the one
screen showing live prices for owned stock had to be maintained by hand.

Selling clears the flag rather than deleting the row. The stock may have been on
the watchlist before it was ever bought — removing it would quietly discard
something the user put there themselves — and a stock just sold is usually still
worth watching.
"""
import logging

from sqlalchemy.orm import Session

from app.database import Watchlist

logger = logging.getLogger("app.holdings")


def _resolve(symbol: str, instrument_key: str = "") -> str:
    """A real instrument key for the watchlist row, or "" when none exists."""
    if instrument_key and "|" in instrument_key:
        tail = instrument_key.split("|")[-1]
        if len(tail) == 12 and tail[:2].isalpha():
            return instrument_key
    try:
        from app.main import resolve_instrument_keys
        keys = resolve_instrument_keys(symbol)
        return keys[0] if keys else ""
    except Exception:
        return ""


def mark_as_holding(db: Session, symbol: str, instrument_key: str = "", name: str = "") -> bool:
    """
    Add a bought stock to the watchlist as a holding, or flag the existing row.

    Returns True when the tracker changed. Failures are logged and swallowed:
    this runs immediately after an order has filled, and nothing about
    bookkeeping should be able to unwind a completed trade.
    """
    symbol = (symbol or "").upper().strip()
    if not symbol:
        return False
    try:
        row = db.query(Watchlist).filter(Watchlist.symbol == symbol).first()
        if row:
            if row.is_holding:
                return False
            row.is_holding = True
            db.commit()
            logger.info(f"📌 [HOLDINGS] {symbol} flagged as held")
            return True

        key = _resolve(symbol, instrument_key)
        if not key:
            logger.warning(f"[HOLDINGS] {symbol} bought but resolves to no instrument — not added")
            return False
        db.add(Watchlist(symbol=symbol, name=name or symbol, instrument_key=key, is_holding=True))
        db.commit()
        logger.info(f"📌 [HOLDINGS] {symbol} added to the watchlist as held")
        return True
    except Exception as e:
        db.rollback()
        logger.error(f"[HOLDINGS] Could not mark {symbol} as held: {e}")
        return False


def clear_holding(db: Session, symbol: str) -> bool:
    """Drop a sold stock out of Holdings, leaving it on the watchlist."""
    symbol = (symbol or "").upper().strip()
    if not symbol:
        return False
    try:
        row = db.query(Watchlist).filter(Watchlist.symbol == symbol).first()
        if not row or not row.is_holding:
            return False
        row.is_holding = False
        db.commit()
        logger.info(f"📌 [HOLDINGS] {symbol} cleared — sold")
        return True
    except Exception as e:
        db.rollback()
        logger.error(f"[HOLDINGS] Could not clear {symbol}: {e}")
        return False
