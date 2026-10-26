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

from app.database import get_db, PortfolioTrade, PortfolioAccount, SystemSetting
from app.services import portfolio as P

logger = logging.getLogger("app.portfolio_router")
router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])

MAX_UPLOAD_CHARS = 20_000_000


def _broker(b: Optional[str]) -> Optional[str]:
    b = (b or "").strip().lower()
    return b if b in P.BROKERS else None


def _name(n: Optional[str]) -> Optional[str]:
    return (n or "").strip() or None


def _accounts(db) -> list:
    """Every known client ID with its tracking name and trade count, plus untagged trades per broker."""
    counts = {a: n for a, n in db.query(PortfolioTrade.account_id, func.count(PortfolioTrade.id))
              .group_by(PortfolioTrade.account_id).all()}
    return [{"client_id": a.client_id, "broker": a.broker, "name": a.name, "trades": counts.get(a.client_id, 0)}
            for a in db.query(PortfolioAccount).order_by(PortfolioAccount.name, PortfolioAccount.client_id).all()]


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
    unassigned = {b: n for b, n in db.query(PortfolioTrade.broker, func.count(PortfolioTrade.id))
                  .filter(PortfolioTrade.account_id.is_(None)).group_by(PortfolioTrade.broker).all()}
    return {"counts": counts, "span": span, "financial_years": P.financial_years(db),
            "current_fy": P.fy_of(P._today()), "upstox_sync": last_sync,
            "accounts": _accounts(db), "names": P.tracking_names(db),
            "upstox_account": P.upstox_account(db), "unassigned": unassigned}


@router.get("/holdings")
def holdings(broker: Optional[str] = None, name: Optional[str] = None, db: Session = Depends(get_db)):
    return P.holdings(db, _broker(broker), name=_name(name))


@router.get("/tax")
def tax(fy: Optional[str] = None, broker: Optional[str] = None, name: Optional[str] = None,
        db: Session = Depends(get_db)):
    fy = fy or P.fy_of(P._today())
    if len(fy) != 7 or fy[4] != "-":
        raise HTTPException(400, "fy looks like 2026-27")
    return P.tax_summary(db, fy, _broker(broker), _name(name))


@router.get("/realised.csv")
def realised_csv(fy: str, broker: Optional[str] = None, name: Optional[str] = None,
                 db: Session = Depends(get_db)):
    import re
    body = P.realised_csv(db, fy, _broker(broker), _name(name))
    who = re.sub(r"[^A-Za-z0-9]+", "_", _name(name) or "")
    name = f"capital_gains_FY{fy}{'_' + who if who else ''}{'_' + broker if _broker(broker) else ''}.csv"
    return Response(content=body, media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


class ImportBody(BaseModel):
    broker: str
    filename: Optional[str] = None
    content: str
    account_id: Optional[str] = None


@router.post("/import")
def import_csv(body: ImportBody, db: Session = Depends(get_db)):
    """A tradebook CSV, read in the browser and sent as text."""
    if len(body.content or "") > MAX_UPLOAD_CHARS:
        raise HTTPException(413, "File too large")
    if (body.filename or "").lower().endswith((".xlsx", ".xls")):
        raise HTTPException(400, "Download the tradebook as CSV, not Excel (Console offers both).")
    broker = _broker(body.broker)
    if not broker:
        raise HTTPException(400, "broker must be upstox or zerodha")
    # The client ID: from Zerodha's file name ("tradebook-QY8357-EQ.csv"), a
    # "Client ID" line in the file, or the account picked on the page. The
    # connected Upstox account is the default for an Upstox file.
    acct = (P.client_id_from(body.filename or "", body.content[:3000])
            or (body.account_id or "").strip().upper()
            or (P.upstox_account(db) if broker == "upstox" else None))
    if not acct:
        raise HTTPException(400, "The client ID is not in the file name (Zerodha names it like "
                                 "tradebook-QY8357-EQ.csv). Pick the account it belongs to and import again.")
    P.ensure_account(db, acct, broker)
    try:
        parsed = P.parse_csv(body.content, broker, acct)
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
            "errors": parsed["errors"], "watchlist": synced, "account_id": acct}


# ─── Accounts: client IDs and their tracking names ──────────────────────────

@router.get("/accounts")
def list_accounts(db: Session = Depends(get_db)):
    return {"accounts": _accounts(db), "names": P.tracking_names(db), "upstox_account": P.upstox_account(db)}


class AccountBody(BaseModel):
    client_id: str
    broker: str
    name: str


@router.post("/accounts")
def save_account(body: AccountBody, db: Session = Depends(get_db)):
    """Add a client ID or rename it. Several IDs can share a tracking name."""
    cid = (body.client_id or "").strip().upper()
    name = (body.name or "").strip()[:60]
    broker = _broker(body.broker)
    if not cid or not name or not broker:
        raise HTTPException(400, "client ID, broker and tracking name are needed")
    a = db.query(PortfolioAccount).filter(PortfolioAccount.client_id == cid).first()
    if a:
        a.name, a.broker = name, broker
    else:
        db.add(PortfolioAccount(client_id=cid, broker=broker, name=name))
    db.commit()
    return {"accounts": _accounts(db), "names": P.tracking_names(db)}


@router.delete("/accounts/{client_id}")
def delete_account(client_id: str, db: Session = Depends(get_db)):
    cid = client_id.strip().upper()
    n = db.query(PortfolioTrade.id).filter(PortfolioTrade.account_id == cid).count()
    if n:
        raise HTTPException(400, f"{cid} has {n} trades on file — rename it instead, or delete its trades first")
    db.query(PortfolioAccount).filter(PortfolioAccount.client_id == cid).delete()
    db.commit()
    return {"accounts": _accounts(db), "names": P.tracking_names(db)}


class AssignBody(BaseModel):
    broker: str
    client_id: str


@router.post("/accounts/assign")
def assign_untagged(body: AssignBody, db: Session = Depends(get_db)):
    """Give every trade of this broker that has no client ID yet to this account (imports made before IDs)."""
    broker = _broker(body.broker)
    cid = (body.client_id or "").strip().upper()
    if not broker or not cid:
        raise HTTPException(400, "broker and client ID are needed")
    P.ensure_account(db, cid, broker)
    n = (db.query(PortfolioTrade).filter(PortfolioTrade.broker == broker, PortfolioTrade.account_id.is_(None))
         .update({PortfolioTrade.account_id: cid}, synchronize_session=False))
    db.commit()
    return {"assigned": n, "accounts": _accounts(db), "names": P.tracking_names(db)}


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
def trades(broker: Optional[str] = None, symbol: Optional[str] = None, name: Optional[str] = None,
           limit: int = 300, db: Session = Depends(get_db)):
    q = db.query(PortfolioTrade)
    if _broker(broker):
        q = q.filter(PortfolioTrade.broker == _broker(broker))
    amap = P.accounts_map(db)
    acct_of = P.acct_resolver(db)
    if _name(name):
        ids = [c for c, a in amap.items() if a["name"] == _name(name)]
        if _name(name) == P.UNASSIGNED:
            q = q.filter(PortfolioTrade.account_id.is_(None), PortfolioTrade.broker != "upstox")
        else:
            ua = P.upstox_account(db)
            cond = PortfolioTrade.account_id.in_(ids or ["-"])
            if ua in ids:
                cond = cond | ((PortfolioTrade.broker == "upstox") & PortfolioTrade.account_id.is_(None))
            q = q.filter(cond)
    if symbol:
        s = symbol.strip().upper()
        q = q.filter((PortfolioTrade.symbol == s) | (PortfolioTrade.isin == s))
    rows = q.order_by(PortfolioTrade.trade_date.desc(), PortfolioTrade.id.desc()).limit(max(1, min(limit, 2000))).all()
    return [{"id": t.id, "broker": t.broker, "symbol": t.symbol, "isin": t.isin, "exchange": t.exchange,
             "trade_date": t.trade_date, "trade_time": t.trade_time, "side": t.side,
             "quantity": t.quantity, "price": t.price, "trade_id": t.trade_id, "source": t.source,
             "note": t.note, "account": acct_of(t) or None, "name": P.name_of(amap, acct_of(t))} for t in rows]


class ManualTrade(BaseModel):
    broker: str
    account_id: Optional[str] = None
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
    acct = (body.account_id or "").strip().upper() or (P.upstox_account(db) if broker == "upstox" else None)
    if not acct:
        ids = [c for c, a in P.accounts_map(db).items() if a["broker"] == broker]
        acct = ids[0] if len(ids) == 1 else None
    if not acct:
        raise HTTPException(400, "Pick the account (client ID) this trade belongs to")
    P.ensure_account(db, acct, broker)
    row = {"uid": f"manual:{uuid.uuid4().hex}", "broker": broker, "account_id": acct, "symbol": sym,
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


# ─── For the Stocks Tracker's Holdings tab ──────────────────────────────────

@router.get("/positions")
def positions(db: Session = Depends(get_db)):
    """
    What is held now, per stock: quantity, average price, amount invested and
    the dated lots (days held), per broker. Upstox's holdings are re-read at
    most every 10 minutes; when they changed, the tracker's H / U / Z chips
    are brought in line.
    """
    pos = P.positions(db)
    if pos["changed"]:
        try:
            P.sync_watchlist(db, pos)
        except Exception as e:
            logger.warning(f"[PORTFOLIO] watchlist sync failed: {e}")
    rows = []
    for p in pos["positions"]:
        rows.append({**{k: p[k] for k in ("key", "symbol", "isin", "instrument_key", "qty", "avg", "invested",
                                          "broker", "lots", "names")},
                     "holders": [{"broker": h["broker"], "account": h["account"], "name": h["name"],
                                  "qty": h["qty"], "avg": h["avg"], "invested": h["invested"]} for h in p["holders"]],
                     "brokers": {b: {"qty": v["qty"], "avg": v["avg"], "invested": v["invested"],
                                     "source": v["source"]} for b, v in p["brokers"].items()}})
    return {"positions": rows, "names": P.tracking_names(db), "upstox_snapshot_at": pos["upstox_snapshot_at"],
            "upstox_error": pos["upstox_error"]}


class SellBody(BaseModel):
    symbol: str
    quantity: int
    order_type: str = "MARKET"
    limit_price: Optional[float] = None


@router.post("/sell")
def sell_holding(body: SellBody, db: Session = Depends(get_db)):
    """
    Sell shares held at Upstox, from the tracker. A delivery (CNC) SELL
    through the same gateway the trading engine uses. Positions the engine
    bought and is still watching for its stoploss are closed (oldest first,
    up to the quantity sold), so the watcher does not try to sell them again.
    """
    import threading
    from app.database import TradeConfig, TradeOrder
    from app.services.broker_gateway import get_broker

    sym = (body.symbol or "").strip().upper()
    pos = P.positions(db)
    p = next((x for x in pos["positions"] if (x.get("symbol") or "").upper() == sym), None)
    up = (p or {}).get("brokers", {}).get("upstox")
    if not up:
        raise HTTPException(400, f"{sym} is not held at Upstox")
    held = int(round(up["qty"]))
    if body.quantity < 1 or body.quantity > held:
        raise HTTPException(400, f"Quantity must be between 1 and {held}")
    ot = (body.order_type or "MARKET").upper()
    if ot not in ("MARKET", "LIMIT"):
        raise HTTPException(400, "order_type is MARKET or LIMIT")
    if ot == "LIMIT" and not (body.limit_price and body.limit_price > 0):
        raise HTTPException(400, "A limit order needs a price")
    key = (p.get("instrument_key") or "").replace(":", "|")
    if "|" not in key:
        raise HTTPException(400, f"No Upstox instrument key for {sym}")

    result = get_broker("upstox").place_order(symbol=sym, instrument_key=key, side="SELL",
                                              quantity=body.quantity, order_type=ot,
                                              limit_price=body.limit_price if ot == "LIMIT" else None)
    db.add(TradeOrder(config_id=0, symbol=sym, side="SELL", quantity=body.quantity, order_type=ot,
                      limit_price=body.limit_price if ot == "LIMIT" else None, price=result.price,
                      broker="upstox", broker_order_id=result.broker_order_id,
                      broker_response=json.dumps(result.raw_response, default=str), status=result.status,
                      error_message=None if result.success else result.message))
    closed = []
    if result.success:
        left = body.quantity
        configs = (db.query(TradeConfig)
                   .filter(TradeConfig.symbol == sym, TradeConfig.status == "bought",
                           TradeConfig.broker == "upstox")
                   .order_by(TradeConfig.bought_at.asc()).all())
        for cfg in configs:
            if left <= 0:
                break
            note = f"Sold {min(left, cfg.quantity)} from Stocks Tracker holdings (order {result.broker_order_id})"
            if cfg.quantity <= left:
                left -= cfg.quantity
                cfg.status = "sold"
                cfg.sold_at = datetime.utcnow()
                cfg.notes = note
                closed.append(cfg.id)
            else:
                cfg.quantity -= left
                cfg.notes = note + f"; {cfg.quantity} still held"
                left = 0
    db.commit()
    logger.info(f"[PORTFOLIO] sell {body.quantity} {sym} ({ot}): {result.status} {result.message}")

    if result.success:
        # Pick the fill up into the ledger and the holdings once it has executed.
        def _later():
            import time
            from app.database import SessionLocal
            for wait in (8, 60):
                time.sleep(wait)
                s = SessionLocal()
                try:
                    P.sync_upstox(s, years=0)
                except Exception as e:
                    logger.info(f"[PORTFOLIO] post-sell sync: {e}")
                finally:
                    s.close()
        threading.Thread(target=_later, daemon=True, name=f"post-sell-{sym}").start()
    return {"success": result.success, "status": result.status, "message": result.message,
            "order_id": result.broker_order_id, "closed_configs": closed}
