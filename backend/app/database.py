from datetime import datetime
import json
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Float, text, Boolean, Index
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from app.config import DATABASE_URL

engine = create_engine(
    DATABASE_URL, connect_args={"check_same_thread": False}
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

# ─── Existing Tables (unchanged) ────────────────────────────────────────────

class Watchlist(Base):
    __tablename__ = "watchlist"
    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String(50), nullable=False)
    name = Column(String(200), nullable=False)
    instrument_key = Column(String(100), unique=True, index=True, nullable=False)
    is_holding = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

class NewsCache(Base):
    __tablename__ = "news_cache"
    # One entry per stock. Stores news as a serialized JSON list of articles
    instrument_key = Column(String(100), primary_key=True, index=True)
    news_json = Column(Text, nullable=False)
    fetched_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class AICache(Base):
    __tablename__ = "ai_cache"
    # One entry per stock.
    instrument_key = Column(String(100), primary_key=True, index=True)
    comment = Column(Text, nullable=False)
    resistance_levels = Column(Text, nullable=True)
    support_levels = Column(Text, nullable=True)
    recommendation = Column(String(50), nullable=True)
    sector = Column(String(100), nullable=True)
    analyst_recommendations = Column(Text, nullable=True)
    fetched_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class SessionStore(Base):
    __tablename__ = "session_store"
    provider = Column(String(50), primary_key=True, index=True) # "upstox"
    access_token = Column(Text, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class HistoricalPriceCache(Base):
    __tablename__ = "historical_price_cache"
    instrument_key = Column(String(100), primary_key=True, index=True)
    period = Column(String(20), primary_key=True, index=True)
    price = Column(Float, nullable=False)
    fetched_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ─── Intelligence Platform Tables ───────────────────────────────────────────

class MarketEvent(Base):
    """
    Unified table for corporate announcements, bulk/block deals, board meetings,
    insider trading, and social media signals.
    """
    __tablename__ = "market_events"
    id = Column(Integer, primary_key=True, autoincrement=True)
    # Type: announcement, bulk_deal, block_deal, board_meeting, insider_trade, social, result
    event_type = Column(String(50), nullable=False, index=True)
    # Source exchange/platform: nse, bse, twitter, etc.
    source = Column(String(50), nullable=False, index=True)
    # Stock symbol (e.g., RELIANCE)
    symbol = Column(String(50), index=True, nullable=True)
    # Upstox-style instrument key (e.g., NSE_EQ|INE002A01018)
    instrument_key = Column(String(100), index=True, nullable=True)
    title = Column(Text, nullable=False)
    description = Column(Text, nullable=True)
    url = Column(Text, nullable=True)
    # Original raw data as JSON string for debugging / reprocessing
    raw_data = Column(Text, nullable=True)
    # SHA-256 hash of (source + event_type + title + event_time) for deduplication
    event_hash = Column(String(64), unique=True, index=True, nullable=False)
    event_time = Column(DateTime, index=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    # AI Analysis fields — populated by the AI analyzer
    ai_sentiment = Column(String(20), nullable=True)   # positive, negative, neutral
    ai_impact_score = Column(Float, nullable=True)       # -1.0 to 1.0
    ai_summary = Column(Text, nullable=True)
    # JSON list of affected stock symbols (e.g., ["RELIANCE", "ONGC"])
    ai_affected_stocks = Column(Text, nullable=True)
    ai_analyzed_at = Column(DateTime, nullable=True)
    # Category for filtering: board_meeting, sebi_filing, corporate_action, earnings, insider_trade, bulk_deal, general
    category = Column(String(50), index=True, nullable=True)

    __table_args__ = (
        Index("ix_market_events_type_time", "event_type", "event_time"),
        Index("ix_market_events_symbol_time", "symbol", "event_time"),
    )


class NewsStory(Base):
    """
    Groups multiple related news articles (from different sources) into one story.
    Prevents duplicate display and provides a consolidated view.
    """
    __tablename__ = "news_stories"
    id = Column(Integer, primary_key=True, autoincrement=True)
    # Canonical headline (from the first/best article in the cluster)
    headline = Column(Text, nullable=False)
    # Comma-separated list of symbols mentioned
    symbols = Column(Text, nullable=True)
    # Number of articles in this story cluster
    article_count = Column(Integer, default=1)
    # Best source tier (1=premium, 2=major, 3=other)
    best_source_tier = Column(Integer, default=3)
    # First and last publish times in the cluster
    first_published = Column(DateTime, index=True, nullable=False)
    last_published = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    # AI Analysis
    ai_sentiment = Column(String(20), nullable=True)
    ai_impact_score = Column(Float, nullable=True)
    ai_summary = Column(Text, nullable=True)
    ai_affected_stocks = Column(Text, nullable=True)
    ai_analyzed_at = Column(DateTime, nullable=True)
    # Category for filtering: market_update, earnings, ipo, policy, global_market, sector_news, stock_specific, general
    category = Column(String(50), index=True, nullable=True)


class NewsItem(Base):
    """Individual news articles from all sources, linked to a NewsStory for dedup."""
    __tablename__ = "news_items"
    id = Column(Integer, primary_key=True, autoincrement=True)
    # Source name: moneycontrol, economic_times, business_standard, livemint, reuters, etc.
    source = Column(String(50), nullable=False, index=True)
    # Source reliability tier: 1=premium (Reuters/Bloomberg), 2=major (MC/ET), 3=other
    source_tier = Column(Integer, default=3)
    headline = Column(Text, nullable=False)
    summary = Column(Text, nullable=True)
    url = Column(Text, nullable=False)
    # Normalized URL for dedup (stripped query params, tracking codes)
    url_hash = Column(String(64), unique=True, index=True, nullable=False)
    # Primary stock symbol this article relates to (if identifiable)
    symbol = Column(String(50), index=True, nullable=True)
    # All mentioned symbols as JSON list
    mentioned_symbols = Column(Text, nullable=True)
    published_at = Column(DateTime, index=True, nullable=False)
    fetched_at = Column(DateTime, default=datetime.utcnow)
    # FK to NewsStory (which story cluster this belongs to)
    story_id = Column(Integer, index=True, nullable=True)
    # AI fields
    ai_sentiment = Column(String(20), nullable=True)
    ai_impact_score = Column(Float, nullable=True)
    ai_analyzed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_news_items_source_published", "source", "published_at"),
        Index("ix_news_items_symbol_published", "symbol", "published_at"),
    )


class CompanyFiling(Base):
    """Quarterly results, investor presentations, conference call transcripts, annual reports."""
    __tablename__ = "company_filings"
    id = Column(Integer, primary_key=True, autoincrement=True)
    # Type: quarterly_result, investor_presentation, transcript, annual_report
    filing_type = Column(String(50), nullable=False, index=True)
    symbol = Column(String(50), nullable=False, index=True)
    instrument_key = Column(String(100), index=True, nullable=True)
    title = Column(Text, nullable=False)
    url = Column(Text, nullable=True)
    url_hash = Column(String(64), unique=True, index=True, nullable=True)
    # Extracted text content from PDF (first N pages)
    extracted_text = Column(Text, nullable=True)
    # Reporting period (e.g., "Q1FY25", "FY2024")
    period = Column(String(20), nullable=True)
    filed_at = Column(DateTime, index=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    # AI Analysis
    ai_summary = Column(Text, nullable=True)
    # JSON object with key financial metrics
    ai_key_metrics = Column(Text, nullable=True)
    ai_sentiment = Column(String(20), nullable=True)
    ai_analyzed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_company_filings_symbol_filed", "symbol", "filed_at"),
    )


class AIAlert(Base):
    """AI-generated alerts for high-impact market events."""
    __tablename__ = "ai_alerts"
    id = Column(Integer, primary_key=True, autoincrement=True)
    # Alert category: high_impact, bulk_deal, insider_buy, insider_sell,
    # result_beat, result_miss, breaking_news, sector_move
    alert_type = Column(String(50), nullable=False, index=True)
    # Severity: critical, high, medium, low
    severity = Column(String(20), nullable=False, index=True)
    symbol = Column(String(50), index=True, nullable=True)
    title = Column(Text, nullable=False)
    description = Column(Text, nullable=False)
    # Source references
    source_event_id = Column(Integer, nullable=True)    # FK to MarketEvent.id
    source_news_id = Column(Integer, nullable=True)     # FK to NewsItem.id
    source_filing_id = Column(Integer, nullable=True)   # FK to CompanyFiling.id
    source_story_id = Column(Integer, nullable=True)    # FK to NewsStory.id
    # User interaction
    is_read = Column(Boolean, default=False, index=True)
    is_notified = Column(Boolean, default=False)         # Telegram/browser notification sent
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    __table_args__ = (
        Index("ix_ai_alerts_severity_created", "severity", "created_at"),
    )


# ─── Database Initialization ────────────────────────────────────────────────

def init_db():
    Base.metadata.create_all(bind=engine)
    # Safely alter table to add columns if migrating from an older database schema
    with engine.begin() as conn:
        # Existing migrations
        _safe_alter(conn, "ALTER TABLE ai_cache ADD COLUMN resistance_levels TEXT")
        _safe_alter(conn, "ALTER TABLE ai_cache ADD COLUMN recommendation VARCHAR(50)")
        _safe_alter(conn, "ALTER TABLE ai_cache ADD COLUMN sector VARCHAR(100)")
        _safe_alter(conn, "ALTER TABLE ai_cache ADD COLUMN support_levels TEXT")
        _safe_alter(conn, "ALTER TABLE ai_cache ADD COLUMN analyst_recommendations TEXT")
        _safe_alter(conn, "ALTER TABLE watchlist ADD COLUMN is_holding BOOLEAN DEFAULT 0")


def _safe_alter(conn, sql: str):
    """Execute an ALTER TABLE statement, ignoring errors if column already exists."""
    try:
        conn.execute(text(sql))
    except Exception:
        pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
