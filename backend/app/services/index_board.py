"""
The index and commodity strip along the top of the dashboard.

Three groups on one board: the headline indices, the sectoral ones, and MCX
commodities. They are quoted the same way — through the shared Upstox feed, so
the strip costs no requests of its own beyond registering its keys with the
refresher every panel already reads.

Two things here are not obvious.

**Commodity keys roll.** There is no instrument called "GOLD" on MCX; there are
several hundred contracts with expiries. A dashboard tile wants the front month,
which changes every few weeks, so the key is resolved from the exchange's own
instrument dump each day rather than written down. A hardcoded key works right
up until expiry and then quietly quotes a dead contract.

**One bad key voids the whole request.** Upstox answers UDAPI1087 for a batch
containing a single unrecognised instrument, taking every good key down with it
— so a new entry is verified against the live feed before it is added here, and
the fetch degrades per-group rather than all at once.

Period changes come from daily candles. One series per instrument, fetched once
and held for the day, answers every period from 5 days to 5 years: a published
candle does not change, and asking for nine separate ranges would be nine times
the requests for the same data.
"""
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

logger = logging.getLogger("app.index_board")

IST = timezone(timedelta(hours=5, minutes=30))

# Verified against the live feed. Anything that answered "no price" is left out
# rather than shipped as a permanently empty tile.
HEADLINE = [
    ("Nifty 50", "NSE_INDEX|Nifty 50"),
    ("Sensex", "BSE_INDEX|SENSEX"),
    ("Nifty Next 50", "NSE_INDEX|Nifty Next 50"),
    ("India VIX", "NSE_INDEX|India VIX"),
]

SECTORS = [
    ("Bank", "NSE_INDEX|Nifty Bank"),
    ("IT", "NSE_INDEX|Nifty IT"),
    ("Pharma", "NSE_INDEX|Nifty Pharma"),
    ("Auto", "NSE_INDEX|Nifty Auto"),
    ("FMCG", "NSE_INDEX|Nifty FMCG"),
    ("Metal", "NSE_INDEX|Nifty Metal"),
    ("Financial Services", "NSE_INDEX|Nifty Fin Service"),
    ("Realty", "NSE_INDEX|Nifty Realty"),
    ("Energy", "NSE_INDEX|Nifty Energy"),
    ("Infra", "NSE_INDEX|Nifty Infra"),
    ("PSU Bank", "NSE_INDEX|Nifty PSU Bank"),
    ("Media", "NSE_INDEX|Nifty Media"),
    ("Commodities", "NSE_INDEX|Nifty Commodities"),
    ("Bankex", "BSE_INDEX|BANKEX"),
]

# Resolved to a front-month futures key at runtime. The name is what the tile
# shows; the prefix is what the contract's symbol starts with.
COMMODITIES = [
    ("Gold", "GOLD"),
    ("Silver", "SILVER"),
    ("Crude Oil", "CRUDEOIL"),
    ("Natural Gas", "NATURALGAS"),
    ("Copper", "COPPER"),
]

_MCX_DUMP = "https://assets.upstox.com/market-quote/instruments/exchange/MCX.json.gz"

# Trading days per period. Approximate on purpose: the change is measured
# against the closest candle at or before the target, so a holiday shifts the
# comparison by a day rather than losing it.
PERIODS = {
    "5d": 5, "10d": 10, "15d": 15, "1m": 21, "3m": 63,
    "6m": 126, "12m": 252, "3y": 756, "5y": 1260,
}

_commodity_keys: Dict[str, str] = {}
_commodity_day = ""
_commodity_lock = threading.Lock()

_candles: Dict[str, tuple] = {}          # key -> (fetched_at, [candles oldest first])
_CANDLE_TTL = 6 * 3600
_candle_lock = threading.Lock()


def _resolve_commodities() -> Dict[str, str]:
    """
    Front-month futures key for each commodity, refreshed once a day.

    Read from the exchange dump rather than written down: these contracts
    expire, and a key that has rolled quotes a contract nobody trades any more
    without erroring — the failure looks exactly like a quiet market.
    """
    global _commodity_keys, _commodity_day

    today = datetime.now(IST).strftime("%Y-%m-%d")
    with _commodity_lock:
        if _commodity_day == today and _commodity_keys:
            return dict(_commodity_keys)
    try:
        import gzip, json, requests
        raw = requests.get(_MCX_DUMP, timeout=60).content
        rows = json.loads(gzip.decompress(raw))
    except Exception as e:
        logger.warning(f"MCX instrument dump unavailable: {e}")
        return dict(_commodity_keys)

    now_ms = time.time() * 1000
    out = {}
    for label, prefix in COMMODITIES:
        live = [d for d in rows
                if d.get("instrument_type") == "FUT"
                and str(d.get("trading_symbol") or "").upper().startswith(prefix)
                and (d.get("expiry") or 0) > now_ms]
        live.sort(key=lambda d: d.get("expiry") or 0)
        if live:
            out[label] = live[0]["instrument_key"]
    with _commodity_lock:
        _commodity_keys, _commodity_day = out, today
    logger.info(f"[INDEX BOARD] resolved {len(out)} front-month commodity contracts.")
    return dict(out)


def _entries() -> List[dict]:
    """Every tile on the board, in display order, with its resolved key."""
    out = [{"name": n, "key": k, "group": "index"} for n, k in HEADLINE]
    out += [{"name": n, "key": k, "group": "sector"} for n, k in SECTORS]
    for label, _ in COMMODITIES:
        key = _resolve_commodities().get(label)
        if key:
            out.append({"name": label, "key": key, "group": "commodity"})
    return out


def _quote_group(feed, keys: List[str]) -> dict:
    """
    Quote a group of keys, degrading to per-key on a batch rejection.

    Upstox voids an entire request for one unrecognised instrument, so a batch
    that fails is retried key by key: a single bad entry then costs its own tile
    rather than the whole strip.
    """
    if not keys:
        return {}
    try:
        return feed.get_quotes(keys) or {}
    except Exception as e:
        logger.warning(f"[INDEX BOARD] batch quote failed ({str(e)[:90]}); retrying singly.")
        out = {}
        for k in keys:
            try:
                out.update(feed.get_quotes([k]) or {})
            except Exception:
                logger.debug(f"[INDEX BOARD] {k} could not be quoted.")
        return out


def live_board(feed) -> List[dict]:
    """Current price and move for every tile."""
    entries = _entries()
    quotes = {}
    for group in ("index", "sector", "commodity"):
        quotes.update(_quote_group(feed, [e["key"] for e in entries if e["group"] == group]))

    out = []
    for e in entries:
        q = quotes.get(e["key"]) or quotes.get(e["key"].replace("|", ":")) or {}
        last = q.get("last_price") or None
        prev = (q.get("ohlc") or {}).get("close") or None
        change = round(last - prev, 2) if last and prev else None
        # No invented fallback. A tile with no quote reads as having no quote —
        # substituting a plausible number produced a strip where every index
        # showed exactly +0.50%, which looked like data and was not.
        out.append({
            **e,
            "last_price": round(last, 2) if last else None,
            "prev_close": round(prev, 2) if prev else None,
            "change": change,
            "pct_change": (round(change / prev * 100, 2) if change is not None and prev else None),
        })
    return out


def _series(feed, key: str) -> List[list]:
    """Daily candles for one instrument, oldest first, held for six hours."""
    with _candle_lock:
        hit = _candles.get(key)
        if hit and time.time() - hit[0] < _CANDLE_TTL:
            return hit[1]

    today = datetime.now(IST)
    start = (today - timedelta(days=int(365 * 5.4))).strftime("%Y-%m-%d")
    try:
        rows = feed.get_historical_candles(key, "day", today.strftime("%Y-%m-%d"), start) or []
    except Exception as e:
        logger.debug(f"[INDEX BOARD] no candles for {key}: {e}")
        rows = []
    rows = sorted(rows, key=lambda c: str(c[0]))
    with _candle_lock:
        _candles[key] = (time.time(), rows)
    return rows


def period_board(feed, period: str) -> List[dict]:
    """
    Move over `period` for every tile, measured from daily closes.

    One series per instrument answers every period, so switching from 5 days to
    5 years costs nothing after the first look at a given instrument.
    """
    back = PERIODS.get(period)
    if not back:
        raise ValueError(f"Unknown period '{period}'")

    live = {e["name"]: e for e in live_board(feed)}
    out = []
    for e in _entries():
        rows = _series(feed, e["key"])
        cur = live.get(e["name"], {})
        now_price = cur.get("last_price")
        then = None
        if rows:
            # Close of the candle `back` sessions ago, or the oldest we hold
            # when the instrument is younger than the window.
            idx = max(0, len(rows) - 1 - back)
            then = rows[idx][4]
            if now_price is None:
                now_price = rows[-1][4]
        change = round(now_price - then, 2) if now_price and then else None
        # A futures contract has no history before it listed. Asking a
        # front-month gold contract for five years returns its own first candle
        # — a few months back — and calling that "5y" would be a number with
        # the wrong label on it, which is worse than no number. The span
        # actually covered is reported so the caller can say what it is.
        available = max(0, len(rows) - 1)
        truncated = bool(rows) and back > available
        out.append({
            **e,
            "last_price": now_price,
            "prev_close": then,
            "change": change,
            "pct_change": (round(change / then * 100, 2) if change is not None and then else None),
            "period": period,
            "sessions": min(back, available) if rows else 0,
            "truncated": truncated,
            "from_date": str(rows[max(0, len(rows) - 1 - back)][0])[:10] if rows else None,
        })
    return out
