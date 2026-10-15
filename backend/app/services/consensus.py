"""
Broker forecasts for a company's quarter: found by web search, kept only if
the cited article really says them.

Why: on 15 Oct 2026 Nestle India grew revenue 20% and PAT 36% and the stock
fell up to 9%. Brokers had forecast ₹6,765-6,809 Cr revenue and ₹1,022-1,035
Cr PAT, and that is what it printed. A price moves on the surprise, and the
filing alone cannot say what was expected.

How (two ways to search; step 2 and 3 are the same for both):
  1a. Own search, the default and the only automatic one. Bing News RSS for
      "<company> Q2 FY27 results preview", the articles on Indian financial
      sites downloaded here, and a model reading them. On the free key with
      a free model it costs nothing.
  1b. OpenRouter's web search (Exa) on the premium key: about $0.03 a search
      ($4 per 1,000 results, 8 results) whatever the model. Only when the
      operator picks it with "Search again".
  2. Every figure is checked against the article's text. A figure that does
     not appear there is dropped. Models invent plausible numbers; a
     consensus built from them would be worse than none.
  3. The consensus is the median of what survives, per line.

When: 08:40 IST on a results day, for every company on that day's calendar
(cache_results_day, own search, free key only), and when an analysis runs
with nothing on file (own search too). Rows are kept per symbol and quarter
in broker_estimates. A completed search is never repeated automatically,
even when it found nothing; only a failed call is retried. "Search again"
on the panel forces a new one with the method and model chosen there, and
keeps the earlier brokers if the new search finds none. DAILY_CAP bounds
the paid searches.

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
    """(enabled, premium key, premium model) — the paid web search's account."""
    from app.services.intel_config import get_intel_config
    cfg = get_intel_config().auto_trading_ai
    key = (cfg.get("premium_openrouter_api_key") or "").strip() \
        or os.getenv("OPENROUTER_PREMIUM_API_KEY", "").strip()
    model = (cfg.get("premium_openrouter_model") or "").strip() or "google/gemini-2.5-flash-lite"
    enabled = bool(cfg.get("broker_estimates_enabled", True))
    return enabled, key, model


def _free_settings():
    """(free key, free model or "" for the best free pick) — the ₹0 search."""
    from app.services.intel_config import get_intel_config
    from app.services.openrouter_models import _clean_key
    cfg = get_intel_config().auto_trading_ai
    key = _clean_key(cfg.get("free_openrouter_api_key")) or _clean_key(os.getenv("OPENROUTER_API_KEY"))
    return key, (cfg.get("free_openrouter_model") or "").strip()


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


# ─── our own search: ₹0 ────────────────────────────────────────────────────
#
# OpenRouter's web plugin bills $4 per 1,000 results whatever the model, so
# a free key with a free model still pays about $0.03 a search. The default
# search is done here instead: Bing News RSS (no key, publisher URLs in its
# redirect links), the matching articles downloaded on the VM, and a model
# reading them. On the free key with a free model the whole lookup costs
# nothing. Every figure still has to appear in the article text (verify).

NEWS_RSS = "https://www.bing.com/news/search"
MAX_ARTICLES = 6
ARTICLE_CHARS = 5000
_RELEVANT = re.compile(r"crore|\bcr\b|estimat|expect|forecast|preview|seen at|likely|consensus|"
                       r"brokerage|analyst|revenue|profit|ebitda|\bpat\b|margin|net sales|"
                       r"kotak|motilal|mofsl|axis|hdfc sec|icici sec|nuvama|jm financial|emkay|"
                       r"elara|prabhudas|nirmal bang|sharekhan|antique|centrum|yes sec|choice", re.I)


def _allowed(url: str) -> bool:
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in FINANCE_DOMAINS)


def _publisher_url(link: str) -> str:
    """Bing wraps each result in apiclick.aspx?...&url=<publisher URL>."""
    from urllib.parse import urlparse, parse_qs
    q = parse_qs(urlparse(link).query)
    return (q.get("url") or [link])[0]


def _http_get(url: str, timeout: float = 12):
    """A browser-like GET; Chrome's TLS handshake where curl_cffi is present."""
    try:
        from app.services import bse_http
        if bse_http.HAVE_CFFI:
            return bse_http.get(url, headers={"Accept": "text/html,application/xhtml+xml,application/rss+xml,*/*"},
                                timeout=timeout)
    except Exception:
        pass
    return requests.get(url, timeout=timeout, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/rss+xml,*/*"})


def news_links(company_name: str, symbol: str, quarter: str) -> list:
    """Candidate preview articles from Indian financial news sites, newest first."""
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime
    name = company_name or symbol
    short = fiscal_label(quarter).split(" (")[0]          # "Q2 FY27"
    queries = [f"{name} {short} results preview", f"{name} {short} estimates brokerage"]
    seen, out = set(), []
    cutoff = datetime.utcnow() - timedelta(days=45)
    for q in queries:
        try:
            r = _http_get(f"{NEWS_RSS}?q={requests.utils.quote(q)}&format=rss&setlang=en-IN", timeout=10)
            if r.status_code != 200:
                continue
            items = [{"link": (i.findtext("link") or "").strip(),
                      "title": (i.findtext("title") or "").strip(),
                      "published": (i.findtext("pubDate") or "").strip()}
                     for i in ET.fromstring(r.content).iter("item")]
        except Exception as e:
            logger.info(f"[ESTIMATES] news search failed for {q!r}: {e}")
            continue
        for it in items:
            url = _publisher_url(it.get("link") or "")
            if not url.startswith("http") or url in seen or not _allowed(url):
                continue
            try:
                when = parsedate_to_datetime(it.get("published")).replace(tzinfo=None)
            except Exception:
                when = None
            if when and when < cutoff:
                continue
            seen.add(url)
            out.append({"url": url, "title": it.get("title") or "",
                        "published": when.strftime("%Y-%m-%d") if when else ""})
    return out[:MAX_ARTICLES * 2]


def article_text(url: str) -> str:
    """The article's own text, cut to the paragraphs that carry figures."""
    try:
        r = _http_get(url, timeout=12)
        if r.status_code != 200 or not r.text:
            return ""
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(r.text, "lxml")
        bodies = []
        for tag in soup.find_all("script", type="application/ld+json"):
            for m in re.finditer(r'"articleBody"\s*:\s*"((?:[^"\\]|\\.)*)"', tag.string or ""):
                try:
                    bodies.append(json.loads('"' + m.group(1) + '"'))
                except Exception:
                    pass
        for t in soup(["script", "style", "nav", "footer", "header", "aside", "form"]):
            t.decompose()
        root = soup.find("article") or soup.body or soup
        parts = [el.get_text(" ", strip=True) for el in root.find_all(["p", "li", "td", "h1", "h2", "h3"])]
        paras = [re.sub(r"\s+", " ", x) for x in bodies + parts if x]
        keep = [x for x in dict.fromkeys(paras) if re.search(r"\d", x) and _RELEVANT.search(x)]
        return "\n".join(keep)[:ARTICLE_CHARS]
    except Exception as e:
        logger.info(f"[ESTIMATES] could not read {url}: {e}")
        return ""


def own_search(symbol: str, company_name: str, quarter: str, key: str, model: str,
               free_only: bool = True) -> dict:
    """
    Search the news ourselves and have `model` read the articles. Same shape
    as search(): {"estimates", "citations", "error"}. An empty list with no
    error means the search completed and found nothing.
    """
    from concurrent.futures import ThreadPoolExecutor
    from app.services.gemini import call_openrouter
    label = fiscal_label(quarter)
    name = company_name or symbol
    links = news_links(name, symbol, quarter)
    if not links:
        return {"estimates": [], "citations": [], "error": "", "method": "own_search"}
    with ThreadPoolExecutor(max_workers=4) as ex:
        texts = list(ex.map(lambda l: article_text(l["url"]), links))
    cites = [{**l, "content": t} for l, t in zip(links, texts) if t][:MAX_ARTICLES]
    if not cites:
        return {"estimates": [], "citations": [], "method": "own_search",
                "error": f"{len(links)} article(s) found but none could be read"}

    blocks = "\n\n".join(f"[{i + 1}] {c['title']}\nURL: {c['url']}\nPublished: {c['published'] or 'unknown'}\n{c['content']}"
                          for i, c in enumerate(cites))
    prompt = f"""Below are news articles about {name} (NSE/BSE: {symbol}). Extract brokerage and
analyst ESTIMATES (previews, forecasts) for {label}, published BEFORE the company announced its results.

Return ONLY JSON:
{{"estimates": [
  {{"broker": "<brokerage name>", "revenue_cr": <number or null>, "ebitda_cr": <number or null>,
    "ebitda_margin_pct": <number or null>, "pat_cr": <number or null>,
    "source_url": "<the URL line of the article it came from, exactly>", "source_title": "<title>",
    "published": "<YYYY-MM-DD>"}}
]}}

Rules:
- One entry per brokerage per article. Figures in Rs crore exactly as the article states them
  (convert only if it states lakh or million).
- Only figures the article states for THIS quarter. Never compute, guess or fill in.
- Not the actual results: if an article reports what the company announced, skip it.
- An estimate with no brokerage named may use "Consensus" only if the article says consensus or poll.
- If there are no estimates, return {{"estimates": []}}.

{blocks}"""
    try:
        fallbacks = []
        if free_only:
            from app.services.openrouter_models import free_fallbacks
            fallbacks = free_fallbacks(exclude=model)
            if not model and fallbacks:
                model, fallbacks = fallbacks[0], fallbacks[1:]
        parsed = call_openrouter(prompt, key, model=model or None, free_only=free_only,
                                 fallbacks=fallbacks or None)
    except Exception as e:
        return {"estimates": [], "citations": cites, "method": "own_search", "error": str(e)[:200]}
    ests = parsed.get("estimates") if isinstance(parsed, dict) else None
    return {"estimates": ests if isinstance(ests, list) else [], "citations": cites,
            "error": "", "method": "own_search"}


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


def _plan(method: str, model: Optional[str]):
    """
    (key, model, free_only, method) for one lookup, or (None, reason, ...).

    own  our news search; the model reads the articles. A ":free" model (or
         none chosen) runs on the free key and costs nothing; a paid model
         runs on the premium key and costs only its tokens (~$0.001).
    web  OpenRouter's web search on the premium key (~$0.03), any model.
    """
    from app.services.openrouter_models import is_free_model
    enabled, premium_key, premium_model = _settings()
    if not enabled:
        return None, "broker estimates are switched off in AI settings", False, method
    model = (model or "").strip()
    if method == "web":
        if not premium_key:
            return None, "the paid web search needs the premium OpenRouter key", False, method
        return premium_key, model or premium_model, False, method
    free_key, free_model = _free_settings()
    if not model or is_free_model(model):
        if not free_key:
            return None, "no free OpenRouter key is set (AI settings)", True, "own"
        return free_key, model or free_model, True, "own"
    if not premium_key:
        return None, f"{model} is a paid model and no premium key is set", False, "own"
    return premium_key, model, False, "own"


def get_or_fetch(db, symbol: str, company_name: str, quarter: str,
                 force: bool = False, allow_fetch: bool = True,
                 method: str = "own", model: Optional[str] = None) -> Optional[dict]:
    """
    The stored estimates; searched only if none were ever found (or the last
    call failed). Automatic lookups use our own ₹0 search on the free key;
    "Search again" passes force with the method and model the operator chose.
    """
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
    # Fetched once, kept for good (owner's call, 15 Oct 2026). A search that
    # completed, brokers found or not, is never repeated automatically: not by
    # the analysis, not by a re-analyse, not by the next 08:40 run. Only a
    # call that failed outright (no answer, so nothing was learned) may be
    # tried again. "Search again" on the panel passes force=True.
    already = bool(data) and not data.get("error")
    if (already and not force) or not allow_fetch:
        return data

    key, chosen, free_only, method = _plan(method, model)
    if not key:
        logger.info(f"[ESTIMATES] {sym}: not searched — {chosen}")
        if force:
            return {**(data or {}), "_refused": chosen}
        return data
    if method == "web" and not _take_budget():
        logger.warning(f"[ESTIMATES] daily cap of {DAILY_CAP} paid searches reached; {sym} skipped")
        return data

    if method == "web":
        found = search(sym, company_name, quarter, key, chosen)
    else:
        found = own_search(sym, company_name, quarter, key, chosen, free_only=free_only)
    brokers, dropped = verify(found["estimates"], found["citations"])
    out = {
        "quarter": quarter, "fiscal": fiscal_label(quarter),
        "brokers": brokers, "consensus": consensus_of(brokers),
        "dropped_unverified": dropped, "model": chosen or "best free model",
        "method": "web search" if method == "web" else "own search",
        "sources_seen": len(found["citations"]), "error": found["error"],
    }
    logger.info(f"[ESTIMATES] {sym} {quarter} ({out['method']}, {out['model']}): "
                f"{len(brokers)} broker(s) verified, {dropped} figure(s) dropped as not in the source"
                f"{'; error ' + found['error'] if found['error'] else ''}")
    if data and data.get("brokers") and (found["error"] or not brokers):
        # A forced search that failed or found nothing keeps what was found before.
        return {**data, "_kept": out}
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
    """
    08:40 IST: estimates for every company on today's results calendar, by
    our own search on the free key only. Nothing here is paid. Paced to stay
    under the free models' 20 requests a minute.
    """
    import time
    from app.services.results_calendar import collect_results_calendar, ist_today
    from app.services.result_compare import infer_quarter
    enabled, _, _ = _settings()
    free_key, _ = _free_settings()
    if not enabled or not free_key:
        logger.info("[ESTIMATES] 08:40 lookup skipped: estimates off or no free key")
        return 0
    quarter = infer_quarter(datetime.utcnow())
    today = ist_today()
    n = 0
    for row in collect_results_calendar(db, today, today):
        sym = (row.get("symbol") or "").upper()
        if not sym:
            continue
        try:
            before = stored(db, sym, quarter)
            get_or_fetch(db, sym, row.get("company_name") or "", quarter, method="own")
            n += 1
            if not before or before.get("error"):
                time.sleep(4)          # a model call was made; free tier allows 20/min
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
