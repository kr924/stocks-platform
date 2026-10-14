"""
Like-for-like: this filing's quarter against the same quarter a year earlier.

What it replaces. The panel and the 08:00 digest used to show Screener's
*latest* quarter beside every filing — whatever quarter that happened to be —
with Screener's own YoY and QoQ for it. On 14 Oct 2026 HDB Financial filed its
September 2026 results; Screener's latest was March 2025, so the screen showed
March 2025's growth rates under a September 2026 filing, and the digest's
BUY/SELL read was computed from them. A result has to be judged against the
same quarter a year earlier and nothing else: Sep 2026 against Sep 2025, never
against Jun 2025 or whatever Screener last published.

How the figures are put together:

  quarter   the filing's own period, as the PDF extractor read it from the
            statement's column headings; failing that, the last quarter that
            ended before the announcement (results in October are September's).
  current   Screener's figure for that exact quarter when Screener has
            published it — then both sides of every comparison come from one
            source on one basis — otherwise the figure read off the filing.
  year ago  Screener's figure for the same quarter one year earlier.
  previous  Screener's figure for the quarter immediately before.

Nothing is ever substituted. A missing base is reported as "not available",
because a growth rate against the wrong quarter is worse than no growth rate:
it looks exactly like a real one.

Definitions are aligned with Screener's, which splits the P&L differently from
a filed statement:

  expenses  filing "total expenses" includes finance cost and depreciation;
            Screener's "Expenses" excludes both. Compared as Screener
            Expenses + Interest + Depreciation.
  ebitda    the extractor's EBITDA is revenue less operating expenses, which is
            what Screener calls Operating Profit. Banks and NBFCs have no
            Operating Profit row on Screener, so theirs is "not available".

All values are ₹ crore. Pure functions plus read-only queries; safe on the
request path.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, Optional

logger = logging.getLogger("app.result_compare")

IST = timezone(timedelta(hours=5, minutes=30))

METRICS = ("revenue", "expenses", "other_income", "ebitda", "pbt", "pat")
LABELS = {
    "revenue": "Revenue",
    "expenses": "Expenses",
    "other_income": "Other Income",
    "ebitda": "EBITDA",
    "pbt": "PBT",
    "pat": "Profit (PAT)",
}

# Below this (₹ crore) a percentage stops meaning anything: two figures a few
# lakh apart can be 300% apart. Same floor the digest's signal uses.
MIN_BASE = 1.0

NOT_AVAILABLE = "numbers not available"

_MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# ─── Quarter labels ─────────────────────────────────────────────────────────

def parse_quarter(label: str) -> Optional[tuple]:
    """'Sep 2026' -> (2026, 9). Anything else -> None."""
    bits = (label or "").strip().split()
    if len(bits) != 2:
        return None
    mon = bits[0][:3].title()
    if mon not in _MON:
        return None
    try:
        return int(bits[1]), _MON.index(mon) + 1
    except ValueError:
        return None


def quarter_label(year: int, month: int) -> str:
    return f"{_MON[month - 1]} {year}"


def shift_quarter(label: str, months: int) -> str:
    """'Sep 2026', -12 -> 'Sep 2025'; 'Sep 2026', -3 -> 'Jun 2026'."""
    ym = parse_quarter(label)
    if not ym:
        return ""
    idx = ym[0] * 12 + (ym[1] - 1) + months
    return quarter_label(idx // 12, idx % 12 + 1)


def infer_quarter(event_time: Optional[datetime]) -> str:
    """
    The quarter a results filing most likely covers: the last quarter that
    ended before it was announced. Results announced in October are
    September's; in late May, March's.
    """
    if not event_time:
        return ""
    when = event_time
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    d = when.astimezone(IST).date()
    # Quarter-end months, latest first, strictly before the announcement date.
    for back in range(0, 13):
        idx = d.year * 12 + (d.month - 1) - back
        y, m = idx // 12, idx % 12 + 1
        if m in (3, 6, 9, 12):
            # The quarter ends on the last day of month m; it has "ended" if we
            # are past that month.
            if (y, m) < (d.year, d.month):
                return quarter_label(y, m)
    return ""


# ─── Screener figures by quarter ────────────────────────────────────────────

_RAW_KEYS = ("revenue", "expenses", "ebitda", "other_income", "interest",
             "depreciation", "pbt", "pat")


def _from_snapshot(screener: Optional[dict], quarter: str) -> Optional[dict]:
    """Raw Screener values for one quarter out of a fetch_latest_quarter() result."""
    if not screener or not quarter:
        return None
    headings = screener.get("quarters") or []
    if quarter not in headings:
        return None
    i = headings.index(quarter)
    metrics = screener.get("metrics") or {}
    out = {}
    for k in _RAW_KEYS:
        series = (metrics.get(k) or {}).get("series") or []
        out[k] = series[i] if i < len(series) else None
    return out if any(v is not None for v in out.values()) else None


def _from_store(db, symbol: str, quarters: Iterable[str]) -> Dict[str, dict]:
    """Raw Screener values from the screener_quarters table, by quarter label."""
    if db is None or not symbol:
        return {}
    try:
        from app.database import ScreenerQuarter
        rows = db.query(ScreenerQuarter).filter(
            ScreenerQuarter.symbol == symbol.strip().upper(),
            ScreenerQuarter.quarter.in_([q for q in quarters if q]),
        ).all()
    except Exception as e:
        logger.debug(f"Screener store unavailable for {symbol}: {e}")
        return {}
    return {r.quarter: {k: getattr(r, k, None) for k in _RAW_KEYS} for r in rows}


def comparable(raw: Optional[dict], metric: str) -> Optional[float]:
    """A Screener figure expressed the way a filed statement states it."""
    if not raw:
        return None
    if metric == "expenses":
        exp = raw.get("expenses")
        if exp is None:
            return None
        return exp + (raw.get("interest") or 0) + (raw.get("depreciation") or 0)
    return raw.get(metric)


def screener_figures(db, symbol: str, quarters: Iterable[str],
                     screener: Optional[dict] = None) -> Dict[str, dict]:
    """
    Raw Screener values for each requested quarter that Screener has, from the
    fetch snapshot first and the stored history second.
    """
    quarters = [q for q in quarters if q]
    found = {}
    for q in quarters:
        snap = _from_snapshot(screener, q)
        if snap:
            found[q] = snap
    missing = [q for q in quarters if q not in found]
    if missing:
        found.update(_from_store(db, symbol, missing))
    return found


# ─── The comparison ─────────────────────────────────────────────────────────

def _pct(cur: Optional[float], base: Optional[float]) -> Optional[float]:
    if cur is None or base is None or abs(base) < MIN_BASE:
        return None
    return round((cur - base) / abs(base) * 100, 2)


def filing_quarter(extraction: Optional[dict], event_time: Optional[datetime]) -> tuple:
    """
    (quarter, source, filing_values_usable).

    The extractor's reading of the column headings wins, provided it is the
    quarter the announcement date implies or the one before it (a late
    filer). Anything else means the extractor read the wrong column — its
    figures are for some other period and must not be used as "this quarter".
    """
    inferred = infer_quarter(event_time)
    ex_q = ((extraction or {}).get("quarter") or "").strip()
    ex_ok = bool((extraction or {}).get("ok")) and bool(parse_quarter(ex_q))
    if ex_ok:
        if not inferred or ex_q in (inferred, shift_quarter(inferred, -3)):
            return ex_q, "filing", True
        return inferred, "announcement date", False
    return inferred, "announcement date", False


def _ai_number(cell) -> Optional[float]:
    if cell is None:
        return None
    if isinstance(cell, (int, float)):
        return float(cell)
    try:
        from app.services.screener_quarters import parse_ai_value
        return parse_ai_value(str(cell))
    except Exception:
        return None


def build(db, symbol: str, event_time: Optional[datetime],
          extraction: Optional[dict] = None, screener: Optional[dict] = None,
          ai_metrics: Optional[dict] = None) -> dict:
    """
    The filing's quarter, its figures, and YoY / QoQ against Screener's
    same-quarter-last-year and previous-quarter figures.

    Returns {
      quarter, quarter_source, yoy_quarter, qoq_quarter, current_source,
      rows: [{key, label, current, year_ago, prev_qtr, yoy_pct, qoq_pct,
              current_note, yoy_note, qoq_note}],
      yoy_available, qoq_available, current_available, message
    }
    """
    quarter, q_source, filing_usable = filing_quarter(extraction, event_time)
    yoy_q = shift_quarter(quarter, -12) if quarter else ""
    qoq_q = shift_quarter(quarter, -3) if quarter else ""

    figs = screener_figures(db, symbol, [quarter, yoy_q, qoq_q], screener)
    scr_cur, scr_yoy, scr_qoq = figs.get(quarter), figs.get(yoy_q), figs.get(qoq_q)

    ex_metrics = ((extraction or {}).get("metrics") or {}) if filing_usable else {}
    if scr_cur:
        current_source = "screener"
    elif ex_metrics and any((ex_metrics.get(m) or {}).get("current_qtr") is not None for m in METRICS):
        current_source = "filing"
    else:
        current_source = ""

    rows = []
    for m in METRICS:
        if current_source == "screener":
            cur = comparable(scr_cur, m)
        elif current_source == "filing":
            cur = (ex_metrics.get(m) or {}).get("current_qtr")
        else:
            cur = None
        ya = comparable(scr_yoy, m)
        pq = comparable(scr_qoq, m)
        yoy = _pct(cur, ya)
        qoq = _pct(cur, pq)

        def note(base, pct_v, base_q):
            if base is None:
                return f"{NOT_AVAILABLE} for {base_q}" if base_q else NOT_AVAILABLE
            if cur is None:
                return "current figure not read"
            if pct_v is None:
                return f"{base_q} base under ₹1 Cr" if abs(base) < MIN_BASE else ""
            return ""

        # The AI's reading of the same quarter, against the same two bases.
        # Only the current figure differs between the two views; the bases
        # are Screener's either way.
        ai_cur = _ai_number(((ai_metrics or {}).get(m) or {}).get("current_qtr"))

        rows.append({
            "key": m,
            "label": LABELS[m],
            "ai_current": ai_cur,
            "ai_yoy_pct": _pct(ai_cur, ya),
            "ai_qoq_pct": _pct(ai_cur, pq),
            "current": cur,
            "year_ago": ya,
            "prev_qtr": pq,
            "yoy_pct": yoy,
            "qoq_pct": qoq,
            "current_note": "" if cur is not None else (
                "not read from the filing" if current_source == "filing" else NOT_AVAILABLE),
            "yoy_note": note(ya, yoy, yoy_q),
            "qoq_note": note(pq, qoq, qoq_q),
        })

    yoy_available = any(r["yoy_pct"] is not None for r in rows)
    qoq_available = any(r["qoq_pct"] is not None for r in rows)
    current_available = any(r["current"] is not None for r in rows)

    if not quarter:
        message = "The filing's quarter could not be determined — " + NOT_AVAILABLE + "."
    elif not current_available:
        if extraction is None:
            message = (f"{quarter} figures not read yet — the filing is being read, "
                       f"and Screener has not published {quarter}.")
        else:
            message = (f"{quarter} figures not available — the filing could not be read "
                       f"and Screener has not published {quarter}.")
    elif not scr_yoy and not scr_qoq:
        message = f"Screener {NOT_AVAILABLE} for {yoy_q} or {qoq_q} — no YoY or QoQ shown."
    elif not scr_yoy:
        message = f"Screener {NOT_AVAILABLE} for {yoy_q} — no YoY shown."
    elif not scr_qoq:
        message = f"Screener {NOT_AVAILABLE} for {qoq_q} — no QoQ shown."
    else:
        message = ""

    result = {
        "quarter": quarter,
        "quarter_source": q_source,
        "yoy_quarter": yoy_q,
        "qoq_quarter": qoq_q,
        "current_source": current_source,
        "rows": rows,
        "yoy_available": yoy_available,
        "qoq_available": qoq_available,
        "current_available": current_available,
        "ai_available": any(r["ai_current"] is not None for r in rows),
        "message": message,
    }
    # The rule-based read of these figures: verdict, positives, negatives.
    result["numbers_read"] = read_numbers(result)
    return result


def as_screener_shape(lfl: dict) -> dict:
    """
    The comparison in the shape screener_signal() and screener_all_positive()
    read, so the BUY/SELL rules stay in one place and now run on the right
    quarters.
    """
    metrics = {}
    for r in lfl.get("rows") or []:
        metrics[r["key"]] = {
            "current_qtr": r["current"],
            "last_year_same_qtr": r["year_ago"],
            "yoy_change_pct": r["yoy_pct"],
            "prev_qtr": r["prev_qtr"],
            "qoq_change_pct": r["qoq_pct"],
        }
    label = lfl.get("quarter") or ""
    if label and lfl.get("yoy_quarter"):
        label = f"{label} vs {lfl['yoy_quarter']}"
    return {"ok": lfl.get("current_available", False), "quarter": label, "metrics": metrics}


# ─── A plain read of the figures ────────────────────────────────────────────

def _margin(num, den):
    if num is None or den is None or abs(den) < MIN_BASE:
        return None
    return num / den * 100


def read_numbers(lfl: dict) -> dict:
    """
    What the like-for-like figures say, in words: the YoY verdict (the same
    rules the digest has always stated — screener_signal) plus the positives
    and negatives a reader would pick out of the table.

    Arithmetic only, so every line can be checked against the table above it.
    Nothing here reads the filing's prose; that is the AI's half.
    """
    from app.services.screener_quarters import screener_signal

    rows = {r["key"]: r for r in lfl.get("rows") or []}
    q, yq, qq = lfl.get("quarter") or "", lfl.get("yoy_quarter") or "", lfl.get("qoq_quarter") or ""
    verdict = screener_signal(as_screener_shape(lfl))
    pos, neg = [], []

    def g(key, field):
        return (rows.get(key) or {}).get(field)

    rev_y, pat_y = g("revenue", "yoy_pct"), g("pat", "yoy_pct")
    pat_q = g("pat", "qoq_pct")
    rev_q = g("revenue", "qoq_pct")
    pat_cur, pat_ya = g("pat", "current"), g("pat", "year_ago")

    if rev_y is not None:
        (pos if rev_y >= 10 else neg if rev_y < 0 else pos if rev_y > 0 else neg).append(
            f"Revenue {rev_y:+.1f}% YoY vs {yq}")
    if pat_cur is not None and pat_ya is not None and abs(pat_ya) >= MIN_BASE:
        if pat_ya < 0 < pat_cur:
            pos.append(f"Swung to a profit of ₹{pat_cur:,.1f} Cr from a ₹{abs(pat_ya):,.1f} Cr loss in {yq}")
        elif pat_ya > 0 > pat_cur:
            neg.append(f"Swung to a loss of ₹{abs(pat_cur):,.1f} Cr from a ₹{pat_ya:,.1f} Cr profit in {yq}")
        elif pat_y is not None:
            (pos if pat_y > 0 else neg).append(f"Profit {pat_y:+.1f}% YoY vs {yq}")
    if pat_q is not None:
        if pat_q >= 5:
            pos.append(f"Profit {pat_q:+.1f}% on the previous quarter ({qq})")
        elif pat_q <= -10:
            neg.append(f"Profit {pat_q:+.1f}% on the previous quarter ({qq})")
    if rev_q is not None and rev_q <= -10:
        neg.append(f"Revenue {rev_q:+.1f}% on the previous quarter ({qq})")

    # Net margin, now against a year ago.
    m_now = _margin(pat_cur, g("revenue", "current"))
    m_then = _margin(pat_ya, g("revenue", "year_ago"))
    if m_now is not None and m_then is not None:
        d = m_now - m_then
        if d >= 1:
            pos.append(f"Net margin up to {m_now:.1f}% from {m_then:.1f}%")
        elif d <= -1:
            neg.append(f"Net margin down to {m_now:.1f}% from {m_then:.1f}%")

    # Costs against revenue: operating leverage, or the lack of it.
    exp_y = g("expenses", "yoy_pct")
    if exp_y is not None and rev_y is not None:
        if exp_y > rev_y + 5:
            neg.append(f"Expenses {exp_y:+.1f}% YoY, ahead of revenue ({rev_y:+.1f}%)")
        elif exp_y < rev_y - 5:
            pos.append(f"Expenses {exp_y:+.1f}% YoY, behind revenue ({rev_y:+.1f}%)")

    # Profit leaning on other income.
    oi, pbt = g("other_income", "current"), g("pbt", "current")
    if oi is not None and pbt is not None and pbt >= MIN_BASE and oi > 0:
        share = oi / pbt * 100
        if share >= 30:
            neg.append(f"Other income is {share:.0f}% of profit before tax")

    if not pos and not neg:
        note = lfl.get("message") or "No comparison available"
    else:
        note = ""
    return {
        "label": verdict.get("label", "NA"),
        "tone": verdict.get("tone", "na"),
        "reason": verdict.get("reason", ""),
        "positives": pos,
        "negatives": neg,
        "note": note,
        "basis": f"{q} vs {yq} and {qq}" if q else "",
    }
