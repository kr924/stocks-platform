"""
Weekly rebuild of the NSE/BSE symbol registry.

The registry is what lets one earnings event filed on both exchanges be
recognised as one event. When a listing is missing from it, `resolve()` falls
back to whatever identifier the exchange happened to send — a bare scrip code
from BSE, a ticker from NSE — and those two share nothing, so `_claim_result_key`
finds no collision and the same result raises two order prompts. ARDEE / 544860
did exactly that on 25 Aug 2026, 13 days after the company listed.

So the registry going stale is not a display problem, it is a correctness
problem with a fuse the length of the gap between a listing and its first
results filing. This job keeps that gap covered.

Three upstream lists, joined on ISIN:

    NSE main board   nsearchives .../content/equities/EQUITY_L.csv
    NSE SME (Emerge) nsearchives .../emerge/corporates/content/SME_EQUITY_L.csv
    BSE, all groups  api.bseindia.com .../ListofScripData

ISIN is the join because it is the only identifier both exchanges publish, and
it is unique within each of the three lists. One BSE row currently carries no
ISIN at all; a listing that cannot be joined to its counterpart is exactly the
row this registry has no use for, so it is dropped and counted.

The BSE call needs no group filter: its Equity segment already includes the SME
platform (groups M / MT / MS, ~520 scrips). The NSE SME list is separate, and
spells its columns differently — `NAME_OF_COMPANY` with underscores, and a
trailing comma on every row — so the reader normalises headers rather than
indexing them by name.
"""
import csv
import io
import json
import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import requests

logger = logging.getLogger("app.registry_builder")

NSE_EQUITY_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
NSE_SME_URL = "https://nsearchives.nseindia.com/emerge/corporates/content/SME_EQUITY_L.csv"
BSE_SCRIPS_URL = (
    "https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w"
    "?Group=&Scripcode=&industry=&segment=Equity&status=Active"
)

# Browser-shaped headers. Both archives refuse a bare client, and NSE wants a
# Referer on its own domain.
_NSE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "text/csv,application/csv,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}
_BSE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.bseindia.com/corporates/List_Scrips.html",
    "Origin": "https://www.bseindia.com",
}

FETCH_TIMEOUT = 60

# Floors below which a fetch is treated as failed rather than as news. These are
# not tuning knobs — they are the difference between "BSE delisted half the
# market this week" and "BSE answered 200 with a truncated body", and only the
# second has ever happened. Set well under today's counts (2,557 / 565 / 4,976)
# so a genuine week of delistings cannot trip them.
MIN_NSE_MAIN_ROWS = 2000
MIN_NSE_SME_ROWS = 300
MIN_BSE_ROWS = 4000

# A rebuild may not shrink the registry by more than this fraction. The registry
# only ever grows in normal weeks; a sudden contraction means an upstream list
# came back partial in a way the per-source floors did not catch.
MAX_SHRINK_RATIO = 0.05

SETTING_LAST_BUILT = "symbol_registry_last_built"
SETTING_LAST_RESULT = "symbol_registry_last_result"


class RegistryFetchError(RuntimeError):
    """An upstream list was missing, short, or unparseable."""


# ─── Fetchers ───────────────────────────────────────────────────────────────

def _normalise_header(name: str) -> str:
    """`  NAME OF COMPANY ` and `NAME_OF_COMPANY` are the same column."""
    return (name or "").strip().upper().replace(" ", "_")


def _fetch_nse_csv(url: str, min_rows: int, label: str) -> List[dict]:
    """
    One NSE equity list, as rows keyed by normalised header.

    The two lists differ in header spelling and the SME file ends every row with
    a trailing comma, which csv turns into an empty extra column. Normalising
    the headers and ignoring the blank one keeps a single reader for both.
    """
    resp = requests.get(url, headers=_NSE_HEADERS, timeout=FETCH_TIMEOUT)
    resp.raise_for_status()
    text = resp.content.decode("utf-8-sig", errors="replace")
    rows = []
    for raw in csv.DictReader(io.StringIO(text)):
        row = {_normalise_header(k): (v or "").strip()
               for k, v in raw.items() if _normalise_header(k)}
        if row.get("SYMBOL"):
            rows.append(row)
    if len(rows) < min_rows:
        raise RegistryFetchError(
            f"{label} returned {len(rows)} rows, under the floor of {min_rows} — "
            "treating as a failed fetch, not as a shrunken market."
        )
    logger.info(f"[REGISTRY] {label}: {len(rows)} listings.")
    return rows


def fetch_nse_main() -> List[dict]:
    return _fetch_nse_csv(NSE_EQUITY_URL, MIN_NSE_MAIN_ROWS, "NSE main board")


def fetch_nse_sme() -> List[dict]:
    return _fetch_nse_csv(NSE_SME_URL, MIN_NSE_SME_ROWS, "NSE SME (Emerge)")


def fetch_bse_scrips() -> List[dict]:
    """
    Every active BSE equity scrip.

    Deliberately calls BSE directly rather than through `BSESession`. That class
    fronts the announcement endpoints with a Cloudflare worker whose failure mode
    is a success-shaped one — HTTP 200 carrying "No Record Found!" — and this is
    a weekly job with no latency budget to defend, so the extra hop buys nothing
    and risks quietly emptying the registry.
    """
    resp = requests.get(BSE_SCRIPS_URL, headers=_BSE_HEADERS, timeout=FETCH_TIMEOUT)
    resp.raise_for_status()
    try:
        data = resp.json()
    except Exception:
        raise RegistryFetchError(
            f"BSE scrip list was not JSON: {resp.text[:200]!r}"
        )
    if not isinstance(data, list):
        raise RegistryFetchError(f"BSE scrip list was {type(data).__name__}, expected a list.")
    rows = [r for r in data if isinstance(r, dict) and str(r.get("SCRIP_CD") or "").strip()]
    if len(rows) < MIN_BSE_ROWS:
        raise RegistryFetchError(
            f"BSE returned {len(rows)} scrips, under the floor of {MIN_BSE_ROWS} — "
            "treating as a failed fetch, not as a shrunken market."
        )
    logger.info(f"[REGISTRY] BSE active equity: {len(rows)} scrips.")
    return rows


# ─── Join ───────────────────────────────────────────────────────────────────

def build_rows(nse_main: List[dict], nse_sme: List[dict],
               bse: List[dict]) -> Tuple[List[dict], Dict[str, int]]:
    """
    Join the three lists on ISIN into one row per listing.

    Returns the rows plus a count of what was dropped, so the caller can log a
    rebuild that silently discarded a chunk of the market rather than reporting
    a clean success.
    """
    by_isin: Dict[str, dict] = {}
    dropped = {"nse_no_isin": 0, "bse_no_isin": 0}

    for row in list(nse_main) + list(nse_sme):
        isin = (row.get("ISIN_NUMBER") or "").strip().upper()
        symbol = (row.get("SYMBOL") or "").strip().upper()
        if not isin:
            dropped["nse_no_isin"] += 1
            continue
        entry = by_isin.setdefault(isin, {"isin": isin})
        entry["nse_symbol"] = symbol
        # NSE's company name is the full registered one; keep it as the default
        # and let BSE's Issuer_Name fill in for a BSE-only listing.
        entry.setdefault("company_name", (row.get("NAME_OF_COMPANY") or "").strip())

    for row in bse:
        isin = str(row.get("ISIN_NUMBER") or "").strip().upper()
        if not isin:
            dropped["bse_no_isin"] += 1
            continue
        entry = by_isin.setdefault(isin, {"isin": isin})
        entry["bse_scrip_cd"] = str(row.get("SCRIP_CD") or "").strip()
        entry["bse_scrip_id"] = str(row.get("scrip_id") or "").strip().upper()
        name = str(row.get("Issuer_Name") or row.get("Scrip_Name") or "").strip()
        if not entry.get("company_name"):
            entry["company_name"] = name

    out = []
    for isin, entry in by_isin.items():
        has_nse = bool(entry.get("nse_symbol"))
        has_bse = bool(entry.get("bse_scrip_cd"))
        out.append({
            "isin": isin,
            "company_name": entry.get("company_name", "")[:250],
            "exchange_listing": ("both nse/bse" if has_nse and has_bse
                                 else "nse" if has_nse else "bse"),
            "nse_symbol": entry.get("nse_symbol", ""),
            "bse_scrip_cd": entry.get("bse_scrip_cd", ""),
            "bse_scrip_id": entry.get("bse_scrip_id", ""),
        })
    out.sort(key=lambda r: r["isin"])
    return out, dropped


# ─── Apply ──────────────────────────────────────────────────────────────────

def _get_setting(db, key: str) -> Optional[str]:
    from app.database import SystemSetting
    row = db.query(SystemSetting).filter(SystemSetting.key == key).first()
    return row.value if row else None


def _set_setting(db, key: str, value: str) -> None:
    from app.database import SystemSetting
    row = db.query(SystemSetting).filter(SystemSetting.key == key).first()
    if row:
        row.value = value
        row.updated_at = datetime.utcnow()
    else:
        db.add(SystemSetting(key=key, value=value))


def rebuild_symbol_registry(db, force: bool = False) -> dict:
    """
    Fetch all three lists, join them, and replace the registry — or change
    nothing at all.

    All-or-nothing on purpose. If only NSE answered, applying the partial result
    would drop every BSE-only listing — around 2,500 companies — and each of them
    would then start resolving to a bare scrip code, which is the very failure
    this job exists to prevent. A partial fetch is a run to retry tomorrow, not a
    registry to install.

    `force` skips only the shrink guard, for the case where the market genuinely
    did contract and an operator has looked at the numbers and accepted them.
    The per-source floors always apply.

    Returns a summary dict; never raises for an upstream failure.
    """
    from app.database import SymbolRegistryEntry
    from app.services import symbol_registry

    started = datetime.utcnow()
    summary = {"ok": False, "at": started.isoformat() + "Z"}

    # Fetch everything before touching anything.
    try:
        nse_main = fetch_nse_main()
        nse_sme = fetch_nse_sme()
        bse = fetch_bse_scrips()
    except Exception as e:
        summary["error"] = str(e)
        logger.error(f"[REGISTRY] rebuild aborted, registry left untouched: {e}")
        _record_result(db, summary)
        return summary

    rows, dropped = build_rows(nse_main, nse_sme, bse)
    summary.update({
        "nse_main": len(nse_main), "nse_sme": len(nse_sme), "bse": len(bse),
        "rows": len(rows), "dropped_no_isin": dropped,
    })
    if dropped["nse_no_isin"] or dropped["bse_no_isin"]:
        logger.info(
            f"[REGISTRY] dropped listings with no ISIN — NSE {dropped['nse_no_isin']}, "
            f"BSE {dropped['bse_no_isin']}. They cannot be joined across exchanges."
        )

    try:
        previous = db.query(SymbolRegistryEntry).count()
    except Exception as e:
        summary["error"] = f"could not read the current registry: {e}"
        logger.error(f"[REGISTRY] {summary['error']}")
        _record_result(db, summary)
        return summary
    summary["previous_rows"] = previous

    if previous and len(rows) < previous * (1 - MAX_SHRINK_RATIO) and not force:
        summary["error"] = (
            f"rebuild would shrink the registry from {previous} to {len(rows)} rows "
            f"(more than {MAX_SHRINK_RATIO:.0%}); refusing. Re-run with force=True "
            "if the contraction is real."
        )
        logger.error(f"[REGISTRY] {summary['error']}")
        _record_result(db, summary)
        return summary

    # Replace in one transaction: a half-written registry is worse than a stale
    # one, because the missing half resolves to bare identifiers.
    try:
        db.query(SymbolRegistryEntry).delete(synchronize_session=False)
        db.bulk_save_objects([
            SymbolRegistryEntry(
                isin=r["isin"], company_name=r["company_name"],
                exchange_listing=r["exchange_listing"], nse_symbol=r["nse_symbol"],
                bse_scrip_cd=r["bse_scrip_cd"], bse_scrip_id=r["bse_scrip_id"],
                updated_at=started,
            )
            for r in rows
        ])
        _set_setting(db, SETTING_LAST_BUILT, started.strftime("%Y-%m-%d"))
        db.commit()
    except Exception as e:
        db.rollback()
        summary["error"] = f"write failed, previous registry intact: {e}"
        logger.error(f"[REGISTRY] {summary['error']}")
        _record_result(db, summary)
        return summary

    # The index is read once at startup and never re-read, so without this the
    # process would go on serving the old map while the new rows sit unused.
    summary["indexed"] = symbol_registry.reload()
    summary["added"] = len(rows) - previous
    summary["ok"] = True
    logger.info(
        f"[REGISTRY] rebuilt: {previous} -> {len(rows)} listings "
        f"({summary['added']:+d}); {summary['indexed']} indexed."
    )
    _record_result(db, summary)
    return summary


def _record_result(db, summary: dict) -> None:
    """Keep the last outcome where an operator can read it without the logs."""
    try:
        _set_setting(db, SETTING_LAST_RESULT, json.dumps(summary)[:4000])
        db.commit()
    except Exception:
        db.rollback()


def last_built_date(db) -> Optional[str]:
    """
    The IST date of the last successful rebuild, as YYYY-MM-DD.

    Persisted rather than held in memory because the scheduler's in-process
    `last_run` guards reset on restart. That is harmless for a daily job, which
    simply runs again the same day, but a weekly job guarded that way would
    re-run on every container restart between now and next Sunday.
    """
    return _get_setting(db, SETTING_LAST_BUILT)
