"""
Multi-Source News Aggregator.

Fetches business news from MoneyControl, Economic Times, Business Standard,
Livemint, Reuters, Bloomberg, NDTV Profit via RSS feeds and Google News RSS.
Includes deduplication and story clustering.
"""
import email.utils
import hashlib
import html
import json
import logging
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.database import NewsItem, NewsStory
from app.services.intel_config import get_intel_config
from app.services.deduplication import (
    url_hash,
    is_duplicate_url,
    find_or_create_story,
    extract_symbols,
    extract_primary_symbol,
    to_iso_utc,
)
from app.services.sse_manager import sse_manager

logger = logging.getLogger("app.news_aggregator")

# Try importing feedparser for better RSS support
try:
    import feedparser
    HAS_FEEDPARSER = True
except ImportError:
    HAS_FEEDPARSER = False
    logger.info("feedparser not installed, using built-in XML parser for RSS")


# ─── RSS Feed Parsing ──────────────────────────────────────────────────────

def _parse_rss_builtin(url: str, max_articles: int = 20) -> List[dict]:
    """Parse RSS feed using built-in urllib + xml.etree (no dependencies)."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    articles = []
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as response:
            xml_data = response.read()
        
        root = ET.fromstring(xml_data)
        
        # Handle both RSS 2.0 (<item>) and Atom (<entry>) formats
        items = root.findall(".//item")
        if not items:
            # Try Atom namespace
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            items = root.findall(".//atom:entry", ns)
        
        for item in items[:max_articles]:
            title_el = item.find("title")
            link_el = item.find("link")
            desc_el = item.find("description")
            pub_el = item.find("pubDate")
            source_el = item.find("source")
            
            # Atom fallback
            if link_el is None:
                link_el = item.find("{http://www.w3.org/2005/Atom}link")
            if title_el is None:
                title_el = item.find("{http://www.w3.org/2005/Atom}title")
            
            headline = html.unescape(title_el.text.strip()) if title_el is not None and title_el.text else ""
            link = ""
            if link_el is not None:
                link = link_el.text.strip() if link_el.text else link_el.get("href", "")
            summary = html.unescape(desc_el.text.strip()) if desc_el is not None and desc_el.text else ""
            source = source_el.text.strip() if source_el is not None and source_el.text else ""
            
            # Clean headline: remove source suffix (e.g., " - Moneycontrol")
            if " - " in headline:
                parts = headline.rsplit(" - ", 1)
                headline = parts[0]
                if not source:
                    source = parts[1]
            
            # Parse published date
            pub_time = None
            pub_text = pub_el.text if pub_el is not None else None
            if pub_text:
                try:
                    pub_time = email.utils.parsedate_to_datetime(pub_text)
                except Exception:
                    pass
            if not pub_time:
                # Try ISO format
                updated_el = item.find("{http://www.w3.org/2005/Atom}updated")
                if updated_el is not None and updated_el.text:
                    try:
                        pub_time = datetime.fromisoformat(updated_el.text.replace("Z", "+00:00"))
                    except Exception:
                        pass
            if not pub_time:
                pub_time = datetime.utcnow()
            
            # Strip HTML tags from summary
            if summary:
                import re
                summary = re.sub(r"<[^>]+>", "", summary)[:500]
            
            if headline and link:
                articles.append({
                    "headline": headline,
                    "summary": summary or headline,
                    "url": link,
                    "source_name": source,
                    "published_at": pub_time,
                })
    except Exception as e:
        logger.error(f"RSS parse error for {url}: {e}")
    
    return articles


def _parse_rss_feedparser(url: str, max_articles: int = 20) -> List[dict]:
    """Parse RSS feed using feedparser library with a browser User-Agent."""
    try:
        agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        feed = feedparser.parse(url, agent=agent)
        articles = []
        for entry in feed.entries[:max_articles]:
            headline = html.unescape(entry.get("title", "")).strip()
            link = entry.get("link", "")
            summary = html.unescape(entry.get("summary", entry.get("description", ""))).strip()
            source = entry.get("source", {}).get("title", "") if hasattr(entry, "source") else ""
            
            # Clean headline
            if " - " in headline:
                parts = headline.rsplit(" - ", 1)
                headline = parts[0]
                if not source:
                    source = parts[1]
            
            # Parse date
            pub_time = None
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                try:
                    import calendar
                    pub_time = datetime.utcfromtimestamp(calendar.timegm(entry.published_parsed))
                except Exception:
                    pass
            if pub_time and pub_time.tzinfo is not None:
                pub_time = pub_time.replace(tzinfo=None)
            if not pub_time:
                pub_time = datetime.utcnow()
            
            # Strip HTML from summary
            if summary:
                import re
                summary = re.sub(r"<[^>]+>", "", summary)[:500]
            
            if headline and link:
                articles.append({
                    "headline": headline,
                    "summary": summary or headline,
                    "url": link,
                    "source_name": source,
                    "published_at": pub_time,
                })
        return articles
    except Exception as e:
        logger.error(f"Feedparser error for {url}: {e}")
        return []


def parse_rss(url: str, max_articles: int = 20) -> List[dict]:
    """Parse RSS feed using the best available library."""
    if HAS_FEEDPARSER:
        return _parse_rss_feedparser(url, max_articles)
    return _parse_rss_builtin(url, max_articles)


# ─── Google News RSS ───────────────────────────────────────────────────────

def fetch_google_news_rss(query: str, max_articles: int = 15) -> List[dict]:
    """Fetch news via Google News RSS for a given query."""
    encoded = urllib.parse.quote(query)
    url = f"https://news.google.com/rss/search?q={encoded}&hl=en-IN&gl=IN&ceid=IN:en"
    return _parse_rss_builtin(url, max_articles)


# ─── Source-Specific Fetchers ──────────────────────────────────────────────

def _fetch_rss_source(source_name: str, source_config: dict, max_per_source: int) -> List[dict]:
    """Fetch articles from an RSS-type source."""
    urls = source_config.get("urls", [])
    tier = source_config.get("tier", 3)
    all_articles = []
    
    for rss_url in urls:
        articles = parse_rss(rss_url, max_per_source)
        for art in articles:
            art["source"] = source_name
            art["source_tier"] = tier
            if not art.get("source_name"):
                art["source_name"] = source_name
        all_articles.extend(articles)
    
    return all_articles[:max_per_source]


def _fetch_google_news_source(source_name: str, source_config: dict, max_per_source: int) -> List[dict]:
    """Fetch articles via Google News RSS for a query-based source."""
    query = source_config.get("query", "")
    tier = source_config.get("tier", 3)
    
    if not query:
        return []
    
    articles = fetch_google_news_rss(query, max_per_source)
    for art in articles:
        art["source"] = source_name
        art["source_tier"] = tier
    
    return articles


def _fetch_newsdata_io(source_config: dict, max_per_source: int) -> List[dict]:
    """Fetch articles from NewsData.io API."""
    api_key = source_config.get("api_key", "")
    if not api_key:
        return []
    
    import requests
    base_url = source_config.get("base_url", "https://newsdata.io/api/1/news")
    params = {
        "apikey": api_key,
        "q": source_config.get("query", "stock market India"),
        "language": source_config.get("language", "en"),
        "country": source_config.get("country", "in"),
    }
    
    try:
        resp = requests.get(base_url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        
        articles = []
        for item in data.get("results", [])[:max_per_source]:
            pub_time = datetime.utcnow()
            if item.get("pubDate"):
                try:
                    pub_time = datetime.fromisoformat(item["pubDate"].replace("Z", "+00:00"))
                except Exception:
                    pass
            
            articles.append({
                "headline": item.get("title", ""),
                "summary": item.get("description", item.get("title", ""))[:500],
                "url": item.get("link", ""),
                "source": "newsdata_io",
                "source_name": item.get("source_id", "newsdata.io"),
                "source_tier": 2,
                "published_at": pub_time,
            })
        return articles
    except Exception as e:
        logger.error(f"NewsData.io API error: {e}")
        return []


# ─── Main Aggregator ──────────────────────────────────────────────────────

def _fetch_single_source(source_name: str, source_config: dict, max_per_source: int) -> Tuple[str, List[dict]]:
    """Fetch from a single source. Returns (source_name, articles)."""
    source_type = source_config.get("type", "rss")
    
    try:
        if source_type == "rss":
            return source_name, _fetch_rss_source(source_name, source_config, max_per_source)
        elif source_type == "google_news_rss":
            return source_name, _fetch_google_news_source(source_name, source_config, max_per_source)
        elif source_type == "api" and source_name == "newsdata_io":
            return source_name, _fetch_newsdata_io(source_config, max_per_source)
        else:
            logger.warning(f"Unknown source type '{source_type}' for {source_name}")
            return source_name, []
    except Exception as e:
        logger.error(f"Error fetching from {source_name}: {e}")
        return source_name, []


def fetch_all_news(db: Session) -> Dict[str, int]:
    """
    Fetch news from all configured sources in parallel,
    deduplicate, cluster into stories, and store in database.
    
    Returns dict of {source_name: new_article_count}.
    """
    config = get_intel_config()
    if not config.news.get("enabled", True):
        return {}
    
    news_config = config.news
    max_per_source = news_config.get("max_articles_per_source", 20)
    sources = news_config.get("sources", {})
    
    # Collect enabled sources
    tasks = []
    for source_name, source_config in sources.items():
        if not source_config.get("enabled", True):
            continue
        tasks.append((source_name, source_config))
    
    if not tasks:
        return {}
    
    # Fetch from all sources in parallel
    all_raw_articles = []
    results_by_source: Dict[str, int] = {}
    
    with ThreadPoolExecutor(max_workers=min(len(tasks), 8)) as executor:
        futures = {
            executor.submit(_fetch_single_source, name, cfg, max_per_source): name
            for name, cfg in tasks
        }
        for future in as_completed(futures):
            source_name = futures[future]
            try:
                name, articles = future.result()
                all_raw_articles.extend(articles)
                results_by_source[name] = 0  # Will be updated with saved count
            except Exception as e:
                logger.error(f"Future error for {source_name}: {e}")
                results_by_source[source_name] = 0
    
    # Process and deduplicate articles
    saved_count = 0
    new_items_for_broadcast = []  # Collect for SSE broadcast after commit
    for article in all_raw_articles:
        try:
            article_url = article.get("url", "")
            headline = article.get("headline", "").strip()
            if not article_url or not headline:
                continue
            
            # Check URL duplicate
            u_hash = url_hash(article_url)
            existing = db.query(NewsItem.id).filter(NewsItem.url_hash == u_hash).first()
            if existing:
                continue
            
            # Extract stock symbols
            full_text = f"{headline} {article.get('summary', '')}"
            symbols = extract_symbols(full_text)
            primary_symbol = symbols[0] if symbols else None
            
            source_name = article.get("source", "unknown")
            source_tier = article.get("source_tier", 3)
            published_at = article.get("published_at", datetime.utcnow())
            if isinstance(published_at, str):
                try:
                    published_at = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
                except Exception:
                    published_at = datetime.utcnow()
            
            # Find or create story cluster
            story_id = find_or_create_story(
                db, headline, published_at,
                symbol=primary_symbol,
                source_tier=source_tier
            )
            
            # Create NewsItem
            news_item = NewsItem(
                source=source_name,
                source_tier=source_tier,
                headline=headline,
                summary=article.get("summary", "")[:500],
                url=article_url,
                url_hash=u_hash,
                symbol=primary_symbol,
                mentioned_symbols=json.dumps(symbols) if symbols else None,
                published_at=published_at,
                story_id=story_id,
            )
            db.add(news_item)
            saved_count += 1
            
            # Collect broadcast payload (id will be available after commit/flush)
            new_items_for_broadcast.append({
                "headline": headline,
                "summary": article.get("summary", "")[:200],
                "url": article_url,
                "source": source_name,
                "source_tier": source_tier,
                "symbol": primary_symbol,
                "published_at": published_at,
                "time": to_iso_utc(published_at),
            })
            
            # Track per-source count
            if source_name in results_by_source:
                results_by_source[source_name] += 1
            
        except Exception as e:
            logger.error(f"Error saving article: {e}")
            continue
    
    if saved_count > 0:
        try:
            db.commit()
        except Exception as e:
            db.rollback()
            logger.error(f"Database commit error: {e}")
            return results_by_source
        logger.info(f"News aggregation complete: {saved_count} new articles — {results_by_source}")
        
        # Broadcast new articles to SSE clients (only if published within last 48h)
        now_utc = datetime.utcnow()
        for item_data in new_items_for_broadcast:
            pub_dt = item_data.get("published_at")
            if pub_dt and (now_utc - pub_dt).total_seconds() > 172800:
                continue  # Skip old archived articles from live SSE push
            sse_manager.broadcast("new_news", {
                "type": "news_story",
                "event_type": "news",
                "source": item_data["source"],
                "symbol": item_data["symbol"],
                "title": item_data["headline"],
                "description": item_data["summary"],
                "url": item_data["url"],
                "time": item_data["time"],
                "source_tier": item_data["source_tier"],
            })
    
    # Purge old articles beyond retention period
    _purge_old_articles(db)
    
    return results_by_source


def _purge_old_articles(db: Session):
    """Remove articles older than the configured retention period."""
    config = get_intel_config()
    retention_days = config.news.get("retention_days", 30)
    max_total = config.news.get("max_total_articles", 5000)
    
    cutoff = datetime.utcnow() - timedelta(days=retention_days)
    
    try:
        # Delete old articles
        deleted = db.query(NewsItem).filter(NewsItem.published_at < cutoff).delete()
        if deleted > 0:
            logger.info(f"Purged {deleted} articles older than {retention_days} days")
        
        # If still over max, delete oldest
        total = db.query(NewsItem).count()
        if total > max_total:
            excess = total - max_total
            oldest = db.query(NewsItem).order_by(NewsItem.published_at.asc()).limit(excess).all()
            for item in oldest:
                db.delete(item)
            logger.info(f"Purged {len(oldest)} excess articles (total was {total}, max {max_total})")
        
        # Clean orphaned stories (no articles referencing them)
        orphan_stories = db.query(NewsStory).filter(
            ~NewsStory.id.in_(
                db.query(NewsItem.story_id).filter(NewsItem.story_id.isnot(None)).distinct()
            )
        ).delete(synchronize_session="fetch")
        if orphan_stories > 0:
            logger.info(f"Purged {orphan_stories} orphaned news stories")
        
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Error purging old articles: {e}")
