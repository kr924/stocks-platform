"""
Trading Engine API Router — CRUD for TradeConfigs, order execution, AI logs, and poller control.
"""
import json
import logging
import re
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

def _serialize_pending(p: PendingResultOrder, ai_log: Optional[TradeAILog] = None) -> dict:
    return {
        "id": p.id,
        "symbol": p.symbol,
        "company_name": p.company_name,
        "tracking_ref": p.tracking_ref,
        "trade_date": p.trade_date,
        "deferred": bool(p.deferred),
        # Lifecycle: announced -> ingested -> alerted -> AI sent -> AI received
        "announced_at": p.event_time.isoformat() if p.event_time else None,
        "ingested_at": p.created_at.isoformat() if p.created_at else None,
        "alert_sent_at": p.alert_sent_at.isoformat() if p.alert_sent_at else None,
        "ai_requested_at": p.ai_requested_at.isoformat() if p.ai_requested_at else None,
        "ai_completed_at": p.ai_completed_at.isoformat() if p.ai_completed_at else None,
        "instrument_key": p.instrument_key,
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
    }


@router.get("/pending-results")
def list_pending_results(
    status: Optional[str] = Query("pending"),
    hours: int = Query(24, ge=1, le=168),
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

    # Retire anything left from an earlier session before listing.
    try:
        from app.services.results_router import expire_stale_pending
        expire_stale_pending(db)
    except Exception:
        pass

    ist = _tz(timedelta(hours=5, minutes=30))
    day = trade_date or datetime.now(ist).strftime("%Y-%m-%d")

    since = datetime.utcnow() - timedelta(hours=hours)
    q = db.query(PendingResultOrder).filter(PendingResultOrder.created_at >= since)
    if status and status != "all":
        q = q.filter(PendingResultOrder.status == status)
    if day != "all":
        q = q.filter(PendingResultOrder.trade_date == day)
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

    return {
        "pending": [_serialize_pending(p, logs.get(p.ai_log_id)) for p in pendings],
        "total": len(pendings),
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
        "pending": _serialize_pending(pending),
        "config": _serialize_config(config),
        "order": _serialize_order(order),
    }


@router.post("/pending-results/{pending_id}/dismiss")
def dismiss_pending_result(pending_id: int, db: Session = Depends(get_db)):
    """Dismiss a result prompt without placing an order."""
    pending = db.query(PendingResultOrder).filter(PendingResultOrder.id == pending_id).first()
    if not pending:
        raise HTTPException(status_code=404, detail="Pending result not found")

    pending.status = "dismissed"
    pending.resolved_at = datetime.utcnow()
    db.commit()
    return {"status": "success", "pending": _serialize_pending(pending)}


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


@router.get("/digest/{for_date}/data")
def get_digest_data(for_date: str, db: Session = Depends(get_db)):
    """
    The digest as JSON, for the dashboard panel.

    Serves the same rows the page and the PDF are built from, so the three views
    cannot drift apart.
    """
    import json as _json
    from app.services.morning_digest import _build_rows, collect_for_digest

    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", for_date):
        raise HTTPException(status_code=400, detail="Date must be YYYY-MM-DD")

    # A past date reads back what was reported that day; today builds live.
    q = db.query(PendingResultOrder).filter(PendingResultOrder.trade_date == for_date)
    pendings = q.order_by(PendingResultOrder.event_time.asc()).all()
    if not pendings:
        pendings = collect_for_digest(db)
        pendings = [p for p in pendings
                    if p.event_time and p.event_time.strftime("%Y-%m-%d") == for_date]

    rows = _build_rows(db, pendings)
    out = []
    for r in rows:
        p, log = r["pending"], r.get("log")
        out.append({
            "symbol": p.symbol,
            "company_name": p.company_name,
            "exchange": p.exchange,
            "tracking_ref": p.tracking_ref,
            "announced_at": p.event_time.isoformat() if p.event_time else None,
            "ingested_at": p.created_at.isoformat() if p.created_at else None,
            "ai_requested_at": p.ai_requested_at.isoformat() if p.ai_requested_at else None,
            "ai_completed_at": p.ai_completed_at.isoformat() if p.ai_completed_at else None,
            "deferred": bool(p.deferred),
            "analysed": r["analysed"],
            "verdict": r["verdict"],
            "title": p.title,
            "attachment_url": p.attachment_url,
            "screener": {
                "ok": r["screener"].get("ok"),
                "quarter": r["screener"].get("quarter"),
                "source_url": r["screener"].get("source_url"),
                "error": r["screener"].get("error"),
            },
            "comparison": r["comparison"],
            "validation": r.get("validation"),
            "ai_summary": log.ai_summary if log else None,
            "future_growth_outlook": log.future_growth_outlook if log else None,
            "future_projected_numbers": log.future_projected_numbers if log else None,
            "broker_estimates": log.broker_estimates if log else None,
        })
    return {
        "date": for_date,
        "total": len(out),
        "analysed": sum(1 for r in out if r["analysed"]),
        "companies": out,
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

