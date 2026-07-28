"""
AI Analysis Engine — Sentiment Analysis, Impact Prediction, and Alert Generation.

Processes market events, news items, and filings through the configured LLM
(Gemini/Groq/OpenAI/Anthropic) to:
1. Classify sentiment (positive/negative/neutral)
2. Score market impact (-1.0 to 1.0)
3. Identify affected stocks
4. Generate human-readable summaries
5. Create AIAlerts for high-impact events
"""
import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session
from sqlalchemy import or_

from app.database import MarketEvent, NewsItem, NewsStory, CompanyFiling, AIAlert
from app.services.intel_config import get_intel_config

logger = logging.getLogger("app.ai_analyzer")


# ─── LLM Call Helpers (reuse existing providers) ─────────────────────────

def _call_llm_for_analysis(prompt: str):
    """
    Call the configured LLM with a structured analysis prompt.
    Tries primary provider, then fallbacks.
    Returns tuple (parsed JSON dict or None, provider_name).
    """
    from app.services.gemini import (
        reload_env_vars, call_gemini, call_groq, call_openai, call_anthropic, call_ollama, clean_json_response
    )
    
    config = get_intel_config()
    ai_config = config.ai
    primary = ai_config.get("primary_provider", "groq")
    fallbacks = ai_config.get("fallback_providers", ["gemini", "openai", "anthropic", "ollama"])
    
    providers = [primary] + [fb for fb in fallbacks if fb != primary]
    env = reload_env_vars()
    
    for provider in providers:
        try:
            if provider == "groq" and env.get("groq_key"):
                res = call_groq(prompt, env["groq_key"], env.get("groq_model", "llama-3.3-70b-versatile"))
                return res, "groq"
            elif provider == "gemini" and env.get("gemini_key"):
                key = env["gemini_key"].strip()
                if key and not key.startswith("AQ."):
                    res = call_gemini(prompt, key)
                    return res, "gemini"
            elif provider == "openai" and env.get("openai_key"):
                res = call_openai(prompt, env["openai_key"])
                return res, "openai"
            elif provider == "anthropic" and env.get("anthropic_key"):
                res = call_anthropic(prompt, env["anthropic_key"])
                return res, "anthropic"
            elif provider == "ollama" and env.get("ollama_url"):
                res = call_ollama(prompt, env["ollama_url"], env.get("ollama_model", "stocks-analyst"))
                return res, "ollama"
        except Exception as e:
            logger.warning(f"LLM provider {provider} failed: {e}")
            continue
    
    logger.error("All LLM providers failed for analysis")
    return None, "stub"


# ─── Sentiment Analysis Prompts ─────────────────────────────────────────

def _build_event_analysis_prompt(events: List[dict]) -> str:
    """Build a batch analysis prompt for market events."""
    events_text = ""
    for i, evt in enumerate(events):
        events_text += f"""
--- Event {i+1} ---
Type: {evt.get('event_type', 'unknown')}
Source: {evt.get('source', 'unknown')}
Symbol: {evt.get('symbol', 'N/A')}
Title: {evt.get('title', '')}
Description: {evt.get('description', '')}
Time: {evt.get('event_time', '')}
"""

    return f"""You are a senior Indian stock market analyst. Conduct a deep, in-depth financial analysis of the following market events to assess their impact on the Indian stock market (NSE/BSE).

For EACH event, analyze the details thoroughly and determine:
1. sentiment: "positive", "negative", or "neutral"
2. impact_score: float from -1.0 (extremely negative) to 1.0 (extremely positive)
3. affected_stocks: List of NSE stock symbols directly impacted OR prominent sector leaders if a sector is broadly impacted. E.g., if a banking regulation is introduced, list the top banks: ["HDFCBANK", "ICICIBANK", "SBIN", "AXISBANK"]. If it affects IT, list: ["TCS", "INFY", "WIPRO", "HCLTECH"].
4. summary: In-depth financial analysis (3-4 sentences). Explain the core logic of why this is positive/negative, the operational/business mechanism of the impact, and end with a structured line: "Sector Impact: [Sector Name] -> Prominent stocks: [TICKER1, TICKER2]".
5. urgency: "immediate", "short_term", or "long_term"

Events to analyze:
{events_text}

Respond with a JSON object:
{{
  "analyses": [
    {{
      "event_index": 0,
      "sentiment": "positive",
      "impact_score": 0.7,
      "affected_stocks": ["RELIANCE", "ONGC", "BPCL"],
      "summary": "Reliance Industries announces a major gas discovery in KG basin, indicating a boost to gas production and downstream energy operations. Sector Impact: Oil & Gas / Energy -> Prominent stocks: RELIANCE, ONGC, BPCL.",
      "urgency": "immediate"
    }}
  ]
}}"""


def _build_news_analysis_prompt(articles: List[dict]) -> str:
    """Build a batch analysis prompt for news articles."""
    articles_text = ""
    for i, art in enumerate(articles):
        articles_text += f"""
--- Article {i+1} ---
Source: {art.get('source', 'unknown')}
Headline: {art.get('headline', '')}
Summary: {art.get('summary', '')}
"""

    return f"""You are a senior Indian stock market analyst. Conduct a deep, in-depth financial analysis of the following news articles/clusters to assess their impact on the Indian stock market (NSE/BSE).

For EACH article, analyze the detailed content (do not just read the headline) and determine:
1. sentiment: "positive", "negative", or "neutral"
2. impact_score: float from -1.0 (extremely negative) to 1.0 (extremely positive)
3. affected_stocks: List of NSE stock symbols directly impacted OR prominent sector leaders if a sector is broadly impacted. E.g., if IT is impacted, list ["TCS", "INFY", "HCLTECH", "WIPRO"]; if Private Banking, list ["HDFCBANK", "ICICIBANK", "AXISBANK", "KOTAKBANK"]; if Auto, list ["TATAMOTORS", "MARUTI", "M&M"]; if Metal, list ["TATASTEEL", "JINDALSTEL", "HINDALCO"].
4. summary: In-depth financial and operational analysis (3-4 sentences). Do not just state a high-level summary; explain the business implications, regulatory context, or financial metrics mentioned in the details. End with a structured line: "Sector Impact: [Sector Name] -> Prominent stocks: [TICKER1, TICKER2]".
5. category: One of: "market_update", "earnings", "ipo", "policy", "global_market", "sector_news", "stock_specific", "general"

Articles to analyze:
{articles_text}

Respond with a JSON object:
{{
  "analyses": [
    {{
      "article_index": 0,
      "sentiment": "positive",
      "impact_score": 0.5,
      "category": "market_update",
      "affected_stocks": ["TCS", "INFY", "HCLTECH"],
      "summary": "The US Fed hints at interest rate cuts, which will likely prompt global enterprises to resume deferred IT spending budgets, benefiting top-tier Indian IT service exporters. Sector Impact: IT Services -> Prominent stocks: TCS, INFY, HCLTECH.",
      "urgency": "short_term"
    }}
  ]
}}"""


def _build_filing_analysis_prompt(filing: dict) -> str:
    """Build analysis prompt for a company filing."""
    return f"""You are a senior Indian stock market analyst. Conduct a deep, in-depth financial analysis of this company filing and assess its market impact.

Filing Details:
Type: {filing.get('filing_type', 'unknown')}
Company: {filing.get('symbol', 'Unknown')}
Title: {filing.get('title', '')}
Period: {filing.get('period', 'N/A')}

Extracted Content (if available):
{(filing.get('extracted_text', '') or 'No text available')[:3000]}

Provide your analysis as JSON:
{{
  "sentiment": "positive/negative/neutral",
  "impact_score": 0.0,
  "summary": "In-depth financial analysis detailing growth vectors, balance sheet/operational metrics, margin shifts, and the long-term sector outlook. Sector Impact: [Sector Name] -> Prominent stocks: [TICKER1, TICKER2]",
  "key_metrics": {{
    "revenue_growth": "X%",
    "profit_change": "X%",
    "margin_percentage": "X%",
    "notable_items": ["item1", "item2"]
  }},
  "affected_stocks": ["{filing.get('symbol', '')}"],
  "recommendation": "Detailed recommendation for long-term and short-term investors"
}}"""


# ─── Analysis Processing ───────────────────────────────────────────────────

def analyze_pending_events(db: Session) -> int:
    """Analyze market events one by one for deep, focused analysis."""
    config = get_intel_config()
    if not config.ai.get("enabled", True):
        return 0
    
    batch_size = config.ai.get("batch_size", 10)
    
    # Find unanalyzed events
    pending = db.query(MarketEvent).filter(
        MarketEvent.ai_analyzed_at.is_(None)
    ).order_by(MarketEvent.event_time.desc()).limit(batch_size).all()
    
    if not pending:
        return 0
    
    count = 0
    alert_threshold = config.ai.get("thresholds", {}).get("alert_threshold", 0.6)
    critical_threshold = config.ai.get("thresholds", {}).get("critical_threshold", 0.85)
    
    for event in pending:
        try:
            event_data = [{
                "event_type": event.event_type,
                "source": event.source,
                "symbol": event.symbol,
                "title": event.title,
                "description": event.description,
                "event_time": event.event_time.isoformat() if event.event_time else "",
            }]
            
            prompt = _build_event_analysis_prompt(event_data)
            result, provider = _call_llm_for_analysis(prompt)
            event.ai_provider = provider
            
            if not result or "analyses" not in result or not result["analyses"]:
                # Mark as analyzed with neutral defaults so it doesn't retry forever
                event.ai_sentiment = "neutral"
                event.ai_impact_score = 0.0
                event.ai_summary = event.title
                event.ai_affected_stocks = json.dumps([event.symbol] if event.symbol else [])
                event.ai_analyzed_at = datetime.utcnow()
                db.commit()
                count += 1
                continue
            
            analysis = result["analyses"][0]
            event.ai_sentiment = analysis.get("sentiment", "neutral")
            event.ai_impact_score = analysis.get("impact_score", 0.0)
            event.ai_summary = analysis.get("summary", "")
            event.ai_affected_stocks = json.dumps(analysis.get("affected_stocks", []))
            event.ai_analyzed_at = datetime.utcnow()
            
            # Auto-assign category if missing
            if not event.category:
                from app.services.nse_bse_scraper import _classify_event_category
                event.category = _classify_event_category(event.event_type, event.title)
            
            db.commit()
            count += 1
            
            # Generate alert if high impact
            impact = abs(analysis.get("impact_score", 0.0))
            if impact >= alert_threshold:
                _create_alert_from_event(db, event, analysis, alert_threshold, critical_threshold)
        except Exception as e:
            logger.error(f"Error analyzing event {event.id}: {e}")
            db.rollback()
            continue
    
    logger.info(f"Analyzed {count} market events")
    return count


def analyze_pending_news(db: Session) -> int:
    """Analyze news stories one by one for deep, focused analysis and assign category."""
    config = get_intel_config()
    if not config.ai.get("enabled", True):
        return 0
    
    batch_size = config.ai.get("batch_size", 10)
    
    # Find unanalyzed stories
    pending_stories = db.query(NewsStory).filter(
        NewsStory.ai_analyzed_at.is_(None)
    ).order_by(NewsStory.last_published.desc()).limit(batch_size).all()
    
    if not pending_stories:
        return 0
    
    count = 0
    alert_threshold = config.ai.get("thresholds", {}).get("alert_threshold", 0.6)
    critical_threshold = config.ai.get("thresholds", {}).get("critical_threshold", 0.85)
    
    for story in pending_stories:
        try:
            # Fetch the actual articles belonging to this clustered story
            news_items = db.query(NewsItem).filter(NewsItem.story_id == story.id).all()
            combined_text = "\n".join([
                f"Outlet ({item.source}): {item.headline}. Details: {item.summary or ''}"
                for item in news_items
            ])
            
            story_data = [{
                "source": "story_cluster",
                "headline": story.headline,
                "summary": combined_text or f"Clustered story with {story.article_count} sources.",
            }]
            
            prompt = _build_news_analysis_prompt(story_data)
            result, provider = _call_llm_for_analysis(prompt)
            story.ai_provider = provider
            
            if not result or "analyses" not in result or not result["analyses"]:
                # Mark as analyzed with neutral defaults so it doesn't retry forever
                story.ai_sentiment = "neutral"
                story.ai_impact_score = 0.0
                story.ai_summary = story.headline
                story.ai_affected_stocks = json.dumps([])
                story.category = "general"
                story.ai_analyzed_at = datetime.utcnow()
                
                db.query(NewsItem).filter(NewsItem.story_id == story.id).update({
                    "ai_sentiment": "neutral",
                    "ai_impact_score": 0.0,
                    "ai_provider": provider,
                    "ai_analyzed_at": datetime.utcnow(),
                })
                db.commit()
                count += 1
                continue
            
            analysis = result["analyses"][0]
            story.ai_sentiment = analysis.get("sentiment", "neutral")
            story.ai_impact_score = analysis.get("impact_score", 0.0)
            story.ai_summary = analysis.get("summary", "")
            story.ai_affected_stocks = json.dumps(analysis.get("affected_stocks", []))
            story.category = analysis.get("category", "general")
            story.ai_analyzed_at = datetime.utcnow()
            
            # Also update individual articles in this story
            db.query(NewsItem).filter(NewsItem.story_id == story.id).update({
                "ai_sentiment": analysis.get("sentiment", "neutral"),
                "ai_impact_score": analysis.get("impact_score", 0.0),
                "ai_provider": provider,
                "ai_analyzed_at": datetime.utcnow(),
            })
            
            db.commit()
            count += 1
            
            # Generate alert if high impact
            impact = abs(analysis.get("impact_score", 0.0))
            if impact >= alert_threshold:
                _create_alert_from_story(db, story, analysis, alert_threshold, critical_threshold)
        except Exception as e:
            logger.error(f"Error analyzing news story {story.id}: {e}")
            db.rollback()
            continue
            
    logger.info(f"Analyzed {count} news stories")
    return count


def analyze_pending_filings(db: Session) -> int:
    """Analyze company filings that haven't been processed by AI yet."""
    config = get_intel_config()
    if not config.ai.get("enabled", True):
        return 0
    
    # Process filings one at a time (they can be large)
    pending = db.query(CompanyFiling).filter(
        CompanyFiling.ai_analyzed_at.is_(None)
    ).order_by(CompanyFiling.filed_at.desc()).limit(5).all()
    
    if not pending:
        return 0
    
    count = 0
    alert_threshold = config.ai.get("thresholds", {}).get("alert_threshold", 0.6)
    critical_threshold = config.ai.get("thresholds", {}).get("critical_threshold", 0.85)
    
    for filing in pending:
        try:
            filing_data = {
                "filing_type": filing.filing_type,
                "symbol": filing.symbol,
                "title": filing.title,
                "period": filing.period,
                "extracted_text": filing.extracted_text,
            }
            
            prompt = _build_filing_analysis_prompt(filing_data)
            result = _call_llm_for_analysis(prompt)
            
            if result:
                filing.ai_sentiment = result.get("sentiment", "neutral")
                filing.ai_summary = result.get("summary", "")
                filing.ai_key_metrics = json.dumps(result.get("key_metrics", {}))
                filing.ai_analyzed_at = datetime.utcnow()
                count += 1
                
                impact = abs(result.get("impact_score", 0.0))
                if impact >= alert_threshold:
                    severity = "critical" if impact >= critical_threshold else "high" if impact >= alert_threshold else "medium"
                    alert = AIAlert(
                        alert_type="filing_analysis",
                        severity=severity,
                        symbol=filing.symbol,
                        title=f"📄 {filing.symbol}: {filing.filing_type.replace('_', ' ').title()} — {result.get('sentiment', 'neutral').upper()}",
                        description=result.get("summary", filing.title),
                        source_filing_id=filing.id,
                    )
                    db.add(alert)
        except Exception as e:
            logger.error(f"Error analyzing filing {filing.id}: {e}")
    
    if count > 0:
        db.commit()
        logger.info(f"Analyzed {count} company filings")
    return count


# ─── Alert Generation ──────────────────────────────────────────────────────

def _create_alert_from_event(
    db: Session, event: MarketEvent, analysis: dict,
    alert_threshold: float, critical_threshold: float
):
    """Create an AIAlert from a high-impact market event."""
    impact = abs(analysis.get("impact_score", 0.0))
    sentiment = analysis.get("sentiment", "neutral")
    
    if impact >= critical_threshold:
        severity = "critical"
    elif impact >= alert_threshold:
        severity = "high"
    else:
        severity = "medium"
    
    # Map event types to alert types
    alert_type_map = {
        "bulk_deal": "bulk_deal",
        "block_deal": "bulk_deal",
        "insider_trade": "insider_buy" if sentiment == "positive" else "insider_sell",
        "announcement": "high_impact",
        "result": "result_beat" if sentiment == "positive" else "result_miss",
        "social": "breaking_news",
    }
    alert_type = alert_type_map.get(event.event_type, "high_impact")
    
    # Emoji for alert type
    emoji_map = {
        "bulk_deal": "📊",
        "insider_buy": "🟢",
        "insider_sell": "🔴",
        "high_impact": "⚡",
        "result_beat": "📈",
        "result_miss": "📉",
        "breaking_news": "🔥",
    }
    emoji = emoji_map.get(alert_type, "⚡")
    
    alert = AIAlert(
        alert_type=alert_type,
        severity=severity,
        symbol=event.symbol,
        title=f"{emoji} {event.symbol or 'Market'}: {event.title[:200]}",
        description=analysis.get("summary", event.title),
        source_event_id=event.id,
    )
    db.add(alert)


def _create_alert_from_story(
    db: Session, story: NewsStory, analysis: dict,
    alert_threshold: float, critical_threshold: float
):
    """Create an AIAlert from a high-impact news story."""
    impact = abs(analysis.get("impact_score", 0.0))
    
    if impact >= critical_threshold:
        severity = "critical"
    elif impact >= alert_threshold:
        severity = "high"
    else:
        severity = "medium"
    
    symbols = story.symbols or ""
    primary_symbol = symbols.split(",")[0] if symbols else None
    
    alert = AIAlert(
        alert_type="breaking_news",
        severity=severity,
        symbol=primary_symbol,
        title=f"📰 {story.headline[:200]}",
        description=f"{analysis.get('summary', '')} [Covered by {story.article_count} sources]",
        source_story_id=story.id,
    )
    db.add(alert)


# ─── Main Analysis Loop Entry Point ──────────────────────────────────────

def run_analysis_cycle(db: Session) -> Dict[str, int]:
    """
    Run one cycle of AI analysis on all pending items.
    Called by the background scheduler every ai_analysis_queue interval.
    """
    config = get_intel_config()
    if not config.ai.get("enabled", True):
        return {}
    
    results = {}
    
    try:
        results["events"] = analyze_pending_events(db)
    except Exception as e:
        logger.error(f"Event analysis failed: {e}")
        results["events"] = 0
    
    try:
        results["news"] = analyze_pending_news(db)
    except Exception as e:
        logger.error(f"News analysis failed: {e}")
        results["news"] = 0
    
    try:
        results["filings"] = analyze_pending_filings(db)
    except Exception as e:
        logger.error(f"Filing analysis failed: {e}")
        results["filings"] = 0
    
    total = sum(results.values())
    if total > 0:
        logger.info(f"AI analysis cycle complete: {total} items analyzed — {results}")
    
    return results


# ─── AI Stock Suggestions ────────────────────────────────────────────────

def get_ai_stock_suggestions(db: Session, limit: int = 10) -> List[dict]:
    """
    Get AI suggestions for stocks that may be impacted positively or negatively
    based on recent events, news, and filings.
    """
    now = datetime.utcnow()
    last_24h = now - timedelta(hours=24)
    
    suggestions = []
    
    # Get high-impact events from last 24h
    high_impact_events = db.query(MarketEvent).filter(
        MarketEvent.ai_analyzed_at.isnot(None),
        MarketEvent.event_time >= last_24h,
        or_(
            MarketEvent.ai_impact_score >= 0.5,
            MarketEvent.ai_impact_score <= -0.5,
        )
    ).order_by(MarketEvent.ai_impact_score.desc()).limit(limit).all()
    
    for event in high_impact_events:
        suggestions.append({
            "symbol": event.symbol,
            "direction": "positive" if (event.ai_impact_score or 0) > 0 else "negative",
            "impact_score": event.ai_impact_score,
            "reason": event.ai_summary or event.title,
            "source_type": "event",
            "event_type": event.event_type,
            "source": event.source,
            "time": event.event_time.isoformat() if event.event_time else None,
        })
    
    # Get high-impact news stories from last 24h
    high_impact_stories = db.query(NewsStory).filter(
        NewsStory.ai_analyzed_at.isnot(None),
        NewsStory.last_published >= last_24h,
        or_(
            NewsStory.ai_impact_score >= 0.5,
            NewsStory.ai_impact_score <= -0.5,
        )
    ).order_by(NewsStory.ai_impact_score.desc()).limit(limit).all()
    
    for story in high_impact_stories:
        primary_symbol = (story.symbols or "").split(",")[0] if story.symbols else None
        suggestions.append({
            "symbol": primary_symbol,
            "direction": "positive" if (story.ai_impact_score or 0) > 0 else "negative",
            "impact_score": story.ai_impact_score,
            "reason": story.ai_summary or story.headline,
            "source_type": "news",
            "article_count": story.article_count,
            "time": story.last_published.isoformat() if story.last_published else None,
        })
    
    # Sort by absolute impact score
    suggestions.sort(key=lambda x: abs(x.get("impact_score", 0)), reverse=True)
    return suggestions[:limit]


def get_market_sentiment(db: Session, force_refresh: bool = False) -> dict:
    """
    Get or generate global market sentiment.
    If cached sentiment is less than 5 minutes old, return it.
    Otherwise, run LLM synthesis to update overall market sentiment and cache it.
    This fetches real-time news from the wider internet (Google News RSS)
    and computes real-time buyer/seller volumes from watchlist quotes.
    """
    from app.database import AICache, Watchlist, SessionStore
    from app.services.feed_factory import get_feed
    from app.services.news_aggregator import fetch_google_news_rss
    from app.config import PROVIDER
    
    # Check cache first
    now = datetime.utcnow()
    cached = db.query(AICache).filter(AICache.instrument_key == "MARKET_SENTIMENT").first()
    if cached and not force_refresh:
        # Check age
        age_seconds = (now - cached.fetched_at).total_seconds()
        if age_seconds < 300: # 5 minutes
            try:
                return json.loads(cached.comment)
            except Exception:
                pass
                
    # 1. Fetch wider internet news using Google News RSS query
    internet_news = []
    try:
        internet_news = fetch_google_news_rss("Indian stock market macro global financial news", 15)
    except Exception as news_err:
        logger.error(f"Error fetching wider internet news: {news_err}")
        
    news_text = ""
    if internet_news:
        for idx, art in enumerate(internet_news[:8]):
            news_text += f"- Headline: {art.get('headline')} (Source: {art.get('source_name') or 'Google News'})\n"
    else:
        # Local DB news story fallback
        recent_stories = db.query(NewsStory).filter(NewsStory.ai_sentiment.isnot(None))\
                           .order_by(NewsStory.last_published.desc()).limit(8).all()
        for idx, s in enumerate(recent_stories):
            news_text += f"- Headline: {s.headline} (Local Sentiment: {s.ai_sentiment})\n"

    # 2. Compute advances, declines, and buyer/seller volumes from Nifty 50 and sectoral stocks
    advances = 0
    declines = 0
    total_buying_vol = 0
    total_selling_vol = 0
    
    # Resolve feed
    feed = get_feed()
    stored_session = db.query(SessionStore).filter(SessionStore.provider == PROVIDER).first()
    if stored_session:
        feed.set_access_token(stored_session.access_token)
    else:
        feed.set_access_token(None)
        
    from app.config import DEFAULT_NIFTY_50
    keys = [item["key"] for item in DEFAULT_NIFTY_50]
    try:
        quotes = feed.get_quotes(keys)
        for key, quote in quotes.items():
            last_price = quote.get("last_price", 0.0)
            ohlc = quote.get("ohlc", {})
            close_price = ohlc.get("close", 0.0)
            volume = quote.get("volume", 0)
            
            if last_price > close_price:
                advances += 1
                total_buying_vol += volume
            elif last_price < close_price:
                declines += 1
                total_selling_vol += volume
    except Exception as quote_err:
        logger.error(f"Error fetching quotes for Nifty 50 depth calculation: {quote_err}")
            
    # Calculate percentages
    total_vol = total_buying_vol + total_selling_vol
    if total_vol > 0:
        buyers_pct = round((total_buying_vol / total_vol) * 100, 1)
        sellers_pct = round((total_selling_vol / total_vol) * 100, 1)
    else:
        total_count = advances + declines
        if total_count > 0:
            buyers_pct = round((advances / total_count) * 100, 1)
            sellers_pct = round((declines / total_count) * 100, 1)
        else:
            buyers_pct = 50.0
            sellers_pct = 50.0

    prompt = f"""
    You are a senior Indian stock market analyst. Synthesize the overall market sentiment based on wider internet news and real-time buying/selling statistics.
    
    Market Depth / Order Book Activity (Nifty 50 & Sectoral Stocks):
    - Advances (stocks up): {advances}
    - Declines (stocks down): {declines}
    - Estimated Buying Pressure (Advancing volume): {buyers_pct}% of total trades
    - Estimated Selling Pressure (Declining volume): {sellers_pct}% of total trades
    
    Recent business news from the wider internet:
    {news_text}
    
    Task:
    Provide a JSON object containing the overall market mood/sentiment matching the following schema:
    1. "sentiment": One of "Bullish", "Bearish", or "Neutral".
    2. "score": An overall sentiment score from -1.0 (extremely bearish) to 1.0 (extremely bullish).
    3. "summary": A concise 2-sentence market commentary summarizing the mood, catalysts, and buy/sell pressure.
    4. "drivers": A list of 3-5 top drivers. Each driver must be a JSON object containing:
       - "title": A short summary of the driving event/news.
       - "impact": "Positive", "Negative", or "Neutral".
       - "source": The source of the driver (e.g. "NSE", "Moneycontrol", "Mint").
       - "time": A relative or absolute time string indicating when it occurred (e.g. "1h ago" or "9 Jul, 10:19 AM").
    5. "sectors": A JSON object containing:
       - "positive": List of 1-3 sectors that are positively impacted by these developments.
       - "negative": List of 1-3 sectors that are negatively impacted by these developments.
       
    Respond ONLY with a valid JSON object matching the requested schema. Do not enclose it in any extra text or tags.
    """
    
    sentiment_data = None
    try:
        sentiment_data = _call_llm_for_analysis(prompt)
    except Exception as e:
        logger.error(f"Error calling LLM for market sentiment: {e}")
        
    if not sentiment_data:
        # Fallback synthesis
        avg_score = round(0.5 if advances > declines else -0.5 if declines > advances else 0.0, 2)
        sentiment = "Bullish" if avg_score > 0.2 else "Bearish" if avg_score < -0.2 else "Neutral"
        
        drivers = []
        if internet_news:
            for art in internet_news[:3]:
                drivers.append({
                    "title": art.get("headline"),
                    "impact": "Neutral",
                    "source": art.get("source_name") or "Google News",
                    "time": "Recent"
                })
                
        sentiment_data = {
            "sentiment": sentiment,
            "score": avg_score,
            "summary": f"Calculated market mood is {sentiment} based on {advances} advancing and {declines} declining stocks. LLM provider keys were unavailable for full synthesis.",
            "drivers": drivers,
            "sectors": {
                "positive": ["IT"] if avg_score > 0 else [],
                "negative": ["Banking"] if avg_score < 0 else []
            }
        }
        
    # Append computed volume and advance-decline statistics
    sentiment_data["last_updated"] = now.isoformat() + "Z"
    sentiment_data["advances"] = advances
    sentiment_data["declines"] = declines
    sentiment_data["buyers_pct"] = buyers_pct
    sentiment_data["sellers_pct"] = sellers_pct
    
    # Cache it
    try:
        if cached:
            cached.comment = json.dumps(sentiment_data)
            cached.fetched_at = now
        else:
            new_cache = AICache(
                instrument_key="MARKET_SENTIMENT",
                comment=json.dumps(sentiment_data),
                fetched_at=now
            )
            db.add(new_cache)
        db.commit()
    except Exception as cache_err:
        db.rollback()
        logger.error(f"Error caching market sentiment: {cache_err}")
        
    return sentiment_data


def reanalyze_single_item(db: Session, item_type: str, item_id: int) -> Optional[dict]:
    """
    Force re-analysis of a single item (event, story, news, filing) using configured LLM providers.
    Sequence: Primary (Groq) -> Fallbacks (Gemini, OpenAI, Anthropic, Ollama local model).
    """
    item_type = item_type.lower()
    
    if item_type in ("event", "market_event"):
        event = db.query(MarketEvent).filter(MarketEvent.id == item_id).first()
        if not event:
            return None
        event_data = [{
            "event_type": event.event_type,
            "source": event.source,
            "symbol": event.symbol,
            "title": event.title,
            "description": event.description,
            "event_time": event.event_time.isoformat() if event.event_time else "",
        }]
        prompt = _build_event_analysis_prompt(event_data)
        result = _call_llm_for_analysis(prompt)
        if result and "analyses" in result and result["analyses"]:
            analysis = result["analyses"][0]
            event.ai_sentiment = analysis.get("sentiment", "neutral")
            event.ai_impact_score = analysis.get("impact_score", 0.0)
            event.ai_summary = analysis.get("summary", "")
            event.ai_affected_stocks = json.dumps(analysis.get("affected_stocks", []))
            event.ai_analyzed_at = datetime.utcnow()
            db.commit()
            return {
                "id": event.id,
                "type": "event",
                "sentiment": event.ai_sentiment,
                "impact_score": event.ai_impact_score,
                "summary": event.ai_summary,
                "affected_stocks": json.loads(event.ai_affected_stocks or "[]"),
                "analyzed_at": event.ai_analyzed_at.isoformat()
            }

    elif item_type in ("story", "news_story"):
        story = db.query(NewsStory).filter(NewsStory.id == item_id).first()
        if not story:
            return None
        articles_data = [{
            "source": story.best_source_tier,
            "headline": story.headline,
            "summary": story.ai_summary or story.headline,
        }]
        prompt = _build_news_analysis_prompt(articles_data)
        result = _call_llm_for_analysis(prompt)
        if result and "analyses" in result and result["analyses"]:
            analysis = result["analyses"][0]
            story.ai_sentiment = analysis.get("sentiment", "neutral")
            story.ai_impact_score = analysis.get("impact_score", 0.0)
            story.ai_summary = analysis.get("summary", "")
            story.ai_affected_stocks = json.dumps(analysis.get("affected_stocks", []))
            story.ai_analyzed_at = datetime.utcnow()
            if analysis.get("category"):
                story.category = analysis.get("category")
            db.commit()
            return {
                "id": story.id,
                "type": "story",
                "sentiment": story.ai_sentiment,
                "impact_score": story.ai_impact_score,
                "summary": story.ai_summary,
                "affected_stocks": json.loads(story.ai_affected_stocks or "[]"),
                "category": story.category,
                "analyzed_at": story.ai_analyzed_at.isoformat()
            }

    elif item_type in ("news", "news_item"):
        item = db.query(NewsItem).filter(NewsItem.id == item_id).first()
        if not item:
            return None
        articles_data = [{
            "source": item.source,
            "headline": item.headline,
            "summary": item.summary or item.headline,
        }]
        prompt = _build_news_analysis_prompt(articles_data)
        result = _call_llm_for_analysis(prompt)
        if result and "analyses" in result and result["analyses"]:
            analysis = result["analyses"][0]
            item.ai_sentiment = analysis.get("sentiment", "neutral")
            item.ai_impact_score = analysis.get("impact_score", 0.0)
            item.ai_analyzed_at = datetime.utcnow()
            db.commit()
            return {
                "id": item.id,
                "type": "news",
                "sentiment": item.ai_sentiment,
                "impact_score": item.ai_impact_score,
                "analyzed_at": item.ai_analyzed_at.isoformat()
            }

    elif item_type in ("filing", "company_filing"):
        filing = db.query(CompanyFiling).filter(CompanyFiling.id == item_id).first()
        if not filing:
            return None
        filing_data = {
            "filing_type": filing.filing_type,
            "symbol": filing.symbol,
            "title": filing.title,
            "period": filing.period,
            "extracted_text": filing.extracted_text,
        }
        prompt = _build_filing_analysis_prompt(filing_data)
        result = _call_llm_for_analysis(prompt)
        if result:
            filing.ai_sentiment = result.get("sentiment", "neutral")
            filing.ai_summary = result.get("summary", "")
            filing.ai_key_metrics = json.dumps(result.get("key_metrics", {}))
            filing.ai_analyzed_at = datetime.utcnow()
            db.commit()
            return {
                "id": filing.id,
                "type": "filing",
                "sentiment": filing.ai_sentiment,
                "summary": filing.ai_summary,
                "key_metrics": json.loads(filing.ai_key_metrics or "{}"),
                "analyzed_at": filing.ai_analyzed_at.isoformat()
            }

    return None

