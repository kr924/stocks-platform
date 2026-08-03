import logging
import os
import re
import json
import time
import requests
import threading
import google.generativeai as genai
from dotenv import load_dotenv
from typing import List, Dict, Any

logger = logging.getLogger("app.gemini")

# Helper function to reload dotenv dynamically
def reload_env_vars():
    load_dotenv(override=True)
    groq_raw = (
        os.getenv("GROQ_API_KEYS") or
        os.getenv("GROQ_API_KEY") or
        os.getenv("GROQ_KEY") or ""
    )
    openrouter_raw = (
        os.getenv("OPENROUTER_API_KEYS") or
        os.getenv("OPENROUTER_API_KEY") or
        os.getenv("OPENROUTER_KEY") or
        os.getenv("OPEN_ROUTER_KEY") or
        os.getenv("OPENROUTER_TOKEN") or ""
    )
    return {
        "openrouter_key": openrouter_raw,
        "gemini_key": os.getenv("GEMINI_API_KEY", ""),
        "openai_key": os.getenv("OPENAI_API_KEY", ""),
        "anthropic_key": os.getenv("ANTHROPIC_API_KEY", ""),
        "groq_key": groq_raw,
        "groq_model": "llama-3.3-70b-versatile",
        "ollama_url": os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434"),
        "ollama_model": os.getenv("OLLAMA_MODEL", "qwen2.5:3b")
    }


_ollama_lock = threading.Lock()


def call_ollama(prompt: str, base_url: str = "http://host.docker.internal:11434", model_name: str = "qwen2.5:3b", timeout: int = 15) -> dict:
    """
    Calls local Ollama API with concurrency lock and 15s timeout to prevent CPU spikes.
    """
    # Prevent concurrent Ollama threads from hammering CPU
    acquired = _ollama_lock.acquire(blocking=False)
    if not acquired:
        raise RuntimeError("Ollama CPU is busy with another request")

    try:
        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": "Respond only with a raw, valid JSON object matching the requested schema. Do not output markdown code blocks."},
                {"role": "user", "content": prompt}
            ],
            "stream": False,
            "format": "json",
            "options": {
                "temperature": 0.1,
                "num_predict": 256,
                "num_ctx": 512
            }
        }
        
        is_docker = os.path.exists('/.dockerenv')
        raw_list = [base_url, os.getenv("OLLAMA_BASE_URL", ""), "http://172.17.0.1:11434", "http://host.docker.internal:11434"]
        candidate_urls = []
        for u in raw_list:
            if not u:
                continue
            if is_docker and ("localhost" in u or "127.0.0.1" in u):
                continue
            if u not in candidate_urls:
                candidate_urls.append(u)
        if not candidate_urls:
            candidate_urls = ["http://172.17.0.1:11434" if is_docker else "http://localhost:11434"]
            
        last_err = None
        for b_url in candidate_urls:
            url = b_url.rstrip("/") + "/api/chat"
            try:
                res = requests.post(url, json=payload, timeout=timeout)
                res.raise_for_status()
                raw_text = res.json()["message"]["content"]
                cleaned = clean_json_response(raw_text)
                return json.loads(cleaned)
            except Exception as e:
                last_err = e
                break
        raise RuntimeError(f"Ollama endpoint failed: {last_err}")
    finally:
        _ollama_lock.release()


def clean_json_response(text: str) -> str:
    """Strip markdown blocks, reasoning tags, and repair truncated JSON responses before parsing."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL)
    if match:
        text = match.group(1).strip()
        
    # Auto-repair truncated JSON strings/brackets if cut off
    text = text.strip()
    if text:
        quotes = text.count('"') - text.count('\\"')
        if quotes % 2 != 0:
            text += '"'
        open_brackets = text.count('[') - text.count(']')
        open_braces = text.count('{') - text.count('}')
        if open_brackets > 0:
            text += ']' * open_brackets
        if open_braces > 0:
            text += '}' * open_braces
            
    return text


def call_openai(prompt: str, api_key: str) -> dict:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": "Respond only with a raw, valid JSON object matching the requested schema. Do not output markdown code blocks."},
            {"role": "user", "content": prompt}
        ],
        "response_format": {"type": "json_object"}
    }
    res = requests.post("https://api.openai.com/v1/chat/completions", json=payload, headers=headers, timeout=15)
    res.raise_for_status()
    raw_text = res.json()["choices"][0]["message"]["content"]
    cleaned = clean_json_response(raw_text)
    return json.loads(cleaned)


def call_openrouter(prompt: str, api_key: str) -> dict:
    """Call OpenRouter API with dynamic live free model discovery and dual payload attempt."""
    key = api_key.strip().rstrip(">").strip('"').strip("'").strip()
    if not key or key.startswith("YOUR_"):
        raise ValueError("Invalid or placeholder OpenRouter API key")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
        "HTTP-Referer": "https://github.com/kr924/stocks-platform",
        "X-Title": "Stocks Platform AI",
    }
    
    # 1. Fetch live free models dynamically from OpenRouter's API
    free_models = []
    try:
        models_res = requests.get("https://openrouter.ai/api/v1/models", timeout=5)
        if models_res.ok:
            m_data = models_res.json().get("data", [])
            for m in m_data:
                m_id = m.get("id", "")
                pricing = m.get("pricing", {})
                p_prompt = str(pricing.get("prompt", "1"))
                p_compl = str(pricing.get("completion", "1"))
                if m_id.endswith(":free") or (p_prompt == "0" and p_compl == "0"):
                    free_models.append(m_id)
    except Exception as e:
        logger.warning(f"Failed to fetch live OpenRouter models list: {e}")
    
    # Fallback model list if dynamic fetch returned 0
    if not free_models:
        free_models = [
            "openrouter/auto",
            "google/gemini-2.0-flash-exp:free",
            "meta-llama/llama-3.1-8b-instruct:free",
            "qwen/qwen-2.5-coder-32b-instruct:free",
            "mistralai/mistral-small-24b-instruct-2501:free",
            "cognitivecomputations/dolphin3.0-r1-mistral-24b:free",
        ]
    
    # Prepend openrouter/auto if not present
    if "openrouter/auto" not in free_models:
        free_models.insert(0, "openrouter/auto")
        
    last_err = None
    for model_name in free_models:
        # Attempt with and without response_format parameter
        for use_json_format in [True, False]:
            payload = {
                "model": model_name,
                "messages": [
                    {"role": "system", "content": "Respond only with a raw, valid JSON object matching the requested schema. Do not output markdown code blocks or conversational text."},
                    {"role": "user", "content": prompt}
                ]
            }
            if use_json_format:
                payload["response_format"] = {"type": "json_object"}
                
            try:
                res = requests.post("https://openrouter.ai/api/v1/chat/completions", json=payload, headers=headers, timeout=20)
                if res.ok:
                    raw_text = res.json()["choices"][0]["message"]["content"]
                    cleaned = clean_json_response(raw_text)
                    logger.info(f"✨ [OPENROUTER] Analysis successfully generated using model '{model_name}'!")
                    return json.loads(cleaned)
                else:
                    last_err = f"Model {model_name} HTTP {res.status_code}: {res.text[:120]}"
            except Exception as e:
                last_err = str(e)
                continue
                
    raise RuntimeError(f"All OpenRouter free models failed: {last_err}")


def call_groq(prompt: str, api_key: str, model_name: str = "llama-3.3-70b-versatile") -> dict:
    """Call Groq API using strictly llama-3.3-70b-versatile for a single API key."""
    key = api_key.strip()
    if not key:
        raise ValueError("Groq API key is empty")
        
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}"
    }
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"}
    }
    res = requests.post("https://api.groq.com/openai/v1/chat/completions", json=payload, headers=headers, timeout=12)
    res.raise_for_status()
    raw_text = res.json()["choices"][0]["message"]["content"]
    cleaned = clean_json_response(raw_text)
    return json.loads(cleaned)


def call_anthropic(prompt: str, api_key: str) -> dict:
    headers = {
        "content-type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01"
    }
    payload = {
        "model": "claude-3-5-sonnet-20241022",
        "max_tokens": 1000,
        "messages": [{"role": "user", "content": prompt}],
        "system": "Respond only with a raw, valid JSON object matching the requested schema. Do not output markdown code blocks."
    }
    res = requests.post("https://api.anthropic.com/v1/messages", json=payload, headers=headers, timeout=15)
    res.raise_for_status()
    raw_text = res.json()["content"][0]["text"]
    cleaned = clean_json_response(raw_text)
    return json.loads(cleaned)


def call_gemini(prompt: str, api_key: str) -> dict:
    key = api_key.strip()
    if not key or key.startswith("AQ.") or key.startswith("YOUR_"):
        raise ValueError("Invalid or placeholder Gemini API key")
    
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={key}"
    headers = {"Content-Type": "application/json"}
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"response_mime_type": "application/json"}
    }
    
    res = requests.post(url, json=payload, headers=headers, timeout=15)
    if not res.ok:
        fallback_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-exp:generateContent?key={key}"
        res = requests.post(fallback_url, json=payload, headers=headers, timeout=15)
    
    res.raise_for_status()
    raw_text = res.json()["candidates"][0]["content"]["parts"][0]["text"]
    cleaned = clean_json_response(raw_text.strip())
    return json.loads(cleaned)


def analyze_stock_with_gemini(symbol: str, name: str, quote: dict, news_articles: List[Dict[str, Any]], candles: List[Any] = None) -> dict:
    """Analyze stock details using the active configured LLM provider (OpenAI, Groq, Anthropic, or Gemini) and return metrics."""
    logger.info(f"🤖 [AI CALL REASON]: Single stock deep analysis requested for symbol '{symbol}' ({name})")
    # Force reload of environment variables from .env to pick up any runtime edits instantly
    cfg = reload_env_vars()
    
    # Check if any LLM API keys are active. If not, return fallback stubs.
    if not (cfg["gemini_key"] or cfg["openai_key"] or cfg["anthropic_key"] or cfg["groq_key"]):
        last_price = quote.get("last_price", 100.0)
        return {
            "sector": "General Market",
            "resistance_levels": f"R1: ₹{round(last_price * 1.03, 2)}, R2: ₹{round(last_price * 1.06, 2)}",
            "support_levels": f"S1: ₹{round(last_price * 0.97, 2)}, S2: ₹{round(last_price * 0.94, 2)}",
            "recommendation": "HOLD",
            "comment": f"Standard indicator scan for {symbol}. All AI API keys are missing in your configuration.",
            "analyst_recommendations": [
                {"analyst_firm": "Default Scanner", "recommendation": "HOLD", "date": datetime.utcnow().strftime("%Y-%m-%d")}
            ]
        }

    # Format price info
    ohlc = quote.get("ohlc", {})
    last_price = quote.get("last_price", 0.0)
    prev_close = ohlc.get("close", 0.0)
    
    pct_change = 0.0
    if prev_close > 0:
        pct_change = ((last_price - prev_close) / prev_close) * 100
        
    price_info = (
        f"LTP: {last_price} INR, Daily Change: {pct_change:.2f}%, "
        f"Open: {ohlc.get('open', 0.0)}, High: {ohlc.get('high', 0.0)}, "
        f"Low: {ohlc.get('low', 0.0)}, Volume: {quote.get('volume', 0)}"
    )

    # Format chart trend info from candles
    chart_info = "No historical chart data available."
    if candles and len(candles) > 0:
        try:
            # candle format is usually: [time, open, high, low, close, volume]
            closes = [float(c[4]) for c in candles if len(c) > 4 and c[4] is not None]
            if closes:
                min_close = min(closes)
                max_close = max(closes)
                recent_trend = "UP" if closes[-1] >= closes[0] else "DOWN"
                chart_info = (
                    f"30-day Price Range: Min ₹{min_close:.2f}, Max ₹{max_close:.2f}. "
                    f"Recent trend over 30 days: {recent_trend}. "
                    f"Latest Close: ₹{closes[-1]:.2f}, Start Close: ₹{closes[0]:.2f}."
                )
        except Exception as chart_err:
            logger.warning(f"Error parsing candle charts in AI analysis: {chart_err}")

    # Format news info (up to 10 articles)
    news_text = ""
    if news_articles:
        for idx, art in enumerate(news_articles[:10]):
            news_text += f"{idx+1}. {art.get('headline')} (Source: {art.get('source') or 'Unknown'}, Date: {art.get('published_at', '')[:10]})\n"
    else:
        news_text = "No recent news headlines available for this instrument."

    prompt = f"""
    You are an expert financial analyst in the Indian stock market. Provide a structured stock analysis for {symbol} ({name}) based on the following real-time data:
    
    Price Data (LTP & daily high/low/volume):
    {price_info}
    
    30-Day Chart Trend Summary:
    {chart_info}
    
    Recent News & Sector Context:
    {news_text}
    
    Task:
    Provide a JSON object containing the following keys:
    1. "sector": The primary industry/sector for the stock (e.g. "Information Technology", "Banking", "Renewable Energy").
    2. "resistance_levels": Two technical resistance levels based on chart high and quote metrics (e.g. "R1: \u20b91560.00, R2: \u20b91590.00").
    3. "support_levels": Two technical support levels based on chart low and quote metrics (e.g. "S1: \u20b91500.00, S2: \u20b91480.00").
    4. "recommendation": One of the values: "BUY", "SELL", or "HOLD". Decide this based on news sentiment, analyst consensus, and 30-day chart trend.
    5. "comment": A concise 2-sentence financial comment summarizing the stock/sector outlook, key technical levels, and catalysts. Do not mention that you are an AI.
    6. "analyst_recommendations": A list of recent recommendations from research firms/analyst groups (provide 3 to 5 records). For each record, include:
       - "analyst_firm": The name of the research firm (e.g., "HDFC Securities", "Motilal Oswal", "ICICI Direct", "Jefferies", "Macquarie").
       - "recommendation": "BUY", "SELL", or "HOLD".
       - "date": The date of recommendation in YYYY-MM-DD format (should be within the last 30 days relative to today).
    """

    res_json = None
    provider_name = None

    try:
        # Route based on key configuration preference: OpenAI -> Groq -> Anthropic -> Gemini
        if cfg["openai_key"]:
            provider_name = "OpenAI"
            res_json = call_openai(prompt, cfg["openai_key"])
        elif cfg["groq_key"]:
            provider_name = f"Groq ({cfg['groq_model']})"
            res_json = call_groq(prompt, cfg["groq_key"], cfg["groq_model"])
        elif cfg["anthropic_key"]:
            provider_name = "Anthropic"
            res_json = call_anthropic(prompt, cfg["anthropic_key"])
        elif cfg["gemini_key"]:
            provider_name = "Gemini"
            res_json = call_gemini(prompt, cfg["gemini_key"])
        
        if res_json:
            return {
                "sector": res_json.get("sector", "General Market"),
                "resistance_levels": res_json.get("resistance_levels", "N/A"),
                "support_levels": res_json.get("support_levels", "N/A"),
                "recommendation": res_json.get("recommendation", "HOLD").upper(),
                "comment": res_json.get("comment", ""),
                "analyst_recommendations": res_json.get("analyst_recommendations", [])
            }
    except Exception as e:
        logger.error(f"Error calling {provider_name or 'LLM Provider'} structured analysis for {symbol}: {str(e)}")
        
    # Graceful fallback indicators on error
    last_price = quote.get("last_price", 100.0)
    from datetime import datetime
    return {
        "sector": "General Market",
        "resistance_levels": f"R1: ₹{round(last_price * 1.02, 2)}, R2: ₹{round(last_price * 1.05, 2)}",
        "support_levels": f"S1: ₹{round(last_price * 0.98, 2)}, S2: ₹{round(last_price * 0.95, 2)}",
        "recommendation": "HOLD",
        "comment": f"Technical scanning for {symbol} indicates immediate resistance near current highs. (Analysis fallback active)",
        "analyst_recommendations": [
            {"analyst_firm": "System Scanner", "recommendation": "HOLD", "date": datetime.utcnow().strftime("%Y-%m-%d")}
        ]
    }


def generate_stock_commentary(symbol: str, name: str, quote: dict, news_articles: List[Dict[str, Any]]) -> str:
    """Compatibility wrapper for generate_stock_commentary."""
    analysis = analyze_stock_with_gemini(symbol, name, quote, news_articles, None)
    return analysis["comment"]


def chat_with_llm(symbol: str, name: str, quote: dict, news: List[Dict[str, Any]], user_message: str, history: List[Dict[str, str]]) -> str:
    """Chat with the active configured LLM provider about a specific stock using real-time price & news context."""
    cfg = reload_env_vars()
    
    if not (cfg["gemini_key"] or cfg["openai_key"] or cfg["anthropic_key"] or cfg["groq_key"]):
        return "Chat assistant is unavailable because no LLM API keys are configured."
        
    # Format quote / price metrics
    ohlc = quote.get("ohlc", {})
    last_price = quote.get("last_price", 0.0)
    prev_close = ohlc.get("close", 0.0)
    pct_change = ((last_price - prev_close) / prev_close) * 100 if prev_close else 0.0
    
    price_info = (
        f"LTP: {last_price} INR, Daily Change: {pct_change:.2f}%, "
        f"Open: {ohlc.get('open', 0.0)}, High: {ohlc.get('high', 0.0)}, "
        f"Low: {ohlc.get('low', 0.0)}, Volume: {quote.get('volume', 0)}"
    )
    
    # Format news text
    news_text = ""
    for idx, art in enumerate(news[:5]):
        news_text += f"- {art.get('headline')} (Source: {art.get('source')})\n"
    if not news_text:
        news_text = "No recent news headlines available."
        
    system_instruction = f"""
    You are a professional financial assistant specializing in the Indian stock market.
    Answer the user's question about {symbol} ({name}) based on the following real-time data:
    
    Price Data:
    {price_info}
    
    Recent News & Discussions:
    {news_text}
    
    Maintain a helpful, objective, and analytical tone. Do not make definitive investment recommendations, but explain trends, metrics, and news. Be concise and keep your response under 3-4 sentences.
    """
    
    provider_name = None
    try:
        # Route based on configuration preference: OpenAI -> Groq -> Anthropic -> Gemini
        if cfg["openai_key"]:
            provider_name = "OpenAI"
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {cfg['openai_key']}"
            }
            messages = [{"role": "system", "content": system_instruction}]
            for msg in history:
                messages.append({"role": msg["role"], "content": msg["content"]})
            messages.append({"role": "user", "content": user_message})
            
            payload = {
                "model": "gpt-4o-mini",
                "messages": messages
            }
            res = requests.post("https://api.openai.com/v1/chat/completions", json=payload, headers=headers, timeout=15)
            res.raise_for_status()
            return res.json()["choices"][0]["message"]["content"]
            
        elif cfg["groq_key"]:
            provider_name = f"Groq ({cfg['groq_model']})"
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {cfg['groq_key']}"
            }
            messages = []
            if "deepseek" in cfg["groq_model"].lower():
                # For Deepseek-R1 reasoning models, wrap instructions in a developer user block
                messages.append({"role": "user", "content": f"{system_instruction}\n\nUser Question: Hello!"})
                messages.append({"role": "assistant", "content": "Hello! I am ready to help you analyze this stock. What would you like to know?"})
            else:
                messages.append({"role": "system", "content": system_instruction})
                
            for msg in history:
                messages.append({"role": msg["role"], "content": msg["content"]})
            messages.append({"role": "user", "content": user_message})
            
            payload = {
                "model": cfg["groq_model"],
                "messages": messages
            }
            res = requests.post("https://api.groq.com/openai/v1/chat/completions", json=payload, headers=headers, timeout=15)
            res.raise_for_status()
            raw_text = res.json()["choices"][0]["message"]["content"]
            return clean_json_response(raw_text)
            
        elif cfg["anthropic_key"]:
            provider_name = "Anthropic"
            headers = {
                "content-type": "application/json",
                "x-api-key": cfg["anthropic_key"],
                "anthropic-version": "2023-06-01"
            }
            messages = []
            for msg in history:
                messages.append({"role": msg["role"], "content": msg["content"]})
            messages.append({"role": "user", "content": user_message})
            
            payload = {
                "model": "claude-3-5-sonnet-20241022",
                "max_tokens": 1000,
                "messages": messages,
                "system": system_instruction
            }
            res = requests.post("https://api.anthropic.com/v1/messages", json=payload, headers=headers, timeout=15)
            res.raise_for_status()
            return res.json()["content"][0]["text"]
            
        elif cfg["gemini_key"]:
            provider_name = "Gemini"
            genai.configure(api_key=cfg["gemini_key"])
            model = genai.GenerativeModel(
                "gemini-2.5-flash",
                system_instruction=system_instruction
            )
            contents = []
            for msg in history:
                role = "user" if msg["role"] == "user" else "model"
                contents.append({"role": role, "parts": [{"text": msg["content"]}]})
            contents.append({"role": "user", "parts": [{"text": user_message}]})
            
            response = model.generate_content(contents)
            return response.text
            
    except Exception as e:
        logger.error(f"Error in {provider_name or 'LLM'} stock chat: {e}")
        return f"Sorry, I encountered an error communicating with the AI assistant: {str(e)}"
        
    return "No active LLM providers configured in your environment variables."
