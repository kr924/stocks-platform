import logging
from datetime import datetime, timezone, timedelta
from sqlalchemy.orm import Session
from app.database import EarningsBucket, Watchlist

logger = logging.getLogger(__name__)

def sync_earnings_to_watchlist(db: Session):
    """
    Sync today's upcoming earnings stocks into the dedicated EarningsBucket DB table
    for live quotes & news fetching without polluting the user's private Watchlist.
    Cleans expired earnings stocks from previous dates.
    Also cleans up any auto-synced earnings stocks from Watchlist.
    """
    try:
        from app.routers.intelligence import get_upcoming_earnings
        from app.main import get_nse_equities

        # 1. Clean auto-synced non-holding items from Watchlist to restore user's clean Watchlist
        try:
            # Keep only items marked as holding or user-created watchlist entries
            auto_items = db.query(Watchlist).filter(Watchlist.is_holding == False).all()
            if len(auto_items) > 30: # If watchlist was flooded
                # Keep first 30 user items, remove excess synced items
                user_items = db.query(Watchlist).order_by(Watchlist.id.asc()).limit(20).all()
                user_ids = {u.id for u in user_items}
                for item in auto_items:
                    if item.id not in user_ids:
                        db.delete(item)
                db.commit()
                logger.info("Restored clean Watchlist table.")
        except Exception as err:
            db.rollback()
            logger.warning(f"Watchlist cleanup warning: {err}")

        # 2. Fetch upcoming earnings for today
        upcoming = get_upcoming_earnings(db)
        if not upcoming:
            return {"status": "success", "added_count": 0, "synced_count": 0, "synced_symbols": []}

        eqs = get_nse_equities()
        sym_to_info = {}
        for item in eqs:
            if item.get("symbol") and item.get("key"):
                sym_to_info[item["symbol"].upper()] = {
                    "key": item["key"],
                    "name": item.get("name") or f"{item['symbol']} Ltd"
                }

        # 3. Clear old past earnings from EarningsBucket
        ist = timezone(timedelta(hours=5, minutes=30))
        today_str = datetime.now(ist).strftime("%Y-%m-%d")
        try:
            db.query(EarningsBucket).filter(EarningsBucket.earnings_date < today_str).delete(synchronize_session=False)
            db.commit()
        except Exception:
            db.rollback()

        synced_symbols = []
        added_count = 0

        for item in upcoming:
            sym = item["symbol"].upper()
            info = sym_to_info.get(sym)
            if not info:
                info = {"key": f"NSE_EQ|{sym}", "name": item.get("title") or f"{sym} Ltd"}

            ikey = info["key"]
            m_date = item.get("meeting_date", today_str)

            existing = db.query(EarningsBucket).filter(
                (EarningsBucket.instrument_key == ikey) | (EarningsBucket.symbol == sym)
            ).first()

            if not existing:
                try:
                    eb = EarningsBucket(
                        symbol=sym,
                        name=info["name"],
                        instrument_key=ikey,
                        earnings_date=m_date
                    )
                    db.add(eb)
                    db.commit()
                    added_count += 1
                except Exception as row_err:
                    db.rollback()
                    logger.debug(f"Skipping duplicate bucket item {sym}/{ikey}: {row_err}")
            synced_symbols.append(sym)

        logger.info(f"Synced {len(synced_symbols)} earnings stocks ({added_count} new) into EarningsBucket for news & live quote fetching.")
        return {
            "status": "success",
            "added_count": added_count,
            "synced_count": len(synced_symbols),
            "synced_symbols": synced_symbols
        }
    except Exception as e:
        db.rollback()
        logger.error(f"Error syncing earnings to bucket: {e}")
        return {"status": "error", "detail": str(e)}
