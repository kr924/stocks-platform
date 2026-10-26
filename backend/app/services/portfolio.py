"""
One ledger for equity trades at Upstox and Zerodha: holdings, realised
profit and a capital-gains summary per financial year.

Where the trades come from
  Upstox   the trade-history API (GET /v2/charges/historical-trades, the last
           three financial years) plus today's trades (get-trades-for-day),
           with the token the app already holds. Daily at 16:10 and on demand.
  Zerodha  the tradebook CSV from Console (Reports -> Tradebook -> CSV),
           uploaded on the Portfolio page. Kite Connect is not wired: the
           free personal plan needs a login every morning.
  By hand  IPO allotments, bonus shares (price 0), and anything older than
           the files reach.
Every row is keyed so that importing the same data again adds nothing
(PortfolioTrade.uid). Only the equity segment is kept.

How a sale is matched (compute())
  Per broker and per stock (ISIN, else symbol), day by day:
  - Bought and sold the same day: the smaller of the day's buys and sells is
    intraday, at the day's average prices. Intraday is speculative business
    income, taxed at slab, so it is reported apart from capital gains.
  - What the day bought beyond that becomes a lot at the day's average buy.
  - What it sold beyond that is matched against the oldest lots first
    (first in, first out, per demat account). A sale with no lot to match
    is reported, not guessed: history is missing, or a bonus/split was not
    entered.
  Each match is short-term if the shares were held 12 months or less, long
  term if more.

Tax (tax_summary()) is an estimate for planning, not a filing:
  - Rates by sale date: before 23 Jul 2024 STCG 15% / LTCG 10%, from then
    20% / 12.5%. LTCG exemption ₹1 lakh a year to FY 2023-24, ₹1.25 lakh
    from FY 2024-25.
  - A short-term loss is set off against long-term gains; a long-term loss
    only against long-term gains (the rest carries forward, not shown).
  - Brokerage, STT and other charges are not in tradebooks and are not
    deducted. Cess and surcharge are not added. Shares bought before
    1 Feb 2018 are flagged (the 31 Jan 2018 value rule) and use actual cost.
"""
import csv
import io
import logging
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from app.services.charges import day_charges

logger = logging.getLogger("app.portfolio")

IST = timezone(timedelta(hours=5, minutes=30))
BROKERS = ("upstox", "zerodha")
RATE_CHANGE = date(2024, 7, 23)
GRANDFATHER_BEFORE = date(2018, 2, 1)
EPS = 1e-6


# ─── Dates ──────────────────────────────────────────────────────────────────

def _today() -> date:
    return datetime.now(IST).date()


def _d(s: str) -> date:
    return datetime.strptime(s[:10], "%Y-%m-%d").date()


def fy_of(d: date) -> str:
    """Financial year label: 15 Oct 2026 -> '2026-27'."""
    y = d.year if d.month >= 4 else d.year - 1
    return f"{y}-{str(y + 1)[-2:]}"


def fy_bounds(fy: str) -> Tuple[date, date]:
    y = int(fy[:4])
    return date(y, 4, 1), date(y + 1, 3, 31)


def _one_year_after(d: date) -> date:
    try:
        return d.replace(year=d.year + 1)
    except ValueError:  # 29 Feb
        return d.replace(year=d.year + 1, day=28)


def is_long_term(buy: date, sell: date) -> bool:
    """Held for more than 12 months."""
    return sell > _one_year_after(buy)


_DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d", "%d-%b-%Y", "%d %b %Y",
                 "%d-%b-%y", "%d/%m/%y", "%m/%d/%Y")


def parse_date(text: str) -> Optional[Tuple[str, Optional[str]]]:
    """('YYYY-MM-DD', 'HH:MM:SS' or None) from the formats brokers write."""
    s = (text or "").strip().replace("T", " ")
    if not s:
        return None
    parts = s.split()
    time_part = None
    m = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", s)
    if m:
        time_part = f"{int(m.group(1)):02d}:{m.group(2)}:{m.group(3) or '00'}"
    day_text = parts[0] if parts else s
    if len(parts) >= 3 and re.match(r"^\d{1,2}$", parts[0]) and re.match(r"^[A-Za-z]{3}", parts[1]):
        day_text = " ".join(parts[:3])
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(day_text, fmt).date().isoformat(), time_part
        except ValueError:
            continue
    try:
        from dateutil import parser as du
        return du.parse(s, dayfirst=True).date().isoformat(), time_part
    except Exception:
        return None


def _num(text) -> Optional[float]:
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    s = str(text).strip().replace(",", "").replace("₹", "")
    if not s:
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


# ─── Storing trades ─────────────────────────────────────────────────────────

def _uid(broker, exchange, trade_id, side, fallback) -> str:
    if trade_id:
        return f"{broker}:{(exchange or '').upper()}:{trade_id}:{side}"
    return f"{broker}:noid:{fallback}"


def store(db, rows: List[dict]) -> dict:
    """Insert rows not already present (by uid). Returns {added, duplicates}."""
    from app.database import PortfolioTrade
    if not rows:
        return {"added": 0, "duplicates": 0}
    uids = [r["uid"] for r in rows]
    have = set()
    for i in range(0, len(uids), 500):
        have.update(u for (u,) in db.query(PortfolioTrade.uid)
                    .filter(PortfolioTrade.uid.in_(uids[i:i + 500])).all())
    added = 0
    seen = set()
    for r in rows:
        if r["uid"] in have or r["uid"] in seen:
            continue
        seen.add(r["uid"])
        db.add(PortfolioTrade(**r))
        added += 1
    db.commit()
    return {"added": added, "duplicates": len(rows) - added}


# ─── CSV import (Zerodha tradebook, or any file with the same columns) ──────

_HEADER_ALIASES = {
    "symbol": ("symbol", "tradingsymbol", "trading_symbol", "scrip", "scrip_name", "stock", "instrument"),
    "isin": ("isin",),
    "date": ("trade_date", "date", "tradedate", "trade_dt"),
    "time": ("order_execution_time", "trade_time", "execution_time", "time", "exchange_timestamp"),
    "side": ("trade_type", "side", "transaction_type", "buy_sell", "buy/sell", "type", "action"),
    "qty": ("quantity", "qty", "traded_qty", "trade_qty"),
    "price": ("price", "trade_price", "rate", "avg_price", "average_price", "traded_price"),
    "trade_id": ("trade_id", "trade_no", "trade_num", "trade_number", "tradeid"),
    "order_id": ("order_id", "order_no", "order_number", "orderid"),
    "exchange": ("exchange", "exch"),
    "segment": ("segment",),
    "series": ("series",),
}
EQUITY_SEGMENTS = {"", "EQ", "EQUITY", "CASH", "NSE_EQ", "BSE_EQ", "CM"}


def _norm(h: str) -> str:
    return re.sub(r"[^a-z0-9/]+", "_", (h or "").strip().lower()).strip("_")


def parse_csv(text: str, broker: str) -> dict:
    """
    Rows from a tradebook CSV. The header row is found by its columns (the
    file may start with title lines), headers are matched by name, and only
    equity trades are kept. Returns {rows, skipped, errors, columns}.
    """
    broker = (broker or "").lower()
    if broker not in BROKERS:
        raise ValueError(f"broker must be one of {', '.join(BROKERS)}")
    lines = list(csv.reader(io.StringIO((text or "").lstrip("﻿"))))
    head_i, cols = None, {}
    for i, line in enumerate(lines[:30]):
        names = [_norm(c) for c in line]
        found = {}
        for key, aliases in _HEADER_ALIASES.items():
            for j, n in enumerate(names):
                if n in aliases and key not in found:
                    found[key] = j
        if all(k in found for k in ("symbol", "date", "side", "qty", "price")):
            head_i, cols = i, found
            break
    if head_i is None:
        raise ValueError("No header row with symbol, trade date, buy/sell, quantity and price was found. "
                         "Zerodha: Console → Reports → Tradebook → Equity → download CSV.")
    rows, skipped, errors = [], 0, []
    src = f"{broker}_csv"
    for n, line in enumerate(lines[head_i + 1:], start=head_i + 2):
        if not any((c or "").strip() for c in line):
            continue

        def g(key):
            j = cols.get(key)
            return line[j].strip() if j is not None and j < len(line) else ""

        seg = g("segment").upper()
        if seg not in EQUITY_SEGMENTS:
            skipped += 1
            continue
        series = g("series").upper()
        side_raw = g("side").upper()
        side = "BUY" if side_raw in ("BUY", "B", "PURCHASE") else "SELL" if side_raw in ("SELL", "S", "SALE") else ""
        qty, price = _num(g("qty")), _num(g("price"))
        parsed = parse_date(g("date"))
        symbol = g("symbol").upper()
        if not (side and qty and price is not None and parsed and symbol):
            errors.append(f"line {n}: could not read ({', '.join(line[:6])[:120]})")
            continue
        day, tm = parsed
        t2 = parse_date(g("time")) if g("time") else None
        if t2 and t2[1]:
            tm = t2[1]
        exchange = g("exchange").upper() or None
        trade_id = g("trade_id") or None
        rows.append({
            "uid": _uid(broker, exchange, trade_id, side,
                        f"{day}:{symbol}:{side}:{qty}:{price}:{n}"),
            "broker": broker, "symbol": symbol, "isin": g("isin").upper() or None,
            "exchange": exchange, "instrument_key": None,
            "trade_date": day, "trade_time": tm, "side": side,
            "quantity": abs(qty), "price": abs(price),
            "trade_id": trade_id, "order_id": g("order_id") or None,
            "source": src, "note": f"series {series}" if series and series != "EQ" else None,
        })
    return {"rows": rows, "skipped": skipped, "errors": errors[:20],
            "columns": sorted(cols)}


# ─── Upstox trade-history API ───────────────────────────────────────────────

def _upstox_get(feed, path: str, params: dict) -> dict:
    import requests
    if not getattr(feed, "access_token", None):
        raise RuntimeError("Upstox is not logged in — log in from the header first")
    res = requests.get(f"https://api.upstox.com{path}", headers=feed._get_headers(),
                       params=params, timeout=30)
    if res.status_code == 401:
        raise RuntimeError("Upstox session expired — log in again")
    if res.status_code != 200:
        raise RuntimeError(f"Upstox {path} HTTP {res.status_code}: {res.text[:200]}")
    return res.json() or {}


def _isin_from_key(key: str) -> Optional[str]:
    tail = (key or "").split("|")[-1]
    return tail if len(tail) == 12 and tail[:2].isalpha() else None


def fetch_upstox(feed, years: int = 3) -> dict:
    """years=0: today's trades only (after a sell from the tracker)."""
    """
    Equity trades from Upstox: every financial year the API keeps (three),
    then today's trades, which the history endpoint has not caught up with.
    """
    rows, errors = [], []
    today = _today()
    this_fy = int(fy_of(today)[:4])
    for y in range(this_fy - years + 1, this_fy + 1):
        start, end = date(y, 4, 1), min(date(y + 1, 3, 31), today)
        page = 1
        while True:
            try:
                data = _upstox_get(feed, "/v2/charges/historical-trades", {
                    "segment": "EQ", "start_date": start.isoformat(), "end_date": end.isoformat(),
                    "page_number": page, "page_size": 500})
            except Exception as e:
                errors.append(f"FY {y}-{str(y + 1)[-2:]}: {e}")
                break
            items = data.get("data") or []
            for t in items:
                side = (t.get("transaction_type") or "").upper()
                if side not in ("BUY", "SELL") or (t.get("segment") or "EQ").upper() != "EQ":
                    continue
                qty, price = _num(t.get("quantity")), _num(t.get("price"))
                day = (t.get("trade_date") or "")[:10]
                if not (qty and price is not None and day):
                    continue
                key = t.get("instrument_token") or ""
                rows.append({
                    "uid": _uid("upstox", t.get("exchange"), t.get("trade_id"), side,
                                f"{day}:{t.get('symbol')}:{side}:{qty}:{price}"),
                    "broker": "upstox", "symbol": (t.get("symbol") or t.get("scrip_name") or "").upper(),
                    "isin": (t.get("isin") or _isin_from_key(key) or "").upper() or None,
                    "exchange": (t.get("exchange") or "").upper() or None,
                    "instrument_key": key or None,
                    "trade_date": day, "trade_time": None, "side": side,
                    "quantity": abs(qty), "price": abs(price),
                    "trade_id": t.get("trade_id"), "order_id": None,
                    "source": "upstox_api", "note": None,
                })
            meta = ((data.get("meta_data") or data.get("metadata") or {}).get("page") or {})
            total_pages = meta.get("total_pages")
            if not items or (total_pages and page >= int(total_pages)) or (not total_pages and len(items) < 500):
                break
            page += 1
    try:
        data = _upstox_get(feed, "/v2/order/trades/get-trades-for-day", {})
        for t in data.get("data") or []:
            key = t.get("instrument_token") or ""
            if "_EQ|" not in key:
                continue
            side = (t.get("transaction_type") or "").upper()
            qty, price = _num(t.get("quantity")), _num(t.get("average_price"))
            if side not in ("BUY", "SELL") or not qty or price is None:
                continue
            parsed = parse_date(t.get("exchange_timestamp") or t.get("order_timestamp") or "")
            day, tm = parsed if parsed else (today.isoformat(), None)
            rows.append({
                "uid": _uid("upstox", t.get("exchange"), t.get("trade_id"), side,
                            f"{day}:{t.get('trading_symbol')}:{side}:{qty}:{price}:{t.get('order_id')}"),
                "broker": "upstox", "symbol": (t.get("trading_symbol") or t.get("tradingsymbol") or "").upper(),
                "isin": _isin_from_key(key), "exchange": (t.get("exchange") or "").upper() or None,
                "instrument_key": key, "trade_date": day, "trade_time": tm, "side": side,
                "quantity": abs(qty), "price": abs(price),
                "trade_id": t.get("trade_id"), "order_id": t.get("order_id"),
                "source": "upstox_api", "note": None,
            })
    except Exception as e:
        errors.append(f"today's trades: {e}")
    return {"rows": rows, "errors": errors}


def sync_upstox(db, years: int = 3) -> dict:
    """Fetch trades and holdings, store, record the outcome, and update the tracker's H chips."""
    import json
    from app.main import get_active_feed
    from app.database import SystemSetting
    got = fetch_upstox(get_active_feed(), years=years)
    res = store(db, got["rows"])
    res.update({"fetched": len(got["rows"]), "errors": got["errors"],
                "at": datetime.utcnow().isoformat() + "Z"})
    try:
        row = db.query(SystemSetting).filter(SystemSetting.key == "portfolio_upstox_sync").first()
        if row:
            row.value = json.dumps(res)
        else:
            db.add(SystemSetting(key="portfolio_upstox_sync", value=json.dumps(res)))
        db.commit()
    except Exception:
        db.rollback()
    try:
        upstox_holdings(db, force=True)
        res["watchlist"] = sync_watchlist(db)
    except Exception as e:
        logger.warning(f"[PORTFOLIO] watchlist sync failed: {e}")
    logger.info(f"[PORTFOLIO] Upstox sync: {res['added']} new of {res['fetched']}; errors: {res['errors']}")
    return res


# ─── Matching ───────────────────────────────────────────────────────────────

def _group_key(t, sym2isin: Optional[dict] = None) -> str:
    """ISIN, else the ISIN another trade of the same symbol carries, else the symbol."""
    isin = (t.isin or "").upper()
    sym = (t.symbol or "").upper()
    return isin or (sym2isin or {}).get(sym) or sym


def _sym2isin(trades) -> dict:
    out = {}
    for t in trades:
        if t.isin and t.symbol:
            out.setdefault(t.symbol.upper(), t.isin.upper())
    return out


def compute(trades) -> dict:
    """
    trades: PortfolioTrade rows (any order). Returns
      lots      open lots per (broker, stock)
      realised  one row per matched sale (or intraday pair)
      unmatched sells with nothing to match
      names     {stock key: (symbol, isin, instrument_key)}
    """
    groups: Dict[tuple, list] = defaultdict(list)
    names: Dict[str, dict] = {}
    s2i = _sym2isin(trades)
    for t in trades:
        k = _group_key(t, s2i)
        groups[(t.broker, k)].append(t)
        n = names.setdefault(k, {"symbol": t.symbol, "isin": t.isin or (k if len(k) == 12 and k[:2].isalpha() else None),
                                 "instrument_key": t.instrument_key})
        if t.instrument_key and not n.get("instrument_key"):
            n["instrument_key"] = t.instrument_key
        if t.source == "upstox_api" and t.symbol:
            n["symbol"] = t.symbol   # Upstox writes the NSE symbol; prefer it

    lots: Dict[tuple, list] = {}
    realised, unmatched = [], []
    for (broker, k), ts in groups.items():
        by_day = defaultdict(list)
        for t in ts:
            by_day[t.trade_date].append(t)
        open_lots: List[dict] = []
        for day in sorted(by_day):
            buys = [t for t in by_day[day] if t.side == "BUY"]
            sells = [t for t in by_day[day] if t.side == "SELL"]
            bq = sum(t.quantity for t in buys)
            sq = sum(t.quantity for t in sells)
            bavg = sum(t.quantity * t.price for t in buys) / bq if bq else 0.0
            savg = sum(t.quantity * t.price for t in sells) / sq if sq else 0.0
            intra = min(bq, sq)
            sd = _d(day)
            ch = day_charges(broker, sd, buys, sells, intra)
            if intra > EPS:
                gain = intra * (savg - bavg)
                # Speculative business income: every charge, STT included, is
                # a business expense.
                realised.append({"broker": broker, "key": k, "buy_date": day, "sell_date": day,
                                 "qty": intra, "buy_price": bavg, "sell_price": savg,
                                 "gain": gain, "buy_charges": 0.0, "sell_charges": ch["intra_other"],
                                 "stt": ch["intra_stt"], "tax_gain": gain - ch["intra_other"] - ch["intra_stt"],
                                 "term": "INTRADAY"})
            dq = bq - intra
            if dq > EPS:
                open_lots.append({"date": day, "qty": dq, "price": bavg,
                                  "charge_ps": ch["buy_other"] / dq, "stt_ps": ch["buy_stt"] / dq})
            left = sq - intra
            sell_ps = ch["sell_other"] / left if left > EPS else 0.0
            sell_stt_ps = ch["sell_stt"] / left if left > EPS else 0.0
            while left > EPS and open_lots:
                lot = open_lots[0]
                take = min(left, lot["qty"])
                bd = _d(lot["date"])
                gain = take * (savg - lot["price"])
                bc, sc = take * lot["charge_ps"], take * sell_ps
                realised.append({"broker": broker, "key": k, "buy_date": lot["date"], "sell_date": day,
                                 "qty": take, "buy_price": lot["price"], "sell_price": savg,
                                 "gain": gain, "buy_charges": bc, "sell_charges": sc,
                                 "stt": take * (lot["stt_ps"] + sell_stt_ps),
                                 # Capital gain: charges other than STT are cost
                                 # of acquisition or expenses of transfer.
                                 "tax_gain": gain - bc - sc,
                                 "term": "LTCG" if is_long_term(bd, sd) else "STCG",
                                 "grandfathered": bd < GRANDFATHER_BEFORE})
                lot["qty"] -= take
                left -= take
                if lot["qty"] <= EPS:
                    open_lots.pop(0)
            if left > EPS:
                unmatched.append({"broker": broker, "key": k, "date": day, "qty": left, "price": savg})
        if open_lots:
            lots[(broker, k)] = open_lots
    return {"lots": lots, "realised": realised, "unmatched": unmatched, "names": names}


def _all_trades(db, broker: Optional[str] = None):
    from app.database import PortfolioTrade
    q = db.query(PortfolioTrade)
    if broker in BROKERS:
        q = q.filter(PortfolioTrade.broker == broker)
    return q.all()


def _instrument_key(info: dict) -> str:
    key = info.get("instrument_key") or ""
    if key and "|" in key:
        return key
    try:
        from app.main import resolve_instrument_keys
        keys = resolve_instrument_keys(info.get("symbol") or "")
    except Exception:
        keys = []
    isin = (info.get("isin") or "").upper()
    for k in keys:
        if isin and k.endswith(isin):
            return k
    return keys[0] if keys else ""


# ─── Upstox's own holdings ──────────────────────────────────────────────────

UPSTOX_HOLDINGS_KEY = "portfolio_upstox_holdings"
UPSTOX_HOLDINGS_MAX_AGE = 600   # seconds; holdings only change when you trade


def fetch_upstox_holdings(feed) -> List[dict]:
    """GET /v2/portfolio/long-term-holdings: what Upstox says you hold now."""
    data = _upstox_get(feed, "/v2/portfolio/long-term-holdings", {})
    out = []
    for h in data.get("data") or []:
        key = h.get("instrument_token") or ""
        out.append({
            "symbol": (h.get("trading_symbol") or h.get("tradingsymbol") or "").upper(),
            "isin": (h.get("isin") or _isin_from_key(key) or "").upper() or None,
            "instrument_key": key, "exchange": (h.get("exchange") or "").upper() or None,
            "company_name": h.get("company_name"),
            "quantity": _num(h.get("quantity")) or 0.0,
            "t1_quantity": _num(h.get("t1_quantity")) or 0.0,
            "average_price": _num(h.get("average_price")) or 0.0,
        })
    return out


def upstox_holdings(db, max_age: int = UPSTOX_HOLDINGS_MAX_AGE, force: bool = False) -> Optional[dict]:
    """
    The last Upstox holdings snapshot, refreshed when older than max_age.
    {at, rows, changed, error}. None when Upstox has never answered.
    """
    import json
    from app.database import SystemSetting
    row = db.query(SystemSetting).filter(SystemSetting.key == UPSTOX_HOLDINGS_KEY).first()
    old = None
    if row and row.value:
        try:
            old = json.loads(row.value)
        except Exception:
            old = None
    if old and not force:
        try:
            age = (datetime.utcnow() - datetime.fromisoformat(old["at"].rstrip("Z"))).total_seconds()
        except Exception:
            age = 1e9
        if age < max_age:
            return {**old, "changed": False}
    try:
        from app.main import get_active_feed
        rows = fetch_upstox_holdings(get_active_feed())
    except Exception as e:
        if old:
            return {**old, "changed": False, "error": str(e)}
        return None
    new = {"at": datetime.utcnow().isoformat() + "Z", "rows": rows}
    try:
        if row:
            row.value = json.dumps(new)
        else:
            db.add(SystemSetting(key=UPSTOX_HOLDINGS_KEY, value=json.dumps(new)))
        db.commit()
    except Exception:
        db.rollback()
    changed = not old or sorted((r["isin"] or r["symbol"], r["quantity"], r["t1_quantity"]) for r in old.get("rows", [])) != \
        sorted((r["isin"] or r["symbol"], r["quantity"], r["t1_quantity"]) for r in rows)
    return {**new, "changed": changed}


# ─── Positions: the ledger, reconciled with Upstox's holdings ───────────────

def _lot_view(lots: List[dict], today: date) -> List[dict]:
    out = []
    for l in lots:
        d = l.get("date")
        out.append({"date": d, "qty": round(l["qty"], 4), "price": l.get("price"),
                    "days": (today - _d(d)).days if d else None,
                    "long_term": (today > _one_year_after(_d(d))) if d else None})
    return out


def _trim_fifo(lots: List[dict], qty: float) -> List[dict]:
    """Keep the newest `qty` shares: older lots were sold first."""
    out, need = [], qty
    for l in reversed(lots):
        if need <= EPS:
            break
        take = min(need, l["qty"])
        out.append({**l, "qty": take})
        need -= take
    return list(reversed(out))


def positions(db, refresh: bool = True) -> dict:
    """
    What is held now, per stock and broker.

    Zerodha: the ledger (tradebook imports). Upstox: the ledger where it
    agrees with Upstox's own holdings; otherwise Upstox's quantity and
    average price, with the ledger's dated lots for as many shares as it
    covers and an undated lot for the rest (bought before the three years the
    trade history reaches). A stock the ledger holds that Upstox does not is
    taken as sold, unless it was bought today (not a holding until T+1).
    """
    trades = _all_trades(db)
    c = compute(trades)
    today = _today()
    tstr = today.isoformat()
    s2i = _sym2isin(trades)
    entries: Dict[str, dict] = {}

    def entry(k, info):
        return entries.setdefault(k, {"key": k, "symbol": info.get("symbol"), "isin": info.get("isin"),
                                      "instrument_key": info.get("instrument_key") or "", "brokers": {}})

    for (brk, k), ls in c["lots"].items():
        qty = sum(l["qty"] for l in ls)
        if qty <= EPS:
            continue
        cost = sum(l["qty"] * l["price"] for l in ls)
        entry(k, c["names"].get(k, {}))["brokers"][brk] = {
            "qty": qty, "avg": cost / qty, "invested": cost, "lots": [dict(l) for l in ls],
            "charges": sum(l["qty"] * l.get("charge_ps", 0) for l in ls), "source": "ledger"}

    snap = upstox_holdings(db) if refresh else None
    snapshot_ok = bool(snap) and not snap.get("error")
    if snap and snap.get("rows") is not None:
        today_net: Dict[str, float] = defaultdict(float)
        today_lots: Dict[str, list] = defaultdict(list)
        for t in trades:
            if t.broker == "upstox" and t.trade_date == tstr:
                k = _group_key(t, s2i)
                today_net[k] += t.quantity if t.side == "BUY" else -t.quantity
        for k, ls in c["lots"].items():
            if k[0] == "upstox":
                today_lots[k[1]] = [l for l in ls if l["date"] == tstr]
        in_snap = set()
        for h in snap["rows"]:
            k = h["isin"] or s2i.get(h["symbol"]) or h["symbol"]
            in_snap.add(k)
            e = entry(k, {"symbol": h["symbol"], "isin": h["isin"], "instrument_key": h["instrument_key"]})
            if not e.get("instrument_key"):
                e["instrument_key"] = h["instrument_key"]
            led = e["brokers"].get("upstox")
            led_prev = (led["qty"] if led else 0.0) - today_net.get(k, 0.0)
            cands = (h["quantity"], h["quantity"] + h["t1_quantity"])
            if led and any(abs(x - led_prev) < 1e-6 for x in cands):
                continue                      # the ledger agrees; keep its lots and costs
            qty = h["quantity"] + h["t1_quantity"] + today_net.get(k, 0.0)
            if qty <= EPS:
                e["brokers"].pop("upstox", None)
                continue
            lots = sorted((led or {}).get("lots") or [], key=lambda l: l["date"])
            known = sum(l["qty"] for l in lots)
            if known > qty:
                lots = _trim_fifo(lots, qty)
            elif qty - known > EPS:
                lots = [{"date": None, "qty": qty - known, "price": h["average_price"]}] + lots
            e["brokers"]["upstox"] = {"qty": qty, "avg": h["average_price"], "invested": h["average_price"] * qty,
                                      "lots": lots, "charges": None, "source": "upstox_holdings"}
        if snapshot_ok:
            for k, e in entries.items():
                if k in in_snap or "upstox" not in e["brokers"]:
                    continue
                tl = today_lots.get(k) or []
                q = sum(l["qty"] for l in tl)
                if q > EPS:
                    cost = sum(l["qty"] * l["price"] for l in tl)
                    e["brokers"]["upstox"] = {"qty": q, "avg": cost / q, "invested": cost, "lots": tl,
                                              "charges": None, "source": "ledger_today"}
                else:
                    e["brokers"].pop("upstox", None)

    out = []
    for k, e in entries.items():
        if not e["brokers"]:
            continue
        lots_all = []
        for brk, b in e["brokers"].items():
            b["lots"] = _lot_view(b["lots"], today)
            for l in b["lots"]:
                lots_all.append({**l, "broker": brk})
        lots_all.sort(key=lambda l: (l["date"] or "0000"))
        qty = sum(b["qty"] for b in e["brokers"].values())
        inv = sum(b["invested"] for b in e["brokers"].values())
        if not e.get("instrument_key"):
            e["instrument_key"] = _instrument_key(e)
        out.append({**e, "qty": qty, "invested": inv, "avg": inv / qty if qty else None, "lots": lots_all,
                    "broker": "both" if len(e["brokers"]) > 1 else next(iter(e["brokers"]))})
    return {"positions": out, "upstox_snapshot_at": (snap or {}).get("at"),
            "upstox_error": (snap or {}).get("error") if snap else "Upstox holdings not fetched yet",
            "changed": bool(snap and snap.get("changed")), "compute": c}


def holdings(db, broker: Optional[str] = None, with_prices: bool = True) -> dict:
    """Open positions per stock and broker, with live value where a quote exists."""
    pos = positions(db)
    c = pos["compute"]
    today = _today()
    rows = []
    for p in pos["positions"]:
        for brk, b in p["brokers"].items():
            if broker in BROKERS and brk != broker:
                continue
            known_lots = [l for l in b["lots"] if l["date"]]
            unknown = sum(l["qty"] for l in b["lots"] if not l["date"])
            lt_qty = sum(l["qty"] for l in known_lots if l["long_term"])
            pending = [l for l in known_lots if not l["long_term"]]
            rows.append({"key": p["key"], "symbol": p["symbol"], "isin": p["isin"], "broker": brk,
                         "qty": round(b["qty"], 4), "avg_cost": b["avg"], "invested": b["invested"],
                         "first_buy": known_lots[0]["date"] if known_lots else None,
                         "last_buy": known_lots[-1]["date"] if known_lots else None,
                         "lots": b["lots"], "undated_qty": round(unknown, 4), "source": b["source"],
                         "long_term_qty": round(lt_qty, 4),
                         "next_long_term": (min((_one_year_after(_d(l["date"])) + timedelta(days=1)).isoformat()
                                                for l in pending) if pending else None),
                         "instrument_key": p["instrument_key"],
                         "ltp": None, "value": None, "pnl": None, "pnl_pct": None, "day_change_pct": None})
    if with_prices and rows:
        try:
            from app.main import get_active_feed
            keys = sorted({r["instrument_key"] for r in rows if r["instrument_key"]})
            quotes = get_active_feed().get_quotes(keys) if keys else {}
        except Exception as e:
            logger.info(f"[PORTFOLIO] quotes unavailable: {e}")
            quotes = {}
        for r in rows:
            q = quotes.get(r["instrument_key"]) or {}
            ltp = q.get("last_price") or 0
            if ltp:
                r["ltp"] = ltp
                r["value"] = ltp * r["qty"]
                r["pnl"] = r["value"] - r["invested"]
                r["pnl_pct"] = (r["pnl"] / r["invested"] * 100) if r["invested"] else None
                close = (q.get("ohlc") or {}).get("close") or 0
                r["day_change_pct"] = ((ltp / close - 1) * 100) if close else None
    rows.sort(key=lambda r: -(r["value"] or r["invested"]))
    fy = fy_of(today)
    real = [x for x in c["realised"] if fy_of(_d(x["sell_date"])) == fy
            and (broker not in BROKERS or x["broker"] == broker)]
    invested = sum(r["invested"] for r in rows)
    priced = [r for r in rows if r["value"] is not None]
    return {
        "rows": rows,
        "totals": {
            "invested": invested,
            "value": sum(r["value"] for r in priced) if priced else None,
            "pnl": sum(r["pnl"] for r in priced) if priced else None,
            "priced_invested": sum(r["invested"] for r in priced),
            "unpriced": len(rows) - len(priced),
            "realised_fy": fy,
            "realised_fy_capital": sum(x["tax_gain"] for x in real if x["term"] != "INTRADAY"),
            "realised_fy_intraday": sum(x["tax_gain"] for x in real if x["term"] == "INTRADAY"),
        },
        "upstox_snapshot_at": pos["upstox_snapshot_at"],
        "upstox_error": pos["upstox_error"],
        "warnings": _warnings(c),
    }


def _warnings(c) -> List[str]:
    out = []
    for u in c["unmatched"][:30]:
        info = c["names"].get(u["key"], {})
        out.append(f"{info.get('symbol') or u['key']} ({u['broker']}): sold {u['qty']:g} on {u['date']} "
                   f"with no earlier buy on file — import older history, or add the buy, IPO allotment "
                   f"or bonus by hand")
    if len(c["unmatched"]) > 30:
        out.append(f"…and {len(c['unmatched']) - 30} more unmatched sales")
    gf = {x["key"] for x in c["realised"] if x.get("grandfathered") and x["term"] == "LTCG"}
    for k in sorted(gf):
        out.append(f"{c['names'].get(k, {}).get('symbol') or k}: shares bought before 1 Feb 2018 were "
                   f"sold — the 31 Jan 2018 value rule may lower the gain; actual cost is used here")
    return out


# ─── Tax summary ────────────────────────────────────────────────────────────

def _rates(sell: date) -> Tuple[float, float]:
    return (0.15, 0.10) if sell < RATE_CHANGE else (0.20, 0.125)


def _exemption(fy: str) -> float:
    return 100000.0 if int(fy[:4]) <= 2023 else 125000.0


def tax_summary(db, fy: str, broker: Optional[str] = None) -> dict:
    c = compute(_all_trades(db, broker))
    rows = [x for x in c["realised"] if fy_of(_d(x["sell_date"])) == fy]
    st = [x for x in rows if x["term"] == "STCG"]
    lt = [x for x in rows if x["term"] == "LTCG"]
    it = [x for x in rows if x["term"] == "INTRADAY"]

    # Gains after estimated charges (services/charges.py): brokerage, exchange,
    # SEBI, stamp, GST and DP are deductible; STT is not.
    def tot(xs, sign):
        return sum(x["tax_gain"] for x in xs if (x["tax_gain"] > 0) == (sign > 0) and x["tax_gain"] != 0)

    st_gain, st_loss = tot(st, 1), tot(st, -1)
    lt_gain, lt_loss = tot(lt, 1), tot(lt, -1)
    st_net, lt_net = st_gain + st_loss, lt_gain + lt_loss
    set_off = 0.0
    if st_net < 0 and lt_net > 0:
        set_off = min(-st_net, lt_net)
        lt_net -= set_off
        st_net += set_off
    exemption = _exemption(fy)
    lt_taxable = max(0.0, lt_net - exemption)

    def wrate(xs, i):
        g = [(x["tax_gain"], _rates(_d(x["sell_date"]))[i]) for x in xs if x["tax_gain"] > 0]
        tg = sum(a for a, _ in g)
        return sum(a * r for a, r in g) / tg if tg else _rates(min(_today(), fy_bounds(fy)[1]))[i]

    st_rate, lt_rate = wrate(st, 0), wrate(lt, 1)
    est = max(0.0, st_net) * st_rate + lt_taxable * lt_rate
    turnover = sum(x["qty"] * x["sell_price"] for x in rows)
    names = c["names"]
    detail = sorted(({**x, "symbol": names.get(x["key"], {}).get("symbol"),
                      "isin": names.get(x["key"], {}).get("isin"),
                      "sale_value": x["qty"] * x["sell_price"], "cost": x["qty"] * x["buy_price"],
                      "charges": x["buy_charges"] + x["sell_charges"]}
                     for x in rows), key=lambda x: (x["sell_date"], x["symbol"] or ""))
    return {
        "fy": fy,
        "stcg": {"gain": st_gain, "loss": st_loss, "net": st_gain + st_loss, "rate": st_rate},
        "ltcg": {"gain": lt_gain, "loss": lt_loss, "net": lt_gain + lt_loss, "rate": lt_rate,
                 "exemption": exemption, "exemption_used": min(exemption, max(0.0, lt_net)),
                 "taxable": lt_taxable},
        "short_term_loss_set_off_against_long_term": set_off,
        "long_term_loss_carried_forward": -lt_net if lt_net < 0 else 0.0,
        "short_term_loss_carried_forward": -st_net if st_net < 0 else 0.0,
        "intraday": {"pnl": sum(x["tax_gain"] for x in it), "gross": sum(x["gain"] for x in it),
                     "turnover": sum(x["qty"] * x["sell_price"] for x in it), "trades": len(it)},
        "charges": {"deductible": sum(x["buy_charges"] + x["sell_charges"] for x in rows),
                    "stt": sum(x["stt"] for x in rows),
                    "gross_gain": sum(x["gain"] for x in rows if x["term"] != "INTRADAY")},
        "estimated_tax": est,
        "sale_value": turnover,
        "rows": detail,
        "warnings": _warnings(c),
        "notes": [
            "Estimate before 4% cess and any surcharge.",
            "Charges are estimated from each broker's published rates (the contract note is exact): brokerage, "
            "exchange, SEBI, stamp duty, GST and DP charges are deducted from capital gains; STT is not deductible "
            "there and is shown separately. Intraday profit is shown after all charges, STT included.",
            "Rates by sale date: STCG 15% / LTCG 10% before 23 Jul 2024, 20% / 12.5% from then.",
            f"LTCG exemption ₹{exemption:,.0f} for FY {fy}.",
            "Intraday (same-day buy and sell) is speculative business income, taxed at your slab — not included.",
            "Check with your CA before filing.",
        ],
    }


def realised_csv(db, fy: str, broker: Optional[str] = None) -> str:
    """The FY's matched sales, one row each, in the shape the ITR capital-gains schedules ask for."""
    s = tax_summary(db, fy, broker)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Type", "Broker", "Symbol", "ISIN", "Buy date", "Sell date", "Quantity",
                "Buy price", "Sell price", "Purchase value", "Buy charges (est.)",
                "Cost of acquisition", "Sale value", "Sale charges (est.)", "STT (est., not deductible)",
                "Gain/Loss before charges", "Gain/Loss after charges", "Bought before 1 Feb 2018"])
    for x in s["rows"]:
        w.writerow([x["term"], x["broker"], x["symbol"], x["isin"] or "", x["buy_date"], x["sell_date"],
                    round(x["qty"], 4), round(x["buy_price"], 4), round(x["sell_price"], 4),
                    round(x["cost"], 2), round(x["buy_charges"], 2), round(x["cost"] + x["buy_charges"], 2),
                    round(x["sale_value"], 2), round(x["sell_charges"], 2), round(x["stt"], 2),
                    round(x["gain"], 2), round(x["tax_gain"], 2),
                    "yes" if x.get("grandfathered") else ""])
    return out.getvalue()


def financial_years(db) -> List[str]:
    from app.database import PortfolioTrade
    days = [d for (d,) in db.query(PortfolioTrade.trade_date).distinct().all() if d]
    fys = sorted({fy_of(_d(d)) for d in days} | {fy_of(_today())}, reverse=True)
    return fys


# ─── The tracker's H / U / Z chips ──────────────────────────────────────────

def sync_watchlist(db, pos: Optional[dict] = None) -> dict:
    """
    Make the Stocks Tracker's holdings match positions(): held -> H with the
    broker(s) that hold it (added to the watchlist if missing); no longer held
    -> H cleared, for stocks the ledger or Upstox knows. Stocks neither knows,
    and stocks with an unmatched sale in the ledger, are left alone.
    """
    from app.database import Watchlist
    from app.services.holdings import mark_as_holding
    pos = pos or positions(db)
    c = pos["compute"]
    held: Dict[str, dict] = {}
    for p in pos["positions"]:
        sym = (p.get("symbol") or "").upper()
        if sym and p["qty"] > EPS:
            held[sym] = p
    known = {(n.get("symbol") or "").upper() for n in c["names"].values()}
    incomplete = {(c["names"].get(u["key"], {}).get("symbol") or "").upper() for u in c["unmatched"]}
    changed = 0
    for sym, p in held.items():
        row = db.query(Watchlist).filter(Watchlist.symbol == sym).first()
        if row:
            if not row.is_holding or row.broker != p["broker"]:
                row.is_holding, row.broker = True, p["broker"]
                changed += 1
        elif mark_as_holding(db, sym, p.get("instrument_key") or "", sym, broker=p["broker"]):
            changed += 1
    for sym in known - set(held) - incomplete:
        row = db.query(Watchlist).filter(Watchlist.symbol == sym).first()
        if row and row.is_holding:
            row.is_holding, row.broker = False, None
            changed += 1
    db.commit()
    return {"changed": changed}
