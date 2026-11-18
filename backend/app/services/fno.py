"""
Futures and options: trades, positions, profit, charges and the tax view.

- **Sources.** Zerodha's F&O tradebook CSV (Console → Reports → Tradebook →
  Futures & Options) through the same import as the equity tradebook — rows
  whose segment is F&O land here instead of being skipped — and Upstox's
  trade history with segment FO. Commodity and currency rows are still skipped.
- **Contracts.** The trading symbol carries the contract: NIFTY24AUGFUT,
  NIFTY24AUG24000CE (monthly), NIFTY2481524000CE (weekly: yy, month 1-9/O/N/D,
  day). The expiry from the source wins when it has one; a monthly contract
  without one gets the last Tuesday (NSE from Sep 2025) or Thursday (NSE before,
  BSE), marked estimated.
- **Matching.** Per account and contract, FIFO, either direction: a sell with
  nothing long opens a short. Quantities are units, not lots.
- **Expiry.** A position still open after its expiry settles there: at the
  price entered for it, else an option at 0 (expired worthless — flagged, since
  an in-the-money option did not), and a future stays unsettled until a price
  is entered.
- **Charges are estimated** from the published rates (dated): STT on the sell
  (futures 0.0125% → 0.02% from 1 Oct 2024 → 0.05% from 1 Apr 2026; options
  premium 0.0625% → 0.1% → 0.15%), exchange charges (NSE futures 0.0019% →
  0.00173%, options 0.0495% → 0.03503% of premium from 1 Oct 2024; BSE options
  0.0325%), stamp duty on the buy (futures 0.002%, options 0.003%), SEBI ₹10 a
  crore, GST 18% on brokerage + exchange + SEBI, brokerage ₹20 an order (Zerodha
  futures 0.03% if less). For F&O, being business income, every charge — STT
  included — is an expense.
- **Tax.** Non-speculative business income at the slab rate. Turnover is the
  sum of each closed trade's absolute profit or loss (ICAI guidance note, 2022),
  which decides whether a tax audit applies.
"""
import json
import logging
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("app.fno")

IST = timezone(timedelta(hours=5, minutes=30))
FNO_SEGMENTS = {"FO", "F&O", "FNO", "NFO", "BFO", "NSE_FO", "BSE_FO", "NFO-FO", "BFO-FO", "FUT", "OPT", "DERIVATIVES"}
SETTLEMENTS_KEY = "fno_settlements"
EPS = 1e-9
_MON = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}
_WEEK_MON = {**{str(i): i for i in range(1, 10)}, "O": 10, "N": 11, "D": 12}
_MONTHLY = re.compile(r"^(?P<u>[A-Z0-9&\-]+?)(?P<yy>\d{2})(?P<mon>JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)"
                      r"(?P<strike>\d+(?:\.\d+)?)?(?P<t>FUT|CE|PE)$")
_WEEKLY = re.compile(r"^(?P<u>[A-Z&\-]+?)(?P<yy>\d{2})(?P<m>[1-9OND])(?P<dd>\d{2})(?P<strike>\d+(?:\.\d+)?)(?P<t>CE|PE)$")


def _today() -> date:
    return datetime.now(IST).date()


def _d(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def is_fno_segment(seg: str, exchange: str = "") -> bool:
    s = (seg or "").strip().upper()
    e = (exchange or "").strip().upper()
    return s in FNO_SEGMENTS or e in ("NFO", "BFO")


def _last_weekday(y: int, m: int, weekday: int) -> date:
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    d = nxt - timedelta(days=1)
    while d.weekday() != weekday:
        d -= timedelta(days=1)
    return d


def parse_contract(symbol: str, exchange: str = "", expiry: Optional[str] = None,
                   strike: Optional[float] = None, kind: Optional[str] = None) -> dict:
    """Underlying, FUT/CE/PE, expiry and strike from the symbol, with whatever the source states preferred."""
    s = re.sub(r"\s+", "", (symbol or "").upper())
    out = {"underlying": None, "instrument": (kind or "").upper() or None, "expiry": expiry, "strike": strike,
           "expiry_estimated": False}
    m = _WEEKLY.match(s)
    if m:
        out["underlying"] = m.group("u")
        out["instrument"] = out["instrument"] or m.group("t")
        out["strike"] = out["strike"] or float(m.group("strike"))
        if not out["expiry"]:
            try:
                out["expiry"] = date(2000 + int(m.group("yy")), _WEEK_MON[m.group("m")], int(m.group("dd"))).isoformat()
            except ValueError:
                pass
        return out
    m = _MONTHLY.match(s)
    if m:
        out["underlying"] = m.group("u")
        out["instrument"] = out["instrument"] or m.group("t")
        if m.group("strike") and not out["strike"]:
            out["strike"] = float(m.group("strike"))
        if not out["expiry"]:
            y, mo = 2000 + int(m.group("yy")), _MON[m.group("mon")]
            bse = (exchange or "").upper().startswith("B")
            weekday = 3 if bse or date(y, mo, 1) < date(2025, 9, 1) else 1     # Thu / Tue
            out["expiry"] = _last_weekday(y, mo, weekday).isoformat()
            out["expiry_estimated"] = True
        return out
    # Upstox-style "NIFTY 24000 CE 29 AUG 24" or anything else: first word is the underlying.
    words = (symbol or "").upper().split()
    out["underlying"] = words[0] if words else s[:20]
    if not out["instrument"]:
        out["instrument"] = "CE" if " CE" in f" {symbol.upper()} " else "PE" if " PE" in f" {symbol.upper()} " else "FUT"
    return out


def _uid(broker, exchange, trade_id, side, fallback) -> str:
    if trade_id:
        return f"fno:{broker}:{(exchange or '').upper()}:{trade_id}:{side}"
    return f"fno:{broker}:noid:{fallback}"


def row_from(broker: str, account_id: Optional[str], symbol: str, exchange: Optional[str], day: str,
             tm: Optional[str], side: str, qty: float, price: float, trade_id: Optional[str],
             order_id: Optional[str], source: str, fallback: str, expiry: Optional[str] = None,
             strike: Optional[float] = None, kind: Optional[str] = None,
             instrument_key: Optional[str] = None) -> dict:
    exch = (exchange or "").upper() or None
    if exch in ("NSE", "NSE_FO"):
        exch = "NFO"
    elif exch in ("BSE", "BSE_FO"):
        exch = "BFO"
    c = parse_contract(symbol, exch or "", expiry, strike, kind)
    return {"uid": _uid(broker, exch, trade_id, side, fallback), "broker": broker, "account_id": account_id,
            "symbol": symbol.upper().strip(), "underlying": c["underlying"], "instrument": c["instrument"],
            "expiry": c["expiry"], "expiry_estimated": c["expiry_estimated"], "strike": c["strike"],
            "exchange": exch, "instrument_key": instrument_key, "trade_date": day, "trade_time": tm,
            "side": side, "quantity": abs(qty), "price": abs(price), "trade_id": trade_id,
            "order_id": order_id, "source": source}


def store(db, rows: List[dict]) -> dict:
    from app.database import FnoTrade
    if not rows:
        return {"added": 0, "duplicates": 0}
    have = {u for (u,) in db.query(FnoTrade.uid).filter(FnoTrade.uid.in_([r["uid"] for r in rows])).all()}
    added = 0
    for r in rows:
        if r["uid"] in have:
            continue
        have.add(r["uid"])
        db.add(FnoTrade(**r))
        added += 1
    db.commit()
    return {"added": added, "duplicates": len(rows) - added}


# ─── Charges ────────────────────────────────────────────────────────────────

GST = 0.18
SEBI = 10 / 1e7
OCT24, APR26 = date(2024, 10, 1), date(2026, 4, 1)


def _stt(opt: bool, d: date) -> float:
    if opt:
        return 0.0015 if d >= APR26 else 0.001 if d >= OCT24 else 0.000625
    return 0.0005 if d >= APR26 else 0.0002 if d >= OCT24 else 0.000125


def _exch(opt: bool, d: date, exchange: str) -> float:
    if (exchange or "").upper().startswith("B"):
        return 0.000325 if opt else 0.0
    if opt:
        return 0.0003503 if d >= OCT24 else 0.000495
    return 0.0000173 if d >= OCT24 else 0.000019


def day_charges(broker: str, opt: bool, d: date, exchange: str, buys: List, sells: List) -> dict:
    """Estimated charges for one contract's trades on one day (lists of (qty, price, order_id))."""
    bt = sum(q * p for q, p, _ in buys)
    st = sum(q * p for q, p, _ in sells)

    def orders(xs):
        ids = {o for _, _, o in xs if o}
        return len(ids) if ids else (1 if xs else 0)
    n = orders(buys) + orders(sells)
    if broker == "zerodha" and not opt:
        brokerage = min(20.0 * n, 0.0003 * (bt + st))
    else:
        brokerage = 20.0 * n
    stt = _stt(opt, d) * st
    exch = _exch(opt, d, exchange) * (bt + st)
    sebi = SEBI * (bt + st)
    stamp = (0.00003 if opt else 0.00002) * bt
    gst = GST * (brokerage + exch + sebi)
    total = brokerage + stt + exch + sebi + stamp + gst
    return {"brokerage": brokerage, "stt": stt, "exchange": exch, "sebi": sebi, "stamp": stamp, "gst": gst,
            "total": total, "turnover": bt + st}


# ─── Matching ───────────────────────────────────────────────────────────────

def settlements(db) -> Dict[str, float]:
    from app.database import SystemSetting
    row = db.query(SystemSetting).filter(SystemSetting.key == SETTLEMENTS_KEY).first()
    try:
        return {k: float(v) for k, v in (json.loads(row.value) if row and row.value else {}).items()}
    except Exception:
        return {}


def set_settlement(db, symbol: str, price: Optional[float]) -> Dict[str, float]:
    from app.database import SystemSetting
    s = settlements(db)
    sym = symbol.strip().upper()
    if price is None:
        s.pop(sym, None)
    else:
        s[sym] = float(price)
    row = db.query(SystemSetting).filter(SystemSetting.key == SETTLEMENTS_KEY).first()
    if row:
        row.value = json.dumps(s)
    else:
        db.add(SystemSetting(key=SETTLEMENTS_KEY, value=json.dumps(s)))
    db.commit()
    return s


def compute(trades, settle: Dict[str, float], amap: Dict[str, dict], today: Optional[date] = None) -> dict:
    """FIFO per account and contract, long or short; positions past expiry settle at the entered price."""
    from app.services import portfolio as P
    today = today or _today()
    groups: Dict[tuple, list] = defaultdict(list)
    for t in trades:
        groups[(t.account_id or "", t.broker, t.symbol)].append(t)
    realised, positions, charges, warnings = [], [], [], []
    for (acct, broker, sym), ts in groups.items():
        ts.sort(key=lambda t: (t.trade_date, t.trade_time or "", t.id or 0))
        first = ts[0]
        opt = (first.instrument or "") in ("CE", "PE")
        name = P.name_of(amap, acct)
        meta = {"symbol": sym, "underlying": first.underlying, "instrument": first.instrument,
                "expiry": first.expiry, "expiry_estimated": bool(first.expiry_estimated), "strike": first.strike,
                "exchange": first.exchange, "broker": broker, "account": acct, "name": name,
                "instrument_key": next((t.instrument_key for t in ts if t.instrument_key), None)}
        by_day: Dict[str, dict] = defaultdict(lambda: {"buys": [], "sells": []})
        lots: List[dict] = []
        for t in ts:
            by_day[t.trade_date]["buys" if t.side == "BUY" else "sells"].append((t.quantity, t.price, t.order_id))
            signed = t.quantity if t.side == "BUY" else -t.quantity
            while abs(signed) > EPS and lots and (lots[0]["qty"] > 0) != (signed > 0):
                l = lots[0]
                m = min(abs(l["qty"]), abs(signed))
                long = l["qty"] > 0
                pnl = (t.price - l["price"]) * m if long else (l["price"] - t.price) * m
                realised.append({**meta, "opened": l["date"], "closed": t.trade_date, "qty": m,
                                 "side": "long" if long else "short", "entry": l["price"], "exit": t.price,
                                 "pnl": pnl, "how": "trade"})
                l["qty"] -= m if long else -m
                signed += m if long else -m
                if abs(l["qty"]) <= EPS:
                    lots.pop(0)
            if abs(signed) > EPS:
                lots.append({"date": t.trade_date, "qty": signed, "price": t.price})
        for day, x in by_day.items():
            c = day_charges(broker, opt, _d(day), first.exchange or "", x["buys"], x["sells"])
            charges.append({**meta, "date": day, **c})
        if not lots:
            continue
        expired = bool(first.expiry) and _d(first.expiry) < today
        if expired:
            price = settle.get(sym)
            assumed = False
            if price is None and opt:
                price, assumed = 0.0, True
            if price is not None:
                for l in lots:
                    long = l["qty"] > 0
                    m = abs(l["qty"])
                    pnl = (price - l["price"]) * m if long else (l["price"] - price) * m
                    realised.append({**meta, "opened": l["date"], "closed": first.expiry, "qty": m,
                                     "side": "long" if long else "short", "entry": l["price"], "exit": price,
                                     "pnl": pnl, "how": "expiry_assumed_zero" if assumed else "expiry"})
                if assumed:
                    warnings.append(f"{sym}: still open at expiry ({first.expiry}) — counted as expired worthless. "
                                    f"If it expired in the money, enter its settlement price.")
                continue
            warnings.append(f"{sym}: a future still open at expiry ({first.expiry}) — enter its settlement "
                            f"price to count the profit or loss.")
        net = sum(l["qty"] for l in lots)
        cost = sum(l["qty"] * l["price"] for l in lots)
        positions.append({**meta, "qty": net, "side": "long" if net > 0 else "short",
                          "avg": cost / net if net else None, "opened": lots[0]["date"],
                          "expired": expired, "settlement": settle.get(sym)})
    return {"realised": realised, "positions": positions, "charges": charges, "warnings": warnings}


def _trades(db, name: Optional[str] = None, broker: Optional[str] = None):
    from app.database import FnoTrade
    from app.services import portfolio as P
    amap = P.accounts_map(db)
    q = db.query(FnoTrade)
    if broker:
        q = q.filter(FnoTrade.broker == broker)
    ts = q.all()
    if name:
        ts = [t for t in ts if P.name_of(amap, t.account_id or "") == name]
    return ts, amap


def fy_summary(realised: List[dict], charges: List[dict], fy: str) -> dict:
    from app.services import portfolio as P
    rs = [r for r in realised if P.fy_of(_d(r["closed"])) == fy]
    cs = [c for c in charges if P.fy_of(_d(c["date"])) == fy]
    gross = sum(r["pnl"] for r in rs)
    ch = {k: sum(c[k] for c in cs) for k in ("brokerage", "stt", "exchange", "sebi", "stamp", "gst", "total", "turnover")}
    by_u: Dict[str, dict] = defaultdict(lambda: {"gross": 0.0, "charges": 0.0, "trades": 0})
    for r in rs:
        by_u[r["underlying"] or r["symbol"]]["gross"] += r["pnl"]
        by_u[r["underlying"] or r["symbol"]]["trades"] += 1
    for c in cs:
        by_u[c["underlying"] or c["symbol"]]["charges"] += c["total"]
    return {"fy": fy, "gross": gross, "charges": ch, "net": gross - ch["total"],
            "turnover": sum(abs(r["pnl"]) for r in rs), "closed": len(rs),
            "futures": sum(r["pnl"] for r in rs if r["instrument"] == "FUT"),
            "options": sum(r["pnl"] for r in rs if r["instrument"] in ("CE", "PE")),
            "by_underlying": sorted(({"underlying": k, **v, "net": v["gross"] - v["charges"]} for k, v in by_u.items()),
                                    key=lambda x: x["net"]),
            "rows": sorted(rs, key=lambda r: (r["closed"], r["symbol"]), reverse=True)}


def report(db, fy: Optional[str] = None, name: Optional[str] = None, broker: Optional[str] = None,
           with_prices: bool = True) -> dict:
    from app.services import portfolio as P
    ts, amap = _trades(db, name, broker)
    c = compute(ts, settlements(db), amap)
    fy = fy or P.fy_of(_today())
    fys = sorted({P.fy_of(_d(t.trade_date)) for t in ts} | {fy}, reverse=True)
    pos = c["positions"]
    if with_prices and pos:
        keys = sorted({p["instrument_key"] for p in pos if p["instrument_key"] and not p["expired"]})
        quotes = {}
        if keys:
            try:
                from app.main import get_active_feed
                quotes = get_active_feed().get_quotes(keys) or {}
            except Exception as e:
                logger.info(f"[FNO] quotes unavailable: {e}")
        for p in pos:
            q = quotes.get(p["instrument_key"]) or {}
            ltp = q.get("last_price")
            p["ltp"] = ltp
            p["mtm"] = (ltp - p["avg"]) * p["qty"] if ltp and p["avg"] is not None else None
    pos.sort(key=lambda p: (p["expired"], p["expiry"] or "", p["symbol"]))
    s = fy_summary(c["realised"], c["charges"], fy)
    return {**s, "financial_years": fys, "positions": pos, "warnings": c["warnings"],
            "trade_count": len(ts), "first": min((t.trade_date for t in ts), default=None),
            "last": max((t.trade_date for t in ts), default=None),
            "notes": [
                "F&O profit is non-speculative business income, taxed at your slab rate with your other income; "
                "every charge, STT included, is a deductible expense. Charges here are estimates from published "
                "rates — the contract notes are exact.",
                "Turnover is the sum of each closed trade's absolute profit or loss (ICAI guidance). A tax audit "
                "applies above ₹10 crore when almost all receipts are digital, or if you declare a profit under "
                "6% of turnover while your income is above the basic exemption.",
                "An F&O loss can be set off against any income except salary, and carried forward 8 years if the "
                "return is filed on time.",
                "Options still open at expiry are counted as expired worthless unless you enter a settlement price.",
            ]}


def trades_list(db, name: Optional[str] = None, broker: Optional[str] = None, symbol: Optional[str] = None,
                limit: int = 2000) -> List[dict]:
    from app.database import FnoTrade
    from app.services import portfolio as P
    amap = P.accounts_map(db)
    q = db.query(FnoTrade)
    if broker:
        q = q.filter(FnoTrade.broker == broker)
    if symbol:
        q = q.filter((FnoTrade.symbol.ilike(f"%{symbol}%")) | (FnoTrade.underlying.ilike(f"%{symbol}%")))
    out = []
    for t in q.order_by(FnoTrade.trade_date.desc(), FnoTrade.trade_time.desc(), FnoTrade.id.desc()).limit(limit * 2).all():
        nm = P.name_of(amap, t.account_id or "")
        if name and nm != name:
            continue
        out.append({"id": t.id, "broker": t.broker, "symbol": t.symbol, "underlying": t.underlying,
                    "instrument": t.instrument, "expiry": t.expiry, "strike": t.strike, "exchange": t.exchange,
                    "trade_date": t.trade_date, "trade_time": t.trade_time, "side": t.side,
                    "quantity": t.quantity, "price": t.price, "source": t.source, "account": t.account_id,
                    "name": nm})
        if len(out) >= limit:
            break
    return out
