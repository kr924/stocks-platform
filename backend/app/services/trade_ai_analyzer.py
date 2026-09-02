"""
Premium AI Analyzer for Trading Engine — Cloud-only, configurable provider.
Separate from the standard intelligence AI pipeline.
"""
import json
import logging
import os
import threading
from datetime import datetime
from typing import Optional, Tuple

logger = logging.getLogger("app.trade_ai_analyzer")


def analyze_trade_event(
    symbol: str,
    title: str,
    description: str = "",
    provider: str = "groq",
    config_id: int = None,
    pdf_text: str = "",
) -> Optional[dict]:
    """
    Run premium cloud AI analysis on a trade-triggered NSE event.
    Returns parsed analysis dict or None on failure.
    Stores result in TradeAILog table.
    """
    from app.services.gemini import (
        reload_env_vars, call_gemini, call_groq, call_openai,
        call_anthropic, call_openrouter, clean_json_response
    )
    from app.services.key_manager import key_manager

    env = reload_env_vars()
    key_manager.sync_all(env)

    prompt = _build_trade_analysis_prompt(symbol, title, description, pdf_text)

    # Try the configured provider first, then fallback chain
    provider_chain = [provider]
    for fb in ["groq", "openrouter", "gemini", "openai", "anthropic"]:
        if fb not in provider_chain:
            provider_chain.append(fb)

    result = None
    used_provider = provider
    for prov in provider_chain:
        ks = key_manager.acquire_key_for_provider(prov)
        if not ks:
            continue
        try:
            logger.info(f"🔬 [TRADE AI]: Calling {prov.upper()} for #{symbol} '{title[:40]}...'")
            if prov == "groq":
                result = call_groq(prompt, ks.key, "llama-3.3-70b-versatile")
            elif prov == "openrouter":
                result = call_openrouter(prompt, ks.key)
            elif prov == "gemini":
                result = call_gemini(prompt, ks.key)
            elif prov == "openai":
                result = call_openai(prompt, ks.key)
            elif prov == "anthropic":
                result = call_anthropic(prompt, ks.key)
            else:
                result = None

            key_manager.release_key(ks, is_rate_limited=False)
            if result:
                used_provider = prov
                break
        except Exception as e:
            is_429 = "429" in str(e) or "Rate limit" in str(e)
            key_manager.release_key(ks, is_rate_limited=is_429, backoff_seconds=60.0)
            logger.warning(f"[TRADE AI] {prov.upper()} failed: {e}")
            continue

    # Parse and store result
    analysis = None
    if result and "analyses" in result and result["analyses"]:
        analysis = result["analyses"][0]

    # Save to TradeAILog
    try:
        from app.database import SessionLocal, TradeAILog
        db = SessionLocal()
        try:
            log_entry = TradeAILog(
                config_id=config_id,
                symbol=symbol,
                provider=used_provider,
                prompt_summary=f"Trade AI analysis for {symbol}: {title[:100]}",
                ai_sentiment=analysis.get("sentiment", "neutral") if analysis else "unknown",
                ai_impact_score=analysis.get("impact_score", 0.0) if analysis else 0.0,
                ai_summary=analysis.get("summary", "") if analysis else f"AI analysis failed for {symbol}",
                raw_response=json.dumps(result) if result else None,
                nse_event_title=title,
                created_at=datetime.utcnow()
            )
            db.add(log_entry)
            db.commit()
            logger.info(f"✅ [TRADE AI SAVED]: {used_provider.upper()} analysis for {symbol}")
        except Exception as db_err:
            db.rollback()
            logger.error(f"Failed to save TradeAILog: {db_err}")
        finally:
            db.close()
    except Exception:
        pass

    # Send Telegram alert with trade AI analysis
    try:
        from app.services.telegram_notifier import send_telegram_alert
        send_telegram_alert(
            title=title,
            symbol=symbol,
            sentiment=analysis.get("sentiment", "neutral") if analysis else "unknown",
            impact_score=analysis.get("impact_score", 0.0) if analysis else 0.0,
            summary=analysis.get("summary", "") if analysis else "AI analysis pending",
            provider=used_provider,
            alert_type="🔬 TRADE AI ANALYSIS"
        )
    except Exception:
        pass

    return analysis


import re

# ─── Structured earnings metric grid ────────────────────────────────────────

# Row order is fixed so the table reads identically everywhere it is rendered.
METRIC_ROWS = ("revenue", "expenses", "other_income", "pat", "ebitda")
# Column order: the current print, the year-ago figure it is measured against,
# then the two changes, then the broker estimate. Estimates are optional — they
# only exist when a research house has published one, so they usually read NA.
METRIC_COLS = (
    "current_qtr",
    "last_year_same_qtr",
    "yoy_change_pct",
    "qoq_change_pct",
    "estimated",
)

_ROW_LABELS = {
    "revenue": "Revenue",
    "expenses": "Expenses",
    "other_income": "Other Income",
    "pat": "Profit (PAT)",
    "ebitda": "EBITDA",
}
_COL_LABELS = {
    "current_qtr": "Current Qtr",
    "last_year_same_qtr": "YoY",
    "yoy_change_pct": "YoY %",
    "qoq_change_pct": "QoQ %",
    "estimated": "Estimated",
}

# Anything a model emits to mean "I could not determine this".
_NA_TOKENS = {"", "NA", "N/A", "NONE", "NULL", "-", "--", "UNKNOWN", "NOT AVAILABLE",
              "NOT PROVIDED", "NOT DISCLOSED", "NIL", "TBD", "?"}


def _clean_cell(value) -> str:
    """Normalise one cell, collapsing every flavour of 'unknown' to ''."""
    if value is None:
        return ""
    text = str(value).strip()
    if text.upper() in _NA_TOKENS:
        return ""
    return text


def normalize_metrics(raw) -> dict:
    """
    Coerce whatever the model returned into the fixed row x column grid.

    Missing cells become "NA" rather than being dropped, so the table always has
    the same shape and a gap is visibly a gap.
    """
    raw = raw if isinstance(raw, dict) else {}
    grid = {}
    for row in METRIC_ROWS:
        cells = raw.get(row)
        cells = cells if isinstance(cells, dict) else {}
        grid[row] = {
            col: (_clean_cell(cells.get(col)) or "NA") for col in METRIC_COLS
        }
    return grid


def metrics_are_usable(metrics: dict) -> bool:
    """
    True when the grid carries enough to justify a verdict.

    Revenue and PAT for the current quarter are the minimum: without both, any
    buy/sell/beat/miss call would be asserting more than the filing supports.
    """
    if not metrics:
        return False
    revenue = metrics.get("revenue", {}).get("current_qtr", "NA")
    pat = metrics.get("pat", {}).get("current_qtr", "NA")
    return bool(_clean_cell(revenue)) and bool(_clean_cell(pat))


def validate_metrics(metrics: dict) -> dict:
    """
    Check an extracted grid against itself.

    Layout is the weak point in reading these filings: every company arranges its
    P&L differently, many are scans, and a number can be picked up from the wrong
    row or the wrong column while still looking like a plausible figure. Nothing
    downstream can tell, because a wrong number is still a number.

    The strongest available check needs no external source: the model reports
    both the values and the percentage change between them, so those two claims
    must agree. If it says revenue went from 386 to 536 but also says that is
    +12%, one of the three was misread. Sign and magnitude checks catch the
    rest — expenses are never negative, PAT never exceeds revenue.

    Returns:
        {"issues": [str], "hard_failures": int, "reconciled": int, "trustworthy": bool}
    """
    from app.services.screener_quarters import parse_ai_value

    issues, hard, reconciled = [], 0, 0
    if not metrics:
        return {"issues": ["no metrics"], "hard_failures": 1, "reconciled": 0, "trustworthy": False}

    def pct(text):
        if not _clean_cell(text):
            return None
        try:
            return float(str(text).replace("%", "").replace("+", "").strip())
        except ValueError:
            return None

    for row in METRIC_ROWS:
        cells = metrics.get(row, {})
        cur = parse_ai_value(cells.get("current_qtr"))
        ly = parse_ai_value(cells.get("last_year_same_qtr"))
        stated = pct(cells.get("yoy_change_pct"))
        label = _ROW_LABELS[row]

        # 1. The model's own percentage must match its own numbers.
        if cur is not None and ly not in (None, 0) and stated is not None:
            computed = (cur - ly) / abs(ly) * 100
            # Allow rounding and a modest relative drift before calling it wrong.
            tolerance = max(2.0, abs(stated) * 0.10)
            if abs(computed - stated) > tolerance:
                hard += 1
                issues.append(
                    f"{label}: stated YoY {stated:+.1f}% but {cur:,.2f} vs {ly:,.2f} "
                    f"computes to {computed:+.1f}% — a value or the change was misread"
                )
            else:
                reconciled += 1

        # 2. Sign sanity. Revenue and expenses are never negative on a P&L.
        for key, value in (("current_qtr", cur), ("last_year_same_qtr", ly)):
            if value is not None and value < 0 and row in ("revenue", "expenses", "ebitda"):
                hard += 1
                issues.append(f"{label}: {key.replace('_', ' ')} is negative ({value:,.2f})")

    # 3. Magnitude sanity across rows.
    rev = parse_ai_value(metrics.get("revenue", {}).get("current_qtr"))
    pat = parse_ai_value(metrics.get("pat", {}).get("current_qtr"))
    exp = parse_ai_value(metrics.get("expenses", {}).get("current_qtr"))
    if rev and pat is not None and abs(pat) > abs(rev):
        hard += 1
        issues.append(f"PAT {pat:,.2f} exceeds revenue {rev:,.2f} — rows were likely transposed")
    if rev and exp is not None and exp > abs(rev) * 3:
        hard += 1
        issues.append(f"Expenses {exp:,.2f} are implausible against revenue {rev:,.2f}")

    return {
        "issues": issues,
        "hard_failures": hard,
        "reconciled": reconciled,
        "trustworthy": hard == 0,
    }


def _cell_summary(metrics: dict, row: str) -> str:
    """One-line rendering of a row, for the legacy single-string columns."""
    cells = metrics.get(row, {})
    return (
        f"{cells.get('current_qtr', 'NA')}"
        f" | YoY: {cells.get('last_year_same_qtr', 'NA')}"
        f" ({cells.get('yoy_change_pct', 'NA')})"
        f" | QoQ: {cells.get('qoq_change_pct', 'NA')}"
        f" | Est: {cells.get('estimated', 'NA')}"
    )


def render_metrics_table(metrics: dict) -> str:
    """
    Render the grid as fixed-width text.

    Used inside Telegram's <pre> block and as a plain-text fallback, so column
    widths are computed rather than hardcoded.
    """
    if not metrics:
        return "No metrics extracted."

    headers = [""] + [_COL_LABELS[c] for c in METRIC_COLS]
    rows = [
        [_ROW_LABELS[r]] + [metrics.get(r, {}).get(c, "NA") for c in METRIC_COLS]
        for r in METRIC_ROWS
    ]

    widths = [
        max(len(headers[i]), max((len(row[i]) for row in rows), default=0))
        for i in range(len(headers))
    ]
    line = lambda cells: "  ".join(c.ljust(widths[i]) for i, c in enumerate(cells)).rstrip()

    out = [line(headers), "-" * min(sum(widths) + 2 * len(widths), 80)]
    out += [line(r) for r in rows]
    return "\n".join(out)


# Words that carry no identifying signal in an Indian company name.
_NAME_STOPWORDS = {
    "LIMITED", "LTD", "PRIVATE", "PVT", "COMPANY", "CORPORATION", "CORP",
    "INDIA", "INDIAN", "INDUSTRIES", "INDUSTRY", "ENTERPRISES", "ENTERPRISE",
    "HOLDINGS", "GROUP", "AND", "THE", "OF", "PRODUCTS", "SERVICES",
    "TECHNOLOGIES", "TECHNOLOGY", "SOLUTIONS", "INTERNATIONAL", "GLOBAL",
    "FINANCE", "FINANCIAL", "INVESTMENTS", "INVESTMENT", "ENGINEERING",
}


# Phrases a model uses when it was asked about a document it never received.
# Deliberately anchored rather than loose substrings: a bare "no document"
# also matches legitimate analysis wording such as "no document-level segment
# breakdown was disclosed", which would discard a perfectly good result.
_NO_DOCUMENT_RE = re.compile(
    r"(?:"
    r"no\s+(?:document|attachment|file|pdf)\s+(?:was\s+)?(?:provided|attached|received|uploaded|shared|available)"
    r"|there\s+is\s+no\s+(?:document|attachment|file|pdf)\b"
    r"|(?:do\s+not|don't|cannot|can't|could\s+not|couldn't)\s+(?:see|find|access|open|read)\s+"
    r"(?:any\s+)?(?:the\s+)?(?:document|attachment|file|pdf|earnings\s+document)"
    r"|(?:did\s+not|didn't)\s+receive\s+(?:any\s+)?(?:document|attachment|file|pdf)"
    r"|please\s+(?:upload|provide|share|attach)\s+(?:the\s+|a\s+)?(?:document|attachment|file|pdf|earnings)"
    r"|(?:document|attachment|file)\s+(?:was\s+)?not\s+(?:provided|attached|received|found)"
    r"|(?:do\s+not|don't)\s+have\s+access\s+to\s+(?:the\s+)?(?:document|attachment|file|pdf)"
    r"|unable\s+to\s+(?:access|read|open)\s+(?:the\s+)?(?:document|attachment|file|pdf)"
    r")",
    re.IGNORECASE,
)


def response_indicates_missing_document(text: str) -> bool:
    """
    True when the model is telling us it had no document to read.

    The browser-driven Gemini path can silently submit a prompt whose attachment
    never landed. Gemini then answers from the prompt text alone — naming the
    company because the prompt named it, which means the wrong-company guard
    will not catch it. Storing that as a verdict would present invented figures
    as extracted ones.
    """
    if not text:
        return False
    return bool(_NO_DOCUMENT_RE.search(text))


def response_matches_company(text: str, symbol: str, company_name: str = "") -> bool:
    """
    Check that an analysis actually describes the company we asked about.

    The Gemini path drives a real browser session. When that session leaked
    context between requests, analyses came back describing a completely
    different company — a report filed under EMMESSA containing Carborundum's
    figures. Nothing downstream could tell, because the numbers were internally
    consistent and only the company was wrong.

    This is deliberately permissive: it only rejects when the response mentions
    neither the ticker nor any distinctive word from the registered name, which
    is a strong signal the wrong document was analysed.
    """
    if not text:
        return False

    haystack = re.sub(r"[^A-Z0-9 ]+", " ", text.upper())
    padded = f" {haystack} "

    if symbol and f" {symbol.upper()} " in padded:
        return True

    if not company_name:
        # Nothing to check against; do not block on an unknown name.
        return True

    distinctive = [
        w for w in re.split(r"[^A-Za-z0-9]+", company_name.upper())
        if len(w) >= 4 and w not in _NAME_STOPWORDS
    ]
    if not distinctive:
        return True

    return any(f" {w} " in padded for w in distinctive)


# Literal control characters that are legal in scraped text but illegal inside a
# JSON string. Tab and newline are the ones that actually occur.
_JSON_CTRL_IN_STRING = re.compile(r'("(?:[^"\\]|\\.)*")', re.DOTALL)


def _escape_control_chars_in_strings(payload: str) -> str:
    """
    Escape raw newlines/tabs that appear *inside* JSON string literals.

    The Gemini path scrapes textContent out of a <pre> block, so a value the
    model wrapped across lines arrives containing a literal newline. That is
    valid text but invalid JSON, and strict json.loads rejects the whole
    document — which is how a complete, correct 3kB response with real figures
    ended up rendering as an all-NA grid.
    """
    def fix(match):
        s = match.group(1)
        inner = s[1:-1]
        inner = (inner.replace("\r\n", "\\n").replace("\n", "\\n")
                      .replace("\r", "\\n").replace("\t", "\\t"))
        return f'"{inner}"'

    return _JSON_CTRL_IN_STRING.sub(fix, payload)


def _loads_tolerant(payload: str) -> Optional[dict]:
    """Parse JSON, tolerating the artefacts that scraped model output carries."""
    payload = payload.strip()
    attempts = (
        # strict=False alone permits control characters inside strings.
        lambda p: json.loads(p, strict=False),
        lambda p: json.loads(_escape_control_chars_in_strings(p), strict=False),
        # Trailing commas before a close brace/bracket.
        lambda p: json.loads(re.sub(r",(\s*[}\]])", r"\1", p), strict=False),
        lambda p: json.loads(
            re.sub(r",(\s*[}\]])", r"\1", _escape_control_chars_in_strings(p)), strict=False
        ),
    )
    for attempt in attempts:
        try:
            result = attempt(payload)
            if isinstance(result, dict):
                return result
        except Exception:
            continue
    return None


def _balanced_json_objects(text: str):
    """
    Yield every balanced {...} region in `text`, longest-first.

    A single regex cannot do this. Scraped Gemini output regularly contains more
    than one JSON body: the container holds a partial draft as well as the final
    answer, and textContent concatenates them, so the payload looks like a
    truncated object immediately followed by a complete one. Matching the
    outermost braces then spans both and parses as neither.

    Brace counting is string- and escape-aware so a "{" inside a value does not
    throw off the depth.
    """
    starts = []
    depth = 0
    in_string = False
    escaped = False
    found = []

    for i, ch in enumerate(text):
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            if depth == 0:
                starts.append(i)
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and starts:
                    found.append(text[starts.pop() : i + 1])

    # Longest first: the complete body beats a truncated fragment.
    return sorted(found, key=len, reverse=True)


def _restart_point_candidates(text: str):
    """
    Rebuild an object from a payload that holds two concatenated renderings.

    Gemini sometimes emits a compact draft and then a fuller revision inside the
    same response container. Scraped together they look like one object whose
    middle is corrupt: the draft's last string is cut off, then a second set of
    the same keys begins. The complete data is the trailing copy — it is only
    missing its opening brace.

    Every key sitting at the start of a line is a possible restart point, so
    each is tried as the head of a synthetic object. Callers score the results
    and keep the richest, which lands on the final rendering.
    """
    for m in re.finditer(r'(?m)^\s*"(\w+)"\s*:', text):
        yield "{" + text[m.start():]


def _score_candidate(obj: dict) -> int:
    """Prefer the parsed object that actually carries the analysis."""
    if not isinstance(obj, dict):
        return -1
    score = len(obj)
    metrics = obj.get("metrics")
    if isinstance(metrics, dict):
        score += 10 * len(metrics)
        for cells in metrics.values():
            if isinstance(cells, dict):
                score += sum(1 for v in cells.values() if _clean_cell(v))
    if _clean_cell(obj.get("summary")):
        score += 3
    return score


def _extract_json_from_llm(text: str) -> dict:
    """Robustly extract JSON dictionary from LLM output, handling conversational intros and code blocks."""
    if not text:
        return {}
    if isinstance(text, dict):
        return text
    text_str = str(text)

    candidates = []

    # 1. Fenced ```json { ... } ``` code block
    for m in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text_str, re.DOTALL | re.IGNORECASE):
        candidates.append(m.group(1))

    # 2. Every balanced object in the payload, longest first
    candidates.extend(_balanced_json_objects(text_str))

    # 3. The whole payload as-is
    candidates.append(text_str)

    # 4. Rebuilt bodies, for payloads holding two concatenated renderings
    candidates.extend(_restart_point_candidates(text_str))

    # Score every candidate that parses and keep the richest. Returning the
    # first success would settle for a fragment such as {"sentiment": "..."},
    # which parses cleanly but carries none of the analysis.
    best, best_score = None, 0
    for candidate in candidates:
        parsed = _loads_tolerant(candidate)
        if not parsed:
            continue
        score = _score_candidate(parsed)
        if score > best_score:
            best, best_score = parsed, score

    if best:
        return best

    logger.warning(
        f"Could not parse JSON from model output ({len(text_str)} chars). "
        f"Starts: {text_str[:120]!r}"
    )
    return {}


_in_progress_analyses = set()
_in_progress_lock = threading.Lock()


def analyze_earnings_disclosure_2step(
    symbol: str,
    title: str,
    attachment_url: str = "",
    pdf_text: str = "",
    config_id: Optional[int] = None,
    tracking_ref: Optional[str] = None,
    model_override: Optional[str] = None,
) -> Optional[dict]:
    """
    Two-Step AI Analysis Pipeline:
    Step 1: POST to custom REST API (url/api/generate) with dynamic prompt.
    Step 2 (Fallback): If Step 1 fails, request OpenRouter Premium with selected model.
    Saves results to TradeAILog and dispatches Telegram alerts.
    """
    import requests
    from datetime import datetime, timedelta
    from app.services.intel_config import get_intel_config
    from app.services.gemini import clean_json_response, call_openrouter

    # --- Concurrency & Deduplication Lock: Prevent duplicate simultaneous calls (within 15 seconds) ---
    dedup_key = f"{symbol.upper().strip()}:{title.strip()}"
    with _in_progress_lock:
        if dedup_key in _in_progress_analyses:
            logger.info(f"⏭️ [AUTO AI SKIPPED]: Simultaneous analysis currently running for #{symbol}. Skipping duplicate call.")
            print(f"⏭️ [AUTO AI SKIPPED]: Simultaneous analysis currently running for #{symbol}. Skipping duplicate call.")
            return {}
        
        try:
            from app.database import SessionLocal, TradeAILog
            db_check = SessionLocal()
            try:
                cutoff = datetime.utcnow() - timedelta(seconds=15)
                existing_log = db_check.query(TradeAILog).filter(
                    TradeAILog.symbol == symbol.upper().strip(),
                    TradeAILog.nse_event_title == title,
                    TradeAILog.created_at >= cutoff
                ).first()
                if existing_log:
                    logger.info(f"⏭️ [AUTO AI SKIPPED]: Duplicate simultaneous call detected for #{symbol}. Skipping REST API call.")
                    print(f"⏭️ [AUTO AI SKIPPED]: Duplicate simultaneous call detected for #{symbol}. Skipping REST API call.")
                    return json.loads(existing_log.raw_response) if existing_log.raw_response and existing_log.raw_response.startswith('{') else {}
            finally:
                db_check.close()
        except Exception:
            pass

        _in_progress_analyses.add(dedup_key)

    try:
        return _do_analyze_earnings_disclosure_2step(
            symbol, title, attachment_url, pdf_text, config_id, tracking_ref,
            model_override,
        )
    finally:
        with _in_progress_lock:
            _in_progress_analyses.discard(dedup_key)


def _do_analyze_earnings_disclosure_2step(
    symbol: str,
    title: str,
    attachment_url: str = "",
    pdf_text: str = "",
    config_id: Optional[int] = None,
    tracking_ref: Optional[str] = None,
    model_override: Optional[str] = None,
) -> Optional[dict]:
    import base64
    import requests
    from datetime import datetime
    from app.services.intel_config import get_intel_config
    from app.services.gemini import clean_json_response, call_openrouter

    ai_requested_at = datetime.utcnow()

    cfg = get_intel_config()
    auto_ai_cfg = cfg.auto_trading_ai
    
    custom_url = auto_ai_cfg.get("custom_api_url", "http://localhost:3000/api/generate").strip()
    openrouter_key = auto_ai_cfg.get("premium_openrouter_api_key", "").strip() or os.getenv("OPENROUTER_PREMIUM_API_KEY", "").strip() or os.getenv("OPENROUTER_API_KEY", "").strip()
    openrouter_model = auto_ai_cfg.get("premium_openrouter_model", "google/gemini-2.5-flash-lite").strip()
    # A re-analysis can name its own model without disturbing the configured
    # default: the operator is asking "what would a stronger model make of this
    # filing", not changing what every future filing is analysed with.
    if model_override and model_override.strip():
        openrouter_model = model_override.strip()
        logger.info(f"[AUTO AI] {symbol}: model overridden for this run -> {openrouter_model}")

    company_name = ""
    try:
        from app.services.symbol_registry import company_name_for
        company_name = company_name_for(symbol)
    except Exception:
        pass

    prompt = f"""You are an Indian stock market research analyst. Analyse the attached quarterly earnings document for {symbol}{f' ({company_name})' if company_name else ''} and return a strict metric grid.

Compare against the same quarter last year and against broker/analyst estimates (refer to screener.in and public broker research for historical and estimated figures).

Document Extract / Content snippet:
{pdf_text[:3500] if pdf_text else 'Refer to attachment document'}

=== EXTRACTION RULES — FOLLOW EXACTLY ===

1. Fill every cell of the grid below for these five rows:
     revenue, expenses, other_income, pat, ebitda
   with these five columns:
     current_qtr        - the reported figure for this quarter
     last_year_same_qtr - the figure for the SAME quarter one year earlier
     yoy_change_pct     - % change of current_qtr versus last_year_same_qtr
     qoq_change_pct     - % change versus the IMMEDIATELY PRECEDING quarter
     estimated          - a published broker/analyst estimate for this quarter

2. If a figure is NOT present in the document and cannot be reliably sourced,
   put the exact string "NA" in that cell. NEVER guess, interpolate, or carry a
   number across from a different line item.
   Most filings carry no broker estimate at all — leave "estimated" as "NA"
   unless a research house has actually published a number for this quarter.
   Do not invent a consensus.

3. ai_suggestion rules — this is critical:
     - Give "BEATS ESTIMATES" or "MISSES ESTIMATES" only when an estimate exists
       AND the corresponding actual was extracted.
     - Give "BUY", "SELL" or "HOLD" only when the core figures (revenue and pat)
       were extracted and the picture is conclusive.
     - Otherwise return exactly "NA".
   Do NOT return "HOLD", "NEUTRAL", "BUY" or "SELL" as a way of expressing
   uncertainty. Uncertainty is always "NA".

4. Set extraction_ok to true only if revenue.current_qtr AND pat.current_qtr are
   both real numbers (not "NA"). Otherwise set it to false and ai_suggestion "NA".

CRITICAL: Output ONLY raw JSON. No prose, no markdown fences, no preamble.

{{
  "metrics": {{
    "revenue":      {{"current_qtr": "₹132.35 Cr", "last_year_same_qtr": "₹61.32 Cr", "yoy_change_pct": "+115.8%", "qoq_change_pct": "-57.0%", "estimated": "₹120.00 Cr"}},
    "expenses":     {{"current_qtr": "₹111.05 Cr", "last_year_same_qtr": "₹56.05 Cr", "yoy_change_pct": "+98.2%",  "qoq_change_pct": "-12.4%", "estimated": "NA"}},
    "other_income": {{"current_qtr": "₹2.10 Cr",   "last_year_same_qtr": "₹2.00 Cr",  "yoy_change_pct": "+5.0%",   "qoq_change_pct": "NA",     "estimated": "NA"}},
    "pat":          {{"current_qtr": "₹25.79 Cr",  "last_year_same_qtr": "₹9.55 Cr",  "yoy_change_pct": "+170.1%", "qoq_change_pct": "-24.1%", "estimated": "₹20.00 Cr"}},
    "ebitda":       {{"current_qtr": "₹35.81 Cr",  "last_year_same_qtr": "₹16.28 Cr", "yoy_change_pct": "+120.0%", "qoq_change_pct": "+8.3%",  "estimated": "NA"}}
  }},
  "future_growth_outlook": "What management says about forward growth, or NA",
  "future_projected_numbers": "Specific forward guidance figures, or NA",
  "broker_estimates": "Broker expectations versus the actual print, or NA",
  "extraction_ok": true,
  "ai_suggestion": "BEATS ESTIMATES",
  "summary": "2-3 sentence executive summary comparing YoY performance and estimates",
  "sentiment": "positive"
}}"""

    result_raw = None
    used_flow = "custom_rest_api"
    used_provider = f"custom_api ({custom_url})"

    # --- FLOW 1: gemcall / Custom REST API ---
    is_ollama_endpoint = any(kw in custom_url.lower() for kw in ["11434", "ollama", "localhost:11434"])
    local_llm_active = cfg.local_llm_enabled

    if custom_url and (not is_ollama_endpoint or local_llm_active):
        try:
            logger.info(f"🔬 [AUTO AI FLOW 1]: Posting to Custom REST API: {custom_url} for #{symbol}")
            print(f"🔬 [AUTO AI FLOW 1]: Posting to Custom REST API: {custom_url} for #{symbol}")

            # 1. Download & Base64 Encode PDF Attachment if URL is available
            base64_pdf = None
            if attachment_url:
                try:
                    from app.services.trade_nse_poller import _get_trade_nse_session
                    session = _get_trade_nse_session().session
                    headers_nse = {
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                        "Accept": "application/pdf,*/*",
                        "Referer": "https://www.nseindia.com/companies-listing/corporate-filings/announcements"
                    }
                    pdf_res = session.get(attachment_url, headers=headers_nse, timeout=15)
                    if pdf_res.status_code == 200 and len(pdf_res.content) > 100:
                        base64_pdf = base64.b64encode(pdf_res.content).decode("utf-8")
                        logger.info(f"✅ [FLOW 1 PDF]: Downloaded & Base64 encoded PDF ({len(pdf_res.content)} bytes) for #{symbol}")
                        print(f"✅ [FLOW 1 PDF]: Downloaded & Base64 encoded PDF ({len(pdf_res.content)} bytes) for #{symbol}")
                except Exception as pdf_err:
                    logger.warning(f"⚠️ [FLOW 1 PDF WARNING]: Could not download PDF for base64 encoding: {pdf_err}")
                    print(f"⚠️ [FLOW 1 PDF WARNING]: Could not download PDF for base64 encoding: {pdf_err}")

            # 2. Build Request Payload
            #
            # The generation budget must be sent in the BODY. `requests(timeout=)`
            # only governs how long this client waits; gemcall reads its own
            # budget from req.body.timeout and otherwise falls back to
            # PAGE_TIMEOUT_MS (60s). That mismatch meant gemcall abandoned
            # every PDF analysis at 60s while this side sat waiting for 180s,
            # and the response came back empty.
            gemcall_timeout_ms = 240000  # 4 min — Gemini needs 2-3 min on a PDF
            payload = {"prompt": prompt, "timeout": gemcall_timeout_ms}
            if base64_pdf:
                payload["images"] = [f"data:application/pdf;base64,{base64_pdf}"]

            # 3. Post to gemcall / Custom REST API. The HTTP timeout must exceed
            #    the generation budget or the client hangs up on a live run.
            resp = requests.post(
                custom_url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=(gemcall_timeout_ms / 1000) + 60,
            )
            if resp.status_code == 200:
                try:
                    resp_data = resp.json()
                    if isinstance(resp_data, dict):
                        result_raw = resp_data.get("json") or resp_data.get("response") or resp_data.get("text") or resp_data.get("content") or resp_data.get("result") or json.dumps(resp_data)
                    else:
                        result_raw = str(resp_data)
                except Exception:
                    result_raw = resp.text

                # Validate that we actually got meaningful content back
                # Gemcall can return 200 with empty response if Playwright failed to extract
                if not result_raw or (isinstance(result_raw, str) and len(result_raw.strip()) < 20):
                    logger.warning(f"⚠️ [AUTO AI FLOW 1 EMPTY]: Custom REST API returned 200 but response is empty/too short for #{symbol}. Falling back.")
                    print(f"⚠️ [AUTO AI FLOW 1 EMPTY]: Custom REST API returned 200 but response is empty/too short for #{symbol}. Falling back.")
                    result_raw = None
                else:
                    used_flow = "custom_rest_api"
                    used_provider = f"Custom REST API ({custom_url})"
                    logger.info(f"✅ [AUTO AI FLOW 1 SUCCESS]: Custom REST API responded for #{symbol} (len={len(str(result_raw))})")
                    print(f"✅ [AUTO AI FLOW 1 SUCCESS]: Custom REST API responded for #{symbol} (len={len(str(result_raw))})")
            else:
                logger.warning(f"⚠️ [AUTO AI FLOW 1 FAILED]: Status {resp.status_code} ({resp.text[:100]}). Triggering OpenRouter fallback.")
                print(f"⚠️ [AUTO AI FLOW 1 FAILED]: Status {resp.status_code} ({resp.text[:100]}). Triggering OpenRouter fallback.")
                result_raw = None
        except Exception as e:
            logger.warning(f"⚠️ [AUTO AI FLOW 1 ERROR]: {e}. Falling back to OpenRouter Premium.")
            print(f"⚠️ [AUTO AI FLOW 1 ERROR]: {e}. Falling back to OpenRouter Premium.")
            result_raw = None
    elif is_ollama_endpoint and not local_llm_active:
        logger.info(f"⏸️ [AUTO AI FLOW 1 SKIPPED]: Local Ollama is OFF during market hours (0% CPU). Falling back to Cloud LLM.")
        print(f"⏸️ [AUTO AI FLOW 1 SKIPPED]: Local Ollama is OFF during market hours (0% CPU). Falling back to Cloud LLM.")

    # --- FLOW 2: OpenRouter Premium Fallback ---
    if not result_raw:
        used_flow = "openrouter_premium"
        used_provider = f"OpenRouter Premium ({openrouter_model})"
        try:
            logger.info(f"🔬 [AUTO AI FLOW 2]: Calling Premium OpenRouter ({openrouter_model}) for #{symbol}")
            print(f"🔬 [AUTO AI FLOW 2]: Calling Premium OpenRouter ({openrouter_model}) for #{symbol}")
            if not openrouter_key:
                logger.error("❌ [AUTO AI FLOW 2]: OpenRouter API Key is missing.")
                print("❌ [AUTO AI FLOW 2]: OpenRouter API Key is missing.")
            else:
                or_res = call_openrouter(prompt, openrouter_key, model=openrouter_model, attachment_url=attachment_url)
                if or_res and "analyses" in or_res and or_res["analyses"]:
                    result_raw = json.dumps(or_res["analyses"][0])
                elif or_res:
                    result_raw = json.dumps(or_res)
                logger.info(f"✅ [AUTO AI FLOW 2 SUCCESS]: OpenRouter ({openrouter_model}) responded for #{symbol}")
                print(f"✅ [AUTO AI FLOW 2 SUCCESS]: OpenRouter ({openrouter_model}) responded for #{symbol}")
        except Exception as e:
            logger.error(f"❌ [AUTO AI FLOW 2 ERROR]: OpenRouter call failed: {e}")
            print(f"❌ [AUTO AI FLOW 2 ERROR]: OpenRouter call failed: {e}")

    # --- Robust JSON Parsing ---
    parsed_json = _extract_json_from_llm(result_raw)

    metrics = normalize_metrics(parsed_json.get("metrics"))
    extraction_ok = metrics_are_usable(metrics)

    # Numbers can be read off the wrong row or column and still look plausible,
    # so the grid is checked against itself before any verdict is derived.
    validation = validate_metrics(metrics)
    if extraction_ok and not validation["trustworthy"]:
        logger.warning(
            f"⚠️ [EXTRACTION SUSPECT] #{symbol}: {validation['hard_failures']} "
            f"consistency failure(s) — {'; '.join(validation['issues'][:3])}"
        )
        extraction_ok = False

    future_growth_outlook = _clean_cell(parsed_json.get("future_growth_outlook")) or "NA"
    future_projected_numbers = _clean_cell(parsed_json.get("future_projected_numbers")) or "NA"
    broker_estimates = _clean_cell(parsed_json.get("broker_estimates")) or "NA"

    # A verdict is only meaningful when the numbers behind it were extracted.
    # Anything uncertain must read "NA" rather than being dressed up as HOLD or
    # NEUTRAL, which a reader would otherwise act on as a real call.
    ai_suggestion = _clean_cell(parsed_json.get("ai_suggestion")).upper() or "NA"
    if not extraction_ok or ai_suggestion in ("", "N/A", "NONE", "NULL", "UNKNOWN", "INCONCLUSIVE"):
        ai_suggestion = "NA"

    ai_summary = parsed_json.get("summary") or (
        str(result_raw)[:500] if result_raw else f"AI analysis pending for {symbol}"
    )
    ai_sentiment = str(parsed_json.get("sentiment", "neutral")).lower().strip()

    # Guard against a verdict built on a document the model never received.
    if response_indicates_missing_document(str(result_raw or "") + " " + str(ai_summary or "")):
        logger.error(
            f"🚫 [NO DOCUMENT]: model reported it had no filing to read for #{symbol}. "
            f"Discarding rather than storing figures it cannot have extracted."
        )
        metrics = normalize_metrics(None)
        extraction_ok = False
        ai_suggestion = "NA"
        ai_sentiment = "neutral"
        ai_summary = (
            f"Discarded: the earnings document did not reach the model, so no figures "
            f"were extracted for {symbol}. Check that the gemcall attachment upload "
            f"succeeded, then re-run."
        )
        revenue = expenses = other_income = operating_profit = pat_yoy = _cell_summary(metrics, "revenue")
        future_growth_outlook = future_projected_numbers = "NA"

    # Guard against an analysis of the wrong company reaching the dashboard.
    elif parsed_json and not response_matches_company(ai_summary, symbol, company_name):
        logger.error(
            f"🚫 [WRONG COMPANY]: analysis for #{symbol} ({company_name or 'unknown'}) "
            f"does not reference it — discarding. Summary began: {str(ai_summary)[:120]}"
        )
        metrics = normalize_metrics(None)
        extraction_ok = False
        ai_suggestion = "NA"
        ai_summary = (
            f"Discarded: the analysis returned did not reference {symbol}"
            f"{f' ({company_name})' if company_name else ''}, so it described a different "
            f"filing. Re-run the analysis."
        )
        revenue = expenses = other_income = operating_profit = pat_yoy = _cell_summary(metrics, "revenue")
        future_growth_outlook = future_projected_numbers = "NA"

    if ai_suggestion == "NA":
        # Do not let a sentiment leak a directional view the numbers cannot support.
        ai_sentiment = "neutral"

    # Legacy single-string columns, kept populated so existing views still render.
    revenue = _cell_summary(metrics, "revenue")
    expenses = _cell_summary(metrics, "expenses")
    other_income = _cell_summary(metrics, "other_income")
    operating_profit = _cell_summary(metrics, "ebitda")
    pat_yoy = _cell_summary(metrics, "pat")
    pbt = "NA"
    growth_projection = future_growth_outlook

    # --- Save to TradeAILog ---
    try:
        from app.database import SessionLocal, TradeAILog
        db = SessionLocal()
        try:
            log_entry = TradeAILog(
                config_id=config_id,
                symbol=symbol,
                company_name=company_name or None,
                tracking_ref=tracking_ref,
                ai_requested_at=ai_requested_at,
                ai_completed_at=datetime.utcnow(),
                provider=used_provider,
                prompt_summary=f"2-Step Earnings Analysis for {symbol}",
                ai_sentiment=ai_sentiment,
                ai_impact_score=(
                    0.9 if "BEAT" in ai_suggestion
                    else 0.2 if "MISS" in ai_suggestion
                    else 0.0 if ai_suggestion == "NA"
                    else 0.5
                ),
                ai_summary=ai_summary,
                raw_response=json.dumps(parsed_json) if parsed_json else str(result_raw),
                nse_event_title=title,
                created_at=datetime.utcnow(),
                revenue=str(revenue),
                expenses=str(expenses),
                operating_profit=str(operating_profit),
                pbt=str(pbt),
                other_income=str(other_income),
                pat_yoy=str(pat_yoy),
                growth_projection=str(growth_projection),
                broker_estimates=str(broker_estimates),
                ai_suggestion=ai_suggestion,
                attachment_url=attachment_url,
                flow_used=used_flow,
                metrics_json=json.dumps(metrics),
                validation_json=json.dumps(validation),
                future_growth_outlook=future_growth_outlook,
                future_projected_numbers=future_projected_numbers,
                extraction_ok=extraction_ok,
            )
            db.add(log_entry)
            db.commit()
            logger.info(f"✅ [EARNINGS AI SAVED]: Log entry created for #{symbol} (Flow: {used_flow})")

            # Proactively update any matching recent MarketEvent to sync the results
            try:
                from app.database import MarketEvent
                from datetime import timedelta
                two_hours_ago = datetime.utcnow() - timedelta(hours=2)
                me = db.query(MarketEvent).filter(
                    MarketEvent.symbol == symbol,
                    MarketEvent.event_time >= two_hours_ago
                ).order_by(MarketEvent.event_time.desc()).first()
                if me:
                    me.ai_sentiment = ai_sentiment
                    me.ai_impact_score = 0.8 if ai_sentiment == "positive" else -0.8 if ai_sentiment == "negative" else 0.0
                    me.ai_summary = f"{ai_suggestion.upper()}: {ai_summary[:200]}"
                    me.ai_provider = used_flow
                    me.ai_analyzed_at = datetime.utcnow()
                    db.commit()
                    logger.info(f"🔄 [SYNC MARKET EVENT]: Updated MarketEvent #{me.id} for #{symbol} with 2-step AI results.")
            except Exception as sync_err:
                logger.error(f"Failed to sync MarketEvent with TradeAILog: {sync_err}")
        except Exception as db_err:
            db.rollback()
            logger.error(f"Failed to save TradeAILog: {db_err}")
        finally:
            db.close()
    except Exception:
        pass

    # --- Dispatch Telegram Alert ---
    # Carries the metric grid, and SELL buttons when the stock is already held so
    # an exit can be taken from the same message that delivers the verdict.
    try:
        from app.services.telegram_notifier import send_earnings_verdict_alert
        from app.database import SessionLocal as _SL, TradeConfig

        held_config_id, held_qty, buy_price, instrument_key = None, 0, None, ""
        db_h = _SL()
        try:
            held = db_h.query(TradeConfig).filter(
                TradeConfig.symbol == symbol,
                TradeConfig.status == "bought",
                TradeConfig.is_active == True,
            ).order_by(TradeConfig.bought_at.desc()).first()
            if held:
                held_config_id = held.id
                held_qty = held.quantity or 0
                buy_price = held.buy_price
                instrument_key = held.instrument_key or ""
        finally:
            db_h.close()

        last_price = None
        if held_config_id:
            try:
                from app.services.results_router import get_ltp
                last_price = get_ltp(instrument_key, symbol)
            except Exception:
                pass

        table = render_metrics_table(metrics)
        extra = f"\n\nOutlook: {future_growth_outlook}\nProjected: {future_projected_numbers}"
        if not extraction_ok:
            extra += "\n\nNote: key figures could not be extracted, so no directional call is given."

        send_earnings_verdict_alert(
            symbol=symbol,
            company_name=company_name,
            verdict=ai_suggestion,
            summary=(ai_summary or "") + extra,
            metrics_table=table,
            provider=used_flow.upper(),
            url=attachment_url,
            held_config_id=held_config_id,
            held_qty=held_qty,
            buy_price=buy_price,
            last_price=last_price,
            tracking_ref=tracking_ref,
            ai_requested_at=ai_requested_at,
            ai_completed_at=datetime.utcnow(),
        )
    except Exception as e:
        logger.error(f"Telegram alert error: {e}")

    return parsed_json


def _build_trade_analysis_prompt(symbol: str, title: str, description: str, pdf_text: str = "") -> str:
    """Build a premium analysis prompt for trade-triggered events."""
    extra = ""
    if pdf_text:
        extra = f"\n\n--- EXTRACTED PDF FILING CONTENT ---\n{pdf_text[:4000]}"

    return f"""You are a senior Indian stock market analyst specializing in board meeting outcomes and earnings results.

CRITICAL: This is a LIVE TRADING analysis. Your verdict directly influences BUY/SELL decisions.

STOCK: {symbol}
NSE ANNOUNCEMENT: {title}
DETAILS: {description or 'No additional details available.'}
{extra}

INSTRUCTIONS:
1. Determine if this announcement is POSITIVE, NEGATIVE, or NEUTRAL for the stock price.
2. Extract any financial numbers: Revenue, Net Profit, EBITDA, margins, YoY/QoQ growth.
3. Assess immediate price impact (next 1-5 minutes post-announcement).
4. Provide a clear BUY / HOLD / SELL recommendation with justification.
5. List affected stocks and sector impact.

Respond with JSON:
{{
  "analyses": [
    {{
      "event_index": 0,
      "sentiment": "positive",
      "impact_score": 0.7,
      "affected_stocks": ["{symbol}"],
      "summary": "2-3 sentence analysis of the announcement impact on {symbol} stock price.",
      "recommendation": "BUY",
      "urgency": "immediate"
    }}
  ]
}}"""

