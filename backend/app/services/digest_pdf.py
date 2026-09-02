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
        "section": ParagraphStyle("sec", parent=base["Normal"], fontSize=11, textColor=_INK,
                                  fontName="Helvetica-Bold", spaceBefore=6, spaceAfter=2),
        # Table cells wrap only as Paragraphs; a plain string is clipped at the
        # column edge, which silently truncates a subject.
        "cell": ParagraphStyle("cl", parent=base["Normal"], fontSize=7, textColor=_INK,
                               leading=8.5),
        "cell_r": ParagraphStyle("cr", parent=base["Normal"], fontSize=7, textColor=_INK,
                                 leading=8.5, alignment=2),
        "cell_c": ParagraphStyle("cc", parent=base["Normal"], fontSize=7, textColor=_INK,
                                 leading=8.5, alignment=1),
    }


def _price_lines(r: dict, st: dict) -> list:
    """
    The two price figures, and where each came from.

    "Since result" is Upstox only: it is measured against a baseline Upstox
    captured when the filing landed, and mixing venues would report a move no
    single tape ever showed. With no Upstox session it is simply absent, rather
    than recomputed from a source that cannot be compared with the baseline.

    The day's change is public information, so it falls back to a public feed
    once the market has closed or when there is no Upstox session at all. Which
    source answered is printed, because a reader comparing this against their
    terminal deserves to know which tape it came off.
    """
    price = r.get("price") or {}
    if not (price.get("at_announcement") or price.get("last") or price.get("day_pct") is not None):
        return []

    def pct(v):
        if v is None:
            return '<font color="#8b95a6">NA</font>'
        colour = "#1a7f4f" if v > 0 else "#c23934" if v < 0 else "#8b95a6"
        return f'<font color="{colour}"><b>{v:+.2f}%</b></font>'

    since = price.get("since_pct")
    if since is None and not price.get("upstox_connected"):
        since_txt = '<font color="#8b95a6">NA (Upstox not connected)</font>'
    else:
        since_txt = pct(since)

    day_txt = pct(price.get("day_pct"))
    if price.get("day_source"):
        day_txt += f' <font color="#8b95a6" size="6">({price["day_source"]})</font>'

    line = (f"<b>At filing:</b> {_num(price.get('at_announcement'))} &nbsp;·&nbsp; "
            f"<b>Now:</b> {_num(price.get('last'))} &nbsp;·&nbsp; "
            f"<b>Since result:</b> {since_txt} &nbsp;·&nbsp; "
            f"<b>Day:</b> {day_txt}")
    if price.get("paused"):
        line += ' &nbsp;·&nbsp; <font color="#8b95a6">price updates paused</font>'
    return [Paragraph(line, st["meta"])]


def _pct_cell(v, st) -> Paragraph:
    """A percentage, coloured, or a muted NA. Never a bare zero for 'unknown'."""
    if v is None:
        return Paragraph('<font color="#8b95a6">NA</font>', st["cell_r"])
    colour = "#1a7f4f" if v > 0 else "#c23934" if v < 0 else "#5a6475"
    return Paragraph(f'<font color="{colour}">{v:+.2f}%</font>', st["cell_r"])


def _non_result_table(rows: List[dict], st: dict) -> list:
    """
    Every non-result prompt as one table, a row each.

    These carry no quarterly figures, so the per-company block that suits a
    results filing — verdict, Screener read, a five-row comparison grid — spends
    most of a page saying what is absent. What there is to say about an order
    win fits on one line: what happened, when, and what the price did. A dozen
    of them belong in a table you can run an eye down, not a dozen headed
    sections.

    Sorted by announcement time, because on a decision sheet the order things
    arrived in is the order they mattered in.
    """
    if not rows:
        return []

    ordered = sorted(rows, key=lambda r: (r["pending"].event_time or datetime.min))

    head = ["Company", "Type", "Time", "Subject", "At filing", "Now", "Since", "Day"]
    data = [[Paragraph(f'<b>{h}</b>', st["cell_r"] if h in ("At filing", "Now", "Since", "Day")
                       else st["cell"]) for h in head]]

    styles = [
        ("BACKGROUND", (0, 0), (-1, 0), _HEAD_BG),
        ("TEXTCOLOR", (0, 0), (-1, 0), _MUTED),
        ("FONTSIZE", (0, 0), (-1, -1), 7),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.4, _LINE),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]

    sources = {}
    for r in ordered:
        p = r["pending"]
        price = r.get("price") or {}
        if price.get("day_source"):
            sources[price["day_source"]] = sources.get(price["day_source"], 0) + 1

        company = _safe(p.symbol)
        if p.company_name:
            company += f'<br/><font size="6" color="#5a6475">{_safe(p.company_name)}</font>'
        exch = (p.exchange or "").upper()
        kind = _safe((getattr(p, "kind", "") or "").replace("_", " ").title())

        data.append([
            Paragraph(f'<b>{company}</b>', st["cell"]),
            Paragraph(f'{kind}<br/><font size="6" color="#5a6475">{exch}</font>', st["cell"]),
            # HH:MM, not HH:MM:SS. Seconds wrap the column onto two lines for a
            # precision nobody scans a table at; the per-company block for a
            # results filing still prints the full timestamp.
            Paragraph(_ist(p.event_time).split(", ")[-1][:5] if p.event_time else "-",
                      st["cell_c"]),
            # The subject in full, wrapped. It is the only thing on the row that
            # says what actually happened.
            Paragraph(_safe(p.title or ""), st["cell"]),
            Paragraph(_num(price.get("at_announcement")), st["cell_r"]),
            Paragraph(_num(price.get("last")), st["cell_r"]),
            _pct_cell(price.get("since_pct"), st),
            _pct_cell(price.get("day_pct"), st),
        ])
        if price.get("paused"):
            styles.append(("TEXTCOLOR", (0, len(data) - 1), (0, len(data) - 1), _NA))

    # 182mm of usable width between the margins.
    table = Table(
        data, repeatRows=1,
        colWidths=[26 * mm, 17 * mm, 11 * mm, 63 * mm, 15 * mm, 15 * mm, 16 * mm, 19 * mm],
    )
    table.setStyle(TableStyle(styles))

    out = [
        Paragraph(f"Other announcements ({len(ordered)})", st["section"]),
        Paragraph(
            "Order wins, acquisitions, buybacks and the like. No quarterly figures are "
            "published for these, so no Screener comparison or earnings analysis is run.",
            st["small"]),
        Spacer(1, 4),
        table,
    ]
    if sources:
        # Which tape each figure came off. Upstox answers while the session is
        # authorised and the market is open; a public feed answers otherwise,
        # and a reader checking these against a terminal should know which.
        parts = ", ".join(f"{k} ({n})" for k, n in sorted(sources.items()))
        out.append(Spacer(1, 3))
        out.append(Paragraph(
            f"Day change source: {parts}. "
            "&quot;Since&quot; is measured against the price captured from Upstox when the "
            "filing landed, so it reads NA without an Upstox session.", st["small"]))
    return out


def _company_block(r: dict, st: dict) -> list:
    """One company: header, timings, the comparison grid, then the AI narrative."""
    p = r["pending"]
    log = r.get("log")
    analysed = r.get("analysed")
    verdict = r.get("verdict") or "NA"
    kind = (getattr(p, "kind", "result") or "result")
    # The caller decides; falling back to the row's own kind keeps the 08:00
    # digest, which passes no flag and carries only results, rendering as before.
    is_result = r.get("is_result", kind == "result")

    flow = []
    flow.append(Paragraph(_safe(p.symbol), st["sym"]))
    if p.company_name:
        flow.append(Paragraph(_safe(p.company_name), st["co"]))

    if is_result:
        vcolour = ("#1a7f4f" if verdict.upper() in ("BUY", "BEATS ESTIMATES")
                   else "#c23934" if verdict.upper() in ("SELL", "MISSES ESTIMATES")
                   else "#8b95a6")
        flow.append(Paragraph(
            f'<b>Verdict:</b> <font color="{vcolour}"><b>{_safe(verdict)}</b></font>'
            f'&nbsp;&nbsp;<font color="#8b95a6">{_safe(p.tracking_ref or "")}</font>',
            st["meta"]))
    else:
        # No verdict line: nothing was analysed, and printing "NA" beside the
        # word Verdict reads as a judgement rather than as an absence.
        flow.append(Paragraph(
            f'<b>{_safe(kind.replace("_", " ").title())}</b>'
            f'&nbsp;&nbsp;<font color="#8b95a6">{_safe(p.tracking_ref or "")}</font>',
            st["meta"]))

    flow.append(Paragraph(
        f"<b>{'Result announced' if is_result else 'Announced'}:</b> {_ist(p.event_time)} &nbsp;·&nbsp; "
        f"<b>Exchange:</b> {(p.exchange or '').upper()} &nbsp;·&nbsp; "
        f"<b>Window:</b> {'outside 09:00-15:30' if p.deferred else 'intraday'}",
        st["meta"]))

    # For a filing that arrived outside market hours this is the only signal on
    # the page — the AI never ran on it.
    sig = (r.get("screener_signal") or {}) if is_result else {}
    if sig:
        scolour = ("#1a7f4f" if sig.get("tone") == "pos"
                   else "#c23934" if sig.get("tone") == "neg" else "#8b95a6")
        flow.append(Paragraph(
            f'<b>Screener read:</b> <font color="{scolour}"><b>{_safe(sig.get("label", "NA"))}</b></font>'
            f'&nbsp;&nbsp;<font color="#8b95a6">{_safe(sig.get("reason", ""))}</font>',
            st["meta"]))
    # Prices, when the caller supplied them. The 08:00 digest runs before there
    # are any, so this is absent there and present on the order-decision export,
    # where the move since the filing is the column the decision turns on.
    flow.extend(_price_lines(r, st))
    if analysed and is_result:
        flow.append(Paragraph(
            f"<b>AI sent:</b> {_ist(p.ai_requested_at)} &nbsp;·&nbsp; "
            f"<b>AI received:</b> {_ist(p.ai_completed_at)}", st["meta"]))
    # The subject in full. Truncating it is fine beside a table of figures that
    # says what the filing was; on a row with no table it is the only thing
    # describing what actually happened, so it is not cut.
    subject = _safe(p.title or "")
    flow.append(Paragraph(f"<i>{subject if is_result else subject[:600]}</i>", st["small"]))
    flow.append(Spacer(1, 4))

    # Only a quarterly filing has figures. An order win, an acquisition or a
    # buyback has no quarter for Screener to publish and nothing for the
    # extractor to read, so the grid below would be five rows of dashes
    # implying we looked and found nothing — when we never asked.
    if not is_result:
        return flow

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


def build_digest_pdf(rows: List[dict], for_date: str, out_path: str,
                     title: str = "") -> str:
    """
    Write the digest PDF and return its path.

    `title` names the export. The same builder serves the 08:00 digest and the
    order-decision panel, and the two must not both claim to be "results
    announced" — the panel's export is a decision sheet that also carries
    prices, which the morning run cannot have.
    """
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    st = _styles()
    heading = title or f"Results announced {for_date}"

    doc = SimpleDocTemplate(
        out_path, pagesize=A4,
        leftMargin=14 * mm, rightMargin=14 * mm,
        topMargin=14 * mm, bottomMargin=14 * mm,
        title=heading, author="Stocks Platform",
    )

    # Results get a section each; everything else is one table. The two are not
    # the same document: a results filing is read one company at a time against
    # its figures, an order win is scanned in a list.
    results = [r for r in rows
               if r.get("is_result", (getattr(r["pending"], "kind", "result") or "result") == "result")]
    others = [r for r in rows if r not in results]

    analysed = sum(1 for r in results if r.get("analysed"))
    bits = []
    if results:
        bits.append(f"{len(results)} result{'' if len(results) == 1 else 's'} "
                    f"({analysed} analysed intraday, "
                    f"{len(results) - analysed} filed outside 09:00-15:30)")
    if others:
        bits.append(f"{len(others)} other announcement{'' if len(others) == 1 else 's'}")
    if results:
        bits.append("all figures Rs crore, consolidated where available")

    story = [
        Paragraph(heading, st["title"]),
        Paragraph(" · ".join(bits) if bits else "Nothing to report.", st["sub"]),
    ]

    if not rows:
        story.append(Paragraph("No results were announced.", st["body"]))

    for i, r in enumerate(results):
        # Keep a company on one page where it fits, so a table never splits from
        # the symbol it belongs to.
        story.append(KeepTogether(_company_block(r, st)))
        if (i + 1) % 3 == 0 and i + 1 < len(results):
            story.append(PageBreak())

    if others:
        if results:
            story.append(PageBreak())
        story.extend(_non_result_table(others, st))

    def _footer(canvas, doc_):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(_MUTED)
        # The heading, not a fixed string: the same builder produces the 08:00
        # digest and the order-decision export, and a footer claiming "Results
        # digest" on a page titled "Order decisions" is the kind of mismatch
        # that gets a printout filed under the wrong thing.
        canvas.drawString(14 * mm, 8 * mm, f"{heading} — {for_date}"
                          if for_date not in heading else heading)
        canvas.drawRightString(A4[0] - 14 * mm, 8 * mm, f"Page {doc_.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    logger.info(f"[DIGEST PDF] wrote {out_path} ({len(rows)} companies)")
    return out_path
