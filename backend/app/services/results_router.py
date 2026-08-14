"""
Financial-results routing.

Financial results are owned by the auto-trading path, not the intelligence feed.
When a result is captured by the exchange hub this module decides what happens:

    Stock IS armed      -> the trading poller already matched the same fetch and
                           is placing the order; we only record the linkage.
    Stock is NOT armed  -> raise a PendingResultOrder so the Auto Trading panel
                           immediately shows an order-placement screen, fire a
                           Telegram alert, then run the existing 2-step earnings
                           AI in the background.

Order matters: the order screen must appear before AI analysis starts, because
the AI call can take minutes and the trading decision cannot wait for it.
"""
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.database import PendingResultOrder, TradeConfig
from app.services.deduplication import to_iso_utc
from app.services.sse_manager import sse_manager

logger = logging.getLogger("app.results_router")

IST = timezone(timedelta(hours=5, minutes=30))

# Single worker: earnings analysis downloads a PDF and calls a premium LLM.
# Running more than one at a time on a 1-vCPU box is what caused the CPU pile-up.
_ai_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="earnings-ai")


# Results filed after this IST time get no alert and no AI analysis. The market
# closes at 15:30, so there is no session left to act in; they are reported in
# the next morning's digest from Screener's figures instead.
INTRADAY_CUTOFF_HOUR = 15
INTRADAY_CUTOFF_MINUTE = 25


def is_within_action_window(now_ist: datetime = None) -> bool:
    """
    True when a result arriving now can still be acted on today.

    False after 15:25 on a working day, and false all day at weekends — in both
    cases there is no session left to trade into, so the result raises no alert
    and consumes no AI budget. It appears in the next 08:00 digest instead.
    """
    now = now_ist or datetime.now(IST)
    if now.weekday() > 4:                       # Saturday / Sunday
        return False
    cutoff = now.replace(hour=INTRADAY_CUTOFF_HOUR, minute=INTRADAY_CUTOFF_MINUTE,
                         second=0, microsecond=0)
    return now <= cutoff


def make_tracking_ref(symbol: str, when: datetime = None) -> str:
    """
    Short correlation id, e.g. AARTIPHAR-0807-3F2A.

    Printed on the arrival alert, on the AI verdict alert and in the app row, so
    a Telegram message can be traced to the analysis it belongs to without
    anyone having to match on timestamps.
    """
    import uuid
    when = when or datetime.now(IST)
    stem = re.sub(r"[^A-Z0-9]", "", (symbol or "GEN").upper())[:9] or "GEN"
    return f"{stem}-{when.strftime('%d%m')}-{uuid.uuid4().hex[:4].upper()}"


def _resolve_instrument_key(symbol: str) -> str:
    """
    Map a symbol to its Upstox instrument key, NSE first then BSE.

    A BSE-only scrip has no NSE key at all, and the synthetic `NSE_EQ|<SYMBOL>`
    we used to fall back to is not a key any endpoint accepts — it cost those
    stocks their live price and their chart.
    """
    if not symbol:
        return ""
    try:
        from app.main import resolve_instrument_keys
        keys = resolve_instrument_keys(symbol)
        if keys:
            return keys[0]
    except Exception:
        pass
    return f"NSE_EQ|{symbol.upper()}"


def _is_armed(db: Session, symbol: str) -> bool:
    """Is there an armed trade config for this symbol today?"""
    if not symbol:
        return False
    today_ist = datetime.now(IST).strftime("%Y-%m-%d")
    return db.query(TradeConfig).filter(
        TradeConfig.symbol == symbol.upper(),
        TradeConfig.status == "armed",
        TradeConfig.is_active == True,
        TradeConfig.purchase_date == today_ist,
    ).first() is not None


def get_ltp(instrument_key: str, symbol: str = "") -> Optional[float]:
    """
    Current traded price, or None when the feed cannot supply one.

    This is the baseline for "move since result", so it is captured once and
    never recomputed — a miss here leaves that column blank for the life of the
    row. The fallback key must therefore be resolved rather than synthesised:
    `NSE_EQ|<SYMBOL>` fails the whole request, which is why every BSE-listed
    filing was landing without a baseline while NSE ones were fine.
    """
    try:
        from app.main import get_active_feed, resolve_instrument_keys
        feed = get_active_feed()
        # Pipe form only. `BSE_EQ:INE...` is how quotes come back keyed, not a
        # key the API accepts — sending it returns UDAPI1087 for the whole
        # request, including the keys that were fine.
        keys = []
        if instrument_key:
            keys.append(instrument_key.replace(":", "|"))
        for k in resolve_instrument_keys(symbol):
            if k not in keys:
                keys.append(k)
        if not keys:
            logger.warning(f"No instrument key resolves for {symbol} — no price baseline captured")
            return None
        quotes = feed.get_quotes(keys)
        for q in quotes.values():
            price = q.get("last_price")
            if price:
                return round(float(price), 2)
        logger.warning(f"No price in feed response for {symbol or instrument_key} ({keys})")
    except Exception as e:
        # Not debug: a silent miss here is invisible until someone notices the
        # column is empty, which is how this went unnoticed.
        logger.warning(f"LTP lookup failed for {symbol or instrument_key}: {e}")
    return None


def _send_result_alert(ann, event, pending_id=None, instrument_key: str = "", pending=None,
                       last_price=None):
    """
    Immediate Telegram alert that a result has landed, ahead of the AI verdict.

    Carries symbol, company name and current price. When the stock is not armed
    the alert also offers inline order buttons addressed to `pending_id`.
    """
    try:
        from app.services.telegram_notifier import send_result_order_alert
        send_result_order_alert(
            symbol=ann.symbol,
            company_name=ann.company_name,
            exchange=ann.exchange,
            title=ann.title or "Financial Results",
            # No second lookup when the caller's came back empty. The only
            # reason it is empty during a results burst is that Upstox is rate
            # limiting us, and retrying immediately doubles the pressure that
            # caused it — the alert can go out without a price.
            last_price=last_price,
            url=ann.url,
            pending_id=pending_id,
            tracking_ref=getattr(pending, "tracking_ref", None),
            announced_at=ann.event_time,
            ingested_at=getattr(pending, "created_at", None),
        )
    except Exception as e:
        logger.debug(f"Telegram dispatch error for {ann.symbol}: {e}")


def _run_earnings_ai(symbol: str, title: str, attachment_url: str, description: str,
                     config_id, pending_id):
    """Background worker: run the existing 2-step earnings analysis."""
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        pending = None
        tracking_ref = None
        if pending_id:
            pending = db.query(PendingResultOrder).filter(PendingResultOrder.id == pending_id).first()
            if pending:
                pending.ai_status = "running"
                pending.ai_requested_at = datetime.utcnow()
                tracking_ref = pending.tracking_ref
                db.commit()

        from app.services.trade_ai_analyzer import analyze_earnings_disclosure_2step
        analyze_earnings_disclosure_2step(
            symbol=symbol,
            title=title,
            attachment_url=attachment_url,
            pdf_text=description,
            config_id=config_id,
            tracking_ref=tracking_ref,
        )

        if pending_id:
            pending = db.query(PendingResultOrder).filter(PendingResultOrder.id == pending_id).first()
            if pending:
                from app.database import TradeAILog
                latest = (
                    db.query(TradeAILog)
                    .filter(TradeAILog.symbol == symbol)
                    .order_by(TradeAILog.created_at.desc())
                    .first()
                )
                pending.ai_status = "done"
                pending.ai_completed_at = datetime.utcnow()
                pending.ai_log_id = latest.id if latest else None
                db.commit()

                sse_manager.broadcast("result_ai_done", {
                    "pending_id": pending.id,
                    "symbol": symbol,
                    "tracking_ref": pending.tracking_ref,
                    "ai_log_id": pending.ai_log_id,
                    "ai_requested_at": to_iso_utc(pending.ai_requested_at),
                    "ai_completed_at": to_iso_utc(pending.ai_completed_at),
                })
    except Exception as e:
        logger.error(f"[EARNINGS AI] failed for {symbol}: {e}")
        try:
            if pending_id:
                pending = db.query(PendingResultOrder).filter(
                    PendingResultOrder.id == pending_id
                ).first()
                if pending:
                    pending.ai_status = "failed"
                    pending.ai_completed_at = datetime.utcnow()
                    db.commit()
        except Exception:
            db.rollback()
    finally:
        db.close()


def route_financial_result(db: Session, ann, event, dedup_key: str):
    """
    Handle one newly captured financial result.

    `ann` is an exchange_hub.Announcement, `event` the persisted MarketEvent.
    """
    symbol = (ann.symbol or "").upper()

    # Mark the event as belonging to the auto-trading path so the intelligence
    # feed can exclude it.
    event.category = "financial_results"
    event.ai_provider = "auto_trading"
    event.ai_summary = f"Financial results filed on {ann.exchange.upper()} — routed to Auto Trading."
    try:
        db.commit()
    except Exception:
        db.rollback()

    armed = _is_armed(db, symbol)

    if armed:
        # The trading poller matched this same fetch and is already executing;
        # it runs its own AI analysis with the real config_id attached.
        logger.info(f"📊 [RESULT] {symbol} is ARMED — trading engine owns execution.")
        _send_result_alert(ann, event, instrument_key=_resolve_instrument_key(symbol))
        return

    # ── Not armed: raise an order prompt immediately ──
    pending = db.query(PendingResultOrder).filter(
        PendingResultOrder.dedup_key == dedup_key
    ).first()
    if pending:
        return

    actionable = is_within_action_window()

    pending = PendingResultOrder(
        symbol=symbol,
        company_name=ann.company_name or None,
        trade_date=datetime.now(IST).strftime("%Y-%m-%d"),
        instrument_key=_resolve_instrument_key(symbol),
        isin=ann.isin or None,
        exchange=ann.exchange,
        title=ann.title[:500],
        description=ann.description,
        attachment_url=ann.url,
        event_time=ann.event_time,
        dedup_key=dedup_key,
        tracking_ref=make_tracking_ref(symbol),
        status="pending",
        deferred=not actionable,
        ai_status="pending" if actionable else "deferred",
    )
    try:
        db.add(pending)
        db.commit()
        db.refresh(pending)
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to create PendingResultOrder for {symbol}: {e}")
        return

    # Push the order screen to the UI regardless — the row is still visible in
    # the app, it simply does not alert or analyse until the morning.
    sse_manager.broadcast("pending_result_order", {
        "id": pending.id,
        "symbol": pending.symbol,
        "company_name": pending.company_name,
        "tracking_ref": pending.tracking_ref,
        "instrument_key": pending.instrument_key,
        "exchange": pending.exchange,
        "title": pending.title,
        "description": (pending.description or "")[:400],
        "attachment_url": pending.attachment_url,
        "time": to_iso_utc(pending.event_time),
        "ingested_at": to_iso_utc(pending.created_at),
        "status": pending.status,
        "ai_status": pending.ai_status,
        "deferred": pending.deferred,
    })

    if not actionable:
        logger.info(
            f"🌙 [RESULT DEFERRED] {symbol} ({pending.tracking_ref}) arrived after the "
            f"15:20 IST cutoff — no alert or AI now; held for the 08:00 digest."
        )
        return

    logger.info(
        f"🔔 [RESULT] {symbol} ({pending.tracking_ref}) is NOT armed — order prompt raised (#{pending.id})."
    )

    # One quote call serves both the alert and the baseline for "move since the
    # result landed", so this costs nothing extra on the ingest path.
    ltp_at_announcement = get_ltp(pending.instrument_key, symbol)

    _send_result_alert(
        ann, event, pending_id=pending.id,
        instrument_key=pending.instrument_key, pending=pending,
        last_price=ltp_at_announcement,
    )
    try:
        pending.alert_sent_at = datetime.utcnow()
        pending.price_at_announcement = ltp_at_announcement
        db.commit()
    except Exception:
        db.rollback()

    # Then run the AI analysis in the background.
    _ai_pool.submit(
        _run_earnings_ai,
        symbol,
        ann.title,
        ann.url,
        ann.description,
        None,
        pending.id,
    )


def expire_stale_pending(db: Session) -> int:
    """
    Retire result prompts left over from previous trading days.

    An order decision on yesterday's print is meaningless once the market has
    re-opened and repriced, so the panel starts each day clean rather than
    accumulating a backlog the user has to scroll past.
    """
    today = datetime.now(IST).strftime("%Y-%m-%d")
    try:
        stale = db.query(PendingResultOrder).filter(
            PendingResultOrder.status == "pending",
            (PendingResultOrder.trade_date != today) | (PendingResultOrder.trade_date.is_(None)),
        ).all()
        if not stale:
            return 0
        for row in stale:
            # Rows predating the trade_date column are dated from created_at.
            if row.trade_date is None and row.created_at:
                inferred = row.created_at.strftime("%Y-%m-%d")
                row.trade_date = inferred
                if inferred == today:
                    continue
            row.status = "expired"
            row.resolved_at = datetime.utcnow()
        db.commit()
        expired = sum(1 for r in stale if r.status == "expired")
        if expired:
            logger.info(f"Expired {expired} result prompts from earlier trading days.")
        return expired
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to expire stale pending results: {e}")
        return 0


def run_ai_for_pending(pending_id: int, config_id=None):
    """Public hook so the API can (re)trigger analysis for a pending result."""
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        pending = db.query(PendingResultOrder).filter(
            PendingResultOrder.id == pending_id
        ).first()
        if not pending:
            return False
        _ai_pool.submit(
            _run_earnings_ai,
            pending.symbol,
            pending.title,
            pending.attachment_url,
            pending.description,
            config_id,
            pending.id,
        )
        return True
    finally:
        db.close()
