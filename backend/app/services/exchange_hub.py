"""
Unified NSE/BSE corporate announcement hub.

This module is the *single owner* of the corporate-announcement endpoints.
Previously three independent loops hit the same two URLs — the intelligence
scraper, a second BSE pass inside the "other" scraper, and the trading poller —
which tripled the outbound request rate and let the same filing be processed
more than once.

One fetch per cycle now fans out, in latency order:

    1. Trading engine   — armed-target trigger matching (must be first)
    2. Financial results — cross-channel dedup, Telegram, auto-trading routing
    3. Intelligence feed — news-impact classification and persistence

Channel-1 (board-meeting outcome) announcements are processed before Channel-2
(direct result filings) within a cycle so the earlier-published channel always
claims the dedup key, per news_fetching_strategy.md.
"""
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.database import MarketEvent, ResultDedupKey
from app.services.announcement_classifier import (
    CHANNEL_BOARD_OUTCOME,
    classify,
    results_dedup_candidates,
    results_dedup_key,
)
from app.services.deduplication import event_hash, is_duplicate_event, to_iso_utc
from app.services.sse_manager import sse_manager

logger = logging.getLogger("app.exchange_hub")


# ─── Normalised announcement record ─────────────────────────────────────────

@dataclass
class Announcement:
    """One corporate announcement, normalised across NSE and BSE field names."""
    exchange: str                       # "nse" | "bse"
    symbol: str
    title: str
    description: str = ""
    url: str = ""
    pdf_filename: str = ""
    isin: str = ""
    scrip_code: str = ""
    category_name: str = ""             # BSE CategoryName, when present
    event_time: datetime = field(default_factory=datetime.utcnow)
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def event_date(self) -> str:
        return self.event_time.strftime("%Y-%m-%d")


# ─── Snapshot shared with the trading poller ────────────────────────────────

_snapshot: List[Announcement] = []
_snapshot_raw: List[dict] = []
_snapshot_at: float = 0.0
_snapshot_lock = threading.Lock()


def get_latest_snapshot() -> Tuple[List[dict], float]:
    """
    Most recent raw NSE announcement list and the monotonic time it was fetched.

    Lets the trading poller observe the hub's fetch instead of issuing its own
    request against the same endpoint.
    """
    with _snapshot_lock:
        return list(_snapshot_raw), _snapshot_at


def _publish_snapshot(announcements: List[Announcement], raw_nse: List[dict]):
    global _snapshot, _snapshot_raw, _snapshot_at
    with _snapshot_lock:
        _snapshot = announcements
        _snapshot_raw = raw_nse
        _snapshot_at = time.monotonic()


# ─── Fetchers ───────────────────────────────────────────────────────────────

def _fetch_nse_raw() -> List[dict]:
    """Fetch the NSE corporate-announcements feed. Returns raw dicts."""
    from app.services.intel_config import get_intel_config
    from app.services.nse_bse_scraper import _get_nse_session

    config = get_intel_config()
    source_config = config.nse_bse.get("sources", {}).get("corporate_announcements", {})
    url = source_config.get(
        "nse_url", "https://www.nseindia.com/api/corporate-announcements?index=equities"
    )

    data = _get_nse_session().get(url)
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    if isinstance(data, dict):
        items = data.get("data", data.get("announcements", []))
        if isinstance(items, list):
            return [d for d in items if isinstance(d, dict)]
    return []


def _fetch_bse_raw() -> List[dict]:
    """Fetch the BSE announcements feed. Returns raw dicts."""
    from app.services.intel_config import get_intel_config
    from app.services.nse_bse_scraper import _get_bse_session

    config = get_intel_config()
    source_config = config.nse_bse.get("sources", {}).get("corporate_announcements", {})
    url = source_config.get(
        "bse_url", "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
    )

    # BSE wants a same-day window here. A multi-day range (strPrevDate set to
    # yesterday) makes this endpoint return zero rows rather than a superset,
    # which is one of the reasons the feed looked dead.
    today = datetime.now().strftime("%Y%m%d")
    # strCat=-1 / subcategory=-1 pull every category in a single call; we
    # classify locally so a miscategorised filing is still caught
    # (news_fetching_strategy.md §4). pageno=1 returns the newest 50, which is
    # all a real-time poller needs.
    params = {
        "pageno": "1",
        "strCat": "-1",
        "subcategory": "-1",
        "strPrevDate": today,
        "strScrip": "",
        "strSearch": "P",
        "strToDate": today,
        "strType": "C",
    }

    data = _get_bse_session().get(url, params=params)
    if isinstance(data, dict):
        items = data.get("Table", data.get("data", []))
        if isinstance(items, list):
            return [d for d in items if isinstance(d, dict)]
    elif isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    return []


# ─── Normalisation ──────────────────────────────────────────────────────────

def _normalize_nse(raw: dict) -> Optional[Announcement]:
    from app.services.nse_bse_scraper import _safe_datetime

    symbol = str(
        raw.get("symbol") or raw.get("sm_name") or raw.get("company_name") or ""
    ).strip().upper()
    subject = str(
        raw.get("desc") or raw.get("subject") or raw.get("an_desc") or raw.get("attchmntText") or ""
    ).strip()
    if not subject:
        return None

    attachment = str(raw.get("attchmntFile") or raw.get("url") or "").strip()
    ann_date = str(
        raw.get("an_dt") or raw.get("bcastDate") or raw.get("broadcastDate")
        or raw.get("date") or raw.get("dt") or ""
    ).strip()

    return Announcement(
        exchange="nse",
        symbol=symbol,
        title=subject,
        description=str(raw.get("attchmntText") or raw.get("description") or subject),
        url=attachment,
        pdf_filename=attachment,
        isin=str(raw.get("isin") or "").strip().upper(),
        scrip_code="",
        category_name=str(raw.get("category") or "").strip(),
        event_time=_safe_datetime(ann_date),
        raw=raw,
    )


def _strip_bse_subject_prefix(newssub: str, long_name: str, scrip_code: str) -> str:
    """
    Pull the actual subject out of BSE's NEWSSUB field.

    BSE prefixes it with the company name and scrip code, e.g.
    "Jindal Poly Investment and Finance Company Ltd - 536773 - Announcement
    under Regulation 30". Only the tail is the subject, and leaving the prefix
    in place skews keyword classification and makes feed titles unreadable.
    """
    subject = (newssub or "").strip()
    if not subject:
        return subject

    # Drop a leading "<company> - <scrip> - " prefix when both parts match.
    parts = [p.strip() for p in subject.split(" - ")]
    if len(parts) >= 3:
        name_matches = long_name and parts[0].lower().startswith(long_name.lower()[:20])
        code_matches = scrip_code and parts[1] == str(scrip_code)
        if name_matches or code_matches:
            return " - ".join(parts[2:]).strip() or subject
    return subject


def _normalize_bse(raw: dict, db: Session) -> Optional[Announcement]:
    from app.services.nse_bse_scraper import _safe_datetime, resolve_bse_symbol

    scrip_code = str(raw.get("SCRIP_CD") or raw.get("scrip_cd") or "").strip()
    long_name = str(raw.get("SLONGNAME") or "").strip()
    # AnnSubCategoryGetData does not return ISIN_CODE, so the ticker resolved
    # from the scrip code / company name is the identifier BSE and NSE share.
    isin = str(raw.get("ISIN_CODE") or "").strip().upper()

    subject = _strip_bse_subject_prefix(
        str(raw.get("NEWSSUB") or raw.get("HEADLINE") or "").strip(), long_name, scrip_code
    )
    if not subject:
        return None

    symbol = resolve_bse_symbol(scrip_code, long_name, isin, db)

    attachment_name = str(raw.get("ATTACHMENTNAME") or "").strip()
    attachment_url = (
        f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{attachment_name}"
        if attachment_name else ""
    )

    ann_date = str(
        raw.get("NEWS_DT") or raw.get("DissemDT") or raw.get("DT_TM") or ""
    ).strip()

    # SUBCATNAME ("Outcome of Board Meeting", "Change in Management") is far more
    # specific than CATEGORYNAME ("Company Update") and maps directly onto the
    # subcategory column in news_fetching_strategy.md.
    subcat = str(raw.get("SUBCATNAME") or "").strip()
    category_name = subcat or str(raw.get("CATEGORYNAME") or "").strip()

    return Announcement(
        exchange="bse",
        symbol=(symbol or "").strip().upper(),
        title=subject,
        description=str(raw.get("MORE") or raw.get("HEADLINE") or subject),
        url=attachment_url,
        pdf_filename=attachment_name,
        isin=isin,
        scrip_code=scrip_code,
        category_name=category_name,
        event_time=_safe_datetime(ann_date),
        raw=raw,
    )


# ─── Cross-channel financial-results dedup ──────────────────────────────────

def _claim_result_key(db: Session, ann: Announcement, channel: str) -> Optional[str]:
    """
    Try to claim this filing on behalf of the first channel that saw it.

    Every candidate key is written, not just the canonical one, so a later
    arrival that only shares *one* identifier (NSE knows the ticker, BSE knows
    the ISIN) still collides. Returns the canonical key, or None if another
    channel already captured this result — in which case the filing is dropped.
    """
    candidates = results_dedup_candidates(
        ann.symbol, ann.isin, ann.scrip_code, ann.pdf_filename, ann.event_date
    )
    if not candidates:
        return None

    existing = (
        db.query(ResultDedupKey.key)
        .filter(ResultDedupKey.key.in_(candidates))
        .first()
    )
    if existing:
        logger.debug(
            f"[RESULT DEDUP] {ann.symbol} already captured via '{existing[0]}' — skipping {ann.exchange}"
        )
        return None

    canonical = candidates[0]
    try:
        for key in candidates:
            db.add(ResultDedupKey(
                key=key,
                symbol=ann.symbol or None,
                isin=ann.isin or None,
                first_source=ann.exchange,
                channel=channel,
                result_date=ann.event_date,
            ))
        db.commit()
        return canonical
    except Exception as e:
        # Another cycle claimed an overlapping key between our check and insert.
        db.rollback()
        logger.debug(f"[RESULT DEDUP] claim for '{canonical}' lost a race: {e}")
        return None


# ─── Persistence ────────────────────────────────────────────────────────────

def _broadcast_event(event: MarketEvent):
    sse_manager.broadcast("new_event", {
        "id": f"event_{event.id}",
        "type": "event",
        "event_type": event.event_type,
        "source": event.source,
        "symbol": event.symbol,
        "title": event.title,
        "description": event.description,
        "url": event.url,
        "time": to_iso_utc(event.event_time),
        "category": event.category,
        "ai_sentiment": event.ai_sentiment,
        "ai_impact_score": event.ai_impact_score,
        "ai_summary": event.ai_summary,
        "ai_provider": event.ai_provider,
    })


def _store_announcement(db: Session, ann: Announcement, classification: dict) -> Optional[MarketEvent]:
    """Persist one announcement as a MarketEvent. Returns the event, or None if duplicate."""
    h = event_hash(ann.exchange, "announcement", f"{ann.symbol}:{ann.title}", ann.event_time.isoformat())
    if is_duplicate_event(db, h):
        return None

    event = MarketEvent(
        event_type="announcement",
        source=ann.exchange,
        symbol=ann.symbol or None,
        title=ann.title[:500],
        description=ann.description,
        url=ann.url,
        raw_data=json.dumps(ann.raw, default=str),
        event_hash=h,
        event_time=ann.event_time,
        category=classification["category"],
    )

    try:
        db.add(event)
        db.commit()
        return event
    except Exception as e:
        db.rollback()
        logger.warning(f"Failed to save {ann.exchange} announcement {ann.symbol}: {e}")
        return None


# ─── Main poll cycle ────────────────────────────────────────────────────────

def poll_exchange_announcements(db: Session) -> Dict[str, int]:
    """
    Run one unified NSE+BSE announcement cycle.

    Returns counts of what happened: fetched, results, general, duplicates.
    """
    from app.services.intel_config import get_intel_config

    config = get_intel_config()
    if not config.nse_bse.get("enabled", True):
        return {}

    stats = {"fetched": 0, "results": 0, "general": 0, "result_duplicates": 0}

    # ── 1. Fetch both exchanges ──
    try:
        raw_nse = _fetch_nse_raw()
    except Exception as e:
        logger.error(f"NSE announcement fetch failed: {e}")
        raw_nse = []

    try:
        raw_bse = _fetch_bse_raw()
    except Exception as e:
        logger.error(f"BSE announcement fetch failed: {e}")
        raw_bse = []

    announcements: List[Announcement] = []
    for raw in raw_nse:
        try:
            ann = _normalize_nse(raw)
            if ann:
                announcements.append(ann)
        except Exception as e:
            logger.debug(f"NSE normalise error: {e}")
    for raw in raw_bse:
        try:
            ann = _normalize_bse(raw, db)
            if ann:
                announcements.append(ann)
        except Exception as e:
            logger.debug(f"BSE normalise error: {e}")

    stats["fetched"] = len(announcements)

    # ── 2. Publish snapshot + hand to the trading engine first (lowest latency) ──
    _publish_snapshot(announcements, raw_nse)
    try:
        from app.services.trade_nse_poller import handle_announcements
        handle_announcements(raw_nse)
    except Exception as e:
        logger.error(f"Trading engine dispatch failed: {e}")

    # ── 3. Classify, then order Channel 1 ahead of Channel 2 ──
    classified: List[Tuple[Announcement, dict]] = []
    for ann in announcements:
        try:
            classified.append((ann, classify(ann.title, ann.description, ann.category_name)))
        except Exception as e:
            logger.debug(f"Classification error for '{ann.title[:60]}': {e}")

    def channel_rank(item: Tuple[Announcement, dict]) -> int:
        # Board-meeting outcomes are published first in practice, so let them
        # claim the dedup key ahead of the explicit result filing.
        return 0 if item[1].get("channel") == CHANNEL_BOARD_OUTCOME else 1

    classified.sort(key=lambda item: (channel_rank(item), item[0].event_time))

    # ── 4. Persist and route ──
    for ann, cls in classified:
        try:
            if cls["is_financial_result"]:
                key = _claim_result_key(db, ann, cls["channel"])
                if not key:
                    stats["result_duplicates"] += 1
                    continue

                event = _store_announcement(db, ann, cls)
                if not event:
                    continue

                # Financial results are owned by the auto-trading path: it marks
                # the event, fires Telegram, and either triggers an armed config
                # or raises an order prompt.
                try:
                    from app.services.results_router import route_financial_result
                    route_financial_result(db, ann, event, key)
                except Exception as e:
                    logger.error(f"Financial-results routing failed for {ann.symbol}: {e}")

                _broadcast_event(event)
                stats["results"] += 1
            else:
                event = _store_announcement(db, ann, cls)
                if not event:
                    continue

                # Everything that is not a result goes to the AI Intelligence
                # news-impact pipeline.
                try:
                    from app.services.ai_analyzer import apply_news_impact_classification
                    apply_news_impact_classification(event)
                    db.commit()
                except Exception as e:
                    db.rollback()
                    logger.debug(f"News-impact classification failed for {ann.symbol}: {e}")

                _broadcast_event(event)
                stats["general"] += 1
        except Exception as e:
            logger.error(f"Error processing announcement '{ann.title[:60]}': {e}")
            continue

    if stats["results"] or stats["general"]:
        logger.info(
            f"Exchange hub: {stats['fetched']} fetched → "
            f"{stats['results']} results, {stats['general']} general, "
            f"{stats['result_duplicates']} cross-channel duplicates suppressed"
        )

    return stats
