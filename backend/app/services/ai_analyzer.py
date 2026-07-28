"""
AI Analysis Engine — Sentiment Analysis, Impact Prediction, and Alert Generation.

Processes market events, news items, and filings through the configured LLM
(Gemini/Groq/OpenAI/Anthropic) to:
1. Classify sentiment (positive/negative/neutral)
2. Score market impact (-1.0 to 1.0)
3. Identify affected stocks
4. Generate human-readable summaries
5. Create AIAlerts for high-impact events
"""
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session
from sqlalchemy import or_

from app.database import MarketEvent, NewsItem, NewsStory, CompanyFiling, AIAlert
from app.services.intel_config import get_intel_config

logger = logging.getLogger("app.ai_analyzer")


# ─── LLM Call Helpers (reuse existing providers) ─────────────────────────

def _call_llm_for_analysis(prompt: str):
    """
    Call LLMs using smart key concurrency pool & state management.
    1. Synchronizes env keys into thread-safe ProviderPools (Groq, Gemini, OpenAI, Anthropic).
    2. Attempts to acquire an IDLE/FREE key for primary provider (Groq) or fallback cloud providers.
    3. If ALL cloud keys across ALL providers are busy or rate-limited:
       -> Routes request to local Ollama with 30s timeout!
    4. If Ollama also fails -> Fallback to 0-CPU Rule Engine.
    """
    from app.services.gemini import (
        reload_env_vars, call_gemini, call_groq, call_openai, call_anthropic, call_ollama
    )
    from app.services.key_manager import key_manager
    
    config = get_intel_config()
    ai_config = config.ai
    primary = ai_config.get("primary_provider", "groq")
    fallbacks = ai_config.get("fallback_providers", ["gemini", "openai", "anthropic"])
    
    cloud_providers = [primary] + [fb for fb in fallbacks if fb != primary and fb != "ollama"]
    env = reload_env_vars()
    
    # Sync environment keys into key_manager pools
    key_manager.sync_all(env)
    
    # ── Phase 1: Try Cloud Providers with Idle Key Pool Concurrency ──
    for provider in cloud_providers:
        # Loop to acquire any free/idle key for this provider
        while True:
            ks = key_manager.acquire_key_for_provider(provider)
            if not ks:
                # No free keys for this provider right now
                break
            
            try:
                logger.info(f"🚀 [AI EXECUTION]: Calling LLM provider '{provider}' using Key #{ks.index + 1}...")
                if provider == "groq":
                    res = call_groq(prompt, ks.key, "llama-3.3-70b-versatile")
                elif provider == "gemini":
                    res = call_gemini(prompt, ks.key)
                elif provider == "openai":
                    res = call_openai(prompt, ks.key)
                elif provider == "anthropic":
                    res = call_anthropic(prompt, ks.key)
                else:
                    res = None
                
                key_manager.release_key(ks, is_rate_limited=False)
                if res:
                    return res, provider
            except Exception as e:
                is_429 = "429" in str(e) or "Too Many Requests" in str(e) or "Rate limit" in str(e)
                key_manager.release_key(ks, is_rate_limited=is_429, backoff_seconds=60.0)
                logger.warning(f"[{provider.upper()}] Key #{ks.index + 1} call failed: {e}")
                # Try next free key for this provider or move on

    # ── Phase 2: If ALL Cloud Keys are Busy/Rate-Limited, Fallback to Local Ollama (30s timeout) ──
    if env.get("ollama_url"):
        try:
            logger.info("🦙 [LLM FALLBACK]: All cloud keys busy/rate-limited. Routing to local Ollama (30s timeout)...")
            res = call_ollama(prompt, env["ollama_url"], env.get("ollama_model", "stocks-analyst"), timeout=30)
            return res, "ollama"
        except Exception as ollama_err:
            logger.warning(f"Local Ollama fallback failed: {ollama_err}")

    # ── Phase 3: Fast 0-CPU Rule Engine fallback ──
    logger.info("⚡ [LLM FALLBACK]: All LLMs unavailable. Using 0-CPU Rule Engine fallback...")
    res = _smart_rule_analysis(prompt)
    return res, "rule_engine"


def _smart_rule_analysis(prompt: str) -> dict:
    """Fast NLP rule engine fallback when cloud LLMs are rate-limited (0% CPU cost)."""
    text_lower = prompt.lower()
    
    pos_keywords = ["profit", "growth", "order received", "contract", "dividend", "bonus", "acquisition", "expansion", "record", "allotment", "beat"]
    neg_keywords = ["loss", "resignation", "penalty", "sebi", "investigation", "decline", "fall", "canceled", "cancelled", "default", "miss"]
    
    pos_matches = [kw for kw in pos_keywords if kw in text_lower]
    neg_matches = [kw for kw in neg_keywords if kw in text_lower]
    
    if "board meeting" in text_lower or "financial results" in text_lower or "results" in text_lower:
        sentiment = "neutral"
        category = "earnings"
        score = 0.1
        summary = "Corporate disclosure: Scheduled board meeting / financial results declaration."
    elif "shareholders meeting" in text_lower or "egm" in text_lower or "agm" in text_lower:
        sentiment = "neutral"
        category = "corporate_action"
        score = 0.0
        summary = "Shareholders meeting filing & corporate governance notification."
    elif "newspaper publication" in text_lower:
        sentiment = "neutral"
        category = "general"
        score = 0.0
        summary = "Statutory newspaper publication notice for regulatory compliance."
    elif len(pos_matches) > len(neg_matches):
        sentiment = "positive"
        category = "general"
        score = min(0.8, 0.4 + (len(pos_matches) * 0.15))
        summary = f"Positive market indicator: Highlights include {', '.join(pos_matches[:3])}."
    elif len(neg_matches) > len(pos_matches):
        sentiment = "negative"
        category = "general"
        score = max(-0.8, -0.4 - (len(neg_matches) * 0.15))
        summary = f"Notice: Critical factors detected include {', '.join(neg_matches[:3])}."
    else:
        sentiment = "neutral"
        category = "general"
        score = 0.0
        summary = "Market filing processed for real-time exchange disclosure."
        
    return {
        "analyses": [{
            "sentiment": sentiment,
            "impact_score": score,
            "summary": summary,
            "affected_stocks": [],
            "category": category
        }]
    }


# ─── Sentiment Analysis Prompts ─────────────────────────────────────────

def extract_pdf_text_from_url(pdf_url: str, max_pages: int = 3) -> str:
    """Download PDF from URL and extract text from first N pages."""
    if not pdf_url or not isinstance(pdf_url, str) or not pdf_url.startswith("http"):
        return ""
    try:
        import requests
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://www.nseindia.com/"
        }
        res = requests.get(pdf_url, headers=headers, timeout=8)
        if res.status_code != 200 or len(res.content) < 500:
            return ""
        
        import io
        import PyPDF2
        reader = PyPDF2.PdfReader(io.BytesIO(res.content))
        extracted = []
        for i in range(min(max_pages, len(reader.pages))):
            t = reader.pages[i].extract_text()
            if t:
                extracted.append(t)
        return "\n".join(extracted)[:4000].strip()
    except Exception as e:
        logger.debug(f"Failed to extract PDF text from {pdf_url}: {e}")
        return ""


# ─── Smart AI Tier Classification ───────────────────────────────────────

# Subjects that are always skipped (case-insensitive exact match)
_SKIP_SUBJECTS = {
    "copy of newspaper publication",
    "press release",
    "press release (revised)",
    "analysts/institutional investor meet/con. call updates",
    "investor presentation",
    "certificate under sebi (depositories and participants) regulations",
}

# Subjects that trigger skip via 'contains' check (case-insensitive)
_SKIP_SUBJECT_CONTAINS = [
    "board meeting —",
    "board meeting -",
]

# Details keywords that trigger skip when subject is "General Updates" / "Updates"
_SKIP_DETAIL_KEYWORDS = ["newspaper publication", "press", "media"]


def _classify_ai_tier(event) -> str:
    """
    Classify a MarketEvent into an AI analysis tier.
    
    Returns one of:
      'skip'              – auto-mark neutral, zero AI calls
      'financial_results' – deep analysis with PDF + Screener.in
      'standard'          – normal Groq AI analysis
      'manual_only'       – no auto AI; user must click Re-analyze
    """
    source = (event.source or "").lower().strip()
    title = (event.title or "").strip()
    title_lower = title.lower()
    desc_lower = (event.description or "").lower()
    
    is_exchange = source in ("nse", "bse")
    
    if not is_exchange:
        # Non-exchange events: no auto AI
        return "manual_only"
    
    # --- Exchange (NSE/BSE) events below ---
    
    # Rule 1: Skip newspaper publications, press releases, analyst meets, investor presentations, SEBI certificates
    if title_lower in _SKIP_SUBJECTS:
        return "skip"
    
    # Rule 1b: Skip subjects containing "Board Meeting —" (intimation notices, not outcome)
    if any(kw in title_lower for kw in _SKIP_SUBJECT_CONTAINS):
        return "skip"
    
    # Rule 2: Skip "General Updates" / "Updates" with newspaper/press/media in details
    if title_lower in ("general updates", "updates"):
        if any(kw in desc_lower for kw in _SKIP_DETAIL_KEYWORDS):
            return "skip"
    
    # Rule 3: Financial Results — "Outcome of Board Meeting" + "finan" in details
    if "outcome of board meeting" in title_lower and "finan" in desc_lower:
        return "financial_results"
    
    # Rule 4: Standard exchange analysis
    return "standard"


def _auto_mark_skip(db, event, reason: str):
    """Mark an event as analyzed with neutral defaults (no AI call)."""
    logger.info(f"⏭️ [AI TIER: SKIP] Event #{event.id} [{event.symbol or 'GENERAL'}]: '{event.title}' — {reason}")
    event.ai_sentiment = "neutral"
    event.ai_impact_score = 0.0
    event.ai_summary = f"Auto-skipped: {reason}"
    event.ai_affected_stocks = json.dumps([event.symbol] if event.symbol else [])
    event.ai_provider = "auto_skip"
    event.ai_analyzed_at = datetime.utcnow()
    db.commit()


def _auto_mark_manual_only(db, event):
    """Mark a non-exchange event as pending manual analysis."""
    logger.info(f"🔒 [AI TIER: MANUAL_ONLY] Event #{event.id} [{event.symbol or 'GENERAL'}]: '{event.title}' — Non-exchange source, AI on demand only")
    event.ai_sentiment = "neutral"
    event.ai_impact_score = 0.0
    event.ai_summary = event.title or "Awaiting manual AI analysis"
    event.ai_affected_stocks = json.dumps([event.symbol] if event.symbol else [])
    event.ai_provider = "manual_pending"
    event.ai_analyzed_at = datetime.utcnow()
    db.commit()


# ─── Screener.in Financial Data Scraper ─────────────────────────────────

def fetch_screener_financials(symbol: str) -> str:
    """
    Fetch key ratios and last few quarters' financial data from Screener.in.
    Returns a compact text block for injection into the AI prompt.
    """
    if not symbol or not isinstance(symbol, str):
        return ""
    
    import requests
    import re
    
    symbol_clean = symbol.strip().upper()
    url = f"https://www.screener.in/company/{symbol_clean}/consolidated/"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }
    
    try:
        res = requests.get(url, headers=headers, timeout=8)
        if res.status_code == 404:
            # Try standalone if consolidated not found
            url = f"https://www.screener.in/company/{symbol_clean}/"
            res = requests.get(url, headers=headers, timeout=8)
        if res.status_code != 200:
            logger.debug(f"Screener.in returned {res.status_code} for {symbol_clean}")
            return ""
        
        html = res.text
        output_parts = []
        
        # 1. Extract meta description (contains summary ratios)
        meta_match = re.search(r'<meta\s+name="description"\s+content="([^"]+)"', html)
        if meta_match:
            output_parts.append(f"Company Summary: {meta_match.group(1)}")
        
        # 2. Extract key ratios from top-ratios list
        ratio_pattern = re.compile(
            r'<span class="name">\s*(.*?)\s*</span>.*?<span class="number">([\d,.]+)</span>',
            re.DOTALL
        )
        ratios = ratio_pattern.findall(html[:15000])  # Ratios are in top section
        if ratios:
            ratio_lines = []
            for name, value in ratios[:12]:
                clean_name = re.sub(r'<[^>]+>', '', name).strip()
                if clean_name:
                    ratio_lines.append(f"  {clean_name}: {value}")
            if ratio_lines:
                output_parts.append("Key Ratios:\n" + "\n".join(ratio_lines))
        
        # 3. Extract Quarterly Results table
        quarters_match = re.search(r'id="quarters".*?<table[^>]*>(.*?)</table>', html, re.DOTALL)
        if quarters_match:
            table_html = quarters_match.group(1)
            
            # Extract column headers (quarter dates)
            header_pattern = re.compile(r'data-date-key="[^"]*">\s*(\w+ \d{4})', re.DOTALL)
            headers_list = header_pattern.findall(table_html)
            # Take last 5 quarters
            headers_list = headers_list[-5:] if len(headers_list) > 5 else headers_list
            
            # Extract row data
            row_pattern = re.compile(r'<tr[^>]*>\s*<td class="text">(.*?)</td>(.*?)</tr>', re.DOTALL)
            rows = row_pattern.findall(table_html)
            
            qtr_lines = []
            if headers_list:
                qtr_lines.append("Quarter:        " + "  |  ".join(headers_list))
            
            for row_label_html, row_cells_html in rows[:8]:  # Sales, Expenses, OP, OPM%, Net Profit, EPS etc.
                label = re.sub(r'<[^>]+>', '', row_label_html).strip().replace('&nbsp;', '').replace('+', '').strip()
                if not label:
                    continue
                cell_values = re.findall(r'<td[^>]*>\s*([\d,.\-%]+)\s*</td>', row_cells_html)
                # Take last 5 values
                cell_values = cell_values[-5:] if len(cell_values) > 5 else cell_values
                if cell_values:
                    qtr_lines.append(f"{label:20s}" + "  |  ".join(v.strip() for v in cell_values))
            
            if qtr_lines:
                output_parts.append("Quarterly Results (Rs. Crores):\n" + "\n".join(qtr_lines))
        
        # 4. Extract Pros and Cons
        pros_match = re.search(r'class="pros".*?<ul>(.*?)</ul>', html, re.DOTALL)
        cons_match = re.search(r'class="cons".*?<ul>(.*?)</ul>', html, re.DOTALL)
        
        if pros_match:
            pros = re.findall(r'<li>(.*?)</li>', pros_match.group(1))
            pros_clean = [re.sub(r'<[^>]+>', '', p).strip() for p in pros]
            if pros_clean:
                output_parts.append("Screener Pros: " + "; ".join(pros_clean[:4]))
        
        if cons_match:
            cons = re.findall(r'<li>(.*?)</li>', cons_match.group(1))
            cons_clean = [re.sub(r'<[^>]+>', '', c).strip() for c in cons]
            if cons_clean:
                output_parts.append("Screener Cons: " + "; ".join(cons_clean[:4]))
        
        result = "\n\n".join(output_parts)
        logger.info(f"📊 [SCREENER DATA] Fetched {len(result)} chars of financial data for {symbol_clean}")
        return result[:3000]
        
    except Exception as e:
        logger.debug(f"Failed to fetch Screener.in data for {symbol_clean}: {e}")
        return ""


# ─── Prompt Builders ────────────────────────────────────────────────────

def _build_event_analysis_prompt(events: List[dict]) -> str:
    """Build a batch analysis prompt for market events."""
    events_text = ""
    for i, evt in enumerate(events):
        events_text += f"""
--- Event {i+1} ---
Type: {evt.get('event_type', 'unknown')}
Source: {evt.get('source', 'unknown')}
Symbol: {evt.get('symbol', 'N/A')}
Title: {evt.get('title', '')}
Description: {evt.get('description', '')}
Time: {evt.get('event_time', '')}
"""

    return f"""You are a senior Indian stock market analyst. Conduct a deep, in-depth financial analysis of the following market events to assess their impact on the Indian stock market (NSE/BSE).

CRITICAL FINANCIAL EXTRACTION RULES:
- If financial numbers (Revenue, Net Profit, EBITDA, Profit Margins, YoY/QoQ growth %, EPS) are present in the event description or PDF document text, EXPLICITLY highlight the exact numbers (e.g. "Net Profit up +34% YoY to ₹45.2 Cr, Revenue ₹210 Cr (+18%)").
- Avoid generic phrases like "this is a routine disclosure requirement". Always detail specific figures, growth vectors, operational highlights, or state "Filing submitted — review attached PDF for complete table."

For EACH event, determine:
1. sentiment: "positive", "negative", or "neutral"
2. impact_score: float from -1.0 (extremely negative) to 1.0 (extremely positive)
3. affected_stocks: List of NSE stock symbols directly impacted OR prominent sector leaders if a sector is broadly impacted. E.g., ["HDFCBANK", "ICICIBANK"].
4. summary: In-depth financial analysis (3-4 sentences). Detail exact financial figures if present, logic of market impact, and end with: "Sector Impact: [Sector Name] -> Prominent stocks: [TICKER1, TICKER2]".
5. urgency: "immediate", "short_term", or "long_term"

Events to analyze:
{events_text}

Respond with a JSON object:
{{
  "analyses": [
    {{
      "event_index": 0,
      "sentiment": "positive",
      "impact_score": 0.7,
      "affected_stocks": ["RELIANCE", "ONGC", "BPCL"],
      "summary": "Reliance Industries Q1 Net Profit surged +24% YoY to ₹18,900 Cr, driven by strong gross refining margins and retail segment growth. Revenue stood at ₹2.3 Lakh Cr (+12%). Sector Impact: Oil & Gas / Energy -> Prominent stocks: RELIANCE, ONGC, BPCL.",
      "urgency": "immediate"
    }}
  ]
}}"""


def _build_financial_results_prompt(event_data: dict, pdf_text: str, screener_text: str) -> str:
    """Build a specialized deep financial analysis prompt for board meeting financial results."""
    symbol = event_data.get("symbol", "UNKNOWN")
    title = event_data.get("title", "")
    description = event_data.get("description", "")
    event_time = event_data.get("event_time", "")
    
    context_sections = []
    
    if pdf_text:
        context_sections.append(f"""--- EXTRACTED PDF FILING CONTENT ---
{pdf_text}""")
    
    if screener_text:
        context_sections.append(f"""--- SCREENER.IN HISTORICAL FINANCIAL DATA ---
{screener_text}""")
    
    extra_context = "\n\n".join(context_sections)
    
    return f"""You are a senior Indian stock market analyst specializing in quarterly earnings analysis. Perform a DEEP financial analysis of this company's board meeting outcome and financial results.

COMPANY: {symbol}
EVENT: {title}
DESCRIPTION: {description}
TIME: {event_time}

{extra_context}

CRITICAL INSTRUCTIONS:
1. Extract and state EXACT financial figures from the PDF filing: Revenue, Net Profit, EBITDA, Operating Profit, OPM%, EPS.
2. Using the Screener.in historical data above, calculate and state YoY (Year-over-Year) and QoQ (Quarter-over-Quarter) growth rates for Revenue and Net Profit.
3. Compare the current quarter's OPM% with the previous quarter and same quarter last year.
4. Highlight any exceptional items, one-time gains/losses, or significant deviations from historical trends.
5. If the PDF data is sparse, state what's available and note "Full financials available in the exchange filing."
6. Give a clear POSITIVE / NEGATIVE / NEUTRAL verdict with justification based on the numbers.

Respond with a JSON object:
{{
  "analyses": [
    {{
      "event_index": 0,
      "sentiment": "positive",
      "impact_score": 0.7,
      "affected_stocks": ["{symbol}"],
      "summary": "Example: {symbol} Q1FY27 Revenue ₹XXX Cr (+XX% YoY, +XX% QoQ). Net Profit ₹XX Cr (+XX% YoY). OPM at XX% vs XX% last quarter. Strong operational performance driven by [key factors]. Sector Impact: [Sector] -> Prominent stocks: {symbol}.",
      "urgency": "immediate",
      "category": "financial_results"
    }}
  ]
}}"""




def _build_news_analysis_prompt(articles: List[dict]) -> str:
    """Build a batch analysis prompt for news articles."""
    articles_text = ""
    for i, art in enumerate(articles):
        articles_text += f"""
--- Article {i+1} ---
Source: {art.get('source', 'unknown')}
Headline: {art.get('headline', '')}
Summary: {art.get('summary', '')}
"""

    return f"""You are a senior Indian stock market analyst. Conduct a deep, in-depth financial analysis of the following news articles/clusters to assess their impact on the Indian stock market (NSE/BSE).

For EACH article, analyze the detailed content (do not just read the headline) and determine:
1. sentiment: "positive", "negative", or "neutral"
2. impact_score: float from -1.0 (extremely negative) to 1.0 (extremely positive)
3. affected_stocks: List of NSE stock symbols directly impacted OR prominent sector leaders if a sector is broadly impacted. E.g., if IT is impacted, list ["TCS", "INFY", "HCLTECH", "WIPRO"]; if Private Banking, list ["HDFCBANK", "ICICIBANK", "AXISBANK", "KOTAKBANK"]; if Auto, list ["TATAMOTORS", "MARUTI", "M&M"]; if Metal, list ["TATASTEEL", "JINDALSTEL", "HINDALCO"].
4. summary: In-depth financial and operational analysis (3-4 sentences). Do not just state a high-level summary; explain the business implications, regulatory context, or financial metrics mentioned in the details. End with a structured line: "Sector Impact: [Sector Name] -> Prominent stocks: [TICKER1, TICKER2]".
5. category: One of: "market_update", "earnings", "ipo", "policy", "global_market", "sector_news", "stock_specific", "general"

Articles to analyze:
{articles_text}

Respond with a JSON object:
{{
  "analyses": [
    {{
      "article_index": 0,
      "sentiment": "positive",
      "impact_score": 0.5,
      "category": "market_update",
      "affected_stocks": ["TCS", "INFY", "HCLTECH"],
      "summary": "The US Fed hints at interest rate cuts, which will likely prompt global enterprises to resume deferred IT spending budgets, benefiting top-tier Indian IT service exporters. Sector Impact: IT Services -> Prominent stocks: TCS, INFY, HCLTECH.",
      "urgency": "short_term"
    }}
  ]
}}"""


def _build_filing_analysis_prompt(filing: dict) -> str:
    """Build analysis prompt for a company filing."""
    return f"""You are a senior Indian stock market analyst. Conduct a deep, in-depth financial analysis of this company filing and assess its market impact.

Filing Details:
Type: {filing.get('filing_type', 'unknown')}
Company: {filing.get('symbol', 'Unknown')}
Title: {filing.get('title', '')}
Period: {filing.get('period', 'N/A')}

Extracted Content (if available):
{(filing.get('extracted_text', '') or 'No text available')[:3000]}

Provide your analysis as JSON:
{{
  "sentiment": "positive/negative/neutral",
  "impact_score": 0.0,
  "summary": "In-depth financial analysis detailing growth vectors, balance sheet/operational metrics, margin shifts, and the long-term sector outlook. Sector Impact: [Sector Name] -> Prominent stocks: [TICKER1, TICKER2]",
  "key_metrics": {{
    "revenue_growth": "X%",
    "profit_change": "X%",
    "margin_percentage": "X%",
    "notable_items": ["item1", "item2"]
  }},
  "affected_stocks": ["{filing.get('symbol', '')}"],
  "recommendation": "Detailed recommendation for long-term and short-term investors"
}}"""


# ─── Analysis Processing ───────────────────────────────────────────────────

def analyze_pending_events(db: Session) -> int:
    """Analyze market events with smart tier-based routing to minimize API calls."""
    config = get_intel_config()
    if not config.ai.get("enabled", True):
        return 0
    
    batch_size = config.ai.get("batch_size", 10)
    
    # Find unanalyzed events
    pending = db.query(MarketEvent).filter(
        MarketEvent.ai_analyzed_at.is_(None)
    ).order_by(MarketEvent.event_time.desc()).limit(batch_size).all()
    
    if not pending:
        return 0
    
    count = 0
    skipped = 0
    manual_only_count = 0
    financial_results_count = 0
    alert_threshold = config.ai.get("thresholds", {}).get("alert_threshold", 0.6)
    critical_threshold = config.ai.get("thresholds", {}).get("critical_threshold", 0.85)
    
    for event in pending:
        try:
            # ── Step 1: Classify event into AI tier ──
            tier = _classify_ai_tier(event)
            
            # ── TIER: SKIP — no AI call, auto-mark neutral ──
            if tier == "skip":
                _auto_mark_skip(db, event, f"Subject '{event.title}' is excluded from AI analysis")
                count += 1
                skipped += 1
                continue
            
            # ── TIER: MANUAL_ONLY — no auto AI, user clicks Re-analyze ──
            if tier == "manual_only":
                _auto_mark_manual_only(db, event)
                count += 1
                manual_only_count += 1
                continue
            
            # ── TIER: FINANCIAL_RESULTS — deep analysis with PDF + Screener.in ──
            if tier == "financial_results":
                logger.info(f"📊 [AI TIER: FINANCIAL_RESULTS] Event #{event.id} [{event.symbol or 'GENERAL'}]: '{event.title}' — Deep financial analysis with PDF + Screener.in")
                financial_results_count += 1
                
                # Extract PDF text from attached filing
                pdf_text = ""
                if event.url:
                    pdf_text = extract_pdf_text_from_url(event.url)
                
                # Fetch historical financials from Screener.in
                screener_text = ""
                if event.symbol:
                    screener_text = fetch_screener_financials(event.symbol)
                
                event_data = {
                    "event_type": event.event_type,
                    "source": event.source,
                    "symbol": event.symbol,
                    "title": event.title,
                    "description": event.description or "",
                    "event_time": event.event_time.isoformat() if event.event_time else "",
                }
                
                prompt = _build_financial_results_prompt(event_data, pdf_text, screener_text)
                result, provider = _call_llm_for_analysis(prompt)
                event.ai_provider = provider
                event.category = "financial_results"  # Tag as financial results
                
                if not result or "analyses" not in result or not result["analyses"]:
                    event.ai_sentiment = "neutral"
                    event.ai_impact_score = 0.0
                    event.ai_summary = f"{event.symbol or ''} financial results filed. Review attached PDF for detailed figures."
                    event.ai_affected_stocks = json.dumps([event.symbol] if event.symbol else [])
                    event.ai_analyzed_at = datetime.utcnow()
                    db.commit()
                    count += 1
                    time.sleep(1.2)
                    continue
                
                analysis = result["analyses"][0]
                event.ai_sentiment = analysis.get("sentiment", "neutral")
                event.ai_impact_score = analysis.get("impact_score", 0.0)
                event.ai_summary = analysis.get("summary", "")
                event.ai_affected_stocks = json.dumps(analysis.get("affected_stocks", []))
                event.ai_analyzed_at = datetime.utcnow()
                
                db.commit()
                count += 1
                
                # Broadcast with financial_results category
                try:
                    from app.services.sse_manager import sse_manager
                    from app.services.deduplication import to_iso_utc
                    sse_manager.broadcast("new_event", {
                        "id": f"event_{event.id}",
                        "type": "event",
                        "event_type": event.event_type,
                        "source": event.source,
                        "symbol": event.symbol,
                        "title": event.title,
                        "description": event.ai_summary or event.description,
                        "url": event.url,
                        "time": to_iso_utc(event.event_time),
                        "ai_sentiment": event.ai_sentiment,
                        "ai_impact_score": event.ai_impact_score,
                        "ai_summary": event.ai_summary,
                        "ai_provider": event.ai_provider,
                        "category": "financial_results",
                    })
                except Exception as sse_err:
                    logger.warning(f"Failed to broadcast analyzed event {event.id}: {sse_err}")
                
                # Generate alert if high impact
                impact = abs(analysis.get("impact_score", 0.0))
                if impact >= alert_threshold:
                    _create_alert_from_event(db, event, analysis, alert_threshold, critical_threshold)
                
                time.sleep(1.2)
                continue
            
            # ── TIER: STANDARD — normal Groq AI analysis ──
            logger.info(f"🤖 [AI TIER: STANDARD] Event #{event.id} [{event.symbol or 'GENERAL'}]: '{event.title}'")
            
            desc_content = event.description or ""
            if event.url and ".pdf" in event.url.lower():
                pdf_text = extract_pdf_text_from_url(event.url)
                if pdf_text:
                    desc_content += f"\n\n--- Extracted Filing PDF Document Content ---\n{pdf_text}"

            event_data = [{
                "event_type": event.event_type,
                "source": event.source,
                "symbol": event.symbol,
                "title": event.title,
                "description": desc_content,
                "event_time": event.event_time.isoformat() if event.event_time else "",
            }]
            
            prompt = _build_event_analysis_prompt(event_data)
            result, provider = _call_llm_for_analysis(prompt)
            event.ai_provider = provider
            
            if not result or "analyses" not in result or not result["analyses"]:
                event.ai_sentiment = "neutral"
                event.ai_impact_score = 0.0
                event.ai_summary = event.title
                event.ai_affected_stocks = json.dumps([event.symbol] if event.symbol else [])
                event.ai_analyzed_at = datetime.utcnow()
                db.commit()
                count += 1
                continue
            
            analysis = result["analyses"][0]
            event.ai_sentiment = analysis.get("sentiment", "neutral")
            event.ai_impact_score = analysis.get("impact_score", 0.0)
            event.ai_summary = analysis.get("summary", "")
            event.ai_affected_stocks = json.dumps(analysis.get("affected_stocks", []))
            event.ai_analyzed_at = datetime.utcnow()
            
            db.commit()
            count += 1
            
            # Broadcast updated item with AI analysis to SSE clients
            try:
                from app.services.sse_manager import sse_manager
                from app.services.deduplication import to_iso_utc
                sse_manager.broadcast("new_event", {
                    "id": f"event_{event.id}",
                    "type": "event",
                    "event_type": event.event_type,
                    "source": event.source,
                    "symbol": event.symbol,
                    "title": event.title,
                    "description": event.ai_summary or event.description,
                    "url": event.url,
                    "time": to_iso_utc(event.event_time),
                    "ai_sentiment": event.ai_sentiment,
                    "ai_impact_score": event.ai_impact_score,
                    "ai_summary": event.ai_summary,
                    "ai_provider": event.ai_provider,
                    "category": event.category,
                })
            except Exception as sse_err:
                logger.warning(f"Failed to broadcast analyzed event {event.id}: {sse_err}")

            # Generate alert if high impact
            impact = abs(analysis.get("impact_score", 0.0))
            if impact >= alert_threshold:
                _create_alert_from_event(db, event, analysis, alert_threshold, critical_threshold)
            
            # Pace requests smoothly (1.2s delay) to respect API rate limits
            time.sleep(1.2)
        except Exception as e:
            logger.error(f"Error analyzing event {event.id}: {e}")
            db.rollback()
            try:
                event.ai_sentiment = "neutral"
                event.ai_impact_score = 0.0
                event.ai_summary = event.title
                event.ai_provider = "stub"
                event.ai_analyzed_at = datetime.utcnow()
                db.commit()
            except Exception:
                db.rollback()
            continue
    
    logger.info(f"Analyzed {count} market events (skipped={skipped}, manual_only={manual_only_count}, financial_results={financial_results_count})")
    return count


def analyze_pending_news(db: Session) -> int:
    """Analyze news stories one by one for deep, focused analysis and assign category."""
    config = get_intel_config()
    if not config.ai.get("enabled", True):
        return 0
    
    batch_size = config.ai.get("batch_size", 10)
    
    # Find unanalyzed stories
    pending_stories = db.query(NewsStory).filter(
        NewsStory.ai_analyzed_at.is_(None)
    ).order_by(NewsStory.last_published.desc()).limit(batch_size).all()
    
    if not pending_stories:
        return 0
    
    count = 0
    alert_threshold = config.ai.get("thresholds", {}).get("alert_threshold", 0.6)
    critical_threshold = config.ai.get("thresholds", {}).get("critical_threshold", 0.85)
    
    for story in pending_stories:
        try:
            logger.info(f"🤖 [AI CALL REASON]: Analyzing unanalyzed NewsStory #{story.id} [{story.symbols or 'GENERAL'}]: '{story.headline}'")
            
            # Fetch the actual articles belonging to this clustered story
            news_items = db.query(NewsItem).filter(NewsItem.story_id == story.id).all()
            combined_text = "\n".join([
                f"Outlet ({item.source}): {item.headline}. Details: {item.summary or ''}"
                for item in news_items
            ])
            
            story_data = [{
                "source": "story_cluster",
                "headline": story.headline,
                "summary": combined_text or f"Clustered story with {story.article_count} sources.",
            }]
            
            prompt = _build_news_analysis_prompt(story_data)
            result, provider = _call_llm_for_analysis(prompt)
            story.ai_provider = provider
            
            if not result or "analyses" not in result or not result["analyses"]:
                # Mark as analyzed with neutral defaults so it doesn't retry forever
                story.ai_sentiment = "neutral"
                story.ai_impact_score = 0.0
                story.ai_summary = story.headline
                story.ai_affected_stocks = json.dumps([])
                story.category = "general"
                story.ai_analyzed_at = datetime.utcnow()
                
                db.query(NewsItem).filter(NewsItem.story_id == story.id).update({
                    "ai_sentiment": "neutral",
                    "ai_impact_score": 0.0,
                    "ai_provider": provider,
                    "ai_analyzed_at": datetime.utcnow(),
                })
                db.commit()
                count += 1
                continue
            
            analysis = result["analyses"][0]
            story.ai_sentiment = analysis.get("sentiment", "neutral")
            story.ai_impact_score = analysis.get("impact_score", 0.0)
            story.ai_summary = analysis.get("summary", "")
            story.ai_affected_stocks = json.dumps(analysis.get("affected_stocks", []))
            story.category = analysis.get("category", "general")
            story.ai_analyzed_at = datetime.utcnow()
            
            # Also update individual articles in this story
            db.query(NewsItem).filter(NewsItem.story_id == story.id).update({
                "ai_sentiment": analysis.get("sentiment", "neutral"),
                "ai_impact_score": analysis.get("impact_score", 0.0),
                "ai_provider": provider,
                "ai_analyzed_at": datetime.utcnow(),
            })
            
            db.commit()
            count += 1
            
            # Generate alert if high impact
            impact = abs(analysis.get("impact_score", 0.0))
            if impact >= alert_threshold:
                _create_alert_from_story(db, story, analysis, alert_threshold, critical_threshold)
            
            # Pace requests smoothly (1.2s delay) to respect API rate limits
            time.sleep(1.2)
        except Exception as e:
            logger.error(f"Error analyzing news story {story.id}: {e}")
            db.rollback()
            try:
                story.ai_sentiment = "neutral"
                story.ai_impact_score = 0.0
                story.ai_summary = story.headline
                story.ai_provider = "stub"
                story.ai_analyzed_at = datetime.utcnow()
                db.commit()
            except Exception:
                db.rollback()
            continue
            
    logger.info(f"Analyzed {count} news stories")
    return count


def analyze_pending_filings(db: Session) -> int:
    """Analyze company filings that haven't been processed by AI yet."""
    config = get_intel_config()
    if not config.ai.get("enabled", True):
        return 0
    
    # Process filings one at a time (they can be large)
    pending = db.query(CompanyFiling).filter(
        CompanyFiling.ai_analyzed_at.is_(None)
    ).order_by(CompanyFiling.filed_at.desc()).limit(5).all()
    
    if not pending:
        return 0
    
    count = 0
    alert_threshold = config.ai.get("thresholds", {}).get("alert_threshold", 0.6)
    critical_threshold = config.ai.get("thresholds", {}).get("critical_threshold", 0.85)
    
    for filing in pending:
        try:
            logger.info(f"🤖 [AI CALL REASON]: Analyzing unanalyzed CompanyFiling #{filing.id} [{filing.symbol}]: '{filing.title}'")
            filing_data = {
                "filing_type": filing.filing_type,
                "symbol": filing.symbol,
                "title": filing.title,
                "period": filing.period,
                "extracted_text": filing.extracted_text,
            }
            
            prompt = _build_filing_analysis_prompt(filing_data)
            result, provider = _call_llm_for_analysis(prompt)
            
            if result:
                filing.ai_sentiment = result.get("sentiment", "neutral")
                filing.ai_summary = result.get("summary", "")
                filing.ai_key_metrics = json.dumps(result.get("key_metrics", {}))
                filing.ai_provider = provider
                filing.ai_analyzed_at = datetime.utcnow()
                count += 1
                
                impact = abs(result.get("impact_score", 0.0))
                if impact >= alert_threshold:
                    severity = "critical" if impact >= critical_threshold else "high" if impact >= alert_threshold else "medium"
                    alert = AIAlert(
                        alert_type="filing_analysis",
                        severity=severity,
                        symbol=filing.symbol,
                        title=f"📄 {filing.symbol}: {filing.filing_type.replace('_', ' ').title()} — {result.get('sentiment', 'neutral').upper()}",
                        description=result.get("summary", filing.title),
                        source_filing_id=filing.id,
                    )
                    db.add(alert)
        except Exception as e:
            logger.error(f"Error analyzing filing {filing.id}: {e}")
    
    if count > 0:
        db.commit()
        logger.info(f"Analyzed {count} company filings")
    return count


# ─── Alert Generation ──────────────────────────────────────────────────────

def _create_alert_from_event(
    db: Session, event: MarketEvent, analysis: dict,
    alert_threshold: float, critical_threshold: float
):
    """Create an AIAlert from a high-impact market event."""
    impact = abs(analysis.get("impact_score", 0.0))
    sentiment = analysis.get("sentiment", "neutral")
    
    if impact >= critical_threshold:
        severity = "critical"
    elif impact >= alert_threshold:
        severity = "high"
    else:
        severity = "medium"
    
    # Map event types to alert types
    alert_type_map = {
        "bulk_deal": "bulk_deal",
        "block_deal": "bulk_deal",
        "insider_trade": "insider_buy" if sentiment == "positive" else "insider_sell",
        "announcement": "high_impact",
        "result": "result_beat" if sentiment == "positive" else "result_miss",
        "social": "breaking_news",
    }
    alert_type = alert_type_map.get(event.event_type, "high_impact")
    
    # Emoji for alert type
    emoji_map = {
        "bulk_deal": "📊",
        "insider_buy": "🟢",
        "insider_sell": "🔴",
        "high_impact": "⚡",
        "result_beat": "📈",
        "result_miss": "📉",
        "breaking_news": "🔥",
    }
    emoji = emoji_map.get(alert_type, "⚡")
    
    alert = AIAlert(
        alert_type=alert_type,
        severity=severity,
        symbol=event.symbol,
        title=f"{emoji} {event.symbol or 'Market'}: {event.title[:200]}",
        description=analysis.get("summary", event.title),
        source_event_id=event.id,
    )
    db.add(alert)


def _create_alert_from_story(
    db: Session, story: NewsStory, analysis: dict,
    alert_threshold: float, critical_threshold: float
):
    """Create an AIAlert from a high-impact news story."""
    impact = abs(analysis.get("impact_score", 0.0))
    
    if impact >= critical_threshold:
        severity = "critical"
    elif impact >= alert_threshold:
        severity = "high"
    else:
        severity = "medium"
    
    symbols = story.symbols or ""
    primary_symbol = symbols.split(",")[0] if symbols else None
    
    alert = AIAlert(
        alert_type="breaking_news",
        severity=severity,
        symbol=primary_symbol,
        title=f"📰 {story.headline[:200]}",
        description=f"{analysis.get('summary', '')} [Covered by {story.article_count} sources]",
        source_story_id=story.id,
    )
    db.add(alert)


# ─── Main Analysis Loop Entry Point ──────────────────────────────────────

def run_analysis_cycle(db: Session) -> Dict[str, int]:
    """
    Run one cycle of AI analysis on all pending items.
    Called by the background scheduler every ai_analysis_queue interval.
    """
    config = get_intel_config()
    if not config.ai.get("enabled", True):
        return {}
    
    results = {}
    
    try:
        results["events"] = analyze_pending_events(db)
    except Exception as e:
        logger.error(f"Event analysis failed: {e}")
        results["events"] = 0
    
    try:
        results["news"] = analyze_pending_news(db)
    except Exception as e:
        logger.error(f"News analysis failed: {e}")
        results["news"] = 0
    
    try:
        results["filings"] = analyze_pending_filings(db)
    except Exception as e:
        logger.error(f"Filing analysis failed: {e}")
        results["filings"] = 0
    
    total = sum(results.values())
    if total > 0:
        logger.info(f"AI analysis cycle complete: {total} items analyzed — {results}")
    
    return results


# ─── AI Stock Suggestions ────────────────────────────────────────────────

def get_ai_stock_suggestions(db: Session, limit: int = 10) -> List[dict]:
    """
    Get AI suggestions for stocks that may be impacted positively or negatively
    based on recent events, news, and filings.
    """
    now = datetime.utcnow()
    last_24h = now - timedelta(hours=24)
    
    suggestions = []
    
    # Get high-impact events from last 24h
    high_impact_events = db.query(MarketEvent).filter(
        MarketEvent.ai_analyzed_at.isnot(None),
        MarketEvent.event_time >= last_24h,
        or_(
            MarketEvent.ai_impact_score >= 0.5,
            MarketEvent.ai_impact_score <= -0.5,
        )
    ).order_by(MarketEvent.ai_impact_score.desc()).limit(limit).all()
    
    for event in high_impact_events:
        suggestions.append({
            "symbol": event.symbol,
            "direction": "positive" if (event.ai_impact_score or 0) > 0 else "negative",
            "impact_score": event.ai_impact_score,
            "reason": event.ai_summary or event.title,
            "source_type": "event",
            "event_type": event.event_type,
            "source": event.source,
            "time": event.event_time.isoformat() if event.event_time else None,
        })
    
    # Get high-impact news stories from last 24h
    high_impact_stories = db.query(NewsStory).filter(
        NewsStory.ai_analyzed_at.isnot(None),
        NewsStory.last_published >= last_24h,
        or_(
            NewsStory.ai_impact_score >= 0.5,
            NewsStory.ai_impact_score <= -0.5,
        )
    ).order_by(NewsStory.ai_impact_score.desc()).limit(limit).all()
    
    for story in high_impact_stories:
        primary_symbol = (story.symbols or "").split(",")[0] if story.symbols else None
        suggestions.append({
            "symbol": primary_symbol,
            "direction": "positive" if (story.ai_impact_score or 0) > 0 else "negative",
            "impact_score": story.ai_impact_score,
            "reason": story.ai_summary or story.headline,
            "source_type": "news",
            "article_count": story.article_count,
            "time": story.last_published.isoformat() if story.last_published else None,
        })
    
    # Sort by absolute impact score
    suggestions.sort(key=lambda x: abs(x.get("impact_score", 0)), reverse=True)
    return suggestions[:limit]


def get_market_sentiment(db: Session, force_refresh: bool = False) -> dict:
    """
    Get or generate global market sentiment.
    If cached sentiment is less than 5 minutes old, return it.
    Otherwise, run LLM synthesis to update overall market sentiment and cache it.
    This fetches real-time news from the wider internet (Google News RSS)
    and computes real-time buyer/seller volumes from watchlist quotes.
    """
    from app.database import AICache, Watchlist, SessionStore
    from app.services.feed_factory import get_feed
    from app.services.news_aggregator import fetch_google_news_rss
    from app.config import PROVIDER
    
    # Check cache first
    now = datetime.utcnow()
    cached = db.query(AICache).filter(AICache.instrument_key == "MARKET_SENTIMENT").first()
    if cached and not force_refresh:
        # Check age
        age_seconds = (now - cached.fetched_at).total_seconds()
        if age_seconds < 300: # 5 minutes
            try:
                return json.loads(cached.comment)
            except Exception:
                pass
                
    # 1. Fetch wider internet news using Google News RSS query
    internet_news = []
    try:
        internet_news = fetch_google_news_rss("Indian stock market macro global financial news", 15)
    except Exception as news_err:
        logger.error(f"Error fetching wider internet news: {news_err}")
        
    news_text = ""
    if internet_news:
        for idx, art in enumerate(internet_news[:8]):
            news_text += f"- Headline: {art.get('headline')} (Source: {art.get('source_name') or 'Google News'})\n"
    else:
        # Local DB news story fallback
        recent_stories = db.query(NewsStory).filter(NewsStory.ai_sentiment.isnot(None))\
                           .order_by(NewsStory.last_published.desc()).limit(8).all()
        for idx, s in enumerate(recent_stories):
            news_text += f"- Headline: {s.headline} (Local Sentiment: {s.ai_sentiment})\n"

    # 2. Compute advances, declines, and buyer/seller volumes from Nifty 50 and sectoral stocks
    advances = 0
    declines = 0
    total_buying_vol = 0
    total_selling_vol = 0
    
    # Resolve feed
    feed = get_feed()
    stored_session = db.query(SessionStore).filter(SessionStore.provider == PROVIDER).first()
    if stored_session:
        feed.set_access_token(stored_session.access_token)
    else:
        feed.set_access_token(None)
        
    from app.config import DEFAULT_NIFTY_50
    keys = [item["key"] for item in DEFAULT_NIFTY_50]
    try:
        quotes = feed.get_quotes(keys)
        for key, quote in quotes.items():
            last_price = quote.get("last_price", 0.0)
            ohlc = quote.get("ohlc", {})
            close_price = ohlc.get("close", 0.0)
            volume = quote.get("volume", 0)
            
            if last_price > close_price:
                advances += 1
                total_buying_vol += volume
            elif last_price < close_price:
                declines += 1
                total_selling_vol += volume
    except Exception as quote_err:
        logger.error(f"Error fetching quotes for Nifty 50 depth calculation: {quote_err}")
            
    # Calculate percentages
    total_vol = total_buying_vol + total_selling_vol
    if total_vol > 0:
        buyers_pct = round((total_buying_vol / total_vol) * 100, 1)
        sellers_pct = round((total_selling_vol / total_vol) * 100, 1)
    else:
        total_count = advances + declines
        if total_count > 0:
            buyers_pct = round((advances / total_count) * 100, 1)
            sellers_pct = round((declines / total_count) * 100, 1)
        else:
            buyers_pct = 50.0
            sellers_pct = 50.0

    prompt = f"""
    You are a senior Indian stock market analyst. Synthesize the overall market sentiment based on wider internet news and real-time buying/selling statistics.
    
    Market Depth / Order Book Activity (Nifty 50 & Sectoral Stocks):
    - Advances (stocks up): {advances}
    - Declines (stocks down): {declines}
    - Estimated Buying Pressure (Advancing volume): {buyers_pct}% of total trades
    - Estimated Selling Pressure (Declining volume): {sellers_pct}% of total trades
    
    Recent business news from the wider internet:
    {news_text}
    
    Task:
    Provide a JSON object containing the overall market mood/sentiment matching the following schema:
    1. "sentiment": One of "Bullish", "Bearish", or "Neutral".
    2. "score": An overall sentiment score from -1.0 (extremely bearish) to 1.0 (extremely bullish).
    3. "summary": A concise 2-sentence market commentary summarizing the mood, catalysts, and buy/sell pressure.
    4. "drivers": A list of 3-5 top drivers. Each driver must be a JSON object containing:
       - "title": A short summary of the driving event/news.
       - "impact": "Positive", "Negative", or "Neutral".
       - "source": The source of the driver (e.g. "NSE", "Moneycontrol", "Mint").
       - "time": A relative or absolute time string indicating when it occurred (e.g. "1h ago" or "9 Jul, 10:19 AM").
    5. "sectors": A JSON object containing:
       - "positive": List of 1-3 sectors that are positively impacted by these developments.
       - "negative": List of 1-3 sectors that are negatively impacted by these developments.
       
    Respond ONLY with a valid JSON object matching the requested schema. Do not enclose it in any extra text or tags.
    """
    
    sentiment_data = None
    try:
        logger.info(f"🤖 [AI CALL REASON]: Synthesizing Market Sentiment (5-min cache expired / user force refresh)")
        res, provider = _call_llm_for_analysis(prompt)
        sentiment_data = res
    except Exception as e:
        logger.error(f"Error calling LLM for market sentiment: {e}")
        
    if not sentiment_data:
        # Fallback synthesis
        avg_score = round(0.5 if advances > declines else -0.5 if declines > advances else 0.0, 2)
        sentiment = "Bullish" if avg_score > 0.2 else "Bearish" if avg_score < -0.2 else "Neutral"
        
        drivers = []
        if internet_news:
            for art in internet_news[:3]:
                drivers.append({
                    "title": art.get("headline"),
                    "impact": "Neutral",
                    "source": art.get("source_name") or "Google News",
                    "time": "Recent"
                })
                
        sentiment_data = {
            "sentiment": sentiment,
            "score": avg_score,
            "summary": f"Calculated market mood is {sentiment} based on {advances} advancing and {declines} declining stocks. LLM provider keys were unavailable for full synthesis.",
            "drivers": drivers,
            "sectors": {
                "positive": ["IT"] if avg_score > 0 else [],
                "negative": ["Banking"] if avg_score < 0 else []
            }
        }
        
    # Append computed volume and advance-decline statistics
    sentiment_data["last_updated"] = now.isoformat() + "Z"
    sentiment_data["advances"] = advances
    sentiment_data["declines"] = declines
    sentiment_data["buyers_pct"] = buyers_pct
    sentiment_data["sellers_pct"] = sellers_pct
    
    # Cache it
    try:
        if cached:
            cached.comment = json.dumps(sentiment_data)
            cached.fetched_at = now
        else:
            new_cache = AICache(
                instrument_key="MARKET_SENTIMENT",
                comment=json.dumps(sentiment_data),
                fetched_at=now
            )
            db.add(new_cache)
        db.commit()
    except Exception as cache_err:
        db.rollback()
        logger.error(f"Error caching market sentiment: {cache_err}")
        
    return sentiment_data


def reanalyze_single_item(db: Session, item_type: str, item_id: int) -> Optional[dict]:
    """
    Force re-analysis of a single item (event, story, news, filing) using configured LLM providers.
    Sequence: Primary (Groq) -> Fallbacks (Gemini, OpenAI, Anthropic, Ollama local model).
    """
    item_type = item_type.lower()
    
    if item_type in ("event", "market_event"):
        event = db.query(MarketEvent).filter(MarketEvent.id == item_id).first()
        if not event:
            return None
        
        # Use tier classification to determine prompt type
        tier = _classify_ai_tier(event)
        logger.info(f"🤖 [AI CALL REASON]: Manual re-analysis of MarketEvent #{event.id} (tier={tier})")
        
        if tier == "financial_results" or (
            "outcome of board meeting" in (event.title or "").lower() and 
            "finan" in (event.description or "").lower()
        ):
            # Deep financial analysis with PDF + Screener
            pdf_text = extract_pdf_text_from_url(event.url) if event.url else ""
            screener_text = fetch_screener_financials(event.symbol) if event.symbol else ""
            
            event_data = {
                "event_type": event.event_type,
                "source": event.source,
                "symbol": event.symbol,
                "title": event.title,
                "description": event.description or "",
                "event_time": event.event_time.isoformat() if event.event_time else "",
            }
            prompt = _build_financial_results_prompt(event_data, pdf_text, screener_text)
        else:
            # Standard analysis with PDF extraction
            desc_content = event.description or ""
            if event.url and ".pdf" in (event.url or "").lower():
                pdf_text = extract_pdf_text_from_url(event.url)
                if pdf_text:
                    desc_content += f"\n\n--- Extracted Filing PDF Document Content ---\n{pdf_text}"
            
            event_data = [{
                "event_type": event.event_type,
                "source": event.source,
                "symbol": event.symbol,
                "title": event.title,
                "description": desc_content,
                "event_time": event.event_time.isoformat() if event.event_time else "",
            }]
            prompt = _build_event_analysis_prompt(event_data)
        
        result, provider = _call_llm_for_analysis(prompt)
        if result and "analyses" in result and result["analyses"]:
            analysis = result["analyses"][0]
            event.ai_sentiment = analysis.get("sentiment", "neutral")
            event.ai_impact_score = analysis.get("impact_score", 0.0)
            event.ai_summary = analysis.get("summary", "")
            event.ai_affected_stocks = json.dumps(analysis.get("affected_stocks", []))
            event.ai_provider = provider
            event.ai_analyzed_at = datetime.utcnow()
            if analysis.get("category"):
                event.category = analysis.get("category")
            db.commit()
            return {
                "id": event.id,
                "type": "event",
                "sentiment": event.ai_sentiment,
                "impact_score": event.ai_impact_score,
                "summary": event.ai_summary,
                "affected_stocks": json.loads(event.ai_affected_stocks or "[]"),
                "analyzed_at": event.ai_analyzed_at.isoformat()
            }

    elif item_type in ("story", "news_story"):
        story = db.query(NewsStory).filter(NewsStory.id == item_id).first()
        if not story:
            return None
        articles_data = [{
            "source": story.best_source_tier,
            "headline": story.headline,
            "summary": story.ai_summary or story.headline,
        }]
        prompt = _build_news_analysis_prompt(articles_data)
        result = _call_llm_for_analysis(prompt)
        if result and "analyses" in result and result["analyses"]:
            analysis = result["analyses"][0]
            story.ai_sentiment = analysis.get("sentiment", "neutral")
            story.ai_impact_score = analysis.get("impact_score", 0.0)
            story.ai_summary = analysis.get("summary", "")
            story.ai_affected_stocks = json.dumps(analysis.get("affected_stocks", []))
            story.ai_analyzed_at = datetime.utcnow()
            if analysis.get("category"):
                story.category = analysis.get("category")
            db.commit()
            return {
                "id": story.id,
                "type": "story",
                "sentiment": story.ai_sentiment,
                "impact_score": story.ai_impact_score,
                "summary": story.ai_summary,
                "affected_stocks": json.loads(story.ai_affected_stocks or "[]"),
                "category": story.category,
                "analyzed_at": story.ai_analyzed_at.isoformat()
            }

    elif item_type in ("news", "news_item"):
        item = db.query(NewsItem).filter(NewsItem.id == item_id).first()
        if not item:
            return None
        articles_data = [{
            "source": item.source,
            "headline": item.headline,
            "summary": item.summary or item.headline,
        }]
        prompt = _build_news_analysis_prompt(articles_data)
        result = _call_llm_for_analysis(prompt)
        if result and "analyses" in result and result["analyses"]:
            analysis = result["analyses"][0]
            item.ai_sentiment = analysis.get("sentiment", "neutral")
            item.ai_impact_score = analysis.get("impact_score", 0.0)
            item.ai_analyzed_at = datetime.utcnow()
            db.commit()
            return {
                "id": item.id,
                "type": "news",
                "sentiment": item.ai_sentiment,
                "impact_score": item.ai_impact_score,
                "analyzed_at": item.ai_analyzed_at.isoformat()
            }

    elif item_type in ("filing", "company_filing"):
        filing = db.query(CompanyFiling).filter(CompanyFiling.id == item_id).first()
        if not filing:
            return None
        filing_data = {
            "filing_type": filing.filing_type,
            "symbol": filing.symbol,
            "title": filing.title,
            "period": filing.period,
            "extracted_text": filing.extracted_text,
        }
        prompt = _build_filing_analysis_prompt(filing_data)
        result = _call_llm_for_analysis(prompt)
        if result:
            filing.ai_sentiment = result.get("sentiment", "neutral")
            filing.ai_summary = result.get("summary", "")
            filing.ai_key_metrics = json.dumps(result.get("key_metrics", {}))
            filing.ai_analyzed_at = datetime.utcnow()
            db.commit()
            return {
                "id": filing.id,
                "type": "filing",
                "sentiment": filing.ai_sentiment,
                "summary": filing.ai_summary,
                "key_metrics": json.loads(filing.ai_key_metrics or "{}"),
                "analyzed_at": filing.ai_analyzed_at.isoformat()
            }

    return None

