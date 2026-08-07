"""
Canonical NSE/BSE symbol registry.

Backed by data/symbol_registry.csv, derived from the combined NSE+BSE equity
list. It replaces the previous approach of fuzzy-matching company names against
the Upstox instrument dump, which was unreliable in both directions: BSE's
current announcements endpoint returns no ISIN, and the NSE dump truncates
company names to ~24 characters.

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
from typing import Dict, Optional

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


def _load() -> None:
    """Load the registry CSV once. Safe to call from multiple threads."""
    global _loaded
    if _loaded:
        return
    with _load_lock:
        if _loaded:
            return
        if not os.path.exists(_CSV_PATH):
            logger.error(
                f"Symbol registry CSV missing at {_CSV_PATH}. "
                "Symbols will fall back to raw exchange identifiers."
            )
            _loaded = True
            return
        try:
            with open(_CSV_PATH, "r", encoding="utf-8", newline="") as f:
                for row in csv.DictReader(f):
                    rec = SymbolRecord(
                        isin=(row.get("ISIN") or "").strip().upper(),
                        company_name=(row.get("COMPANY_NAME") or "").strip(),
                        exchange_listing=(row.get("EXCHANGE_LISTING") or "").strip(),
                        nse_symbol=(row.get("NSE_SYMBOL") or "").strip().upper(),
                        bse_scrip_cd=(row.get("BSE_SCRIP_CD") or "").strip(),
                        bse_scrip_id=(row.get("BSE_SCRIP_ID") or "").strip().upper(),
                    )
                    if rec.bse_scrip_cd:
                        _by_scrip_cd.setdefault(rec.bse_scrip_cd, rec)
                    if rec.bse_scrip_id:
                        _by_scrip_id.setdefault(rec.bse_scrip_id, rec)
                    if rec.nse_symbol:
                        _by_nse_symbol.setdefault(rec.nse_symbol, rec)
                    if rec.isin:
                        _by_isin.setdefault(rec.isin, rec)
            logger.info(
                f"Symbol registry loaded: {len(_by_scrip_cd)} BSE codes, "
                f"{len(_by_nse_symbol)} NSE symbols, {len(_by_isin)} ISINs"
            )
        except Exception as e:
            logger.error(f"Failed to load symbol registry: {e}")
        _loaded = True


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
