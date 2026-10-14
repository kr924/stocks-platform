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
# The unified exchange hub (app/services/exchange_hub.py) drives announcement
# fetching now, and calls handle_announcements() the moment data lands. This
# interval only governs the watchdog that covers the hub going silent, so it no
# longer needs to be sub-second — that was the main source of CPU burn.
_poll_interval = 5.0
# Event loop that owns the trade coroutines, captured at startup so worker
# threads can schedule execution back onto it.
_main_loop: Optional[asyncio.AbstractEventLoop] = None
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


def _config_identifiers(symbol: str) -> set:
    """
    Every identifier an armed target can be recognised by.

    A target is armed under one symbol, but the same company arrives from NSE
    as a ticker and from BSE as a scrip code / scrip id. The registry maps them
    onto each other, so a BSE filing matches a target armed as "ELECON" and an
    NSE filing matches one armed as "505700".
    """
    symbol = (symbol or "").strip().upper()
    ids = {symbol} if symbol else set()
    try:
        from app.services import symbol_registry
        rec = symbol_registry.resolve(
            nse_symbol=symbol, scrip_id=symbol,
            scrip_cd=symbol if symbol.isdigit() else "",
        )
        for k in ("symbol", "nse_symbol", "bse_scrip_id", "bse_scrip_cd", "isin"):
            v = rec.get(k)
            if v:
                ids.add(str(v).strip().upper())
    except Exception:
        pass
    return ids


def _announcement_identifiers(ann) -> set:
    return {
        str(v).strip().upper()
        for v in (ann.symbol, ann.nse_symbol, ann.bse_scrip_id, ann.scrip_code, ann.isin)
        if v
    }


def _check_match(ann, armed_configs: list, assume_result: bool = False) -> Optional[dict]:
    """
    Match a normalised announcement (either exchange) against armed configs.

    Auto-trading fires on *financial results only*. The previous implementation
    fell through to "any announcement for an armed symbol", which meant a routine
    compliance filing or a trading-window notice could place a live order. The
    shared classifier (news_fetching_strategy.md) makes that call, with the BSE
    subcategory included, so the trading engine and the intelligence feed agree
    on what counts as a result.

    `assume_result` is for filings already proven to be results by other means —
    the first page of their PDF, or a body the exchange filled in later.
    """
    from app.services.announcement_classifier import is_financial_result

    ann_ids = _announcement_identifiers(ann)
    if not ann_ids:
        return None

    matching = [c for c in armed_configs if c["identifiers"] & ann_ids]
    if not matching:
        return None

    if assume_result:
        channel = "confirmed"
    else:
        is_result, channel = is_financial_result(ann.title, ann.description, ann.category_name)
        if not is_result:
            logger.debug(
                f"⏭️ [TRADE POLLER]: '{(ann.title or '')[:70]}' for {ann.symbol} "
                f"({ann.exchange.upper()}) is not a financial result — no trigger."
            )
            return None

    config = matching[0]
    trigger = (config.get("trigger_subject") or "").lower().strip()
    if trigger and trigger not in f"{ann.title} {ann.description}".lower():
        # Subject-specific arm that this filing does not satisfy; the result is
        # still a result, so let it through on the results channel it arrived on.
        logger.debug(
            f"[TRADE POLLER]: {ann.symbol} result matched via channel '{channel}' "
            f"(configured trigger '{trigger}' not present)."
        )
    return config


def _is_recent(ann, max_age_minutes: int = 30) -> bool:
    """
    Is this filing from today's session (IST), or within `max_age_minutes`?

    Same rule as before both exchanges were matched — any filing from today
    counts — now read off the normalised UTC event_time rather than NSE's
    an_dt string, which BSE does not have.
    """
    when = getattr(ann, "event_time", None)
    if not when:
        return True
    try:
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        now = datetime.now(IST)
        if when.astimezone(IST).date() == now.date():
            return True
        return (now - when.astimezone(IST)).total_seconds() < max_age_minutes * 60
    except Exception:
        return True


def _claim_config(config_id: int) -> bool:
    """
    Atomically move one armed target to 'triggered'. True for exactly one caller.

    This is what makes "only the first filing buys". The same result reaches us
    up to four times — the board outcome and the results PDF, on each of NSE and
    BSE — sometimes inside one fetch, sometimes minutes apart, sometimes from the
    PDF-probe thread while the hub is mid-cycle. The guard used to be an
    in-memory set keyed on (symbol, subject, date), which two exchanges with
    different subjects passed straight through. A conditional UPDATE in the
    database is the one place every path agrees on.
    """
    try:
        from app.database import SessionLocal, TradeConfig
        db = SessionLocal()
        try:
            n = db.query(TradeConfig).filter(
                TradeConfig.id == config_id,
                TradeConfig.status == "armed",
            ).update(
                {"status": "triggered", "triggered_at": datetime.utcnow()},
                synchronize_session=False,
            )
            db.commit()
            return n == 1
        finally:
            db.close()
    except Exception as e:
        logger.error(f"[TRADE POLLER]: Could not claim config {config_id}: {e}")
        return False


def _to_announcement(item):
    """Accept a normalised Announcement, or a raw NSE dict from the watchdog path."""
    if hasattr(item, "exchange") and hasattr(item, "title"):
        return item
    if isinstance(item, dict):
        try:
            from app.services.exchange_hub import _normalize_nse
            return _normalize_nse(item)
        except Exception as e:
            logger.debug(f"[TRADE POLLER]: could not normalise NSE row: {e}")
    return None


def _trade_payload(ann) -> dict:
    """The fields _execute_trade and the earnings AI read, from either exchange."""
    return {
        "exchange": ann.exchange,
        "symbol": ann.symbol,
        "desc": ann.title,
        "subject": ann.title,
        "attchmntText": ann.description,
        "description": ann.description,
        # A full URL on both exchanges once normalised, so the AI does not
        # mistake a BSE attachment for an NSE archive path.
        "attchmntFile": ann.url,
        "an_dt": ann.event_time.isoformat() if getattr(ann, "event_time", None) else "",
    }


_offmarket_interval_minutes = 45.0


def _is_high_speed_polling_hours() -> bool:
    """Check if current time is Monday-Friday 9:00 AM to 3:30 PM IST."""
    now = datetime.now(IST)
    if now.weekday() > 4:  # Saturday = 5, Sunday = 6
        return False
    start_time = now.replace(hour=9, minute=0, second=0, microsecond=0)
    end_time = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return start_time <= now <= end_time


def handle_announcements(announcements: list, assume_result: bool = False) -> int:
    """
    Match a freshly fetched announcement batch — NSE and BSE — against armed configs.

    Called synchronously by the exchange hub the instant data lands, so the
    trading engine sees a filing on the same fetch that feeds the intelligence
    pipeline — no second HTTP round-trip and no extra latency. Also called by
    the PDF probe and the late-body path with `assume_result=True` once a
    filing has been proven to be a result.

    The earliest filing in the batch is considered first, and each target is
    claimed atomically before an order is scheduled, so the first filing of a
    result buys and every later copy — the other exchange, the other channel —
    finds the target already triggered.

    Safe to call from a worker thread: trade execution is scheduled back onto the
    main event loop. Returns the number of triggers fired.
    """
    global _last_poll_status

    if not announcements:
        return 0

    armed = _get_armed_configs()
    _last_poll_status["armed_count"] = len(armed)
    _last_poll_status["polls_total"] += 1
    _last_poll_status["last_poll_at"] = datetime.now(IST).isoformat()

    if not armed:
        return 0

    for c in armed:
        c["identifiers"] = _config_identifiers(c["symbol"])

    batch = [a for a in (_to_announcement(x) for x in announcements) if a is not None]
    batch.sort(key=lambda a: getattr(a, "event_time", None) or datetime.utcnow())

    triggers = 0
    for ann in batch:
        if not armed:
            break

        matched_config = _check_match(ann, armed, assume_result=assume_result)
        if not matched_config:
            continue

        if not _is_recent(ann, max_age_minutes=30):
            continue

        if not _claim_config(matched_config["id"]):
            # Another filing — usually the same result on the other exchange —
            # already triggered this target.
            armed = [c for c in armed if c["id"] != matched_config["id"]]
            logger.info(
                f"[TRADE POLLER]: {matched_config['symbol']} already triggered — "
                f"{ann.exchange.upper()} copy '{(ann.title or '')[:60]}' ignored."
            )
            continue
        armed = [c for c in armed if c["id"] != matched_config["id"]]

        # ========== TRIGGER! ==========
        logger.info(
            f"⚡ [TRADE TRIGGER]: {matched_config['symbol']} — {ann.exchange.upper()} "
            f"'{(ann.title or '')[:80]}' → Placing {matched_config['order_type']} "
            f"{matched_config['broker'].upper()} order!"
        )
        _last_poll_status["triggers_total"] += 1
        _last_poll_status["last_trigger"] = {
            "symbol": matched_config["symbol"],
            "exchange": ann.exchange,
            "title": (ann.title or "")[:200],
            "at": datetime.now(IST).isoformat(),
        }
        triggers += 1

        _schedule_trade(matched_config, _trade_payload(ann))

    return triggers


def _schedule_trade(config: dict, announcement: dict):
    """Schedule trade execution on the main event loop from any thread."""
    coro = _execute_trade(config, announcement)
    loop = _main_loop
    if loop and loop.is_running():
        asyncio.run_coroutine_threadsafe(coro, loop)
        return
    try:
        asyncio.get_event_loop().create_task(coro)
    except RuntimeError:
        logger.error("[TRADE POLLER]: No event loop available to execute trade")
        coro.close()


async def _poll_loop():
    """
    Watchdog loop.

    The exchange hub is the primary feed and calls handle_announcements()
    directly. This loop only fetches when the hub's snapshot has gone stale,
    which covers the hub erroring out or being starved. Under normal operation it
    issues no HTTP requests at all.
    """
    global _poller_running, _last_poll_status, _offmarket_interval_minutes
    _poller_running = True
    _last_poll_status["running"] = True
    logger.info("🚀 [TRADE POLLER]: Watchdog started (exchange hub is the primary feed)")

    nse = _get_trade_nse_session()
    consecutive_errors = 0
    # Treat the hub as stale after this long without a successful fetch.
    stale_after_seconds = 30.0

    while _poller_running:
        try:
            armed = _get_armed_configs()
            _last_poll_status["armed_count"] = len(armed)

            if not armed:
                await asyncio.sleep(30)
                continue

            is_market_open = _is_high_speed_polling_hours()
            _last_poll_status["mode"] = (
                f"hub_driven (watchdog {_poll_interval}s)" if is_market_open
                else f"off_market ({_offmarket_interval_minutes}m)"
            )
            _last_poll_status["offmarket_interval_minutes"] = _offmarket_interval_minutes
            _last_poll_status["poll_interval_seconds"] = _poll_interval

            if not is_market_open:
                await asyncio.sleep(_offmarket_interval_minutes * 60)
                continue

            # Only fetch if the hub has gone quiet.
            import time as _time
            from app.services.exchange_hub import get_latest_snapshot

            _, snapshot_at = get_latest_snapshot()
            hub_age = _time.monotonic() - snapshot_at if snapshot_at else float("inf")

            if hub_age > stale_after_seconds:
                logger.warning(
                    f"[TRADE POLLER]: Exchange hub stale ({hub_age:.0f}s) — watchdog fetching directly"
                )
                announcements = await asyncio.get_event_loop().run_in_executor(
                    None, nse.fetch_announcements
                )
                await asyncio.get_event_loop().run_in_executor(
                    None, handle_announcements, announcements
                )
                _last_poll_status["last_error"] = None

            consecutive_errors = 0
            await asyncio.sleep(_poll_interval)

        except asyncio.CancelledError:
            break
        except Exception as e:
            consecutive_errors += 1
            _last_poll_status["last_error"] = str(e)
            logger.error(f"[TRADE POLLER]: Error in watchdog loop: {e}")
            wait = min(5 * consecutive_errors, 30)
            await asyncio.sleep(wait)

    _poller_running = False
    _last_poll_status["running"] = False
    logger.info("🛑 [TRADE POLLER]: Watchdog stopped")


def execute_manual_poll() -> dict:
    """Trigger an immediate, on-demand manual poll of NSE announcements."""
    logger.info("⚡ [TRADE POLLER]: Manual poll requested by user")
    nse = _get_trade_nse_session()
    announcements = nse.fetch_announcements()
    armed = _get_armed_configs()

    triggers_count = handle_announcements(announcements)

    return {
        "success": True,
        "announcements_fetched": len(announcements),
        "armed_configs_checked": len(armed),
        "triggers_found": triggers_count,
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

        # Auto-delete TradeConfig so completed arm automatically vanishes from target table
        tc = db.query(TradeConfig).filter(TradeConfig.id == config_id).first()
        if tc:
            logger.info(f"🗑️ [AUTO-DELETE TARGET CONFIG]: Removing #{symbol} (Config ID {config_id}) after execution complete.")
            db.delete(tc)

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
                    f"Trigger: [{str(announcement.get('exchange') or 'nse').upper()}] {subject[:100]}\n"
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
    global _poller_task, _poller_running, _main_loop
    if _poller_running and _poller_task and not _poller_task.done():
        logger.info("[TRADE POLLER]: Already running")
        return
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    # Remember the loop so hub callbacks running on worker threads can schedule
    # trade coroutines back onto it.
    _main_loop = loop
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
