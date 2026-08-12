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
from typing import Dict, Optional

import requests

logger = logging.getLogger("app.screener_quarters")

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}

# Screener's row labels mapped onto our metric rows. Order matters: the first
# label that matches a row wins, so more specific labels come first.
_ROW_LABELS = {
    "revenue": ("sales", "revenue"),
    "expenses": ("expenses",),
    "other_income": ("other income",),
    "pat": ("net profit",),
    "ebitda": ("operating profit",),
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


def fetch_latest_quarter(symbol: str) -> dict:
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
    for path in (f"{sym}/consolidated", sym):
        url = f"https://www.screener.in/company/{path}/"
        try:
            res = requests.get(url, headers=_HEADERS, timeout=10)
        except Exception as e:
            out["error"] = f"request failed: {e}"
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
                metrics[key] = {"current_qtr": None, "last_year_same_qtr": None, "yoy_change_pct": None}
                continue

            current = series[-1] if series else None
            # Four columns back is the same quarter a year earlier.
            year_ago = series[-5] if len(series) >= 5 else None
            change = None
            if current is not None and year_ago not in (None, 0):
                change = round((current - year_ago) / abs(year_ago) * 100, 1)
            metrics[key] = {
                "current_qtr": current,
                "last_year_same_qtr": year_ago,
                "yoy_change_pct": change,
            }

        out.update({
            "ok": any(m["current_qtr"] is not None for m in metrics.values()),
            "source_url": url,
            "quarter": headings[-1] if headings else "",
            "metrics": metrics,
            "error": "" if metrics else out["error"],
        })
        if out["ok"]:
            return out

    return out


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
