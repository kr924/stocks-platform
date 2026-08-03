"""
Ultra-Fast NSE Poller for Trading Engine — Dedicated high-frequency corporate announcements monitor.
Runs in its own asyncio background task, polling NSE every 500ms–2s for armed TradeConfigs.
"""
import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, List

import requests

logger = logging.getLogger("app.trade_nse_poller")

IST = timezone(timedelta(hours=5, minutes=30))

# Module-level state
_poller_task: Optional[asyncio.Task] = None
_poller_running = False
_poll_interval = 1.0  # seconds (configurable)
_last_poll_status: Dict = {
    "running": False,
    "armed_count": 0,
    "last_poll_at": None,
    "polls_total": 0,
    "triggers_total": 0,
    "last_error": None,
}


class _TradingNSESession:
    """Separate NSE session for trading poller (isolated from intelligence poller)."""

    def __init__(self):
        self.session = requests.Session()
        self._last_cookie_refresh = 0
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
        })

    def _refresh_cookies(self):
        now = time.time()
        if now - self._last_cookie_refresh < 120:  # Refresh every 2min for speed
            return
        try:
            self.session.get("https://www.nseindia.com", headers={
                "Accept": "text/html,application/xhtml+xml",
                "Referer": "https://www.google.com/"
            }, timeout=8, allow_redirects=True)
            self._last_cookie_refresh = now
            logger.debug("[TRADE POLLER]: NSE cookies refreshed")
        except Exception as e:
            logger.debug(f"[TRADE POLLER]: Cookie refresh failed: {e}")

    def fetch_announcements(self) -> list:
        """Fetch the latest corporate announcements from NSE."""
        self._refresh_cookies()
        url = "https://www.nseindia.com/api/corporate-announcements?index=equities"
        try:
            res = self.session.get(url, headers={
                "Referer": "https://www.nseindia.com/companies-listing/corporate-filings/announcements"
            }, timeout=8)
            if res.status_code in (401, 403):
                self._last_cookie_refresh = 0
                self._refresh_cookies()
                res = self.session.get(url, headers={
                    "Referer": "https://www.nseindia.com/companies-listing/corporate-filings/announcements"
                }, timeout=8)
            if res.status_code != 200:
                return []
            data = res.json()
            if isinstance(data, list):
                return data
            elif isinstance(data, dict):
                return data.get("data", data.get("announcements", []))
            return []
        except Exception as e:
            logger.error(f"[TRADE POLLER]: NSE fetch error: {e}")
            return []


_trade_nse_session: Optional[_TradingNSESession] = None


def _get_trade_nse_session() -> _TradingNSESession:
    global _trade_nse_session
    if _trade_nse_session is None:
        _trade_nse_session = _TradingNSESession()
    return _trade_nse_session


def _get_armed_configs() -> List:
    """Get all TradeConfigs with status 'armed' for today's date."""
    try:
        from app.database import SessionLocal, TradeConfig
        db = SessionLocal()
        try:
            today_ist = datetime.now(IST).strftime("%Y-%m-%d")
            configs = db.query(TradeConfig).filter(
                TradeConfig.status == "armed",
                TradeConfig.is_active == True,
                TradeConfig.purchase_date == today_ist
            ).all()
            # Detach from session to avoid lazy-load issues
            result = []
            for c in configs:
                result.append({
                    "id": c.id,
                    "symbol": c.symbol.upper().strip(),
                    "instrument_key": c.instrument_key,
                    "quantity": c.quantity,
                    "stoploss_pct": c.stoploss_pct,
                    "stoploss_type": c.stoploss_type,
                    "broker": c.broker,
                    "order_type": c.order_type,
                    "limit_price": c.limit_price,
                    "ai_provider": c.ai_provider,
                    "trigger_subject": c.trigger_subject or "Outcome of Board Meeting",
                })
            return result
        finally:
            db.close()
    except Exception as e:
        logger.error(f"[TRADE POLLER]: Error getting armed configs: {e}")
        return []


def _check_match(announcement: dict, armed_configs: list) -> Optional[dict]:
    """Check if an NSE announcement matches any armed config's trigger subject + symbol."""
    subject = str(announcement.get("desc") or announcement.get("subject") or announcement.get("an_desc") or "").strip()
    details = str(announcement.get("attchmntText") or announcement.get("details") or announcement.get("desc") or "").strip()
    ann_symbol = str(announcement.get("symbol") or announcement.get("sm_name") or "").strip().upper()

    if not subject or not ann_symbol:
        return None

    full_text = f"{subject} {details}".lower()

    for config in armed_configs:
        trigger = config["trigger_subject"].lower()
        if trigger in subject.lower() and ann_symbol == config["symbol"]:
            # If trigger subject is 'Outcome of Board Meeting', require 'finan' in details
            if "outcome of board meeting" in trigger:
                if "finan" not in full_text:
                    continue
            return config
    return None


def _is_recent_announcement(announcement: dict, max_age_minutes: int = 30) -> bool:
    """Check if the announcement is recent (within max_age_minutes)."""
    date_str = str(
        announcement.get("an_dt") or
        announcement.get("bcastDate") or
        announcement.get("broadcastDate") or
        announcement.get("date") or ""
    ).strip()
    if not date_str:
        return True  # If no date, assume it's recent

    try:
        from dateutil import parser
        parsed = parser.parse(date_str)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=IST)
        now = datetime.now(IST)
        age = now - parsed.astimezone(IST)
        return age.total_seconds() < max_age_minutes * 60
    except Exception:
        return True


_offmarket_interval_minutes = 45.0


def _is_high_speed_polling_hours() -> bool:
    """Check if current time is Monday-Friday 9:00 AM to 3:30 PM IST."""
    now = datetime.now(IST)
    if now.weekday() > 4:  # Saturday = 5, Sunday = 6
        return False
    start_time = now.replace(hour=9, minute=0, second=0, microsecond=0)
    end_time = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return start_time <= now <= end_time


# Track already-triggered event hashes to avoid duplicate triggers
_triggered_hashes = set()


async def _poll_loop():
    """Main polling loop — runs continuously while there are armed configs."""
    global _poller_running, _last_poll_status, _triggered_hashes, _offmarket_interval_minutes
    _poller_running = True
    _last_poll_status["running"] = True
    logger.info("🚀 [TRADE POLLER]: Starting adaptive NSE polling loop")

    nse = _get_trade_nse_session()
    consecutive_errors = 0

    while _poller_running:
        try:
            armed = _get_armed_configs()
            _last_poll_status["armed_count"] = len(armed)

            if not armed:
                # No armed configs — sleep longer and recheck
                await asyncio.sleep(10)
                continue

            is_market_open = _is_high_speed_polling_hours()
            _last_poll_status["mode"] = f"high_speed ({_poll_interval}s)" if is_market_open else f"off_market ({_offmarket_interval_minutes}m)"
            _last_poll_status["offmarket_interval_minutes"] = _offmarket_interval_minutes
            _last_poll_status["poll_interval_seconds"] = _poll_interval

            # Fetch announcements
            announcements = await asyncio.get_event_loop().run_in_executor(
                None, nse.fetch_announcements
            )
            _last_poll_status["polls_total"] += 1
            _last_poll_status["last_poll_at"] = datetime.now(IST).isoformat()
            consecutive_errors = 0

            for ann in announcements:
                if not isinstance(ann, dict):
                    continue

                matched_config = _check_match(ann, armed)
                if not matched_config:
                    continue

                if not _is_recent_announcement(ann, max_age_minutes=30):
                    continue

                # Compute event hash to avoid re-triggering
                subject = str(ann.get("desc") or ann.get("subject") or "")
                ann_date = str(ann.get("an_dt") or ann.get("bcastDate") or "")
                evt_hash = f"{matched_config['symbol']}:{subject[:100]}:{ann_date}"
                if evt_hash in _triggered_hashes:
                    continue
                _triggered_hashes.add(evt_hash)

                # ========== TRIGGER! ==========
                logger.info(f"⚡ [TRADE TRIGGER]: {matched_config['symbol']} — '{subject[:80]}' → Placing {matched_config['order_type']} {matched_config['broker'].upper()} order!")
                _last_poll_status["triggers_total"] += 1

                # Run trade execution in a background task to avoid blocking the poller
                asyncio.create_task(_execute_trade(matched_config, ann))

            # Adaptive Sleep:
            # - During market hours (Mon-Fri 9:00 AM to 3:30 PM IST): high speed (_poll_interval)
            # - Outside market hours: _offmarket_interval_minutes (default 45 min)
            if is_market_open:
                await asyncio.sleep(_poll_interval)
            else:
                logger.info(f"🌙 [TRADE POLLER]: Outside market hours (Mon-Fri 9:00 AM - 3:30 PM). Next poll in {_offmarket_interval_minutes} minutes.")
                await asyncio.sleep(_offmarket_interval_minutes * 60)

        except asyncio.CancelledError:
            break
        except Exception as e:
            consecutive_errors += 1
            _last_poll_status["last_error"] = str(e)
            logger.error(f"[TRADE POLLER]: Error in poll loop: {e}")
            wait = min(5 * consecutive_errors, 30)
            await asyncio.sleep(wait)

    _poller_running = False
    _last_poll_status["running"] = False
    logger.info("🛑 [TRADE POLLER]: Polling loop stopped")


def execute_manual_poll() -> dict:
    """Trigger an immediate, on-demand manual poll of NSE announcements."""
    logger.info("⚡ [TRADE POLLER]: Manual poll requested by user")
    nse = _get_trade_nse_session()
    announcements = nse.fetch_announcements()
    armed = _get_armed_configs()

    _last_poll_status["polls_total"] += 1
    _last_poll_status["last_poll_at"] = datetime.now(IST).isoformat()

    triggers_count = 0
    matched_items = []

    for ann in announcements:
        if not isinstance(ann, dict):
            continue
        matched_config = _check_match(ann, armed)
        if not matched_config:
            continue
        if not _is_recent_announcement(ann, max_age_minutes=30):
            continue

        subject = str(ann.get("desc") or ann.get("subject") or "")
        ann_date = str(ann.get("an_dt") or ann.get("bcastDate") or "")
        evt_hash = f"{matched_config['symbol']}:{subject[:100]}:{ann_date}"
        if evt_hash in _triggered_hashes:
            continue
        _triggered_hashes.add(evt_hash)

        triggers_count += 1
        matched_items.append({
            "symbol": matched_config["symbol"],
            "subject": subject,
            "config_id": matched_config["id"]
        })

        try:
            loop = asyncio.get_event_loop()
            loop.create_task(_execute_trade(matched_config, ann))
        except Exception:
            pass

    return {
        "success": True,
        "announcements_fetched": len(announcements),
        "armed_configs_checked": len(armed),
        "triggers_found": triggers_count,
        "matched_items": matched_items,
        "timestamp": datetime.now(IST).isoformat()
    }


def set_offmarket_interval(minutes: float):
    """Set the off-market polling interval in minutes (min 1m, max 1440m/24h)."""
    global _offmarket_interval_minutes
    _offmarket_interval_minutes = max(1.0, min(1440.0, minutes))
    logger.info(f"[TRADE POLLER]: Off-market polling interval set to {_offmarket_interval_minutes} minutes")


async def _execute_trade(config: dict, announcement: dict):
    """Execute a trade: update DB → place broker order → run premium AI → send Telegram."""
    from app.database import SessionLocal, TradeConfig, TradeOrder
    from app.services.broker_gateway import get_broker
    from app.services.trade_ai_analyzer import analyze_earnings_disclosure_2step

    config_id = config["id"]
    symbol = config["symbol"]
    subject = str(announcement.get("desc") or announcement.get("subject") or "")
    description = str(announcement.get("attchmntText") or announcement.get("description") or subject)

    # 1. Update TradeConfig status to 'triggered'
    db = SessionLocal()
    try:
        tc = db.query(TradeConfig).filter(TradeConfig.id == config_id).first()
        if tc:
            tc.status = "triggered"
            tc.triggered_at = datetime.utcnow()
            db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()

    # 2. Place broker order
    broker = get_broker(config["broker"])
    result = await asyncio.get_event_loop().run_in_executor(
        None,
        lambda: broker.place_order(
            symbol=symbol,
            instrument_key=config.get("instrument_key", ""),
            side="BUY",
            quantity=config["quantity"],
            order_type=config["order_type"],
            limit_price=config.get("limit_price"),
            stoploss_type=config["stoploss_type"],
            stoploss_pct=config["stoploss_pct"]
        )
    )

    # 3. Save TradeOrder
    db = SessionLocal()
    try:
        order = TradeOrder(
            config_id=config_id,
            symbol=symbol,
            side="BUY",
            quantity=config["quantity"],
            order_type=config["order_type"],
            limit_price=config.get("limit_price"),
            price=result.price,
            broker=config["broker"],
            broker_order_id=result.broker_order_id,
            broker_response=json.dumps(result.raw_response, default=str),
            status=result.status,
            error_message=result.message if not result.success else None,
            created_at=datetime.utcnow(),
            filled_at=datetime.utcnow() if result.status == "filled" else None,
        )
        db.add(order)

        # Update TradeConfig status
        tc = db.query(TradeConfig).filter(TradeConfig.id == config_id).first()
        if tc:
            if result.success:
                tc.status = "bought"
                tc.buy_price = result.price
                tc.bought_at = datetime.utcnow()
                logger.info(f"✅ [TRADE BOUGHT]: {symbol} {config['quantity']}x @ {result.price}")
            else:
                tc.status = "failed"
                tc.notes = f"Order failed: {result.message}"
                logger.error(f"❌ [TRADE FAILED]: {symbol} → {result.message}")

        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"[TRADE POLLER]: DB error saving order: {e}")
    finally:
        db.close()

    # 4. Send Telegram alert for the trade
    try:
        from app.services.telegram_notifier import send_telegram_alert
        status_emoji = "✅" if result.success else "❌"
        send_telegram_alert(
            title=f"{status_emoji} {symbol} {'BOUGHT' if result.success else 'ORDER FAILED'}",
            symbol=symbol,
            sentiment="positive" if result.success else "negative",
            impact_score=0.0,
            summary=f"{'Bought' if result.success else 'Failed'} {config['quantity']}x {symbol} via {config['broker'].upper()}\n"
                    f"Price: ₹{result.price or 'N/A'}\n"
                    f"Order ID: {result.broker_order_id or 'N/A'}\n"
                    f"Trigger: {subject[:100]}\n"
                    f"Message: {result.message}",
            provider=config["broker"].upper(),
            alert_type=f"⚡ TRADE EXECUTION"
        )
    except Exception:
        pass

    # 5. Run 2-Step AI Earnings Analysis (Custom REST API -> OpenRouter Fallback)
    try:
        attachment_file = str(
            announcement.get("attachment") or
            announcement.get("attchmntFile") or
            announcement.get("url") or ""
        ).strip()

        if attachment_file and not attachment_file.startswith("http"):
            attachment_url = f"https://nsearchives.nseindia.com/corporate/{attachment_file}"
        else:
            attachment_url = attachment_file

        await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: analyze_earnings_disclosure_2step(
                symbol=symbol,
                title=subject,
                attachment_url=attachment_url,
                pdf_text=description,
                config_id=config_id,
            )
        )
    except Exception as e:
        logger.error(f"[TRADE AI]: 2-Step Earnings Analysis failed for {symbol}: {e}")



# ─── Public API ──────────────────────────────────────────────────────────────

def start_trade_poller():
    """Start the trade NSE poller as an asyncio background task."""
    global _poller_task, _poller_running
    if _poller_running and _poller_task and not _poller_task.done():
        logger.info("[TRADE POLLER]: Already running")
        return
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    _poller_running = True
    _poller_task = loop.create_task(_poll_loop())
    logger.info("🚀 [TRADE POLLER]: Background task started")


def stop_trade_poller():
    """Stop the trade poller."""
    global _poller_running, _poller_task
    _poller_running = False
    if _poller_task and not _poller_task.done():
        _poller_task.cancel()
    logger.info("🛑 [TRADE POLLER]: Background task stopped")


def get_poller_status() -> dict:
    """Get current poller status."""
    return dict(_last_poll_status)


def set_poll_interval(seconds: float):
    """Set the polling interval (min 0.5s, max 30s)."""
    global _poll_interval
    _poll_interval = max(0.5, min(30.0, seconds))
    logger.info(f"[TRADE POLLER]: Poll interval set to {_poll_interval}s")
