"""
News Deduplication & Story Clustering Engine.

Handles:
1. URL normalization — strips tracking params, anchors, etc.
2. Headline similarity — trigram-based fuzzy matching (no ML dependencies)
3. Story clustering — groups related articles into a single NewsStory
"""
import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional, Set, Tuple
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

from sqlalchemy.orm import Session

from app.database import NewsItem, NewsStory
from app.services.intel_config import get_intel_config

logger = logging.getLogger("app.dedup")


def to_iso_utc(dt: Any) -> Optional[str]:
    """Convert any datetime or ISO date string into a clean UTC ISO 8601 string ending with 'Z'."""
    if not dt:
        return None
    if isinstance(dt, str):
        s = dt.strip()
        if not s:
            return None
        # Handle SQL style string "YYYY-MM-DD HH:MM:SS" or ISO string
        clean_s = s.replace(" ", "T").rstrip("Z")
        try:
            dt = datetime.fromisoformat(clean_s)
        except Exception:
            return s
    if isinstance(dt, datetime):
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(dt)



# ─── URL Normalization ──────────────────────────────────────────────────────

# Common tracking / analytics query parameters to strip
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "gclsrc", "dclid", "zanpid", "msclkid",
    "ref", "source", "amp", "_ga", "_gid", "mc_cid", "mc_eid",
    "ncid", "sr_share", "icid", "s_cid", "tag", "trk",
}


def normalize_url(url: str) -> str:
    """
    Normalize a URL for deduplication:
    - Remove fragments (#...)
    - Remove tracking query parameters
    - Lowercase the scheme and host
    - Remove trailing slashes
    - Remove /amp/ paths
    """
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        # Lowercase scheme and netloc
        scheme = parsed.scheme.lower()
        netloc = parsed.netloc.lower()
        # Remove /amp/ segments
        path = re.sub(r"/amp/?", "/", parsed.path)
        # Remove trailing slash
        path = path.rstrip("/")
        # Filter out tracking query params
        if parsed.query:
            params = parse_qs(parsed.query, keep_blank_values=False)
            filtered = {k: v for k, v in params.items() if k.lower() not in _TRACKING_PARAMS}
            query = urlencode(filtered, doseq=True) if filtered else ""
        else:
            query = ""
        # Rebuild without fragment
        normalized = urlunparse((scheme, netloc, path, parsed.params, query, ""))
        return normalized
    except Exception:
        return url


def url_hash(url: str) -> str:
    """Generate a SHA-256 hash of the normalized URL."""
    normalized = normalize_url(url)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def event_hash(source: str, event_type: str, title: str, event_time: str) -> str:
    """Generate a SHA-256 hash for market event deduplication."""
    raw = f"{source}|{event_type}|{title}|{event_time}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ─── Headline Similarity (Trigram-based, no ML dependencies) ────────────────

def _trigrams(text: str) -> Set[str]:
    """Extract character trigrams from text after cleaning."""
    # Normalize: lowercase, remove punctuation, collapse whitespace
    cleaned = re.sub(r"[^\w\s]", "", text.lower())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) < 3:
        return {cleaned}
    return {cleaned[i:i+3] for i in range(len(cleaned) - 2)}


def headline_similarity(h1: str, h2: str) -> float:
    """
    Compute similarity between two headlines using trigram overlap (Jaccard coefficient).
    Returns a float between 0.0 (completely different) and 1.0 (identical).
    """
    if not h1 or not h2:
        return 0.0
    # Quick exact match
    if h1.lower().strip() == h2.lower().strip():
        return 1.0
    t1 = _trigrams(h1)
    t2 = _trigrams(h2)
    if not t1 or not t2:
        return 0.0
    intersection = len(t1 & t2)
    union = len(t1 | t2)
    return intersection / union if union > 0 else 0.0


# ─── Duplicate Detection ───────────────────────────────────────────────────

def is_duplicate_url(db: Session, article_url: str) -> bool:
    """Check if an article with the same normalized URL already exists."""
    h = url_hash(article_url)
    existing = db.query(NewsItem.id).filter(NewsItem.url_hash == h).first()
    return existing is not None


def is_duplicate_event(db: Session, hash_val: str) -> bool:
    """Check if a market event with the same hash already exists."""
    from app.database import MarketEvent
    existing = db.query(MarketEvent.id).filter(MarketEvent.event_hash == hash_val).first()
    return existing is not None


def find_similar_articles(
    db: Session,
    headline: str,
    published_at: datetime,
    threshold: float = None,
    time_window_hours: int = None
) -> List[NewsItem]:
    """
    Find existing articles with similar headlines within the time window.
    Returns a list of matching NewsItem records.
    """
    config = get_intel_config()
    if threshold is None:
        threshold = config.deduplication.get("headline_similarity_threshold", 0.75)
    if time_window_hours is None:
        time_window_hours = config.deduplication.get("time_window_hours", 48)
    
    if published_at and published_at.tzinfo is not None:
        published_at = published_at.replace(tzinfo=None)
    
    window_start = published_at - timedelta(hours=time_window_hours)
    window_end = published_at + timedelta(hours=time_window_hours)
    
    # Fetch candidate articles within time window
    candidates = db.query(NewsItem).filter(
        NewsItem.published_at >= window_start,
        NewsItem.published_at <= window_end
    ).all()
    
    matches = []
    for candidate in candidates:
        sim = headline_similarity(headline, candidate.headline)
        if sim >= threshold:
            matches.append(candidate)
    
    return matches


# ─── Story Clustering ──────────────────────────────────────────────────────

def find_or_create_story(
    db: Session,
    headline: str,
    published_at: datetime,
    symbol: Optional[str] = None,
    source_tier: int = 3
) -> Optional[int]:
    """
    Find an existing NewsStory that this article belongs to, or create a new one.
    Returns the story_id.
    """
    config = get_intel_config()
    clustering = config.deduplication.get("story_clustering", {})
    if not clustering.get("enabled", True):
        return None
    
    max_gap_hours = clustering.get("max_gap_hours", 24)
    threshold = config.deduplication.get("headline_similarity_threshold", 0.75)
    
    if published_at and published_at.tzinfo is not None:
        published_at = published_at.replace(tzinfo=None)
        
    # Search existing stories within time window
    window_start = published_at - timedelta(hours=max_gap_hours)
    window_end = published_at + timedelta(hours=max_gap_hours)
    
    candidate_stories = db.query(NewsStory).filter(
        NewsStory.last_published >= window_start,
        NewsStory.first_published <= window_end
    ).all()
    
    best_match: Optional[NewsStory] = None
    best_sim = 0.0
    
    for story in candidate_stories:
        sim = headline_similarity(headline, story.headline)
        if sim >= threshold and sim > best_sim:
            best_sim = sim
            best_match = story
    
    if best_match:
        # Update existing story
        best_match.article_count += 1
        if published_at > best_match.last_published:
            best_match.last_published = published_at
        if published_at < best_match.first_published:
            best_match.first_published = published_at
        if source_tier < best_match.best_source_tier:
            best_match.best_source_tier = source_tier
            # Update canonical headline to the better source's headline
            best_match.headline = headline
        # Merge symbols
        if symbol:
            existing_symbols = set((best_match.symbols or "").split(","))
            existing_symbols.discard("")
            existing_symbols.add(symbol)
            best_match.symbols = ",".join(sorted(existing_symbols))
        db.flush()
        return best_match.id
    else:
        # Create new story
        story = NewsStory(
            headline=headline,
            symbols=symbol or "",
            article_count=1,
            best_source_tier=source_tier,
            first_published=published_at,
            last_published=published_at,
        )
        db.add(story)
        db.flush()
        return story.id


# ─── Symbol Extraction ─────────────────────────────────────────────────────

# Cache of known symbols for entity extraction
_known_symbols: Optional[Set[str]] = None


def _load_known_symbols() -> Set[str]:
    """Load known stock symbols from config and database."""
    global _known_symbols
    if _known_symbols is not None:
        return _known_symbols
    
    from app.config import DEFAULT_NIFTY_50
    symbols = set()
    for stock in DEFAULT_NIFTY_50:
        symbols.add(stock["symbol"].upper())
        # Also add common name parts (e.g., "Reliance", "TCS")
        name_parts = stock["name"].replace("Ltd.", "").replace("Limited", "").strip().split()
        for part in name_parts:
            if len(part) > 3:  # Skip short words like "of", "and"
                symbols.add(part.upper())
    
    _known_symbols = symbols
    return _known_symbols


def extract_symbols(text: str) -> List[str]:
    """
    Extract stock symbols mentioned in text using pattern matching and company name mappings.
    Returns a list of detected NSE symbols.
    """
    if not text:
        return []
    
    found = set()
    text_upper = text.upper()
    
    # 1. Broad mapping of company names to their actual stock tickers (NSE symbols)
    name_to_symbol_map = {
        "KALYAN JEWELLERS": "KALYANKJIL",
        "KALYAN JEWELLER": "KALYANKJIL",
        "KALYANKJIL": "KALYANKJIL",
        "KALYAN": "KALYANKJIL",
        "TRENT": "TRENT",
        "TATA MOTORS": "TATAMOTORS",
        "TATA MOTOR": "TATAMOTORS",
        "TATA STEEL": "TATASTEEL",
        "TATA CONSULTANCY": "TCS",
        "TCS": "TCS",
        "HDFC BANK": "HDFCBANK",
        "HDFCBANK": "HDFCBANK",
        "ICICI BANK": "ICICIBANK",
        "ICICIBANK": "ICICIBANK",
        "INFOSYS": "INFY",
        "INFY": "INFY",
        "RELIANCE": "RELIANCE",
        "STATE BANK OF INDIA": "SBIN",
        "SBI": "SBIN",
        "AXIS BANK": "AXISBANK",
        "AXISBANK": "AXISBANK",
        "KOTAK": "KOTAKBANK",
        "KOTAKBANK": "KOTAKBANK",
        "MARUTI": "MARUTI",
        "MAHINDRA": "M&M",
        "M&M": "M&M",
        "ADANI ENTERPRISES": "ADANIENT",
        "ADANIENT": "ADANIENT",
        "ADANI POWER": "ADANIPOWER",
        "ADANIPOWER": "ADANIPOWER",
        "ADANI PORTS": "ADANIPORTS",
        "ADANIPORTS": "ADANIPORTS",
        "WIPRO": "WIPRO",
        "ZOMATO": "ZOMATO",
        "PAYTM": "PAYTM",
        "OLA ELECTRIC": "OLAELEC",
        "OLA ELEC": "OLAELEC",
        "OLAELEC": "OLAELEC",
        "ORBEXP": "ORBEXP",
        "ORBIT EXPORTS": "ORBEXP",
        "NTPCGREEN": "NTPCGREEN",
        "NTPC GREEN": "NTPCGREEN",
        "PANACEABIO": "PANACEABIO",
        "PANACEA BIO": "PANACEABIO",
        "DIGITIDE": "DIGITIDE",
        "GOODLUCK": "GOODLUCK",
        "JINDAL SAW": "JINDALSAW",
        "JINDALSAW": "JINDALSAW",
    }
    
    # Check for direct company name matches in the text
    for key, sym in name_to_symbol_map.items():
        pattern = r'\b' + re.escape(key) + r'\b'
        if re.search(pattern, text_upper):
            found.add(sym)
            
    # 2. Look for $SYMBOL cashtag patterns
    known = _load_known_symbols()
    cashtags = re.findall(r"\$([A-Z]{2,20})", text_upper)
    for tag in cashtags:
        if tag in known or tag in name_to_symbol_map.values():
            found.add(tag)
            
    # 3. Dynamic loading from database (Watchlist and MarketEvent) to map watchlist items
    try:
        from app.database import SessionLocal, Watchlist
        db = SessionLocal()
        try:
            watchlist_items = db.query(Watchlist).all()
            for w in watchlist_items:
                sym = w.symbol.upper()
                name_upper = w.name.upper()
                # If watchlist name is in the text, add its symbol
                cleaned_name = name_upper.replace("LTD.", "").replace("LIMITED", "").strip()
                if cleaned_name and len(cleaned_name) > 3:
                    if cleaned_name in text_upper:
                        found.add(sym)
                # If watchlist symbol is directly mentioned as whole word
                pattern = r'\b' + re.escape(sym) + r'\b'
                if re.search(pattern, text_upper):
                    found.add(sym)
        finally:
            db.close()
    except Exception:
        pass
    
    # 4. Look for other known symbols as whole words
    for symbol in known:
        if len(symbol) >= 3:  # Avoid false positives with very short symbols
            pattern = r'\b' + re.escape(symbol) + r'\b'
            if re.search(pattern, text_upper):
                found.add(symbol)
                
    # Filter out false positives
    false_positives = {"THE", "AND", "FOR", "FROM", "WITH", "OVER", "INTO", "ALSO",
                       "BANK", "INDIA", "STOCK", "MARKET", "NEWS", "SHARE", "PRICE", "COMPANY"}
    found -= false_positives
    
    return sorted(found)


def extract_primary_symbol(text: str) -> Optional[str]:
    """Extract the most likely primary stock symbol from text."""
    symbols = extract_symbols(text)
    return symbols[0] if symbols else None
