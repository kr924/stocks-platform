"""
Day-change quotes from a public source, for when Upstox cannot answer.

Upstox is the platform's only price feed and it is the right one while the
market is open: it is the account that trades, and its quotes are the ones an
order is priced against. But it needs an authorised session, and after the
close it is neither available nor the point — a settled day's change is public
information that any source can report.

So this exists for exactly two cases, both of them in the PDF export:

    Upstox is not connected at all
    the market has closed and the day's change is a settled number

Yahoo's chart endpoint is used because it needs no key, returns the previous
close alongside the last price in one call, and — unlike NSE's own quote
endpoint — does not demand a cookie handshake per request.

**Tickers come from the symbol registry, not from the display symbol.** Yahoo
keys NSE by ticker (`RELIANCE.NS`) and BSE by *scrip id* (`VIJIFIN.BO`), never
by the numeric scrip code: `500325.BO` resolves to an empty placeholder rather
than an error, which is the failure shape that hides itself. The registry is
the only place holding both identifiers for one company.

Never used for the "since result" figure. That baseline was captured from
Upstox at the moment the filing landed, and comparing it against a different
venue's price would produce a move that no single tape ever showed.
"""
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Optional

import requests

logger = logging.getLogger("app.public_quotes")

_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "application/json",
}
_TIMEOUT = 12
# Modest: a PDF covers a few dozen rows, and a public endpoint asked for
# hundreds of quotes at once starts refusing them.
_MAX_WORKERS = 6


def ticker_for(symbol: str) -> Optional[str]:
    """
    The Yahoo ticker for one of our symbols, or None if it cannot be resolved.

    NSE listing wins: it is the more liquid line for any dual-listed company,
    and it is the venue the rest of the platform prices against.
    """
    try:
        from app.services.symbol_registry import lookup
    except Exception:
        return None

    sym = (symbol or "").strip().upper()
    if not sym:
        return None
    rec = lookup(nse_symbol=sym, scrip_id=sym, scrip_cd=sym if sym.isdigit() else "")
    if rec:
        if rec.nse_symbol:
            return f"{rec.nse_symbol}.NS"
        if rec.bse_scrip_id:
            return f"{rec.bse_scrip_id}.BO"
        # A bare scrip code is not a Yahoo ticker. Returning one would look like
        # a working lookup and quietly report no price for the company.
        return None
    # Unknown to the registry: a plain alphabetic symbol is almost certainly an
    # NSE ticker, and a wrong guess costs one empty quote rather than a wrong one.
    return f"{sym}.NS" if sym.isalpha() else None


def _fetch_one(ticker: str) -> Optional[dict]:
    try:
        resp = requests.get(_CHART_URL.format(ticker=ticker),
                            params={"interval": "1d", "range": "1d"},
                            headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        chart = (resp.json() or {}).get("chart") or {}
        if chart.get("error"):
            return None
        results = chart.get("result") or []
        if not results:
            return None
        meta = results[0].get("meta") or {}
        last = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        # An unknown ticker comes back as a well-formed body full of nulls on a
        # placeholder exchange rather than as a 404, so absence of a price is
        # the only reliable way to tell it apart from a real answer.
        if last is None or not prev:
            return None
        return {
            "last_price": round(float(last), 2),
            "prev_close": round(float(prev), 2),
            "change_pct": round((float(last) - float(prev)) / float(prev) * 100, 2),
            "source": "yahoo",
            "exchange": meta.get("fullExchangeName"),
        }
    except Exception as e:
        logger.debug(f"Public quote failed for {ticker}: {e}")
        return None


def fetch_day_changes(symbols) -> Dict[str, dict]:
    """
    Day change for each symbol, keyed by the symbol as passed in.

    Symbols that cannot be resolved or priced are simply absent from the result;
    the caller renders those as NA rather than as zero, which is the same rule
    every other uncertain figure in this codebase follows.
    """
    wanted = {}
    for s in symbols:
        key = (s or "").strip().upper()
        if not key or key in wanted:
            continue
        t = ticker_for(key)
        if t:
            wanted[key] = t
    if not wanted:
        return {}

    out: Dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        futures = {pool.submit(_fetch_one, t): sym for sym, t in wanted.items()}
        for fut in as_completed(futures):
            sym = futures[fut]
            try:
                data = fut.result()
            except Exception:
                data = None
            if data:
                data["ticker"] = wanted[sym]
                out[sym] = data
    logger.info(f"Public quotes: {len(out)}/{len(wanted)} symbols priced.")
    return out
