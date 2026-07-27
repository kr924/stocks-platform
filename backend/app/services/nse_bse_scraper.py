"""
NSE/BSE Data Scraper — Corporate Announcements, Bulk/Block Deals, Board Meetings, Insider Trading.

Uses NSE's internal JSON APIs with proper session/cookie management to avoid 403s.
Stores results in the MarketEvent table with deduplication via event_hash.
"""
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any

import requests
from sqlalchemy.orm import Session

from app.database import MarketEvent, get_db
from app.services.intel_config import get_intel_config
from app.services.deduplication import event_hash, is_duplicate_event, to_iso_utc
from app.services.sse_manager import sse_manager

logger = logging.getLogger("app.nse_bse_scraper")


class NSESession:
    """
    Maintains a requests.Session with valid NSE cookies.
    NSE requires a session cookie obtained by visiting the homepage first.
    """
    
    def __init__(self):
        self.session = requests.Session()
        self.config = get_intel_config()
        self._last_cookie_refresh = 0
        self._setup_headers()
    
    def _setup_headers(self):
        headers = self.config.nse_bse.get("headers", {})
        self.session.headers.update(headers)
    
    def _refresh_cookies(self):
        """Visit NSE homepage to obtain session cookies with browser-like HTML headers."""
        now = time.time()
        refresh_interval = self.config.nse_bse.get("cookie_refresh_interval", 300)
        if now - self._last_cookie_refresh < refresh_interval:
            return
        
        homepage_headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.google.com/",
        }
        try:
            # Visit the homepage to get cookies
            resp = self.session.get(
                "https://www.nseindia.com",
                headers=homepage_headers,
                timeout=10,
                allow_redirects=True
            )
            resp.raise_for_status()
            self._last_cookie_refresh = now
            logger.debug("NSE cookies refreshed successfully")
        except Exception as e:
            logger.debug(f"Failed to refresh NSE cookies: {e}")
    
    def get(self, url: str, params: dict = None, timeout: int = 15) -> Optional[dict]:
        """Make a GET request to NSE API with proper cookie management."""
        self._refresh_cookies()
        try:
            resp = self.session.get(url, params=params, timeout=timeout)
            if resp.status_code == 403:
                # Force cookie refresh and retry
                self._last_cookie_refresh = 0
                self._refresh_cookies()
                resp = self.session.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.JSONDecodeError:
            logger.error(f"Non-JSON response from {url}: {resp.text[:200]}")
            return None
        except Exception as e:
            logger.error(f"NSE API error for {url}: {e}")
            return None


class BSESession:
    """Simple session for BSE API calls."""
    
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
            "Referer": "https://www.bseindia.com/",
        })
    
    def get(self, url: str, params: dict = None, timeout: int = 15) -> Optional[Any]:
        """Make a GET request to BSE API."""
        try:
            resp = self.session.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"BSE API error for {url}: {e}")
            return None


# Module-level session instances (reused across polls)
_nse_session: Optional[NSESession] = None
_bse_session: Optional[BSESession] = None


def _get_nse_session() -> NSESession:
    global _nse_session
    if _nse_session is None:
        _nse_session = NSESession()
    return _nse_session


def _get_bse_session() -> BSESession:
    global _bse_session
    if _bse_session is None:
        _bse_session = BSESession()
    return _bse_session


def _safe_datetime(date_str: str, formats: List[str] = None) -> datetime:
    """Parse a date string trying multiple formats and return as UTC (converting from IST)."""
    from datetime import timedelta
    if not date_str:
        return datetime.utcnow()
    formats = formats or [
        "%d-%b-%Y %H:%M:%S",
        "%d-%b-%Y",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%d-%m-%Y",
    ]
    for fmt in formats:
        try:
            # All NSE/BSE dates are in Indian Standard Time (IST = UTC+5:30)
            local_time = datetime.strptime(date_str.strip(), fmt)
            return local_time - timedelta(hours=5, minutes=30)
        except (ValueError, AttributeError):
            continue
    return datetime.utcnow()


def _classify_event_category(event_type: str, title: str) -> str:
    """Auto-classify a market event into a category based on event_type and title keywords."""
    t = title.lower()
    et = event_type.lower()
    
    if et == "board_meeting":
        if any(kw in t for kw in ["financial result", "quarterly", "annual", "dividend"]):
            return "earnings"
        return "board_meeting"
    if et in ("bulk_deal", "block_deal"):
        return "bulk_deal"
    if et == "insider_trade":
        return "insider_trade"
    if any(kw in t for kw in ["sebi", "depositories", "depository", "regulation"]):
        return "sebi_filing"
    if any(kw in t for kw in ["financial result", "quarterly result", "annual result", "dividend", "earnings"]):
        return "earnings"
    if any(kw in t for kw in ["buyback", "bonus", "split", "merger", "amalgamation", "scheme of arrangement", "rights issue"]):
        return "corporate_action"
    if any(kw in t for kw in ["credit rating", "rating"]):
        return "credit_rating"
    if any(kw in t for kw in ["board meeting", "board"]):
        return "board_meeting"
    return "general"


# ─── Corporate Announcements ───────────────────────────────────────────────

def fetch_corporate_announcements(db: Session) -> int:
    """Fetch latest corporate announcements from NSE. Returns count of new events."""
    config = get_intel_config()
    if not config.is_source_enabled("nse_bse", "corporate_announcements"):
        return 0
    
    nse = _get_nse_session()
    source_config = config.nse_bse.get("sources", {}).get("corporate_announcements", {})
    url = source_config.get("nse_url", "https://www.nseindia.com/api/corporate-announcements?index=equities")
    
    data = nse.get(url)
    if not data:
        return 0
    
    announcements = data if isinstance(data, list) else data.get("data", data.get("announcements", []))
    if not isinstance(announcements, list):
        logger.warning(f"Unexpected announcements format: {type(announcements)}")
        return 0
    
    count = 0
    for ann in announcements:
        try:
            symbol = ann.get("symbol", ann.get("sm_name", "")).strip().upper()
            subject = ann.get("desc", ann.get("subject", ann.get("an_desc", ""))).strip()
            if not subject:
                continue
            
            ann_date = ann.get("an_dt", ann.get("date", ann.get("dt", "")))
            evt_time = _safe_datetime(ann_date)
            
            # Generate dedup hash
            h = event_hash("nse", "announcement", subject, evt_time.isoformat())
            if is_duplicate_event(db, h):
                continue
            
            event = MarketEvent(
                event_type="announcement",
                source="nse",
                symbol=symbol or None,
                title=subject[:500],
                description=ann.get("attchmntText", ann.get("description", subject)),
                url=ann.get("attchmntFile", ann.get("url", "")),
                raw_data=json.dumps(ann, default=str),
                event_hash=h,
                event_time=evt_time,
                category=_classify_event_category("announcement", subject),
            )
            try:
                db.add(event)
                db.commit()
                count += 1
                # Broadcast to SSE clients
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
                })
            except Exception as inner_err:
                db.rollback()
                logger.warning(f"Failed to save announcement {symbol}: {inner_err}")
        except Exception as e:
            logger.error(f"Error processing announcement: {e}")
            continue
    
    if count > 0:
        logger.info(f"Saved {count} new corporate announcements from NSE")
    return count


# ─── Bulk/Block Deals ──────────────────────────────────────────────────────

def fetch_bulk_block_deals(db: Session) -> int:
    """Fetch latest bulk and block deals from NSE. Returns count of new events."""
    config = get_intel_config()
    if not config.is_source_enabled("nse_bse", "bulk_block_deals"):
        return 0
    
    nse = _get_nse_session()
    source_config = config.nse_bse.get("sources", {}).get("bulk_block_deals", {})
    url = source_config.get("nse_url", "https://www.nseindia.com/api/snapshot-capital-market-largedeal")
    
    data = nse.get(url)
    if not data:
        return 0
    
    # NSE returns separate arrays for bulk and block deals
    all_deals = []
    if isinstance(data, dict):
        for deal_type in ["BULK_DEALS", "BLOCK_DEALS", "bulkDeals", "blockDeals"]:
            deals = data.get(deal_type, [])
            if isinstance(deals, list):
                for d in deals:
                    d["_deal_category"] = "bulk_deal" if "BULK" in deal_type.upper() or "bulk" in deal_type else "block_deal"
                all_deals.extend(deals)
    elif isinstance(data, list):
        all_deals = data
    
    count = 0
    for deal in all_deals:
        try:
            symbol = deal.get("symbol", deal.get("sm_name", "")).strip().upper()
            client = deal.get("clientName", deal.get("client_name", deal.get("name", "Unknown")))
            qty = deal.get("quantity", deal.get("qty", ""))
            price = deal.get("price", deal.get("avgPrice", deal.get("watp", "")))
            trade_type = deal.get("buySell", deal.get("buy_sell", deal.get("action", "")))
            deal_category = deal.get("_deal_category", "bulk_deal")
            
            title = f"{symbol}: {client} {'bought' if str(trade_type).upper() in ('BUY', 'B') else 'sold'} {qty} shares @ ₹{price}"
            
            deal_date = deal.get("dealDate", deal.get("date", deal.get("dt", "")))
            evt_time = _safe_datetime(deal_date)
            
            h = event_hash("nse", deal_category, title, evt_time.isoformat())
            if is_duplicate_event(db, h):
                continue
            
            event = MarketEvent(
                event_type=deal_category,
                source="nse",
                symbol=symbol or None,
                title=title,
                description=f"Client: {client} | Quantity: {qty} | Price: ₹{price} | Type: {trade_type}",
                raw_data=json.dumps(deal, default=str),
                event_hash=h,
                event_time=evt_time,
                category=_classify_event_category(deal_category, title),
            )
            try:
                db.add(event)
                db.commit()
                count += 1
                # Broadcast to SSE clients
                sse_manager.broadcast("new_event", {
                    "id": f"event_{event.id}",
                    "type": "event",
                    "event_type": event.event_type,
                    "source": event.source,
                    "symbol": event.symbol,
                    "title": event.title,
                    "description": event.description,
                    "time": to_iso_utc(event.event_time),
                    "category": event.category,
                })
            except Exception as inner_err:
                db.rollback()
                logger.warning(f"Failed to save deal {symbol}: {inner_err}")
        except Exception as e:
            logger.error(f"Error processing deal: {e}")
            continue
    
    if count > 0:
        logger.info(f"Saved {count} new bulk/block deals from NSE")
    return count


# ─── Board Meetings ────────────────────────────────────────────────────────

def fetch_board_meetings(db: Session) -> int:
    """Fetch upcoming/recent board meeting schedules from NSE. Returns count of new events."""
    config = get_intel_config()
    if not config.is_source_enabled("nse_bse", "board_meetings"):
        return 0
    
    nse = _get_nse_session()
    source_config = config.nse_bse.get("sources", {}).get("board_meetings", {})
    base_url = source_config.get("nse_url", "https://www.nseindia.com/api/corporate-board-meetings?index=equities")
    
    # Construct rolling from_date and to_date query parameters if not custom-specified
    if "from_date" not in base_url and "to_date" not in base_url:
        from_date_str = (datetime.utcnow() - timedelta(days=7)).strftime("%d-%m-%Y")
        to_date_str = (datetime.utcnow() + timedelta(days=30)).strftime("%d-%m-%Y")
        sep = "&" if "?" in base_url else "?"
        url = f"{base_url}{sep}from_date={from_date_str}&to_date={to_date_str}"
    else:
        url = base_url
        
    data = nse.get(url)
    if not data:
        return 0
    
    meetings = data if isinstance(data, list) else data.get("data", data.get("boardMeetings", []))
    if not isinstance(meetings, list):
        return 0
    
    count = 0
    for meeting in meetings:
        try:
            symbol = meeting.get("bm_symbol", meeting.get("symbol", meeting.get("sm_name", ""))).strip().upper()
            purpose = meeting.get("purpose", meeting.get("bm_purpose", meeting.get("agenda", "Board Meeting"))).strip()
            # Use filing timestamp (when the intimation was submitted) as event_time
            filing_timestamp = meeting.get("bm_timestamp", meeting.get("sysTime", ""))
            # Scheduled meeting date (future) — include in description only
            scheduled_date = meeting.get("meetingDate", meeting.get("bm_date", meeting.get("bm_dt", "")))
            
            if not purpose:
                continue
            
            title = f"{symbol}: Board Meeting — {purpose}"
            # Use filing timestamp for chronological sorting; fall back to scheduled date
            evt_time = _safe_datetime(filing_timestamp) if filing_timestamp else _safe_datetime(scheduled_date)
            
            description = f"{purpose} | Scheduled: {scheduled_date}" if scheduled_date else purpose
            
            h = event_hash("nse", "board_meeting", title, evt_time.isoformat())
            if is_duplicate_event(db, h):
                continue
            
            event = MarketEvent(
                event_type="board_meeting",
                source="nse",
                symbol=symbol or None,
                title=title,
                description=description,
                raw_data=json.dumps(meeting, default=str),
                event_hash=h,
                event_time=evt_time,
                category=_classify_event_category("board_meeting", title),
            )
            try:
                db.add(event)
                db.commit()
                count += 1
                # Broadcast to SSE clients
                sse_manager.broadcast("new_event", {
                    "id": f"event_{event.id}",
                    "type": "event",
                    "event_type": event.event_type,
                    "source": event.source,
                    "symbol": event.symbol,
                    "title": event.title,
                    "description": event.description,
                    "time": to_iso_utc(event.event_time),
                    "category": event.category,
                })
            except Exception as inner_err:
                db.rollback()
                logger.warning(f"Failed to save board meeting {symbol}: {inner_err}")
        except Exception as e:
            logger.error(f"Error processing board meeting: {e}")
            continue
    
    if count > 0:
        logger.info(f"Saved {count} new board meetings from NSE")
    return count


# ─── Insider Trading (SAST/PIT) ────────────────────────────────────────────

def fetch_insider_trading(db: Session) -> int:
    """Fetch insider trading (Prohibition of Insider Trading) data from NSE. Returns count of new events."""
    config = get_intel_config()
    if not config.is_source_enabled("nse_bse", "insider_trading"):
        return 0
    
    nse = _get_nse_session()
    source_config = config.nse_bse.get("sources", {}).get("insider_trading", {})
    url = source_config.get("nse_url", "https://www.nseindia.com/api/corporates-pit")
    
    data = nse.get(url)
    if not data:
        return 0
    
    trades = data if isinstance(data, list) else data.get("data", data.get("insiderTrading", []))
    if not isinstance(trades, list):
        return 0
    
    count = 0
    for trade in trades:
        try:
            symbol = trade.get("symbol", trade.get("company", "")).strip().upper()
            person = trade.get("acqName", trade.get("personName", trade.get("name", "Unknown")))
            person_cat = trade.get("personCategory", trade.get("category", ""))
            action = trade.get("acqMode", trade.get("transactionType", trade.get("buySell", "")))
            qty = trade.get("secAcq", trade.get("noOfSecurities", trade.get("quantity", "")))
            
            title = f"{symbol}: Insider {action} by {person} ({person_cat}) — {qty} shares"
            
            trade_date = trade.get("acqfromDt", trade.get("date", trade.get("intimationDate", "")))
            evt_time = _safe_datetime(trade_date)
            
            h = event_hash("nse", "insider_trade", title, evt_time.isoformat())
            if is_duplicate_event(db, h):
                continue
            
            event = MarketEvent(
                event_type="insider_trade",
                source="nse",
                symbol=symbol or None,
                title=title,
                description=f"Person: {person} | Category: {person_cat} | Action: {action} | Quantity: {qty}",
                raw_data=json.dumps(trade, default=str),
                event_hash=h,
                event_time=evt_time,
                category=_classify_event_category("insider_trade", title),
            )
            try:
                db.add(event)
                db.commit()
                count += 1
                # Broadcast to SSE clients
                sse_manager.broadcast("new_event", {
                    "id": f"event_{event.id}",
                    "type": "event",
                    "event_type": event.event_type,
                    "source": event.source,
                    "symbol": event.symbol,
                    "title": event.title,
                    "description": event.description,
                    "time": to_iso_utc(event.event_time),
                    "category": event.category,
                })
            except Exception as inner_err:
                db.rollback()
                logger.warning(f"Failed to save insider trade {symbol}: {inner_err}")
        except Exception as e:
            logger.error(f"Error processing insider trade: {e}")
            continue
    
    if count > 0:
        logger.info(f"Saved {count} new insider trading events from NSE")
    return count


# ─── BSE Announcements ─────────────────────────────────────────────────────

def fetch_bse_announcements(db: Session) -> int:
    """Fetch latest announcements from BSE. Returns count of new events."""
    config = get_intel_config()
    if not config.nse_bse.get("enabled", True):
        return 0
    
    bse = _get_bse_session()
    source_config = config.nse_bse.get("sources", {}).get("corporate_announcements", {})
    url = source_config.get("bse_url", "https://api.bseindia.com/BseIndiaAPI/api/AnnGetData/w")
    
    # BSE API parameters
    today = datetime.now().strftime("%Y%m%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
    params = {
        "strCat": "-1",       # All categories
        "strPrevDate": yesterday,
        "strScrip": "",
        "strSearch": "P",
        "strToDate": today,
        "strType": "C",       # Company
    }
    
    data = bse.get(url, params=params)
    if not data or not isinstance(data, dict):
        return 0
    
    table = data.get("Table", [])
    if not isinstance(table, list):
        return 0
    
    count = 0
    for ann in table:
        try:
            symbol = ann.get("SLONGNAME", ann.get("SCRIP_CD", "")).strip()
            headline = ann.get("HEADLINE", ann.get("NEWS_SUBJECT", "")).strip()
            if not headline:
                continue
            
            ann_date = ann.get("NEWS_DT", ann.get("DT_TM", ""))
            evt_time = _safe_datetime(ann_date)
            
            h = event_hash("bse", "announcement", headline, evt_time.isoformat())
            if is_duplicate_event(db, h):
                continue
            
            attachment_url = ""
            if ann.get("ATTACHMENTNAME"):
                attachment_url = f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{ann['ATTACHMENTNAME']}"
            
            event = MarketEvent(
                event_type="announcement",
                source="bse",
                symbol=symbol or None,
                title=headline[:500],
                description=ann.get("NEWSSUB", headline),
                url=attachment_url,
                raw_data=json.dumps(ann, default=str),
                event_hash=h,
                event_time=evt_time,
            )
            try:
                db.add(event)
                db.commit()
                count += 1
                # Broadcast to SSE clients
                sse_manager.broadcast("new_event", {
                    "id": f"event_{event.id}",
                    "type": "event",
                    "event_type": event.event_type,
                    "source": event.source,
                    "symbol": event.symbol,
                    "title": event.title,
                    "description": event.description,
                    "time": to_iso_utc(event.event_time),
                    "category": event.category,
                })
            except Exception as inner_err:
                db.rollback()
                logger.warning(f"Failed to save BSE announcement {symbol}: {inner_err}")
        except Exception as e:
            logger.error(f"Error processing BSE announcement: {e}")
            continue
    
    if count > 0:
        logger.info(f"Saved {count} new announcements from BSE")
    return count


# ─── Financial Results ──────────────────────────────────────────────────────

def fetch_financial_results(db: Session) -> int:
    """Fetch quarterly/annual financial results from NSE. Returns count of new events."""
    config = get_intel_config()
    if not config.is_source_enabled("nse_bse", "financial_results"):
        return 0
    
    nse = _get_nse_session()
    source_config = config.nse_bse.get("sources", {}).get("financial_results", {})
    url = source_config.get("nse_url", "https://www.nseindia.com/api/corporates-financial-results?index=equities")
    
    data = nse.get(url)
    if not data:
        return 0
    
    results = data if isinstance(data, list) else data.get("data", data.get("results", []))
    if not isinstance(results, list):
        return 0
    
    count = 0
    for result in results:
        try:
            symbol = result.get("symbol", result.get("sm_name", "")).strip().upper()
            period = result.get("period", result.get("re_forperiod", ""))
            broadcat = result.get("broadcastDt", result.get("date", ""))
            
            title = f"{symbol}: Financial Results for {period}"
            evt_time = _safe_datetime(broadcat)
            
            h = event_hash("nse", "result", title, evt_time.isoformat())
            if is_duplicate_event(db, h):
                continue
            
            xbrl_link = result.get("xbrl", result.get("re_xbrl", ""))
            
            event = MarketEvent(
                event_type="result",
                source="nse",
                symbol=symbol or None,
                title=title,
                description=f"Period: {period} | Consolidated: {result.get('consolidated', 'N/A')}",
                url=xbrl_link,
                raw_data=json.dumps(result, default=str),
                event_hash=h,
                event_time=evt_time,
            )
            try:
                db.add(event)
                db.commit()
                count += 1
                # Broadcast to SSE clients
                sse_manager.broadcast("new_event", {
                    "id": f"event_{event.id}",
                    "type": "event",
                    "event_type": event.event_type,
                    "source": event.source,
                    "symbol": event.symbol,
                    "title": event.title,
                    "description": event.description,
                    "time": to_iso_utc(event.event_time),
                    "category": event.category,
                })
            except Exception as inner_err:
                db.rollback()
                logger.warning(f"Failed to save financial result {symbol}: {inner_err}")
        except Exception as e:
            logger.error(f"Error processing financial result: {e}")
            continue
    
    if count > 0:
        logger.info(f"Saved {count} new financial results from NSE")
    return count


# ─── Aggregate Fetch ────────────────────────────────────────────────────────

def fetch_all_nse_bse(db: Session) -> Dict[str, int]:
    """
    Run all NSE/BSE scrapers. Returns a dict of {source: new_count}.
    Called by the background scheduler.
    """
    config = get_intel_config()
    if not config.nse_bse.get("enabled", True):
        return {}
    
    results = {}
    
    try:
        results["announcements"] = fetch_corporate_announcements(db)
    except Exception as e:
        logger.error(f"Corporate announcements fetch failed: {e}")
        results["announcements"] = 0
    
    try:
        results["bulk_block_deals"] = fetch_bulk_block_deals(db)
    except Exception as e:
        logger.error(f"Bulk/block deals fetch failed: {e}")
        results["bulk_block_deals"] = 0
    
    try:
        results["board_meetings"] = fetch_board_meetings(db)
    except Exception as e:
        logger.error(f"Board meetings fetch failed: {e}")
        results["board_meetings"] = 0
    
    try:
        results["insider_trading"] = fetch_insider_trading(db)
    except Exception as e:
        logger.error(f"Insider trading fetch failed: {e}")
        results["insider_trading"] = 0
    
    try:
        results["bse_announcements"] = fetch_bse_announcements(db)
    except Exception as e:
        logger.error(f"BSE announcements fetch failed: {e}")
        results["bse_announcements"] = 0
    
    try:
        results["financial_results"] = fetch_financial_results(db)
    except Exception as e:
        logger.error(f"Financial results fetch failed: {e}")
        results["financial_results"] = 0
    
    total = sum(results.values())
    if total > 0:
        logger.info(f"NSE/BSE scrape complete: {total} new events — {results}")
    
    return results
