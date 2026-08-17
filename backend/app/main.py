import json
import logging
import html
import email.utils
import asyncio
import os
import sys
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET

# Windows consoles default to a legacy code page (cp1252) that cannot encode the
# emoji used throughout our log and print statements. That raised
# UnicodeEncodeError from inside the earnings AI worker and aborted the analysis
# entirely. Force UTF-8 on the standard streams so a log line can never take down
# a code path.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Nothing ever called basicConfig, so every logger.info/error in this codebase
# was discarded — only uvicorn's own access log reached the container output.
# That is how a dead BSE endpoint stayed invisible: the handler logged the
# failure on every cycle and none of it was ever written anywhere.
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    stream=sys.stdout,
    force=True,
)
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any
from fastapi import FastAPI, Depends, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel

logger = logging.getLogger("app.main")

from app.config import PROVIDER, DEFAULT_NIFTY_50
from app.database import init_db, get_db, Watchlist, NewsCache, AICache, SessionStore, HistoricalPriceCache
from app.services.feed_factory import get_feed
from app.services.base_feed import BaseMarketFeed
from app.services.gemini import generate_stock_commentary, analyze_stock_with_gemini, chat_with_llm
from app.services.upstox_feed import UpstoxAuthError

app = FastAPI(title="Indian Stock Market Intelligence Platform")

# Register Intelligence API router
from app.routers.intelligence import router as intelligence_router
app.include_router(intelligence_router)

# Register Trading Engine API router
from app.routers.trading import router as trading_router
app.include_router(trading_router)

# Register Telegram callback handler (inline order / sell buttons)
from app.routers.telegram_webhook import router as telegram_router
app.include_router(telegram_router)

@app.exception_handler(UpstoxAuthError)
def upstox_auth_error_handler(request, exc: UpstoxAuthError):
    """FastAPI exception handler to automatically clear invalid/expired Upstox sessions."""
    db = next(get_db())
    try:
        db.query(SessionStore).filter(SessionStore.provider == PROVIDER).delete()
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()
    
    # Reset in-memory active feed token
    feed = get_feed()
    feed.set_access_token(None)
    
    return JSONResponse(
        status_code=401,
        content={
            "detail": "Upstox access token is invalid or has expired. Please authorize again.",
            "authenticated": False,
            "provider": PROVIDER
        }
    )

# Setup CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Startup: Initialize SQLite database schema and launch background intelligence tasks
@app.on_event("startup")
async def on_startup():
    init_db()
    # Seed Watchlist if empty to provide a good initial user experience
    db = next(get_db())
    try:
        if db.query(Watchlist).count() == 0:
            for stock in DEFAULT_NIFTY_50[:4]: # Seed Reliance, TCS, HDFC, Airtel
                item = Watchlist(
                    symbol=stock["symbol"],
                    name=stock["name"],
                    instrument_key=stock["key"]
                )
                db.add(item)
            db.commit()
    finally:
        db.close()
    
    # Launch background intelligence tasks
    asyncio.create_task(_intelligence_scheduler())
    
    # Initialize SSE manager with the running event loop for cross-thread broadcasts
    from app.services.sse_manager import sse_manager
    sse_manager.set_event_loop(asyncio.get_running_loop())

    # Launch Trading Engine background tasks
    try:
        from app.services.trade_nse_poller import start_trade_poller
        from app.services.stoploss_monitor import start_stoploss_monitor
        start_trade_poller()
        start_stoploss_monitor()
        logger.info("⚡ Trading Engine: NSE poller + stoploss monitor started")
    except Exception as e:
        logger.error(f"Failed to start Trading Engine background tasks: {e}")


async def _intelligence_scheduler():
    """
    Background scheduler for all intelligence data collection and AI analysis.

    Every task runs under a single-flight guard (see _single_flight). A cycle that
    is still executing when its next tick comes around is skipped rather than
    queued, which is what keeps a slow upstream (NSE timing out at 15s) from
    building an unbounded backlog of worker threads.
    """
    from app.services.intel_config import get_intel_config

    logger.info("Intelligence scheduler started")

    # Monotonic timestamp of the last *dispatch* for each task
    last_run = {
        "corporate_announcements": 0.0,
        "other_nse_bse": 0.0,
        "news": 0.0,
        "social": 0.0,
        "filings": 0.0,
        "ai_analysis": 0.0,
        "scheduled_buys": 0.0,
    }

    while True:
        try:
            config = get_intel_config()
            polling = config.polling
            now = asyncio.get_event_loop().time()

            # Check if we're in market hours (affects polling frequency)
            market_hours = config.market_hours
            from datetime import time as dt_time
            try:
                import zoneinfo
                tz = zoneinfo.ZoneInfo(market_hours.get("timezone", "Asia/Kolkata"))
            except Exception:
                tz = None

            current_time = datetime.now(tz) if tz else datetime.now()
            market_open = dt_time(*map(int, market_hours.get("open", "09:00").split(":")))
            market_close = dt_time(*map(int, market_hours.get("close", "16:30").split(":")))
            is_weekday = current_time.weekday() < 5
            is_market_hours = is_weekday and market_open <= current_time.time() <= market_close

            multiplier = 1 if is_market_hours else polling.get("off_market_multiplier", 6)

            def due(task: str, interval_key: str, default: int, scaled: bool = True) -> bool:
                interval = polling.get(interval_key, default) * (multiplier if scaled else 1)
                if now - last_run[task] >= interval:
                    last_run[task] = now
                    return True
                return False

            # Unified NSE+BSE corporate announcements (the real-time path)
            if due("corporate_announcements", "nse_bse_announcements", 2):
                asyncio.create_task(asyncio.to_thread(_run_corporate_announcements_scraper))

            # Slow-moving NSE/BSE datasets: bulk deals, board meetings, insider, results index
            if due("other_nse_bse", "nse_bse_other", 300):
                asyncio.create_task(asyncio.to_thread(_run_other_nse_bse_scraper))

            if due("news", "news_aggregator", 300):
                asyncio.create_task(asyncio.to_thread(_run_news_aggregator))

            if due("social", "social_media", 900):
                asyncio.create_task(asyncio.to_thread(_run_social_monitor))

            if due("filings", "company_filings", 1800):
                asyncio.create_task(asyncio.to_thread(_run_filings_scraper))

            # Scheduled buys: orders placed while the market was shut, waiting
            # for the open. Checked every 30s so one fires within half a minute
            # of 09:15 rather than whenever the next slow task happens to run.
            if due("scheduled_buys", "scheduled_buys", 30, scaled=False):
                asyncio.create_task(asyncio.to_thread(_run_scheduled_buys))

            # AI queue drains on its own cadence, independent of market hours
            if due("ai_analysis", "ai_analysis_queue", 120, scaled=False):
                asyncio.create_task(asyncio.to_thread(_run_ai_analysis))

            # Daily IST jobs, each guarded by the date it last ran on so a
            # restart during the window cannot fire them twice.
            try:
                from datetime import datetime as dt_cls, timezone as tz_cls, timedelta as td_cls
                ist = tz_cls(td_cls(hours=5, minutes=30))
                now_ist = dt_cls.now(ist)
                today_str = now_ist.strftime("%Y-%m-%d")

                # 01:00 — drop result prompts older than 72h. Overnight, so a
                # deletion never races the panel someone is deciding on.
                if now_ist.hour >= 1 and last_run.get("pending_purge_date") != today_str:
                    last_run["pending_purge_date"] = today_str
                    await asyncio.to_thread(_run_pending_purge)

                # 02:00 — retire target configs whose date has passed
                if now_ist.hour >= 2 and last_run.get("config_expiry_date") != today_str:
                    last_run["config_expiry_date"] = today_str
                    await asyncio.to_thread(_run_config_expiry)

                # 06:00 — sync upcoming earnings into the watchlist
                if now_ist.hour >= 6 and last_run.get("earnings_sync_date") != today_str:
                    last_run["earnings_sync_date"] = today_str
                    await asyncio.to_thread(_run_earnings_sync)

                # 08:00 — publish the digest page and send the single alert
                if now_ist.hour >= 8 and last_run.get("morning_digest_date") != today_str:
                    last_run["morning_digest_date"] = today_str
                    asyncio.create_task(asyncio.to_thread(_run_morning_digest))
            except Exception as e:
                logger.error(f"Daily IST job error: {e}")

        except Exception as e:
            logger.error(f"Intelligence scheduler error: {e}")

        # Tick at the fastest configured cadence, but never faster than 1s.
        try:
            tick = max(1.0, float(get_intel_config().polling.get("nse_bse_announcements", 2)))
        except Exception:
            tick = 2.0
        await asyncio.sleep(tick)


import threading
import functools

_task_locks: Dict[str, threading.Lock] = {}
_task_locks_guard = threading.Lock()


def _single_flight(name: str):
    """
    Ensure at most one execution of a scraper is in flight at any time.

    The scheduler ticks on a fixed cadence, but an upstream call can take far
    longer than one tick (NSE routinely takes 8-15s and can hang to its timeout).
    Without this guard each tick queues another worker, threads pile up without
    bound, and the box saturates. A tick that arrives while the previous run is
    still going is dropped — the next tick will pick up the fresh data anyway.
    """
    def decorator(fn):
        with _task_locks_guard:
            _task_locks.setdefault(name, threading.Lock())

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            lock = _task_locks[name]
            if not lock.acquire(blocking=False):
                logger.debug(f"[SKIP] '{name}' still running from a previous cycle")
                return None
            try:
                return fn(*args, **kwargs)
            finally:
                lock.release()
        return wrapper
    return decorator


@_single_flight("scheduled_buys")
def _run_scheduled_buys():
    """Place buys that were queued while the market was closed."""
    try:
        from app.services.order_scheduler import run_due_scheduled_buys
        db = next(get_db())
        try:
            run_due_scheduled_buys(db)
        finally:
            db.close()
    except Exception as e:
        logger.error(f"Scheduled buy sweep error: {e}")


@_single_flight("pending_purge")
def _run_pending_purge():
    """01:00 IST — bound how long result prompts are kept."""
    try:
        from app.services.results_router import purge_old_pending
        db = next(get_db())
        try:
            purge_old_pending(db, hours=72)
        finally:
            db.close()
    except Exception as e:
        logger.error(f"Pending purge error: {e}")


@_single_flight("config_expiry")
def _run_config_expiry():
    """02:00 IST — retire target configs whose target date has passed."""
    try:
        from app.services.results_router import expire_stale_configs
        db = next(get_db())
        try:
            expire_stale_configs(db)
        finally:
            db.close()
    except Exception as e:
        logger.error(f"Config expiry error: {e}")


@_single_flight("earnings_sync")
def _run_earnings_sync():
    """Thread-safe wrapper for daily 6 AM IST earnings watchlist sync."""
    try:
        from app.services.earnings_sync import sync_earnings_to_watchlist
        db = next(get_db())
        try:
            sync_earnings_to_watchlist(db)
        finally:
            db.close()
    except Exception as e:
        logger.error(f"Daily earnings sync error: {e}")


@_single_flight("morning_digest")
def _run_morning_digest():
    """08:00 IST — publish the comparison page and send one summary alert."""
    try:
        from app.services.morning_digest import run_morning_digest
        db = next(get_db())
        try:
            run_morning_digest(db, public_base_url=os.getenv("PUBLIC_BASE_URL", ""))
        finally:
            db.close()
    except Exception as e:
        logger.error(f"Morning digest error: {e}")


@_single_flight("corporate_announcements")
def _run_corporate_announcements_scraper():
    """
    Thread-safe wrapper for the unified NSE+BSE corporate announcements hub.

    This is the single owner of the corporate-announcements endpoints; the
    trading engine consumes the same fetch rather than issuing its own.
    """
    try:
        from app.services.exchange_hub import poll_exchange_announcements
        db = next(get_db())
        try:
            poll_exchange_announcements(db)
        finally:
            db.close()
    except Exception as e:
        logger.error(f"Corporate announcements hub error: {e}")


@_single_flight("other_nse_bse")
def _run_other_nse_bse_scraper():
    """
    Thread-safe wrapper for the slow-moving NSE/BSE datasets.

    BSE announcements are deliberately absent here — they are fetched once by the
    unified hub above. Fetching them again in this cycle was duplicating every
    BSE call and doubling the load on the proxy.
    """
    try:
        from app.services.nse_bse_scraper import (
            fetch_bulk_block_deals,
            fetch_board_meetings,
            fetch_insider_trading,
            fetch_financial_results,
        )
        db = next(get_db())
        try:
            fetch_bulk_block_deals(db)
            fetch_board_meetings(db)
            fetch_insider_trading(db)
            fetch_financial_results(db)
        finally:
            db.close()
    except Exception as e:
        logger.error(f"Other NSE/BSE scraper error: {e}")


@_single_flight("news_aggregator")
def _run_news_aggregator():
    """Thread-safe wrapper for news aggregation."""
    try:
        from app.services.news_aggregator import fetch_all_news
        db = next(get_db())
        try:
            fetch_all_news(db)
        finally:
            db.close()
    except Exception as e:
        logger.error(f"News aggregator error: {e}")


@_single_flight("social_monitor")
def _run_social_monitor():
    """Thread-safe wrapper for social media monitoring."""
    try:
        from app.services.social_monitor import fetch_social_media
        db = next(get_db())
        try:
            fetch_social_media(db)
        finally:
            db.close()
    except Exception as e:
        logger.error(f"Social monitor error: {e}")


@_single_flight("filings_scraper")
def _run_filings_scraper():
    """Thread-safe wrapper for filings scraping."""
    try:
        from app.services.filings_scraper import fetch_all_filings
        db = next(get_db())
        try:
            fetch_all_filings(db)
        finally:
            db.close()
    except Exception as e:
        logger.error(f"Filings scraper error: {e}")


@_single_flight("ai_analysis")
def _run_ai_analysis():
    """Thread-safe wrapper for AI analysis."""
    try:
        from app.services.ai_analyzer import run_analysis_cycle
        db = next(get_db())
        try:
            run_analysis_cycle(db)
        finally:
            db.close()
    except Exception as e:
        logger.error(f"AI analysis error: {e}")

# Dependency to resolve the active feed and inject the current access token
def get_active_feed(db: Any = Depends(get_db)) -> BaseMarketFeed:
    feed = get_feed()
    if db is None or not hasattr(db, "query"):
        try:
            db = next(get_db())
        except Exception:
            db = None
    if db and hasattr(db, "query"):
        try:
            stored_session = db.query(SessionStore).filter(SessionStore.provider == PROVIDER).first()
            if stored_session:
                feed.set_access_token(stored_session.access_token)
            else:
                feed.set_access_token(None)
        except Exception as e:
            logger.warning(f"Session lookup in get_active_feed: {e}")
    return feed

import gzip
import io
import requests
from concurrent.futures import ThreadPoolExecutor

# ----------------- AUTH ENDPOINTS -----------------

@app.get("/api/auth/login")
def get_login_url(feed: BaseMarketFeed = Depends(get_active_feed)):
    """Retrieve the URL to redirect the user to for OAuth authentication."""
    return {"url": feed.get_auth_url(), "provider": PROVIDER}

@app.get("/api/auth/callback")
def auth_callback(code: str, db: Session = Depends(get_db), feed: BaseMarketFeed = Depends(get_active_feed)):
    """OAuth callback redirected to by the broker. Exchange code and store access token."""
    try:
        access_token = feed.authenticate(code)
        
        # Save token to db
        stored = db.query(SessionStore).filter(SessionStore.provider == PROVIDER).first()
        if stored:
            stored.access_token = access_token
            stored.updated_at = datetime.utcnow()
        else:
            stored = SessionStore(provider=PROVIDER, access_token=access_token)
            db.add(stored)
        db.commit()
        
        # Redirect back to the frontend dashboard
        frontend_url = os.getenv("FRONTEND_URL", "/").rstrip("/")
        return RedirectResponse(url=f"{frontend_url}/?auth=success")
    except Exception as e:
        db.rollback()
        # In case of error, redirect to frontend with error flag
        return RedirectResponse(url=f"http://localhost:5173?auth=error&message={str(e)}")

@app.get("/api/auth/status")
def get_auth_status(db: Session = Depends(get_db)):
    """Check if the backend has an active token for the configured provider."""
    stored = db.query(SessionStore).filter(SessionStore.provider == PROVIDER).first()
    return {
        "authenticated": stored is not None,
        "provider": PROVIDER,
        "updated_at": stored.updated_at.isoformat() if stored else None
    }

@app.post("/api/auth/logout")
def logout(db: Session = Depends(get_db), feed: BaseMarketFeed = Depends(get_active_feed)):
    """Revoke/clear local authentication session."""
    try:
        db.query(SessionStore).filter(SessionStore.provider == PROVIDER).delete()
        db.commit()
        feed.set_access_token(None)
        return {"status": "success"}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to clear session: {str(e)}")

# ----------------- WATCHLIST ENDPOINTS -----------------

@app.get("/api/market/quotes-by-symbols")
def get_quotes_by_symbols(
    symbols: str = Query(...),
    db: Session = Depends(get_db),
    feed: BaseMarketFeed = Depends(get_active_feed)
):
    """
    Real-time quotes for a comma-separated list of symbols, without requiring
    them to be in the watchlist DB.

    Symbols that resolve to no listing on either exchange are dropped rather
    than guessed at: Upstox rejects the whole batch when one key is malformed,
    so a single unknown scrip would blank every price on the screen.
    """
    sym_list = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    if not sym_list:
        return {}

    keys = []
    key_to_sym = {}
    key_rank = {}
    for s in sym_list:
        for rank, k in enumerate(resolve_instrument_keys(s)):
            keys.append(k)
            for variant in (k, k.replace("|", ":")):
                key_to_sym[variant] = s
                key_rank[variant] = rank
        key_to_sym[s] = s

    if not keys:
        return {}

    try:
        quotes = feed.get_quotes(keys)
        result = {}
        for k, q in quotes.items():
            sym = key_to_sym.get(k) or key_to_sym.get(k.replace(":", "|"))
            if not sym and ":" in k:
                sym = k.split(":")[-1]
            if not sym and "|" in k:
                sym = k.split("|")[-1]
            if not sym:
                continue

            last_price = q.get("last_price", 0.0)
            ohlc = q.get("ohlc", {})
            prev_close = q.get("close") or ohlc.get("close", 0.0)
            day_high = ohlc.get("high", 0.0) or q.get("high", 0.0)
            day_low = ohlc.get("low", 0.0) or q.get("low", 0.0)
            pct_change = 0.0
            if prev_close > 0 and last_price > 0:
                pct_change = ((last_price - prev_close) / prev_close) * 100

            # A dually-listed scrip comes back once per exchange. Prefer the
            # book that actually traded, and NSE on a tie — it is the deeper one.
            rank = key_rank.get(k, key_rank.get(k.replace(":", "|"), 99))
            existing = result.get(sym.upper())
            if existing:
                traded, was_traded = last_price > 0, existing["last_price"] > 0
                if was_traded and not traded:
                    continue
                if was_traded == traded and rank >= existing["_rank"]:
                    continue

            result[sym.upper()] = {
                "_rank": rank,
                "symbol": sym.upper(),
                "instrument_key": k,
                "last_price": last_price,
                "change": round(pct_change, 2),
                "close": prev_close,
                "high": day_high,
                "low": day_low,
                "depth_buy_pct": q.get("depth_buy_pct", 50.0),
                "depth_sell_pct": q.get("depth_sell_pct", 50.0),
                "total_buy_qty": q.get("total_buy_qty", 0),
                "total_sell_qty": q.get("total_sell_qty", 0)
            }
        for row in result.values():
            row.pop("_rank", None)
        return result
    except Exception as e:
        print(f"Error in quotes-by-symbols: {e}")
        return {}


@app.get("/api/market/resolve-symbol")
def resolve_symbol(symbol: str = Query(...)):
    """
    Instrument keys for a symbol, NSE first.

    Exists so the UI never has to guess one. `NSE_EQ|<SYMBOL>` looks like a key
    and is accepted nowhere — it is why BSE-only scrips showed an empty chart.
    Reads the cached index, so it costs no metered request.
    """
    keys = resolve_instrument_keys(symbol)
    return {"symbol": symbol.upper(), "keys": keys, "key": keys[0] if keys else None}


@app.get("/api/watchlist")
def get_watchlist(
    period: str = "today",
    db: Session = Depends(get_db), 
    feed: BaseMarketFeed = Depends(get_active_feed)
):
    """Get all watchlist stocks with prices and percentage change for the selected period."""
    items = db.query(Watchlist).all()
    if not items:
        return []
    try:
        keys = [item.instrument_key for item in items]
        quotes = feed.get_quotes(keys)
        
        # Resolve historical prices if period is not "today"
        historical_prices = {}
        if period != "today":
            today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            cached_entries = db.query(HistoricalPriceCache).filter(
                HistoricalPriceCache.period == period,
                HistoricalPriceCache.fetched_at >= today_start,
                HistoricalPriceCache.instrument_key.in_(keys)
            ).all()
            
            for entry in cached_entries:
                historical_prices[entry.instrument_key] = entry.price
                
            missing_keys = [k for k in keys if k not in historical_prices]
            if missing_keys:
                interval, from_date, to_date = get_period_params(period)
                for key in missing_keys:
                    try:
                        candles = feed.get_historical_candles(key, interval, to_date, from_date)
                        if candles and len(candles) > 0:
                            hist_close = candles[0][4]
                            
                            # Cache in DB
                            cached = db.query(HistoricalPriceCache).filter(
                                HistoricalPriceCache.instrument_key == key,
                                HistoricalPriceCache.period == period
                            ).first()
                            if cached:
                                cached.price = hist_close
                                cached.fetched_at = datetime.utcnow()
                            else:
                                cached = HistoricalPriceCache(
                                    instrument_key=key,
                                    period=period,
                                    price=hist_close
                                )
                                db.add(cached)
                            historical_prices[key] = hist_close
                    except Exception:
                        pass
                db.commit()
                
        keys = [item.instrument_key for item in items]
        analysis_map = {}
        try:
            cached_analyses = db.query(AICache).filter(AICache.instrument_key.in_(keys)).all()
            for ca in cached_analyses:
                analysis_map[ca.instrument_key] = {
                    "comment": ca.comment,
                    "resistance_levels": ca.resistance_levels,
                    "support_levels": ca.support_levels,
                    "recommendation": ca.recommendation,
                    "sector": ca.sector,
                    "analyst_recommendations": json.loads(ca.analyst_recommendations) if ca.analyst_recommendations else [],
                    "fetched_at": ca.fetched_at.isoformat() if ca.fetched_at else None
                }
        except Exception:
            pass

        result = []
        for item in items:
            q = quotes.get(item.instrument_key, {})
            last_price = q.get("last_price", 0.0)
            ohlc = q.get("ohlc", {})
            prev_close = ohlc.get("close", 0.0)
            
            if period == "today":
                pct_change = 0.0
                if prev_close > 0:
                    pct_change = ((last_price - prev_close) / prev_close) * 100
            else:
                hist_close = historical_prices.get(item.instrument_key, prev_close)
                pct_change = 0.0
                if hist_close > 0:
                    pct_change = ((last_price - hist_close) / hist_close) * 100
                    
            result.append({
                "id": item.id,
                "symbol": item.symbol,
                "name": item.name,
                "instrument_key": item.instrument_key,
                "last_price": last_price,
                "change": round(pct_change, 2),
                "high": ohlc.get("high", 0.0),
                "low": ohlc.get("low", 0.0),
                "close": prev_close,
                "is_holding": item.is_holding,
                "analysis": analysis_map.get(item.instrument_key),
                "depth_buy_pct": q.get("depth_buy_pct", 50.0),
                "depth_sell_pct": q.get("depth_sell_pct", 50.0),
                "total_buy_qty": q.get("total_buy_qty", 0),
                "total_sell_qty": q.get("total_sell_qty", 0)
            })
        return result
    except Exception as e:
        if isinstance(e, UpstoxAuthError):
            raise
        # Fallback to database models if feed quotes fails (e.g., unauthorized)
        return [{
            "id": item.id,
            "symbol": item.symbol,
            "name": item.name,
            "instrument_key": item.instrument_key,
            "last_price": 0.0,
            "change": 0.0
        } for item in items]

@app.post("/api/watchlist")
def add_to_watchlist(symbol: str, name: str, instrument_key: str, db: Session = Depends(get_db)):
    """Add a stock to the user's watchlist."""
    # Check if already exists
    existing = db.query(Watchlist).filter(Watchlist.instrument_key == instrument_key).first()
    if existing:
        return existing
        
    item = Watchlist(symbol=symbol, name=name, instrument_key=instrument_key)
    try:
        db.add(item)
        db.commit()
        db.refresh(item)
        return item
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"Failed to add to watchlist: {str(e)}")

@app.delete("/api/watchlist/{watchlist_id}")
def delete_from_watchlist(watchlist_id: int, db: Session = Depends(get_db)):
    """Remove a stock from the watchlist."""
    item = db.query(Watchlist).filter(Watchlist.id == watchlist_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Watchlist item not found")
    try:
        db.delete(item)
        db.commit()
        return {"status": "success"}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/watchlist/{watchlist_id}/toggle-holding")
def toggle_holding(watchlist_id: int, db: Session = Depends(get_db)):
    """Toggle the holdings status of a stock in the watchlist."""
    item = db.query(Watchlist).filter(Watchlist.id == watchlist_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Watchlist item not found")
    try:
        item.is_holding = not item.is_holding
        db.commit()
        db.refresh(item)
        return {"status": "success", "is_holding": item.is_holding}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/market/indices")
def get_indices(db: Session = Depends(get_db), feed: BaseMarketFeed = Depends(get_active_feed)):
    """Fetch live quotes for Nifty 50, Sensex, and major sectoral indices."""
    keys = [
        "NSE_INDEX|Nifty 50", 
        "BSE_INDEX|SENSEX",
        "NSE_INDEX|Nifty Bank",
        "NSE_INDEX|Nifty IT",
        "NSE_INDEX|Nifty Pharma",
        "NSE_INDEX|Nifty Auto",
        "NSE_INDEX|Nifty FMCG",
        "NSE_INDEX|Nifty Metal",
        "NSE_INDEX|Nifty Fin Service",
        "NSE_INDEX|Nifty Realty"
    ]
    try:
        quotes = feed.get_quotes(keys)
        nifty = quotes.get("NSE_INDEX|Nifty 50") or quotes.get("NSE_INDEX:Nifty 50") or {}
        sensex = quotes.get("BSE_INDEX|SENSEX") or quotes.get("BSE_INDEX:SENSEX") or {}
        
        bank = quotes.get("NSE_INDEX|Nifty Bank") or quotes.get("NSE_INDEX:Nifty Bank") or {}
        it = quotes.get("NSE_INDEX|Nifty IT") or quotes.get("NSE_INDEX:Nifty IT") or {}
        pharma = quotes.get("NSE_INDEX|Nifty Pharma") or quotes.get("NSE_INDEX:Nifty Pharma") or {}
        auto = quotes.get("NSE_INDEX|Nifty Auto") or quotes.get("NSE_INDEX:Nifty Auto") or {}
        fmcg = quotes.get("NSE_INDEX|Nifty FMCG") or quotes.get("NSE_INDEX:Nifty FMCG") or {}
        metal = quotes.get("NSE_INDEX|Nifty Metal") or quotes.get("NSE_INDEX:Nifty Metal") or {}
        fin = quotes.get("NSE_INDEX|Nifty Fin Service") or quotes.get("NSE_INDEX:Nifty Fin Service") or {}
        realty = quotes.get("NSE_INDEX|Nifty Realty") or quotes.get("NSE_INDEX:Nifty Realty") or {}
        
        def format_index(quote_data, default_price, name, symbol):
            last_price = quote_data.get("last_price", 0.0)
            ohlc = quote_data.get("ohlc", {})
            prev_close = ohlc.get("close", 0.0)
            if last_price == 0.0:
                last_price = default_price
                prev_close = default_price * 0.995
            
            change = last_price - prev_close
            pct_change = (change / prev_close) * 100 if prev_close > 0 else 0.0
            
            return {
                "name": name,
                "symbol": symbol,
                "last_price": round(last_price, 2),
                "change": round(change, 2),
                "pct_change": round(pct_change, 2)
            }
            
        return {
            "nifty": format_index(nifty, 23550.0, "Nifty 50", "NIFTY 50"),
            "sensex": format_index(sensex, 77200.0, "BSE SENSEX", "SENSEX"),
            "sectors": [
                format_index(bank, 51200.0, "Nifty Bank", "NIFTY BANK"),
                format_index(it, 38500.0, "Nifty IT", "NIFTY IT"),
                format_index(pharma, 19500.0, "Nifty Pharma", "NIFTY PHARMA"),
                format_index(auto, 25400.0, "Nifty Auto", "NIFTY AUTO"),
                format_index(fmcg, 57200.0, "Nifty FMCG", "NIFTY FMCG"),
                format_index(metal, 9800.0, "Nifty Metal", "NIFTY METAL"),
                format_index(fin, 23800.0, "Nifty Financial Services", "NIFTY FIN SERVICE"),
                format_index(realty, 1050.0, "Nifty Realty", "NIFTY REALTY"),
            ]
        }
    except Exception as e:
        logger.error(f"Error fetching indices: {e}")
        return {
            "nifty": {"name": "Nifty 50", "symbol": "NIFTY 50", "last_price": 23550.0, "change": 117.5, "pct_change": 0.5},
            "sensex": {"name": "BSE SENSEX", "symbol": "SENSEX", "last_price": 77200.0, "change": 385.0, "pct_change": 0.5},
            "sectors": [
                {"name": "Nifty Bank", "symbol": "NIFTY BANK", "last_price": 51200.0, "change": 256.0, "pct_change": 0.5},
                {"name": "Nifty IT", "symbol": "NIFTY IT", "last_price": 38500.0, "change": 192.5, "pct_change": 0.5},
                {"name": "Nifty Pharma", "symbol": "NIFTY PHARMA", "last_price": 19500.0, "change": 97.5, "pct_change": 0.5},
                {"name": "Nifty Auto", "symbol": "NIFTY AUTO", "last_price": 25400.0, "change": 127.0, "pct_change": 0.5},
                {"name": "Nifty FMCG", "symbol": "NIFTY FMCG", "last_price": 57200.0, "change": 286.0, "pct_change": 0.5},
                {"name": "Nifty Metal", "symbol": "NIFTY METAL", "last_price": 9800.0, "change": 49.0, "pct_change": 0.5},
                {"name": "Nifty Financial Services", "symbol": "NIFTY FIN SERVICE", "last_price": 23800.0, "change": 119.0, "pct_change": 0.5},
                {"name": "Nifty Realty", "symbol": "NIFTY REALTY", "last_price": 1050.0, "change": 5.25, "pct_change": 0.5},
            ]
        }

# ----------------- MARKET ENDPOINTS -----------------

@app.get("/api/market/search")
def search_stocks(query: str = Query(..., min_length=1), feed: BaseMarketFeed = Depends(get_active_feed)):
    """Autocomplete search for NSE stocks based on symbol or company name."""
    try:
        return feed.search(query)
    except Exception as e:
        if isinstance(e, UpstoxAuthError):
            raise
        query_upper = query.upper()
        matches = []
        for stock in DEFAULT_NIFTY_50:
            if query_upper in stock["symbol"].upper() or query_upper in stock["name"].upper():
                matches.append({
                    "symbol": stock["symbol"],
                    "name": stock["name"],
                    "key": stock["key"]
                })
        return matches[:10]


def get_period_params(period: str):
    to_date_str = datetime.now().strftime("%Y-%m-%d")
    if period == "week":
        from_date = datetime.now() - timedelta(days=10)
        return "day", from_date.strftime("%Y-%m-%d"), to_date_str
    elif period == "month":
        from_date = datetime.now() - timedelta(days=45)
        return "day", from_date.strftime("%Y-%m-%d"), to_date_str
    elif period == "3months":
        from_date = datetime.now() - timedelta(days=110)
        return "day", from_date.strftime("%Y-%m-%d"), to_date_str
    elif period == "6months":
        from_date = datetime.now() - timedelta(days=210)
        return "day", from_date.strftime("%Y-%m-%d"), to_date_str
    elif period == "1year":
        from_date = datetime.now() - timedelta(days=400)
        return "day", from_date.strftime("%Y-%m-%d"), to_date_str
    elif period == "5years":
        from_date = datetime.now() - timedelta(days=1900)
        return "month", from_date.strftime("%Y-%m-%d"), to_date_str
    return "day", None, to_date_str

_nse_equities_cache = None

def get_nse_equities():
    global _nse_equities_cache
    if _nse_equities_cache is not None:
        return _nse_equities_cache
        
    url = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
    try:
        res = requests.get(url, timeout=10)
        if res.status_code == 200:
            compressed_file = io.BytesIO(res.content)
            with gzip.GzipFile(fileobj=compressed_file) as f:
                data = json.load(f)
            eq_keys = []
            for item in data:
                key = item.get("instrument_key", "")
                if key and key.startswith("NSE_EQ|") and item.get("instrument_type") == "EQ":
                    eq_keys.append({
                        "key": key,
                        "symbol": item.get("trading_symbol", ""),
                        "name": item.get("name", "")
                    })
            if eq_keys:
                _nse_equities_cache = eq_keys
                return _nse_equities_cache
    except Exception as e:
        print(f"Error fetching NSE instruments: {e}")
        
    # Fallback to DEFAULT_NIFTY_50
    return [{"key": x["key"], "symbol": x["symbol"], "name": x["name"]} for x in DEFAULT_NIFTY_50]


_bse_equities_cache = None

# BSE marks the trading group in `instrument_type` (A/B/T/X/…) rather than "EQ",
# so equities are selected by segment. F and G are debt and government paper.
_BSE_NON_EQUITY_GROUPS = {"F", "G"}


def get_bse_equities():
    """
    BSE equity instruments, keyed the way Upstox expects (`BSE_EQ|<ISIN>`).

    Plenty of small caps file results with BSE only and have no NSE listing at
    all. Without this map their quote request is built from a symbol NSE has
    never heard of, and the panel shows no price.
    """
    global _bse_equities_cache
    if _bse_equities_cache is not None:
        return _bse_equities_cache

    url = "https://assets.upstox.com/market-quote/instruments/exchange/BSE.json.gz"
    try:
        res = requests.get(url, timeout=10)
        if res.status_code == 200:
            compressed_file = io.BytesIO(res.content)
            with gzip.GzipFile(fileobj=compressed_file) as f:
                data = json.load(f)
            eq_keys = []
            for item in data:
                key = item.get("instrument_key", "")
                if not key.startswith("BSE_EQ|"):
                    continue
                if item.get("instrument_type") in _BSE_NON_EQUITY_GROUPS:
                    continue
                eq_keys.append({
                    "key": key,
                    "symbol": item.get("trading_symbol", ""),
                    "name": item.get("name", ""),
                    "isin": item.get("isin", ""),
                    # The scrip code. BSE filings sometimes carry it where a
                    # ticker would go, and the registry has no row for those.
                    "token": str(item.get("exchange_token") or ""),
                })
            if eq_keys:
                _bse_equities_cache = eq_keys
                return _bse_equities_cache
    except Exception as e:
        print(f"Error fetching BSE instruments: {e}")

    return []


_symbol_key_index: Optional[Dict[str, List[str]]] = None


def _get_symbol_key_index() -> Dict[str, List[str]]:
    """Symbol → instrument keys, NSE first. Built once; the panels resolve
    hundreds of symbols per poll and a linear scan of both dumps does not."""
    global _symbol_key_index
    if _symbol_key_index is not None:
        return _symbol_key_index

    index: Dict[str, List[str]] = {}
    for eqs in (get_nse_equities(), get_bse_equities()):
        for eq in eqs:
            key = eq.get("key")
            if not key:
                continue
            for handle in ((eq.get("symbol") or "").upper(), eq.get("token") or ""):
                if handle and key not in index.setdefault(handle, []):
                    index[handle].append(key)
    if index:
        _symbol_key_index = index
    return index


def resolve_instrument_keys(symbol: str) -> List[str]:
    """
    Every Upstox key a symbol could trade under, NSE first.

    NSE leads because it is the more liquid book, but a BSE-only scrip must
    still resolve to something real — a synthesised `NSE_EQ|<SYMBOL>` is not a
    valid key and costs the whole batch, not just that one row.
    """
    if not symbol:
        return []
    return list(_get_symbol_key_index().get(symbol.upper(), []))


def resolve_stock_info(instrument_key: str, db: Session) -> tuple:
    """Resolve symbol and name for a given instrument_key."""
    # 1. Try DEFAULT_NIFTY_50 first
    for s in DEFAULT_NIFTY_50:
        if s["key"] == instrument_key:
            return s["symbol"], s["name"]
            
    # 2. Try database Watchlist
    try:
        wl_item = db.query(Watchlist).filter(Watchlist.instrument_key == instrument_key).first()
        if wl_item:
            return wl_item.symbol, wl_item.name
    except Exception:
        pass
        
    # 3. Try get_nse_equities cache
    try:
        stocks = get_nse_equities()
        for s in stocks:
            if s["key"] == instrument_key:
                return s["symbol"], s["name"]
    except Exception:
        pass
        
    # Fallback: parse symbol from instrument key (e.g. NSE_EQ|INE269A01021)
    symbol = instrument_key.split("|")[-1] if "|" in instrument_key else instrument_key
    return symbol, "NSE Equities stock"


def fetch_rss_news(query: str) -> List[dict]:
    encoded_query = urllib.parse.quote(query)
    url = f"https://news.google.com/rss/search?q={encoded_query}&hl=en-IN&gl=IN&ceid=IN:en"
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    
    articles = []
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as response:
            xml_data = response.read()
            
        root = ET.fromstring(xml_data)
        for item in root.findall('.//item')[:15]:
            title = item.find('title').text if item.find('title') is not None else ""
            link = item.find('link').text if item.find('link') is not None else ""
            pub_date = item.find('pubDate').text if item.find('pubDate') is not None else ""
            source_el = item.find('source')
            source = source_el.text if source_el is not None else "Google News"
            
            headline = html.unescape(title)
            if " - " in headline:
                parts = headline.rsplit(" - ", 1)
                headline = parts[0]
                source = parts[1]
                
            pub_time = None
            if pub_date:
                try:
                    dt = email.utils.parsedate_to_datetime(pub_date)
                    pub_time = dt.isoformat()
                except Exception:
                    pass
            if not pub_time:
                pub_time = datetime.utcnow().isoformat() + "Z"
                
            articles.append({
                "headline": headline,
                "summary": headline,
                "source": source,
                "url": link,
                "published_at": pub_time
            })
    except Exception as e:
        logger.error(f"Error fetching RSS news for query '{query}': {e}")
    return articles


def gather_stock_news(symbol: str, name: str, feed_news: List[dict] = None, sector: str = None) -> List[dict]:
    """Gather real news from multiple RSS channels in parallel and combine with feed news."""
    clean_name = name.replace("Ltd.", "").replace("Limited", "").strip()
    queries = [
        f'"{symbol}" OR "{clean_name}" stock',
        f'site:moneycontrol.com "{clean_name}" OR "{symbol}"',
        f'"{symbol}" OR "{clean_name}" buy sell target recommendation rating',
    ]
    if sector and sector != "General Market":
        queries.append(f'"{sector}" sector industry news stock')
    
    with ThreadPoolExecutor(max_workers=len(queries)) as executor:
        results = list(executor.map(fetch_rss_news, queries))
        
    combined = []
    seen_urls = set()
    
    # Add news from feed first
    if feed_news:
        for art in feed_news:
            url = art.get("url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                combined.append(art)
                
    # Add news from RSS queries
    for r in results:
        for art in r:
            url = art.get("url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                combined.append(art)
                
    # Sort combined news by date (newest first)
    def get_sort_key(art):
        pub_at = art.get("published_at", "")
        try:
            return datetime.fromisoformat(pub_at.replace("Z", "+00:00"))
        except Exception:
            return datetime.min
            
    combined.sort(key=get_sort_key, reverse=True)
    return combined[:25]


@app.get("/api/market/movers")
def get_market_movers(
    period: str = "today", 
    db: Session = Depends(get_db), 
    feed: BaseMarketFeed = Depends(get_active_feed)
):
    """Retrieve quotes for all active equities, calculate percentage changes, and sort gainers/losers."""
    try:
        stocks = get_nse_equities()
        keys = [stock["key"] for stock in stocks]
        
        # If period is not "today", we restrict the evaluated list to Nifty 50 to avoid rate limits
        if period != "today":
            nifty_keys = {x["key"] for x in DEFAULT_NIFTY_50}
            stocks = [s for s in stocks if s["key"] in nifty_keys]
            keys = [stock["key"] for stock in stocks]
            
        # Fetch quotes in parallel chunks of 500
        quotes = {}
        chunks = [keys[i:i + 500] for i in range(0, len(keys), 500)]
        
        def fetch_chunk(chunk):
            try:
                return feed.get_quotes(chunk)
            except Exception:
                return {}
                
        with ThreadPoolExecutor(max_workers=5) as executor:
            results = list(executor.map(fetch_chunk, chunks))
            
        for r in results:
            quotes.update(r)
            
        # If period is not "today", retrieve/calculate historical base prices from cache/candles
        historical_prices = {}
        if period != "today":
            today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            cached_entries = db.query(HistoricalPriceCache).filter(
                HistoricalPriceCache.period == period,
                HistoricalPriceCache.fetched_at >= today_start
            ).all()
            
            for entry in cached_entries:
                historical_prices[entry.instrument_key] = entry.price
                
            missing_keys = [k for k in keys if k not in historical_prices]
            if missing_keys:
                interval, from_date, to_date = get_period_params(period)
                for key in missing_keys:
                    try:
                        candles = feed.get_historical_candles(key, interval, to_date, from_date)
                        if candles and len(candles) > 0:
                            hist_close = candles[0][4]
                            
                            # Cache in DB
                            cached = db.query(HistoricalPriceCache).filter(
                                HistoricalPriceCache.instrument_key == key,
                                HistoricalPriceCache.period == period
                            ).first()
                            if cached:
                                cached.price = hist_close
                                cached.fetched_at = datetime.utcnow()
                            else:
                                cached = HistoricalPriceCache(
                                    instrument_key=key,
                                    period=period,
                                    price=hist_close
                                )
                                db.add(cached)
                            historical_prices[key] = hist_close
                    except Exception:
                        pass
                db.commit()
                
        analysis_map = {}
        try:
            cached_analyses = db.query(AICache).filter(AICache.instrument_key.in_(keys)).all()
            for ca in cached_analyses:
                analysis_map[ca.instrument_key] = {
                    "comment": ca.comment,
                    "resistance_levels": ca.resistance_levels,
                    "support_levels": ca.support_levels,
                    "recommendation": ca.recommendation,
                    "sector": ca.sector,
                    "analyst_recommendations": json.loads(ca.analyst_recommendations) if ca.analyst_recommendations else [],
                    "fetched_at": ca.fetched_at.isoformat() if ca.fetched_at else None
                }
        except Exception:
            pass

        movers_list = []
        for stock in stocks:
            key = stock["key"]
            quote = quotes.get(key)
            if not quote:
                continue
                
            last_price = quote.get("last_price", 0.0)
            ohlc = quote.get("ohlc", {})
            volume = quote.get("volume", 0)
            prev_close = ohlc.get("close", 0.0)
            
            # Avoid showing inactive/illiquid/zero-volume stocks in Movers
            if last_price == 0.0 or prev_close == 0.0 or volume < 10000:
                continue
                
            if period == "today":
                pct_change = 0.0
                if prev_close > 0:
                    pct_change = ((last_price - prev_close) / prev_close) * 100
            else:
                hist_close = historical_prices.get(key, prev_close)
                pct_change = 0.0
                if hist_close > 0:
                    pct_change = ((last_price - hist_close) / hist_close) * 100
                    
            movers_list.append({
                "symbol": stock["symbol"],
                "name": stock["name"],
                "instrument_key": key,
                "last_price": last_price,
                "change": round(pct_change, 2),
                "volume": volume,
                "open": ohlc.get("open", 0.0),
                "high": ohlc.get("high", 0.0),
                "low": ohlc.get("low", 0.0),
                "close": prev_close,
                "analysis": analysis_map.get(key),
                "depth_buy_pct": quote.get("depth_buy_pct", 50.0),
                "depth_sell_pct": quote.get("depth_sell_pct", 50.0),
                "total_buy_qty": quote.get("total_buy_qty", 0),
                "total_sell_qty": quote.get("total_sell_qty", 0)
            })
            
        # Sort
        gainers = sorted(movers_list, key=lambda x: x["change"], reverse=True)
        losers = sorted(movers_list, key=lambda x: x["change"])
        
        return {
            "gainers": gainers[:50],
            "losers": losers[:50]
        }
    except Exception as e:
        if isinstance(e, UpstoxAuthError):
            raise
        raise HTTPException(status_code=500, detail=f"Failed to fetch market movers: {str(e)}")

@app.get("/api/market/stock/{instrument_key}")
def get_stock_detail(
    instrument_key: str, 
    db: Session = Depends(get_db), 
    feed: BaseMarketFeed = Depends(get_active_feed)
):
    """Retrieve quotes, historical 30-day daily chart candles, and cached news/AI comments."""
    try:
        # 1. Fetch Quote
        quotes = feed.get_quotes([instrument_key])
        quote = quotes.get(instrument_key)
        if not quote:
            raise HTTPException(status_code=404, detail="Stock quote not found")
            
        # Get company name & symbol
        symbol, name = resolve_stock_info(instrument_key, db)

        # 2. Fetch Historical 30-day daily candles for chart
        to_date = datetime.now().strftime("%Y-%m-%d")
        candles = feed.get_historical_candles(instrument_key, "day", to_date)

        # 3. Retrieve Cached News and AI comment. Auto-populate if missing.
        news_entry = db.query(NewsCache).filter(NewsCache.instrument_key == instrument_key).first()
        ai_entry = db.query(AICache).filter(AICache.instrument_key == instrument_key).first()

        if not news_entry or not ai_entry:
            try:
                # Fetch fresh news from provider and combine with RSS
                feed_news = feed.get_news(instrument_key)
                sector = ai_entry.sector if ai_entry else None
                news_items = gather_stock_news(symbol, name, feed_news, sector)
                
                # Save news to cache
                if news_entry:
                    news_entry.news_json = json.dumps(news_items)
                    news_entry.fetched_at = datetime.utcnow()
                else:
                    news_entry = NewsCache(instrument_key=instrument_key, news_json=json.dumps(news_items))
                    db.add(news_entry)
                
                # Generate structured AI Analysis based on fresh news and candles
                analysis = analyze_stock_with_gemini(symbol, name, quote, news_items, candles)
                
                # Save AI analysis to cache
                if ai_entry:
                    ai_entry.comment = analysis["comment"]
                    ai_entry.resistance_levels = analysis["resistance_levels"]
                    ai_entry.support_levels = analysis.get("support_levels", "")
                    ai_entry.recommendation = analysis["recommendation"]
                    ai_entry.sector = analysis["sector"]
                    ai_entry.analyst_recommendations = json.dumps(analysis.get("analyst_recommendations", []))
                    ai_entry.fetched_at = datetime.utcnow()
                else:
                    ai_entry = AICache(
                        instrument_key=instrument_key,
                        comment=analysis["comment"],
                        resistance_levels=analysis["resistance_levels"],
                        support_levels=analysis.get("support_levels", ""),
                        recommendation=analysis["recommendation"],
                        sector=analysis["sector"],
                        analyst_recommendations=json.dumps(analysis.get("analyst_recommendations", []))
                    )
                    db.add(ai_entry)
                
                db.commit()
                
                # Refresh variables
                news_entry = db.query(NewsCache).filter(NewsCache.instrument_key == instrument_key).first()
                ai_entry = db.query(AICache).filter(AICache.instrument_key == instrument_key).first()
            except Exception as auto_err:
                db.rollback()
                logger.error(f"Failed to auto-fetch news and AI for {symbol}: {str(auto_err)}")

        news_items = json.loads(news_entry.news_json) if news_entry else []
        
        return {
            "instrument_key": instrument_key,
            "name": name,
            "quote": quote,
            "candles": candles,
            "news": news_items,
            "news_fetched_at": news_entry.fetched_at.isoformat() if news_entry else None,
            "ai_comment": ai_entry.comment if ai_entry else None,
            "ai_fetched_at": ai_entry.fetched_at.isoformat() if ai_entry and ai_entry.fetched_at else None,
            "analysis": {
                "comment": ai_entry.comment if ai_entry else None,
                "resistance_levels": ai_entry.resistance_levels if ai_entry else None,
                "support_levels": ai_entry.support_levels if ai_entry else None,
                "recommendation": ai_entry.recommendation if ai_entry else None,
                "sector": ai_entry.sector if ai_entry else None,
                "analyst_recommendations": json.loads(ai_entry.analyst_recommendations) if ai_entry and ai_entry.analyst_recommendations else [],
                "fetched_at": ai_entry.fetched_at.isoformat() if ai_entry and ai_entry.fetched_at else None
            }
        }
    except Exception as e:
        if isinstance(e, UpstoxAuthError):
            raise
        raise HTTPException(status_code=500, detail=f"Error loading stock detail: {str(e)}")

@app.post("/api/market/stock/{instrument_key}/fetch-news")
def force_fetch_news_and_ai(
    instrument_key: str, 
    db: Session = Depends(get_db), 
    feed: BaseMarketFeed = Depends(get_active_feed)
):
    """Trigger an on-demand API fetch of latest news from the provider, store in DB, and update Gemini AI comments."""
    try:
        # Retrieve quote for context
        quotes = feed.get_quotes([instrument_key])
        quote = quotes.get(instrument_key)
        if not quote:
            raise HTTPException(status_code=404, detail="Stock quote not found")

        # Get company name & symbol
        symbol, name = resolve_stock_info(instrument_key, db)

        # 1. Fetch fresh news from provider and combine with RSS
        feed_news = feed.get_news(instrument_key)
        ai_entry = db.query(AICache).filter(AICache.instrument_key == instrument_key).first()
        sector = ai_entry.sector if ai_entry else None
        news_items = gather_stock_news(symbol, name, feed_news, sector)
        
        # 2. Save news to cache
        news_entry = db.query(NewsCache).filter(NewsCache.instrument_key == instrument_key).first()
        if news_entry:
            news_entry.news_json = json.dumps(news_items)
            news_entry.fetched_at = datetime.utcnow()
        else:
            news_entry = NewsCache(instrument_key=instrument_key, news_json=json.dumps(news_items))
            db.add(news_entry)
            
        # Get candles for chart analysis context
        to_date = datetime.now().strftime("%Y-%m-%d")
        candles = feed.get_historical_candles(instrument_key, "day", to_date)
            
        # 3. Generate structured AI Analysis based on fresh news and candles
        analysis = analyze_stock_with_gemini(symbol, name, quote, news_items, candles)
        
        # 4. Save AI analysis to cache
        if ai_entry:
            ai_entry.comment = analysis["comment"]
            ai_entry.resistance_levels = analysis["resistance_levels"]
            ai_entry.support_levels = analysis.get("support_levels", "")
            ai_entry.recommendation = analysis["recommendation"]
            ai_entry.sector = analysis["sector"]
            ai_entry.analyst_recommendations = json.dumps(analysis.get("analyst_recommendations", []))
            ai_entry.fetched_at = datetime.utcnow()
        else:
            ai_entry = AICache(
                instrument_key=instrument_key,
                comment=analysis["comment"],
                resistance_levels=analysis["resistance_levels"],
                support_levels=analysis.get("support_levels", ""),
                recommendation=analysis["recommendation"],
                sector=analysis["sector"],
                analyst_recommendations=json.dumps(analysis.get("analyst_recommendations", []))
            )
            db.add(ai_entry)
            
        db.commit()
        
        return {
            "news": news_items,
            "news_fetched_at": news_entry.fetched_at.isoformat(),
            "ai_comment": analysis["comment"],
            "ai_fetched_at": ai_entry.fetched_at.isoformat() if ai_entry.fetched_at else None,
            "analysis": {
                "comment": analysis["comment"],
                "resistance_levels": analysis["resistance_levels"],
                "support_levels": analysis.get("support_levels", ""),
                "recommendation": analysis["recommendation"],
                "sector": analysis["sector"],
                "analyst_recommendations": analysis.get("analyst_recommendations", []),
                "fetched_at": ai_entry.fetched_at.isoformat() if ai_entry.fetched_at else None
            }
        }
    except Exception as e:
        db.rollback()
        if isinstance(e, UpstoxAuthError):
            raise
        raise HTTPException(status_code=500, detail=f"Failed to fetch updates: {str(e)}")

# ----------------- STOCK AI CHAT ASSISTANT ENDPOINT -----------------

class ChatMessageSchema(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    message: str
    history: List[ChatMessageSchema] = []

@app.post("/api/market/stock/{instrument_key}/chat")
def chat_about_stock(
    instrument_key: str,
    req: ChatRequest,
    db: Session = Depends(get_db),
    feed: BaseMarketFeed = Depends(get_active_feed)
):
    """Chat with the active LLM assistant about a specific stock using real-time details and cached news context."""
    try:
        # 1. Fetch Quote
        quotes = feed.get_quotes([instrument_key])
        quote = quotes.get(instrument_key)
        if not quote:
            raise HTTPException(status_code=404, detail="Stock quote not found")
            
        # Get company name & symbol
        symbol, name = resolve_stock_info(instrument_key, db)
        
        # 2. Get Cached News
        news_entry = db.query(NewsCache).filter(NewsCache.instrument_key == instrument_key).first()
        news_items = json.loads(news_entry.news_json) if news_entry else []
        
        # 3. Convert history list
        history_list = [{"role": msg.role, "content": msg.content} for msg in req.history]
        
        # 4. Invoke LLM Chat service
        response_text = chat_with_llm(
            symbol=symbol,
            name=name,
            quote=quote,
            news=news_items,
            user_message=req.message,
            history=history_list
        )
        
        return {"response": response_text}
    except Exception as e:
        if isinstance(e, UpstoxAuthError):
            raise
        logger.error(f"Error in chat endpoint for {instrument_key}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to process chat: {str(e)}")


@app.get("/api/market/stock/{instrument_key}/candles")
def get_stock_candles(
    instrument_key: str,
    period: str = "1M",
    feed = Depends(get_active_feed)
):
    """Retrieve candles for a specific stock and period."""
    from datetime import datetime, timedelta
    to_date = datetime.now().strftime("%Y-%m-%d")
    
    # Map period to interval and from_date
    interval = "day"
    from_date = None
    
    period = period.upper()
    if period == "1D":
        interval = "1minute"
        from_date = to_date
    elif period == "5D":
        interval = "30minute"
        from_date = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")
    elif period == "1M":
        interval = "day"
        from_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    elif period == "3M":
        interval = "day"
        from_date = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
    elif period == "6M":
        interval = "day"
        from_date = (datetime.now() - timedelta(days=180)).strftime("%Y-%m-%d")
    elif period == "1Y":
        interval = "day"
        from_date = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
    elif period == "5Y":
        interval = "week"
        from_date = (datetime.now() - timedelta(days=1825)).strftime("%Y-%m-%d")
    else:
        # Fallback 1M
        interval = "day"
        from_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        
    try:
        # If period is 5D, retrieve historical candles up to yesterday, and today's intraday ticks, then merge.
        if period == "5D":
            yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            hist_from_str = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")
            
            hist_candles = []
            try:
                hist_candles = feed.get_historical_candles(instrument_key, "30minute", yesterday_str, hist_from_str)
            except Exception as e:
                logger.error(f"Error fetching historical 5D chunk for {instrument_key}: {e}")
                
            today_candles = []
            try:
                today_candles = feed.get_historical_candles(instrument_key, "30minute", to_date, to_date)
            except Exception as e:
                logger.error(f"Error fetching intraday 5D chunk for {instrument_key}: {e}")
                
            candles = hist_candles + today_candles
        else:
            candles = feed.get_historical_candles(instrument_key, interval, to_date, from_date)
            
            # Append today's daily candle from live quote for daily intervals (1M, 3M, 6M, 1Y)
            if period in ("1M", "3M", "6M", "1Y") and datetime.now().weekday() < 5:
                has_today = False
                if candles:
                    last_time = candles[-1][0]
                    if to_date in last_time:
                        has_today = True
                
                if not has_today:
                    try:
                        quotes = feed.get_quotes([instrument_key])
                        quote = quotes.get(instrument_key)
                        if quote:
                            last_price = quote.get("last_price", 0.0)
                            ohlc = quote.get("ohlc", {})
                            volume = quote.get("volume", 0)
                            open_p = ohlc.get("open", last_price)
                            high_p = ohlc.get("high", last_price)
                            low_p = ohlc.get("low", last_price)
                            
                            if last_price > 0:
                                today_candle = [
                                    f"{to_date}T09:15:00+05:30",
                                    open_p,
                                    high_p,
                                    low_p,
                                    last_price,
                                    volume,
                                    0
                                ]
                                candles.append(today_candle)
                    except Exception as e:
                        logger.error(f"Error appending today's daily candle: {e}")
                        
        return {"candles": candles}
    except Exception as e:
        if isinstance(e, UpstoxAuthError):
            raise
        logger.error(f"Error fetching candles for {instrument_key} period {period}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Serve compiled React frontend if frontend/dist exists
import os
from fastapi.staticfiles import StaticFiles

static_frontend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "frontend", "dist"))
if os.path.exists(static_frontend_dir):
    logger.info(f"Serving static frontend from {static_frontend_dir}")
    app.mount("/", StaticFiles(directory=static_frontend_dir, html=True), name="frontend")


