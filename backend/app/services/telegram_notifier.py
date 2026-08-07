"""
Telegram notifications and interactive order placement.

Two kinds of message:

  * Plain alerts (send_telegram_alert) — informational.
  * Actionable alerts — carry an inline keyboard so an order can be placed, or a
    held position sold, straight from the chat. Button presses arrive back as
    callback queries and are handled in app/routers/telegram_webhook.py.

Callback payloads are capped at 64 bytes by Telegram, so they are kept terse:

    ord:<pending_id>:<qty>:<MKT|LMT>     place a buy for a pending result
    sell:<config_id>:<qty>               sell a held position
    dis:<pending_id>                     dismiss the prompt
"""
import logging
import os
from typing import List, Optional

import requests

logger = logging.getLogger("app.telegram_notifier")

_API = "https://api.telegram.org/bot{token}/{method}"


def _credentials() -> tuple:
    """Resolve (bot_token, chat_id) from the environment, then the YAML config."""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    if not bot_token or not chat_id:
        try:
            from app.services.intel_config import get_intel_config
            cfg = get_intel_config()
            # IntelConfig exposes the parsed document as `.raw`; `.raw_config`
            # never existed, so this fallback used to raise and every alert was
            # silently dropped whenever the env vars were unset.
            telegram_cfg = cfg.raw.get("notifications", {}).get("telegram", {})
            bot_token = bot_token or str(telegram_cfg.get("bot_token", "")).strip()
            chat_id = chat_id or str(telegram_cfg.get("chat_id", "")).strip()
        except Exception:
            pass

    if bot_token == "***configured***":
        bot_token = ""
    return bot_token, chat_id


def _post(method: str, payload: dict) -> Optional[dict]:
    """Call a Telegram Bot API method. Returns the parsed result, or None."""
    bot_token, _ = _credentials()
    if not bot_token:
        logger.debug(f"Telegram {method} skipped: bot token not configured.")
        return None
    try:
        res = requests.post(_API.format(token=bot_token, method=method), json=payload, timeout=10)
        data = res.json()
        if not data.get("ok"):
            logger.warning(f"Telegram {method} rejected: {str(data)[:200]}")
            return None
        return data.get("result")
    except Exception as e:
        logger.error(f"Telegram {method} failed: {e}")
        return None


def _esc(text) -> str:
    """Escape the three characters Telegram's HTML parse mode cares about."""
    return str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send_message(text: str, reply_markup: dict = None, chat_id: str = None) -> Optional[int]:
    """Send an HTML message. Returns the message id when it lands."""
    bot_token, default_chat = _credentials()
    target = chat_id or default_chat
    if not bot_token or not target:
        logger.debug("Telegram send skipped: bot token or chat id not configured.")
        return None

    payload = {
        "chat_id": target,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    result = _post("sendMessage", payload)
    return result.get("message_id") if result else None


def answer_callback(callback_query_id: str, text: str = "", show_alert: bool = False):
    """Acknowledge a button press so the client stops showing a spinner."""
    _post("answerCallbackQuery", {
        "callback_query_id": callback_query_id,
        "text": text[:200],
        "show_alert": show_alert,
    })


def edit_message(chat_id, message_id: int, text: str, reply_markup: dict = None):
    """Replace a message's text, typically to record the outcome of an action."""
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    _post("editMessageText", payload)


# ─── Plain alerts ───────────────────────────────────────────────────────────

def send_telegram_alert(
    title: str,
    symbol: Optional[str] = None,
    sentiment: str = "neutral",
    impact_score: float = 0.0,
    summary: str = "",
    provider: str = "AI",
    url: Optional[str] = None,
    alert_type: str = "FINANCIAL ANALYSIS ALERT",
    company_name: str = "",
    reply_markup: dict = None,
) -> bool:
    """Send a formatted informational alert. Returns True when delivered."""
    sent_upper = (sentiment or "neutral").upper()
    emoji = "🟢" if "POS" in sent_upper else "🔴" if "NEG" in sent_upper else "⚪"
    impact_str = f"{'+' if impact_score > 0 else ''}{impact_score:.1f}"

    symbol_tag = f"#{symbol.upper()}" if symbol else "#GENERAL"
    company_line = f"\n<b>Company:</b> {_esc(company_name)}" if company_name else ""

    message = (
        f"<b>🚨 [{_esc(alert_type)}]</b>\n\n"
        f"<b>Stock:</b> {_esc(symbol_tag)}{company_line}\n"
        f"<b>Headline:</b> {_esc(title)}\n"
        f"<b>Verdict:</b> {emoji} <b>{_esc(sent_upper)} ({impact_str})</b>\n"
        f"<b>AI Engine:</b> {_esc(provider.upper())}\n\n"
        f"<b>Summary:</b>\n<i>{_esc(summary)[:900]}</i>"
    )
    if url and str(url).startswith("http"):
        message += f'\n\n🔗 <a href="{url}">View Full Announcement</a>'

    msg_id = send_message(message, reply_markup=reply_markup)
    if msg_id:
        logger.info(f"📲 [TELEGRAM SENT]: {symbol or 'GENERAL'} — '{title[:40]}'")
        return True
    return False


# ─── Actionable alerts ──────────────────────────────────────────────────────

# Quantities offered as one-tap buttons. Anything else is typed as a reply
# command, which the webhook also understands.
_QTY_CHOICES = (1, 5, 10, 25)


def _order_keyboard(pending_id: int) -> dict:
    """Inline keyboard: pick a quantity, at market or limit, or dismiss."""
    market_row = [
        {"text": f"MKT x{q}", "callback_data": f"ord:{pending_id}:{q}:MKT"}
        for q in _QTY_CHOICES
    ]
    limit_row = [
        {"text": f"LMT x{q}", "callback_data": f"ord:{pending_id}:{q}:LMT"}
        for q in _QTY_CHOICES
    ]
    return {
        "inline_keyboard": [
            market_row,
            limit_row,
            [{"text": "✖ Dismiss", "callback_data": f"dis:{pending_id}"}],
        ]
    }


def send_result_order_alert(
    symbol: str,
    company_name: str,
    exchange: str,
    title: str,
    last_price: Optional[float],
    url: Optional[str] = None,
    pending_id: Optional[int] = None,
) -> bool:
    """
    Alert that a financial result has landed, with one-tap order buttons.

    Buttons only appear when `pending_id` is set — that is, when the stock is not
    already armed and therefore still needs an order decision.
    """
    price_line = f"₹{last_price:,.2f}" if last_price else "unavailable"
    company_line = f"\n<b>Company:</b> {_esc(company_name)}" if company_name else ""

    message = (
        f"<b>📊 FINANCIAL RESULTS</b>\n\n"
        f"<b>Symbol:</b> #{_esc(symbol)}{company_line}\n"
        f"<b>Exchange:</b> {_esc(exchange.upper())}\n"
        f"<b>Current Price:</b> {price_line}\n"
        f"<b>Filing:</b> {_esc(title)[:220]}\n"
    )
    if url and str(url).startswith("http"):
        message += f'\n🔗 <a href="{url}">Open filing</a>\n'

    if pending_id:
        limit_note = (
            f"at ₹{last_price:,.2f}" if last_price else "at the last traded price"
        )
        message += (
            f"\n<b>Place an order</b> — MKT buys at market, LMT places a limit {limit_note}.\n"
            f"<i>For a custom quantity or price reply:</i>\n"
            f"<code>/buy {pending_id} &lt;qty&gt; &lt;MKT|LMT&gt; [price]</code>"
        )
        markup = _order_keyboard(pending_id)
    else:
        message += "\n<i>This stock is armed — the trading engine is placing the order.</i>"
        markup = None

    return send_message(message, reply_markup=markup) is not None


def _sell_keyboard(config_id: int, held_qty: int) -> dict:
    """Inline keyboard offering partial or full exit of a held position."""
    row = []
    for q in (1, 5, 10):
        if q < held_qty:
            row.append({"text": f"SELL {q}", "callback_data": f"sell:{config_id}:{q}"})
    row.append({"text": f"SELL ALL ({held_qty})", "callback_data": f"sell:{config_id}:{held_qty}"})
    return {"inline_keyboard": [row]}


def send_earnings_verdict_alert(
    symbol: str,
    company_name: str,
    verdict: str,
    summary: str,
    metrics_table: str,
    provider: str,
    url: Optional[str] = None,
    held_config_id: Optional[int] = None,
    held_qty: int = 0,
    buy_price: Optional[float] = None,
    last_price: Optional[float] = None,
) -> bool:
    """
    Post the structured earnings verdict.

    When the stock is already held, the alert carries SELL buttons and the live
    P&L, so an exit can be taken from the same message that delivered the news.
    """
    company_line = f"\n<b>Company:</b> {_esc(company_name)}" if company_name else ""
    verdict_txt = verdict or "NA"
    emoji = (
        "🟢" if verdict_txt.upper() in ("BUY", "BEATS ESTIMATES")
        else "🔴" if verdict_txt.upper() in ("SELL", "MISSES ESTIMATES")
        else "⚪"
    )

    message = (
        f"<b>🧠 EARNINGS ANALYSIS — {_esc(verdict_txt)}</b> {emoji}\n\n"
        f"<b>Symbol:</b> #{_esc(symbol)}{company_line}\n"
        f"<b>Engine:</b> {_esc(provider)}\n\n"
        f"<pre>{_esc(metrics_table)}</pre>\n"
        f"<b>Summary:</b>\n<i>{_esc(summary)[:700]}</i>"
    )

    markup = None
    if held_config_id and held_qty > 0:
        pnl_line = ""
        if buy_price and last_price:
            pnl = (last_price - buy_price) * held_qty
            pct = ((last_price - buy_price) / buy_price * 100) if buy_price else 0
            pnl_line = (
                f"\n\n<b>Position:</b> {held_qty} @ ₹{buy_price:,.2f} → "
                f"₹{last_price:,.2f} ({pct:+.2f}%, ₹{pnl:+,.2f})"
            )
        message += pnl_line + "\n\n<i>You hold this stock — sell below.</i>"
        markup = _sell_keyboard(held_config_id, held_qty)

    if url and str(url).startswith("http"):
        message += f'\n\n🔗 <a href="{url}">Open filing</a>'

    return send_message(message, reply_markup=markup) is not None


# ─── Webhook registration ───────────────────────────────────────────────────

def set_webhook(public_url: str) -> bool:
    """
    Point Telegram at our callback endpoint.

    `public_url` must be the externally reachable base URL; the bot API refuses
    plain HTTP and private addresses.
    """
    url = public_url.rstrip("/") + "/api/telegram/webhook"
    result = _post("setWebhook", {
        "url": url,
        "allowed_updates": ["callback_query", "message"],
    })
    if result is not None:
        logger.info(f"Telegram webhook registered at {url}")
        return True
    return False


def delete_webhook() -> bool:
    return _post("deleteWebhook", {}) is not None


def get_webhook_info() -> Optional[dict]:
    return _post("getWebhookInfo", {})
