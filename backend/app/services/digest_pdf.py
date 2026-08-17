"""
Render the morning digest as a PDF.

Telegram truncates long messages, so a digest covering thirty companies cannot
be delivered as text. A PDF renders inline in the Telegram client and carries
the whole thing: per company the symbol, registered name, when the result was
announced, Screener.in's figures for the quarter and our AI's extraction beside
them.

reportlab is used deliberately — it is pure Python, so the slim container needs
no extra system libraries.
"""
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

logger = logging.getLogger("app.digest_pdf")

IST = timezone(timedelta(hours=5, minutes=30))

# Print palette: dark on white, unlike the on-screen digest. A dark-background
# PDF is unreadable once printed and heavy in a Telegram preview.
_INK = colors.HexColor("#1a1f29")
_MUTED = colors.HexColor("#5a6475")
_LINE = colors.HexColor("#d4d9e2")
_POS = colors.HexColor("#1a7f4f")
_NEG = colors.HexColor("#c23934")
_NA = colors.HexColor("#8b95a6")
_HEAD_BG = colors.HexColor("#eef1f6")


# reportlab's built-in Helvetica has no rupee glyph, so ₹ renders as a hollow
# box. Embedding a Unicode TTF would mean shipping a font file with the image;
# substituting the ASCII form keeps the PDF dependency-free and readable.
_GLYPH_SUBS = {
    "₹": "Rs ",   # ₹
    "‑": "-",     # non-breaking hyphen
    "−": "-",     # unicode minus
}


def _safe(text) -> str:
    """Replace glyphs the base PDF fonts cannot draw."""
    if text is None:
        return ""
    out = str(text)
    for bad, good in _GLYPH_SUBS.items():
        out = out.replace(bad, good)
    return out


def _ist(dt) -> str:
    if not dt:
        return "-"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).strftime("%d %b %Y, %H:%M:%S")


def _num(v: Optional[float]) -> str:
    return "-" if v is None else f"{v:,.2f}"


def _styles():
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("t", parent=base["Title"], fontSize=16, textColor=_INK,
                                spaceAfter=2, alignment=TA_LEFT),
        "sub": ParagraphStyle("s", parent=base["Normal"], fontSize=8.5, textColor=_MUTED,
                              spaceAfter=10),
        "sym": ParagraphStyle("sy", parent=base["Normal"], fontSize=12, textColor=_INK,
                              fontName="Helvetica-Bold", spaceAfter=0),
        "co": ParagraphStyle("c", parent=base["Normal"], fontSize=9, textColor=_MUTED,
                             spaceAfter=1),
        "meta": ParagraphStyle("m", parent=base["Normal"], fontSize=7.5, textColor=_MUTED,
                               spaceAfter=1, leading=10),
        "body": ParagraphStyle("b", parent=base["Normal"], fontSize=8, textColor=_INK,
                               spaceAfter=2, leading=11),
        "small": ParagraphStyle("sm", parent=base["Normal"], fontSize=7, textColor=_MUTED,
                                leading=9),
    }


def _company_block(r: dict, st: dict) -> list:
    """One company: header, timings, the comparison grid, then the AI narrative."""
    p = r["pending"]
    log = r.get("log")
    analysed = r.get("analysed")
    verdict = r.get("verdict") or "NA"

    flow = []
    flow.append(Paragraph(_safe(p.symbol), st["sym"]))
    if p.company_name:
        flow.append(Paragraph(_safe(p.company_name), st["co"]))

    vcolour = ("#1a7f4f" if verdict.upper() in ("BUY", "BEATS ESTIMATES")
               else "#c23934" if verdict.upper() in ("SELL", "MISSES ESTIMATES")
               else "#8b95a6")
    flow.append(Paragraph(
        f'<b>Verdict:</b> <font color="{vcolour}"><b>{_safe(verdict)}</b></font>'
        f'&nbsp;&nbsp;<font color="#8b95a6">{_safe(p.tracking_ref or "")}</font>',
        st["meta"]))
    flow.append(Paragraph(
        f"<b>Result announced:</b> {_ist(p.event_time)} &nbsp;·&nbsp; "
        f"<b>Exchange:</b> {(p.exchange or '').upper()} &nbsp;·&nbsp; "
        f"<b>Window:</b> {'outside 09:00-15:30' if p.deferred else 'intraday'}",
        st["meta"]))

    # For a filing that arrived outside market hours this is the only signal on
    # the page — the AI never ran on it.
    sig = r.get("screener_signal") or {}
    if sig:
        scolour = ("#1a7f4f" if sig.get("tone") == "pos"
                   else "#c23934" if sig.get("tone") == "neg" else "#8b95a6")
        flow.append(Paragraph(
            f'<b>Screener read:</b> <font color="{scolour}"><b>{_safe(sig.get("label", "NA"))}</b></font>'
            f'&nbsp;&nbsp;<font color="#8b95a6">{_safe(sig.get("reason", ""))}</font>',
            st["meta"]))
    if analysed:
        flow.append(Paragraph(
            f"<b>AI sent:</b> {_ist(p.ai_requested_at)} &nbsp;·&nbsp; "
            f"<b>AI received:</b> {_ist(p.ai_completed_at)}", st["meta"]))
    flow.append(Paragraph(f"<i>{_safe((p.title or '')[:170])}</i>", st["small"]))
    flow.append(Spacer(1, 4))

    sc = r.get("screener") or {}
    head = ["", "Our AI", f"Screener ({sc.get('quarter') or 'latest qtr'})", "Variance",
            "AI YoY", "Scr YoY", "Scr QoQ"]
    data = [head]
    styles = [
        ("BACKGROUND", (0, 0), (-1, 0), _HEAD_BG),
        ("TEXTCOLOR", (0, 0), (-1, 0), _MUTED),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 7.5),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.4, _LINE),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("FONTNAME", (0, 1), (0, -1), "Helvetica-Bold"),
    ]

    for i, c in enumerate(r.get("comparison", []), start=1):
        ai_txt = c.get("ai_text")
        if not analysed:
            ai_cell = "not analysed"
        elif not ai_txt or str(ai_txt).upper() == "NA":
            ai_cell = "NA"
        else:
            ai_cell = _safe(ai_txt)

        if c.get("match") is True:
            var, vcol = "match", _POS
        elif c.get("match") is False:
            var, vcol = f"{c['diff_pct']:+.1f}%", _NEG
        else:
            var, vcol = "-", _NA

        data.append([
            _safe(c["label"]), ai_cell, _num(c.get("actual")), var,
            _safe(c.get("ai_yoy") or "NA") if analysed else "-",
            f"{c['actual_yoy']:.1f}%" if c.get("actual_yoy") is not None else "-",
            f"{c['actual_qoq']:.1f}%" if c.get("actual_qoq") is not None else "-",
        ])
        styles.append(("TEXTCOLOR", (3, i), (3, i), vcol))
        if ai_cell in ("not analysed", "NA"):
            styles.append(("TEXTCOLOR", (1, i), (1, i), _NA))

    table = Table(data, colWidths=[24 * mm, 31 * mm, 31 * mm, 18 * mm, 19 * mm, 19 * mm, 19 * mm])
    table.setStyle(TableStyle(styles))
    flow.append(table)

    if sc.get("source_url"):
        flow.append(Paragraph(f'Screener: {sc["source_url"]}', st["small"]))
    elif sc.get("error"):
        flow.append(Paragraph(f'Screener unavailable — {sc["error"]}', st["small"]))

    # The AI narrative in full: this is what Telegram was truncating.
    if analysed and log:
        flow.append(Spacer(1, 3))
        if log.ai_summary:
            flow.append(Paragraph(f"<b>Analysis:</b> {_safe(log.ai_summary)}", st["body"]))
        if log.future_growth_outlook and log.future_growth_outlook != "NA":
            flow.append(Paragraph(f"<b>Outlook:</b> {_safe(log.future_growth_outlook)}", st["body"]))
        if log.future_projected_numbers and log.future_projected_numbers != "NA":
            flow.append(Paragraph(f"<b>Projected:</b> {_safe(log.future_projected_numbers)}", st["body"]))
        if log.broker_estimates and log.broker_estimates not in ("NA", "N/A"):
            flow.append(Paragraph(f"<b>Broker estimates:</b> {_safe(log.broker_estimates)}", st["body"]))
        val = r.get("validation")
        if val and val.get("issues"):
            flow.append(Paragraph(
                "<b><font color='#c23934'>Figures failed consistency checks:</font></b> "
                + _safe("; ".join(val["issues"][:3])), st["small"]))
    elif not analysed:
        flow.append(Spacer(1, 3))
        flow.append(Paragraph(
            "Filed after the 15:25 cutoff, so no AI analysis was run. "
            "Screener's figures above are the source for this company.", st["small"]))

    flow.append(Spacer(1, 10))
    return flow


def build_digest_pdf(rows: List[dict], for_date: str, out_path: str) -> str:
    """Write the digest PDF and return its path."""
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    st = _styles()

    doc = SimpleDocTemplate(
        out_path, pagesize=A4,
        leftMargin=14 * mm, rightMargin=14 * mm,
        topMargin=14 * mm, bottomMargin=14 * mm,
        title=f"Results digest {for_date}", author="Stocks Platform",
    )

    analysed = sum(1 for r in rows if r.get("analysed"))
    story = [
        Paragraph(f"Results announced {for_date}", st["title"]),
        Paragraph(
            f"{len(rows)} compan{'y' if len(rows) == 1 else 'ies'} · "
            f"{analysed} analysed intraday · {len(rows) - analysed} filed after the 15:25 cutoff "
            f"(Screener figures only) · all values Rs crore, consolidated where available",
            st["sub"]),
    ]

    if not rows:
        story.append(Paragraph("No results were announced.", st["body"]))
    for i, r in enumerate(rows):
        # Keep a company on one page where it fits, so a table never splits from
        # the symbol it belongs to.
        story.append(KeepTogether(_company_block(r, st)))
        if (i + 1) % 3 == 0 and i + 1 < len(rows):
            story.append(PageBreak())

    def _footer(canvas, doc_):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(_MUTED)
        canvas.drawString(14 * mm, 8 * mm, f"Results digest — {for_date}")
        canvas.drawRightString(A4[0] - 14 * mm, 8 * mm, f"Page {doc_.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    logger.info(f"[DIGEST PDF] wrote {out_path} ({len(rows)} companies)")
    return out_path
