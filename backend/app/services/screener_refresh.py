"""
Re-asking Screener for quarters it had not published when a filing arrived.

A results prompt fetches Screener once, the moment it is created. The filing
is usually a few hours old at that point, and Screener publishes the quarter
later, often overnight. So the stored snapshot holds the year-ago and
previous-quarter bases but not the filing's own quarter, and the Screener tab
read "not published yet" for good.

"A published quarter cannot change, so never re-fetch" still holds. That rule
is about quarters Screener has. A snapshot that lacks the filing's quarter is
incomplete, not settled, and asking again is the only way to complete it.

When:
  08:00 IST, before the digest is built: every row in the digest, so the
           report carries Screener's figures where they exist.
  08:00 IST, after the digest is sent: every other result from the last
           7 days still missing its quarter. After, so a heavy season's paced
           requests never hold the digest back.
  On demand: the Screener tab's "Check Screener" button, for one filing.

A row is re-asked at most once every 6 hours unless forced, and each fetch is
paced by screener_quarters._get. The fetch passes want_quarter, so a stale
consolidated page does not hide a standalone page that has the quarter.
"""
import json
import logging
from datetime import datetime, timedelta
from typing import Iterable, Optional

logger = logging.getLogger("app.screener_refresh")

RECHECK_HOURS = 6
WINDOW_DAYS = 7


def _snapshot(p) -> Optional[dict]:
    raw = getattr(p, "screener_json", None)
    if not raw:
        return None
    try:
        snap = json.loads(raw)
        return snap if isinstance(snap, dict) else None
    except Exception:
        return None


def _extraction(p) -> Optional[dict]:
    raw = getattr(p, "extraction_json", None)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def is_result(p) -> bool:
    return (getattr(p, "kind", "result") or "result") == "result"


def filing_quarter_of(p) -> str:
    from app.services.result_compare import filing_quarter
    quarter, _src, _ok = filing_quarter(_extraction(p), getattr(p, "event_time", None))
    return quarter or ""


def has_quarter(db, p, quarter: str) -> bool:
    """Screener's figures for `quarter` are already on the row or in the store."""
    from app.services.result_compare import screener_figures
    if not quarter:
        return True   # nothing to look for
    return bool(screener_figures(db, p.symbol, [quarter], _snapshot(p)).get(quarter))


def refresh_row(db, p, force: bool = False) -> dict:
    """
    Fetch Screener again for one filing if its quarter is missing.

    Returns {"status": ..., "quarter": ...}, status one of:
      published   Screener has the quarter (already, or after this fetch)
      not_yet     fetched; Screener has not published it
      recent      checked under RECHECK_HOURS ago; not asked again
      failed      the fetch did not succeed
      not_result  impact news; nothing to compare
    """
    if not is_result(p):
        return {"status": "not_result", "quarter": ""}
    quarter = filing_quarter_of(p)
    if has_quarter(db, p, quarter):
        return {"status": "published", "quarter": quarter}

    snap = _snapshot(p) or {}
    checked = snap.get("fetched_at")
    if checked and not force:
        try:
            if datetime.utcnow() - datetime.fromisoformat(checked) < timedelta(hours=RECHECK_HOURS):
                return {"status": "recent", "quarter": quarter}
        except ValueError:
            pass

    from app.services.screener_quarters import fetch_latest_quarter
    fresh = fetch_latest_quarter(p.symbol, want_quarter=quarter, force=True)
    if not fresh.get("ok"):
        # Keep the old snapshot; record only that we asked.
        if snap:
            snap["fetched_at"] = datetime.utcnow().isoformat()
            p.screener_json = json.dumps(snap)
            db.commit()
        return {"status": "failed", "quarter": quarter, "error": fresh.get("error", "")}

    fresh = dict(fresh)
    fresh["fetched_at"] = datetime.utcnow().isoformat()
    p.screener_json = json.dumps(fresh)
    db.commit()
    got = quarter in (fresh.get("quarters") or [])
    return {"status": "published" if got else "not_yet", "quarter": quarter}


def refresh_rows(db, rows: Iterable, force: bool = False) -> dict:
    counts = {"published": 0, "not_yet": 0, "recent": 0, "failed": 0, "not_result": 0, "newly_published": 0}
    for p in rows:
        try:
            before = has_quarter(db, p, filing_quarter_of(p)) if is_result(p) else True
            res = refresh_row(db, p, force=force)
            counts[res["status"]] = counts.get(res["status"], 0) + 1
            if res["status"] == "published" and not before:
                counts["newly_published"] += 1
        except Exception as e:
            db.rollback()
            counts["failed"] += 1
            logger.warning(f"[SCREENER REFRESH] {getattr(p, 'symbol', '?')}: {e}")
    return counts


def refresh_recent(db, days: int = WINDOW_DAYS, exclude_ids: Iterable[int] = ()) -> dict:
    """Every result from the last `days` still missing its own quarter."""
    from app.database import PendingResultOrder
    since = datetime.utcnow() - timedelta(days=days)
    skip = set(exclude_ids or ())
    rows = [p for p in db.query(PendingResultOrder)
            .filter(PendingResultOrder.created_at >= since)
            .order_by(PendingResultOrder.event_time.desc()).all()
            if p.id not in skip and is_result(p)]
    counts = refresh_rows(db, rows)
    logger.info(f"[SCREENER REFRESH] last {days}d: {counts}")
    return counts
