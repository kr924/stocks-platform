"""
Intelligence API Router — Endpoints for the Market Intelligence Dashboard.

Provides endpoints for:
- Live intelligence feed with filters and pagination
- Real-time SSE streaming of new events
- AI alerts management
- Stock-specific event timeline
- Trending stocks, bulk deals, insider trades, filings
- AI stock suggestions
- Configuration management
"""
import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import or_, and_, func, desc, case

from app.database import (
    get_db, MarketEvent, NewsItem, NewsStory, CompanyFiling, AIAlert, SystemSetting
)
from app.services.deduplication import to_iso_utc

from pydantic import BaseModel

logger = logging.getLogger("app.intelligence_router")

router = APIRouter(prefix="/api/intelligence", tags=["intelligence"])


class IntelligenceSettingsUpdate(BaseModel):
    local_llm_enabled: bool


@router.get("/settings")
def get_intelligence_settings():
    """Get AI Intelligence configuration including local LLM status."""
    from app.services.intel_config import get_intel_config
    cfg = get_intel_config()
    return {
        "local_llm_enabled": cfg.local_llm_enabled
    }


@router.post("/settings")
def update_intelligence_settings(body: IntelligenceSettingsUpdate):
    """Enable or disable Local Ollama LLM to manage CPU load."""
    from app.services.intel_config import get_intel_config
    cfg = get_intel_config()
    cfg.set_local_llm_enabled(body.local_llm_enabled)
    return {
        "status": "success",
        "local_llm_enabled": cfg.local_llm_enabled,
        "message": f"Local LLM is now {'ENABLED' if cfg.local_llm_enabled else 'DISABLED (0% CPU)'}"
    }


# ─── Live Intelligence Feed ──────────────────────────────────────────────

@router.get("/feed")
def get_intelligence_feed(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=10, le=200),
    event_type: Optional[str] = None,       # announcement, bulk_deal, insider_trade, social, result
    source: Optional[str] = None,            # nse, bse, twitter, moneycontrol, etc.
    sentiment: Optional[str] = None,         # positive, negative, neutral
    symbol: Optional[str] = None,
    category: Optional[str] = None,          # board_meeting, sebi_filing, earnings, etc.
    search: Optional[str] = None,            # free-text search across title/description
    hours: int = Query(24, ge=1, le=168),    # Time window (default: last 24h)
    db: Session = Depends(get_db),
):
    """Get the live intelligence feed with filtering, pagination, and AI analysis."""
    since = datetime.utcnow() - timedelta(hours=hours)
    
    # Map high-level categories to event_types
    if category == "news":
        event_type = "news"
    elif category == "filing":
        event_type = "filing"

    # Build unified feed from multiple sources
    feed_items = []
    
    # 1. Market Events
    events_q = db.query(MarketEvent).filter(MarketEvent.event_time >= since)
    if event_type:
        events_q = events_q.filter(MarketEvent.event_type == event_type)
    if source:
        events_q = events_q.filter(MarketEvent.source == source)
    if sentiment:
        events_q = events_q.filter(MarketEvent.ai_sentiment == sentiment)
    if symbol:
        events_q = events_q.filter(MarketEvent.symbol == symbol.upper())

    CLOUD_PROVIDERS = ["groq", "gemini", "openrouter", "openai", "anthropic", "cloud", "financial_results"]

    # Define strict Finance News SQL condition according to user rules:
    # 1. Subject contains "Outcome of Board Meeting" AND details contain "finan"
    # 2. Subject contains "Updates" AND details contain "finan", "revenue", or "profit"
    # 3. Subject or details contain "Acquisition", "Merger", "Dividend", "Bonus", "Split", "Financial Result", "Quarterly Result", "Audited Result"
    # 4. Analyzed by Cloud Finance AI
    finance_news_cond = or_(
        and_(
            MarketEvent.title.ilike("%Outcome of Board Meeting%"),
            MarketEvent.description.ilike("%finan%")
        ),
        and_(
            MarketEvent.title.ilike("%Update%"),
            or_(
                MarketEvent.description.ilike("%finan%"),
                MarketEvent.description.ilike("%revenue%"),
                MarketEvent.description.ilike("%profit%")
            )
        ),
        MarketEvent.title.ilike("%Acquisition%"),
        MarketEvent.title.ilike("%Merger%"),
        MarketEvent.title.ilike("%Dividend%"),
        MarketEvent.title.ilike("%Bonus%"),
        MarketEvent.title.ilike("%Split%"),
        MarketEvent.title.ilike("%Financial Result%"),
        MarketEvent.title.ilike("%Quarterly Result%"),
        MarketEvent.title.ilike("%Audited Result%"),
        MarketEvent.description.ilike("%Acquisition%"),
        MarketEvent.description.ilike("%Merger%"),
        MarketEvent.description.ilike("%Dividend%"),
        MarketEvent.description.ilike("%Bonus%"),
        MarketEvent.description.ilike("%Split%"),
        MarketEvent.category == "financial_results",
        MarketEvent.ai_provider.in_(CLOUD_PROVIDERS)
    )

    auto_skip_cond = or_(
        MarketEvent.ai_provider == "auto_skip",
        MarketEvent.category == "auto_skip",
        MarketEvent.ai_summary.ilike("Auto-skipped%")
    )

    # Category handling (Strictly Mutually Exclusive across all 4 tabs)
    if category == "all_exchange":
        # Category: All Live NSE/BSE Exchange Announcements
        events_q = events_q.filter(
            MarketEvent.source.in_(["nse", "bse"]),
            ~auto_skip_cond
        )
    elif category == "auto_skip":
        # Category 4: NSE/BSE Auto-Skipped
        events_q = events_q.filter(auto_skip_cond)
    elif category in ("finance_ai", "all", None):
        # Category 1: Finance News (Default View) — Strictly NSE & BSE exchange announcements
        events_q = events_q.filter(
            MarketEvent.source.in_(["nse", "bse"]),
            ~auto_skip_cond,
            finance_news_cond
        )
    elif category in ("nse_bse_general", "nse_bse_active"):
        # Category 2: NSE/BSE General Updates
        events_q = events_q.filter(
            MarketEvent.source.in_(["nse", "bse"]),
            ~auto_skip_cond,
            ~finance_news_cond
        )
    elif category == "other_news":
        # Category 3: Other Market News (Media stories & web updates)
        events_q = events_q.filter(
            MarketEvent.source.notin_(["nse", "bse"]),
            ~auto_skip_cond,
            ~finance_news_cond
        )
    elif category not in ("news", "filing"):
        events_q = events_q.filter(MarketEvent.category == category)
    if search:
        search_pattern = f"%{search}%"
        events_q = events_q.filter(
            or_(
                MarketEvent.title.ilike(search_pattern),
                MarketEvent.description.ilike(search_pattern),
                MarketEvent.ai_summary.ilike(search_pattern),
                MarketEvent.symbol.ilike(search_pattern),
            )
        )
    
    events = events_q.order_by(MarketEvent.event_time.desc()).limit(200).all()
    
    for e in events:
        feed_items.append({
            "id": f"event_{e.id}",
            "type": "event",
            "event_type": e.event_type,
            "source": e.source,
            "symbol": e.symbol,
            "title": e.title,
            "description": e.description,
            "url": e.url,
            "time": to_iso_utc(e.event_time),
            "created_at": to_iso_utc(e.created_at),
            "ai_sentiment": e.ai_sentiment,
            "ai_impact_score": e.ai_impact_score,
            "ai_summary": e.ai_summary,
            "ai_provider": getattr(e, "ai_provider", None),
            "ai_affected_stocks": json.loads(e.ai_affected_stocks) if e.ai_affected_stocks else [],
            "category": e.category,
        })
    
    # 2. News Stories (deduplicated) - ONLY include under "other_news" or explicit "news" event_type
    if (not event_type or event_type == "news") and category in ("other_news", "news"):
        stories_q = db.query(NewsStory).filter(NewsStory.last_published >= since)
        if sentiment:
            stories_q = stories_q.filter(NewsStory.ai_sentiment == sentiment)
        if symbol:
            stories_q = stories_q.filter(NewsStory.symbols.contains(symbol.upper()))
        if category and category not in ("finance_ai", "all", "other_news", "news"):
            stories_q = stories_q.filter(NewsStory.category == category)
        if search:
            search_pattern = f"%{search}%"
            stories_q = stories_q.filter(
                or_(
                    NewsStory.headline.ilike(search_pattern),
                    NewsStory.ai_summary.ilike(search_pattern),
                    NewsStory.symbols.ilike(search_pattern),
                )
            )
        
        stories = stories_q.order_by(NewsStory.last_published.desc()).limit(100).all()
        
        for s in stories:
            # Get the articles in this story
            articles = db.query(NewsItem).filter(NewsItem.story_id == s.id).order_by(
                NewsItem.source_tier.asc(), NewsItem.published_at.desc()
            ).all()
            
            feed_items.append({
                "id": f"story_{s.id}",
                "type": "news_story",
                "event_type": "news",
                "source": "multi",
                "symbol": (s.symbols or "").split(",")[0] if s.symbols else None,
                "title": s.headline,
                "description": s.ai_summary or s.headline,
                "url": articles[0].url if articles else None,
                "time": to_iso_utc(s.last_published),
                "created_at": to_iso_utc(s.created_at),
                "ai_sentiment": s.ai_sentiment,
                "ai_impact_score": s.ai_impact_score,
                "ai_summary": s.ai_summary,
                "ai_provider": getattr(s, "ai_provider", None),
                "ai_affected_stocks": json.loads(s.ai_affected_stocks) if s.ai_affected_stocks else [],
                "article_count": s.article_count,
                "best_source_tier": s.best_source_tier,
                "symbols": s.symbols,
                "category": s.category or "news",
                "articles": [{
                    "source": a.source,
                    "headline": a.headline,
                    "url": a.url,
                    "published_at": to_iso_utc(a.published_at),
                    "source_tier": a.source_tier,
                } for a in articles[:5]],
            })
    
    # 3. Company Filings
    if not event_type or event_type == "filing":
        filings_q = db.query(CompanyFiling).filter(CompanyFiling.filed_at >= since)
        if symbol:
            filings_q = filings_q.filter(CompanyFiling.symbol == symbol.upper())
        if sentiment:
            filings_q = filings_q.filter(CompanyFiling.ai_sentiment == sentiment)
        if search:
            search_pattern = f"%{search}%"
            filings_q = filings_q.filter(
                or_(
                    CompanyFiling.title.ilike(search_pattern),
                    CompanyFiling.ai_summary.ilike(search_pattern),
                    CompanyFiling.symbol.ilike(search_pattern),
                )
            )
        
        filings = filings_q.order_by(CompanyFiling.filed_at.desc()).limit(50).all()
        
        for f in filings:
            feed_items.append({
                "id": f"filing_{f.id}",
                "type": "filing",
                "event_type": f.filing_type,
                "source": "filing",
                "symbol": f.symbol,
                "title": f.title,
                "description": f.ai_summary or f.title,
                "url": f.url,
                "time": to_iso_utc(f.filed_at),
                "created_at": to_iso_utc(f.created_at),
                "ai_sentiment": f.ai_sentiment,
                "ai_impact_score": None,
                "ai_summary": f.ai_summary,
                "ai_affected_stocks": [f.symbol] if f.symbol else [],
                "category": "filing",
                "period": f.period,
                "ai_key_metrics": json.loads(f.ai_key_metrics) if f.ai_key_metrics else None,
            })
    
    # Sort all items by time (newest first)
    feed_items.sort(key=lambda x: x.get("time", ""), reverse=True)
    
    # Paginate
    total = len(feed_items)
    start = (page - 1) * page_size
    end = start + page_size
    paginated = feed_items[start:end]
    
    return {
        "items": paginated,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size,
    }


# ─── AI Alerts ──────────────────────────────────────────────────────────

@router.get("/alerts")
def get_alerts(
    unread_only: bool = False,
    severity: Optional[str] = None,
    symbol: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """Get AI-generated alerts, sorted by severity and time."""
    q = db.query(AIAlert)
    
    if unread_only:
        q = q.filter(AIAlert.is_read == False)
    if severity:
        q = q.filter(AIAlert.severity == severity)
    if symbol:
        q = q.filter(AIAlert.symbol == symbol.upper())
    
    alerts = q.order_by(
        # Sort: critical first, then high, then by time
        case(
            (AIAlert.severity == "critical", 0),
            (AIAlert.severity == "high", 1),
            (AIAlert.severity == "medium", 2),
            else_=3
        ),
        AIAlert.created_at.desc()
    ).limit(limit).all()
    
    unread_count = db.query(func.count(AIAlert.id)).filter(AIAlert.is_read == False).scalar()
    
    return {
        "alerts": [{
            "id": a.id,
            "alert_type": a.alert_type,
            "severity": a.severity,
            "symbol": a.symbol,
            "title": a.title,
            "description": a.description,
            "is_read": a.is_read,
            "created_at": a.created_at.isoformat() if a.created_at else None,
            "source_event_id": a.source_event_id,
            "source_news_id": a.source_news_id,
            "source_filing_id": a.source_filing_id,
            "source_story_id": a.source_story_id,
        } for a in alerts],
        "unread_count": unread_count,
    }


@router.post("/alerts/{alert_id}/read")
def mark_alert_read(alert_id: int, db: Session = Depends(get_db)):
    """Mark an alert as read."""
    alert = db.query(AIAlert).filter(AIAlert.id == alert_id).first()
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    alert.is_read = True
    db.commit()
    return {"status": "success"}


@router.post("/alerts/read-all")
def mark_all_alerts_read(db: Session = Depends(get_db)):
    """Mark all alerts as read."""
    db.query(AIAlert).filter(AIAlert.is_read == False).update({"is_read": True})
    db.commit()
    return {"status": "success"}


# ─── Stock-Specific Events ──────────────────────────────────────────────

@router.get("/stock/{symbol}/events")
def get_stock_events(
    symbol: str,
    hours: int = Query(72, ge=1, le=720),
    db: Session = Depends(get_db),
):
    """Get all intelligence events for a specific stock."""
    symbol_upper = symbol.upper()
    since = datetime.utcnow() - timedelta(hours=hours)
    
    # Market events
    events = db.query(MarketEvent).filter(
        MarketEvent.symbol == symbol_upper,
        MarketEvent.event_time >= since
    ).order_by(MarketEvent.event_time.desc()).all()
    
    # News
    news = db.query(NewsItem).filter(
        or_(
            NewsItem.symbol == symbol_upper,
            NewsItem.mentioned_symbols.contains(symbol_upper)
        ),
        NewsItem.published_at >= since
    ).order_by(NewsItem.published_at.desc()).all()
    
    # Filings
    filings = db.query(CompanyFiling).filter(
        CompanyFiling.symbol == symbol_upper,
        CompanyFiling.filed_at >= since
    ).order_by(CompanyFiling.filed_at.desc()).all()
    
    # Alerts
    alerts = db.query(AIAlert).filter(
        AIAlert.symbol == symbol_upper,
        AIAlert.created_at >= since
    ).order_by(AIAlert.created_at.desc()).all()
    
    return {
        "symbol": symbol_upper,
        "events": [{
            "id": e.id,
            "event_type": e.event_type,
            "source": e.source,
            "title": e.title,
            "description": e.description,
            "url": e.url,
            "time": e.event_time.isoformat() if e.event_time else None,
            "ai_sentiment": e.ai_sentiment,
            "ai_impact_score": e.ai_impact_score,
            "ai_summary": e.ai_summary,
        } for e in events],
        "news": [{
            "id": n.id,
            "source": n.source,
            "headline": n.headline,
            "url": n.url,
            "published_at": n.published_at.isoformat() if n.published_at else None,
            "ai_sentiment": n.ai_sentiment,
            "ai_impact_score": n.ai_impact_score,
        } for n in news],
        "filings": [{
            "id": f.id,
            "filing_type": f.filing_type,
            "title": f.title,
            "url": f.url,
            "period": f.period,
            "filed_at": f.filed_at.isoformat() if f.filed_at else None,
            "ai_summary": f.ai_summary,
            "ai_sentiment": f.ai_sentiment,
        } for f in filings],
        "alerts": [{
            "id": a.id,
            "alert_type": a.alert_type,
            "severity": a.severity,
            "title": a.title,
            "description": a.description,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        } for a in alerts],
    }


# ─── Trending Stocks ────────────────────────────────────────────────────

@router.get("/trending")
def get_trending_stocks(
    hours: int = Query(6, ge=1, le=48),
    limit: int = Query(20, ge=5, le=50),
    db: Session = Depends(get_db),
):
    """Get stocks with the most activity in the last N hours."""
    since = datetime.utcnow() - timedelta(hours=hours)
    
    # Count events per symbol
    event_counts = db.query(
        MarketEvent.symbol,
        func.count(MarketEvent.id).label("event_count"),
        func.avg(MarketEvent.ai_impact_score).label("avg_impact"),
    ).filter(
        MarketEvent.symbol.isnot(None),
        MarketEvent.event_time >= since
    ).group_by(MarketEvent.symbol).order_by(
        desc("event_count")
    ).limit(limit).all()
    
    # Count news mentions per symbol
    news_counts = db.query(
        NewsItem.symbol,
        func.count(NewsItem.id).label("news_count"),
    ).filter(
        NewsItem.symbol.isnot(None),
        NewsItem.published_at >= since
    ).group_by(NewsItem.symbol).all()
    
    news_map = {n.symbol: n.news_count for n in news_counts}
    
    trending = []
    for ec in event_counts:
        if not ec.symbol:
            continue
        trending.append({
            "symbol": ec.symbol,
            "event_count": ec.event_count,
            "news_count": news_map.get(ec.symbol, 0),
            "total_activity": ec.event_count + news_map.get(ec.symbol, 0),
            "avg_impact_score": round(ec.avg_impact or 0, 3),
        })
    
    trending.sort(key=lambda x: x["total_activity"], reverse=True)
    return {"trending": trending[:limit], "hours": hours}


# ─── Bulk/Block Deals ───────────────────────────────────────────────────

@router.get("/bulk-deals")
def get_bulk_deals(
    days: int = Query(7, ge=1, le=30),
    limit: int = Query(50, ge=10, le=200),
    db: Session = Depends(get_db),
):
    """Get latest bulk and block deals with AI analysis."""
    since = datetime.utcnow() - timedelta(days=days)
    
    deals = db.query(MarketEvent).filter(
        MarketEvent.event_type.in_(["bulk_deal", "block_deal"]),
        MarketEvent.event_time >= since
    ).order_by(MarketEvent.event_time.desc()).limit(limit).all()
    
    return {
        "deals": [{
            "id": d.id,
            "deal_type": d.event_type,
            "source": d.source,
            "symbol": d.symbol,
            "title": d.title,
            "description": d.description,
            "time": d.event_time.isoformat() if d.event_time else None,
            "ai_sentiment": d.ai_sentiment,
            "ai_impact_score": d.ai_impact_score,
            "ai_summary": d.ai_summary,
        } for d in deals],
        "total": len(deals),
    }


# ─── Insider Trades ────────────────────────────────────────────────────

@router.get("/insider-trades")
def get_insider_trades(
    days: int = Query(7, ge=1, le=30),
    limit: int = Query(50, ge=10, le=200),
    db: Session = Depends(get_db),
):
    """Get latest insider trading activity with AI analysis."""
    since = datetime.utcnow() - timedelta(days=days)
    
    trades = db.query(MarketEvent).filter(
        MarketEvent.event_type == "insider_trade",
        MarketEvent.event_time >= since
    ).order_by(MarketEvent.event_time.desc()).limit(limit).all()
    
    return {
        "trades": [{
            "id": t.id,
            "source": t.source,
            "symbol": t.symbol,
            "title": t.title,
            "description": t.description,
            "time": t.event_time.isoformat() if t.event_time else None,
            "ai_sentiment": t.ai_sentiment,
            "ai_impact_score": t.ai_impact_score,
            "ai_summary": t.ai_summary,
        } for t in trades],
        "total": len(trades),
    }


# ─── Company Filings ───────────────────────────────────────────────────

@router.get("/filings")
def get_filings(
    filing_type: Optional[str] = None,
    symbol: Optional[str] = None,
    days: int = Query(30, ge=1, le=90),
    limit: int = Query(50, ge=10, le=200),
    db: Session = Depends(get_db),
):
    """Get recent company filings with AI summaries."""
    since = datetime.utcnow() - timedelta(days=days)
    
    q = db.query(CompanyFiling).filter(CompanyFiling.filed_at >= since)
    if filing_type:
        q = q.filter(CompanyFiling.filing_type == filing_type)
    if symbol:
        q = q.filter(CompanyFiling.symbol == symbol.upper())
    
    filings = q.order_by(CompanyFiling.filed_at.desc()).limit(limit).all()
    
    return {
        "filings": [{
            "id": f.id,
            "filing_type": f.filing_type,
            "symbol": f.symbol,
            "title": f.title,
            "url": f.url,
            "period": f.period,
            "filed_at": f.filed_at.isoformat() if f.filed_at else None,
            "ai_summary": f.ai_summary,
            "ai_key_metrics": json.loads(f.ai_key_metrics) if f.ai_key_metrics else None,
            "ai_sentiment": f.ai_sentiment,
        } for f in filings],
        "total": len(filings),
    }


# ─── AI Stock Suggestions ──────────────────────────────────────────────

@router.get("/suggestions")
def get_stock_suggestions(
    limit: int = Query(10, ge=5, le=50),
    db: Session = Depends(get_db),
):
    """Get AI-powered stock impact suggestions based on recent intelligence."""
    from app.services.ai_analyzer import get_ai_stock_suggestions
    suggestions = get_ai_stock_suggestions(db, limit)
    return {"suggestions": suggestions}


@router.get("/market-sentiment")
def get_global_market_sentiment(
    force_refresh: bool = Query(False),
    db: Session = Depends(get_db),
):
    """Get synthesized global market sentiment and high-impact drivers, cached for 5 mins."""
    from app.services.ai_analyzer import get_market_sentiment
    sentiment = get_market_sentiment(db, force_refresh)
    return sentiment


# ─── Dashboard Stats ──────────────────────────────────────────────────

@router.get("/stats")
def get_dashboard_stats(db: Session = Depends(get_db)):
    """Get summary statistics for the intelligence dashboard."""
    now = datetime.utcnow()
    last_24h = now - timedelta(hours=24)
    last_1h = now - timedelta(hours=1)
    
    # Counts
    events_24h = db.query(func.count(MarketEvent.id)).filter(
        MarketEvent.event_time >= last_24h
    ).scalar() or 0
    
    news_24h = db.query(func.count(NewsItem.id)).filter(
        NewsItem.published_at >= last_24h
    ).scalar() or 0
    
    stories_24h = db.query(func.count(NewsStory.id)).filter(
        NewsStory.last_published >= last_24h
    ).scalar() or 0
    
    filings_24h = db.query(func.count(CompanyFiling.id)).filter(
        CompanyFiling.filed_at >= last_24h
    ).scalar() or 0
    
    unread_alerts = db.query(func.count(AIAlert.id)).filter(
        AIAlert.is_read == False
    ).scalar() or 0
    
    critical_alerts = db.query(func.count(AIAlert.id)).filter(
        AIAlert.is_read == False,
        AIAlert.severity == "critical"
    ).scalar() or 0
    
    # Sentiment distribution (last 24h)
    sentiment_dist = {}
    for sentiment in ["positive", "negative", "neutral"]:
        count = db.query(func.count(MarketEvent.id)).filter(
            MarketEvent.event_time >= last_24h,
            MarketEvent.ai_sentiment == sentiment
        ).scalar() or 0
        sentiment_dist[sentiment] = count
    
    # Event type distribution
    event_types = db.query(
        MarketEvent.event_type,
        func.count(MarketEvent.id)
    ).filter(
        MarketEvent.event_time >= last_24h
    ).group_by(MarketEvent.event_type).all()
    
    return {
        "events_24h": events_24h,
        "news_articles_24h": news_24h,
        "news_stories_24h": stories_24h,
        "filings_24h": filings_24h,
        "unread_alerts": unread_alerts,
        "critical_alerts": critical_alerts,
        "sentiment_distribution": sentiment_dist,
        "event_types": {et: c for et, c in event_types},
        "last_updated": now.isoformat(),
    }


# ─── Configuration Endpoint ────────────────────────────────────────────

@router.get("/config")
def get_config():
    """Get the current intelligence configuration (sanitized — no API keys)."""
    from app.services.intel_config import get_intel_config
    config = get_intel_config()
    raw = config.raw.copy()
    
    # Sanitize — remove sensitive values
    if "social_media" in raw:
        twitter = raw.get("social_media", {}).get("twitter", {})
        if twitter.get("bearer_token"):
            twitter["bearer_token"] = "***configured***"
    if "notifications" in raw:
        telegram = raw.get("notifications", {}).get("telegram", {})
        if telegram.get("bot_token"):
            telegram["bot_token"] = "***configured***"
    if "news" in raw:
        newsdata = raw.get("news", {}).get("sources", {}).get("newsdata_io", {})
        if newsdata.get("api_key"):
            newsdata["api_key"] = "***configured***"
    
    return raw


@router.post("/config/reload")
def reload_config():
    """Reload the intelligence configuration from disk."""
    from app.services.intel_config import get_intel_config
    config = get_intel_config()
    config.reload()
    return {"status": "success", "message": "Configuration reloaded"}


# ─── New Active Stocks & Upcoming Earnings Endpoints ─────────────────────

_1Y_RETURNS_CACHE = {}
_1Y_RETURNS_CACHE_TIME = 0.0
_1Y_RETURNS_CACHE_TTL = 86400.0  # 24-hour TTL (runs strictly ONCE PER DAY)


def _load_1y_returns_cache_from_db(db: Session):
    global _1Y_RETURNS_CACHE, _1Y_RETURNS_CACHE_TIME
    try:
        setting = db.query(SystemSetting).filter(SystemSetting.key == "1y_returns_cache_24h").first()
        if setting and setting.value:
            data = json.loads(setting.value)
            _1Y_RETURNS_CACHE = data.get("cache", {})
            _1Y_RETURNS_CACHE_TIME = data.get("timestamp", 0.0)
    except Exception:
        pass


def _save_1y_returns_cache_to_db(db: Session):
    global _1Y_RETURNS_CACHE, _1Y_RETURNS_CACHE_TIME
    try:
        val_str = json.dumps({"cache": _1Y_RETURNS_CACHE, "timestamp": _1Y_RETURNS_CACHE_TIME})
        setting = db.query(SystemSetting).filter(SystemSetting.key == "1y_returns_cache_24h").first()
        if not setting:
            setting = SystemSetting(key="1y_returns_cache_24h", value=val_str)
            db.add(setting)
        else:
            setting.value = val_str
        db.commit()
    except Exception:
        db.rollback()


@router.get("/upcoming-earnings")
def get_upcoming_earnings(db: Session = Depends(get_db)):
    """Get stocks with upcoming earnings sorted chronologically by meeting date with 1-year returns."""
    import json, re, time
    from datetime import datetime, timedelta
    from sqlalchemy import or_, desc

    today = datetime.utcnow().date()
    end_date = today + timedelta(days=30)
    
    bms = db.query(MarketEvent).filter(
        or_(
            (MarketEvent.event_type == "board_meeting"),
            (MarketEvent.category == "earnings"),
            (MarketEvent.category == "board_meeting")
        )
    ).all()
    
    def parse_date(date_str):
        if not date_str:
            return None
        date_str = str(date_str).strip()
        for fmt in ('%d-%b-%Y', '%d-%m-%Y', '%Y-%m-%d', '%d/%m/%Y', '%b %d, %Y'):
            try:
                return datetime.strptime(date_str, fmt).date()
            except ValueError:
                pass
        m = re.search(r'(\d{1,2})[-/\s]+([A-Za-z]{3})[-/\s]+(\d{2,4})', date_str)
        if m:
            try:
                day, month_str, year = m.groups()
                if len(year) == 2:
                    year = '20' + year
                return datetime.strptime(f'{day}-{month_str}-{year}', '%d-%b-%Y').date()
            except Exception:
                pass
        return None

    upcoming = []
    seen = set()

    for bm in bms:
        symbol = bm.symbol
        if not symbol:
            continue
        symbol = symbol.strip().upper()
        raw = {}
        if bm.raw_data:
            try:
                raw = json.loads(bm.raw_data)
            except Exception:
                pass

        m_date_str = raw.get("bm_date", raw.get("meetingDate", raw.get("bm_dt", "")))
        m_date = parse_date(m_date_str)
        if not m_date:
            if bm.event_time and bm.event_time.date() >= (today - timedelta(days=1)):
                m_date = bm.event_time.date()
            else:
                continue

        # Filter strictly to earnings disclosures on or after today (clearing old past dates)
        if m_date >= today and m_date <= end_date:
            key = (symbol, m_date)
            if key not in seen:
                seen.add(key)
                purpose = raw.get("bm_purpose", raw.get("purpose", bm.title or "Financial Results"))
                purpose_lower = str(purpose).lower()
                is_earnings = any(k in purpose_lower for k in [
                    "financial", "result", "quarterly", "audited", "unaudited", "q1", "q2", "q3", "q4", "earning", "profit", "loss"
                ])
                if not is_earnings:
                    continue
                upcoming.append({
                    "id": bm.id,
                    "symbol": symbol,
                    "date": m_date.isoformat(),
                    "meeting_date": m_date.isoformat(),
                    "display_date": m_date.strftime("%d %b %Y"),
                    "purpose": purpose,
                    "title": bm.title or f"{symbol}: Board Meeting",
                    "description": bm.description or purpose,
                    "created_at": bm.created_at.isoformat() if bm.created_at else None,
                    "ltp": 0.0,
                    "prev_close": 0.0,
                    "day_high": 0.0,
                    "change_pct": 0.0
                })

    upcoming.sort(key=lambda x: x["date"])

    # ── Live Upstox Market Feed 1-Day Intraday Quote Enrichment ──
    try:
        from app.main import get_nse_equities, get_active_feed
        eqs = get_nse_equities()
        sym_to_key = {item["symbol"].upper(): item["key"] for item in eqs if item.get("symbol") and item.get("key")}
        
        upcoming_keys = []
        for item in upcoming:
            sym = item["symbol"].upper()
            ikey = sym_to_key.get(sym) or f"NSE_EQ|{sym}"
            item["instrument_key"] = ikey
            upcoming_keys.append(ikey)
            upcoming_keys.append(ikey.replace("|", ":"))

        if upcoming_keys:
            feed = get_active_feed()
            live_quotes = feed.get_quotes(upcoming_keys)
            for item in upcoming:
                sym = item["symbol"].upper()
                ikey = item.get("instrument_key", "")
                
                q = live_quotes.get(ikey) or live_quotes.get(ikey.replace("|", ":")) or live_quotes.get(sym)
                if q:
                    last_price = q.get("last_price", 0.0)
                    ohlc = q.get("ohlc", {})
                    prev_close = q.get("prev_close") or ohlc.get("close", 0.0)
                    day_high = ohlc.get("high", 0.0) or q.get("high", 0.0)
                    if last_price > 0:
                        item["ltp"] = round(last_price, 2)
                        if prev_close > 0:
                            item["prev_close"] = round(prev_close, 2)
                            item["change_pct"] = round(((last_price - prev_close) / prev_close) * 100, 2)
                        if day_high > 0:
                            item["day_high"] = round(day_high, 2)
                        if "depth_buy_pct" in q and q["depth_buy_pct"] is not None:
                            item["depth_buy_pct"] = round(q["depth_buy_pct"], 1)
                        if "depth_sell_pct" in q and q["depth_sell_pct"] is not None:
                            item["depth_sell_pct"] = round(q["depth_sell_pct"], 1)
                        if "total_buy_qty" in q and q["total_buy_qty"] is not None:
                            item["buy_qty"] = q["total_buy_qty"]
                        if "total_sell_qty" in q and q["total_sell_qty"] is not None:
                            item["sell_qty"] = q["total_sell_qty"]
    except Exception as err:
        logger.warning(f"Live Upstox feed quote enrichment error: {err}")

    return upcoming


@router.get("/active-stocks")
def get_active_stocks(db: Session = Depends(get_db)):
    """Get stocks with news/actions in the past 24 hours, prioritized by highest AI impact score."""
    from datetime import datetime, timedelta
    since = datetime.utcnow() - timedelta(hours=24)
    
    active_stocks = {}
    
    def get_impact_rank(sentiment, score):
        # Rank by absolute score magnitude
        val = abs(score) if score is not None else 0.0
        # Give a small rank boost to clear positive/negative sentiments if score is missing
        if val == 0.0 and sentiment in ["positive", "negative"]:
            return 0.5
        return val

    # 1. Market Events (all sentiments)
    events = db.query(MarketEvent).filter(
        MarketEvent.event_time >= since
    ).all()
    
    for e in events:
        if not e.symbol:
            continue
        symbol = e.symbol.strip().upper()
        if symbol == "COMPANY" or len(symbol) < 2:
            continue
            
        time_str = to_iso_utc(e.event_time)
        sentiment = e.ai_sentiment or "neutral"
        score = e.ai_impact_score or 0.0
        
        # Calculate impact rank
        new_rank = get_impact_rank(sentiment, score)
        
        if symbol not in active_stocks:
            should_update = True
        else:
            existing_rank = get_impact_rank(active_stocks[symbol]["sentiment"], active_stocks[symbol].get("impact_score", 0.0))
            if new_rank > existing_rank:
                should_update = True
            elif new_rank == existing_rank and e.event_time > active_stocks[symbol]["timestamp"]:
                should_update = True
            else:
                should_update = False
                
        if should_update:
            active_stocks[symbol] = {
                "symbol": symbol,
                "timestamp": e.event_time,
                "time": time_str,
                "sentiment": sentiment,
                "impact_score": score,
                "title": e.title,
                "type": "event"
            }
            
    # 2. News Stories (all sentiments)
    stories = db.query(NewsStory).filter(
        NewsStory.last_published >= since
    ).all()
    
    for s in stories:
        if not s.symbols:
            continue
        symbols = [sym.strip().upper() for sym in s.symbols.split(",") if sym.strip()]
        time_str = to_iso_utc(s.last_published)
        sentiment = s.ai_sentiment or "neutral"
        score = s.ai_impact_score or 0.0
        
        # Calculate impact rank
        new_rank = get_impact_rank(sentiment, score)
        
        for symbol in symbols:
            if symbol == "COMPANY" or len(symbol) < 2:
                continue
                
            if symbol not in active_stocks:
                should_update = True
            else:
                existing_rank = get_impact_rank(active_stocks[symbol]["sentiment"], active_stocks[symbol].get("impact_score", 0.0))
                if new_rank > existing_rank:
                    should_update = True
                elif new_rank == existing_rank and s.last_published > active_stocks[symbol]["timestamp"]:
                    should_update = True
                else:
                    should_update = False
                    
            if should_update:
                active_stocks[symbol] = {
                    "symbol": symbol,
                    "timestamp": s.last_published,
                    "time": time_str,
                    "sentiment": sentiment,
                    "impact_score": score,
                    "title": s.headline,
                    "type": "news"
                }
                
    # Convert dict to list and sort by timestamp descending
    result = list(active_stocks.values())
    result.sort(key=lambda x: x["timestamp"], reverse=True)
    
    # Remove timestamp datetime object before returning JSON
    for item in result:
        item.pop("timestamp", None)
        
    return result


# ─── Real-Time SSE Stream ────────────────────────────────────────────────

@router.post("/reanalyze/{item_type}/{item_id}")
def reanalyze_item_endpoint(
    item_type: str, 
    item_id: str, 
    provider: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    """
    Manually trigger AI re-analysis on a news story, market event, or filing.
    Accepts raw integer ID or prefixed string ID (e.g. 'event_2262', 'story_105', '2262').
    Optional query param `provider`: 'groq', 'openrouter', 'gemini', 'openai', 'anthropic', 'ollama'.
    """
    from app.services.ai_analyzer import reanalyze_single_item
    
    # Strip any text prefix (e.g., 'event_2262' -> 2262)
    clean_id_str = str(item_id).split("_")[-1]
    try:
        numeric_id = int(clean_id_str)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid numeric item ID: '{item_id}'")
        
    try:
        result = reanalyze_single_item(db, item_type, numeric_id, provider_name=provider)
        if not result:
            raise HTTPException(status_code=404, detail=f"Item '{item_type}' with ID {item_id} not found or analysis failed.")
        return result
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Re-analysis via provider '{provider or 'auto'}' failed: {str(e)}")


@router.get("/logs")
def get_ai_activity_logs(limit: int = 50):
    """Get recent real-time AI activity logs for live UI display."""
    from app.services.ai_log_tracker import get_recent_ai_logs
    return get_recent_ai_logs(limit)


@router.post("/poll")
async def trigger_manual_poll():
    """
    Manually trigger immediate execution of all scrapers (NSE/BSE, news, filings).
    """
    import asyncio
    from app.main import (
        _run_corporate_announcements_scraper,
        _run_other_nse_bse_scraper,
        _run_news_aggregator,
        _run_filings_scraper
    )
    asyncio.create_task(asyncio.to_thread(_run_corporate_announcements_scraper))
    asyncio.create_task(asyncio.to_thread(_run_other_nse_bse_scraper))
    asyncio.create_task(asyncio.to_thread(_run_news_aggregator))
    asyncio.create_task(asyncio.to_thread(_run_filings_scraper))
    return {"status": "ok", "message": "Triggered manual poll of all scrapers in background."}


@router.get("/stream")
async def stream_intelligence(request: Request):
    """
    Server-Sent Events (SSE) endpoint for real-time intelligence streaming.

    Pushes new market events, news stories, alerts, and filings to connected
    clients as they arrive — no polling needed.

    Event types sent:
    - connected     : Initial connection confirmation with current stats
    - new_event     : New NSE/BSE market event (announcement, deal, etc.)
    - new_news      : New news story from aggregator
    - new_alert     : New AI-generated alert
    - new_filing    : New company filing
    - heartbeat     : Keep-alive ping (every 30s)
    """
    from app.services.sse_manager import sse_manager

    queue = await sse_manager.subscribe()

    async def event_generator():
        try:
            # Send initial connection event with current stats
            db = next(get_db())
            try:
                now = datetime.utcnow()
                last_24h = now - timedelta(hours=24)
                events_count = db.query(func.count(MarketEvent.id)).filter(
                    MarketEvent.event_time >= last_24h
                ).scalar() or 0
                news_count = db.query(func.count(NewsItem.id)).filter(
                    NewsItem.published_at >= last_24h
                ).scalar() or 0
                unread_alerts = db.query(func.count(AIAlert.id)).filter(
                    AIAlert.is_read == False
                ).scalar() or 0
            finally:
                db.close()

            # Send initial 1KB comment buffer to flush Cloudflare/Nginx proxies immediately
            yield f": {' ' * 1024}\n\n"

            connected_data = json.dumps({
                "status": "connected",
                "clients": sse_manager.client_count,
                "events_24h": events_count,
                "news_24h": news_count,
                "unread_alerts": unread_alerts,
            })
            yield f"event: connected\ndata: {connected_data}\n\n"

            # Main event loop: wait for broadcasts or send heartbeats (every 5 seconds)
            while True:
                # Check if client disconnected
                if await request.is_disconnected():
                    break

                try:
                    # Wait up to 5s for a new event; if none, send heartbeat
                    message = await asyncio.wait_for(queue.get(), timeout=5.0)
                    event_type = message.get("type", "message")
                    event_data = json.dumps(message.get("data", {}), default=str)
                    yield f"event: {event_type}\ndata: {event_data}\n\n"
                except asyncio.TimeoutError:
                    # Send heartbeat every 5s to keep connection alive
                    heartbeat_data = json.dumps({
                        "clients": sse_manager.client_count,
                        "timestamp": datetime.utcnow().isoformat(),
                    })
                    yield f"event: heartbeat\ndata: {heartbeat_data}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            await sse_manager.unsubscribe(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "Content-Type": "text/event-stream",
            "X-Accel-Buffering": "no",
        },
    )
