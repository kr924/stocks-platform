"""
Broker forecasts for a company's quarter: found by web search, kept only if
the cited article really says them.

Why: on 15 Oct 2026 Nestle India grew revenue 20% and PAT 36% and the stock
fell up to 9%. Brokers had forecast ₹6,765-6,809 Cr revenue and ₹1,022-1,035
Cr PAT, and that is what it printed. A price moves on the surprise, and the
filing alone cannot say what was expected.

How:
  1. One OpenRouter call on the PREMIUM key with the web plugin (Exa, about
     $0.03 a search: $4 per 1,000 results, 8 results plus the model's tokens), restricted to Indian financial
     news sites. It asks for the brokers' published estimates for the
     quarter, with source URLs.
  2. Every figure is checked against the cited article's text: OpenRouter's
     citation content, or the page itself fetched here. A figure that does
     not appear there is dropped. Models invent plausible numbers; a
     consensus built from them would be worse than none.
  3. The consensus is the median of what survives, per line.

When: 08:40 IST on a results day, for every company on that day's calendar
(cache_results_day), and on demand when an analysis runs with nothing on
file. At most DAILY_CAP searches a day. Rows are kept per symbol and quarter
in broker_estimates. An empty result is retried after 6 hours, because
previews often appear the day before.

Comparison against the actual figures is arithmetic, in
result_compare.estimates_view, not the model's opinion.
"""
import json
import logging
import os
import re
import statistics
import threading
from datetime import datetime, timedelta
from typing import Optional

import requests

logger = logging.getLogger("app.consensus")

DAILY_CAP = 80
EMPTY_RETRY_HOURS = 6
METRICS = ("revenue_cr", "ebitda_cr", "ebitda_margin_pct", "pat_cr")

FINANCE_DOMAINS = [
    "business-standard.com", "moneycontrol.com", "economictimes.indiatimes.com",
    "livemint.com", "zeebiz.com", "cnbctv18.com", "ndtvprofit.com",
    "financialexpress.com", "thehindubusinessline.com", "kotakneo.com",
    "angelone.in", "5paisa.com", "upstox.com", "groww.in", "reuters.com",
    "trendlyne.com", "goodreturns.in", "businesstoday.in", "news18.com",
    "indiainfoline.com", "outlookbusiness.com", "whalesbook.com",
]

_lock = threading.Lock()
_count = {"day": "", "n": 0}


def fiscal_label(quarter: str) -> str:
    """'Sep 2026' -> 'Q2 FY27 (quarter ended 30 September 2026)'."""
    from app.services.result_compare import parse_quarter
    p = parse_quarter(quarter)
    if not p:
        return quarter
    year, month = p
    q = {6: 1, 9: 2, 12: 3, 3: 4}.get(month)
    fy = year + 1 if month > 3 else year
    end_day = {3: 31, 6: 30, 9: 30, 12: 31}[month]
    month_name = datetime(year, month, 1).strftime("%B")
    return f"Q{q} FY{fy % 100:02d} (quarter ended {end_day} {month_name} {year})"


# ─── checking a figure against its source ──────────────────────────────────

def _number_pattern(v: float) -> Optional[re.Pattern]:
    if v is None:
        return None
    if abs(v - round(v)) < 1e-9:
        body = str(int(round(v))) + r"(?:\.0+)?"
    else:
        s = f"{v:.2f}".rstrip("0").rstrip(".")
        body = re.escape(s) + r"\d*"
    return re.compile(r"(?<![\d.])" + body + r"(?![\d])")


def figure_in_text(v, text: str) -> bool:
    """Does the figure appear in the text (commas ignored)?"""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return False
    pat = _number_pattern(v)
    return bool(pat and pat.search(text.replace(",", "")))


def _fetch_page_text(url: str) -> str:
    try:
        r = requests.get(url, timeout=12, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml"})
        if r.status_code != 200:
            return ""
        html = r.text
        html = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
        return re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", html))
    except Exception:
        return ""


def verify(estimates: list, citations: list) -> tuple:
    """
    Keep only the figures that appear in the source. Returns (kept, dropped):
    kept carries a "verified" list per broker, dropped counts the figures
    removed.
    """
    by_url = {}
    for c in citations or []:
        u = (c.get("url") or "").split("#")[0]
        if u:
            by_url[u] = by_url.get(u, "") + " " + (c.get("content") or "") + " " + (c.get("title") or "")
    every = " ".join(by_url.values())
    fetched = {}
    kept, dropped = [], 0
    for e in estimates or []:
        if not isinstance(e, dict):
            continue
        url = (e.get("source_url") or "").split("#")[0]
        text = by_url.get(url, "")
        verified = {}
        for m in METRICS:
            v = e.get(m)
            if v in (None, "", "NA"):
                continue
            ok = figure_in_text(v, text) or figure_in_text(v, every)
            if not ok and url.startswith("http"):
                if url not in fetched:
                    fetched[url] = _fetch_page_text(url)
                ok = figure_in_text(v, fetched[url])
            if ok:
                verified[m] = float(v)
            else:
                dropped += 1
        if verified:
            kept.append({"broker": str(e.get("broker") or "unnamed")[:60],
                         **verified,
                         "verified": sorted(verified),
                         "source_url": url, "title": str(e.get("source_title") or "")[:160],
                         "published": str(e.get("published") or "")[:20]})
    return kept, dropped


def consensus_of(brokers: list) -> dict:
    out = {}
    for m in METRICS:
        vals = [b[m] for b in brokers if isinstance(b.get(m), (int, float))]
        if vals:
            out[m] = round(statistics.median(vals), 2)
            out[m + "_n"] = len(vals)
    return out


# ─── the search ────────────────────────────────────────────────────────────

def _settings():
    from app.services.intel_config import get_intel_config
    cfg = get_intel_config().auto_trading_ai
    key = (cfg.get("premium_openrouter_api_key") or "").strip() \
        or os.getenv("OPENROUTER_PREMIUM_API_KEY", "").strip()
    model = (cfg.get("premium_openrouter_model") or "").strip() or "google/gemini-2.5-flash-lite"
    enabled = bool(cfg.get("broker_estimates_enabled", True))
    return enabled, key, model


def search(symbol: str, company_name: str, quarter: str, key: str, model: str) -> dict:
    """One web-search call. Returns {"estimates": [...], "citations": [...], "error": ""}."""
    from app.services.gemini import clean_json_response
    label = fiscal_label(quarter)
    name = company_name or symbol
    prompt = f"""Find brokerage and analyst ESTIMATES (previews, forecasts) for {name} (NSE/BSE: {symbol})
for {label}, published BEFORE the company announced these results.

Look for result previews from brokers such as Kotak, Motilal Oswal (MOFSL), Axis, HDFC Securities,
ICICI Securities, Nuvama, JM Financial, Emkay, Elara, Prabhudas Lilladher, Nirmal Bang, Sharekhan,
or a Bloomberg/Reuters/Trendlyne consensus.

Return ONLY JSON:
{{"estimates": [
  {{"broker": "<name>", "revenue_cr": <number or null>, "ebitda_cr": <number or null>,
    "ebitda_margin_pct": <number or null>, "pat_cr": <number or null>,
    "source_url": "<the article's URL>", "source_title": "<title>", "published": "<YYYY-MM-DD>"}}
]}}

Rules:
- Figures in Rs crore exactly as the article states them (convert only if it states lakh or million).
- Only figures the article actually states for THIS quarter. Never compute, guess or fill in.
- Not the actual results: if the article reports what the company announced, skip it.
- If you find no estimates, return {{"estimates": []}}."""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You find published figures and report them exactly, with sources. Output raw JSON only."},
            {"role": "user", "content": prompt},
        ],
        "plugins": [{"id": "web", "engine": "exa", "max_results": 8,
                     "include_domains": FINANCE_DOMAINS}],
    }
    try:
        res = requests.post("https://openrouter.ai/api/v1/chat/completions", json=payload, timeout=120,
                            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                     "HTTP-Referer": "https://github.com/kr924/stocks-platform",
                                     "X-Title": "Stocks Platform estimates"})
        if not res.ok:
            return {"estimates": [], "citations": [], "error": f"HTTP {res.status_code}: {res.text[:160]}"}
        msg = res.json()["choices"][0]["message"]
        content = msg.get("content") or ""
        cites = []
        for a in msg.get("annotations") or []:
            c = (a or {}).get("url_citation") or {}
            if c.get("url"):
                cites.append({"url": c["url"], "title": c.get("title") or "", "content": c.get("content") or ""})
        try:
            parsed = json.loads(clean_json_response(content))
        except Exception:
            parsed = {}
        ests = parsed.get("estimates") if isinstance(parsed, dict) else None
        return {"estimates": ests if isinstance(ests, list) else [], "citations": cites, "error": ""}
    except Exception as e:
        return {"estimates": [], "citations": [], "error": str(e)[:200]}


def _take_budget() -> bool:
    today = datetime.utcnow().strftime("%Y-%m-%d")
    with _lock:
        if _count["day"] != today:
            _count.update(day=today, n=0)
        if _count["n"] >= DAILY_CAP:
            return False
        _count["n"] += 1
        return True


def stored(db, symbol: str, quarter: str) -> Optional[dict]:
    from app.database import BrokerEstimate
    row = db.query(BrokerEstimate).filter(BrokerEstimate.symbol == symbol.upper(),
                                          BrokerEstimate.quarter == quarter).first()
    if not row or not row.data_json:
        return None
    try:
        data = json.loads(row.data_json)
    except Exception:
        return None
    data["fetched_at"] = row.fetched_at.isoformat() if row.fetched_at else None
    return data


def get_or_fetch(db, symbol: str, company_name: str, quarter: str,
                 force: bool = False, allow_fetch: bool = True) -> Optional[dict]:
    """The stored estimates, fetching them first if missing or stale."""
    from app.database import BrokerEstimate
    if not symbol or not quarter:
        return None
    sym = symbol.upper()
    row = db.query(BrokerEstimate).filter(BrokerEstimate.symbol == sym,
                                          BrokerEstimate.quarter == quarter).first()
    data = None
    if row and row.data_json:
        try:
            data = json.loads(row.data_json)
        except Exception:
            data = None
    fresh_enough = bool(data) and (
        data.get("brokers") or
        (row.fetched_at and datetime.utcnow() - row.fetched_at < timedelta(hours=EMPTY_RETRY_HOURS)))
    if (fresh_enough and not force) or not allow_fetch:
        return data

    enabled, key, model = _settings()
    if not enabled:
        return data
    if not key:
        logger.info(f"[ESTIMATES] {sym}: no premium OpenRouter key; web search needs credit")
        return data
    if not _take_budget():
        logger.warning(f"[ESTIMATES] daily cap of {DAILY_CAP} searches reached; {sym} skipped")
        return data

    found = search(sym, company_name, quarter, key, model)
    brokers, dropped = verify(found["estimates"], found["citations"])
    out = {
        "quarter": quarter, "fiscal": fiscal_label(quarter),
        "brokers": brokers, "consensus": consensus_of(brokers),
        "dropped_unverified": dropped, "model": model,
        "sources_seen": len(found["citations"]), "error": found["error"],
    }
    logger.info(f"[ESTIMATES] {sym} {quarter}: {len(brokers)} broker(s) verified, "
                f"{dropped} figure(s) dropped as not in the source"
                f"{'; error ' + found['error'] if found['error'] else ''}")
    try:
        if row is None:
            row = BrokerEstimate(symbol=sym, quarter=quarter)
            db.add(row)
        row.data_json = json.dumps(out)
        row.fetched_at = datetime.utcnow()
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning(f"[ESTIMATES] could not store {sym}: {e}")
    return out


def cache_results_day(db) -> int:
    """08:40 IST: estimates for every company on today's results calendar."""
    from app.services.results_calendar import collect_results_calendar, ist_today
    from app.services.result_compare import infer_quarter
    enabled, key, _ = _settings()
    if not enabled or not key:
        return 0
    quarter = infer_quarter(datetime.utcnow())
    today = ist_today()
    n = 0
    for row in collect_results_calendar(db, today, today):
        sym = (row.get("symbol") or "").upper()
        if not sym:
            continue
        try:
            get_or_fetch(db, sym, row.get("company_name") or "", quarter)
            n += 1
        except Exception as e:
            db.rollback()
            logger.warning(f"[ESTIMATES] {sym}: {e}")
    return n


def prompt_block(data: Optional[dict]) -> str:
    """The estimates as lines for the earnings AI's prompt, or ""."""
    if not data or not data.get("brokers"):
        return ""
    lines = ["=== BROKER ESTIMATES (published before the results; every figure checked against its article) ==="]
    names = {"revenue_cr": "revenue", "ebitda_cr": "EBITDA", "ebitda_margin_pct": "EBITDA margin", "pat_cr": "PAT"}
    for b in data["brokers"]:
        figs = ", ".join(f"{names[m]} {'%.1f%%' % b[m] if m.endswith('pct') else '₹%s Cr' % format(b[m], ',.0f')}"
                         for m in METRICS if m in b)
        lines.append(f"  {b['broker']}: {figs}")
    c = data.get("consensus") or {}
    cons = ", ".join(f"{names[m]} {'%.1f%%' % c[m] if m.endswith('pct') else '₹%s Cr' % format(c[m], ',.0f')}"
                     for m in METRICS if m in c)
    if cons:
        lines.append(f"  Consensus (median): {cons}")
    return "\n".join(lines) + "\n"
