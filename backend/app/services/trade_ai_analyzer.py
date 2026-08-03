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


def analyze_earnings_disclosure_2step(
    symbol: str,
    title: str,
    attachment_url: str = "",
    pdf_text: str = "",
    config_id: Optional[int] = None
) -> Optional[dict]:
    """
    Two-Step AI Analysis Pipeline:
    Step 1: POST to custom REST API (url/api/generate) with dynamic prompt.
    Step 2 (Fallback): If Step 1 fails, request OpenRouter Premium with selected model.
    Saves results to TradeAILog and dispatches Telegram alerts.
    """
    import requests
    from app.services.intel_config import get_intel_config
    from app.services.gemini import clean_json_response, call_openrouter

    cfg = get_intel_config()
    auto_ai_cfg = cfg.auto_trading_ai
    
    custom_url = auto_ai_cfg.get("custom_api_url", "http://localhost:11434/api/generate").strip()
    openrouter_key = auto_ai_cfg.get("premium_openrouter_api_key", "").strip() or os.getenv("OPENROUTER_PREMIUM_API_KEY", "").strip() or os.getenv("OPENROUTER_API_KEY", "").strip()
    openrouter_model = auto_ai_cfg.get("premium_openrouter_model", "anthropic/claude-3.5-sonnet").strip()

    prompt = f"""stock symbol {symbol}   you are indian stock market research analyst. i have companies earnings document in url {attachment_url or 'N/A'}, please analyse this document for earnings and compare results with old earnings (for earnings refer to screener.in) and provide me info in json format of revenue, expenses, Operating Profit, Other Income,Interest,Depreciation,PBT, pat (yoy % , last quarter%, last year same quarter% ) and as per the document analyse how company is projecting future growth and also check how much is expected returns by brokers for this stock in websites and compare with actual and finally provide suggestion like beats estimates, misses estimates(if estimate exists) else give buy , sell, hold

Document Extract / Content snippet:
{pdf_text[:3000] if pdf_text else 'Refer to attachment document URL'}

Respond STRICTLY in JSON format with the following keys:
{{
  "revenue": "Revenue details with YoY / QoQ %",
  "expenses": "Total Expenses details",
  "operating_profit": "Operating Profit & OPM %",
  "pbt": "Profit Before Tax (PBT)",
  "pat_yoy": "PAT details (YoY %, last quarter %, last year same quarter %)",
  "growth_projection": "Future growth projections by management",
  "broker_estimates": "Broker expected returns/estimates vs actual performance",
  "ai_suggestion": "BEATS ESTIMATES",
  "summary": "Concise executive summary of earnings report",
  "sentiment": "positive"
}}"""

    result_raw = None
    used_flow = "custom_rest_api"
    used_provider = f"custom_api ({custom_url})"

    # --- FLOW 1: Custom REST API ---
    try:
        logger.info(f"🔬 [AUTO AI FLOW 1]: Posting to Custom REST API: {custom_url} for #{symbol}")
        resp = requests.post(
            custom_url,
            json={"prompt": prompt},
            headers={"Content-Type": "application/json"},
            timeout=20
        )
        if resp.status_code == 200:
            resp_data = resp.json()
            if isinstance(resp_data, dict):
                result_raw = resp_data.get("response", resp_data.get("text", resp_data.get("content", json.dumps(resp_data))))
            else:
                result_raw = str(resp_data)
            used_flow = "custom_rest_api"
            used_provider = f"Custom REST API ({custom_url})"
            logger.info(f"✅ [AUTO AI FLOW 1 SUCCESS]: Custom REST API responded for #{symbol}")
        else:
            logger.warning(f"⚠️ [AUTO AI FLOW 1 FAILED]: Status {resp.status_code}. Triggering OpenRouter fallback.")
            result_raw = None
    except Exception as e:
        logger.warning(f"⚠️ [AUTO AI FLOW 1 ERROR]: {e}. Falling back to OpenRouter Premium.")
        result_raw = None

    # --- FLOW 2: OpenRouter Premium Fallback ---
    if not result_raw:
        used_flow = "openrouter_premium"
        used_provider = f"OpenRouter Premium ({openrouter_model})"
        try:
            logger.info(f"🔬 [AUTO AI FLOW 2]: Calling Premium OpenRouter ({openrouter_model}) for #{symbol}")
            if not openrouter_key:
                logger.error("❌ [AUTO AI FLOW 2]: OpenRouter API Key is missing.")
            else:
                or_res = call_openrouter(prompt, openrouter_key, model=openrouter_model)
                if or_res and "analyses" in or_res and or_res["analyses"]:
                    result_raw = json.dumps(or_res["analyses"][0])
                elif or_res:
                    result_raw = json.dumps(or_res)
                logger.info(f"✅ [AUTO AI FLOW 2 SUCCESS]: OpenRouter ({openrouter_model}) responded for #{symbol}")
        except Exception as e:
            logger.error(f"❌ [AUTO AI FLOW 2 ERROR]: OpenRouter call failed: {e}")

    # --- Parse Result ---
    parsed_json = {}
    if result_raw:
        try:
            cleaned = clean_json_response(result_raw)
            if isinstance(cleaned, dict):
                parsed_json = cleaned
            elif isinstance(cleaned, str):
                parsed_json = json.loads(cleaned)
        except Exception:
            try:
                parsed_json = json.loads(result_raw)
            except Exception:
                parsed_json = {"summary": str(result_raw)[:500], "ai_suggestion": "HOLD"}

    # Extract structured fields
    revenue = parsed_json.get("revenue", "N/A")
    expenses = parsed_json.get("expenses", "N/A")
    operating_profit = parsed_json.get("operating_profit", "N/A")
    pbt = parsed_json.get("pbt", "N/A")
    pat_yoy = parsed_json.get("pat_yoy", "N/A")
    growth_projection = parsed_json.get("growth_projection", "N/A")
    broker_estimates = parsed_json.get("broker_estimates", "N/A")
    ai_suggestion = str(parsed_json.get("ai_suggestion", "HOLD")).upper().strip()
    ai_summary = parsed_json.get("summary", f"Earnings analysis for {symbol}")
    ai_sentiment = str(parsed_json.get("sentiment", "positive" if "BEAT" in ai_suggestion or "BUY" in ai_suggestion else "negative" if "MISS" in ai_suggestion or "SELL" in ai_suggestion else "neutral"))

    # --- Save to TradeAILog ---
    try:
        from app.database import SessionLocal, TradeAILog
        db = SessionLocal()
        try:
            log_entry = TradeAILog(
                config_id=config_id,
                symbol=symbol,
                provider=used_provider,
                prompt_summary=f"2-Step Earnings Analysis for {symbol}",
                ai_sentiment=ai_sentiment,
                ai_impact_score=0.9 if "BEAT" in ai_suggestion else 0.2 if "MISS" in ai_suggestion else 0.5,
                ai_summary=ai_summary,
                raw_response=json.dumps(parsed_json),
                nse_event_title=title,
                created_at=datetime.utcnow(),
                revenue=str(revenue),
                expenses=str(expenses),
                operating_profit=str(operating_profit),
                pbt=str(pbt),
                pat_yoy=str(pat_yoy),
                growth_projection=str(growth_projection),
                broker_estimates=str(broker_estimates),
                ai_suggestion=ai_suggestion,
                attachment_url=attachment_url,
                flow_used=used_flow
            )
            db.add(log_entry)
            db.commit()
            logger.info(f"✅ [EARNINGS AI SAVED]: Log entry created for #{symbol} (Flow: {used_flow})")
        except Exception as db_err:
            db.rollback()
            logger.error(f"Failed to save TradeAILog: {db_err}")
        finally:
            db.close()
    except Exception:
        pass

    # --- Dispatch Telegram Alert ---
    try:
        from app.services.telegram_notifier import send_telegram_alert
        alert_body = f"""Revenue: {revenue}
Expenses: {expenses}
Operating Profit: {operating_profit}
PAT (YoY/QoQ): {pat_yoy}
Future Growth: {growth_projection}
Broker Estimates: {broker_estimates}
Verdict Suggestion: {ai_suggestion}

Summary: {ai_summary}"""

        send_telegram_alert(
            title=f"{symbol} Earnings Analysis ({ai_suggestion})",
            symbol=symbol,
            sentiment=ai_sentiment,
            impact_score=0.8,
            summary=alert_body,
            provider=f"{used_flow.upper()}",
            url=attachment_url,
            alert_type=f"EARNINGS AI VERDICT: {ai_suggestion}"
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

