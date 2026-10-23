"""
Checks on a results verdict that are worked out in code, not left to the model.

Why this exists. On 23 Oct 2026 the premium AI called BUY on two filings and
both stocks fell about 7%:

  SAKAR       PAT +149% YoY, but against a weak Sep 2025. Against its own
              run-rate the quarter was flat: PAT ₹10-11 Cr for four quarters
              running, EBITDA margin down 127 bps QoQ. The stock had tripled
              in a year, traded at 59x earnings and rose 5% the day before.
  PONNIERODE  A seasonal sugar company: Q1 is a loss and Sep is always its best
              quarter, so "+1,863% QoQ" means nothing. EBITDA growth matched
              the trailing year's; sector policy was turning against sugar.

The model listed most of this itself (margin down, reaction risk "medium")
and still said BUY, because the rule "no serious red flag" was a sentence in
the prompt that only the model enforced. So:

  expectation()   before the AI call: the run-rate (or, for a seasonal
                  business, the same quarter last year and the trailing-year
                  growth), from Screener's stored quarters. Goes in the prompt.
  price_context() the price going into the result: 5-session, 1-month and
                  1-year returns, P/E, distance from the 52-week high.
  trend_check()   after the AI call: the quarter against that expectation,
                  arithmetic on the AI's figures. "above", "in line", "below",
                  "turnaround" or "n/a".
  gate()          BUY stands only if the model rates reaction risk low, lists
                  no red flags, the trend check is not "in line"/"below", the
                  EBITDA margin did not fall more than 100 bps, and — when the
                  price already ran up or the valuation is rich — the quarter
                  clears a higher bar. Otherwise it becomes NA, with the
                  reasons stored and shown ("BUY withheld").

Uncertainty stays NA, never HOLD. A check that cannot be computed (no
Screener history, no price) does not block a BUY; it says "n/a".
"""
import logging
import math
from datetime import datetime, timedelta
from typing import Dict, List, Optional

logger = logging.getLogger("app.verdict_checks")

# The quarter must beat the higher of last quarter and the 3-quarter average
# by this much to be "above" its run-rate.
STEP_UP = 0.15
# ...and by this much when the price ran up into the result or the valuation
# is rich: priced for more, so it needs more.
STEP_UP_PRICED = 0.25
# Below the lower of the two by this much is "below".
STEP_DOWN = 0.10
# Seasonal businesses: the quarter's YoY growth against the trailing year's,
# as a ratio of growth factors (1+g)/(1+T).
SEASONAL_BAND = 0.15
# EBITDA margin down by more than this against the comparison quarter.
MARGIN_DROP_BPS = 100
# Price going into the result.
RUNUP_1M_PCT = 15.0
RUNUP_5D_PCT = 8.0
RICH_PE = 50.0
# Seasonality: residuals around a log-linear trend whose same-quarter signs
# repeat across years, with this typical size.
SEASONAL_MIN_SWING = 0.12

MIN_BASE = 1.0  # ₹ crore; same floor as result_compare


# ─── Screener history ───────────────────────────────────────────────────────

def _history(db, symbol: str, quarter: str, n: int = 12) -> Dict[str, dict]:
    """Raw Screener figures for the n quarters before `quarter`, by label."""
    from app.services.result_compare import screener_figures, shift_quarter
    qs = [shift_quarter(quarter, -3 * k) for k in range(n, 0, -1)]
    return screener_figures(db, symbol, [q for q in qs if q])


def _series(hist: Dict[str, dict], quarter: str, metric: str, n: int = 12) -> List[tuple]:
    """[(label, value)] oldest first for the n quarters before `quarter`, gaps as None."""
    from app.services.result_compare import comparable, shift_quarter
    out = []
    for k in range(n, 0, -1):
        q = shift_quarter(quarter, -3 * k)
        out.append((q, comparable(hist.get(q), metric)))
    return out


def seasonality(hist: Dict[str, dict], quarter: str) -> dict:
    """
    Whether revenue swings with the time of year rather than with the trend.

    Fits a straight line to log revenue over the quarters available (up to
    twelve), then looks at each quarter's residual. A seasonal business shows
    the same quarters above and below the line every year (Ponni: Dec high,
    Jun low) by a sizeable margin. A steady grower (Sakar) has small residuals
    with no repeating sign. Needs at least eight positive quarters, else
    "not enough history" and the business is treated as non-seasonal.
    """
    pts = [(i, q, v) for i, (q, v) in enumerate(_series(hist, quarter, "revenue"))
           if v is not None and v >= MIN_BASE]
    if len(pts) < 8:
        return {"seasonal": False, "why": "not enough quarterly history to tell", "swing_pct": None}
    xs = [p[0] for p in pts]
    ys = [math.log(p[2]) for p in pts]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs) or 1.0
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    resid = {p[0]: y - (my + slope * (p[0] - mx)) for p, y in zip(pts, ys)}
    swing = math.exp(sum(abs(r) for r in resid.values()) / n) - 1
    # Same quarter of the year is four positions apart.
    pairs = [(resid[i], resid[i + 4]) for i in resid if i + 4 in resid]
    meaningful = [(a, b) for a, b in pairs if max(abs(a), abs(b)) >= SEASONAL_MIN_SWING / 2]
    agree = sum(1 for a, b in meaningful if (a > 0) == (b > 0))
    seasonal = (swing >= SEASONAL_MIN_SWING and len(meaningful) >= 3
                and agree / len(meaningful) >= 0.75)
    why = (f"revenue swings about ±{swing * 100:.0f}% around its trend in the same quarters each year"
           if seasonal else f"revenue tracks its trend (typical swing ±{swing * 100:.0f}%)")
    return {"seasonal": seasonal, "why": why, "swing_pct": round(swing * 100, 1)}


def expectation(db, symbol: str, quarter: str) -> dict:
    """
    What the market had in hand before this filing, from Screener's quarters.

    Non-seasonal: last quarter and the average of the last three, for revenue,
    EBITDA (Screener's operating profit) and PAT. Seasonal: the same quarter
    last year and the trailing year's growth (last four quarters against the
    four before), on EBITDA, which exceptional items do not distort.
    """
    from app.services.result_compare import comparable, shift_quarter
    out = {"quarter": quarter, "available": False}
    if db is None or not symbol or not quarter:
        return out
    try:
        hist = _history(db, symbol, quarter)
    except Exception as e:
        logger.debug(f"[CHECKS] no history for {symbol}: {e}")
        return out
    if not hist:
        return out
    season = seasonality(hist, quarter)
    out.update({"available": True, "seasonal": season["seasonal"], "season_why": season["why"]})

    prev = [shift_quarter(quarter, -3 * k) for k in (1, 2, 3)]
    for m in ("revenue", "ebitda", "pat"):
        vals = [comparable(hist.get(q), m) for q in prev]
        out[f"{m}_last"] = vals[0]
        out[f"{m}_avg3"] = (sum(vals) / 3) if all(v is not None for v in vals) else None
        ya = comparable(hist.get(shift_quarter(quarter, -12)), m)
        out[f"{m}_year_ago"] = ya
        last4 = [comparable(hist.get(shift_quarter(quarter, -3 * k)), m) for k in (1, 2, 3, 4)]
        prior4 = [comparable(hist.get(shift_quarter(quarter, -3 * k)), m) for k in (5, 6, 7, 8)]
        if all(v is not None for v in last4 + prior4):
            s1, s0 = sum(last4), sum(prior4)
            out[f"{m}_ttm_growth"] = ((s1 - s0) / abs(s0) * 100) if abs(s0) >= MIN_BASE and s0 > 0 else None
        else:
            out[f"{m}_ttm_growth"] = None
    rev, ebitda = out.get("revenue_last"), out.get("ebitda_last")
    out["margin_last"] = (ebitda / rev * 100) if (rev and ebitda is not None and rev >= MIN_BASE) else None
    rev, ebitda = out.get("revenue_year_ago"), out.get("ebitda_year_ago")
    out["margin_year_ago"] = (ebitda / rev * 100) if (rev and ebitda is not None and rev >= MIN_BASE) else None
    out["prev_quarter"] = prev[0]
    out["year_ago_quarter"] = shift_quarter(quarter, -12)
    return out


def _cr(v: Optional[float]) -> str:
    return "n/a" if v is None else f"₹{v:,.1f} Cr"


def expectation_text(exp: dict) -> str:
    """The expectation as prompt lines."""
    if not exp.get("available"):
        return ""
    lines = []
    if exp.get("seasonal"):
        lines.append(f"  Seasonal business: {exp['season_why']}. Judge the quarter against "
                     f"{exp['year_ago_quarter']} (the same quarter last year), never the previous quarter.")
        lines.append(f"  {exp['year_ago_quarter']}: revenue {_cr(exp.get('revenue_year_ago'))}, "
                     f"EBITDA {_cr(exp.get('ebitda_year_ago'))}, PAT {_cr(exp.get('pat_year_ago'))}"
                     + (f", EBITDA margin {exp['margin_year_ago']:.1f}%" if exp.get("margin_year_ago") is not None else ""))
        if exp.get("ebitda_ttm_growth") is not None:
            lines.append(f"  The last four quarters' EBITDA grew {exp['ebitda_ttm_growth']:+.0f}% on the four before: "
                         f"that growth rate is the expectation. Matching it is IN LINE.")
    else:
        lines.append(f"  Not seasonal ({exp['season_why']}). The expectation is the run-rate:")
        lines.append(f"  {exp['prev_quarter']}: revenue {_cr(exp.get('revenue_last'))}, EBITDA {_cr(exp.get('ebitda_last'))}, "
                     f"PAT {_cr(exp.get('pat_last'))}"
                     + (f", EBITDA margin {exp['margin_last']:.1f}%" if exp.get("margin_last") is not None else ""))
        lines.append(f"  Average of the last 3 quarters: revenue {_cr(exp.get('revenue_avg3'))}, "
                     f"EBITDA {_cr(exp.get('ebitda_avg3'))}, PAT {_cr(exp.get('pat_avg3'))}")
        lines.append(f"  ABOVE trend means EBITDA (PAT where there is no EBITDA) more than {STEP_UP * 100:.0f}% above "
                     f"the higher of those two ({STEP_UP_PRICED * 100:.0f}% when the price is already up or the "
                     f"valuation rich, below). Big YoY growth against a weak year-ago quarter is NOT above trend.")
    return "\n".join(lines)


# ─── Price going into the result ────────────────────────────────────────────

def _bases_now(key: str) -> Optional[dict]:
    """Today's period bases for one key: stored, else one candle request now."""
    from app.database import SessionLocal, PeriodBase
    from app.services import period_returns as PR
    import json
    db = SessionLocal()
    try:
        have = PR.stored(db, [key])
        if key in have:
            return have[key]
        if PR.failed_today(key):
            return None
        from app.main import get_active_feed
        today = PR._today()
        data = PR._fetch_one(get_active_feed(), key, today)
        try:
            db.merge(PeriodBase(instrument_key=key, day=today, data_json=json.dumps(data),
                                fetched_at=datetime.utcnow()))
            db.commit()
        except Exception:
            db.rollback()
        return data
    except Exception as e:
        logger.debug(f"[CHECKS] no candles for {key}: {e}")
        return None
    finally:
        db.close()


def price_context(db, symbol: str) -> dict:
    """
    The price as the result landed and what it had done before: returns over
    5 sessions, 1 month and 1 year, distance from the 52-week high, P/E and
    market cap (Screener's page as last fetched). Everything optional.
    """
    out = {"available": False, "flags": []}
    if not symbol:
        return out
    sym = symbol.strip().upper()
    price, key = None, ""
    try:
        from app.database import PendingResultOrder
        since = datetime.utcnow() - timedelta(days=2)
        p = (db.query(PendingResultOrder)
             .filter(PendingResultOrder.symbol == sym, PendingResultOrder.event_time >= since)
             .order_by(PendingResultOrder.event_time.desc()).first())
        if p:
            price = p.price_at_announcement
            key = (p.instrument_key or "").replace(":", "|")
    except Exception:
        pass
    keys = [key] if key and "|" in key else []
    try:
        from app.main import resolve_instrument_keys
        keys += [k for k in resolve_instrument_keys(sym) if k not in keys]
    except Exception:
        pass
    if price is None and keys:
        try:
            from app.services.results_router import get_ltp
            price = get_ltp(keys[0], sym)
        except Exception:
            price = None

    bases = _bases_now(keys[0]) if keys else None
    if price is None and bases:
        price = bases.get("last_close")
    out["price"] = price
    rets = {}
    if price and bases:
        for name in ("5d", "1m", "1y"):
            b = (bases.get("bases") or {}).get(name)
            if b:
                rets[name] = round((price / b - 1) * 100, 1)
    out["returns"] = rets

    ratios = {}
    try:
        from app.services.screener_quarters import fetch_latest_quarter
        snap = fetch_latest_quarter(sym, cache_only=True)
        ratios = snap.get("ratios") or {}
        if not ratios:
            ratios = fetch_latest_quarter(sym).get("ratios") or {}
    except Exception:
        pass
    out["pe"] = ratios.get("pe")
    out["market_cap"] = ratios.get("market_cap")
    out["high_52w"] = ratios.get("high_52w")
    out["low_52w"] = ratios.get("low_52w")
    if price and out["high_52w"]:
        out["off_high_pct"] = round((price / out["high_52w"] - 1) * 100, 1)
    out["available"] = bool(rets or out["pe"] or out["high_52w"])

    flags = []
    r1m, r5d = rets.get("1m"), rets.get("5d")
    if (r1m is not None and r1m >= RUNUP_1M_PCT) or (r5d is not None and r5d >= RUNUP_5D_PCT):
        bits = [f"{r5d:+.1f}% in 5 sessions" if r5d is not None else "",
                f"{r1m:+.1f}% in a month" if r1m is not None else ""]
        flags.append("ran up into the result: " + ", ".join(b for b in bits if b))
    if out["pe"] is not None and out["pe"] >= RICH_PE:
        flags.append(f"rich valuation: P/E {out['pe']:.0f}")
    out["flags"] = flags
    out["priced_in"] = bool(flags)
    return out


def price_text(pc: dict) -> str:
    if not pc.get("available"):
        return ""
    r = pc.get("returns") or {}
    parts = []
    if pc.get("price"):
        parts.append(f"price ₹{pc['price']:,.2f}")
    for name, label in (("5d", "5 sessions"), ("1m", "1 month"), ("1y", "1 year")):
        if r.get(name) is not None:
            parts.append(f"{label} {r[name]:+.1f}%")
    if pc.get("off_high_pct") is not None:
        parts.append(f"{pc['off_high_pct']:+.1f}% vs 52-week high ₹{pc['high_52w']:,.0f}")
    if pc.get("pe") is not None:
        parts.append(f"P/E {pc['pe']:.1f}")
    if pc.get("market_cap"):
        parts.append(f"market cap ₹{pc['market_cap']:,.0f} Cr")
    text = "  Going into the result: " + ", ".join(parts) + "."
    if pc.get("flags"):
        flags = "; ".join(pc["flags"])
        text += ("\n  " + flags[:1].upper() + flags[1:]
                 + ". Good news may already be in the price: an in-line quarter is likely to be sold, "
                   "so reaction_risk cannot be low unless the quarter is clearly above trend.")
    return text


def prompt_block(exp: dict, pc: dict) -> str:
    body = "\n".join(t for t in (expectation_text(exp), price_text(pc)) if t)
    if not body:
        return ""
    return ("=== COMPUTED CHECKS (worked out from Screener's published quarters and market prices; "
            "trust these over your own reading of the trend) ===\n" + body + "\n")


# ─── After the AI: the quarter against the expectation ──────────────────────

def _num(cell) -> Optional[float]:
    from app.services.result_compare import _ai_number
    return _ai_number(cell)


def _vs_run_rate(cur, last, avg3, bar) -> Optional[str]:
    refs = [v for v in (last, avg3) if v is not None]
    if cur is None or not refs:
        return None
    hi, lo = max(refs), min(refs)
    if hi <= 0:
        return "turnaround" if cur > 0 else None
    if cur > hi * (1 + bar):
        return "above"
    if lo > 0 and cur < lo * (1 - STEP_DOWN):
        return "below"
    return "in line"


def trend_check(exp: dict, metrics: dict, priced_in: bool = False) -> dict:
    """
    The quarter (the AI's figures) against the expectation. Returns
    {verdict, detail, margin_bps, margin_detail, basis}.
    """
    out = {"verdict": "n/a", "detail": "", "margin_bps": None, "margin_detail": ""}
    metrics = metrics or {}
    cur = {m: _num((metrics.get(m) or {}).get("current_qtr")) for m in ("revenue", "ebitda", "pat")}
    prevq = {m: _num((metrics.get(m) or {}).get("previous_qtr")) for m in ("revenue", "ebitda", "pat")}
    yago = {m: _num((metrics.get(m) or {}).get("last_year_same_qtr")) for m in ("revenue", "ebitda", "pat")}

    def margin(rev, e):
        return e / rev * 100 if (rev and e is not None and rev >= MIN_BASE) else None

    seasonal = bool(exp.get("seasonal"))
    out["basis"] = "trailing-year growth (seasonal business, judged YoY)" if seasonal else "run-rate"

    # Margin: against the filing's own comparison column, else Screener's.
    m_now = margin(cur["revenue"], cur["ebitda"])
    if seasonal:
        m_then = margin(yago["revenue"], yago["ebitda"])
        if m_then is None:
            m_then = exp.get("margin_year_ago")
        against = "the same quarter last year"
    else:
        m_then = margin(prevq["revenue"], prevq["ebitda"])
        if m_then is None:
            m_then = exp.get("margin_last")
        against = "the previous quarter"
    if m_now is not None and m_then is not None:
        bps = round((m_now - m_then) * 100)
        out["margin_bps"] = bps
        out["margin_detail"] = f"EBITDA margin {m_now:.1f}% vs {m_then:.1f}% in {against} ({bps:+d} bps)"

    if not exp.get("available"):
        out["detail"] = "no Screener history to judge the trend against"
        return out

    bar = STEP_UP_PRICED if priced_in else STEP_UP
    if seasonal:
        res = {}
        for m in ("ebitda", "pat"):
            ya = yago[m] if yago[m] is not None else exp.get(f"{m}_year_ago")
            ttm = exp.get(f"{m}_ttm_growth")
            c = cur[m]
            if c is None or ya is None:
                continue
            if ya <= 0:
                res[m] = ("turnaround" if c > 0 else None, f"{m.upper()} {_cr(c)} vs {_cr(ya)} a year ago")
                continue
            g = (c - ya) / ya * 100
            if ttm is None:
                res[m] = (None, f"{m.upper()} {g:+.0f}% YoY")
                continue
            ratio = (1 + g / 100) / (1 + ttm / 100) if ttm > -100 else None
            hi = 1 + (SEASONAL_BAND + (bar - STEP_UP))
            v = None if ratio is None else ("above" if ratio > hi else "below" if ratio < 1 - SEASONAL_BAND else "in line")
            res[m] = (v, f"{'EBITDA' if m == 'ebitda' else 'PAT'} {g:+.0f}% YoY against {ttm:+.0f}% over the trailing year")
    else:
        res = {}
        for m in ("ebitda", "pat"):
            last = prevq[m] if prevq[m] is not None else exp.get(f"{m}_last")
            v = _vs_run_rate(cur[m], last, exp.get(f"{m}_avg3"), bar)
            if v is None:
                continue
            res[m] = (v, f"{'EBITDA' if m == 'ebitda' else 'PAT'} {_cr(cur[m])} vs last quarter {_cr(last)}, "
                         f"3-quarter average {_cr(exp.get(f'{m}_avg3'))}")
    primary = res.get("ebitda") if res.get("ebitda") and res["ebitda"][0] else res.get("pat")
    if not primary or not primary[0]:
        out["detail"] = "trend could not be computed from the figures read"
        return out
    verdict = primary[0]
    other = res.get("pat") if primary is res.get("ebitda") else None
    detail = primary[1]
    if verdict == "above" and other and other[0] == "below":
        # Operations up, bottom line down: tax, interest or a one-off took it.
        verdict = "in line"
        detail += f"; but {other[1]}"
    out["verdict"] = verdict
    out["detail"] = detail
    if priced_in and verdict != "above":
        out["detail"] += f" (bar raised to +{bar * 100:.0f}%: price already up / valuation rich)"
    return out


# ─── The gate ───────────────────────────────────────────────────────────────

def gate(suggestion: str, highlights: dict, trend: dict, price: dict) -> tuple:
    """
    (suggestion, reasons). A BUY that fails any check becomes NA, with the
    reasons. Other verdicts pass through untouched.
    """
    if (suggestion or "").upper() != "BUY":
        return suggestion, []
    reasons = []
    risk = (highlights.get("reaction_risk") or "").strip()
    if not risk.lower().startswith("low"):
        reasons.append(f"reaction risk {risk.split(':')[0].strip().lower() or 'not stated'}")
    flags = highlights.get("red_flags") or []
    if flags:
        reasons.append(f"{len(flags)} red flag{'s' if len(flags) > 1 else ''}: {flags[0][:120]}")
    v = (trend or {}).get("verdict")
    if v in ("in line", "below"):
        reasons.append(f"{v} with its {trend.get('basis', 'trend')} — {trend.get('detail', '')}".strip(" —"))
    bps = (trend or {}).get("margin_bps")
    if bps is not None and bps <= -MARGIN_DROP_BPS:
        reasons.append(trend.get("margin_detail") or f"EBITDA margin {bps:+d} bps")
    if (price or {}).get("priced_in") and v not in ("above", "turnaround"):
        reasons.append("; ".join(price.get("flags") or []))
    if reasons:
        return "NA", reasons
    return suggestion, []


def run(db, symbol: str, quarter: str, exp: Optional[dict], pc: Optional[dict],
        suggestion: str, metrics: dict, highlights: dict) -> tuple:
    """
    Everything after the AI answers: trend check, gate, and the record kept
    in highlights["checks"]. Returns (suggestion, checks).
    """
    exp = exp or {}
    pc = pc or {}
    trend = trend_check(exp, metrics, priced_in=bool(pc.get("priced_in")))
    final, reasons = gate(suggestion, highlights, trend, pc)
    checks = {
        "trend": trend,
        "seasonal": bool(exp.get("seasonal")),
        "season_why": exp.get("season_why") or "",
        "price": {k: pc.get(k) for k in ("price", "returns", "pe", "market_cap",
                                          "high_52w", "off_high_pct", "flags", "priced_in")},
    }
    if reasons:
        checks["gate"] = {"was": (suggestion or "").upper(), "reasons": reasons}
        logger.info(f"[CHECKS] {symbol}: BUY withheld — {' | '.join(reasons)}")
    return final, checks
