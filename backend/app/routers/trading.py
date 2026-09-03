"""
Trading Engine API Router — CRUD for TradeConfigs, order execution, AI logs, and poller control.
"""
import json
import logging
import re
import threading
from datetime import datetime
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.database import (
    get_db, TradeConfig, TradeOrder, TradeAILog, PendingResultOrder,
)

logger = logging.getLogger("app.trading_router")

router = APIRouter(prefix="/api/trading", tags=["trading"])


# ─── Pydantic Schemas ───────────────────────────────────────────────────────

class TradeConfigCreate(BaseModel):
    symbol: str
    instrument_key: Optional[str] = None
    purchase_date: str
    quantity: int = 1
    stoploss_pct: float = 2.0
    stoploss_type: str = "software"
    broker: str = "upstox"
    order_type: str = "MARKET"
    limit_price: Optional[float] = None
    ai_provider: str = "groq"
    trigger_subject: str = "Outcome of Board Meeting"
    notes: Optional[str] = None


class TradeConfigUpdate(BaseModel):
    symbol: Optional[str] = None
    instrument_key: Optional[str] = None
    purchase_date: Optional[str] = None
    quantity: Optional[int] = None
    stoploss_pct: Optional[float] = None
    stoploss_type: Optional[str] = None
    broker: Optional[str] = None
    order_type: Optional[str] = None
    limit_price: Optional[float] = None
    ai_provider: Optional[str] = None
    trigger_subject: Optional[str] = None
    notes: Optional[str] = None


class ManualOrderRequest(BaseModel):
    side: str = "BUY"  # BUY or SELL
    quantity: Optional[int] = None
    order_type: Optional[str] = None
    limit_price: Optional[float] = None


class AutoTradingSettingsUpdate(BaseModel):
    custom_api_url: Optional[str] = None
    premium_openrouter_api_key: Optional[str] = None
    premium_openrouter_model: Optional[str] = None


class DirectBuyRequest(BaseModel):
    """A buy placed straight away, with no filing to wait for and no arming."""
    symbol: str
    instrument_key: Optional[str] = None
    quantity: int = 1
    order_type: str = "MARKET"
    limit_price: Optional[float] = None
    stoploss_pct: float = 2.0
    stoploss_type: str = "software"
    broker: str = "upstox"


class ReanalyseRequest(BaseModel):
    # An OpenRouter model id for this run only; blank uses the configured
    # default. Free text on purpose — OpenRouter adds models faster than a
    # hardcoded list can follow, and a wrong id fails loudly on one filing
    # rather than silently changing every future one.
    model: Optional[str] = None
    # "ai" today. "ocr" is accepted and rejected with a clear message rather
    # than 404ing, so the UI can offer the tab before the engine exists.
    flow: Optional[str] = "ai"


class PendingResultOrderRequest(BaseModel):
    """Order details supplied from the results order screen."""
    quantity: int = 1
    order_type: str = "MARKET"
    limit_price: Optional[float] = None
    stoploss_pct: float = 2.0
    stoploss_type: str = "software"
    broker: str = "upstox"



# ─── Serializer helpers ─────────────────────────────────────────────────────

def _serialize_config(c: TradeConfig) -> dict:
    return {
        "id": c.id,
        "symbol": c.symbol,
        "instrument_key": c.instrument_key,
        "purchase_date": c.purchase_date,
        "quantity": c.quantity,
        "stoploss_pct": c.stoploss_pct,
        "stoploss_type": c.stoploss_type,
        "broker": c.broker,
        "order_type": c.order_type,
        "limit_price": c.limit_price,
        "ai_provider": c.ai_provider,
        "status": c.status,
        "is_active": c.is_active,
        "trigger_subject": c.trigger_subject,
        "buy_price": c.buy_price,
        "sell_price": c.sell_price,
        "pnl": c.pnl,
        "notes": c.notes,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "triggered_at": c.triggered_at.isoformat() if c.triggered_at else None,
        "bought_at": c.bought_at.isoformat() if c.bought_at else None,
        "sold_at": c.sold_at.isoformat() if c.sold_at else None,
    }


def _serialize_order(o: TradeOrder) -> dict:
    return {
        "id": o.id,
        "config_id": o.config_id,
        "symbol": o.symbol,
        "side": o.side,
        "quantity": o.quantity,
        "order_type": o.order_type,
        "limit_price": o.limit_price,
        "price": o.price,
        "stoploss_price": o.stoploss_price,
        "broker": o.broker,
        "broker_order_id": o.broker_order_id,
        "broker_response": o.broker_response,
        "status": o.status,
        "error_message": o.error_message,
        "created_at": o.created_at.isoformat() if o.created_at else None,
        "filled_at": o.filled_at.isoformat() if o.filled_at else None,
    }


def _serialize_ai_log(a: TradeAILog) -> dict:
    metrics = None
    if getattr(a, "metrics_json", None):
        try:
            metrics = json.loads(a.metrics_json)
        except Exception:
            metrics = None
    return {
        "id": a.id,
        "config_id": a.config_id,
        "symbol": a.symbol,
        "company_name": getattr(a, "company_name", None),
        "tracking_ref": getattr(a, "tracking_ref", None),
        "ai_requested_at": a.ai_requested_at.isoformat() if getattr(a, "ai_requested_at", None) else None,
        "ai_completed_at": a.ai_completed_at.isoformat() if getattr(a, "ai_completed_at", None) else None,
        "metrics": metrics,
        "future_growth_outlook": getattr(a, "future_growth_outlook", None),
        "future_projected_numbers": getattr(a, "future_projected_numbers", None),
        "extraction_ok": bool(getattr(a, "extraction_ok", False)),
        "validation": (json.loads(a.validation_json)
                       if getattr(a, "validation_json", None) else None),
        "provider": a.provider,
        "prompt_summary": a.prompt_summary,
        "ai_sentiment": a.ai_sentiment,
        "ai_impact_score": a.ai_impact_score,
        "ai_summary": a.ai_summary,
        "nse_event_title": a.nse_event_title,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "revenue": a.revenue,
        "expenses": a.expenses,
        "operating_profit": a.operating_profit,
        "pbt": a.pbt,
        "other_income": a.other_income,
        "pat_yoy": a.pat_yoy,
        "growth_projection": a.growth_projection,
        "broker_estimates": a.broker_estimates,
        "ai_suggestion": a.ai_suggestion,
        "attachment_url": a.attachment_url,
        "flow_used": a.flow_used,
    }


# ─── Trade Config CRUD ──────────────────────────────────────────────────────

@router.get("/configs")
def list_configs(
    status: Optional[str] = None,
    include_expired: bool = False,
    db: Session = Depends(get_db)
):
    """
    Trade configurations, most recent first.

    Configs retired by the 02:00 sweep are hidden unless asked for: their target
    date has passed, so they are history rather than something to act on, and
    they are kept in the database rather than deleted because some of them
    record real orders.
    """
    q = db.query(TradeConfig).order_by(TradeConfig.created_at.desc())
    if status:
        q = q.filter(TradeConfig.status == status)
    elif not include_expired:
        q = q.filter(TradeConfig.status != "expired")
    configs = q.all()
    return {"configs": [_serialize_config(c) for c in configs]}


@router.post("/configs")
def create_config(body: TradeConfigCreate, db: Session = Depends(get_db)):
    """Create a new trade configuration."""
    config = TradeConfig(
        symbol=body.symbol.upper().strip(),
        instrument_key=body.instrument_key,
        purchase_date=body.purchase_date,
        quantity=body.quantity,
        stoploss_pct=body.stoploss_pct,
        stoploss_type=body.stoploss_type,
        broker=body.broker.lower(),
        order_type=body.order_type.upper(),
        limit_price=body.limit_price,
        ai_provider=body.ai_provider,
        trigger_subject=body.trigger_subject,
        notes=body.notes,
        status="pending",
        is_active=True,
    )
    db.add(config)
    db.commit()
    db.refresh(config)
    logger.info(f"📋 [TRADE CONFIG]: Created #{config.id} {config.symbol} for {config.purchase_date}")
    return {"config": _serialize_config(config)}


@router.put("/configs/{config_id}")
def update_config(config_id: int, body: TradeConfigUpdate, db: Session = Depends(get_db)):
    """Update an existing trade configuration."""
    config = db.query(TradeConfig).filter(TradeConfig.id == config_id).first()
    if not config:
        raise HTTPException(status_code=404, detail="Trade config not found")

    for field, value in body.dict(exclude_unset=True).items():
        if value is not None:
            if field == "symbol":
                value = value.upper().strip()
            elif field == "broker":
                value = value.lower()
            elif field == "order_type":
                value = value.upper()
            setattr(config, field, value)

    db.commit()
    db.refresh(config)
    return {"config": _serialize_config(config)}


@router.delete("/configs/{config_id}")
def delete_config(config_id: int, db: Session = Depends(get_db)):
    """Delete a trade configuration."""
    config = db.query(TradeConfig).filter(TradeConfig.id == config_id).first()
    if not config:
        raise HTTPException(status_code=404, detail="Trade config not found")
    db.delete(config)
    db.commit()
    return {"message": f"Trade config #{config_id} deleted"}


# ─── Arm / Disarm ───────────────────────────────────────────────────────────

@router.post("/configs/{config_id}/arm")
def arm_config(config_id: int, db: Session = Depends(get_db)):
    """Arm a trade config — the NSE poller will start watching for its trigger."""
    config = db.query(TradeConfig).filter(TradeConfig.id == config_id).first()
    if not config:
        raise HTTPException(status_code=404, detail="Trade config not found")

    if config.status not in ("pending", "disarmed"):
        raise HTTPException(status_code=400, detail=f"Cannot arm config in '{config.status}' status. Must be 'pending' or 'disarmed'.")

    config.status = "armed"
    config.is_active = True
    db.commit()
    db.refresh(config)

    # Ensure the poller is running
    try:
        from app.services.trade_nse_poller import start_trade_poller
        start_trade_poller()
    except Exception as e:
        logger.error(f"Failed to start trade poller: {e}")

    logger.info(f"⚡ [ARMED]: Config #{config_id} {config.symbol} armed for {config.purchase_date}")
    return {"config": _serialize_config(config)}


@router.post("/configs/{config_id}/disarm")
def disarm_config(config_id: int, db: Session = Depends(get_db)):
    """Disarm a trade config — stop watching for its trigger."""
    config = db.query(TradeConfig).filter(TradeConfig.id == config_id).first()
    if not config:
        raise HTTPException(status_code=404, detail="Trade config not found")

    if config.status != "armed":
        raise HTTPException(status_code=400, detail=f"Cannot disarm config in '{config.status}' status. Must be 'armed'.")

    config.status = "disarmed"
    db.commit()
    db.refresh(config)
    logger.info(f"🛑 [DISARMED]: Config #{config_id} {config.symbol}")
    return {"config": _serialize_config(config)}


# ─── Manual Buy / Sell ───────────────────────────────────────────────────────

@router.post("/buy-now")
def direct_buy(body: DirectBuyRequest, db: Session = Depends(get_db)):
    """
    Buy a stock outright — no target, no arming, no waiting for a filing.

    Inside 09:15-15:30 IST on a weekday the order goes to the broker now.
    Outside it, the config is stored as `scheduled` for the next open, because
    a broker rejects an order sent to a shut exchange and the intent should not
    be lost to the user having to remember at 09:15 the next morning.

    Either way a TradeConfig is created, so the order appears in the targets
    table alongside everything else rather than in a parallel list.
    """
    from app.main import resolve_instrument_keys
    from app.services.order_scheduler import execute_buy, schedule_or_now

    symbol = body.symbol.upper().strip()
    if not symbol:
        raise HTTPException(status_code=400, detail="Symbol is required")
    if body.quantity < 1:
        raise HTTPException(status_code=400, detail="Quantity must be at least 1")
    if body.order_type.upper() == "LIMIT" and not body.limit_price:
        raise HTTPException(status_code=400, detail="A LIMIT order needs a limit price")

    key = body.instrument_key
    if not key or not _ISIN_RE.match(key.split("|")[-1]):
        resolved = resolve_instrument_keys(symbol)
        if not resolved:
            raise HTTPException(
                status_code=400,
                detail=f"{symbol} does not resolve to an instrument on NSE or BSE",
            )
        key = resolved[0]

    now, scheduled_for = schedule_or_now()
    config = TradeConfig(
        symbol=symbol,
        instrument_key=key,
        purchase_date=datetime.utcnow().strftime("%Y-%m-%d"),
        quantity=body.quantity,
        stoploss_pct=body.stoploss_pct,
        stoploss_type=body.stoploss_type,
        broker=body.broker.lower(),
        order_type=body.order_type.upper(),
        limit_price=body.limit_price,
        status="triggered" if now else "scheduled",
        scheduled_for=scheduled_for,
        trigger_subject="Direct buy",
        notes="Placed directly" if now else "Queued for the next market open",
        triggered_at=datetime.utcnow(),
    )
    db.add(config)
    db.commit()
    db.refresh(config)

    if not now:
        logger.info(f"⏰ [SCHEDULED BUY] {symbol} x{body.quantity} queued for {scheduled_for} UTC")
        return {
            "success": True,
            "scheduled": True,
            "scheduled_for": scheduled_for.isoformat(),
            "message": f"Market is closed — {symbol} is queued for the next open (09:15 IST).",
            "config": _serialize_config(config),
        }

    outcome = execute_buy(db, config)
    db.refresh(config)
    return {
        "success": outcome["success"],
        "scheduled": False,
        "message": outcome["message"],
        "config": _serialize_config(config),
    }


@router.post("/configs/{config_id}/buy")
def manual_buy(config_id: int, body: ManualOrderRequest = None, db: Session = Depends(get_db)):
    """Manually place a BUY order for a trade config."""
    config = db.query(TradeConfig).filter(TradeConfig.id == config_id).first()
    if not config:
        raise HTTPException(status_code=404, detail="Trade config not found")

    if config.status in ("bought", "sold"):
        raise HTTPException(status_code=400, detail=f"Config is already in '{config.status}' status.")

    from app.services.broker_gateway import get_broker

    broker = get_broker(config.broker)
    qty = (body.quantity if body and body.quantity else config.quantity)
    ot = (body.order_type if body and body.order_type else config.order_type)
    lp = (body.limit_price if body and body.limit_price else config.limit_price)

    result = broker.place_order(
        symbol=config.symbol,
        instrument_key=config.instrument_key or "",
        side="BUY",
        quantity=qty,
        order_type=ot,
        limit_price=lp,
        stoploss_type=config.stoploss_type,
        stoploss_pct=config.stoploss_pct,
    )

    # Save order
    order = TradeOrder(
        config_id=config_id,
        symbol=config.symbol,
        side="BUY",
        quantity=qty,
        order_type=ot,
        limit_price=lp,
        price=result.price,
        broker=config.broker,
        broker_order_id=result.broker_order_id,
        broker_response=json.dumps(result.raw_response, default=str),
        status=result.status,
        error_message=result.message if not result.success else None,
    )
    db.add(order)

    if result.success:
        config.status = "bought"
        config.buy_price = result.price
        config.bought_at = datetime.utcnow()
        # The tracker's Holdings tab is the screen that shows live prices for
        # owned stock; it has to learn about this without being told by hand.
        from app.services.holdings import mark_as_holding
        mark_as_holding(db, config.symbol, config.instrument_key or "")
    else:
        config.notes = f"Manual buy failed: {result.message}"

    db.commit()
    db.refresh(config)

    return {
        "config": _serialize_config(config),
        "order": _serialize_order(order),
        "success": result.success,
        "message": result.message,
    }


@router.post("/configs/{config_id}/sell")
def manual_sell(config_id: int, body: ManualOrderRequest = None, db: Session = Depends(get_db)):
    """Manually place a SELL order for a trade config."""
    config = db.query(TradeConfig).filter(TradeConfig.id == config_id).first()
    if not config:
        raise HTTPException(status_code=404, detail="Trade config not found")

    if config.status != "bought":
        raise HTTPException(status_code=400, detail=f"Cannot sell — config is in '{config.status}' status. Must be 'bought'.")

    from app.services.broker_gateway import get_broker

    broker = get_broker(config.broker)
    qty = (body.quantity if body and body.quantity else config.quantity)
    ot = (body.order_type if body and body.order_type else "MARKET")

    result = broker.place_order(
        symbol=config.symbol,
        instrument_key=config.instrument_key or "",
        side="SELL",
        quantity=qty,
        order_type=ot,
        limit_price=(body.limit_price if body and body.limit_price else None),
    )

    order = TradeOrder(
        config_id=config_id,
        symbol=config.symbol,
        side="SELL",
        quantity=qty,
        order_type=ot,
        price=result.price,
        broker=config.broker,
        broker_order_id=result.broker_order_id,
        broker_response=json.dumps(result.raw_response, default=str),
        status=result.status,
        error_message=result.message if not result.success else None,
    )
    db.add(order)

    if result.success:
        sell_price = result.price or 0
        config.status = "sold"
        config.sell_price = sell_price
        from app.services.holdings import clear_holding
        clear_holding(db, config.symbol)
        config.sold_at = datetime.utcnow()
        if config.buy_price:
            config.pnl = round((sell_price - config.buy_price) * qty, 2)
    else:
        config.notes = f"Manual sell failed: {result.message}"

    db.commit()
    db.refresh(config)

    return {
        "config": _serialize_config(config),
        "order": _serialize_order(order),
        "success": result.success,
        "message": result.message,
    }


# ─── Orders ─────────────────────────────────────────────────────────────────

@router.get("/orders")
def list_orders(
    config_id: Optional[int] = None,
    db: Session = Depends(get_db)
):
    """List all executed orders."""
    q = db.query(TradeOrder).order_by(TradeOrder.created_at.desc())
    if config_id:
        q = q.filter(TradeOrder.config_id == config_id)
    orders = q.limit(200).all()
    return {"orders": [_serialize_order(o) for o in orders]}


@router.get("/orders/{order_id}/status")
def check_order_status(order_id: int, db: Session = Depends(get_db)):
    """Check the current status of a broker order."""
    order = db.query(TradeOrder).filter(TradeOrder.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if order.broker_order_id:
        from app.services.broker_gateway import get_broker
        broker = get_broker(order.broker)
        result = broker.get_order_status(order.broker_order_id)
        if result.status != "unknown":
            order.status = result.status
            if result.price:
                order.price = result.price
            if result.status == "filled" and not order.filled_at:
                order.filled_at = datetime.utcnow()
            db.commit()

    return {"order": _serialize_order(order)}


# ─── AI Logs ─────────────────────────────────────────────────────────────────

@router.get("/ai-logs")
def list_ai_logs(
    config_id: Optional[int] = None,
    symbol: Optional[str] = None,
    search: Optional[str] = Query(None, description="symbol, company or summary text"),
    date_from: Optional[str] = Query(None, description="YYYY-MM-DD inclusive"),
    date_to: Optional[str] = Query(None, description="YYYY-MM-DD inclusive"),
    page: int = 1,
    page_size: int = 50,
    db: Session = Depends(get_db),
):
    """List premium AI analysis logs (deduplicated by symbol + nse_event_title)."""
    from datetime import timedelta

    q = db.query(TradeAILog).order_by(TradeAILog.created_at.desc())
    if config_id:
        q = q.filter(TradeAILog.config_id == config_id)
    if symbol:
        q = q.filter(TradeAILog.symbol == symbol.upper())
    if search:
        pattern = f"%{search.strip()}%"
        q = q.filter(or_(
            TradeAILog.symbol.ilike(pattern),
            TradeAILog.company_name.ilike(pattern),
            TradeAILog.ai_summary.ilike(pattern),
            TradeAILog.nse_event_title.ilike(pattern),
            TradeAILog.tracking_ref.ilike(pattern),
        ))
    if date_from:
        try:
            q = q.filter(TradeAILog.created_at >= datetime.strptime(date_from, "%Y-%m-%d"))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid date_from: '{date_from}'")
    if date_to:
        try:
            # Inclusive of the whole day.
            end = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
            q = q.filter(TradeAILog.created_at < end)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid date_to: '{date_to}'")

    raw_logs = q.all()

    # Deduplicate by (symbol, nse_event_title) to avoid duplicate UI cards
    seen = set()
    unique_logs = []
    for l in raw_logs:
        sym_key = (l.symbol or "").upper().strip()
        title_key = (l.nse_event_title or "").strip()
        key = (sym_key, title_key)
        if key not in seen:
            seen.add(key)
            unique_logs.append(l)

    total = len(unique_logs)
    paginated = unique_logs[(page - 1) * page_size : page * page_size]
    return {
        "logs": [_serialize_ai_log(a) for a in paginated],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.delete("/ai-logs/clear")
def clear_ai_logs(db: Session = Depends(get_db)):
    """Clear all AI analysis logs to reset the 2-Step AI Earnings Analysis dashboard."""
    try:
        num_deleted = db.query(TradeAILog).delete()
        db.commit()
        return {"message": "AI analysis logs cleared successfully", "deleted_count": num_deleted}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


# ─── Poller & Monitor Status ────────────────────────────────────────────────

@router.get("/poller/status")
def poller_status():
    """Get trading NSE poller status."""
    from app.services.trade_nse_poller import get_poller_status
    return get_poller_status()


@router.post("/poller/start")
def start_poller():
    """Manually start the trading NSE poller."""
    from app.services.trade_nse_poller import start_trade_poller
    start_trade_poller()
    return {"message": "Trade poller started"}


@router.post("/poller/poll-now")
def trigger_poll_now():
    """Trigger an immediate, on-demand manual poll of NSE corporate announcements."""
    from app.services.trade_nse_poller import execute_manual_poll
    result = execute_manual_poll()
    return result


@router.post("/poller/interval")
def set_poller_interval(
    seconds: Optional[float] = None,
    offmarket_minutes: Optional[float] = None,
):
    """Customize polling intervals (high-speed market seconds or off-market minutes)."""
    from app.services.trade_nse_poller import set_poll_interval, set_offmarket_interval
    if seconds is not None:
        set_poll_interval(seconds)
    if offmarket_minutes is not None:
        set_offmarket_interval(offmarket_minutes)
    return {
        "message": "Poller interval updated",
        "seconds": seconds,
        "offmarket_minutes": offmarket_minutes
    }


@router.get("/stoploss/status")
def stoploss_status():
    """Get stoploss monitor status."""
    from app.services.stoploss_monitor import get_stoploss_status
    return get_stoploss_status()


# ─── Upcoming Earnings Calendar & Auto Trading AI Settings ─────────────────

@router.get("/upcoming-earnings")
def get_upcoming_earnings(db: Session = Depends(get_db)):
    """Fetch upcoming earnings / board meetings for stocks with date sorting and 1y returns."""
    from app.routers.intelligence import get_upcoming_earnings as get_intel_earnings
    items = get_intel_earnings(db)
    return {"upcoming_earnings": items}


@router.post("/upcoming-earnings/sync")
def sync_upcoming_earnings_now(db: Session = Depends(get_db)):
    """Manually trigger immediate sync of upcoming earnings stocks to live Upstox Watchlist quote feed."""
    from app.services.earnings_sync import sync_earnings_to_watchlist
    res = sync_earnings_to_watchlist(db)
    return res


# ─── Pending Financial-Result Order Prompts ─────────────────────────────────

# A real Upstox key carries an ISIN: NSE_EQ|INE002A01018. Anything else in that
# position is the synthetic NSE_EQ|<SYMBOL> form written before BSE-only scrips
# were resolvable, which no quote or candle endpoint accepts.
_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")


def _usable_instrument_key(p: PendingResultOrder) -> Optional[str]:
    """The stored key, re-resolved on the way out when it is the synthetic form."""
    key = p.instrument_key or ""
    if key and _ISIN_RE.match(key.split("|")[-1]):
        return key
    try:
        from app.main import resolve_instrument_keys
        resolved = resolve_instrument_keys(p.symbol or "")
        if resolved:
            return resolved[0]
    except Exception:
        pass
    return key or None


def _position_for(p: PendingResultOrder, db: Session) -> Optional[dict]:
    """
    The trade this filing led to, when there is one.

    The panel has to distinguish "decision still open" from "already bought" —
    without it the same card offers to buy a stock that is already held, and
    there is nowhere to sell it from.
    """
    if not p.config_id:
        return None
    c = db.query(TradeConfig).filter(TradeConfig.id == p.config_id).first()
    if not c:
        return None
    return {
        "config_id": c.id,
        "status": c.status,
        "quantity": c.quantity,
        "buy_price": c.buy_price,
        "sell_price": c.sell_price,
        "pnl": c.pnl,
        "can_sell": c.status == "bought",
    }


def _serialize_pending(p: PendingResultOrder, ai_log: Optional[TradeAILog] = None,
                       db: Session = None) -> dict:
    return {
        "id": p.id,
        "position": _position_for(p, db) if db is not None else None,
        "symbol": p.symbol,
        "company_name": p.company_name,
        "tracking_ref": p.tracking_ref,
        "trade_date": p.trade_date,
        "deferred": bool(p.deferred),
        # "result" or an impact kind (order_win, stock_split, ...). The panel
        # uses it to label the row and to hide the AI section, which never runs
        # for impact news.
        "kind": getattr(p, "kind", "result") or "result",
        "paused": bool(getattr(p, "paused", False)),
        "extraction": (json.loads(p.extraction_json)
                       if getattr(p, "extraction_json", None) else None),
        # Filled by the listing when a close is on file for this trade date.
        "stored_price": None,
        "price_at_announcement": p.price_at_announcement,
        # Lifecycle: announced -> ingested -> alerted -> AI sent -> AI received
        "announced_at": p.event_time.isoformat() if p.event_time else None,
        "ingested_at": p.created_at.isoformat() if p.created_at else None,
        "alert_sent_at": p.alert_sent_at.isoformat() if p.alert_sent_at else None,
        "ai_requested_at": p.ai_requested_at.isoformat() if p.ai_requested_at else None,
        "ai_completed_at": p.ai_completed_at.isoformat() if p.ai_completed_at else None,
        "instrument_key": _usable_instrument_key(p),
        "isin": p.isin,
        "exchange": p.exchange,
        "title": p.title,
        "description": p.description,
        "attachment_url": p.attachment_url,
        "event_time": p.event_time.isoformat() if p.event_time else None,
        "status": p.status,
        "ai_status": p.ai_status,
        "ai_log_id": p.ai_log_id,
        "config_id": p.config_id,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "resolved_at": p.resolved_at.isoformat() if p.resolved_at else None,
        "ai_analysis": _serialize_ai_log(ai_log) if ai_log else None,
        # Screener's published figures for the quarter, filled in by
        # _attach_screener. Absent on impact news, which has no quarter.
        "screener": None,
        "screener_signal": None,
        "all_positive": False,
        "comparison": None,
    }


def _attach_screener(db: Session, pendings: list, serialized: list) -> int:
    """
    Fold the digest's Screener comparison onto the prompt rows, in place.

    The panel and the digest were reading the same PendingResultOrder rows and
    rendering them twice, once with the order form and once with the figures.
    This is the same builder the digest uses, so the two cannot drift: one
    comparison shape, one set of signals.

    cache_only, always. Screener is paced at roughly a company a second, and a
    heavy day is minutes of walking it — far longer than a panel can wait, and
    waiting is what made the digest report a full day as empty. Whatever is
    already on the row is returned now and the rest is fetched behind the
    request. Returns how many rows are still missing figures.
    """
    from app.services.morning_digest import _build_rows, serialize_digest_rows

    result_rows = [p for p in pendings
                   if (getattr(p, "kind", "result") or "result") == "result"]
    if not result_rows:
        return 0
    try:
        rows = _build_rows(db, result_rows, cache_only=True)
        by_ref = {c["tracking_ref"]: c for c in serialize_digest_rows(rows) if c.get("tracking_ref")}
    except Exception as e:
        logger.warning(f"Screener attach failed: {e}")
        return 0

    missing = 0
    for item in serialized:
        c = by_ref.get(item.get("tracking_ref"))
        if not c:
            continue
        item["screener"] = c.get("screener")
        item["screener_signal"] = c.get("screener_signal")
        item["all_positive"] = c.get("all_positive")
        item["comparison"] = c.get("comparison")
        if not (c.get("screener") or {}).get("ok"):
            missing += 1
    return missing


@router.get("/pending-results")
def list_pending_results(
    status: Optional[str] = Query("pending"),
    hours: int = Query(24, ge=1, le=24 * 40),
    search: Optional[str] = Query(None),
    trade_date: Optional[str] = Query(None, description="YYYY-MM-DD; defaults to today IST"),
    db: Session = Depends(get_db),
):
    """
    Financial results awaiting an order decision.

    These are results that landed for a stock with no armed config, so the user
    is prompted to place an order. The AI earnings analysis is attached once it
    finishes.
    """
    from datetime import timedelta, timezone as _tz

    # Retire anything left from an earlier session before listing. Expiring is
    # about not acting on a stale print, not about hiding it — the rows stay
    # readable for the 30 days they are kept.
    try:
        from app.services.results_router import expire_stale_pending
        expire_stale_pending(db)
    except Exception:
        pass

    ist = _tz(timedelta(hours=5, minutes=30))
    day = trade_date or datetime.now(ist).strftime("%Y-%m-%d")

    q = db.query(PendingResultOrder)
    if day != "all":
        # The date IS the filter. Also applying a rolling `hours` window meant
        # asking for a day two weeks back returned whatever slice of it happened
        # to fall inside the last 24 hours — which for any past day is nothing,
        # so the picker looked broken rather than empty.
        q = q.filter(PendingResultOrder.trade_date == day)
    else:
        q = q.filter(PendingResultOrder.created_at >= datetime.utcnow() - timedelta(hours=hours))
    if status and status != "all":
        q = q.filter(PendingResultOrder.status == status)
    if search:
        pattern = f"%{search.strip()}%"
        q = q.filter(or_(
            PendingResultOrder.symbol.ilike(pattern),
            PendingResultOrder.company_name.ilike(pattern),
            PendingResultOrder.title.ilike(pattern),
            PendingResultOrder.tracking_ref.ilike(pattern),
        ))

    pendings = q.order_by(PendingResultOrder.created_at.desc()).all()

    log_ids = [p.ai_log_id for p in pendings if p.ai_log_id]
    logs = {}
    if log_ids:
        for log in db.query(TradeAILog).filter(TradeAILog.id.in_(log_ids)).all():
            logs[log.id] = log

    serialized = [_serialize_pending(p, logs.get(p.ai_log_id), db) for p in pendings]

    # The close recorded for this day, where one exists. Live quotes are only
    # fetched for today, so without this a finished day has nothing to show
    # even when its price is on file.
    if pendings:
        try:
            from app.database import DailyPrice
            syms = {p.symbol.upper() for p in pendings if p.symbol}
            stored = {d.symbol: d for d in db.query(DailyPrice).filter(
                DailyPrice.trade_date == day, DailyPrice.symbol.in_(syms)).all()} if syms else {}
            for item in serialized:
                d = stored.get((item.get("symbol") or "").upper())
                if d:
                    item["stored_price"] = {
                        "last": d.last_price, "prev_close": d.prev_close,
                        "change_pct": d.change_pct, "source": d.source,
                    }
        except Exception as e:
            logger.debug(f"Stored day prices unavailable: {e}")

    # Screener figures, so the panel carries what the digest used to show
    # separately. Anything not yet on the rows is fetched behind the request.
    missing = _attach_screener(db, pendings, serialized)
    if missing:
        _start_digest_build(day if day != "all" else datetime.now(ist).strftime("%Y-%m-%d"))

    return {
        "pending": serialized,
        "total": len(pendings),
        "building": bool(missing),
        "pending_screener": missing,
    }


@router.post("/pending-results/{pending_id}/order")
def place_pending_result_order(
    pending_id: int,
    body: PendingResultOrderRequest,
    db: Session = Depends(get_db),
):
    """
    Place a BUY order for a financial result that arrived on an unarmed stock.

    Creates the backing TradeConfig, places the order, and re-runs the earnings
    AI with the new config attached so the analysis is linked to the position.
    """
    pending = db.query(PendingResultOrder).filter(PendingResultOrder.id == pending_id).first()
    if not pending:
        raise HTTPException(status_code=404, detail="Pending result not found")
    if pending.status != "pending":
        raise HTTPException(status_code=400, detail=f"Already '{pending.status}'")

    from app.services.broker_gateway import get_broker

    config = TradeConfig(
        symbol=pending.symbol,
        instrument_key=pending.instrument_key,
        purchase_date=datetime.utcnow().strftime("%Y-%m-%d"),
        quantity=body.quantity,
        stoploss_pct=body.stoploss_pct,
        stoploss_type=body.stoploss_type,
        broker=body.broker,
        order_type=body.order_type,
        limit_price=body.limit_price,
        status="triggered",
        trigger_subject=pending.title[:200],
        notes=f"Created from financial result #{pending.id}",
        triggered_at=datetime.utcnow(),
    )
    # Outside trading hours the broker would reject this outright. The intent is
    # real, so it is queued for the next open exactly like a direct buy — the
    # filing that prompted it does not stop being a reason to buy at 15:31.
    from app.services.order_scheduler import schedule_or_now
    can_place, scheduled_for = schedule_or_now()
    if not can_place:
        config.status = "scheduled"
        config.scheduled_for = scheduled_for
        config.notes = f"Queued for the next market open — from financial result #{pending.id}"
        pending.status = "ordered"
        pending.config_id = config.id
        pending.resolved_at = datetime.utcnow()
        db.add(config)
        db.commit()
        db.refresh(config)
        logger.info(f"⏰ [SCHEDULED BUY] {pending.symbol} queued for {scheduled_for} UTC from result #{pending.id}")
        return {
            "success": True,
            "scheduled": True,
            "message": f"Market is closed — {pending.symbol} is queued for the next open (09:15 IST).",
            "pending": _serialize_pending(pending, None, db),
            "config": _serialize_config(config),
            "order": None,
        }

    db.add(config)
    db.commit()
    db.refresh(config)

    broker = get_broker(body.broker)
    result = broker.place_order(
        symbol=pending.symbol,
        instrument_key=pending.instrument_key or "",
        side="BUY",
        quantity=body.quantity,
        order_type=body.order_type,
        limit_price=body.limit_price,
        stoploss_type=body.stoploss_type,
        stoploss_pct=body.stoploss_pct,
    )

    order = TradeOrder(
        config_id=config.id,
        symbol=pending.symbol,
        side="BUY",
        quantity=body.quantity,
        order_type=body.order_type,
        limit_price=body.limit_price,
        price=result.price,
        broker=body.broker,
        broker_order_id=result.broker_order_id,
        broker_response=json.dumps(result.raw_response, default=str),
        status=result.status,
        error_message=result.message if not result.success else None,
    )
    db.add(order)

    if result.success:
        config.status = "bought"
        config.buy_price = result.price
        config.bought_at = datetime.utcnow()
        from app.services.holdings import mark_as_holding
        mark_as_holding(db, config.symbol, config.instrument_key or "", pending.company_name or "")
    else:
        config.notes = f"Order from result prompt failed: {result.message}"

    pending.status = "ordered"
    pending.config_id = config.id
    pending.resolved_at = datetime.utcnow()
    db.commit()
    db.refresh(config)

    # Re-run the earnings analysis now that a position exists to attach it to.
    try:
        from app.services.results_router import run_ai_for_pending
        run_ai_for_pending(pending.id, config_id=config.id)
    except Exception as e:
        logger.error(f"Failed to queue earnings AI for pending #{pending.id}: {e}")

    return {
        "success": result.success,
        "message": result.message,
        "pending": _serialize_pending(pending, None, db),
        "config": _serialize_config(config),
        "order": _serialize_order(order),
    }


@router.post("/pending-results/{pending_id}/pause")
def pause_pending_result(pending_id: int, db: Session = Depends(get_db)):
    """
    Stop (or resume) refreshing this row's live quote, without hiding the row.

    Dismissing is destructive in the only sense that matters on this panel: the
    card disappears, so quietening a price meant losing sight of the filing.
    Pausing leaves it listed with the last price it had, and the panel skips it
    when collecting symbols to quote — which is also what keeps a long results
    day inside the Upstox request budget.
    """
    pending = db.query(PendingResultOrder).filter(PendingResultOrder.id == pending_id).first()
    if not pending:
        raise HTTPException(status_code=404, detail="Pending result not found")

    pending.paused = not bool(getattr(pending, "paused", False))
    db.commit()
    return {"status": "success", "paused": bool(pending.paused),
            "pending": _serialize_pending(pending, None, db)}


@router.post("/pending-results/{pending_id}/reanalyse")
def reanalyse_pending_result(pending_id: int, body: ReanalyseRequest,
                             db: Session = Depends(get_db)):
    """
    Run the earnings AI over this filing again, optionally with another model.

    The verdict is not cleared first. A re-run that fails or returns NA would
    otherwise destroy the verdict that was already there, which is worse than
    the stale one it replaced.
    """
    pending = db.query(PendingResultOrder).filter(PendingResultOrder.id == pending_id).first()
    if not pending:
        raise HTTPException(status_code=404, detail="Pending result not found")

    flow = (body.flow or "ai").strip().lower()
    if flow == "ocr":
        raise HTTPException(
            status_code=501,
            detail="OCR extraction is not implemented yet — use the AI flow.",
        )
    if flow != "ai":
        raise HTTPException(status_code=400, detail=f"Unknown flow '{flow}'")

    if (getattr(pending, "kind", "result") or "result") != "result":
        raise HTTPException(
            status_code=400,
            detail=("This is impact news, not a results filing — there are no "
                    "quarterly figures to extract."),
        )

    from app.services.results_router import run_ai_for_pending
    started = run_ai_for_pending(pending.id, config_id=pending.config_id,
                                 model_override=(body.model or "").strip() or None)
    if not started:
        raise HTTPException(status_code=409, detail="Could not queue the analysis")

    pending.ai_status = "running"
    pending.ai_requested_at = datetime.utcnow()
    db.commit()
    return {"status": "success", "model": (body.model or "").strip() or "configured default",
            "pending": _serialize_pending(pending, None, db)}


@router.post("/pending-results/fetch-prices")
def fetch_prices_for_day(trade_date: Optional[str] = Query(None),
                         db: Session = Depends(get_db)):
    """
    Fetch whatever price is still recoverable for one day's prompts.

    On today that is the live quote: it fills a baseline for any filing that
    landed without one, and records the day's price. On a finished day it is
    that day's actual close from the daily candles — the baseline is gone, and
    is left alone rather than filled with a number from a different moment.
    """
    from datetime import timedelta as _td, timezone as _tz
    ist = _tz(_td(hours=5, minutes=30))
    day = trade_date or datetime.now(ist).strftime("%Y-%m-%d")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise HTTPException(status_code=400, detail="Date must be YYYY-MM-DD")

    from app.services.price_backfill import backfill
    return backfill(db, day)


@router.get("/pending-results/pdf")
def pending_results_pdf(trade_date: Optional[str] = Query(None), db: Session = Depends(get_db)):
    """
    The order-decision panel as a PDF, including how each stock has moved.

    The digest PDF is built from the same rows but knows nothing about prices —
    it is written at 08:00, before there are any. This one carries the baseline
    captured when the filing landed and the move since, which is the column the
    decision actually turns on.
    """
    import os
    import tempfile
    from datetime import timedelta, timezone as _tz
    from fastapi.responses import FileResponse
    from app.services.morning_digest import _build_rows
    from app.services.digest_pdf import build_digest_pdf

    ist = _tz(timedelta(hours=5, minutes=30))
    day = trade_date or datetime.now(ist).strftime("%Y-%m-%d")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise HTTPException(status_code=400, detail="Date must be YYYY-MM-DD")

    pendings = (db.query(PendingResultOrder)
                .filter(PendingResultOrder.trade_date == day)
                .order_by(PendingResultOrder.event_time.asc()).all())
    if not pendings:
        raise HTTPException(status_code=404, detail=f"No order prompts on {day}")

    rows = _build_rows(db, pendings, cache_only=True)

    # ── Prices ──
    #
    # Two figures, from two sources, for two different reasons.
    #
    # "Since result" is the move against the baseline captured from Upstox when
    # the filing landed, so it is only ever computed from an Upstox price. A
    # baseline from one venue against a last price from another is a move no
    # single tape ever showed, and it is the number an order gets placed on.
    #
    # The day's change is public information. Upstox answers it while the
    # market is open and the session is authorised; after the close, or with no
    # session at all, it is a settled number any source can report — so the
    # export falls back to the public feed rather than printing NA all down the
    # page for a day that has already finished.
    # Only today is priced. Every price source available here — Upstox, the
    # public feed — reports the price *now*, and "now" against a baseline
    # captured three weeks ago is not a move anyone made: it is today's quote
    # subtracted from an old number. Browsing a past day shows the baseline that
    # was captured then and nothing else, which is the honest answer.
    is_today = day == datetime.now(ist).strftime("%Y-%m-%d")

    quotes = {}
    upstox_ok = False
    keys = sorted({(p.instrument_key or "").replace(":", "|")
                   for p in pendings if p.instrument_key})
    keys = [k for k in keys if "|" in k]
    if is_today and keys:
        try:
            from app.main import get_active_feed
            quotes = get_active_feed().get_quotes(keys) or {}
            upstox_ok = bool(quotes)
        except Exception as e:
            # A PDF without live prices is still worth having; the baseline and
            # the figures do not depend on the feed being up.
            logger.warning(f"PDF Upstox lookup failed, falling back to public quotes: {e}")

    from app.services.results_router import is_within_action_window
    market_open_now = is_within_action_window()
    use_upstox_for_day = upstox_ok and market_open_now

    # A finished day cannot be quoted, but its close may be recorded. Reading
    # it here is what stops the export printing "prices are shown for the
    # current day only" over figures that are on file.
    closes = {}
    if not is_today:
        try:
            from app.database import DailyPrice
            syms = {p.symbol.upper() for p in pendings if p.symbol}
            if syms:
                closes = {d.symbol: d for d in db.query(DailyPrice).filter(
                    DailyPrice.trade_date == day, DailyPrice.symbol.in_(syms)).all()}
        except Exception as e:
            logger.debug(f"Stored closes unavailable for {day}: {e}")

    public = {}
    if is_today and not use_upstox_for_day:
        try:
            from app.services.public_quotes import fetch_day_changes
            public = fetch_day_changes([p.symbol for p in pendings])
        except Exception as e:
            logger.warning(f"PDF public quote lookup failed: {e}")

    # Hang the price block on each row. build_digest_pdf reads r["pending"] as
    # an object, so it takes _build_rows output rather than the serialised dicts.
    for r in rows:
        rp = r.get("pending")
        if rp is None:
            continue
        base = rp.price_at_announcement
        key = (rp.instrument_key or "").replace(":", "|")
        uq = quotes.get(key) or {}
        ltp = uq.get("last_price")

        day_pct = None
        day_src = None
        if use_upstox_for_day and ltp:
            prev = (uq.get("ohlc") or {}).get("close")
            if prev:
                day_pct = round((ltp - prev) / prev * 100, 2)
                day_src = "Upstox"
        if day_pct is None:
            pq = public.get((rp.symbol or "").upper()) or {}
            if pq.get("change_pct") is not None:
                day_pct = pq["change_pct"]
                day_src = "public feed"

        # On a finished day the recorded close stands in for the live quote.
        # "Since result" still cannot be computed — its baseline was never
        # captured for these rows — so it stays absent rather than being
        # measured from a price taken hours later.
        stored = closes.get((rp.symbol or "").upper()) if closes else None
        if stored is not None:
            r["is_result"] = (getattr(rp, "kind", "result") or "result") == "result"
            r["price"] = {
                "at_announcement": base,
                "last": stored.last_price,
                "since_pct": (round((stored.last_price - base) / base * 100, 2)
                              if base and stored.last_price else None),
                "upstox_connected": upstox_ok,
                "day_pct": stored.change_pct,
                "day_source": f"{stored.source or 'recorded'} close",
                "is_close": True,
                "paused": bool(getattr(rp, "paused", False)),
            }
            continue

        r["is_result"] = (getattr(rp, "kind", "result") or "result") == "result"
        r["price"] = {
            "at_announcement": base,
            "last": ltp,
            # Upstox only, deliberately — see the note above.
            "since_pct": (round((ltp - base) / base * 100, 2) if base and ltp else None),
            "upstox_connected": upstox_ok,
            "day_pct": day_pct,
            "day_source": day_src,
            "historical": not is_today,
            "paused": bool(getattr(rp, "paused", False)),
        }

    out = os.path.join(tempfile.gettempdir(), f"order_decisions_{day}.pdf")
    build_digest_pdf(rows, day, out, title=f"Order decisions {day}")
    return FileResponse(out, media_type="application/pdf",
                        filename=f"order-decisions-{day}.pdf")


@router.post("/pending-results/{pending_id}/dismiss")
def dismiss_pending_result(pending_id: int, db: Session = Depends(get_db)):
    """Dismiss a result prompt without placing an order."""
    pending = db.query(PendingResultOrder).filter(PendingResultOrder.id == pending_id).first()
    if not pending:
        raise HTTPException(status_code=404, detail="Pending result not found")

    pending.status = "dismissed"
    pending.resolved_at = datetime.utcnow()
    db.commit()
    return {"status": "success", "pending": _serialize_pending(pending, None, db)}


@router.post("/pending-results/{pending_id}/extract")
def extract_pending_result(pending_id: int, refresh: bool = Query(False),
                           db: Session = Depends(get_db)):
    """
    Read this filing's PDF and score the figures against Screener.

    Independent of the AI path by design: a different source read by different
    code, so agreement between the two is evidence rather than one answer
    repeated.

    Runs in the background. A text-layer read is a tenth of a second, but a
    filing whose statement page has to be found by OCR costs about a minute —
    measured at 64s on a live filing — and blocking a request for that is a
    timeout in front of the user and a tied-up worker on a 2-core box. The
    panel polls, so the result lands the way an AI verdict does.

    Cached on the row. A published quarter does not change, so re-reading the
    same filing spends a download and an OCR pass to get the same numbers;
    `refresh=true` forces it anyway.
    """
    pending = db.query(PendingResultOrder).filter(PendingResultOrder.id == pending_id).first()
    if not pending:
        raise HTTPException(status_code=404, detail="Pending result not found")
    if (getattr(pending, "kind", "result") or "result") != "result":
        raise HTTPException(
            status_code=400,
            detail="This is impact news, not a results filing — it has no quarterly figures.")
    if not pending.attachment_url:
        raise HTTPException(status_code=400, detail="This filing carries no PDF to read.")

    if not refresh and getattr(pending, "extraction_json", None):
        return json.loads(pending.extraction_json)

    from app.services.results_extractor import run_extraction_async
    run_extraction_async(pending.id)
    return {"status": "running",
            "confidence": {"tier": "READING", "pct": None, "flags": [],
                           "reason": "Reading the filing. A text layer is instant; "
                                     "a scan can take up to a minute to find the "
                                     "statement page."}}


@router.get("/extractor/capabilities")
def extractor_capabilities():
    """
    What this deployment can read, so the panel can say so rather than failing.

    OCR is an optional extra: without an engine installed the text layer still
    handles roughly two thirds of filings, and the rest report that they are
    scans rather than pretending to have been read.
    """
    from app.services.results_extractor import available
    return available()


@router.get("/quarterly-history/{symbol}")
def get_quarterly_history(symbol: str, quarters: int = Query(8, ge=4, le=20)):
    """
    Reported quarters for one symbol, each paired with the same quarter a year
    earlier — what the history chart under the price chart draws.

    Screener is the source rather than the filings: it carries every quarter
    already normalised to Rs crore, so year-on-year comes off two adjacent
    columns instead of two separately parsed PDFs. Served from the same
    six-hour cache the digest uses, so opening a chart does not spend a paced
    Screener request.
    """
    from app.services.screener_quarters import quarterly_history
    return quarterly_history(symbol, quarters=quarters)


# ─── Morning results digest ─────────────────────────────────────────────────

@router.get("/digest/{for_date}")
def get_digest_page(for_date: str):
    """
    Serve the dated digest page.

    Rebuilds on demand if the file is missing, so a link stays useful even if
    the container was replaced after it was sent.
    """
    import os
    from fastapi.responses import HTMLResponse
    from app.services.morning_digest import _DIGEST_DIR, build_and_publish

    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", for_date):
        raise HTTPException(status_code=400, detail="Date must be YYYY-MM-DD")

    path = os.path.join(_DIGEST_DIR, f"digest_{for_date}.html")
    if not os.path.exists(path):
        db = next(get_db())
        try:
            build_and_publish(db, for_date=for_date)
        finally:
            db.close()
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail=f"No digest for {for_date}")

    with open(path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@router.get("/digest")
def list_digests():
    """Available digest dates, newest first."""
    import os
    from app.services.morning_digest import _DIGEST_DIR

    if not os.path.isdir(_DIGEST_DIR):
        return {"digests": []}
    dates = sorted(
        (f[len("digest_"):-len(".html")] for f in os.listdir(_DIGEST_DIR)
         if f.startswith("digest_") and f.endswith(".html")),
        reverse=True,
    )
    return {"digests": [{"date": d, "url": f"/api/trading/digest/{d}"} for d in dates]}


_digest_builds: dict = {}
_digest_build_guard = threading.Lock()


def _start_digest_build(for_date: str) -> None:
    """
    Fetch the day's Screener figures in the background and save the result.

    Guarded per date: the panel polls, and every poll would otherwise start
    another walk of the same few hundred companies.
    """
    with _digest_build_guard:
        thread = _digest_builds.get(for_date)
        if thread and thread.is_alive():
            return

        def build():
            # Walks the day once, storing each company's Screener figures on its
            # row. Nothing is written to disk: the rows are the store, and the
            # next request for this date — or any view of these filings — reads
            # them without touching Screener again.
            from app.database import SessionLocal
            from app.services.morning_digest import _build_rows
            db = SessionLocal()
            try:
                pendings = (db.query(PendingResultOrder)
                            .filter(PendingResultOrder.trade_date == for_date)
                            .order_by(PendingResultOrder.event_time.asc()).all())
                if not pendings:
                    return
                rows = _build_rows(db, pendings)
                got = sum(1 for r in rows if (r.get("screener") or {}).get("ok"))
                logger.info(f"[DIGEST] Screener backfill for {for_date}: {got}/{len(rows)} companies")
            except Exception as e:
                logger.error(f"[DIGEST] Screener backfill failed for {for_date}: {e}")
            finally:
                db.close()

        th = threading.Thread(target=build, name=f"digest-build-{for_date}", daemon=True)
        _digest_builds[for_date] = th
        th.start()


@router.get("/digest/{for_date}/data")
def get_digest_data(for_date: str, limit: int = 40, db: Session = Depends(get_db)):
    """
    The digest as JSON, for the dashboard panel.

    Served from the file the 08:00 run wrote whenever there is one. Building it
    live walks Screener once per company at the pacing Screener demands, which
    on a heavy day is many minutes — long enough that the panel timed out and
    reported the day as having no results at all.

    A date with no saved file is built live but capped, so the panel answers
    quickly and says plainly that it is showing part of the day.
    """
    import os
    from app.services.morning_digest import (
        _DIGEST_DIR, _build_rows, collect_for_digest, serialize_digest_rows,
    )

    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", for_date):
        raise HTTPException(status_code=400, detail="Date must be YYYY-MM-DD")

    # Deliberately not reading digest_<date>.json here. That file is named for
    # the morning the digest ran, and covers the 08:00-to-08:00 window ending
    # that morning — so it never holds "results announced on <date>", which is
    # what this endpoint is asked for. Screener figures live on the rows now,
    # which makes rebuilding from the database cheap and correctly dated.

    # A past date reads back what was reported that day; today builds live.
    q = db.query(PendingResultOrder).filter(PendingResultOrder.trade_date == for_date)
    pendings = q.order_by(PendingResultOrder.event_time.asc()).all()
    if not pendings:
        pendings = [p for p in collect_for_digest(db)
                    if p.event_time and p.event_time.strftime("%Y-%m-%d") == for_date]

    # Answer from whatever Screener data is already in memory and fetch the rest
    # behind the request. Screener has to be called once per company and paced,
    # which is minutes for a heavy day — far past what a panel can wait for, and
    # waiting is what made it report the day as empty.
    rows = _build_rows(db, pendings, cache_only=True)
    companies = serialize_digest_rows(rows)
    missing = sum(1 for c in companies if not (c["screener"] or {}).get("ok"))
    if missing:
        _start_digest_build(for_date)

    return {
        "date": for_date,
        "total": len(companies),
        "analysed": sum(1 for r in companies if r["analysed"]),
        "companies": companies,
        # The panel can say "figures still arriving" rather than showing gaps
        # that look like missing data.
        "building": bool(missing),
        "pending_screener": missing,
    }


@router.get("/digest/{for_date}/pdf")
def get_digest_pdf(for_date: str):
    """Download the digest PDF, rebuilding it if the file is not on disk."""
    import os
    from fastapi.responses import FileResponse
    from app.services.morning_digest import _DIGEST_DIR, build_and_publish

    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", for_date):
        raise HTTPException(status_code=400, detail="Date must be YYYY-MM-DD")

    path = os.path.join(_DIGEST_DIR, f"digest_{for_date}.pdf")
    if not os.path.exists(path):
        db = next(get_db())
        try:
            build_and_publish(db, for_date=for_date)
        finally:
            db.close()
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail=f"No digest PDF for {for_date}")
    return FileResponse(path, media_type="application/pdf",
                        filename=f"results-digest-{for_date}.pdf")


@router.post("/digest/run")
def run_digest_now(
    analyse_first: bool = Query(False, description="Run any deferred analyses before building"),
    send_alert: bool = Query(False, description="Also send the Telegram summary"),
    db: Session = Depends(get_db),
):
    """Build the digest immediately, without waiting for 08:00 IST."""
    import os
    from app.services.morning_digest import (
        build_and_publish, run_deferred_analyses, send_digest_alert,
    )

    analysed = run_deferred_analyses(db) if analyse_first else 0
    result = build_and_publish(db)
    if send_alert:
        send_digest_alert(result, os.getenv("PUBLIC_BASE_URL", ""))

    return {
        "date": result["date"],
        "companies": result["count"],
        "analysed_now": analysed,
        "url": result["url_path"],
        "alert_sent": send_alert,
    }


@router.get("/settings")
def get_auto_trading_settings():
    """Get auto trading AI configuration."""
    from app.services.intel_config import get_intel_config
    cfg = get_intel_config()
    return {"settings": cfg.auto_trading_ai}


@router.post("/settings")
def update_auto_trading_settings(body: AutoTradingSettingsUpdate):
    """Save auto trading AI configuration (Custom REST API URL, OpenRouter key, Model)."""
    from app.services.intel_config import get_intel_config
    cfg = get_intel_config()
    cfg.update_auto_trading_ai(
        custom_api_url=body.custom_api_url,
        premium_openrouter_api_key=body.premium_openrouter_api_key,
        premium_openrouter_model=body.premium_openrouter_model
    )
    return {"message": "Auto-trading AI settings updated successfully", "settings": cfg.auto_trading_ai}

