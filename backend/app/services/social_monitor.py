"""
Social Media Monitor — X (Twitter) and financial social media tracking.

Uses Google News RSS with site:x.com filters as a free alternative to the paid X API.
Optionally supports direct X API v2 if bearer token is configured.
"""
import json
import logging
import urllib.parse
from datetime import datetime
from typing import Dict, List

from sqlalchemy.orm import Session

from app.database import MarketEvent
from app.services.intel_config import get_intel_config
from app.services.deduplication import event_hash, is_duplicate_event, extract_primary_symbol
from app.services.news_aggregator import fetch_google_news_rss

logger = logging.getLogger("app.social_monitor")


def _fetch_twitter_via_rss(db: Session) -> int:
    """
    Monitor X/Twitter via Google News RSS queries.
    This is free and doesn't require an API key.
    """
    config = get_intel_config()
    twitter_config = config.social_media.get("twitter", {})
    
    tracked_accounts = twitter_config.get("tracked_accounts", [])
    keywords = twitter_config.get("keywords", [])
    
    # Build search queries
    queries = []
    
    # Query for tracked accounts
    if tracked_accounts:
        accounts_query = " OR ".join([f'from:{acc}' for acc in tracked_accounts[:5]])
        queries.append(f"site:x.com ({accounts_query}) stock market")
    
    # Query for market keywords
    if keywords:
        kw_query = " OR ".join([f'"{kw}"' for kw in keywords[:5]])
        queries.append(f"site:x.com ({kw_query})")
    
    # General financial twitter
    queries.append("site:x.com NSE BSE India stock breaking")
    
    count = 0
    seen_urls = set()
    
    for query in queries:
        try:
            articles = fetch_google_news_rss(query, max_articles=10)
            
            for art in articles:
                url = art.get("url", "")
                headline = art.get("headline", "").strip()
                if not url or not headline or url in seen_urls:
                    continue
                seen_urls.add(url)
                
                pub_time = art.get("published_at", datetime.utcnow())
                if isinstance(pub_time, str):
                    try:
                        pub_time = datetime.fromisoformat(pub_time.replace("Z", "+00:00"))
                    except Exception:
                        pub_time = datetime.utcnow()
                
                h = event_hash("twitter", "social", headline, pub_time.isoformat())
                if is_duplicate_event(db, h):
                    continue
                
                symbol = extract_primary_symbol(headline)
                
                event = MarketEvent(
                    event_type="social",
                    source="twitter",
                    symbol=symbol,
                    title=headline[:500],
                    description=art.get("summary", headline)[:1000],
                    url=url,
                    event_hash=h,
                    event_time=pub_time,
                )
                db.add(event)
                count += 1
        except Exception as e:
            logger.error(f"Error fetching Twitter RSS for query '{query[:50]}...': {e}")
    
    if count > 0:
        db.commit()
        logger.info(f"Saved {count} new social media events from Twitter/X")
    
    return count


def _fetch_twitter_via_api(db: Session) -> int:
    """
    Monitor X/Twitter using the official v2 API (requires bearer token).
    Only used if method is set to "api" and a bearer token is configured.
    """
    config = get_intel_config()
    twitter_config = config.social_media.get("twitter", {})
    bearer_token = twitter_config.get("bearer_token", "")
    
    if not bearer_token:
        logger.warning("Twitter API method selected but no bearer token configured")
        return 0
    
    import requests
    keywords = twitter_config.get("keywords", [])
    
    # Build search query for recent tweets
    query_parts = []
    for kw in keywords[:10]:
        query_parts.append(f'"{kw}"')
    
    search_query = " OR ".join(query_parts) + " -is:retweet lang:en"
    
    headers = {
        "Authorization": f"Bearer {bearer_token}",
        "User-Agent": "StockIntelligencePlatform/1.0",
    }
    params = {
        "query": search_query,
        "max_results": 20,
        "tweet.fields": "created_at,author_id,text,public_metrics",
        "sort_order": "recency",
    }
    
    try:
        resp = requests.get(
            "https://api.twitter.com/2/tweets/search/recent",
            headers=headers,
            params=params,
            timeout=15
        )
        resp.raise_for_status()
        data = resp.json()
        
        count = 0
        for tweet in data.get("data", []):
            tweet_text = tweet.get("text", "").strip()
            tweet_id = tweet.get("id", "")
            created_at = tweet.get("created_at", "")
            
            if not tweet_text:
                continue
            
            pub_time = datetime.utcnow()
            if created_at:
                try:
                    pub_time = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                except Exception:
                    pass
            
            h = event_hash("twitter_api", "social", tweet_text[:200], pub_time.isoformat())
            if is_duplicate_event(db, h):
                continue
            
            symbol = extract_primary_symbol(tweet_text)
            url = f"https://x.com/i/web/status/{tweet_id}" if tweet_id else ""
            
            event = MarketEvent(
                event_type="social",
                source="twitter_api",
                symbol=symbol,
                title=tweet_text[:500],
                description=tweet_text,
                url=url,
                raw_data=json.dumps(tweet, default=str),
                event_hash=h,
                event_time=pub_time,
            )
            db.add(event)
            count += 1
        
        if count > 0:
            db.commit()
            logger.info(f"Saved {count} new tweets via Twitter API")
        
        return count
    except Exception as e:
        logger.error(f"Twitter API error: {e}")
        return 0


def fetch_social_media(db: Session) -> Dict[str, int]:
    """
    Main entry point: fetch social media signals from all configured sources.
    Returns dict of {source: new_count}.
    """
    config = get_intel_config()
    if not config.social_media.get("enabled", True):
        return {}
    
    results = {}
    
    twitter_config = config.social_media.get("twitter", {})
    if twitter_config.get("enabled", True):
        method = twitter_config.get("method", "rss")
        try:
            if method == "api":
                results["twitter_api"] = _fetch_twitter_via_api(db)
            else:
                results["twitter_rss"] = _fetch_twitter_via_rss(db)
        except Exception as e:
            logger.error(f"Social media fetch error: {e}")
    
    total = sum(results.values())
    if total > 0:
        logger.info(f"Social media scan complete: {total} new events — {results}")
    
    return results
