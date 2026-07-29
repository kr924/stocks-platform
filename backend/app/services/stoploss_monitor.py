"""
Stoploss Monitor — Background worker that monitors LTP for bought positions
and auto-sells when stoploss threshold is breached.
Supports both software stoploss (server-side) and bracket order tracking.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict

logger = logging.getLogger("app.stoploss_monitor")

IST = timezone(timedelta(hours=5, minutes=30))

_monitor_task: Optional[asyncio.Task] = None
_monitor_running = False
_check_interval = 5.0  # seconds

_monitor_status: Dict = {
    "running": False,
    "watching_count": 0,
    "last_check_at": None,
    "stoploss_triggers": 0,
}


def _get_bought_configs():
    """Get all TradeConfigs with status 'bought' and stoploss_type 'software'."""
    try:
        from app.database import SessionLocal, TradeConfig
        db = SessionLocal()
        try:
            configs = db.query(TradeConfig).filter(
                TradeConfig.status == "bought",
                TradeConfig.is_active == True,
                TradeConfig.stoploss_type == "software"
            ).all()
            result = []
            for c in configs:
                if c.buy_price and c.buy_price > 0:
                    result.append({
                        "id": c.id,
                        "symbol": c.symbol,
                        "instrument_key": c.instrument_key,
                        "quantity": c.quantity,
                        "buy_price": c.buy_price,
                        "stoploss_pct": c.stoploss_pct,
                        "broker": c.broker,
                        "stoploss_price": round(c.buy_price * (1 - c.stoploss_pct / 100), 2),
                    })
            return result
        finally:
            db.close()
    except Exception as e:
        logger.error(f"[STOPLOSS]: Error getting bought configs: {e}")
        return []


def _is_market_hours() -> bool:
    """Check if within NSE market hours."""
    now = datetime.now(IST)
    if now.weekday() > 4:
        return False
    market_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
    market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return market_open <= now <= market_close


async def _monitor_loop():
    """Main stoploss monitoring loop."""
    global _monitor_running, _monitor_status
    _monitor_running = True
    _monitor_status["running"] = True
    logger.info("🛡️ [STOPLOSS MONITOR]: Started")

    while _monitor_running:
        try:
            if not _is_market_hours():
                await asyncio.sleep(30)
                continue

            configs = await asyncio.get_event_loop().run_in_executor(
                None, _get_bought_configs
            )
            _monitor_status["watching_count"] = len(configs)
            _monitor_status["last_check_at"] = datetime.now(IST).isoformat()

            if not configs:
                await asyncio.sleep(10)
                continue

            from app.services.broker_gateway import get_broker

            for config in configs:
                try:
                    broker = get_broker(config["broker"])
                    ltp = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: broker.get_ltp(config["instrument_key"])
                    )

                    if ltp is None:
                        continue

                    stoploss_price = config["stoploss_price"]
                    if ltp <= stoploss_price:
                        # STOPLOSS BREACHED! Execute sell order
                        logger.warning(
                            f"🔴 [STOPLOSS TRIGGERED]: {config['symbol']} LTP ₹{ltp} <= SL ₹{stoploss_price} "
                            f"(bought @ ₹{config['buy_price']}, SL {config['stoploss_pct']}%)"
                        )
                        _monitor_status["stoploss_triggers"] += 1

                        await _execute_stoploss_sell(config, ltp, stoploss_price)

                except Exception as e:
                    logger.error(f"[STOPLOSS]: Error checking {config['symbol']}: {e}")

            await asyncio.sleep(_check_interval)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[STOPLOSS MONITOR]: Error: {e}")
            await asyncio.sleep(10)

    _monitor_running = False
    _monitor_status["running"] = False
    logger.info("🛑 [STOPLOSS MONITOR]: Stopped")


async def _execute_stoploss_sell(config: dict, ltp: float, stoploss_price: float):
    """Execute a stoploss sell order and update DB."""
    from app.database import SessionLocal, TradeConfig, TradeOrder
    from app.services.broker_gateway import get_broker

    broker = get_broker(config["broker"])
    result = await asyncio.get_event_loop().run_in_executor(
        None,
        lambda: broker.place_order(
            symbol=config["symbol"],
            instrument_key=config["instrument_key"],
            side="SELL",
            quantity=config["quantity"],
            order_type="MARKET",
        )
    )

    # Save order and update config
    db = SessionLocal()
    try:
        order = TradeOrder(
            config_id=config["id"],
            symbol=config["symbol"],
            side="SELL",
            quantity=config["quantity"],
            order_type="MARKET",
            price=result.price or ltp,
            stoploss_price=stoploss_price,
            broker=config["broker"],
            broker_order_id=result.broker_order_id,
            broker_response=json.dumps(result.raw_response, default=str),
            status=result.status,
            error_message=result.message if not result.success else None,
            created_at=datetime.utcnow(),
            filled_at=datetime.utcnow() if result.status == "filled" else None,
        )
        db.add(order)

        tc = db.query(TradeConfig).filter(TradeConfig.id == config["id"]).first()
        if tc:
            sell_price = result.price or ltp
            tc.status = "sold"
            tc.sell_price = sell_price
            tc.sold_at = datetime.utcnow()
            tc.pnl = round((sell_price - config["buy_price"]) * config["quantity"], 2)
            tc.notes = f"Stoploss triggered @ ₹{ltp} (SL: ₹{stoploss_price}). P&L: ₹{tc.pnl}"
            logger.info(f"📉 [STOPLOSS SOLD]: {config['symbol']} @ ₹{sell_price} → P&L: ₹{tc.pnl}")

        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"[STOPLOSS]: DB error: {e}")
    finally:
        db.close()

    # Telegram alert
    try:
        from app.services.telegram_notifier import send_telegram_alert
        pnl = round((result.price or ltp - config["buy_price"]) * config["quantity"], 2)
        send_telegram_alert(
            title=f"🔴 STOPLOSS: {config['symbol']} SOLD",
            symbol=config["symbol"],
            sentiment="negative",
            impact_score=-1.0,
            summary=f"Stoploss triggered for {config['symbol']}\n"
                    f"Buy: ₹{config['buy_price']} → Sell: ₹{result.price or ltp}\n"
                    f"Loss: ₹{pnl}\n"
                    f"SL%: {config['stoploss_pct']}%",
            provider=config["broker"].upper(),
            alert_type="🛡️ STOPLOSS EXECUTION"
        )
    except Exception:
        pass


# ─── Public API ──────────────────────────────────────────────────────────────

def start_stoploss_monitor():
    """Start the stoploss monitor background task."""
    global _monitor_task, _monitor_running
    if _monitor_running and _monitor_task and not _monitor_task.done():
        logger.info("[STOPLOSS MONITOR]: Already running")
        return
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    _monitor_running = True
    _monitor_task = loop.create_task(_monitor_loop())
    logger.info("🛡️ [STOPLOSS MONITOR]: Background task started")


def stop_stoploss_monitor():
    """Stop the stoploss monitor."""
    global _monitor_running, _monitor_task
    _monitor_running = False
    if _monitor_task and not _monitor_task.done():
        _monitor_task.cancel()


def get_stoploss_status() -> dict:
    """Get current stoploss monitor status."""
    return dict(_monitor_status)
