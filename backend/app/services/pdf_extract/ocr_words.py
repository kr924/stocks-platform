#!/usr/bin/env python3
"""
Path B: recover a page's words by reading its PIXELS instead of its text layer.

Some filings carry a text layer that disagrees with what is printed. AFFLE is
the reference case: the page shows "7,243.77" while the embedded text says
"724377" -- comma and decimal simply absent -- and its column headers arrive as
"Marct31," and "December3l,". No PDF library can fix that, because they all
read the same broken layer. Rendering the page and running OCR over it gets the
printed values back.

The output is deliberately shaped exactly like PyMuPDF's page.get_text("words")
-- (x0, y0, x1, y1, text, block, line, word_no) in PDF points -- so the whole
existing pipeline (column clustering, date headers, label matching, identity
checks) runs over OCR'd pages unchanged.

Engines, in preference order:
  RapidOCR   pure onnxruntime, no PaddlePaddle, ~50MB of models, aarch64-friendly
  Tesseract  fallback; needs the system binary, weaker on ruled tables
"""
import re

_ENGINE = None
_KIND = None


import os

# Preference order. Tesseract is first: on these filings it is ~6x faster than
# RapidOCR and keeps inter-word spaces, which RapidOCR drops ("Revenue
# fromoperations"). Set OCR_ENGINE=rapidocr to override.
PREFER = os.environ.get('OCR_ENGINE', 'tesseract')
TESS_PATHS = [os.path.join('C:' + os.sep, 'Program Files', 'Tesseract-OCR',
                           'tesseract.exe'),
              os.path.join('C:' + os.sep, 'Program Files (x86)', 'Tesseract-OCR',
                           'tesseract.exe'),
              '/usr/bin/tesseract', '/usr/local/bin/tesseract']


def _load_tesseract():
    try:
        import pytesseract
        for c in TESS_PATHS:
            if os.path.exists(c):
                pytesseract.pytesseract.tesseract_cmd = c
                break
        pytesseract.get_tesseract_version()
        return pytesseract, 'tesseract'
    except Exception:
        return None


def _load_rapid():
    try:
        from rapidocr_onnxruntime import RapidOCR
        return RapidOCR(), 'rapidocr'
    except Exception:
        return None


_LOADERS = {'tesseract': _load_tesseract, 'rapidocr': _load_rapid}
_CACHE = {}
# Cheapest first. Tesseract runs ~5x faster and keeps inter-word spaces;
# RapidOCR is slower but reads these ruled tables more reliably, so it is
# worth paying for only when Tesseract's read does not hold up.
CASCADE = ['tesseract', 'rapidocr']


def get_engine(name):
    """Load one named engine, or None if unavailable."""
    if name not in _CACHE:
        _CACHE[name] = _LOADERS[name]() if name in _LOADERS else None
    return _CACHE[name]


def available():
    """Engine names in cascade order that are actually installed."""
    return [n for n in CASCADE if get_engine(n)]


def engine():
    """Backwards-compatible single-engine accessor."""
    order = CASCADE if PREFER != 'rapidocr' else ['rapidocr', 'tesseract']
    for n in order:
        got = get_engine(n)
        if got:
            return got
    return False, None


MON = r'(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*'
DATE_RUN = re.compile(
    rf'\d{{1,2}}\s*{MON}\s*\d{{2,4}}|{MON}\s*\d{{1,2}},?\s*\d{{4}}'
    rf'|\d{{1,2}}[./-]\d{{1,2}}[./-]\d{{2,4}}', re.I)


def _by_char(text, x0, x1, spans):
    """Cut a box at character offsets, apportioning x linearly."""
    n = max(len(text), 1)
    span = x1 - x0
    return [(text[a:b], x0 + span * a / n, x0 + span * b / n) for a, b in spans]


def _split(text, x0, x1):
    """Split a box into words, apportioning x by character position.

    Two OCR behaviours are handled. Detection sometimes fuses adjacent header
    cells into one box -- "31December2025|31March202531March2026" -- which
    leaves the columns undatable; those are cut apart at date boundaries.
    Numeric cells come back as their own box, so the exact right edge that
    column clustering depends on is preserved.
    """
    text = text.strip()
    ms = list(DATE_RUN.finditer(text))
    if len(ms) >= 2:
        return [(t, a, b) for t, a, b in
                _by_char(text, x0, x1, [(m.start(), m.end()) for m in ms]) if t.strip()]
    parts = [p for p in re.split(r'[\s|]+', text) if p]
    if len(parts) <= 1:
        return [(text, x0, x1)]
    total = sum(len(p) for p in parts)
    out, cur = [], x0
    span = x1 - x0
    for p in parts:
        w = span * len(p) / total
        out.append((p, cur, cur + w))
        cur += w
    return out


STATEMENT_CUE = re.compile(
    r'revenue\s*from\s*operation|total\s*income|profit\s*before\s*tax'
    r'|total\s*expens|income\s*from\s*operation', re.I)


# A results statement is mostly numbers; the prose around it is not. Measured
# on a scanned filing whose statement pages carried ~100 numeric tokens against
# 8-19 on the auditor's report, the notes and the press release, this separates
# them far more sharply than the cue words do.
_NUMERIC_TOKEN = re.compile(r'^\(?[-+]?[\d,]*\.?\d+\)?[*#]?$')
# Below this a page is prose that happens to quote figures, not a table.
MIN_TABLE_NUMBERS = 30


def _basis_of(text):
    """Consolidated / Standalone / Unknown, from the heading."""
    t = text.lower()
    head = t[:1500]
    c, s = head.count('consolidated'), head.count('standalone')
    if c > s:
        return 'Consolidated'
    if s > c:
        return 'Standalone'
    c, s = t.count('consolidated'), t.count('standalone')
    return 'Consolidated' if c > s else ('Standalone' if s else 'Unknown')


_BASIS_RANK = {'Consolidated': 2, 'Unknown': 1, 'Standalone': 0}


def find_statement_page(doc, max_pages=12, dpi=150, min_hits=2, engine_name=None):
    """Locate the results table in a PDF whose text layer cannot be scored.

    Image-only filings never reach page selection, because scoring reads the
    text layer and there isn't one.

    Three things this gets wrong if done naively, all found by measurement on a
    scanned board-meeting bundle:

    1. dpi=100 is too coarse for these scans. The real statement page scored
       *zero* cue hits at 100 and six at 150 -- its heading simply was not
       legible -- so discovery skipped it and settled on a prose page whose
       larger type survived. 150 is no slower here: fewer garbled tokens to
       post-process.

    2. Stopping at the first page that clears the cue threshold picks the
       auditor's review report or the notes, because both are written in the
       same vocabulary as the statement they describe. Every page is scored and
       the most table-like one wins, with the numeric count breaking a tie that
       keywords cannot.

    3. A filing carries the standalone statement and the consolidated one on
       consecutive pages, near-identical in shape. The text-layer path already
       prefers consolidated; this must too, or which one gets read comes down
       to page order. Consolidated is what these figures are compared against.
    """
    if not (get_engine(engine_name) if engine_name else engine()):
        return None
    best = None
    for i in range(min(len(doc), max_pages)):
        try:
            toks = [w[4] for w in
                    words_from_page(doc[i], dpi=dpi, engine_name=engine_name)]
        except Exception:
            continue
        txt = ' '.join(toks)
        squashed = re.sub(r'\s+', '', txt)
        hits = len(set(STATEMENT_CUE.findall(txt))
                   | set(STATEMENT_CUE.findall(squashed)))
        numbers = sum(1 for t in toks if _NUMERIC_TOKEN.match(t.strip()))
        if hits < min_hits and numbers < MIN_TABLE_NUMBERS:
            continue
        # OCR mangles spacing, so the basis word is looked for in both the
        # spaced and the squashed rendering.
        basis = _basis_of(txt)
        if basis == 'Unknown':
            basis = _basis_of(squashed)
        # A dense grid of figures outranks vocabulary -- the notes page talks
        # about the results, the statement page *is* the results -- and among
        # real tables, consolidated outranks standalone.
        score = (numbers >= MIN_TABLE_NUMBERS, _BASIS_RANK[basis], numbers, hits)
        if best is None or score > best[0]:
            best = (score, i)
    return best[1] if best else None


def words_from_page(page, dpi=150, min_conf=0.5, engine_name=None):
    """OCR a PDF page and return words in get_text("words") shape."""
    got = get_engine(engine_name) if engine_name else engine()
    if not got:
        return []
    eng, kind = got
    import fitz
    pix = page.get_pixmap(dpi=dpi)
    scale = 72.0 / dpi          # pixels -> PDF points
    png = pix.tobytes('png')

    raw = []
    if kind == 'rapidocr':
        res, _ = eng(png)
        for box, txt, conf in (res or []):
            if conf is not None and conf < min_conf:
                continue
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            raw.append((min(xs), min(ys), max(xs), max(ys), txt))
    else:
        import io, pandas as pd
        from PIL import Image
        df = eng.image_to_data(Image.open(io.BytesIO(png)),
                               output_type=eng.Output.DATAFRAME)
        df = df[df.conf.astype(float) > min_conf * 100]
        for _, r in df.iterrows():
            t = str(r.text).strip()
            if t:
                raw.append((r.left, r.top, r.left + r.width, r.top + r.height, t))

    out = []
    for x0, y0, x1, y1, txt in raw:
        for part, px0, px1 in _split(txt, x0, x1):
            out.append((px0 * scale, y0 * scale, px1 * scale, y1 * scale,
                        part, 0, 0, 0))
    out.sort(key=lambda w: (w[1], w[0]))
    return out
