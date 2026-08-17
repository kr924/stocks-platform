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

class EarningsBucket(Base):
    __tablename__ = "earnings_bucket"
    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String(50), nullable=False)
    name = Column(String(200), nullable=True)
    instrument_key = Column(String(100), unique=True, index=True, nullable=False)
    earnings_date = Column(String(50), nullable=True)
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
    # Stock symbol (e.g., RELIANCE) — NSE ticker, else BSE scrip id
    symbol = Column(String(50), index=True, nullable=True)
    # Registered company name, resolved via the symbol registry
    company_name = Column(String(250), nullable=True)
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
    ai_provider = Column(String(50), nullable=True)     # groq, gemini, ollama, openai, anthropic, stub
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
    ai_provider = Column(String(50), nullable=True)     # groq, gemini, ollama, openai, anthropic, stub
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
    ai_provider = Column(String(50), nullable=True)
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
    ai_provider = Column(String(50), nullable=True)      # groq, gemini, ollama, openai, anthropic, etc.
    ai_analyzed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_company_filings_symbol_filed", "symbol", "filed_at"),
    )


class SystemSetting(Base):
    """Persistent key-value system settings stored in market_tracker.db."""
    __tablename__ = "system_settings"
    key = Column(String(100), primary_key=True, index=True)
    value = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


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


# ─── Trading Automation Engine Tables ────────────────────────────────────────

class TradeConfig(Base):
    """User-configured stock trading targets with purchase date, quantity, stoploss."""
    __tablename__ = "trade_configs"
    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String(50), nullable=False, index=True)           # e.g. "RELIANCE"
    instrument_key = Column(String(100), nullable=True)               # e.g. "NSE_EQ|INE002A01018"
    purchase_date = Column(String(20), nullable=False)                 # e.g. "2026-07-30"
    quantity = Column(Integer, nullable=False, default=1)
    stoploss_pct = Column(Float, nullable=False, default=2.0)         # e.g. 2.0 means 2%
    stoploss_type = Column(String(20), default="software")            # "software" or "bracket"
    broker = Column(String(20), default="upstox")                     # "upstox" or "zerodha"
    order_type = Column(String(20), default="MARKET")                 # "MARKET" or "LIMIT"
    limit_price = Column(Float, nullable=True)                        # used when order_type = "LIMIT"
    ai_provider = Column(String(50), default="groq")                  # premium AI provider for this stock
    status = Column(String(30), default="pending", index=True)        # pending → armed → triggered → bought → sold → cancelled
    is_active = Column(Boolean, default=True, index=True)
    trigger_subject = Column(String(200), default="Outcome of Board Meeting")
    buy_price = Column(Float, nullable=True)                          # actual fill price after buy
    sell_price = Column(Float, nullable=True)                         # actual fill price after sell
    pnl = Column(Float, nullable=True)                                # profit/loss after sell
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    triggered_at = Column(DateTime, nullable=True)                    # when NSE news was detected
    bought_at = Column(DateTime, nullable=True)
    sold_at = Column(DateTime, nullable=True)
    # When a buy was requested outside trading hours: the order waits here until
    # the next open rather than being sent to a broker that will reject it.
    scheduled_for = Column(DateTime, nullable=True)


class TradeOrder(Base):
    """Executed broker orders (buy/sell) linked to a TradeConfig."""
    __tablename__ = "trade_orders"
    id = Column(Integer, primary_key=True, index=True)
    config_id = Column(Integer, nullable=False, index=True)           # FK to TradeConfig.id
    symbol = Column(String(50), nullable=False, index=True)
    side = Column(String(10), nullable=False)                         # "BUY" or "SELL"
    quantity = Column(Integer, nullable=False)
    order_type = Column(String(20), default="MARKET")                 # "MARKET" or "LIMIT"
    limit_price = Column(Float, nullable=True)
    price = Column(Float, nullable=True)                              # fill price
    stoploss_price = Column(Float, nullable=True)
    broker = Column(String(20), nullable=False)                       # "upstox" or "zerodha"
    broker_order_id = Column(String(100), nullable=True)              # broker's order ID
    broker_response = Column(Text, nullable=True)                     # raw JSON response
    status = Column(String(30), default="pending", index=True)        # pending → placed → filled → failed → cancelled
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    filled_at = Column(DateTime, nullable=True)


class TradeAILog(Base):
    """Premium AI analysis logs for purchased stocks (separate from standard AI pipeline)."""
    __tablename__ = "trade_ai_logs"
    id = Column(Integer, primary_key=True, index=True)
    config_id = Column(Integer, nullable=True, index=True)            # FK to TradeConfig.id
    symbol = Column(String(50), nullable=False, index=True)
    company_name = Column(String(250), nullable=True)
    # Correlation id shared with the PendingResultOrder that triggered this
    tracking_ref = Column(String(40), index=True, nullable=True)
    # Dispatch / receipt of the AI call itself
    ai_requested_at = Column(DateTime, nullable=True)
    ai_completed_at = Column(DateTime, nullable=True)
    provider = Column(String(50), nullable=False)                     # "custom_rest_api", "openrouter_premium", etc.
    prompt_summary = Column(Text, nullable=True)                      # brief description of what was analyzed
    ai_sentiment = Column(String(20), nullable=True)
    ai_impact_score = Column(Float, nullable=True)
    ai_summary = Column(Text, nullable=True)
    raw_response = Column(Text, nullable=True)                        # full LLM response JSON
    nse_event_title = Column(String(500), nullable=True)              # the NSE announcement that triggered this
    created_at = Column(DateTime, default=datetime.utcnow)

    # Enhanced Earnings Analysis Fields
    revenue = Column(Text, nullable=True)
    expenses = Column(Text, nullable=True)
    operating_profit = Column(Text, nullable=True)
    pbt = Column(Text, nullable=True)
    other_income = Column(Text, nullable=True)
    pat_yoy = Column(Text, nullable=True)
    growth_projection = Column(Text, nullable=True)
    broker_estimates = Column(Text, nullable=True)
    ai_suggestion = Column(String(50), nullable=True)                  # "BEATS ESTIMATES", "MISSES ESTIMATES", "BUY", "SELL", "HOLD"
    attachment_url = Column(Text, nullable=True)
    flow_used = Column(String(50), nullable=True)                     # "custom_rest_api" or "openrouter_premium"
    # Fixed metric grid as JSON: rows (revenue/expenses/other_income/pat/ebitda)
    # x columns (current_qtr / yoy_change_pct / last_year_same_qtr / estimated).
    # "NA" wherever a figure could not be extracted from the filing.
    metrics_json = Column(Text, nullable=True)
    # Result of the internal consistency checks: which figures failed to
    # reconcile, so a suspect extraction can be seen rather than just distrusted.
    validation_json = Column(Text, nullable=True)
    future_growth_outlook = Column(Text, nullable=True)
    future_projected_numbers = Column(Text, nullable=True)
    # True when the filing yielded no usable figures — suppresses any verdict
    extraction_ok = Column(Boolean, default=False)


class ResultDedupKey(Base):
    """
    Cross-channel suppression for financial results.

    The same result reaches us through up to four paths (board-meeting outcome
    and direct result filing, on each of NSE and BSE). The first path to arrive
    claims the key; later arrivals for the same key are dropped.
    """
    __tablename__ = "result_dedup_keys"
    key = Column(String(200), primary_key=True, index=True)
    symbol = Column(String(50), index=True, nullable=True)
    isin = Column(String(30), index=True, nullable=True)
    # Which exchange published it first: nse | bse
    first_source = Column(String(10), nullable=True)
    # Which channel it arrived on: board_meeting_outcome | direct_result
    channel = Column(String(30), nullable=True)
    event_id = Column(Integer, nullable=True)          # FK to MarketEvent.id
    result_date = Column(String(20), index=True, nullable=True)   # YYYY-MM-DD
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class PendingResultOrder(Base):
    """
    A financial result arrived for a stock that was NOT armed.

    Surfaces on the Auto Trading panel as an order-placement prompt. Once the
    user places (or dismisses) the order, AI analysis runs against the filing.
    """
    __tablename__ = "pending_result_orders"
    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(50), nullable=False, index=True)
    company_name = Column(String(250), nullable=True)
    instrument_key = Column(String(100), nullable=True)
    isin = Column(String(30), nullable=True)
    exchange = Column(String(10), nullable=False)      # nse | bse
    # Trading date (IST) this result belongs to, for the daily reset
    trade_date = Column(String(20), index=True, nullable=True)
    # Last traded price at the moment the result was captured. The move since
    # then is the number that actually matters on this panel — the market has
    # usually already reacted by the time you look.
    price_at_announcement = Column(Float, nullable=True)
    title = Column(Text, nullable=False)
    description = Column(Text, nullable=True)
    attachment_url = Column(Text, nullable=True)
    # ── Lifecycle timestamps (all UTC) ──
    # When the exchange published it
    event_time = Column(DateTime, nullable=False, index=True)
    # When our poller ingested it -> created_at below
    # When the arrival alert went out
    alert_sent_at = Column(DateTime, nullable=True)
    # When the earnings AI request was dispatched, and when its reply landed
    ai_requested_at = Column(DateTime, nullable=True)
    ai_completed_at = Column(DateTime, nullable=True)

    # Short human-readable correlation id (e.g. AARTI-0807-3F2A). Printed on the
    # arrival alert, the AI verdict alert and the app row so one alert can be
    # traced through to its analysis.
    tracking_ref = Column(String(40), unique=True, index=True, nullable=True)

    # Arrived after the intraday cutoff, so alerts and AI are held for the
    # next morning digest rather than firing overnight.
    deferred = Column(Boolean, default=False, index=True)
    digest_sent_at = Column(DateTime, nullable=True)
    # Screener's figures for this filing, fetched once and kept. The digest and
    # the panel both read them from here: fetching is rate limited to roughly
    # one company a second, so re-fetching a past day on demand is minutes of
    # waiting for numbers that cannot have changed.
    screener_json = Column(Text, nullable=True)

    dedup_key = Column(String(200), unique=True, index=True, nullable=False)
    # pending → ordered | dismissed | expired
    status = Column(String(20), default="pending", index=True)
    # AI analysis lifecycle: pending → deferred → running → done | failed
    ai_status = Column(String(20), default="pending", index=True)
    ai_log_id = Column(Integer, nullable=True)          # FK to TradeAILog.id
    config_id = Column(Integer, nullable=True)          # FK to TradeConfig.id once ordered
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    resolved_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_pending_result_status_created", "status", "created_at"),
    )


# ─── Database Initialization ────────────────────────────────────────────────

def init_db():
    if "sqlite" in str(engine.url):
        with engine.begin() as conn:
            try:
                conn.execute(text("PRAGMA journal_mode=WAL;"))
                conn.execute(text("PRAGMA synchronous=NORMAL;"))
            except Exception:
                pass
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
        _safe_alter(conn, "ALTER TABLE market_events ADD COLUMN ai_provider VARCHAR(50)")
        _safe_alter(conn, "ALTER TABLE news_stories ADD COLUMN ai_provider VARCHAR(50)")
        _safe_alter(conn, "ALTER TABLE news_items ADD COLUMN ai_provider VARCHAR(50)")
        _safe_alter(conn, "ALTER TABLE company_filings ADD COLUMN ai_provider VARCHAR(50)")

        # Migrations for TradeAILog earnings metrics
        _safe_alter(conn, "ALTER TABLE trade_ai_logs ADD COLUMN revenue TEXT")
        _safe_alter(conn, "ALTER TABLE trade_ai_logs ADD COLUMN expenses TEXT")
        _safe_alter(conn, "ALTER TABLE trade_ai_logs ADD COLUMN operating_profit TEXT")
        _safe_alter(conn, "ALTER TABLE trade_ai_logs ADD COLUMN pbt TEXT")
        _safe_alter(conn, "ALTER TABLE trade_ai_logs ADD COLUMN other_income TEXT")
        _safe_alter(conn, "ALTER TABLE trade_ai_logs ADD COLUMN pat_yoy TEXT")
        _safe_alter(conn, "ALTER TABLE trade_ai_logs ADD COLUMN growth_projection TEXT")
        _safe_alter(conn, "ALTER TABLE trade_ai_logs ADD COLUMN broker_estimates TEXT")
        _safe_alter(conn, "ALTER TABLE trade_ai_logs ADD COLUMN ai_suggestion VARCHAR(50)")
        _safe_alter(conn, "ALTER TABLE trade_ai_logs ADD COLUMN attachment_url TEXT")
        _safe_alter(conn, "ALTER TABLE trade_ai_logs ADD COLUMN flow_used VARCHAR(50)")

        # Symbol registry + structured earnings grid
        _safe_alter(conn, "ALTER TABLE market_events ADD COLUMN company_name VARCHAR(250)")
        _safe_alter(conn, "ALTER TABLE pending_result_orders ADD COLUMN company_name VARCHAR(250)")
        _safe_alter(conn, "ALTER TABLE pending_result_orders ADD COLUMN trade_date VARCHAR(20)")
        _safe_alter(conn, "ALTER TABLE trade_ai_logs ADD COLUMN company_name VARCHAR(250)")
        _safe_alter(conn, "ALTER TABLE trade_ai_logs ADD COLUMN metrics_json TEXT")
        _safe_alter(conn, "ALTER TABLE trade_ai_logs ADD COLUMN future_growth_outlook TEXT")
        _safe_alter(conn, "ALTER TABLE trade_ai_logs ADD COLUMN future_projected_numbers TEXT")
        _safe_alter(conn, "ALTER TABLE trade_ai_logs ADD COLUMN extraction_ok BOOLEAN DEFAULT 0")

        # Lifecycle timestamps + alert correlation
        _safe_alter(conn, "ALTER TABLE pending_result_orders ADD COLUMN alert_sent_at DATETIME")
        _safe_alter(conn, "ALTER TABLE pending_result_orders ADD COLUMN ai_requested_at DATETIME")
        _safe_alter(conn, "ALTER TABLE pending_result_orders ADD COLUMN ai_completed_at DATETIME")
        _safe_alter(conn, "ALTER TABLE pending_result_orders ADD COLUMN tracking_ref VARCHAR(40)")
        _safe_alter(conn, "ALTER TABLE pending_result_orders ADD COLUMN deferred BOOLEAN DEFAULT 0")
        _safe_alter(conn, "ALTER TABLE pending_result_orders ADD COLUMN digest_sent_at DATETIME")
        _safe_alter(conn, "ALTER TABLE trade_ai_logs ADD COLUMN tracking_ref VARCHAR(40)")
        _safe_alter(conn, "ALTER TABLE trade_ai_logs ADD COLUMN ai_requested_at DATETIME")
        _safe_alter(conn, "ALTER TABLE trade_ai_logs ADD COLUMN ai_completed_at DATETIME")
        _safe_alter(conn, "ALTER TABLE trade_ai_logs ADD COLUMN validation_json TEXT")
        _safe_alter(conn, "ALTER TABLE pending_result_orders ADD COLUMN price_at_announcement FLOAT")
        _safe_alter(conn, "ALTER TABLE pending_result_orders ADD COLUMN screener_json TEXT")
        _safe_alter(conn, "ALTER TABLE trade_configs ADD COLUMN scheduled_for DATETIME")


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

