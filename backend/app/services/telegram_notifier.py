"""
Real-time Telegram Notification Service for Financial Results & Breaking Stock Market News.
Sends formatted HTML messages to a configured Telegram bot & chat ID.
"""
import logging
import os
import requests
from typing import Optional

logger = logging.getLogger("app.telegram_notifier")


def send_telegram_alert(
    title: str,
    symbol: Optional[str] = None,
    sentiment: str = "neutral",
    impact_score: float = 0.0,
    summary: str = "",
    provider: str = "AI",
    url: Optional[str] = None,
    alert_type: str = "FINANCIAL ANALYSIS ALERT"
) -> bool:
    """
    Send a real-time Telegram notification alert for news / financial results.
    Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from environment or config.
    """
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    if not bot_token or not chat_id:
        # Fallback to intel_config if env vars not directly set
        try:
            from app.services.intel_config import get_intel_config
            cfg = get_intel_config()
            # IntelConfig exposes the parsed document as `.raw`; `.raw_config`
            # never existed, so this fallback always raised and every alert was
            # silently dropped whenever the env vars were unset.
            telegram_cfg = cfg.raw.get("notifications", {}).get("telegram", {})
            bot_token = bot_token or telegram_cfg.get("bot_token", "").strip()
            chat_id = chat_id or str(telegram_cfg.get("chat_id", "")).strip()
        except Exception:
            pass

    if not bot_token or not chat_id or bot_token == "***configured***":
        logger.debug("Telegram notification skipped: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not configured.")
        return False

    # Emoji badge based on sentiment
    sent_upper = (sentiment or "neutral").upper()
    emoji = "🟢" if "POS" in sent_upper else "🔴" if "NEG" in sent_upper else "⚪"
    impact_str = f"{'+' if impact_score > 0 else ''}{impact_score:.1f}"

    symbol_tag = f"#{symbol.upper()}" if symbol else "#GENERAL"
    cleaned_summary = summary.replace("<", "&lt;").replace(">", "&gt;") if summary else "No summary available."
    cleaned_title = title.replace("<", "&lt;").replace(">", "&gt;")

    message = f"""<b>🚨 [{alert_type}]</b>

<b>Stock:</b> {symbol_tag}
<b>Headline:</b> {cleaned_title}
<b>Verdict:</b> {emoji} <b>{sent_upper} ({impact_str})</b>
<b>AI Engine:</b> {provider.upper()}

<b>Summary:</b>
<i>{cleaned_summary[:750]}</i>
"""
    if url and url.startswith("http"):
        message += f'\n🔗 <a href="{url}">View Full Announcement</a>'

    api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False
    }

    try:
        res = requests.post(api_url, json=payload, timeout=8)
        if res.status_code == 200:
            logger.info(f"📲 [TELEGRAM SENT]: Alert dispatched for [{symbol or 'GENERAL'}] '{title[:30]}...'")
            return True
        else:
            logger.warning(f"Telegram API response {res.status_code}: {res.text[:100]}")
            return False
    except Exception as e:
        logger.error(f"Failed to send Telegram alert: {e}")
        return False
