import logging
from datetime import datetime, timezone, timedelta
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

def sync_earnings_to_watchlist(db: Session):
    """
    Auto-add today's upcoming earnings stocks into the Watchlist DB table
    so Upstox live market feed tracks and streams real-time quotes for them.
    Also cleans expired past earnings stocks from previous dates.
    """
    try:
        from app.routers.intelligence import get_upcoming_earnings
        from app.main import get_nse_equities
        from app.database import Watchlist

        upcoming = get_upcoming_earnings(db)
        if not upcoming:
            return {"status": "success", "added_count": 0, "synced_count": 0, "synced_symbols": []}

        eqs = get_nse_equities(db=db)
        sym_to_info = {}
        for item in eqs:
            if item.get("symbol") and item.get("key"):
                sym_to_info[item["symbol"].upper()] = {
                    "key": item["key"],
                    "name": item.get("name") or f"{item['symbol']} Ltd"
                }

        synced_symbols = []
        added_count = 0

        for item in upcoming:
            sym = item["symbol"].upper()
            info = sym_to_info.get(sym)
            if not info:
                info = {"key": f"NSE_EQ|{sym}", "name": item.get("title") or f"{sym} Ltd"}

            ikey = info["key"]
            existing = db.query(Watchlist).filter(Watchlist.instrument_key == ikey).first()
            if not existing:
                wl = Watchlist(
                    symbol=sym,
                    name=info["name"],
                    instrument_key=ikey,
                    is_holding=False
                )
                db.add(wl)
                added_count += 1
            synced_symbols.append(sym)

        db.commit()
        logger.info(f"Synced {len(synced_symbols)} earnings stocks ({added_count} new) to Watchlist for live Upstox quotes.")
        return {
            "status": "success",
            "added_count": added_count,
            "synced_count": len(synced_symbols),
            "synced_symbols": synced_symbols
        }
    except Exception as e:
        db.rollback()
        logger.error(f"Error syncing earnings to watchlist: {e}")
        return {"status": "error", "detail": str(e)}
