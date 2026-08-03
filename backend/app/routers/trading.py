"""
Trading Engine API Router — CRUD for TradeConfigs, order execution, AI logs, and poller control.
"""
import json
import logging
from datetime import datetime
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import (
    get_db, TradeConfig, TradeOrder, TradeAILog,
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
    return {
        "id": a.id,
        "config_id": a.config_id,
        "symbol": a.symbol,
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
    db: Session = Depends(get_db)
):
    """List all trade configurations."""
    q = db.query(TradeConfig).order_by(TradeConfig.created_at.desc())
    if status:
        q = q.filter(TradeConfig.status == status)
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
    page: int = 1,
    page_size: int = 50,
    db: Session = Depends(get_db),
):
    """List premium AI analysis logs (deduplicated by symbol + nse_event_title)."""
    q = db.query(TradeAILog).order_by(TradeAILog.created_at.desc())
    if config_id:
        q = q.filter(TradeAILog.config_id == config_id)
    if symbol:
        q = q.filter(TradeAILog.symbol == symbol.upper())
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

