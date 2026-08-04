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
import os
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


# ─── LLM Call Helpers — Tier-Based Routing ────────────────────────────────

def _call_cloud_llm(prompt: str, event_info: str = ""):
    """
    Cloud-first LLM routing for HIGH-PRIORITY items (financial_results + market sentiment).
    1. Tries cloud providers via key pool concurrency (OpenRouter → Groq → Gemini → OpenAI → Anthropic).
    2. If ALL cloud keys are busy/rate-limited → falls back to local Ollama (30s timeout).
    3. If Ollama also fails → falls back to 0-CPU Rule Engine.
    """
    from app.services.gemini import (
        reload_env_vars, call_gemini, call_groq, call_openai, call_anthropic, call_ollama, call_openrouter
    )
    from app.services.key_manager import key_manager
    
    config = get_intel_config()
    ai_config = config.ai
    primary = ai_config.get("primary_provider", "openrouter")
    fallbacks = ai_config.get("fallback_providers", ["groq", "gemini", "openai", "anthropic"])
    
    cloud_providers = [primary] + [fb for fb in fallbacks if fb != primary and fb != "ollama"]
    env = reload_env_vars()
    
    # Sync environment keys into key_manager pools
    key_manager.sync_all(env)
    
    # Priority Override: If openrouter_key exists in env, force openrouter as #1 primary provider
    if env.get("openrouter_key"):
        cloud_providers = ["openrouter"] + [p for p in cloud_providers if p != "openrouter"]
    
    info_suffix = f" for {event_info}" if event_info else ""

    # ── Phase 1: Try Cloud Providers with Idle Key Pool Concurrency ──
    for provider in cloud_providers:
        while True:
            ks = key_manager.acquire_key_for_provider(provider)
            if not ks:
                break
            
            try:
                logger.info(f"🚀 [CLOUD AI]: Calling '{provider}' Key #{ks.index + 1}{info_suffix}...")
                try:
                    from app.services.ai_log_tracker import record_ai_log
                    record_ai_log(f"Calling '{provider.upper()}' Key #{ks.index + 1}{info_suffix}", provider=provider, key_index=ks.index + 1, tier="execution", details=event_info)
                except Exception:
                    pass
                if provider == "openrouter":
                    res = call_openrouter(prompt, ks.key)
                elif provider == "groq":
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
                    try:
                        from app.services.ai_log_tracker import record_ai_log
                        record_ai_log(f"✅ Cloud AI done via {provider.upper()} Key #{ks.index + 1}{info_suffix}", provider=provider, key_index=ks.index + 1, tier="success", level="success", details=event_info)
                    except Exception:
                        pass
                    return res, provider
            except Exception as e:
                is_429 = "429" in str(e) or "Too Many Requests" in str(e) or "Rate limit" in str(e)
                key_manager.release_key(ks, is_rate_limited=is_429, backoff_seconds=60.0)
                err_msg = str(e)
                logger.warning(f"[{provider.upper()}] Key #{ks.index + 1} call failed: {err_msg}")
                try:
                    from app.services.ai_log_tracker import record_ai_log
                    record_ai_log(f"❌ {provider.upper()} Key #{ks.index + 1} Error: {err_msg[:100]}{info_suffix}", provider=provider, key_index=ks.index + 1, tier="error", level="error", details=err_msg)
                except Exception:
                    pass

    # ── Phase 2: Ollama fallback ──
    local_enabled = get_intel_config().local_llm_enabled

    if local_enabled and env.get("ollama_url"):
        try:
            logger.info("🦙 [CLOUD→OLLAMA FALLBACK]: Routing to Ollama (15s timeout)...")
            res = call_ollama(prompt, env["ollama_url"], env.get("ollama_model", "qwen2.5:3b"), timeout=15)
            try:
                from app.services.ai_log_tracker import record_ai_log
                record_ai_log("✅ Ollama fallback completed", provider="ollama", tier="success", level="success")
            except Exception:
                pass
            return res, "ollama"
        except Exception as ollama_err:
            logger.warning(f"Ollama fallback skipped/failed ({ollama_err}). Using Rule Engine.")
    elif not local_enabled:
        logger.info("⏸️ [LOCAL LLM DISABLED]: Local Ollama is turned OFF from UI. Skipping to Rule Engine.")

    # ── Phase 3: Rule Engine fallback ──
    logger.info("⚡ [RULE ENGINE]: Using fast keyword rule engine...")
    try:
        from app.services.ai_log_tracker import record_ai_log
        record_ai_log("⚡ Rule Engine fallback (0 AI calls)", provider="", tier="rule_engine", level="warning")
    except Exception:
        pass
    res = _smart_rule_analysis(prompt)
    return res, "rule_engine"


def _call_local_llm(prompt: str, event_info: str = ""):
    """
    Local-only LLM routing for STANDARD tier items.
    1. Calls local Ollama if enabled in UI.
    2. If disabled, fails, or busy, falls back immediately to Rule Engine.
    """
    if not get_intel_config().local_llm_enabled:
        logger.info("⏸️ [LOCAL LLM DISABLED]: Local Ollama is turned OFF from UI. Using Rule Engine.")
        res = _smart_rule_analysis(prompt)
        return res, "rule_engine"

    from app.services.gemini import reload_env_vars, call_ollama
    
    env = reload_env_vars()
    default_ollama = "http://host.docker.internal:11434" if os.path.exists('/.dockerenv') else "http://localhost:11434"
    ollama_url = env.get("ollama_url") or default_ollama
    ollama_model = env.get("ollama_model", "qwen2.5:3b")
    info_suffix = f" for {event_info}" if event_info else ""
    
    try:
        logger.info(f"🦙 [LOCAL LLM]: Calling Ollama (15s limit){info_suffix}...")
        res = call_ollama(prompt, ollama_url, ollama_model, timeout=15)
        return res, "ollama"
    except Exception as e:
        logger.warning(f"[OLLAMA] Skipped or timed out ({e}){info_suffix}. Falling back to Rule Engine.")
        try:
            from app.services.ai_log_tracker import record_ai_log
            record_ai_log(f"⚡ Ollama skipped ({str(e)[:60]}). Rule Engine used{info_suffix}", provider="rule_engine", tier="warning", level="warning")
        except Exception:
            pass
        res = _smart_rule_analysis(prompt)
        return res, "rule_engine"
    
    # Both attempts failed — return failure signal
    logger.warning(f"🚫 [LOCAL LLM FAILED]: Ollama unavailable after 2 attempts{info_suffix}. Leaving unanalyzed for manual re-analysis.")
    try:
        from app.services.ai_log_tracker import record_ai_log
        record_ai_log(f"🚫 Ollama failed after 2 attempts{info_suffix}. Awaiting manual re-analysis.", provider="ollama", tier="error", level="warning")
    except Exception:
        pass
    return None, "ollama_failed"


def _call_chosen_provider(prompt: str, provider_name: str, event_info: str = ""):
    """
    Call a SPECIFIC provider by name for manual re-analysis.
    The user chooses which provider to use from the dashboard.
    Falls back to cloud chain if the chosen provider fails.
    """
    from app.services.gemini import (
        reload_env_vars, call_gemini, call_groq, call_openai, call_anthropic, call_ollama, call_openrouter
    )
    from app.services.key_manager import key_manager
    
    env = reload_env_vars()
    key_manager.sync_all(env)
    info_suffix = f" for {event_info}" if event_info else ""
    
    provider = provider_name.lower().strip()
    
    # Handle local Ollama separately (no key pool)
    if provider == "ollama":
        try:
            logger.info(f"🦙 [MANUAL]: Calling Ollama{info_suffix}...")
            default_ollama = "http://host.docker.internal:11434" if os.path.exists('/.dockerenv') else "http://localhost:11434"
            target_url = env.get("ollama_url") or default_ollama
            res = call_ollama(prompt, target_url, env.get("ollama_model", "qwen2.5:3b"), timeout=70)
            return res, "ollama"
        except Exception as e:
            raise RuntimeError(f"Ollama call failed: {e}")
    
    # Cloud provider — acquire key from pool
    ks = key_manager.acquire_key_for_provider(provider)
    if not ks:
        raise RuntimeError(f"No available API key for provider '{provider}'. Check your .env configuration.")
    
    try:
        logger.info(f"🎯 [MANUAL]: Calling '{provider}' Key #{ks.index + 1}{info_suffix}...")
        try:
            from app.services.ai_log_tracker import record_ai_log
            record_ai_log(f"Manual re-analysis via '{provider.upper()}' Key #{ks.index + 1}{info_suffix}", provider=provider, key_index=ks.index + 1, tier="execution", details=event_info)
        except Exception:
            pass
        
        if provider == "openrouter":
            res = call_openrouter(prompt, ks.key)
        elif provider == "groq":
            res = call_groq(prompt, ks.key, "llama-3.3-70b-versatile")
        elif provider == "gemini":
            res = call_gemini(prompt, ks.key)
        elif provider == "openai":
            res = call_openai(prompt, ks.key)
        elif provider == "anthropic":
            res = call_anthropic(prompt, ks.key)
        else:
            raise ValueError(f"Unknown provider: '{provider}'")
        
        key_manager.release_key(ks, is_rate_limited=False)
        try:
            from app.services.ai_log_tracker import record_ai_log
            record_ai_log(f"✅ Manual re-analysis done via {provider.upper()}{info_suffix}", provider=provider, key_index=ks.index + 1, tier="success", level="success")
        except Exception:
            pass
        return res, provider
    except Exception as e:
        is_429 = "429" in str(e) or "Too Many Requests" in str(e) or "Rate limit" in str(e)
        key_manager.release_key(ks, is_rate_limited=is_429, backoff_seconds=60.0)
        raise


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
    "shareholders meeting",
    "shareholders meeting...",
    "voting results",
    "scrutinizer report",
}

# Subjects that trigger skip via 'contains' check (case-insensitive)
_SKIP_SUBJECT_CONTAINS = [
    "board meeting — financial results",
    "board meeting - financial results",
    "allotment of securities",
    "certificate under sebi",
    "shareholders meeting",
    "voting results",
    "scrutinizer report",
    "loss of share certificate",
    "duplicate share certificate",
    "trading window",
    "closure of trading window",
    "trading window closure",
]

# Details keywords that trigger skip when subject is "General Updates" / "Updates"
_SKIP_DETAIL_KEYWORDS = ["newspaper publication", "press", "media", "newspaper"]


def _classify_ai_tier(event) -> str:
    """
    Classify a MarketEvent into an AI analysis tier.
    
    Returns one of:
      'skip'              – auto-mark neutral, ZERO AI calls
      'financial_results' – deep analysis with PDF + Screener.in
      'standard'          – normal local Ollama / rule engine analysis
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
    
    # ── RULE 1: Auto-skip subjects requested by user ──
    # - "Board Meeting — Financial Results" / "Board Meeting - Financial Results"
    # - "Allotment of Securities"
    # - "Certificate under SEBI"
    if (
        "board meeting — financial results" in title_lower or
        "board meeting - financial results" in title_lower or
        "allotment of securities" in title_lower or
        "certificate under sebi" in title_lower or
        title_lower in _SKIP_SUBJECTS
    ):
        return "skip"
    
    if "intimation" in title_lower or "notice of board meeting" in title_lower:
        return "skip"

    # ── RULE 2: Financial Results / Finance AI Routing ──
    # User Rules:
    # 1. Subject contains "Outcome of Board Meeting" AND details contain "finan"
    # 2. Subject contains "Updates" AND details contain "finan", "revenue", or "profit"
    # 3. Subject or details contain "Acquisition", "Merger", "Dividend", "Bonus", "Split", "Financial Results", "Quarterly Results", "Audited Results"
    has_outcome_with_finan = "outcome of board meeting" in title_lower and "finan" in desc_lower
    has_updates_with_finan = "update" in title_lower and any(kw in desc_lower for kw in ["finan", "revenue", "profit"])
    has_finance_keywords = any(kw in title_lower or kw in desc_lower for kw in [
        "acquisition", "merger", "dividend", "bonus", "split",
        "financial results", "financial result", "quarterly result", "quarterly results", "audited result", "audited results"
    ])
    
    if (has_outcome_with_finan or has_updates_with_finan or has_finance_keywords) and "intimation" not in title_lower:
        # ── RULE 3: 30-Minute Recency Filter for Financial Results ──
        # Finance AI only analyzes fresh live news (< 30 mins of current time) to avoid old news AI requests
        if event.event_time:
            now_utc = datetime.utcnow()
            event_age = now_utc - event.event_time
            if event_age > timedelta(minutes=30):
                logger.info(f"⏳ [FINANCIAL RESULTS RECENCY SKIP]: Event [{event.symbol or 'GENERAL'}] '{event.title}' is {event_age.total_seconds()/60:.1f} mins old (>30m). Routing to standard tier.")
                return "standard"
        return "financial_results"

    if any(kw in title_lower for kw in _SKIP_SUBJECT_CONTAINS):
        return "skip"
    
    # Rule 2c: Skip "General Updates" / "Updates" with newspaper/press/media in details
    if title_lower in ("general updates", "updates"):
        if any(kw in desc_lower for kw in _SKIP_DETAIL_KEYWORDS):
            return "skip"
    
    # RULE 3: Standard exchange analysis
    return "standard"


def apply_instant_tier_classification(event):
    """
    Check AI tier immediately upon ingestion/creation of a MarketEvent.
    - 'skip' / 'manual_only': Set fields instantly (0 AI calls).
    - 'financial_results': Trigger IMMEDIATE cloud AI analysis inline (PDF + Screener.in + cloud LLM).
    - 'standard': Leave ai_analyzed_at = NULL for background local-LLM queue.
    """
    tier = _classify_ai_tier(event)
    if tier == "skip":
        event.ai_sentiment = "neutral"
        event.ai_impact_score = 0.0
        event.ai_summary = f"Auto-skipped: Subject '{event.title}' is excluded from AI analysis"
        event.ai_affected_stocks = json.dumps([event.symbol] if event.symbol else [])
        event.ai_provider = "auto_skip"
        event.ai_analyzed_at = datetime.utcnow()
        try:
            from app.services.ai_log_tracker import record_ai_log
            record_ai_log(f"Auto-skipped during ingestion (0 AI calls): [{event.symbol or 'GENERAL'}] '{event.title}'", provider="", tier="skip", level="info", details=event.title)
        except Exception:
            pass
    elif tier == "manual_only":
        event.ai_sentiment = "neutral"
        event.ai_impact_score = 0.0
        event.ai_summary = event.title or "Awaiting manual AI analysis"
        event.ai_affected_stocks = json.dumps([event.symbol] if event.symbol else [])
        event.ai_provider = "manual_pending"
        event.ai_analyzed_at = datetime.utcnow()
    elif tier == "financial_results":
        # ── IMMEDIATE cloud AI analysis for financial results ──
        try:
            logger.info(f"📊 [IMMEDIATE AI]: Financial results detected at ingestion — [{event.symbol or 'GENERAL'}] '{event.title}'")
            try:
                from app.services.ai_log_tracker import record_ai_log
                record_ai_log(f"📊 Immediate cloud AI for financial results: [{event.symbol or 'GENERAL'}] '{event.title}'", provider="", tier="financial_results", level="info")
            except Exception:
                pass
            
            # Extract PDF text from attached filing
            pdf_text = ""
            if event.url:
                pdf_text = extract_pdf_text_from_url(event.url)
            
            # Fetch historical financials from Screener.in
            screener_text = ""
            if event.symbol:
                screener_text = fetch_screener_financials(event.symbol)
            
            event_data = {
                "event_type": getattr(event, "event_type", "announcement"),
                "source": getattr(event, "source", "nse"),
                "symbol": event.symbol,
                "title": event.title,
                "description": event.description or "",
                "event_time": event.event_time.isoformat() if event.event_time else "",
            }
            
            prompt = _build_financial_results_prompt(event_data, pdf_text, screener_text)
            result, provider = _call_cloud_llm(prompt, event_info=f"[{event.symbol or 'GENERAL'}] '{event.title}'")
            event.ai_provider = provider
            event.category = "financial_results"
            
            if result and "analyses" in result and result["analyses"]:
                analysis = result["analyses"][0]
                event.ai_sentiment = analysis.get("sentiment", "neutral")
                event.ai_impact_score = analysis.get("impact_score", 0.0)
                event.ai_summary = analysis.get("summary", "")
                event.ai_affected_stocks = json.dumps(analysis.get("affected_stocks", []))
            else:
                event.ai_sentiment = "neutral"
                event.ai_impact_score = 0.0
                event.ai_summary = f"{event.symbol or ''} financial results filed. Review attached PDF for details."
                event.ai_affected_stocks = json.dumps([event.symbol] if event.symbol else [])
            event.ai_analyzed_at = datetime.utcnow()
            
            # Real-time Telegram alert for financial results
            try:
                from app.services.telegram_notifier import send_telegram_alert
                send_telegram_alert(
                    title=event.title or "Financial Results",
                    symbol=event.symbol,
                    sentiment=event.ai_sentiment or "neutral",
                    impact_score=event.ai_impact_score or 0.0,
                    summary=event.ai_summary or "",
                    provider=event.ai_provider or "cloud",
                    url=event.url,
                    alert_type="FINANCIAL RESULTS ALERT"
                )
            except Exception as tg_err:
                logger.debug(f"Telegram dispatch error: {tg_err}")
        except Exception as e:
            logger.error(f"Immediate financial_results AI failed for [{event.symbol}]: {e}")
            # Fallback: mark as analyzed with neutral so it doesn't re-queue
            event.ai_sentiment = "neutral"
            event.ai_impact_score = 0.0
            event.ai_summary = f"{event.symbol or ''} financial results filed. AI analysis failed — click Re-analyze to retry."
            event.ai_affected_stocks = json.dumps([event.symbol] if event.symbol else [])
            event.ai_provider = "failed"
            event.ai_analyzed_at = datetime.utcnow()
    # 'standard' tier: leave ai_analyzed_at = NULL → picked up by background queue


def _auto_mark_skip(db, event, reason: str):
    """Mark an event as analyzed with neutral defaults (0 AI calls)."""
    logger.info(f"⏭️ [AI TIER: SKIP] Event #{event.id} [{event.symbol or 'GENERAL'}]: '{event.title}' — {reason}")
    try:
        from app.services.ai_log_tracker import record_ai_log
        record_ai_log(f"Auto-skipped (0 AI calls): [{event.symbol or 'GENERAL'}] '{event.title}'", provider="", tier="skip", level="info", details=reason)
    except Exception:
        pass
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
    try:
        from app.services.ai_log_tracker import record_ai_log
        record_ai_log(f"Non-exchange event (on demand only): [{event.symbol or 'GENERAL'}] '{event.title}'", provider="", tier="manual_only", level="info")
    except Exception:
        pass
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
      "affected_stocks": ["TICKER"],
      "summary": "Brief 2-3 sentence financial and market impact analysis strictly based on event 1 above. Sector Impact: [Sector Name] -> Prominent stocks: TICKER.",
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
      "affected_stocks": ["TICKER"],
      "summary": "Brief 2-3 sentence financial and market impact analysis strictly based on article 1 above. Sector Impact: [Sector Name] -> Prominent stocks: TICKER.",
      "urgency": "short_term"
    }}
  ]
}}"""


def _build_filing_analysis_prompt(filing: dict) -> str:
    """Build analysis prompt for a company filing. Uses same unified JSON schema as events/news."""
    return f"""You are a senior Indian stock market analyst. Conduct a deep, in-depth financial analysis of this company filing and assess its market impact.

Filing Details:
Type: {filing.get('filing_type', 'unknown')}
Company: {filing.get('symbol', 'Unknown')}
Title: {filing.get('title', '')}
Period: {filing.get('period', 'N/A')}

Extracted Content (if available):
{(filing.get('extracted_text', '') or 'No text available')[:3000]}

For this filing, determine:
1. sentiment: "positive", "negative", or "neutral"
2. impact_score: float from -1.0 (extremely negative) to 1.0 (extremely positive)
3. affected_stocks: List of NSE stock symbols directly impacted
4. summary: In-depth financial analysis (3-4 sentences). Detail growth vectors, margins, and end with "Sector Impact: [Sector] -> Prominent stocks: [TICKER1, TICKER2]".
5. category: One of: "earnings", "corporate_action", "general"

Respond with a JSON object:
{{
  "analyses": [
    {{
      "sentiment": "positive",
      "impact_score": 0.5,
      "affected_stocks": ["{filing.get('symbol', '')}"],
      "summary": "Detailed analysis...",
      "category": "earnings"
    }}
  ]
}}"""


# ─── Analysis Processing ───────────────────────────────────────────────────

def analyze_pending_events(db: Session) -> int:
    """Analyze market events: standard tier → local Ollama only. Financial results are already analyzed at ingestion."""
    config = get_intel_config()
    if not config.ai.get("enabled", True):
        return 0
    
    batch_size = config.ai.get("batch_size", 10)
    
    # Find unanalyzed events (ai_analyzed_at = NULL means not yet processed)
    pending = db.query(MarketEvent).filter(
        MarketEvent.ai_analyzed_at.is_(None)
    ).order_by(MarketEvent.event_time.desc()).limit(batch_size).all()
    
    if not pending:
        return 0
    
    count = 0
    skipped = 0
    manual_only_count = 0
    ollama_failed_count = 0
    alert_threshold = config.ai.get("thresholds", {}).get("alert_threshold", 0.6)
    critical_threshold = config.ai.get("thresholds", {}).get("critical_threshold", 0.85)
    
    for event in pending:
        try:
            tier = _classify_ai_tier(event)
            
            # ── SKIP ──
            if tier == "skip":
                _auto_mark_skip(db, event, f"Subject '{event.title}' is excluded from AI analysis")
                count += 1
                skipped += 1
                continue
            
            # ── MANUAL_ONLY ──
            if tier == "manual_only":
                _auto_mark_manual_only(db, event)
                count += 1
                manual_only_count += 1
                continue
            
            # ── FINANCIAL_RESULTS — should already be analyzed at ingestion ──
            if tier == "financial_results":
                # If we reach here, the ingestion-time analysis must have been missed.
                # Mark it for manual re-analysis rather than using cloud API from the queue.
                logger.warning(f"⚠️ Financial results event #{event.id} [{event.symbol}] was not analyzed at ingestion. Marking for manual re-analysis.")
                event.ai_sentiment = "neutral"
                event.ai_impact_score = 0.0
                event.ai_summary = f"{event.symbol or ''} financial results filed. Click Re-analyze to get AI analysis."
                event.ai_affected_stocks = json.dumps([event.symbol] if event.symbol else [])
                event.ai_provider = "manual_pending"
                event.ai_analyzed_at = datetime.utcnow()
                db.commit()
                count += 1
                continue
            
            # ── STANDARD — Local Ollama if enabled, else Rule Engine ──
            local_on = get_intel_config().local_llm_enabled
            prov_label = "ollama" if local_on else "rule_engine"
            tier_label = "Local Ollama" if local_on else "Rule Engine (Local LLM OFF)"
            logger.info(f"⚡ [STANDARD → {tier_label}] Event #{event.id} [{event.symbol or 'GENERAL'}]: '{event.title}'")
            try:
                from app.services.ai_log_tracker import record_ai_log
                record_ai_log(f"Standard tier → {tier_label}: [{event.symbol or 'GENERAL'}] '{event.title}'", provider=prov_label, tier="standard", level="info")
            except Exception:
                pass
            
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
            result, provider = _call_local_llm(prompt, event_info=f"[{event.symbol or 'GENERAL'}] '{event.title}'")
            
            # If local LLM failed → leave unanalyzed for manual re-analysis
            if result is None or provider == "ollama_failed":
                event.ai_sentiment = "neutral"
                event.ai_impact_score = 0.0
                event.ai_summary = f"Local LLM unavailable. Click Re-analyze and choose a provider."
                event.ai_affected_stocks = json.dumps([event.symbol] if event.symbol else [])
                event.ai_provider = "ollama_failed"
                event.ai_analyzed_at = datetime.utcnow()
                db.commit()
                count += 1
                ollama_failed_count += 1
                continue
            
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
    
    logger.info(f"Analyzed {count} market events (skipped={skipped}, manual_only={manual_only_count}, ollama_failed={ollama_failed_count})")
    return count


def analyze_pending_news(db: Session) -> int:
    """Analyze news stories via local Ollama only. Cloud providers used only for manual re-analysis."""
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
            logger.info(f"🦙 [NEWS → LOCAL LLM]: Analyzing NewsStory #{story.id} [{story.symbols or 'GENERAL'}]: '{story.headline}'")
            
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
            result, provider = _call_local_llm(prompt, event_info=f"[{story.symbols or 'GENERAL'}] '{story.headline}'")
            story.ai_provider = provider
            
            # If local LLM failed → leave unanalyzed for manual re-analysis
            if result is None or provider == "ollama_failed":
                story.ai_sentiment = "neutral"
                story.ai_impact_score = 0.0
                story.ai_summary = f"Local LLM unavailable. Click Re-analyze and choose a provider."
                story.ai_affected_stocks = json.dumps([])
                story.category = "general"
                story.ai_provider = "ollama_failed"
                story.ai_analyzed_at = datetime.utcnow()
                db.query(NewsItem).filter(NewsItem.story_id == story.id).update({
                    "ai_sentiment": "neutral",
                    "ai_impact_score": 0.0,
                    "ai_provider": "ollama_failed",
                    "ai_analyzed_at": datetime.utcnow(),
                })
                db.commit()
                count += 1
                continue
            
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
    """
    Analyze company filings via local Ollama only.
    - quarterly_result: SKIPPED (already covered by MarketEvent financial_results tier).
    - transcript / investor_presentation: Local Ollama, retry once.
    """
    config = get_intel_config()
    if not config.ai.get("enabled", True):
        return 0
    
    # Find unanalyzed filings
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
            # Skip quarterly_results — already analyzed via MarketEvent financial_results tier
            if filing.filing_type == "quarterly_result":
                logger.info(f"⏭️ [FILING SKIP]: quarterly_result #{filing.id} [{filing.symbol}] — already covered by MarketEvent tier")
                filing.ai_sentiment = "neutral"
                filing.ai_summary = f"Covered by MarketEvent financial results analysis for {filing.symbol}."
                filing.ai_key_metrics = json.dumps({})
                filing.ai_provider = "auto_skip"
                filing.ai_analyzed_at = datetime.utcnow()
                count += 1
                continue
            
            # transcript / investor_presentation → Local Ollama
            logger.info(f"🦙 [FILING → LOCAL LLM]: CompanyFiling #{filing.id} [{filing.symbol}]: '{filing.title}'")
            filing_data = {
                "filing_type": filing.filing_type,
                "symbol": filing.symbol,
                "title": filing.title,
                "period": filing.period,
                "extracted_text": filing.extracted_text,
            }
            
            prompt = _build_filing_analysis_prompt(filing_data)
            result, provider = _call_local_llm(prompt, event_info=f"[{filing.symbol}] '{filing.title}'")
            
            # If local LLM failed → leave unanalyzed for manual re-analysis
            if result is None or provider == "ollama_failed":
                filing.ai_sentiment = "neutral"
                filing.ai_summary = f"Local LLM unavailable. Click Re-analyze and choose a provider."
                filing.ai_key_metrics = json.dumps({})
                filing.ai_provider = "ollama_failed"
                filing.ai_analyzed_at = datetime.utcnow()
                count += 1
                continue
            
            # Parse unified analyses[] response
            if result and "analyses" in result and result["analyses"]:
                analysis = result["analyses"][0]
                filing.ai_sentiment = analysis.get("sentiment", "neutral")
                filing.ai_summary = analysis.get("summary", "")
                filing.ai_key_metrics = json.dumps({})
                filing.ai_provider = provider
                filing.ai_analyzed_at = datetime.utcnow()
                count += 1
                
                impact = abs(analysis.get("impact_score", 0.0))
                if impact >= alert_threshold:
                    severity = "critical" if impact >= critical_threshold else "high" if impact >= alert_threshold else "medium"
                    alert = AIAlert(
                        alert_type="filing_analysis",
                        severity=severity,
                        symbol=filing.symbol,
                        title=f"📄 {filing.symbol}: {filing.filing_type.replace('_', ' ').title()} — {analysis.get('sentiment', 'neutral').upper()}",
                        description=analysis.get("summary", filing.title),
                        source_filing_id=filing.id,
                    )
                    db.add(alert)
            else:
                filing.ai_sentiment = "neutral"
                filing.ai_summary = filing.title
                filing.ai_key_metrics = json.dumps({})
                filing.ai_provider = provider
                filing.ai_analyzed_at = datetime.utcnow()
                count += 1
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
        res, provider = _call_cloud_llm(prompt, event_info="Market Sentiment Synthesis")
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


def reanalyze_single_item(db: Session, item_type: str, item_id: int, provider_name: Optional[str] = None) -> Optional[dict]:
    """
    Force re-analysis of a single item (event, story, news, filing) using configured or chosen LLM provider.
    If provider_name is provided, attempts to execute using that specific provider.
    Otherwise uses cloud-first routing chain.
    """
    item_type = item_type.lower()
    
    def execute_call(prompt: str, info: str):
        if provider_name:
            return _call_chosen_provider(prompt, provider_name, event_info=info)
        return _call_cloud_llm(prompt, event_info=info)
    
    if item_type in ("event", "market_event"):
        event = db.query(MarketEvent).filter(MarketEvent.id == item_id).first()
        if not event:
            return None
        
        tier = _classify_ai_tier(event)
        logger.info(f"🤖 [MANUAL RE-ANALYSIS]: MarketEvent #{event.id} [{event.symbol}] via provider='{provider_name or 'auto'}'")
        
        if tier == "financial_results" or (
            "outcome of board meeting" in (event.title or "").lower() and 
            "finan" in (event.description or "").lower()
        ):
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
        
        result, provider = execute_call(prompt, f"Manual event #{event.id} [{event.symbol}]")
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
                "provider": event.ai_provider,
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
        result, provider = execute_call(prompt, f"Manual story #{story.id}")
        if result and "analyses" in result and result["analyses"]:
            analysis = result["analyses"][0]
            story.ai_sentiment = analysis.get("sentiment", "neutral")
            story.ai_impact_score = analysis.get("impact_score", 0.0)
            story.ai_summary = analysis.get("summary", "")
            story.ai_affected_stocks = json.dumps(analysis.get("affected_stocks", []))
            story.ai_provider = provider
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
                "provider": story.ai_provider,
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
        result, provider = execute_call(prompt, f"Manual news #{item.id}")
        if result and "analyses" in result and result["analyses"]:
            analysis = result["analyses"][0]
            item.ai_sentiment = analysis.get("sentiment", "neutral")
            item.ai_impact_score = analysis.get("impact_score", 0.0)
            item.ai_provider = provider
            item.ai_analyzed_at = datetime.utcnow()
            db.commit()
            return {
                "id": item.id,
                "type": "news",
                "sentiment": item.ai_sentiment,
                "impact_score": item.ai_impact_score,
                "provider": item.ai_provider,
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
        result, provider = execute_call(prompt, f"Manual filing #{filing.id} [{filing.symbol}]")
        if result and "analyses" in result and result["analyses"]:
            analysis = result["analyses"][0]
            filing.ai_sentiment = analysis.get("sentiment", "neutral")
            filing.ai_summary = analysis.get("summary", "")
            filing.ai_provider = provider
            filing.ai_analyzed_at = datetime.utcnow()
            db.commit()
            return {
                "id": filing.id,
                "type": "filing",
                "sentiment": filing.ai_sentiment,
                "summary": filing.ai_summary,
                "provider": filing.ai_provider,
                "analyzed_at": filing.ai_analyzed_at.isoformat()
            }

    return None

    return None

