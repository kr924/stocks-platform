"""
Morning digest for results filed after the intraday cutoff.

Results land mostly after the close, when there is no session left to trade
into. Those are deferred (see results_router.is_within_action_window) and
collected here instead:

    08:00 IST  publish one HTML page and send a single Telegram alert linking to it

The page lists every company whose results were announced the previous day and
shows Screener.in's reported figures for the quarter. Our AI's extraction sits
beside them *only where it exists* — that is, where the filing arrived before the
09:00-15:30 window and was analysed live. Filings outside it are
never analysed, so their AI column reads "not analysed", and Screener is the
single source for them.
"""
import html
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import PendingResultOrder, TradeAILog
from app.services.screener_quarters import (
    compare, fetch_latest_quarter, parse_ai_value, screener_all_positive,
    screener_signal,
)
from app.services.trade_ai_analyzer import (
    METRIC_ROWS, _ROW_LABELS, normalize_metrics,
)

logger = logging.getLogger("app.morning_digest")

IST = timezone(timedelta(hours=5, minutes=30))

_DIGEST_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "digests",
)

# Cap the overnight queue so one heavy results day cannot run the AI for hours.
MAX_ANALYSES_PER_RUN = 40


def _ist_str(dt) -> str:
    if not dt:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).strftime("%d %b %H:%M:%S")


DIGEST_WINDOW_HOUR = 8  # IST


def digest_window(now_ist: datetime = None) -> tuple:
    """
    The 08:00-to-08:00 IST window this digest reports on, as naive UTC bounds.

    The 08:00 run covers everything filed from 08:00 the previous day up to
    08:00 today, so a filing belongs to exactly one digest and the boundary sits
    where nobody is trading rather than at midnight, mid-way through the
    overnight batch.
    """
    now = now_ist or datetime.now(IST)
    end_ist = now.replace(hour=DIGEST_WINDOW_HOUR, minute=0, second=0, microsecond=0)
    if now < end_ist:                      # a run before 08:00 belongs to the previous window
        end_ist -= timedelta(days=1)
    start_ist = end_ist - timedelta(days=1)
    return (start_ist.astimezone(timezone.utc).replace(tzinfo=None),
            end_ist.astimezone(timezone.utc).replace(tzinfo=None))


def collect_for_digest(db: Session, lookback_days: int = 4) -> List[PendingResultOrder]:
    """
    Everything in this digest's 08:00-to-08:00 window, oldest first.

    Anything older that was never digested is included too — a weekend, or a
    morning the job did not run, would otherwise vanish silently, and a filing
    reported late is better than one never reported. On an ordinary weekday that
    tail is empty and the digest is exactly the window.

    Both intraday and out-of-hours filings appear: the difference between them
    is whether an AI analysis exists, not whether they are reported.
    """
    start, end = digest_window()
    since = datetime.utcnow() - timedelta(days=lookback_days)
    filed_at = func.coalesce(PendingResultOrder.event_time, PendingResultOrder.created_at)
    return (
        db.query(PendingResultOrder)
        .filter(
            PendingResultOrder.created_at >= since,
            PendingResultOrder.digest_sent_at.is_(None),
            filed_at < end,
        )
        .order_by(PendingResultOrder.event_time.asc())
        .all()
    )


# Kept under the old name so existing callers and tests keep working.
collect_deferred = collect_for_digest


def run_deferred_analyses(db: Session) -> int:
    """
    Retained as a manual escape hatch only; nothing schedules it.

    Post-cutoff filings are deliberately not analysed — that is the point of the
    cutoff. Calling this analyses them anyway, which is occasionally useful when
    reviewing a past day by hand, but it is never invoked automatically.
    """
    from app.services.trade_ai_analyzer import analyze_earnings_disclosure_2step

    pending = [p for p in collect_for_digest(db) if p.ai_status in ("deferred", "pending", "failed")]
    if not pending:
        return 0

    logger.info(f"[DIGEST] Manually analysing {min(len(pending), MAX_ANALYSES_PER_RUN)} filings.")
    done = 0
    for row in pending[:MAX_ANALYSES_PER_RUN]:
        try:
            row.ai_status = "running"
            row.ai_requested_at = datetime.utcnow()
            db.commit()
            analyze_earnings_disclosure_2step(
                symbol=row.symbol, title=row.title,
                attachment_url=row.attachment_url or "", pdf_text=row.description or "",
                config_id=None, tracking_ref=row.tracking_ref,
            )
            latest = (db.query(TradeAILog).filter(TradeAILog.symbol == row.symbol)
                      .order_by(TradeAILog.created_at.desc()).first())
            row.ai_log_id = latest.id if latest else None
            row.ai_status = "done"
            row.ai_completed_at = datetime.utcnow()
            db.commit()
            done += 1
        except Exception as e:
            db.rollback()
            logger.error(f"[DIGEST] Manual analysis failed for {row.symbol}: {e}")
    return done


def _build_rows(db: Session, pendings: List[PendingResultOrder],
                cache_only: bool = False) -> List[dict]:
    """Assemble per-company comparison data: our AI's figures against Screener's."""
    rows = []
    for p in pendings:
        log = None
        if p.ai_log_id:
            log = db.query(TradeAILog).filter(TradeAILog.id == p.ai_log_id).first()
        if log is None:
            log = (
                db.query(TradeAILog)
                .filter(TradeAILog.symbol == p.symbol)
                .order_by(TradeAILog.created_at.desc())
                .first()
            )

        ai_metrics = {}
        if log and log.metrics_json:
            try:
                ai_metrics = normalize_metrics(json.loads(log.metrics_json))
            except Exception:
                ai_metrics = {}

        screener = fetch_latest_quarter(p.symbol, cache_only=cache_only)

        comparison = []
        for key in METRIC_ROWS:
            ai_cell = (ai_metrics.get(key) or {}).get("current_qtr", "NA")
            ai_num = parse_ai_value(ai_cell)
            actual = (screener.get("metrics", {}).get(key) or {}).get("current_qtr")
            actual_yoy = (screener.get("metrics", {}).get(key) or {}).get("yoy_change_pct")
            actual_qoq = (screener.get("metrics", {}).get(key) or {}).get("qoq_change_pct")
            comparison.append({
                "label": _ROW_LABELS[key],
                "ai_text": ai_cell,
                "ai_num": ai_num,
                "actual": actual,
                "actual_yoy": actual_yoy,
                "actual_qoq": actual_qoq,
                "ai_yoy": (ai_metrics.get(key) or {}).get("yoy_change_pct", "NA"),
                **compare(ai_num, actual),
            })

        # "not analysed" and "analysed but inconclusive" are different states and
        # must not both render as NA — one is a policy decision, the other a
        # judgement about the filing.
        analysed = bool(log and log.ai_completed_at)
        validation = None
        if log and getattr(log, "validation_json", None):
            try:
                validation = json.loads(log.validation_json)
            except Exception:
                validation = None
        rows.append({
            "pending": p,
            "log": log,
            "screener": screener,
            "screener_signal": screener_signal(screener),
            "all_positive": screener_all_positive(screener),
            "comparison": comparison,
            "validation": validation,
            "analysed": analysed,
            "verdict": ((log.ai_suggestion if log else None) or "NA") if analysed else "NOT ANALYSED",
        })
    return rows


# ─── HTML rendering ─────────────────────────────────────────────────────────

_CSS = """
:root{--bg:#0f1319;--s1:#161b24;--s2:#1c222d;--line:rgba(255,255,255,.10);
--tx:#e3e7ee;--tx2:#a8b2c4;--tx3:#8b95a6;--pos:#3fbf87;--neg:#f0736f;
--warn:#e0a33e;--acc:#5b9dff;--na:#7c8698}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--tx);font:14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;padding:24px}
h1{font-size:20px;margin-bottom:4px}
.sub{color:var(--tx3);font-size:12px;margin-bottom:20px}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:11px;color:var(--tx3);
margin-bottom:20px;padding:10px 12px;background:var(--s1);border:1px solid var(--line);border-radius:8px}
.card{background:var(--s1);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:16px}
.hd{display:flex;flex-wrap:wrap;align-items:baseline;gap:10px;margin-bottom:4px}
.sym{font-size:17px;font-weight:800}
.co{color:var(--tx2);font-size:13px}
.ref{font-family:ui-monospace,Menlo,monospace;font-size:10px;color:var(--tx3);
background:var(--s2);padding:2px 6px;border-radius:4px}
.badge{font-size:10px;font-weight:800;padding:2px 8px;border-radius:4px}
.b-pos{color:var(--pos);background:rgba(63,191,135,.14)}
.b-neg{color:var(--neg);background:rgba(240,115,111,.14)}
.b-na{color:var(--na);background:rgba(124,134,152,.14)}
.title{color:var(--tx2);font-size:12px;margin-bottom:10px}
.times{display:flex;flex-wrap:wrap;gap:14px;font-size:10px;color:var(--tx3);margin-bottom:12px}
.times b{color:var(--tx2);font-weight:600}
table{width:100%;border-collapse:collapse;font-size:12px}
th{text-align:right;padding:6px 8px;color:var(--tx3);font-size:10px;
text-transform:uppercase;letter-spacing:.4px;border-bottom:1px solid var(--line)}
th:first-child{text-align:left}
td{text-align:right;padding:6px 8px;border-bottom:1px solid rgba(255,255,255,.04);
font-variant-numeric:tabular-nums}
td:first-child{text-align:left;font-weight:600}
.na{color:var(--na);font-style:italic}
.ok{color:var(--pos)}.bad{color:var(--neg)}.unk{color:var(--na)}
.src{font-size:10px;color:var(--tx3);margin-top:8px}
a{color:var(--acc)}
.empty{padding:40px;text-align:center;color:var(--tx3)}
@media(max-width:640px){body{padding:12px}table{font-size:11px}}
"""


def _fmt(v) -> str:
    if v is None:
        return '<span class="na">NA</span>'
    return f"{v:,.2f}"


def render_html(rows: List[dict], for_date: str) -> str:
    """One self-contained page: every company filed overnight, AI beside actuals."""
    e = html.escape
    cards = []

    for r in rows:
        p, log, sc = r["pending"], r["log"], r["screener"]
        verdict = r["verdict"]
        cls = ("b-pos" if verdict.upper() in ("BUY", "BEATS ESTIMATES")
               else "b-neg" if verdict.upper() in ("SELL", "MISSES ESTIMATES")
               else "b-na")
        badge_title = ("Filed outside 09:00-15:30 IST, so it was not analysed — Screener is the source here"
                       if not r["analysed"] else "Verdict from our AI analysis")

        # The Screener read stands on its own: it is the only signal available
        # for a filing that arrived outside market hours and was never analysed.
        sig = r["screener_signal"]
        sig_cls = "b-pos" if sig["tone"] == "pos" else "b-neg" if sig["tone"] == "neg" else "b-na"

        body = []
        for c in r["comparison"]:
            if c["match"] is True:
                verdict_cell = '<span class="ok">match</span>'
            elif c["match"] is False:
                verdict_cell = f'<span class="bad">{c["diff_pct"]:+.1f}%</span>'
            else:
                verdict_cell = '<span class="unk">—</span>'
            ai_txt = c["ai_text"]
            if not r["analysed"]:
                ai_disp = '<span class="na">not analysed</span>'
            elif not ai_txt or ai_txt.upper() == "NA":
                ai_disp = '<span class="na">NA</span>'
            else:
                ai_disp = e(str(ai_txt))
            body.append(
                f"<tr><td>{e(c['label'])}</td>"
                f"<td>{ai_disp}</td>"
                f"<td>{_fmt(c['actual'])}</td>"
                f"<td>{verdict_cell}</td>"
                f"<td>{e(str(c['ai_yoy']))}</td>"
                f"<td>{('%.1f%%' % c['actual_yoy']) if c['actual_yoy'] is not None else '<span class=na>NA</span>'}</td>"
                f"<td>{('%.1f%%' % c['actual_qoq']) if c.get('actual_qoq') is not None else '<span class=na>NA</span>'}</td>"
                f"</tr>"
            )

        src = (f'<a href="{e(sc["source_url"])}" target="_blank">screener.in</a> · '
               f'quarter {e(sc.get("quarter") or "?")}' if sc.get("ok")
               else f'<span class="na">screener.in unavailable — {e(sc.get("error") or "no data")}</span>')

        cards.append(f"""
<div class="card">
  <div class="hd">
    <span class="sym">{e(p.symbol)}</span>
    <span class="co">{e(p.company_name or '')}</span>
    <span class="badge {cls}" title="{e(badge_title)}">{e(verdict)}</span>
    <span class="badge {sig_cls}" title="{e('Mechanical read of Screener year-on-year figures: ' + sig['reason'])}">
      SCREENER {e(sig['label'])}
    </span>
    <span class="ref">{e(p.tracking_ref or '')}</span>
  </div>
  <div class="title">{e((p.title or '')[:190])}</div>
  <div class="times">
    <span><b>Announced</b> {_ist_str(p.event_time)}</span>
    <span><b>Loaded</b> {_ist_str(p.created_at)}</span>
    <span><b>AI sent</b> {_ist_str(p.ai_requested_at)}</span>
    <span><b>AI received</b> {_ist_str(p.ai_completed_at)}</span>
    <span><b>Window</b> {'intraday' if not p.deferred else 'outside 09:00-15:30'}</span>
    <span><b>Exchange</b> {e(p.exchange.upper())}</span>
  </div>
  <table>
    <thead><tr>
      <th></th><th>Our AI</th><th>Screener (₹ Cr)</th><th>Variance</th>
      <th>AI YoY</th><th title="Against the same quarter last year">Screener YoY</th>
      <th title="Against the previous quarter — sequential momentum">Screener QoQ</th>
    </tr></thead>
    <tbody>{''.join(body)}</tbody>
  </table>
  <div class="src">{src} · <b>Screener read</b> {e(sig['reason'])}</div>
</div>""")

    content = "".join(cards) if cards else '<div class="empty">No results were filed after yesterday\'s cutoff.</div>'

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Results digest — {e(for_date)}</title>
<style>{_CSS}</style></head>
<body>
<h1>Results announced {e(for_date)}</h1>
<div class="sub">{len(rows)} compan{'y' if len(rows) == 1 else 'ies'} ·
 {sum(1 for r in rows if r['analysed'])} analysed intraday ·
 {sum(1 for r in rows if not r['analysed'])} filed outside market hours (Screener only)</div>
<div class="legend">
  <span><b style="color:var(--pos)">match</b> within 2% of Screener</span>
  <span><b style="color:var(--neg)">±n%</b> our figure differs by that much</span>
  <span><b style="color:var(--na)">—</b> one side missing, so no comparison is possible</span>
  <span><b style="color:var(--na)">not analysed</b> filed outside 09:00-15:30; no AI is run on those</span>
  <span>All Screener values in ₹ crore, consolidated where available</span>
</div>
{content}
</body></html>"""


def serialize_digest_rows(rows: List[dict]) -> List[dict]:
    """
    Digest rows as plain JSON, for the dashboard panel and the saved file.

    One shape, used by the page, the PDF and the API, so the three views cannot
    drift apart.
    """
    out = []
    for r in rows:
        p, log = r["pending"], r.get("log")
        out.append({
            "symbol": p.symbol,
            "company_name": p.company_name,
            "exchange": p.exchange,
            "tracking_ref": p.tracking_ref,
            "announced_at": p.event_time.isoformat() if p.event_time else None,
            "ingested_at": p.created_at.isoformat() if p.created_at else None,
            "ai_requested_at": p.ai_requested_at.isoformat() if p.ai_requested_at else None,
            "ai_completed_at": p.ai_completed_at.isoformat() if p.ai_completed_at else None,
            "deferred": bool(p.deferred),
            "analysed": r["analysed"],
            "verdict": r["verdict"],
            "screener_signal": r.get("screener_signal"),
            "all_positive": bool(r.get("all_positive")),
            "title": p.title,
            "attachment_url": p.attachment_url,
            "screener": {
                "ok": r["screener"].get("ok"),
                "quarter": r["screener"].get("quarter"),
                "prev_quarter": r["screener"].get("prev_quarter"),
                "source_url": r["screener"].get("source_url"),
                "error": r["screener"].get("error"),
            },
            "comparison": r["comparison"],
            "validation": r.get("validation"),
            "ai_summary": log.ai_summary if log else None,
            "future_growth_outlook": log.future_growth_outlook if log else None,
            "future_projected_numbers": log.future_projected_numbers if log else None,
            "broker_estimates": log.broker_estimates if log else None,
        })
    return out


def build_and_publish(db: Session, for_date: Optional[str] = None) -> dict:
    """
    Build the digest page and return {path, url_path, count, date}.

    Writes a dated file so an earlier morning can still be opened later.
    """
    now_ist = datetime.now(IST)
    for_date = for_date or now_ist.strftime("%Y-%m-%d")

    pendings = collect_deferred(db)
    rows = _build_rows(db, pendings)

    os.makedirs(_DIGEST_DIR, exist_ok=True)
    filename = f"digest_{for_date}.html"
    path = os.path.join(_DIGEST_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(render_html(rows, for_date))

    # The PDF is what actually reaches Telegram; the page is for the browser.
    pdf_path = os.path.join(_DIGEST_DIR, f"digest_{for_date}.pdf")
    try:
        from app.services.digest_pdf import build_digest_pdf
        build_digest_pdf(rows, for_date, pdf_path)
    except Exception as e:
        logger.error(f"[DIGEST] PDF generation failed: {e}")
        pdf_path = None

    # The dashboard panel reads this rather than rebuilding: assembling it walks
    # Screener once per company, which at the pacing Screener requires is many
    # minutes for a heavy day — long enough that the panel gave up and reported
    # the day as empty.
    try:
        with open(os.path.join(_DIGEST_DIR, f"digest_{for_date}.json"), "w", encoding="utf-8") as f:
            json.dump({
                "date": for_date,
                "total": len(rows),
                "analysed": sum(1 for r in rows if r["analysed"]),
                "companies": serialize_digest_rows(rows),
                "built_at": datetime.utcnow().isoformat(),
            }, f, default=str)
    except Exception as e:
        logger.error(f"[DIGEST] Could not persist JSON for {for_date}: {e}")

    stamp = datetime.utcnow()
    for p in pendings:
        p.digest_sent_at = stamp
    try:
        db.commit()
    except Exception:
        db.rollback()

    logger.info(f"[DIGEST] Published {len(rows)} companies to {path}")
    return {
        "path": path,
        "pdf_path": pdf_path,
        "url_path": f"/api/trading/digest/{for_date}",
        "count": len(rows),
        "date": for_date,
        "rows": rows,
    }


def send_digest_alert(result: dict, public_base_url: str = "") -> bool:
    """Single Telegram alert summarising the digest and linking to the page."""
    from app.services.telegram_notifier import send_message, _esc

    rows = result.get("rows", [])
    if not rows:
        return send_message(
            f"<b>📋 MORNING RESULTS DIGEST — {_esc(result['date'])}</b>\n\n"
            f"No results were announced."
        ) is not None

    analysed = [r for r in rows if r.get("analysed")]
    unanalysed = [r for r in rows if not r.get("analysed")]
    beats = sum(1 for r in analysed if "BEAT" in (r["verdict"] or "").upper())
    misses = sum(1 for r in analysed if "MISS" in (r["verdict"] or "").upper())
    na = sum(1 for r in analysed if (r["verdict"] or "NA").upper() == "NA")
    # Only an analysed filing can disagree with Screener; the rest have nothing
    # of ours to compare against.
    mismatches = sum(
        1 for r in analysed if any(c["match"] is False for c in r["comparison"])
    )

    lines = []
    for r in rows[:25]:
        p = r["pending"]
        flag = ""
        if any(c["match"] is False for c in r["comparison"]):
            flag = " ⚠"
        mark = "" if r.get("analysed") else " ·"
        lines.append(
            f"• <b>{_esc(p.symbol)}</b> — {_esc(r['verdict'])}{flag}{mark}  "
            f"<code>{_esc(p.tracking_ref or '')}</code>"
        )
    if len(rows) > 25:
        lines.append(f"…and {len(rows) - 25} more on the page.")

    link = ""
    if public_base_url:
        link = f"\n\n🔗 <a href=\"{public_base_url.rstrip('/')}{result['url_path']}\">Open full comparison</a>"

    message = (
        f"<b>📋 MORNING RESULTS DIGEST — {_esc(result['date'])}</b>\n\n"
        f"<b>{len(rows)}</b> compan{'y' if len(rows) == 1 else 'ies'} filed after yesterday's cutoff.\n"
        f"Beats <b>{beats}</b> · Misses <b>{misses}</b> · No call <b>{na}</b>\n"
        + (f"⚠ <b>{mismatches}</b> with figures that disagree with Screener.\n" if mismatches else "")
        + "\n" + "\n".join(lines) + link
    )
    # Send the PDF first: it carries every company in full, where a text message
    # would be cut off partway through the list.
    pdf_path = result.get("pdf_path")
    if pdf_path and os.path.exists(pdf_path):
        from app.services.telegram_notifier import send_document
        caption = (
            f"<b>📋 Results digest — {_esc(result['date'])}</b>\n"
            f"{len(rows)} compan{'y' if len(rows) == 1 else 'ies'} · "
            f"{len(analysed)} analysed intraday · {len(unanalysed)} after cutoff"
        )
        if send_document(pdf_path, caption=caption,
                         filename=f"results-digest-{result['date']}.pdf"):
            # The summary still follows, for the at-a-glance counts and refs.
            send_message(message)
            return True
        logger.warning("[DIGEST] PDF send failed; falling back to a text summary.")

    return send_message(message) is not None


def run_morning_digest(db: Session, public_base_url: str = "") -> dict:
    """Publish the page and send the alert. Called by the 08:00 IST job."""
    result = build_and_publish(db)
    try:
        send_digest_alert(result, public_base_url)
    except Exception as e:
        logger.error(f"[DIGEST] Telegram alert failed: {e}")
    return result
