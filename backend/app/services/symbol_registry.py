"""
Canonical NSE/BSE symbol registry.

Backed by the `symbol_registry` table, derived from the combined NSE+BSE equity
list and rebuilt weekly by `services.registry_builder`. It replaces the previous
approach of fuzzy-matching company names against the Upstox instrument dump,
which was unreliable in both directions: BSE's current announcements endpoint
returns no ISIN, and the NSE dump truncates company names to ~24 characters.

data/symbol_registry.csv is the checked-in seed, used to fill the table the
first time it is found empty and as the fallback when the table cannot be read.
The live copy lives in the database because the CSV is baked into the Docker
image from git, so a refresh written to the file would not survive a redeploy.

A stale registry is not a cosmetic problem. A listing missing from it resolves
to a bare BSE scrip code on one exchange and to an NSE ticker on the other,
and those two share no identifier at all, so cross-exchange dedup cannot see
them as one event and the result is prompted twice. That is what ARDEE / 544860
did on 25 Aug 2026, 13 days after the company listed.

Each listing resolves to one record carrying every identifier we need:

    ISIN            the authoritative cross-exchange join
    NSE_SYMBOL      the NSE ticker, when the company is listed there
    BSE_SCRIP_CD    the numeric code BSE's API returns
    BSE_SCRIP_ID    the readable BSE ticker to display
    COMPANY_NAME    the display name

`display_symbol` prefers the NSE ticker and falls back to the BSE scrip id, so a
BSE-only listing shows "3BFILMS" rather than the meaningless code "544412".
"""
import csv
import logging
import os
import threading
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("app.symbol_registry")

_CSV_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "symbol_registry.csv",
)


@dataclass(frozen=True)
class SymbolRecord:
    isin: str
    company_name: str
    exchange_listing: str
    nse_symbol: str
    bse_scrip_cd: str
    bse_scrip_id: str

    @property
    def display_symbol(self) -> str:
        """Ticker to show in the UI and alerts — never a bare numeric code."""
        return self.nse_symbol or self.bse_scrip_id or self.bse_scrip_cd

    @property
    def instrument_key(self) -> str:
        """Upstox instrument key, only meaningful for NSE-listed names."""
        if self.nse_symbol and self.isin:
            return f"NSE_EQ|{self.isin}"
        if self.nse_symbol:
            return f"NSE_EQ|{self.nse_symbol}"
        return ""

    def as_dict(self) -> dict:
        return {
            "symbol": self.display_symbol,
            "company_name": self.company_name,
            "nse_symbol": self.nse_symbol or None,
            "bse_scrip_id": self.bse_scrip_id or None,
            "bse_scrip_cd": self.bse_scrip_cd or None,
            "isin": self.isin or None,
            "exchange_listing": self.exchange_listing or None,
        }


_by_scrip_cd: Dict[str, SymbolRecord] = {}
_by_scrip_id: Dict[str, SymbolRecord] = {}
_by_nse_symbol: Dict[str, SymbolRecord] = {}
_by_isin: Dict[str, SymbolRecord] = {}
_loaded = False
_load_lock = threading.Lock()


def _read_csv_seed() -> List[SymbolRecord]:
    """The checked-in CSV, as records. Empty list when it is missing."""
    if not os.path.exists(_CSV_PATH):
        logger.error(
            f"Symbol registry CSV missing at {_CSV_PATH}. "
            "Symbols will fall back to raw exchange identifiers."
        )
        return []
    out = []
    try:
        with open(_CSV_PATH, "r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                out.append(SymbolRecord(
                    isin=(row.get("ISIN") or "").strip().upper(),
                    company_name=(row.get("COMPANY_NAME") or "").strip(),
                    exchange_listing=(row.get("EXCHANGE_LISTING") or "").strip(),
                    nse_symbol=(row.get("NSE_SYMBOL") or "").strip().upper(),
                    bse_scrip_cd=(row.get("BSE_SCRIP_CD") or "").strip(),
                    bse_scrip_id=(row.get("BSE_SCRIP_ID") or "").strip().upper(),
                ))
    except Exception as e:
        logger.error(f"Failed to read the symbol registry CSV: {e}")
    return out


def _read_db_rows() -> List[SymbolRecord]:
    """
    The `symbol_registry` table, as records.

    Returns an empty list both when the table is empty and when it cannot be
    read at all. The caller treats those identically — fall back to the CSV —
    because either way there is nothing here to serve lookups from.
    """
    try:
        from app.database import SessionLocal, SymbolRegistryEntry
    except Exception as e:
        logger.error(f"Symbol registry: database unavailable ({e}); using the CSV seed.")
        return []
    db = SessionLocal()
    try:
        return [
            SymbolRecord(
                isin=(r.isin or "").strip().upper(),
                company_name=(r.company_name or "").strip(),
                exchange_listing=(r.exchange_listing or "").strip(),
                nse_symbol=(r.nse_symbol or "").strip().upper(),
                bse_scrip_cd=(r.bse_scrip_cd or "").strip(),
                bse_scrip_id=(r.bse_scrip_id or "").strip().upper(),
            )
            for r in db.query(SymbolRegistryEntry).all()
        ]
    except Exception as e:
        logger.error(f"Failed to read the symbol_registry table: {e}")
        return []
    finally:
        db.close()


def _index(records: List[SymbolRecord]) -> Tuple[dict, dict, dict, dict]:
    """
    Build the four lookup maps from a list of records.

    Built into fresh dicts and returned rather than mutated in place, so that a
    reload can swap a complete index in under the lock. A caller resolving a
    symbol on the announcement path must never observe a half-populated map.
    """
    by_scrip_cd, by_scrip_id, by_nse_symbol, by_isin = {}, {}, {}, {}
    for rec in records:
        if rec.bse_scrip_cd:
            by_scrip_cd.setdefault(rec.bse_scrip_cd, rec)
        if rec.bse_scrip_id:
            by_scrip_id.setdefault(rec.bse_scrip_id, rec)
        if rec.nse_symbol:
            by_nse_symbol.setdefault(rec.nse_symbol, rec)
        if rec.isin:
            by_isin.setdefault(rec.isin, rec)
    return by_scrip_cd, by_scrip_id, by_nse_symbol, by_isin


def _seed_table_from_csv(records: List[SymbolRecord]) -> None:
    """
    Fill an empty `symbol_registry` table from the CSV, once.

    Only ever called when the table read came back empty, so it cannot overwrite
    a rebuild. A failure is not fatal: the records are already indexed in memory
    and the next weekly rebuild populates the table properly.
    """
    if not records:
        return
    try:
        from app.database import SessionLocal, SymbolRegistryEntry
        db = SessionLocal()
        try:
            db.bulk_save_objects([
                SymbolRegistryEntry(
                    isin=r.isin, company_name=r.company_name,
                    exchange_listing=r.exchange_listing, nse_symbol=r.nse_symbol,
                    bse_scrip_cd=r.bse_scrip_cd, bse_scrip_id=r.bse_scrip_id,
                )
                for r in records if r.isin
            ])
            db.commit()
            logger.info(f"Seeded the symbol_registry table with {len(records)} rows from the CSV.")
        except Exception as e:
            db.rollback()
            logger.warning(f"Could not seed the symbol_registry table: {e}")
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"Could not seed the symbol_registry table: {e}")


def _load() -> None:
    """Load the registry once. Safe to call from multiple threads."""
    global _loaded, _by_scrip_cd, _by_scrip_id, _by_nse_symbol, _by_isin
    if _loaded:
        return
    with _load_lock:
        if _loaded:
            return
        records = _read_db_rows()
        source = "database"
        if not records:
            records = _read_csv_seed()
            source = "CSV seed"
            _seed_table_from_csv(records)
        _by_scrip_cd, _by_scrip_id, _by_nse_symbol, _by_isin = _index(records)
        _loaded = True
        logger.info(
            f"Symbol registry loaded from {source}: {len(_by_scrip_cd)} BSE codes, "
            f"{len(_by_nse_symbol)} NSE symbols, {len(_by_isin)} ISINs"
        )


def reload() -> int:
    """
    Re-read the registry from the database and swap the index in.

    Without this a rebuild changes nothing until the container restarts: `_load`
    sets `_loaded` on the first call and returns early on every later one, so
    the process would go on serving the map it read at startup while the fresh
    rows sat in the table unread. Returns the number of listings now indexed.

    An empty read leaves the current index alone. The rebuild has already
    refused to write a short result, so nothing here should ever see one, but
    swapping in an empty map would blank every symbol on the platform and that
    is not a failure worth risking on a defensive path.
    """
    global _loaded, _by_scrip_cd, _by_scrip_id, _by_nse_symbol, _by_isin
    records = _read_db_rows()
    if not records:
        logger.warning("Symbol registry reload found no rows — keeping the current index.")
        return len(_by_isin)
    with _load_lock:
        _by_scrip_cd, _by_scrip_id, _by_nse_symbol, _by_isin = _index(records)
        _loaded = True
    logger.info(f"Symbol registry reloaded: {len(_by_isin)} listings.")
    return len(_by_isin)


def lookup(
    scrip_cd: str = "",
    nse_symbol: str = "",
    isin: str = "",
    scrip_id: str = "",
) -> Optional[SymbolRecord]:
    """
    Find a listing by any identifier, most authoritative first.

    Returns None when nothing matches, so callers can fall back to whatever the
    exchange gave them.
    """
    _load()

    isin = (isin or "").strip().upper()
    if isin and isin in _by_isin:
        return _by_isin[isin]

    scrip_cd = str(scrip_cd or "").strip()
    if scrip_cd and scrip_cd in _by_scrip_cd:
        return _by_scrip_cd[scrip_cd]

    nse_symbol = (nse_symbol or "").strip().upper()
    if nse_symbol and nse_symbol in _by_nse_symbol:
        return _by_nse_symbol[nse_symbol]

    scrip_id = (scrip_id or "").strip().upper()
    if scrip_id and scrip_id in _by_scrip_id:
        return _by_scrip_id[scrip_id]

    # A numeric-looking "symbol" is a BSE scrip code that reached us mislabelled.
    if nse_symbol.isdigit() and nse_symbol in _by_scrip_cd:
        return _by_scrip_cd[nse_symbol]

    return None


def resolve(scrip_cd: str = "", nse_symbol: str = "", isin: str = "",
            scrip_id: str = "") -> dict:
    """
    Resolve identifiers to a display-ready dict.

    Always returns a usable dict; unknown listings echo back what was supplied
    rather than dropping the announcement.
    """
    rec = lookup(scrip_cd=scrip_cd, nse_symbol=nse_symbol, isin=isin, scrip_id=scrip_id)
    if rec:
        return rec.as_dict()

    fallback = (nse_symbol or scrip_id or scrip_cd or "").strip().upper()
    return {
        "symbol": fallback,
        "company_name": "",
        "nse_symbol": (nse_symbol or "").strip().upper() or None,
        "bse_scrip_id": (scrip_id or "").strip().upper() or None,
        "bse_scrip_cd": str(scrip_cd or "").strip() or None,
        "isin": (isin or "").strip().upper() or None,
        "exchange_listing": None,
    }


def company_name_for(symbol: str) -> str:
    """Best-effort company name for a display symbol."""
    rec = lookup(nse_symbol=symbol, scrip_id=symbol)
    return rec.company_name if rec else ""


def stats() -> dict:
    _load()
    return {
        "bse_scrip_codes": len(_by_scrip_cd),
        "bse_scrip_ids": len(_by_scrip_id),
        "nse_symbols": len(_by_nse_symbol),
        "isins": len(_by_isin),
        "csv_path": _CSV_PATH,
        "loaded": _loaded,
    }
