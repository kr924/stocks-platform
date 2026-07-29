"""
Premium AI Analyzer for Trading Engine — Cloud-only, configurable provider.
Separate from the standard intelligence AI pipeline.
"""
import json
import logging
import os
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
