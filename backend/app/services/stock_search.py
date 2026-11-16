"""
Search box for the Stocks Tracker: every NSE and BSE listing, not only NSE.

The box used to call Upstox's instrument search with exchanges=NSE, ten
results, in Upstox's own order (16 Nov 2026). So a BSE-only company (about
2,400 of them, many of the small caps the results feed brings up) could not be
found at all, ETFs crowded out companies, and without an Upstox login the
search fell back to the Nifty 50.

It now searches the symbol registry, which joins NSE main board, NSE SME and
BSE on ISIN with full company names, and then the Upstox instrument dumps for
what the registry does not carry (ETFs and other funds). A dual-listed company
appears once, on NSE, marked "NSE · BSE". The quote key is resolved from the
exchange dumps, never synthesised.
"""
import re
import threading
from typing import Dict, List, Optional

_cache: Dict[str, object] = {}
_lock = threading.Lock()

_FUND = re.compile(r"(ETF|BEES|\bAMC\s*-|AMC-|\bGILT|\bLIQUID|\bFUND\b)")


def _rows() -> List[dict]:
    from app.services import symbol_registry
    from app.main import get_nse_equities, get_bse_equities
    recs = symbol_registry.all_records()
    marker = (id(symbol_registry._by_isin), len(recs))
    with _lock:
        if _cache.get("marker") == marker and _cache.get("rows"):
            return _cache["rows"]
    rows, isins = [], set()
    for r in recs:
        listing = []
        if r.nse_symbol:
            listing.append("NSE")
        if r.bse_scrip_cd or r.bse_scrip_id:
            listing.append("BSE")
        rows.append({
            "symbol": r.display_symbol, "name": r.company_name or r.display_symbol,
            "nse_symbol": r.nse_symbol, "scrip_cd": r.bse_scrip_cd, "scrip_id": r.bse_scrip_id,
            "isin": r.isin, "exchange": " · ".join(listing) or "NSE",
            "fund": bool(_FUND.search(f"{r.display_symbol} {r.company_name}".upper())),
        })
        if r.isin:
            isins.add(r.isin)
    # Funds and anything else the registry does not list, from the dumps.
    for exch, eqs in (("NSE", get_nse_equities()), ("BSE", get_bse_equities())):
        for e in eqs:
            key = e.get("key") or ""
            isin = e.get("isin") or (key.split("|", 1)[1] if "|" in key else "")
            if not key or not isin or isin in isins:
                continue
            isins.add(isin)
            sym, name = e.get("symbol") or "", e.get("name") or ""
            rows.append({
                "symbol": sym, "name": name or sym, "nse_symbol": sym if exch == "NSE" else "",
                "scrip_cd": e.get("token", "") if exch == "BSE" else "", "scrip_id": sym if exch == "BSE" else "",
                "isin": isin, "exchange": exch, "key": key,
                "fund": bool(_FUND.search(f"{sym} {name}".upper())),
            })
    for row in rows:
        row["_sym"] = (row["symbol"] or "").upper()
        row["_alt"] = {(row.get("nse_symbol") or "").upper(), (row.get("scrip_id") or "").upper()} - {""}
        row["_code"] = row.get("scrip_cd") or ""
        row["_name"] = (row["name"] or "").upper()
        row["_words"] = re.split(r"[^A-Z0-9&]+", row["_name"])
    with _lock:
        _cache["marker"], _cache["rows"] = marker, rows
    return rows


def _score(row: dict, q: str) -> Optional[float]:
    # A BSE scrip code is six digits; "5" or "53" would match half the board.
    code = row["_code"]
    if q == row["_sym"] or q in row["_alt"] or (code and q == code):
        s = 0
    elif row["_sym"].startswith(q) or any(a.startswith(q) for a in row["_alt"]):
        s = 1
    elif code and len(q) >= 4 and code.startswith(q):
        s = 1.2
    elif row["_name"].startswith(q):
        s = 2
    elif any(w.startswith(q) for w in row["_words"] if w):
        s = 3
    elif q in row["_sym"]:
        s = 4
    elif len(q) >= 3 and q in row["_name"]:
        s = 5
    else:
        return None
    return s + (0.5 if row["fund"] else 0)


def _key_for(row: dict) -> str:
    if row.get("key"):
        return row["key"]
    from app.main import resolve_instrument_keys
    for handle in (row.get("nse_symbol"), row.get("scrip_cd"), row.get("scrip_id")):
        if handle:
            ks = resolve_instrument_keys(handle)
            if ks:
                return ks[0]
    if row.get("isin"):
        return f"{'NSE' if row.get('nse_symbol') else 'BSE'}_EQ|{row['isin']}"
    return ""


def search(query: str, limit: int = 15) -> List[dict]:
    q = (query or "").strip().upper()
    if not q:
        return []
    hits = []
    for row in _rows():
        s = _score(row, q)
        if s is not None:
            hits.append((s, len(row["_sym"]), row["_sym"], row))
    hits.sort(key=lambda h: h[:3])
    out = []
    for _, _, _, row in hits:
        key = _key_for(row)
        if not key:
            continue
        out.append({"symbol": row["symbol"], "name": row["name"], "key": key,
                    "exchange": row["exchange"], "isin": row.get("isin") or None,
                    "scrip_code": row.get("scrip_cd") or None})
        if len(out) >= limit:
            break
    return out
