"""
NSE/BSE Data Scraper — Corporate Announcements, Bulk/Block Deals, Board Meetings, Insider Trading.

Uses NSE's internal JSON APIs with proper session/cookie management to avoid 403s.
Stores results in the MarketEvent table with deduplication via event_hash.
"""
import json
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any

import requests
from sqlalchemy.orm import Session

from app.database import MarketEvent, get_db
from app.services.intel_config import get_intel_config
from app.services.deduplication import event_hash, is_duplicate_event, to_iso_utc
from app.services.sse_manager import sse_manager

# Lookup indexes over the NSE equity list, built once and reused. Resolving a
# BSE symbol used to linear-scan ~2000 equities twice per announcement, which at
# a few hundred announcements per cycle was pure wasted CPU.
_bse_isin_index: Optional[Dict[str, str]] = None
_bse_name_index: Optional[Dict[str, str]] = None


_COMPANY_SUFFIXES = (
    "LIMITED", "LTD", "PRIVATE", "PVT", "CORPORATION", "CORP",
    "INCORPORATED", "INC", "COMPANY",
)


def _norm_company_name(name: str) -> str:
    """
    Reduce a company name to a form that matches across exchanges.

    BSE and NSE spell the same registrant differently — "Tata Consultancy
    Services Ltd" versus "TATA CONSULTANCY SERVICES LIMITED" — so a literal
    comparison misses dual-listed companies. Since BSE's current endpoint
    returns no ISIN, the company name is the only join left, and a miss here
    means the same financial result gets processed twice, once per exchange.
    """
    if not name:
        return ""
    collapsed = re.sub(r"[^A-Za-z0-9]+", "", name).upper()
    # Strip trailing legal-form suffixes, repeatedly ("... PVT LTD").
    changed = True
    while changed:
        changed = False
        for suffix in _COMPANY_SUFFIXES:
            if collapsed.endswith(suffix) and len(collapsed) > len(suffix) + 3:
                collapsed = collapsed[: -len(suffix)]
                changed = True
    return collapsed


# A shared prefix must be at least this long, and cover at least this much of
# the shorter name, to count as the same company. Tuned so "TATACONSULTANCYSERV"
# joins the two spellings of TCS while "BAJAJFIN" does not merge Bajaj Finance
# with Bajaj Finserv.
_PREFIX_MIN_CHARS = 10
_PREFIX_MIN_RATIO = 0.85


def _best_prefix_match(norm: str, name_index: Dict[str, str]) -> Optional[str]:
    """
    Find the NSE symbol whose normalised name shares the longest prefix with
    `norm`, provided the overlap is decisive.

    Exact and simple prefix matching is not enough because the NSE instrument
    dump truncates names to roughly 24 characters and mangles the suffix in the
    process: TCS is listed as "TATA CONSULTANCY SERV LT", which neither equals
    nor prefixes BSE's "Tata Consultancy Services Ltd".
    """
    best_symbol = None
    best_len = 0
    best_gap = None
    for eq_name, symbol in name_index.items():
        limit = min(len(norm), len(eq_name))
        if limit < _PREFIX_MIN_CHARS:
            continue
        i = 0
        while i < limit and norm[i] == eq_name[i]:
            i += 1
        if i < _PREFIX_MIN_CHARS or i / limit < _PREFIX_MIN_RATIO:
            continue
        # Longest shared prefix wins; ties go to the closest overall length so
        # the result does not depend on dict ordering. Without this, a parent and
        # its demerged sibling ("TATA MOTORS LIMITED" vs "TATA MOTORS PASS VEH
        # LTD") could resolve differently between runs.
        gap = abs(len(eq_name) - len(norm))
        if i > best_len or (i == best_len and best_gap is not None and gap < best_gap):
            best_len = i
            best_gap = gap
            best_symbol = symbol
    return best_symbol


def _build_symbol_indexes() -> tuple:
    """Build (isin -> symbol, normalised_name -> symbol) indexes from the NSE equity list."""
    global _bse_isin_index, _bse_name_index
    if _bse_isin_index is not None and _bse_name_index is not None:
        return _bse_isin_index, _bse_name_index

    isin_index: Dict[str, str] = {}
    name_index: Dict[str, str] = {}
    try:
        from app.main import get_nse_equities
        equities = get_nse_equities()
        if not equities:
            return isin_index, name_index
        for eq in equities:
            symbol = (eq.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            key = eq.get("key", "")
            isin = (key.split("|")[-1] if "|" in key else key).strip().upper()
            if isin:
                isin_index.setdefault(isin, symbol)
            norm_name = _norm_company_name(eq.get("name") or "")
            if norm_name:
                name_index.setdefault(norm_name, symbol)
    except Exception as e:
        logger.debug(f"Failed to build symbol indexes: {e}")
        # Leave the caches unset so a later call can retry once the list loads.
        return isin_index, name_index

    _bse_isin_index = isin_index
    _bse_name_index = name_index
    return isin_index, name_index


def resolve_bse_symbol(scrip_cd: str, slongname: str, isin_code: str, db: Session) -> str:
    """
    Resolve BSE scrip_cd / long name / ISIN to the standard NSE symbol.

    Returns the scrip code unchanged for BSE-only listings, which have no NSE
    ticker to resolve to.
    """
    scrip_cd = str(scrip_cd or "").strip().upper()
    slongname = str(slongname or "").strip()
    isin_code = str(isin_code or "").strip().upper()

    isin_index, name_index = _build_symbol_indexes()

    # 1. ISIN is the authoritative join, when the endpoint provides one.
    if isin_code:
        hit = isin_index.get(isin_code)
        if hit:
            return hit

    # 2. Company name, normalised past punctuation and legal-form suffixes.
    if slongname:
        norm = _norm_company_name(slongname)
        if norm:
            hit = name_index.get(norm)
            if hit:
                return hit
            best = _best_prefix_match(norm, name_index)
            if best:
                return best

    # 3. Fallback: a non-numeric scrip code is already a ticker.
    if scrip_cd and not scrip_cd.isdigit():
        return scrip_cd

    return scrip_cd or slongname.upper()

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
        self.session.headers.update({
            "Accept-Encoding": "gzip, deflate",
        })
    
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
            "Accept-Encoding": "gzip, deflate",
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
    
    def get(self, url: str, params: dict = None, headers: dict = None, timeout: int = 15) -> Optional[dict]:
        """Make a GET request to NSE API with proper cookie management."""
        self._refresh_cookies()
        req_headers = {}
        if headers:
            req_headers.update(headers)
        if "corporate-announcements" in url:
            req_headers["Referer"] = "https://www.nseindia.com/companies-listing/corporate-filings/announcements"
        try:
            resp = self.session.get(url, params=params, headers=req_headers if req_headers else None, timeout=timeout)
            if resp.status_code in (401, 403):
                # Force cookie refresh and retry
                self._last_cookie_refresh = 0
                self._refresh_cookies()
                resp = self.session.get(url, params=params, headers=req_headers if req_headers else None, timeout=timeout)
            resp.raise_for_status()
            try:
                return resp.json()
            except Exception:
                content = resp.content
                try:
                    import gzip
                    return json.loads(gzip.decompress(content).decode("utf-8"))
                except Exception:
                    pass
                try:
                    import brotli
                    return json.loads(brotli.decompress(content).decode("utf-8"))
                except Exception:
                    pass
                logger.error(f"Non-JSON response from {url}: {resp.text[:200]}")
                return None
        except Exception as e:
            logger.error(f"NSE API error for {url}: {e}")
            return None


class BSESession:
    """
    BSE API client with a Cloudflare-worker proxy front and a direct fallback.

    The proxy exists to work around BSE geo-blocking, but it is a single point of
    failure: when the worker is slow or down every call used to burn the full
    15s timeout and return nothing, so BSE news simply stopped flowing. Now the
    proxy gets a short budget, and any failure falls straight through to the
    direct API. A circuit breaker stops us re-testing a dead proxy on every call.
    """

    # How long to give the proxy before falling back.
    PROXY_TIMEOUT = 6
    # Consecutive proxy failures before the breaker opens.
    PROXY_FAILURE_THRESHOLD = 3
    # How long the breaker stays open.
    PROXY_COOLDOWN_SECONDS = 300

    def __init__(self):
        self.session = requests.Session()
        self.config = get_intel_config()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.bseindia.com/",
        })
        self._last_cookie_refresh = 0
        self._proxy_failures = 0
        self._proxy_disabled_until = 0.0

    def _refresh_cookies(self):
        """Visit BSE homepage first to establish session cookies for the direct route."""
        now = time.time()
        if now - self._last_cookie_refresh < 300:
            return
        try:
            self.session.get("https://www.bseindia.com/", timeout=8)
            self._last_cookie_refresh = now
        except Exception as e:
            logger.debug(f"BSE homepage cookie refresh failed: {e}")

    def _proxy_available(self) -> Optional[str]:
        """Return the proxy base URL if it is configured and the breaker is closed."""
        if time.time() < self._proxy_disabled_until:
            return None
        proxy_url = (self.config.general.get("bse_proxy_url") or "").strip()
        if not proxy_url or "workers.dev" not in proxy_url:
            return None
        if not proxy_url.startswith("http"):
            proxy_url = "https://" + proxy_url
        return proxy_url.rstrip("/")

    def _record_proxy_failure(self, err: Exception):
        self._proxy_failures += 1
        if self._proxy_failures >= self.PROXY_FAILURE_THRESHOLD:
            self._proxy_disabled_until = time.time() + self.PROXY_COOLDOWN_SECONDS
            self._proxy_failures = 0
            logger.warning(
                f"BSE proxy failed {self.PROXY_FAILURE_THRESHOLD}x ({err}). "
                f"Falling back to the direct API for {self.PROXY_COOLDOWN_SECONDS}s."
            )
        else:
            logger.debug(f"BSE proxy attempt failed ({err}); using direct API for this call.")

    @staticmethod
    def _parse(resp) -> Optional[Any]:
        """Turn a BSE response into JSON, tolerating its plain-text empty marker."""
        resp.raise_for_status()
        text_data = resp.text.strip()
        if text_data in ('"No Record Found!"', "No Record Found!"):
            return {"Table": []}
        return resp.json()

    @staticmethod
    def _is_empty(data: Any) -> bool:
        """True when a parsed BSE response carries no rows."""
        if data is None:
            return True
        if isinstance(data, dict):
            table = data.get("Table", data.get("data"))
            return not table
        if isinstance(data, list):
            return not data
        return False

    def get(self, url: str, params: dict = None, timeout: int = 15) -> Optional[Any]:
        """
        GET a BSE endpoint, preferring the proxy and falling back to direct.

        An *empty* proxy response also falls through to the direct route. A proxy
        pinned to a retired endpoint answers HTTP 200 "No Record Found!" to every
        query — a success-shaped failure that is indistinguishable from a quiet
        day, and that silently starved this feed for weeks. Verifying emptiness
        against the origin costs one extra call on genuinely quiet cycles and
        makes that failure mode impossible to miss.

        Returns parsed JSON, or None only when every route fails.
        """
        import urllib.parse

        proxy_base = self._proxy_available()
        if proxy_base:
            try:
                query_str = urllib.parse.urlencode(params) if params else ""
                sep = "&" if "?" in proxy_base else "/?"
                request_url = f"{proxy_base}{sep}{query_str}" if query_str else proxy_base
                resp = self.session.get(request_url, timeout=self.PROXY_TIMEOUT)
                data = self._parse(resp)
                self._proxy_failures = 0
                if not self._is_empty(data):
                    return data
                logger.debug("BSE proxy returned no rows; confirming against the direct API.")
            except Exception as e:
                self._record_proxy_failure(e)

        # Direct route — the proxy is unavailable, failed, or returned nothing.
        try:
            self._refresh_cookies()
            resp = self.session.get(url, params=params, timeout=timeout)
            return self._parse(resp)
        except Exception as e:
            logger.error(f"BSE API error for {url} (direct route): {e}")
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
    """Parse a date string trying dateutil parser first, then fallback formats, returning UTC (converting from IST)."""
    from datetime import timedelta
    if not date_str or not isinstance(date_str, str):
        return datetime.utcnow()
    
    clean_str = date_str.strip()
    if not clean_str:
        return datetime.utcnow()

    # Try dateutil parser first
    try:
        from dateutil import parser
        parsed = parser.parse(clean_str)
        if parsed.tzinfo is not None:
            parsed = parsed.replace(tzinfo=None)
        return parsed - timedelta(hours=5, minutes=30)
    except Exception:
        pass

    # Titlecase for strptime fallback ("28-JUL-2026" -> "28-Jul-2026")
    title_str = clean_str.title()
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
            local_time = datetime.strptime(title_str, fmt)
            return local_time - timedelta(hours=5, minutes=30)
        except (ValueError, AttributeError):
            continue
            
    return datetime.utcnow()

def _classify_event_category(event_type: str, title: str, description: str = "") -> str:
    """Auto-classify a market event into a category based on event_type, title, and description."""
    t = title.lower()
    desc = (description or "").lower()
    et = event_type.lower()
    
    # 1. Subject contains "Outcome of Board Meeting" AND details contains "finan"
    if "outcome of board meeting" in t and "finan" in desc:
        return "earnings"
        
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
            
            # Board-meeting *intimations* are calendar entries, not results, so
            # they take the cheap news-impact path. An actual results filing
            # arrives separately through the exchange hub.
            try:
                from app.services.ai_analyzer import apply_news_impact_classification
            except ImportError:
                apply_news_impact_classification = None

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
            if apply_news_impact_classification:
                apply_news_impact_classification(event)
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
                    "ai_sentiment": event.ai_sentiment,
                    "ai_impact_score": event.ai_impact_score,
                    "ai_summary": event.ai_summary,
                    "ai_provider": event.ai_provider,
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
    Run the slow-moving NSE/BSE scrapers. Returns a dict of {source: new_count}.

    Corporate announcements are NOT fetched here — they belong to the unified
    exchange hub (app/services/exchange_hub.py), which is the single owner of
    those endpoints for both the intelligence feed and the trading engine.
    """
    config = get_intel_config()
    if not config.nse_bse.get("enabled", True):
        return {}

    results = {}

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
        results["financial_results"] = fetch_financial_results(db)
    except Exception as e:
        logger.error(f"Financial results fetch failed: {e}")
        results["financial_results"] = 0

    total = sum(results.values())
    if total > 0:
        logger.info(f"NSE/BSE scrape complete: {total} new events — {results}")

    return results
