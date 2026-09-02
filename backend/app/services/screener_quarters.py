"""
Structured latest-quarter figures from Screener.in.

ai_analyzer.fetch_screener_financials() returns prose for injection into a
prompt. The morning digest needs the same data as *values*, so the numbers our
AI read out of a filing can be placed beside an independent source and the
variance shown.

Screener publishes the quarterly table as plain HTML, so the last column of each
labelled row is the most recent quarter. All figures are in ₹ crore.
"""
import logging
import re
import threading
import time
from typing import Dict, List, Optional

import requests

logger = logging.getLogger("app.screener_quarters")

# Screener answers 429 when called back to back, and the digest walks a couple
# of hundred companies in a row. One request at a time, spaced, costs the digest
# a few minutes it has to spare at 08:00 and keeps the data complete.
_MIN_REQUEST_GAP = 0.7
_RETRY_AFTER_429 = 6.0
_request_lock = threading.Lock()
_last_request_at = 0.0


def _get(url: str) -> Optional[requests.Response]:
    """Paced GET. Retries once when Screener rate limits us."""
    global _last_request_at
    for attempt in (1, 2):
        with _request_lock:
            wait = _MIN_REQUEST_GAP - (time.monotonic() - _last_request_at)
            if wait > 0:
                time.sleep(wait)
            _last_request_at = time.monotonic()
        try:
            res = requests.get(url, headers=_HEADERS, timeout=10)
        except Exception as e:
            logger.debug(f"Screener request failed for {url}: {e}")
            return None
        if res.status_code == 429 and attempt == 1:
            time.sleep(_RETRY_AFTER_429)
            continue
        return res
    return None


# Screener publishes a quarter once; re-fetching it minutes later cannot return
# anything new, and at 1.5s a request the digest's few hundred companies are the
# difference between a page that loads and one that times out.
_QUARTER_CACHE_TTL = 6 * 3600
_quarter_cache: Dict[str, tuple] = {}


_bse_code_index: Optional[Dict[str, str]] = None


def _bse_codes_for(symbol: str) -> List[str]:
    """
    Screener keys BSE-only companies by scrip code, not by ticker.

    LIMECHM, MIL and ASHRAM all 404 as tickers while bare codes like 514446
    resolve, so a symbol that Screener has never heard of is retried as its BSE
    code before being written off as unavailable.
    """
    global _bse_code_index
    if _bse_code_index is None:
        index = {}
        try:
            from app.main import get_bse_equities
            for eq in get_bse_equities():
                sym = (eq.get("symbol") or "").upper()
                token = str(eq.get("token") or "")
                if sym and token:
                    index[sym] = token
        except Exception as e:
            logger.debug(f"BSE code index unavailable: {e}")
        _bse_code_index = index
    code = _bse_code_index.get(str(symbol).strip().upper())
    return [code] if code else []

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}

# Screener's row labels mapped onto our metric rows. Order matters: the first
# label that matches a row wins, so more specific labels come first.
_ROW_LABELS = {
    "revenue": ("sales", "revenue"),
    "expenses": ("expenses",),
    "ebitda": ("operating profit",),
    "other_income": ("other income",),
    "interest": ("interest",),
    "depreciation": ("depreciation",),
    "pbt": ("profit before tax",),
    "pat": ("net profit",),
}

# Statement order, for anything drawing the whole chain. Revenue at the top and
# what is left at the bottom, so a chart of them reads down the P&L.
METRIC_ORDER = ("revenue", "expenses", "ebitda", "other_income",
                "interest", "depreciation", "pbt", "pat")

METRIC_LABELS = {
    "revenue": "Revenue",
    "expenses": "Expenses",
    "ebitda": "Operating profit",
    "other_income": "Other income",
    "interest": "Interest",
    "depreciation": "Depreciation",
    "pbt": "Profit before tax",
    "pat": "Profit after tax",
}

_TAG = re.compile(r"<[^>]+>")


def _strip(html: str) -> str:
    return _TAG.sub("", html).replace("&nbsp;", " ").strip()


def _to_float(text: str) -> Optional[float]:
    """Parse a Screener cell into a number, or None when it is blank."""
    if not text:
        return None
    cleaned = text.replace(",", "").replace("%", "").strip()
    # Screener renders negatives in a leading-minus form.
    m = re.match(r"^-?\d+(?:\.\d+)?$", cleaned)
    if not m:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_quarters_table(html: str) -> Dict[str, list]:
    """Return {normalised_row_label: [values oldest..newest]} from the quarters table."""
    section = re.search(r'id="quarters".*?<table[^>]*>(.*?)</table>', html, re.DOTALL)
    if not section:
        return {}

    rows: Dict[str, list] = {}
    for row_html in re.findall(r"<tr[^>]*>(.*?)</tr>", section.group(1), re.DOTALL):
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row_html, re.DOTALL)
        if len(cells) < 2:
            continue
        label = _strip(cells[0]).lower().rstrip("+").strip()
        if not label:
            continue
        values = [_to_float(_strip(c)) for c in cells[1:]]
        if any(v is not None for v in values):
            rows[label] = values
    return rows


def _quarter_headings(html: str) -> list:
    """Column headings of the quarters table, e.g. ['Jun 2025', ..., 'Jun 2026']."""
    section = re.search(r'id="quarters".*?<table[^>]*>(.*?)</table>', html, re.DOTALL)
    if not section:
        return []
    head = re.search(r"<thead[^>]*>(.*?)</thead>", section.group(1), re.DOTALL)
    if not head:
        return []
    cells = re.findall(r"<th[^>]*>(.*?)</th>", head.group(1), re.DOTALL)
    return [_strip(c) for c in cells[1:] if _strip(c)]


def fetch_latest_quarter(symbol: str, cache_only: bool = False) -> dict:
    """
    Latest reported quarter for `symbol`, as values.

    Returns:
        {
          "ok": bool,
          "source_url": str,
          "quarter": "Jun 2026" | "",
          "metrics": { row: {"current_qtr": float|None,
                             "last_year_same_qtr": float|None,
                             "yoy_change_pct": float|None} },
          "error": str,          # only when ok is False
        }

    Values are ₹ crore. The year-ago column is taken four quarters back, which
    is what "same quarter last year" means in Screener's layout.
    """
    out = {"ok": False, "source_url": "", "quarter": "", "metrics": {}, "error": ""}
    if not symbol or not str(symbol).strip():
        out["error"] = "no symbol"
        return out

    sym = str(symbol).strip().upper()

    cached = _quarter_cache.get(sym)
    if cached and (time.monotonic() - cached[0]) <= _QUARTER_CACHE_TTL:
        return cached[1]
    if cache_only:
        # Caller cannot afford a network round trip — say so rather than block.
        out["error"] = "not fetched yet"
        return out
    # Ticker first, then the BSE scrip code — Screener indexes BSE-only
    # companies under the code, and most of a results day is BSE-only.
    slugs = [sym] + _bse_codes_for(sym)
    paths = [p for slug in slugs for p in (f"{slug}/consolidated", slug)]
    for path in paths:
        url = f"https://www.screener.in/company/{path}/"
        res = _get(url)
        if res is None:
            out["error"] = "request failed"
            continue
        if res.status_code != 200:
            out["error"] = f"HTTP {res.status_code}"
            continue

        rows = _parse_quarters_table(res.text)
        if not rows:
            out["error"] = "no quarterly table"
            continue

        headings = _quarter_headings(res.text)
        metrics = {}
        for key, labels in _ROW_LABELS.items():
            series = None
            for label in labels:
                for row_label, values in rows.items():
                    if row_label.startswith(label):
                        series = values
                        break
                if series:
                    break
            if not series:
                metrics[key] = {"current_qtr": None, "last_year_same_qtr": None,
                                "yoy_change_pct": None, "prev_qtr": None, "qoq_change_pct": None}
                continue

            def pct(cur, base):
                if cur is None or base in (None, 0):
                    return None
                # Two decimals: these are read beside the figures they come
                # from, and one decimal quietly turns 0.04% into 0.0%.
                return round((cur - base) / abs(base) * 100, 2)

            current = series[-1] if series else None
            # Four columns back is the same quarter a year earlier.
            year_ago = series[-5] if len(series) >= 5 else None
            # One column back is the quarter immediately before this one. It
            # answers a different question from YoY — sequential momentum rather
            # than annual growth — and for a seasonal business the two routinely
            # disagree, which is the point of showing both.
            prev_qtr = series[-2] if len(series) >= 2 else None
            metrics[key] = {
                # Every quarter screener publishes, oldest first, aligned with
                # `quarters`. Already parsed to answer the YoY question below —
                # keeping it costs nothing and is what the history chart draws.
                "series": series,
                "current_qtr": current,
                "last_year_same_qtr": year_ago,
                "yoy_change_pct": pct(current, year_ago),
                "prev_qtr": prev_qtr,
                "qoq_change_pct": pct(current, prev_qtr),
            }

        out.update({
            "ok": any(m["current_qtr"] is not None for m in metrics.values()),
            "source_url": url,
            "quarter": headings[-1] if headings else "",
            "prev_quarter": headings[-2] if len(headings) >= 2 else "",
            # Column headings for the series above, same order.
            "quarters": headings,
            "metrics": metrics,
            "error": "" if metrics else out["error"],
        })
        if out["ok"]:
            _quarter_cache[sym] = (time.monotonic(), out)
            return out

    _quarter_cache[sym] = (time.monotonic(), out)
    return out


def quarterly_history(symbol: str, quarters: int = 8,
                      cache_only: bool = False) -> dict:
    """
    The last `quarters` reported quarters, each paired with the same quarter a
    year earlier.

    Paired rather than plotted as a plain run of bars because most Indian
    businesses are seasonal: a December quarter below the September before it
    says nothing, while a December quarter below *last* December says a great
    deal. Reading growth off adjacent bars of a seasonal series is the error
    this pairing exists to prevent.

    Screener is the source, not the filings. It carries every quarter already
    normalised to Rs crore, so year-on-year is read off two columns rather than
    parsed out of two different PDFs — which removes the largest source of error
    in doing this from filings.
    """
    data = fetch_latest_quarter(symbol, cache_only=cache_only)
    headings = data.get("quarters") or []
    if not data.get("ok") or not headings:
        return {"ok": False, "symbol": symbol.upper(), "error": data.get("error") or "no data",
                "source_url": data.get("source_url", ""), "points": []}

    def at(series, i):
        if not series or i < 0 or i >= len(series):
            return None
        return series[i]

    metrics = data.get("metrics") or {}
    points = []
    # Newest last, so the chart reads left to right in time.
    start = max(0, len(headings) - quarters)
    for i in range(start, len(headings)):
        point = {"quarter": headings[i]}
        # Four columns back is the same quarter a year earlier. Absent for the
        # oldest quarters screener holds, which is why the value can be null and
        # the chart must draw a gap rather than a zero.
        yoy_i = i - 4
        point["year_ago_quarter"] = headings[yoy_i] if yoy_i >= start - 4 and yoy_i >= 0 else None
        for key in METRIC_ORDER:
            series = (metrics.get(key) or {}).get("series") or []
            cur = at(series, i)
            prev_year = at(series, yoy_i) if yoy_i >= 0 else None
            point[key] = cur
            point[key + "_year_ago"] = prev_year
            # Left blank off a negative or zero base on purpose: a swing out of
            # a loss has no meaningful percentage, and printing one invites the
            # reader to compare it with a real growth rate.
            point[key + "_yoy_pct"] = (
                round((cur - prev_year) / abs(prev_year) * 100, 2)
                if cur is not None and prev_year not in (None, 0) and prev_year > 0
                else None
            )
        points.append(point)

    # Only the metrics this company actually reports. Banks have no meaningful
    # operating profit and NBFCs report interest income rather than turnover, so
    # a fixed list of eight would draw empty strips for a third of the market.
    present = [k for k in METRIC_ORDER
               if any(p.get(k) is not None for p in points)]

    return {
        "ok": bool(points),
        "symbol": symbol.upper(),
        "source_url": data.get("source_url", ""),
        "unit": "Rs crore",
        "metrics": [{"key": k, "label": METRIC_LABELS[k]} for k in present],
        "points": points,
        "error": "",
    }


# Below this (₹ crore) a year-on-year percentage stops meaning anything: a
# micro-cap going from ₹0.02 Cr profit to ₹0.06 Cr loss is "-400%", which reads
# as catastrophic and is really a rounding difference on two lakh rupees.
_MIN_MEANINGFUL_BASE = 1.0


def screener_all_positive(screener: dict) -> bool:
    """
    True when revenue and profit are both up on the year *and* on the quarter.

    Four numbers agreeing is a different statement from any one of them, and it
    is the cheapest way to find the handful of filings worth reading first on a
    day that produced seven hundred.
    """
    metrics = (screener or {}).get("metrics") or {}
    checks = []
    for key in ("revenue", "pat"):
        m = metrics.get(key) or {}
        checks += [m.get("yoy_change_pct"), m.get("qoq_change_pct")]
    return bool(checks) and all(v is not None and v > 0 for v in checks)


def screener_signal(screener: dict) -> dict:
    """
    A mechanical read of the filing from Screener's year-on-year figures.

    Deliberately simple and stated in full on the page, because the reader has
    to be able to disagree with it: arithmetic on two published numbers, not a
    view on the company. Profit leads, revenue confirms — profit up on falling
    revenue is a margin or one-off story these two numbers cannot separate, so
    it produces no call.

    A swing between profit and loss is read as the swing itself rather than as a
    percentage, and any comparison against a base under ₹1 crore is refused.
    Both exist because the percentages are otherwise arithmetically true and
    completely misleading.

    Inconclusive is NA, never a hedge, for the same reason the AI verdict is:
    HOLD reads as a decision to anyone acting on it.
    """
    metrics = (screener or {}).get("metrics") or {}
    quarter = (screener or {}).get("quarter") or ""
    qual = f" ({quarter})" if quarter else ""

    pat = metrics.get("pat") or {}
    rev = metrics.get("revenue") or {}
    pat_cur, pat_prev = pat.get("current_qtr"), pat.get("last_year_same_qtr")
    rev_pct = rev.get("yoy_change_pct")
    rev_prev = rev.get("last_year_same_qtr")

    na = lambda why: {"label": "NA", "tone": "na", "reason": why}

    if pat_cur is None or pat_prev is None:
        return na(f"no comparable profit figure on Screener{qual}")

    # Size gate first, before any rule including the swing: a company whose
    # profit moves between ₹0.02 Cr and -₹0.06 Cr has not told us anything, and
    # "swung to a loss" overstates two lakh rupees exactly as badly as -400% did.
    if max(abs(pat_cur), abs(pat_prev)) < _MIN_MEANINGFUL_BASE:
        return na(f"profit figures under ₹1 Cr either side — too small to read{qual}")

    # Profit turning to loss, or back, is the clearest read available and does
    # not survive being expressed as a percentage.
    if pat_prev > 0 and pat_cur < 0:
        return {"label": "SELL", "tone": "neg",
                "reason": f"swung from ₹{pat_prev:,.2f} Cr profit to ₹{abs(pat_cur):,.2f} Cr loss{qual}"}
    if pat_prev < 0 and pat_cur > 0:
        return {"label": "BUY", "tone": "pos",
                "reason": f"swung from ₹{abs(pat_prev):,.2f} Cr loss to ₹{pat_cur:,.2f} Cr profit{qual}"}

    if abs(pat_prev) < _MIN_MEANINGFUL_BASE:
        return na(f"year-ago profit was only ₹{pat_prev:,.2f} Cr — too small to read a change from{qual}")

    pat_pct = pat.get("yoy_change_pct")
    if pat_pct is None:
        return na(f"no year-on-year profit change available{qual}")

    # Revenue only corroborates when its own base is large enough to trust.
    rev_usable = rev_pct is not None and rev_prev is not None and abs(rev_prev) >= _MIN_MEANINGFUL_BASE
    detail = f"PAT {pat_pct:+.1f}% YoY" + (f", revenue {rev_pct:+.1f}%" if rev_usable else "") + qual

    if pat_pct >= 20 and (not rev_usable or rev_pct >= 10):
        return {"label": "BUY", "tone": "pos", "reason": f"{detail} — growth on both lines"}
    if pat_pct >= 20 and rev_usable and rev_pct < 0:
        return na(f"{detail} — profit up on falling revenue")
    if pat_pct <= -20:
        return {"label": "SELL", "tone": "neg", "reason": f"{detail} — sharp profit decline"}
    if pat_pct > 0 and rev_usable and rev_pct > 0:
        return {"label": "BUY", "tone": "pos", "reason": f"{detail} — both lines improving"}
    if pat_pct < 0 and rev_usable and rev_pct < 0:
        return {"label": "SELL", "tone": "neg", "reason": f"{detail} — both lines weaker"}
    return na(f"{detail} — mixed")


_NUM_IN_TEXT = re.compile(r"-?[\d,]+(?:\.\d+)?")


# Currency symbols and separators sit between the sign and the digits, so they
# are stripped before the sign is read.
_CURRENCY_NOISE = re.compile(r"[₹$€£¥,\s]")


def parse_ai_value(text: str) -> Optional[float]:
    """
    Pull a number out of an AI cell such as "₹535.80 Cr" so it can be compared.

    Handles the crore/lakh/million suffixes the model uses, normalising
    everything to crore, which is what Screener reports in.

    Sign is resolved before parsing. "-₹442.59 Cr" puts the minus in front of the
    currency symbol, and "(442.59)" is the accounting form of a negative — read
    naively both come back positive, which would silently turn a quarterly loss
    into a profit.
    """
    if not text:
        return None
    s = str(text).strip()
    if s.upper() in ("NA", "N/A", "", "-", "--"):
        return None

    compact = _CURRENCY_NOISE.sub("", s)
    negative = (
        compact.startswith("-")
        or compact.startswith("−")          # unicode minus
        or ("(" in compact and ")" in compact)   # accounting negative
    )

    m = _NUM_IN_TEXT.search(compact)
    if not m:
        return None
    try:
        value = abs(float(m.group(0).lstrip("-−")))
    except ValueError:
        return None

    low = s.lower()
    if "lakh" in low or " lac" in low:
        value /= 100.0                 # 100 lakh = 1 crore
    elif "mn" in low or "million" in low:
        value /= 10.0                  # 10 million = 1 crore
    elif "bn" in low or "billion" in low:
        value *= 100.0

    return round(-value if negative else value, 2)


def compare(ai_value: Optional[float], actual: Optional[float]) -> dict:
    """
    Compare an AI-extracted figure with Screener's.

    `match` is None when either side is missing — that is "cannot tell", which
    must not be presented as agreement.
    """
    if ai_value is None or actual is None:
        return {"diff": None, "diff_pct": None, "match": None}
    diff = round(ai_value - actual, 2)
    diff_pct = round(diff / abs(actual) * 100, 1) if actual else None
    # 2% absorbs consolidated-vs-standalone and rounding differences.
    match = diff_pct is not None and abs(diff_pct) <= 2.0
    return {"diff": diff, "diff_pct": diff_pct, "match": match}
