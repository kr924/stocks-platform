"""
Telegram callback handling — places and exits orders from chat.

Telegram delivers button presses as callback queries and typed commands as
messages. Both routes end in the same broker call, and every outcome is written
back into the originating message so the chat is an accurate record of what
happened.

Callback payloads (Telegram caps these at 64 bytes):

    ord:<pending_id>:<qty>:<MKT|LMT>    buy against a pending result
    sell:<config_id>:<qty>              exit a held position
    dis:<pending_id>                    dismiss the prompt

Typed commands:

    /buy <pending_id> <qty> <MKT|LMT> [price]
    /sell <symbol> <qty>
"""
import logging
from datetime import datetime

from fastapi import APIRouter, Request

from app.database import SessionLocal, PendingResultOrder, TradeConfig, TradeOrder
from app.services.telegram_notifier import answer_callback, send_message

logger = logging.getLogger("app.telegram_webhook")

router = APIRouter(prefix="/api/telegram", tags=["telegram"])


def _fmt_result(ok: bool, action: str, symbol: str, qty: int, price, message: str) -> str:
    head = "✅" if ok else "❌"
    price_txt = f"₹{price:,.2f}" if price else "n/a"
    return (
        f"{head} <b>{action} {'PLACED' if ok else 'FAILED'}</b>\n"
        f"<b>Symbol:</b> #{symbol}\n"
        f"<b>Quantity:</b> {qty}\n"
        f"<b>Fill price:</b> {price_txt}\n"
        f"<b>Detail:</b> {message[:300]}"
    )


def _place_buy(pending_id: int, qty: int, order_type: str, limit_price=None) -> tuple:
    """
    Buy against a pending result. Returns (ok, human_message).

    Mirrors the dashboard's order path so both surfaces produce identical state:
    a TradeConfig, a TradeOrder, and the prompt marked resolved.
    """
    from app.services.broker_gateway import get_broker
    from app.services.results_router import get_ltp, run_ai_for_pending

    db = SessionLocal()
    try:
        pending = db.query(PendingResultOrder).filter(
            PendingResultOrder.id == pending_id
        ).first()
        if not pending:
            return False, f"Result prompt #{pending_id} no longer exists."
        if pending.status != "pending":
            return False, f"#{pending.symbol} was already {pending.status}."

        if order_type == "LIMIT" and not limit_price:
            limit_price = get_ltp(pending.instrument_key, pending.symbol)
            if not limit_price:
                return False, (
                    f"No live price for {pending.symbol}, so a limit price could not "
                    f"be derived. Send an explicit price: "
                    f"/buy {pending_id} {qty} LMT <price>"
                )

        config = TradeConfig(
            symbol=pending.symbol,
            instrument_key=pending.instrument_key,
            purchase_date=datetime.utcnow().strftime("%Y-%m-%d"),
            quantity=qty,
            stoploss_pct=2.0,
            stoploss_type="software",
            broker="upstox",
            order_type=order_type,
            limit_price=limit_price,
            status="triggered",
            trigger_subject=(pending.title or "")[:200],
            notes=f"Placed from Telegram against result #{pending.id}",
            triggered_at=datetime.utcnow(),
        )
        db.add(config)
        db.commit()
        db.refresh(config)

        broker = get_broker("upstox")
        result = broker.place_order(
            symbol=pending.symbol,
            instrument_key=pending.instrument_key or "",
            side="BUY",
            quantity=qty,
            order_type=order_type,
            limit_price=limit_price,
            stoploss_type="software",
            stoploss_pct=2.0,
        )

        db.add(TradeOrder(
            config_id=config.id,
            symbol=pending.symbol,
            side="BUY",
            quantity=qty,
            order_type=order_type,
            limit_price=limit_price,
            price=result.price,
            broker="upstox",
            broker_order_id=result.broker_order_id,
            status=result.status,
            error_message=None if result.success else result.message,
        ))

        if result.success:
            config.status = "bought"
            config.buy_price = result.price
            config.bought_at = datetime.utcnow()
            pending.status = "ordered"
            pending.config_id = config.id
            pending.resolved_at = datetime.utcnow()
        else:
            config.status = "cancelled"
            config.is_active = False
            config.notes = f"Telegram order failed: {result.message}"
        db.commit()

        text = _fmt_result(
            result.success, f"{order_type} BUY", pending.symbol, qty,
            result.price, result.message or "",
        )
        if result.success:
            try:
                run_ai_for_pending(pending.id, config_id=config.id)
            except Exception as e:
                logger.error(f"Could not queue earnings AI for #{pending.id}: {e}")
        return result.success, text
    except Exception as e:
        db.rollback()
        logger.error(f"Telegram buy failed for pending #{pending_id}: {e}")
        return False, f"❌ <b>ORDER FAILED</b>\nUnexpected error: {str(e)[:250]}"
    finally:
        db.close()


def _place_sell(config_id: int, qty: int) -> tuple:
    """Exit a held position. Returns (ok, human_message)."""
    from app.services.broker_gateway import get_broker

    db = SessionLocal()
    try:
        config = db.query(TradeConfig).filter(TradeConfig.id == config_id).first()
        if not config:
            return False, f"Position #{config_id} no longer exists."
        if config.status != "bought":
            return False, f"#{config.symbol} is '{config.status}', not a held position."

        qty = min(qty, config.quantity)
        broker = get_broker(config.broker or "upstox")
        result = broker.place_order(
            symbol=config.symbol,
            instrument_key=config.instrument_key or "",
            side="SELL",
            quantity=qty,
            order_type="MARKET",
        )

        db.add(TradeOrder(
            config_id=config.id,
            symbol=config.symbol,
            side="SELL",
            quantity=qty,
            order_type="MARKET",
            price=result.price,
            broker=config.broker or "upstox",
            broker_order_id=result.broker_order_id,
            status=result.status,
            error_message=None if result.success else result.message,
        ))

        if result.success:
            sell_price = result.price or 0.0
            if qty >= config.quantity:
                config.status = "sold"
                config.sell_price = sell_price
                config.sold_at = datetime.utcnow()
                if config.buy_price:
                    config.pnl = round((sell_price - config.buy_price) * qty, 2)
            else:
                # Partial exit: keep the position open for the remainder.
                config.quantity -= qty
                config.notes = f"Partial exit of {qty} via Telegram @ ₹{sell_price}"
        db.commit()

        return result.success, _fmt_result(
            result.success, "SELL", config.symbol, qty, result.price, result.message or "",
        )
    except Exception as e:
        db.rollback()
        logger.error(f"Telegram sell failed for config #{config_id}: {e}")
        return False, f"❌ <b>SELL FAILED</b>\nUnexpected error: {str(e)[:250]}"
    finally:
        db.close()


def _dismiss(pending_id: int) -> tuple:
    db = SessionLocal()
    try:
        pending = db.query(PendingResultOrder).filter(
            PendingResultOrder.id == pending_id
        ).first()
        if not pending:
            return False, f"Result prompt #{pending_id} no longer exists."
        pending.status = "dismissed"
        pending.resolved_at = datetime.utcnow()
        db.commit()
        return True, f"✖ Dismissed #{pending.symbol} — no order placed."
    except Exception as e:
        db.rollback()
        return False, f"Could not dismiss: {str(e)[:200]}"
    finally:
        db.close()


def _handle_callback(cb: dict):
    """Route one inline-button press."""
    data = (cb.get("data") or "").strip()
    cb_id = cb.get("id")
    msg = cb.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")

    parts = data.split(":")
    action = parts[0] if parts else ""

    try:
        if action == "ord" and len(parts) >= 4:
            pending_id, qty = int(parts[1]), int(parts[2])
            order_type = "LIMIT" if parts[3].upper() == "LMT" else "MARKET"
            answer_callback(cb_id, f"Placing {order_type} buy x{qty}…")
            ok, text = _place_buy(pending_id, qty, order_type)

        elif action == "sell" and len(parts) >= 3:
            config_id, qty = int(parts[1]), int(parts[2])
            answer_callback(cb_id, f"Selling {qty}…")
            ok, text = _place_sell(config_id, qty)

        elif action == "dis" and len(parts) >= 2:
            answer_callback(cb_id, "Dismissed")
            ok, text = _dismiss(int(parts[1]))

        else:
            answer_callback(cb_id, "Unrecognised action")
            return
    except Exception as e:
        logger.error(f"Callback '{data}' failed: {e}")
        answer_callback(cb_id, "Failed — see the chat for details")
        text = f"❌ Action failed: {str(e)[:200]}"

    # Replace the prompt with the outcome, so the buttons cannot be pressed twice.
    if chat_id and message_id:
        from app.services.telegram_notifier import edit_message
        original = msg.get("text") or ""
        header = original.split("\n")[0] if original else ""
        edit_message(chat_id, message_id, f"{header}\n\n{text}", reply_markup={"inline_keyboard": []})
    else:
        send_message(text)


def _handle_message(message: dict):
    """Route a typed /buy or /sell command."""
    text = (message.get("text") or "").strip()
    if not text.startswith("/"):
        return

    parts = text.split()
    cmd = parts[0].lower().lstrip("/").split("@")[0]

    if cmd == "buy":
        # /buy <pending_id> <qty> <MKT|LMT> [price]
        if len(parts) < 3:
            send_message("Usage: <code>/buy &lt;result_id&gt; &lt;qty&gt; &lt;MKT|LMT&gt; [price]</code>")
            return
        try:
            pending_id, qty = int(parts[1]), int(parts[2])
        except ValueError:
            send_message("Result id and quantity must both be numbers.")
            return
        order_type = "MARKET"
        limit_price = None
        if len(parts) >= 4 and parts[3].upper() in ("LMT", "LIMIT"):
            order_type = "LIMIT"
            if len(parts) >= 5:
                try:
                    limit_price = float(parts[4])
                except ValueError:
                    send_message(f"'{parts[4]}' is not a valid price.")
                    return
        _, result_text = _place_buy(pending_id, qty, order_type, limit_price)
        send_message(result_text)

    elif cmd == "sell":
        # /sell <symbol> <qty>
        if len(parts) < 3:
            send_message("Usage: <code>/sell &lt;symbol&gt; &lt;qty&gt;</code>")
            return
        symbol = parts[1].upper().lstrip("#")
        try:
            qty = int(parts[2])
        except ValueError:
            send_message("Quantity must be a number.")
            return
        db = SessionLocal()
        try:
            config = db.query(TradeConfig).filter(
                TradeConfig.symbol == symbol,
                TradeConfig.status == "bought",
                TradeConfig.is_active == True,
            ).order_by(TradeConfig.bought_at.desc()).first()
            config_id = config.id if config else None
        finally:
            db.close()
        if not config_id:
            send_message(f"No open position found for #{symbol}.")
            return
        _, result_text = _place_sell(config_id, qty)
        send_message(result_text)

    elif cmd in ("positions", "holdings"):
        db = SessionLocal()
        try:
            held = db.query(TradeConfig).filter(
                TradeConfig.status == "bought", TradeConfig.is_active == True
            ).all()
            if not held:
                send_message("No open positions.")
                return
            lines = [
                f"#{c.symbol} — {c.quantity} @ ₹{c.buy_price or 0:,.2f}  (id {c.id})"
                for c in held
            ]
            send_message("<b>Open positions</b>\n" + "\n".join(lines))
        finally:
            db.close()


@router.post("/webhook")
async def telegram_webhook(request: Request):
    """Receive Telegram updates. Always 200 so Telegram does not retry."""
    try:
        update = await request.json()
    except Exception:
        return {"ok": True}

    try:
        if "callback_query" in update:
            _handle_callback(update["callback_query"])
        elif "message" in update:
            _handle_message(update["message"])
    except Exception as e:
        logger.error(f"Telegram update handling failed: {e}")

    return {"ok": True}


@router.post("/register-webhook")
def register_webhook(public_url: str):
    """Register this deployment's public URL with Telegram."""
    from app.services.telegram_notifier import set_webhook, get_webhook_info
    ok = set_webhook(public_url)
    return {"success": ok, "info": get_webhook_info()}


@router.get("/webhook-info")
def webhook_info():
    from app.services.telegram_notifier import get_webhook_info
    return get_webhook_info() or {"error": "Telegram bot token not configured"}
