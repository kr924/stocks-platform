import json
import logging
import html
import email.utils
import asyncio
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
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


async def _intelligence_scheduler():
    """Background scheduler for all intelligence data collection and AI analysis."""
    from app.services.intel_config import get_intel_config
    
    logger.info("Intelligence scheduler started")
    
    # Track last run times for each task
    last_run = {
        "corporate_announcements": 0,
        "other_nse_bse": 0,
        "news": 0,
        "social": 0,
        "filings": 0,
        "ai_analysis": 0,
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
            is_market_hours = market_open <= current_time.time() <= market_close
            
            multiplier = 1 if is_market_hours else polling.get("off_market_multiplier", 4)
            
            # Corporate Announcements Scraping
            ann_interval = polling.get("nse_bse_announcements", 150) * multiplier
            if now - last_run["corporate_announcements"] >= ann_interval:
                last_run["corporate_announcements"] = now
                await asyncio.to_thread(_run_corporate_announcements_scraper)
            
            # Other NSE/BSE Scraping
            other_nse_interval = polling.get("nse_bse_other", 150) * multiplier
            if now - last_run["other_nse_bse"] >= other_nse_interval:
                last_run["other_nse_bse"] = now
                await asyncio.to_thread(_run_other_nse_bse_scraper)
            
            # News Aggregation
            news_interval = polling.get("news_aggregator", 150) * multiplier
            if now - last_run["news"] >= news_interval:
                last_run["news"] = now
                await asyncio.to_thread(_run_news_aggregator)
            
            # Social Media
            social_interval = polling.get("social_media", 150) * multiplier
            if now - last_run["social"] >= social_interval:
                last_run["social"] = now
                await asyncio.to_thread(_run_social_monitor)
            
            # Company Filings
            filings_interval = polling.get("company_filings", 900) * multiplier
            if now - last_run["filings"] >= filings_interval:
                last_run["filings"] = now
                await asyncio.to_thread(_run_filings_scraper)
            
            # AI Analysis
            ai_interval = polling.get("ai_analysis_queue", 120)
            if now - last_run["ai_analysis"] >= ai_interval:
                last_run["ai_analysis"] = now
                await asyncio.to_thread(_run_ai_analysis)
            
        except Exception as e:
            logger.error(f"Intelligence scheduler error: {e}")
        
        # Sleep for 5 seconds between loop iterations for fast responsiveness
        await asyncio.sleep(5)


def _run_corporate_announcements_scraper():
    """Thread-safe wrapper for corporate announcements scraping."""
    try:
        from app.services.nse_bse_scraper import fetch_corporate_announcements
        db = next(get_db())
        try:
            fetch_corporate_announcements(db)
        finally:
            db.close()
    except Exception as e:
        logger.error(f"Corporate announcements scraper error: {e}")


def _run_other_nse_bse_scraper():
    """Thread-safe wrapper for other NSE/BSE scraping."""
    try:
        from app.services.nse_bse_scraper import (
            fetch_bulk_block_deals,
            fetch_board_meetings,
            fetch_insider_trading,
            fetch_bse_announcements,
            fetch_financial_results,
        )
        db = next(get_db())
        try:
            fetch_bulk_block_deals(db)
            fetch_board_meetings(db)
            fetch_insider_trading(db)
            fetch_bse_announcements(db)
            fetch_financial_results(db)
        finally:
            db.close()
    except Exception as e:
        logger.error(f"Other NSE/BSE scraper error: {e}")


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
def get_active_feed(db: Session = Depends(get_db)) -> BaseMarketFeed:
    feed = get_feed()
    stored_session = db.query(SessionStore).filter(SessionStore.provider == PROVIDER).first()
    if stored_session:
        feed.set_access_token(stored_session.access_token)
    else:
        feed.set_access_token(None)
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


