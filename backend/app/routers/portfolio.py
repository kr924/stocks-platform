"""
Portfolio ledger API: Upstox and Zerodha trades in one place, holdings,
realised gains and the capital-gains summary (services/portfolio.py).
"""
import json
import logging
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db, PortfolioTrade, SystemSetting
from app.services import portfolio as P

logger = logging.getLogger("app.portfolio_router")
router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])

MAX_UPLOAD_CHARS = 20_000_000


def _broker(b: Optional[str]) -> Optional[str]:
    b = (b or "").strip().lower()
    return b if b in P.BROKERS else None


@router.get("/summary")
def summary(db: Session = Depends(get_db)):
    counts = {f"{b}:{s}": n for b, s, n in
              db.query(PortfolioTrade.broker, PortfolioTrade.source, func.count(PortfolioTrade.id))
              .group_by(PortfolioTrade.broker, PortfolioTrade.source).all()}
    span = {b: {"first": f, "last": l} for b, f, l in
            db.query(PortfolioTrade.broker, func.min(PortfolioTrade.trade_date), func.max(PortfolioTrade.trade_date))
            .group_by(PortfolioTrade.broker).all()}
    last_sync = None
    row = db.query(SystemSetting).filter(SystemSetting.key == "portfolio_upstox_sync").first()
    if row:
        try:
            last_sync = json.loads(row.value)
        except Exception:
            pass
    return {"counts": counts, "span": span, "financial_years": P.financial_years(db),
            "current_fy": P.fy_of(P._today()), "upstox_sync": last_sync}


@router.get("/holdings")
def holdings(broker: Optional[str] = None, db: Session = Depends(get_db)):
    return P.holdings(db, _broker(broker))


@router.get("/tax")
def tax(fy: Optional[str] = None, broker: Optional[str] = None, db: Session = Depends(get_db)):
    fy = fy or P.fy_of(P._today())
    if len(fy) != 7 or fy[4] != "-":
        raise HTTPException(400, "fy looks like 2026-27")
    return P.tax_summary(db, fy, _broker(broker))


@router.get("/realised.csv")
def realised_csv(fy: str, broker: Optional[str] = None, db: Session = Depends(get_db)):
    body = P.realised_csv(db, fy, _broker(broker))
    name = f"capital_gains_FY{fy}{'_' + broker if _broker(broker) else ''}.csv"
    return Response(content=body, media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


class ImportBody(BaseModel):
    broker: str
    filename: Optional[str] = None
    content: str


@router.post("/import")
def import_csv(body: ImportBody, db: Session = Depends(get_db)):
    """A tradebook CSV, read in the browser and sent as text."""
    if len(body.content or "") > MAX_UPLOAD_CHARS:
        raise HTTPException(413, "File too large")
    if (body.filename or "").lower().endswith((".xlsx", ".xls")):
        raise HTTPException(400, "Download the tradebook as CSV, not Excel (Console offers both).")
    try:
        parsed = P.parse_csv(body.content, body.broker)
    except ValueError as e:
        raise HTTPException(400, str(e))
    res = P.store(db, parsed["rows"])
    synced = {}
    if res["added"]:
        try:
            synced = P.sync_watchlist(db)
        except Exception as e:
            logger.warning(f"[PORTFOLIO] watchlist sync after import failed: {e}")
    logger.info(f"[PORTFOLIO] import {body.broker} {body.filename}: {res['added']} added, "
                f"{res['duplicates']} already on file, {parsed['skipped']} non-equity skipped")
    return {**res, "read": len(parsed["rows"]), "skipped_non_equity": parsed["skipped"],
            "errors": parsed["errors"], "watchlist": synced}


@router.post("/sync/upstox")
def sync_upstox(db: Session = Depends(get_db)):
    try:
        return P.sync_upstox(db)
    except Exception as e:
        raise HTTPException(502, str(e))


@router.post("/sync-watchlist")
def sync_watchlist(db: Session = Depends(get_db)):
    return P.sync_watchlist(db)


@router.get("/trades")
def trades(broker: Optional[str] = None, symbol: Optional[str] = None, limit: int = 300,
           db: Session = Depends(get_db)):
    q = db.query(PortfolioTrade)
    if _broker(broker):
        q = q.filter(PortfolioTrade.broker == _broker(broker))
    if symbol:
        s = symbol.strip().upper()
        q = q.filter((PortfolioTrade.symbol == s) | (PortfolioTrade.isin == s))
    rows = q.order_by(PortfolioTrade.trade_date.desc(), PortfolioTrade.id.desc()).limit(max(1, min(limit, 2000))).all()
    return [{"id": t.id, "broker": t.broker, "symbol": t.symbol, "isin": t.isin, "exchange": t.exchange,
             "trade_date": t.trade_date, "trade_time": t.trade_time, "side": t.side,
             "quantity": t.quantity, "price": t.price, "trade_id": t.trade_id, "source": t.source,
             "note": t.note} for t in rows]


class ManualTrade(BaseModel):
    broker: str
    symbol: str
    isin: Optional[str] = None
    trade_date: str
    side: str
    quantity: float
    price: float
    note: Optional[str] = None


@router.post("/trades")
def add_trade(body: ManualTrade, db: Session = Depends(get_db)):
    """By hand: an IPO allotment, bonus shares (price 0), or history older than the files."""
    broker = _broker(body.broker)
    side = (body.side or "").upper()
    parsed = P.parse_date(body.trade_date)
    if not broker or side not in ("BUY", "SELL") or not parsed or body.quantity <= 0 or body.price < 0:
        raise HTTPException(400, "broker, a date, BUY/SELL, quantity > 0 and price >= 0 are needed")
    sym = body.symbol.strip().upper()
    if not sym:
        raise HTTPException(400, "symbol is needed")
    row = {"uid": f"manual:{uuid.uuid4().hex}", "broker": broker, "symbol": sym,
           "isin": (body.isin or "").strip().upper() or None, "exchange": None, "instrument_key": None,
           "trade_date": parsed[0], "trade_time": None, "side": side, "quantity": body.quantity,
           "price": body.price, "trade_id": None, "order_id": None, "source": "manual",
           "note": (body.note or "")[:200] or None}
    P.store(db, [row])
    P.sync_watchlist(db)
    return {"status": "ok"}


@router.delete("/trades/{trade_id}")
def delete_trade(trade_id: int, db: Session = Depends(get_db)):
    t = db.query(PortfolioTrade).filter(PortfolioTrade.id == trade_id).first()
    if not t:
        raise HTTPException(404, "not found")
    db.delete(t)
    db.commit()
    return {"status": "ok"}
