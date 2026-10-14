"""
Results calendar — which companies are due to report, on both exchanges.

Two jobs:

1. **Fetch BSE's own results calendar.** `Corpforthresults/w` is BSE's
   "Forthcoming Results" list: scrip code, short name, company and meeting
   date, ~400 rows covering the next month. Before this the calendar had NSE's
   board-meeting API plus whatever BSE intimations could be parsed out of
   prose, which covered 25% of BSE filers. Rows are stored as `board_meeting`
   MarketEvents, the same shape the NSE calendar already uses, so the existing
   view, the earnings sync and the retention tier all pick them up unchanged.

   Called directly, not through `BSESession`: the worker proxy is pinned to the
   announcements endpoint and would answer this URL's query with announcements.

2. **Answer "does this company report today?"** for the exchange hub. A
   company on today's calendar that files something vague (Elecon on BSE at
   13:16 on 14 Oct 2026: "Outcome of the board meeting is attached.") has its
   PDF's first page read for the results, rather than waiting for a wordier
   copy on the other exchange.
"""
import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone, date
from typing import Dict, Iterable, List, Optional, Set

import requests
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.database import MarketEvent

logger = logging.getLogger("app.results_calendar")

IST = timezone(timedelta(hours=5, minutes=30))

BSE_RESULTS_CALENDAR_URL = "https://api.bseindia.com/BseIndiaAPI/api/Corpforthresults/w"

# The list changes a few times a day at most. Fetched from the 5-minute slow
# scraper, but only this often.
_BSE_CALENDAR_EVERY_SECONDS = 30 * 60
_bse_calendar_last = 0.0

_EARNINGS_WORDS = (
    "financial", "result", "quarterly", "audited", "unaudited",
    "q1", "q2", "q3", "q4", "earning", "profit", "loss",
)


def ist_today() -> date:
    return datetime.now(IST).date()


# ─── 1. BSE Forthcoming Results ─────────────────────────────────────────────

def _bse_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.bseindia.com/",
        "Origin": "https://www.bseindia.com",
    })
    try:
        s.get("https://www.bseindia.com/", timeout=8)
    except Exception as e:
        logger.debug(f"BSE cookie warm-up failed: {e}")
    return s


def _parse_bse_meeting_date(value: str) -> Optional[date]:
    """BSE writes "14 Oct 2026"; tolerate the other spellings it has used."""
    value = (value or "").strip()
    for fmt in ("%d %b %Y", "%d-%b-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d %B %Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def fetch_bse_results_calendar(db: Session, force: bool = False) -> int:
    """
    Store BSE's forthcoming-results list as board_meeting events.

    One row per (scrip code, meeting date); a rescheduled meeting is a new row
    and the calendar shows the nearest date. Returns the number of new rows.
    """
    global _bse_calendar_last
    now = time.monotonic()
    if not force and _bse_calendar_last and now - _bse_calendar_last < _BSE_CALENDAR_EVERY_SECONDS:
        return 0

    from app.services.deduplication import event_hash, is_duplicate_event
    from app.services import symbol_registry

    try:
        resp = _bse_session().get(BSE_RESULTS_CALENDAR_URL, timeout=15)
        resp.raise_for_status()
        text = resp.text.strip()
        rows = [] if text in ('"No Record Found!"', "No Record Found!") else resp.json()
    except Exception as e:
        logger.error(f"BSE results calendar fetch failed: {e}")
        return 0
    if isinstance(rows, dict):
        rows = rows.get("Table") or rows.get("data") or []
    if not isinstance(rows, list):
        logger.error(f"BSE results calendar returned {type(rows).__name__}, not a list")
        return 0

    # Only mark the fetch done once it has actually returned something usable,
    # so a failure is retried on the next 5-minute tick rather than in 30.
    _bse_calendar_last = now

    added = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        scrip_cd = str(row.get("scrip_Code") or row.get("SCRIP_CD") or "").strip()
        short_name = str(row.get("short_name") or "").strip().upper()
        long_name = str(row.get("Long_Name") or "").strip()
        m_date = _parse_bse_meeting_date(str(row.get("meeting_date") or ""))
        if not scrip_cd or not m_date:
            continue

        ident = symbol_registry.resolve(scrip_cd=scrip_cd, scrip_id=short_name)
        symbol = (ident.get("nse_symbol") or ident.get("symbol") or short_name or scrip_cd).upper()

        h = event_hash("bse", "results_calendar", f"{scrip_cd}:{m_date.isoformat()}", m_date.isoformat())
        if is_duplicate_event(db, h):
            continue

        raw = dict(row)
        raw["meeting_date_iso"] = m_date.isoformat()
        raw["purpose"] = "Financial Results"
        raw["calendar"] = "bse_forthcoming_results"
        display = m_date.strftime("%d-%b-%Y")
        event = MarketEvent(
            event_type="board_meeting",
            source="bse",
            symbol=symbol or None,
            company_name=ident.get("company_name") or long_name or None,
            title=f"{symbol}: Board Meeting — Financial Results",
            description=f"Financial Results | Scheduled: {display}",
            url=str(row.get("URL") or "") or None,
            raw_data=json.dumps(raw, default=str),
            event_hash=h,
            event_time=datetime.utcnow(),
            category="board_meeting",
        )
        try:
            db.add(event)
            db.commit()
            added += 1
        except Exception as e:
            db.rollback()
            logger.debug(f"BSE calendar row {scrip_cd} not saved: {e}")

    invalidate_today_cache()
    logger.info(f"BSE results calendar: {len(rows)} listed, {added} new")
    return added


# ─── 2. One calendar from both exchanges ────────────────────────────────────

def _parse_any_date(value) -> Optional[date]:
    if not value:
        return None
    value = str(value).strip()
    for fmt in ("%d-%b-%Y", "%d %b %Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y", "%b %d, %Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    m = re.search(r"(\d{1,2})[-/\s]+([A-Za-z]{3})[-/\s]+(\d{2,4})", value)
    if m:
        day, mon, year = m.groups()
        if len(year) == 2:
            year = "20" + year
        try:
            return datetime.strptime(f"{day}-{mon}-{year}", "%d-%b-%Y").date()
        except ValueError:
            pass
    return None


def _canonical(symbol: str) -> dict:
    """Registry record for a stored symbol, which may be a ticker or a scrip code."""
    symbol = (symbol or "").strip().upper()
    try:
        from app.services import symbol_registry
        rec = symbol_registry.resolve(
            nse_symbol=symbol, scrip_id=symbol,
            scrip_cd=symbol if symbol.isdigit() else "",
        )
    except Exception:
        rec = {"symbol": symbol}
    rec = dict(rec)
    rec["canonical"] = (rec.get("nse_symbol") or rec.get("symbol") or symbol).upper()
    return rec


def collect_results_calendar(db: Session, start: date, end: date) -> List[dict]:
    """
    Every company with a results board meeting between start and end (IST dates),
    merged across NSE and BSE into one row per company.

    The window and the "is this a results meeting" test are applied *before*
    choosing a row per company. Doing it after — as the view used to — meant a
    company whose first-loaded row was a fundraising meeting vanished from the
    calendar even though its results meeting was listed too, and "first loaded"
    was not "nearest", because the query has no order.
    """
    rows = db.query(MarketEvent).filter(
        or_(
            MarketEvent.event_type == "board_meeting",
            MarketEvent.category == "board_meeting",
            and_(MarketEvent.source == "bse",
                 MarketEvent.raw_data.like('%"SUBCATNAME": "Board Meeting%')),
        )
    ).all()

    by_company: Dict[str, dict] = {}
    for bm in rows:
        if not bm.symbol:
            continue
        raw = {}
        if bm.raw_data:
            try:
                raw = json.loads(bm.raw_data)
            except Exception:
                raw = {}

        m_date = _parse_any_date(
            raw.get("meeting_date_iso") or raw.get("bm_date") or raw.get("meetingDate")
            or raw.get("bm_dt") or raw.get("meeting_date")
        )
        if not m_date and bm.source == "bse":
            from app.services.nse_bse_scraper import extract_meeting_date
            m_date = extract_meeting_date(
                " ".join(str(raw.get(k) or "") for k in ("HEADLINE", "NEWSSUB"))
                + " " + (bm.title or "") + " " + (bm.description or "")
            )
        if not m_date:
            if bm.event_time and bm.event_time.date() >= start - timedelta(days=1):
                m_date = bm.event_time.date()
            else:
                continue
        if m_date < start or m_date > end:
            continue

        purpose = raw.get("bm_purpose") or raw.get("purpose") or bm.title or "Financial Results"
        if not any(k in str(purpose).lower() for k in _EARNINGS_WORDS):
            continue

        rec = _canonical(bm.symbol)
        key = rec["canonical"]
        exchange = (bm.source or "").upper()
        cur = by_company.get(key)
        if cur is None or m_date < cur["meeting_date"]:
            # The nearer date wins the row; what the other exchange said about
            # the same company is kept.
            merged_exchanges = set(cur["exchanges"]) if cur else set()
            merged_ids = set(cur["identifiers"]) if cur else set()
            cur = {
                "id": bm.id,
                "symbol": key,
                "company_name": rec.get("company_name") or bm.company_name or "",
                "meeting_date": m_date,
                "purpose": purpose,
                "title": bm.title or f"{key}: Board Meeting",
                "description": bm.description or purpose,
                "created_at": bm.created_at,
                "exchanges": merged_exchanges,
                "identifiers": merged_ids,
            }
            by_company[key] = cur
        if exchange:
            cur["exchanges"].add(exchange)
        for ident in (key, bm.symbol, rec.get("nse_symbol"), rec.get("bse_scrip_id"),
                      rec.get("bse_scrip_cd"), rec.get("isin"), raw.get("scrip_Code")):
            if ident:
                cur["identifiers"].add(str(ident).strip().upper())

    return sorted(by_company.values(), key=lambda r: (r["meeting_date"], r["symbol"]))


# ─── 3. "Does this company report today?" ───────────────────────────────────

_TODAY_TTL_SECONDS = 120
_today_lock = threading.Lock()
_today_cache: Dict[str, object] = {"day": None, "at": 0.0, "ids": frozenset()}


def invalidate_today_cache():
    with _today_lock:
        _today_cache["at"] = 0.0


def expected_today_identifiers(db: Session) -> Set[str]:
    """Every identifier of every company whose results meeting is today (IST)."""
    today = ist_today()
    now = time.monotonic()
    with _today_lock:
        if _today_cache["day"] == today and now - float(_today_cache["at"]) < _TODAY_TTL_SECONDS:
            return set(_today_cache["ids"])
    ids: Set[str] = set()
    try:
        for row in collect_results_calendar(db, today, today):
            ids |= row["identifiers"]
    except Exception as e:
        logger.warning(f"Results calendar for today unavailable: {e}")
        return set()
    with _today_lock:
        _today_cache.update({"day": today, "at": now, "ids": frozenset(ids)})
    return ids


def expects_results_today(db: Session, identifiers: Iterable[str]) -> bool:
    wanted = {str(i).strip().upper() for i in identifiers if i}
    if not wanted:
        return False
    return bool(wanted & expected_today_identifiers(db))
