"""
What the earnings AI is given to read: the pages that matter, as text.

Sending the whole filing PDF went wrong three ways on 15 Oct 2026:

  - OpenRouter fetching it by URL: BSE answered 403, so the model had nothing.
  - Sending the file itself: "Failed to parse the file: The file exceeds the
    maximum size supported" on INDIANB. Outcome PDFs bundle the results with
    reports, presentations and press cuttings; Elecon's ran to 11 MB.
  - A free model reading Nestle's 15-page PDF through OpenRouter's text
    parser came back with revenue of ₹4,520 Cr against an actual ₹6,788 Cr.
    The parser glues table columns together ("65,147.460,730.554,110.2" for
    three figures), and the PDF carries hidden template text for a
    different quarter ("Quarter Ended 30th June 2024").

So the filing is read here with PyMuPDF, which the extractor already depends
on. Each page is scored the way the extractor finds its statement page, and
the press release is picked out alongside. Only those pages are sent, as
text rebuilt row by row from word coordinates, so a table row reads
"Revenue from Operations | 67,883.4 | 63,781.8 | 56,436.1". That is small,
column-faithful, and any text model can read it.

A scanned filing has no text layer, so it is sent as a trimmed PDF instead:
the chosen pages only, which a PDF-reading model can OCR natively.
"""
import logging
import re
from typing import Optional

logger = logging.getLogger("app.filing_text")

MAX_PAGES = 8
MAX_CHARS = 60000
MIN_TEXT_CHARS = 1500       # less than this on the chosen pages: treat as scanned

# A formatted figure: digits with a thousands separator or a decimal point.
# Bare "0"s do not count; one filing carried a hidden template page of zeros.
_NUM = re.compile(r"^\(?-?\d[\d,]*[.,]\d+\)?$")
_PRESS_STRONG = re.compile(r"press release|commenting on the (results|performance)|"
                           r"investor release|earnings release", re.I)
_NOTES = re.compile(r"^\s*notes?\s*[:.]?", re.I)
_SEGMENT = re.compile(r"segment", re.I)
_AUDITOR = re.compile(r"independent auditor|review report|limited review|"
                      r"chartered accountants", re.I)


def _numeric_tokens(words) -> int:
    return sum(1 for w in words if _NUM.match(str(w[4]).strip()))


_CRORE_FIG = re.compile(r"(?:INR|Rs\.?|₹)\s*([\d,]+(?:\.\d+)?)\s*(?:crores?|cr\b)", re.I)
_SCALES = ((1, "crore"), (10, "million"), (100, "lakh"), (10000, "thousand"))


def _num(s: str):
    try:
        return float(s.strip("()").replace(",", ""))
    except ValueError:
        return None


def infer_unit(statement_words, all_text: str) -> str:
    """
    The statement's unit, worked out from the press release when the heading
    is unreadable. Nestle's statement is in ₹ million but its "(₹ in million)"
    heading does not survive text extraction. Its press release says "INR
    6,782.2 crore", and 67,821.7 on the statement is that figure x10. Each
    "₹ X crore" in the text is matched against the statement's figures at
    every plausible scale, and the scale that matches most often wins.
    """
    figs = [v for v in (_num(str(w[4])) for w in statement_words
                        if _NUM.match(str(w[4]).strip())) if v and v > 0]
    if not figs:
        return ""
    votes = {}
    for m in _CRORE_FIG.finditer(all_text):
        cr = _num(m.group(1))
        if not cr or cr < 1:
            continue
        for scale, name in _SCALES:
            target = cr * scale
            if any(abs(f - target) <= max(0.06 * scale, target * 0.0005) for f in figs):
                votes[name] = votes.get(name, 0) + 1
    return max(votes.items(), key=lambda kv: kv[1])[0] if votes else ""


def _rows_text(words) -> str:
    """Words back into table rows: one line per row, cells separated by ' | '."""
    try:
        from app.services.pdf_extract.quarterly_extract import group_lines
        lines = group_lines(words)
    except Exception:
        lines = []
        for w in sorted(words, key=lambda w: (round(w[1]), w[0])):
            if lines and abs(lines[-1][0][1] - w[1]) <= 3:
                lines[-1].append(w)
            else:
                lines.append([w])
    out = []
    for line in lines:
        cells, prev_x1 = [], None
        for w in sorted(line, key=lambda t: t[0]):
            txt = str(w[4])
            # A wide gap is a column boundary; a narrow one is a space in a label.
            if prev_x1 is not None and w[0] - prev_x1 > 12:
                cells.append(" | ")
            elif cells:
                cells.append(" ")
            cells.append(txt)
            prev_x1 = w[2]
        out.append("".join(cells).strip())
    return "\n".join(l for l in out if l)


def _choose(pages: list) -> list:
    """Indexes of the pages worth sending, in document order."""
    from app.services.pdf_extract.quarterly_extract import page_score

    scored = []
    for p in pages:
        s = page_score(p["text"])
        # The statement is the most table-like page; a page whose "statement"
        # wording comes with no figures is template residue, not a result.
        if p["nums"] < 40:
            s -= 5
        if _AUDITOR.search(p["text"][:1500]):
            s -= 4
        scored.append((s, p["i"]))
    statements = [i for s, i in sorted(scored, reverse=True) if s >= 6][:4]
    by_i = {p["i"]: p for p in pages}
    # The notes that follow a statement explain its one-offs and exceptional items.
    notes = [p["i"] for p in pages if _NOTES.match(p["text"][:40] or "")
             and p["i"] not in statements and not _AUDITOR.search(p["text"][:1500])][:2]
    # The press release, and the pages that continue it (management commentary,
    # segment and volume growth, margins).
    press = []
    for p in pages:
        if _PRESS_STRONG.search(p["text"][:600]):
            press.append(p["i"])
            for j in (p["i"] + 1, p["i"] + 2):
                q = by_i.get(j)
                if q and q["nums"] < 40 and not _AUDITOR.search(q["text"][:1500]):
                    press.append(j)
    press = [i for i in dict.fromkeys(press) if i not in statements][:3]
    segment = [p["i"] for p in pages if _SEGMENT.search(p["text"]) and p["nums"] >= 30
               and p["i"] not in statements + press + notes][:1]
    chosen = sorted(set(statements + notes + press + segment))[:MAX_PAGES]
    if not chosen:
        chosen = list(range(min(len(pages), MAX_PAGES)))
    return chosen


def prepare_for_ai(pdf_bytes: Optional[bytes]) -> dict:
    """
    {"mode": "text", "text": ..., "pages": [...]}           text layer found
    {"mode": "pdf",  "pdf": <trimmed bytes>, "pages": [...]} scanned
    {"mode": "none", "reason": ...}                          nothing usable
    """
    if not pdf_bytes:
        return {"mode": "none", "reason": "no PDF"}
    try:
        import fitz
    except Exception as e:
        return {"mode": "pdf", "pdf": pdf_bytes, "pages": [], "reason": f"PyMuPDF unavailable: {e}"}
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:
        return {"mode": "none", "reason": f"PDF could not be opened: {e}"}

    try:
        pages = []
        for i, page in enumerate(doc):
            words = page.get_text("words")
            pages.append({"i": i, "words": words, "text": page.get_text(),
                          "nums": _numeric_tokens(words)})
        chosen = _choose(pages)

        parts = []
        for i in chosen:
            body = _rows_text(pages[i]["words"])
            if body.strip():
                parts.append(f"===== PAGE {i + 1} of {len(pages)} =====\n{body}")
        text = "\n\n".join(parts)
        # The unit line is often a lone "(₹ in million)" in a corner, and a
        # glyph like ₹ does not always survive extraction. The extractor's own
        # unit detection reads it more reliably, so it is stated up front: a
        # unit misread is a 10x or 100x error in every figure.
        try:
            from app.services.pdf_extract.quarterly_extract import detect_unit
            stmt = [i for i in chosen if pages[i]["nums"] >= 40] or chosen
            _, unit = detect_unit(pages[stmt[0]]["text"], "\n".join(p["text"] for p in pages))
            if "assumed" not in unit:
                unit_line = f"UNIT STATED ON THE STATEMENT: Rs. {unit}"
            else:
                inferred = infer_unit([w for i in stmt for w in pages[i]["words"]],
                                      "\n".join(p["text"] for p in pages))
                unit_line = (f"UNIT OF THE STATEMENT: Rs. {inferred} (the heading is unreadable; "
                             f"worked out from the crore figures in the press release)"
                             if inferred else
                             "UNIT: not found in the text; read it from the statement heading")
        except Exception:
            unit_line = ""
        if unit_line:
            text = unit_line + "\n\n" + text
        if len(text) >= MIN_TEXT_CHARS:
            if len(text) > MAX_CHARS:
                text = text[:MAX_CHARS] + "\n[... truncated]"
            return {"mode": "text", "text": text, "pages": [i + 1 for i in chosen],
                    "total_pages": len(pages)}

        # Scanned: send only the chosen pages as a PDF.
        out = fitz.open()
        for i in chosen:
            out.insert_pdf(doc, from_page=i, to_page=i)
        trimmed = out.tobytes(garbage=3, deflate=True)
        out.close()
        return {"mode": "pdf", "pdf": trimmed, "pages": [i + 1 for i in chosen],
                "total_pages": len(pages)}
    except Exception as e:
        logger.warning(f"[FILING TEXT] could not prepare filing: {e}")
        return {"mode": "pdf", "pdf": pdf_bytes, "pages": [], "reason": str(e)}
    finally:
        doc.close()
