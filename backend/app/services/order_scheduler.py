"""
Buy orders placed outside trading hours, and the loop that releases them.

A buy asked for at 20:00 cannot be sent to the broker — the exchange is shut and
the order is rejected — but the intent is real and should not be lost to the
user having to remember at 09:15 the next morning. Those are stored against a
TradeConfig with `scheduled_for` set, and executed by the first sweep after the
market opens.

This is deliberately separate from the arming path in trade_nse_poller: an armed
target waits for a *filing* and may never fire, whereas these are unconditional
buys that are merely early.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.database import TradeConfig, TradeOrder

logger = logging.getLogger("app.order_scheduler")

IST = timezone(timedelta(hours=5, minutes=30))

# NSE continuous trading. The pre-open auction runs 09:00-09:15 and does not
# accept the plain market orders this places, so 09:15 is the first usable
# moment rather than 09:00.
MARKET_OPEN = (9, 15)
MARKET_CLOSE = (15, 30)


def market_is_open(now_ist: datetime = None) -> bool:
    """True during continuous trading on a weekday."""
    now = now_ist or datetime.now(IST)
    if now.weekday() > 4:
        return False
    opens = now.replace(hour=MARKET_OPEN[0], minute=MARKET_OPEN[1], second=0, microsecond=0)
    closes = now.replace(hour=MARKET_CLOSE[0], minute=MARKET_CLOSE[1], second=0, microsecond=0)
    return opens <= now <= closes


def next_market_open(now_ist: datetime = None) -> datetime:
    """
    The next moment an order can actually be sent, in IST.

    Weekends roll to Monday. Exchange holidays are not modelled — the sweep
    simply tries, and a rejected order is recorded with the broker's reason
    rather than being silently dropped.
    """
    now = now_ist or datetime.now(IST)
    candidate = now.replace(hour=MARKET_OPEN[0], minute=MARKET_OPEN[1], second=0, microsecond=0)
    if now >= candidate:
        candidate += timedelta(days=1)
    while candidate.weekday() > 4:
        candidate += timedelta(days=1)
    return candidate


def schedule_or_now(now_ist: datetime = None) -> tuple:
    """
    (execute_now, scheduled_for_utc) for a buy requested at this moment.

    Returns a naive UTC datetime to match how every other timestamp in this
    schema is stored.
    """
    now = now_ist or datetime.now(IST)
    if market_is_open(now):
        return True, None
    when = next_market_open(now)
    return False, when.astimezone(timezone.utc).replace(tzinfo=None)


def execute_buy(db: Session, config: TradeConfig) -> dict:
    """
    Send one config's buy to the broker and record the outcome.

    Shared by the immediate path and the scheduled sweep so a scheduled order
    cannot drift from a manual one in how it is placed or recorded.
    """
    from app.services.broker_gateway import get_broker

    broker = get_broker(config.broker)
    result = broker.place_order(
        symbol=config.symbol,
        instrument_key=config.instrument_key or "",
        side="BUY",
        quantity=config.quantity,
        order_type=config.order_type,
        limit_price=config.limit_price,
        stoploss_type=config.stoploss_type,
        stoploss_pct=config.stoploss_pct,
    )

    db.add(TradeOrder(
        config_id=config.id,
        symbol=config.symbol,
        side="BUY",
        quantity=config.quantity,
        order_type=config.order_type,
        limit_price=config.limit_price,
        price=result.price,
        broker=config.broker,
        broker_order_id=result.broker_order_id,
        broker_response=str(result.raw_response),
        status=result.status,
        error_message=result.message if not result.success else None,
    ))

    if result.success:
        config.status = "bought"
        config.buy_price = result.price
        config.bought_at = datetime.utcnow()
        config.scheduled_for = None
        from app.services.holdings import mark_as_holding
        mark_as_holding(db, config.symbol, config.instrument_key or "")
    else:
        # Left scheduled would mean retrying every sweep against a broker that
        # has already refused it. The failure is recorded and the config stops.
        config.status = "failed"
        config.scheduled_for = None
        config.notes = f"Scheduled buy failed: {result.message}"

    try:
        db.commit()
    except Exception:
        db.rollback()
        raise

    return {"success": result.success, "message": result.message, "price": result.price}


def run_due_scheduled_buys(db: Session) -> int:
    """Place every scheduled buy whose time has come. Returns how many were sent."""
    if not market_is_open():
        return 0

    now = datetime.utcnow()
    due = (db.query(TradeConfig)
           .filter(TradeConfig.status == "scheduled",
                   TradeConfig.scheduled_for.isnot(None),
                   TradeConfig.scheduled_for <= now)
           .all())
    if not due:
        return 0

    sent = 0
    for config in due:
        try:
            outcome = execute_buy(db, config)
            sent += 1
            logger.info(
                f"⏰ [SCHEDULED BUY] {config.symbol} x{config.quantity} "
                f"{'placed' if outcome['success'] else 'REJECTED'}: {outcome['message']}"
            )
            try:
                from app.services.telegram_notifier import send_message, _esc
                send_message(
                    f"<b>⏰ SCHEDULED BUY {'PLACED' if outcome['success'] else 'FAILED'}</b>\n"
                    f"{_esc(config.symbol)} × {config.quantity} "
                    f"({_esc(config.order_type)})\n{_esc(outcome['message'] or '')}"
                )
            except Exception:
                pass
        except Exception as e:
            db.rollback()
            logger.error(f"Scheduled buy failed for {config.symbol}: {e}")
    return sent
