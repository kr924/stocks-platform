"""
Morning digest for results filed after the intraday cutoff.

Results land mostly after the close, when there is no session left to trade
into. Those are deferred (see results_router.is_within_action_window) and
collected here instead:

    07:00 IST  run the held analyses — quiet time, market shut, and it gives
               the slow browser-driven AI an hour to work through the queue
    08:00 IST  publish one HTML page and send a single Telegram alert linking
               to it

The page puts our AI's extracted figures beside Screener.in's reported numbers
for the same quarter, with the variance, so every company filed overnight can be
checked on one screen.
"""
import html
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from sqlalchemy.orm import Session

from app.database import PendingResultOrder, TradeAILog
from app.services.screener_quarters import compare, fetch_latest_quarter, parse_ai_value
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


def collect_deferred(db: Session, since_hours: int = 20) -> List[PendingResultOrder]:
    """Results held back since the last cutoff, oldest first."""
    since = datetime.utcnow() - timedelta(hours=since_hours)
    return (
        db.query(PendingResultOrder)
        .filter(
            PendingResultOrder.deferred == True,
            PendingResultOrder.created_at >= since,
            PendingResultOrder.digest_sent_at.is_(None),
        )
        .order_by(PendingResultOrder.event_time.asc())
        .all()
    )


def run_deferred_analyses(db: Session) -> int:
    """
    Work through the overnight queue, one filing at a time.

    Sequential on purpose: the earnings AI downloads a PDF and drives a browser
    session, and running several at once is what previously pinned the CPU.
    """
    from app.services.trade_ai_analyzer import analyze_earnings_disclosure_2step

    pending = [p for p in collect_deferred(db) if p.ai_status in ("deferred", "pending", "failed")]
    if not pending:
        logger.info("[DIGEST] No deferred results awaiting analysis.")
        return 0

    logger.info(f"[DIGEST] Running {min(len(pending), MAX_ANALYSES_PER_RUN)} deferred analyses.")
    done = 0
    for row in pending[:MAX_ANALYSES_PER_RUN]:
        try:
            row.ai_status = "running"
            row.ai_requested_at = datetime.utcnow()
            db.commit()

            analyze_earnings_disclosure_2step(
                symbol=row.symbol,
                title=row.title,
                attachment_url=row.attachment_url or "",
                pdf_text=row.description or "",
                config_id=None,
                tracking_ref=row.tracking_ref,
            )

            latest = (
                db.query(TradeAILog)
                .filter(TradeAILog.symbol == row.symbol)
                .order_by(TradeAILog.created_at.desc())
                .first()
            )
            row.ai_log_id = latest.id if latest else None
            row.ai_status = "done"
            row.ai_completed_at = datetime.utcnow()
            db.commit()
            done += 1
        except Exception as e:
            db.rollback()
            logger.error(f"[DIGEST] Analysis failed for {row.symbol}: {e}")
            try:
                row.ai_status = "failed"
                row.ai_completed_at = datetime.utcnow()
                db.commit()
            except Exception:
                db.rollback()

    logger.info(f"[DIGEST] Completed {done} deferred analyses.")
    return done


def _build_rows(db: Session, pendings: List[PendingResultOrder]) -> List[dict]:
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

        screener = fetch_latest_quarter(p.symbol)

        comparison = []
        for key in METRIC_ROWS:
            ai_cell = (ai_metrics.get(key) or {}).get("current_qtr", "NA")
            ai_num = parse_ai_value(ai_cell)
            actual = (screener.get("metrics", {}).get(key) or {}).get("current_qtr")
            actual_yoy = (screener.get("metrics", {}).get(key) or {}).get("yoy_change_pct")
            comparison.append({
                "label": _ROW_LABELS[key],
                "ai_text": ai_cell,
                "ai_num": ai_num,
                "actual": actual,
                "actual_yoy": actual_yoy,
                "ai_yoy": (ai_metrics.get(key) or {}).get("yoy_change_pct", "NA"),
                **compare(ai_num, actual),
            })

        rows.append({
            "pending": p,
            "log": log,
            "screener": screener,
            "comparison": comparison,
            "verdict": (log.ai_suggestion if log else None) or "NA",
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

        body = []
        for c in r["comparison"]:
            if c["match"] is True:
                verdict_cell = '<span class="ok">match</span>'
            elif c["match"] is False:
                verdict_cell = f'<span class="bad">{c["diff_pct"]:+.1f}%</span>'
            else:
                verdict_cell = '<span class="unk">—</span>'
            ai_txt = c["ai_text"]
            ai_disp = ('<span class="na">NA</span>' if not ai_txt or ai_txt.upper() == "NA"
                       else e(str(ai_txt)))
            body.append(
                f"<tr><td>{e(c['label'])}</td>"
                f"<td>{ai_disp}</td>"
                f"<td>{_fmt(c['actual'])}</td>"
                f"<td>{verdict_cell}</td>"
                f"<td>{e(str(c['ai_yoy']))}</td>"
                f"<td>{('%.1f%%' % c['actual_yoy']) if c['actual_yoy'] is not None else '<span class=na>NA</span>'}</td>"
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
    <span class="badge {cls}">{e(verdict)}</span>
    <span class="ref">{e(p.tracking_ref or '')}</span>
  </div>
  <div class="title">{e((p.title or '')[:190])}</div>
  <div class="times">
    <span><b>Announced</b> {_ist_str(p.event_time)}</span>
    <span><b>Loaded</b> {_ist_str(p.created_at)}</span>
    <span><b>AI sent</b> {_ist_str(p.ai_requested_at)}</span>
    <span><b>AI received</b> {_ist_str(p.ai_completed_at)}</span>
    <span><b>Exchange</b> {e(p.exchange.upper())}</span>
  </div>
  <table>
    <thead><tr>
      <th></th><th>Our AI</th><th>Screener (₹ Cr)</th><th>Variance</th>
      <th>AI YoY</th><th>Screener YoY</th>
    </tr></thead>
    <tbody>{''.join(body)}</tbody>
  </table>
  <div class="src">{src}</div>
</div>""")

    content = "".join(cards) if cards else '<div class="empty">No results were filed after yesterday\'s cutoff.</div>'

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Results digest — {e(for_date)}</title>
<style>{_CSS}</style></head>
<body>
<h1>Results filed after cutoff — {e(for_date)}</h1>
<div class="sub">{len(rows)} compan{'y' if len(rows) == 1 else 'ies'} ·
 our AI's extraction beside Screener.in's reported figures for the same quarter</div>
<div class="legend">
  <span><b style="color:var(--pos)">match</b> within 2% of Screener</span>
  <span><b style="color:var(--neg)">±n%</b> our figure differs by that much</span>
  <span><b style="color:var(--na)">—</b> one side missing, so no comparison is possible</span>
  <span>All Screener values in ₹ crore, consolidated where available</span>
</div>
{content}
</body></html>"""


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
            f"No results were filed after yesterday's 15:20 cutoff."
        ) is not None

    beats = sum(1 for r in rows if "BEAT" in (r["verdict"] or "").upper())
    misses = sum(1 for r in rows if "MISS" in (r["verdict"] or "").upper())
    na = sum(1 for r in rows if (r["verdict"] or "NA").upper() == "NA")
    mismatches = sum(
        1 for r in rows if any(c["match"] is False for c in r["comparison"])
    )

    lines = []
    for r in rows[:25]:
        p = r["pending"]
        flag = ""
        if any(c["match"] is False for c in r["comparison"]):
            flag = " ⚠"
        lines.append(
            f"• <b>{_esc(p.symbol)}</b> — {_esc(r['verdict'])}{flag}  "
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
    return send_message(message) is not None


def run_morning_digest(db: Session, public_base_url: str = "") -> dict:
    """Publish the page and send the alert. Called by the 08:00 IST job."""
    result = build_and_publish(db)
    try:
        send_digest_alert(result, public_base_url)
    except Exception as e:
        logger.error(f"[DIGEST] Telegram alert failed: {e}")
    return result
