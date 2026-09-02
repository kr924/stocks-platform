#!/usr/bin/env python3
"""
Reliable quarterly-results extractor for NSE/BSE earnings PDFs.

Why this design (the earlier pipelines failed on exactly these points):
  1. Work off the PDF *text layer with coordinates* (PyMuPDF "words"), never
     off linearised get_text() -- linearisation destroys column association.
  2. Numbers in Indian P&L statements are RIGHT-aligned, so cluster numeric
     tokens by right edge (x1) to recover the true column grid.
  3. Classify each column as QUARTER / YTD / YEAR from the spanning header
     ("Quarter ended" vs "Year ended") plus its end date. This is what stops
     a full-year figure being reported as the quarter.
  4. Match row labels via a synonym table with explicit *exclusions*
     (comprehensive income, EPS, ratios) -- the classic false positives.
  5. Normalise units (thousand / lakh / million / crore) -> Rs Crore.
  6. Derive EBITDA two independent ways and require agreement. Disagreement
     means the row mapping is wrong, so flag it rather than emit a bad number.

Usage:
    python quarterly_extract.py --symbols ECLERX RELIANCE
    python quarterly_extract.py --all --workers 8 --out quarterly_results.csv
"""
import fitz, re, os, sys, json, csv, argparse
from concurrent.futures import ProcessPoolExecutor, as_completed

PDF_DIR = "downloaded_stock_pdfs"

# MuPDF writes font and page-tree complaints straight to stderr from C. They
# are noise -- a malformed embedded font or a missing page is handled, and the
# affected file still reports its own status in the output. Silencing them
# keeps the progress log readable.
try:
    fitz.TOOLS.mupdf_display_errors(False)
except Exception:
    pass

# ------------------------------------------------------------------ numbers
NUM_RE = re.compile(r'\(?[-+]?[\d,]*\.?\d+\)?[*#]?')
DASH = {'-', '--', '–', '—', '−', ''}


def parse_num(s):
    s = s.strip().rstrip('*#')
    if s in DASH:
        return 0.0
    neg = s.startswith('(') and s.endswith(')')
    s = s.strip('()').replace(',', '').replace('₹', '').strip()
    if s.startswith('-'):
        neg, s = True, s[1:]
    if not s or not re.fullmatch(r'\d*\.?\d+', s):
        return None
    v = float(s)
    return -v if neg else v


def is_num(s):
    t = s.strip().rstrip('*#')
    return t in DASH or bool(NUM_RE.fullmatch(t))


# ------------------------------------------------------------------ units
TO_CRORE = {'thousand': 1e-4, 'lakh': 1e-2, 'lac': 1e-2, 'million': 1e-1,
            'mn': 1e-1, 'billion': 1e2, 'bn': 1e2, 'crore': 1.0, 'cr': 1.0}
_PLURAL = {'lakhs': 'lakh', 'lacs': 'lac', 'millions': 'million',
           'crores': 'crore', 'thousands': 'thousand', 'billions': 'billion'}
UNIT_WORD = re.compile(
    r'\b(thousands?|lakhs?|lacs?|millions?|mn|billions?|bn|crores?|cr)\b\.?', re.I)
# The scale marker is written many ways -- "(Rs. in Lakhs)", "(In Rs. million)",
# "(All amounts in Rs Million)", "(₹ in crore)". Rather than enumerate every
# spelling, find the unit word and require a currency / "in" / "amount" cue
# just before it.
UNIT_CUE = re.compile(r'(?:\bin\b|\brs\b|\binr\b|₹|\brupees\b|\bamounts?\b)', re.I)


# IT companies publish the same table in two currencies ("In US $ million" and
# "In Rs crore" on one page). Taking the first unit word found reads the rupee
# figures at the dollar scale -- a silent 10x error -- so dollar-scoped markers
# are skipped unless a rupee cue sits alongside.
USD_CUE = re.compile(r'us\s*\$|\busd\b|us\s*dollar|\$', re.I)
INR_CUE = re.compile(r'₹|\brs\b|\binr\b|rupee', re.I)


def _unit_of(m):
    w = _PLURAL.get(m.group(1).lower().rstrip('.'), m.group(1).lower().rstrip('.'))
    return TO_CRORE[w], ('lakh' if w == 'lac' else w)


def _usd_scoped(text, start):
    back = text[max(0, start - 30):start]
    return bool(USD_CUE.search(back)) and not INR_CUE.search(back)


def _scan_unit(text):
    for m in UNIT_WORD.finditer(text):
        if _usd_scoped(text, m.start()):
            continue
        if UNIT_CUE.search(text[max(0, m.start() - 32):m.start()]):
            return _unit_of(m)
    return None


PAREN = re.compile(r'\([^)]{0,60}\)')


def _unit_in(text):
    """Unit word anywhere in a short parenthetical -- no currency cue needed.

    Scanned statements mangle the cue ("(Rs in Millions)" arrives as
    "(fin Millions"), so inside a short bracket the unit word alone is enough.
    Dollar-scoped markers are still skipped.
    """
    for m in UNIT_WORD.finditer(text):
        if _usd_scoped(text, m.start()):
            continue
        return _unit_of(m)
    return None


def detect_unit(page_text, doc_text=None):
    """Return (multiplier_to_crore, unit_name).

    Statement page first, then a majority vote over the whole document.
    Getting this wrong is a 10x or 100x error, so an undetermined unit is
    reported as assumed rather than silently defaulted.
    """
    for par in PAREN.findall(page_text):
        got = _unit_in(par)
        if got:
            return got
    got = _scan_unit(page_text)
    if got:
        return got
    if doc_text:
        votes = {}
        for par in PAREN.findall(doc_text):
            got = _unit_in(par)
            if got:
                votes[got] = votes.get(got, 0) + 1
        if votes:
            return max(votes.items(), key=lambda kv: kv[1])[0]
    return 1e-2, 'lakh(assumed)'


# ------------------------------------------------------------------ labels
def norm(s):
    s = re.sub(r'[^a-z0-9 ]', ' ', s.lower())
    return re.sub(r'\s+', ' ', s).strip()


# Rows that must never be matched. Word-anchored on purpose: as a plain
# substring, "ratio" hides inside "ope-ratio-ns" and silently discards every
# "Revenue from operations" row -- the most important line in the statement.
BAD_RE = re.compile(
    r'\b(?:comprehensive income|earnings per share|per share|paid[- ]up'
    r'|reserves?|face value|debentures?|net worth|debt[- ]equity|ratios?'
    r'|capital redemption|outstanding|equity share capital|basic|diluted'
    r'|annualised|annualized)\b')

METRICS = [
    ('revenue', ['revenue from operations', 'income from operations',
                 'revenue from contract', 'net sales', 'gross revenue',
                 'total revenue from operations', 'sales and services',
                 'value of sales', 'turnover', 'total operating income']),
    ('other_income', ['other income']),
    ('total_income', ['total income', 'total revenue']),
    ('total_exp', ['total expenses', 'total expenditure', 'total cost']),
    ('depreciation', ['depreciation and amorti', 'depreciation amorti',
                      'depreciation expense', 'depreciation']),
    ('finance_cost', ['finance cost', 'finance charge', 'interest expense',
                      'interest cost', 'interest and finance']),
    ('pbt_pre_exc', ['profit before exceptional', 'profit loss before exceptional',
                     'profit before share of', 'profit before exceptional items']),
    ('exceptional', ['exceptional item']),
    ('pbt', ['profit before tax', 'profit loss before tax', 'loss before tax',
             'profit before taxation', 'profit before income tax']),
    ('tax', ['tax expense', 'income tax expense', 'total tax',
             'provision for tax', 'tax expenses']),
    ('pat', ['profit for the period', 'profit for the quarter',
             'profit loss for the period', 'profit loss for the quarter',
             'net profit for the period', 'profit after tax',
             'profit loss after tax', 'profit for the year', 'net profit']),
]

BANK_HINT = ['interest earned', 'net interest income',
             'provisions and contingencies', 'deposits from customers']


SR_RE = re.compile(r'^(?:[ivxlc]+|\d+|[a-h])[ .)]+')
PROFIT_RE = re.compile(r'^(?:net\s+|total\s+)?(?:profit|loss)\b')
# A row whose label *starts with* "Profit/Loss ..." can only be a profit line.
# Without this, "Profit before exceptional items and tax" is mis-read as the
# exceptional-items row, which silently corrupts every derived figure.
PROFIT_KEYS = {'pbt', 'pat', 'pbt_pre_exc'}


def classify_label(lbl):
    n = norm(lbl)
    if not n or len(n) < 3:
        return None
    if BAD_RE.search(n):
        return None
    core = SR_RE.sub('', n)
    # OCR routinely loses inter-word spaces ("Revenue fromoperations",
    # "Totalexpenses"), so compare with whitespace squashed out as well.
    squash = core.replace(' ', '')
    profit_row = bool(PROFIT_RE.match(core))
    for key, pats in METRICS:
        if profit_row and key not in PROFIT_KEYS:
            continue
        for p in pats:
            if (core.startswith(p) or n.startswith(p) or (' ' + p) in (' ' + n)
                    or squash.startswith(p.replace(' ', ''))):
                return key
    return None


# ------------------------------------------------------------------ geometry
NUMISH = re.compile(r'^[\d.,()\-–—]+$')


def merge_numeric(line, gap=2.5):
    """Glue number fragments back together.

    Extractors routinely split a figure at the thousands separator, so
    "151,835.5" arrives as "151" + ",835.5" and "31.03.2026" as "31" +
    ".03.2026". Left alone, the leading fragment wins its column and the row
    reports 151 instead of 151,835.5.
    """
    out = []
    for w in line:
        w = tuple(w)
        if (out and NUMISH.match(w[4]) and NUMISH.match(out[-1][4])
                and w[0] - out[-1][2] <= gap):
            p = out[-1]
            out[-1] = (p[0], min(p[1], w[1]), w[2], max(p[3], w[3]), p[4] + w[4])
        else:
            out.append(w)
    return out


def group_lines(words, tol=3.0):
    ws = sorted(words, key=lambda w: (w[1], w[0]))
    lines, cur, ref = [], [], None
    for w in ws:
        if ref is None or abs(w[1] - ref) <= tol:
            cur.append(w)
            ref = w[1] if ref is None else ref
        else:
            lines.append(merge_numeric(sorted(cur, key=lambda t: t[0])))
            cur, ref = [w], w[1]
    if cur:
        lines.append(merge_numeric(sorted(cur, key=lambda t: t[0])))
    return lines


def cluster1d(xs, tol=14.0):
    xs = sorted(xs)
    out = []
    for x in xs:
        if out and x - out[-1][-1] <= tol:
            out[-1].append(x)
        else:
            out.append([x])
    return out


# ------------------------------------------------------------------ dates
MONTHS = {m: i + 1 for i, m in enumerate(
    ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'])}
MON_NAME = ['', 'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']


def _yr(y):
    y = int(y)
    return y + 2000 if y < 100 else y


_NOW = __import__('datetime').date.today().year
# A results PDF can only be about a recent period. Without this bound, a
# column header truncated to "Mar 31" parses as March 2031 and then wins the
# "latest quarter" contest.
YR_LO, YR_HI = _NOW - 6, _NOW + 1


def _ord(y, mo):
    y = _yr(y)
    return y * 12 + mo if 1 <= mo <= 12 and YR_LO <= y <= YR_HI else None


# Ordered most-specific first. Each is scanned with finditer so that a bogus
# early match (e.g. "Note 17 32") is skipped rather than accepted.
DATE_PATS = [
    (r'\b(\d{1,2}) (\d{1,2}) (\d{2,4})\b', lambda m: _ord(m.group(3), int(m.group(2)))),
    (r'\b(\d{1,2})\s*([a-z]{3,9})\s*(\d{2,4})\b',
     lambda m: _ord(m.group(3), MONTHS[m.group(2)[:3]]) if m.group(2)[:3] in MONTHS else None),
    (r'\b([a-z]{3,9})\s*(\d{1,2})\s*(\d{2,4})\b',
     lambda m: _ord(m.group(3), MONTHS[m.group(1)[:3]]) if m.group(1)[:3] in MONTHS else None),
    (r'\b([a-z]{3,9})\s*(\d{2,4})\b',
     lambda m: _ord(m.group(2), MONTHS[m.group(1)[:3]]) if m.group(1)[:3] in MONTHS else None),
]


def parse_date(txt):
    """Return ordinal months (year*12+month) for a column header, else None."""
    t = norm(txt)
    for pat, fn in DATE_PATS:
        for m in re.finditer(pat, t):
            o = fn(m)
            if o:
                return o
    return None


def om(o):
    if not o:
        return ''
    return f"{MON_NAME[(o - 1) % 12 + 1]} {(o - 1) // 12}"


SPAN_Q = re.compile(r'(quarter|3\s*months|three\s*months)\s*(ended|ending)', re.I)
SPAN_Y = re.compile(r'(year|12\s*months|twelve\s*months)\s*(to\s*date|ended|ending)', re.I)
SPAN_P = re.compile(r'(six\s*months|9\s*months|nine\s*months|half\s*year|period)\s*(ended|ending)', re.I)

# Title / letterhead lines. They contain dates ("...FOR THE QUARTER ENDED 30TH
# JUNE, 2026") and would otherwise be scattered across the column headers.
NOT_HEADER = re.compile(
    r'statement of|financial results|results for the|regd\.?\s*off|registered office'
    r'|\bcin\b|website|www\.|phone|e-?mail|corporate office|extract of', re.I)
HDR_TOK = re.compile(
    r'^(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[,.]?$'
    r'|^\d{1,2}[./-]\d{1,2}[./-]\d{2,4}$'
    r'|^\d{1,2}[-/](?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[-/]\d{2,4}$'
    r'|^\d{4}$|^\d{1,2}(?:st|nd|rd|th)?[,.]?$|^\(?(?:un)?audited\)?$'
    r'|^\d{1,2}\s*,\s*\d{4}$'
    # compact, separator-free form that OCR produces: "31March2026"
    r'|^\d{1,2}(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\d{2,4}$', re.I)


# ------------------------------------------------------------------ page pick
def page_score(text):
    t = text.lower()
    if len(t) < 200:
        return 0
    s = 0
    if re.search(r'revenue from operations|income from operations|total income'
                 r'|interest earned|net sales', t, re.S):
        s += 3
    if re.search(r'total expens|total expenditure|total cost', t, re.S):
        s += 3
    if re.search(r'profit\s+.{0,25}before\s+tax|profit for the (period|quarter|year)'
                 r'|net profit', t, re.S):
        s += 3
    if re.search(r'quarter\s+ended|3\s*months\s+ended|three\s+months\s+ended'
                 r'|period\s+ended', t, re.S):
        s += 2
    if 'depreciation' in t:
        s += 1
    if 'finance cost' in t or 'interest' in t:
        s += 1
    if 'statement of' in t and 'result' in t:
        s += 2
    if 'balance sheet' in t or 'cash flow' in t or 'assets' in t and 'liabilities' in t:
        s -= 4
    if 'segment' in t and 'revenue' in t and 'result' in t:
        s -= 3
    if 'subsidiar' in t and 'statement of' not in t:
        s -= 2
    if 'fact sheet' in t:
        s -= 4          # investor fact sheets restate old quarters
    return s


def basis_of(text):
    t = text.lower()
    head = t[:1500]
    c, s = head.count('consolidated'), head.count('standalone')
    if c > s:
        return 'Consolidated'
    if s > c:
        return 'Standalone'
    c, s = t.count('consolidated'), t.count('standalone')
    return 'Consolidated' if c > s else ('Standalone' if s else 'Unknown')


# ------------------------------------------------------------------ core
def extract_page(page, why=None, words=None):
    """Return dict of parsed grid for one page, or None.

    `words` lets a caller supply word boxes from somewhere other than the text
    layer -- specifically OCR of the rendered page -- so the same grid logic
    serves both paths.
    """
    words = page.get_text("words") if words is None else words
    if not words:
        return None
    W = page.rect.width
    lines = group_lines(words)

    # --- 1. locate data rows and build the numeric column grid -------------
    rows = []
    for ln in lines:
        nums = [w for w in ln if is_num(w[4]) and w[4].strip() not in DASH]
        txts = [w for w in ln if not is_num(w[4])]
        label = ' '.join(w[4] for w in txts)
        rows.append({'line': ln, 'nums': nums, 'label': label,
                     'y': ln[0][1], 'key': classify_label(label)})

    # Some filings put a wrapped label and its figures on slightly different
    # baselines, leaving the label row empty. Adopt the figures from an
    # adjacent unlabelled row so the line is not lost.
    for i, r in enumerate(rows):
        if not r['key'] or r['nums']:
            continue
        for j in (i + 1, i - 1):
            if 0 <= j < len(rows):
                nb = rows[j]
                if not nb['key'] and len(nb['nums']) >= 2 and len(norm(nb['label'])) < 4:
                    r['nums'], nb['nums'] = nb['nums'], []
                    break

    data_rows = [r for r in rows if r['key'] and len(r['nums']) >= 2]
    if len(data_rows) < 3:
        if why is not None: why.append('few_data_rows(%d)' % len(data_rows))
        return None

    # right-edge clustering over numeric tokens of identified data rows
    edges = []
    for r in data_rows:
        edges += [w[2] for w in r['nums']]
    cl = [c for c in cluster1d(edges) if len(c) >= max(2, len(data_rows) // 3)]
    if len(cl) < 2:
        if why is not None: why.append('few_num_cols(%d)' % len(cl))
        return None
    centers = [sum(c) / len(c) for c in cl]

    def col_of(w):
        d = [abs(w[2] - c) for c in centers]
        i = d.index(min(d))
        return i if d[i] < 30 else None

    # --- 1b. drop the "Sr. No." pseudo-column ------------------------------
    # Roman/arabic row numbers on the left cluster like a real column and
    # shift every downstream column assignment by one.
    bycol = {i: [] for i in range(len(centers))}
    for r in data_rows:
        for w in r['nums']:
            i = col_of(w)
            if i is not None:
                bycol[i].append(w[4].strip())
    keep = [i for i, c in enumerate(centers)
            if not (c < W * 0.34 and bycol[i]
                    and all(re.fullmatch(r'\d{1,3}', v) for v in bycol[i]))]
    centers = [centers[i] for i in keep]
    if len(centers) < 2:
        if why is not None: why.append('all_cols_srno')
        return None

    # --- 2. header: spanning period type + per-column end date -------------
    # Header cells are wider than the right-aligned numbers beneath them
    # ("June 30, 2026" starts well left of the number's right edge), so assign
    # header tokens by x-interval, not by distance to the column's right edge.
    gaps = [centers[i + 1] - centers[i] for i in range(len(centers) - 1)] or [60.0]
    g = min(gaps)
    # `centers` are the RIGHT edges of right-aligned numbers, while a header
    # cell sits centred over its column. So the split between two columns lies
    # only a quarter-gap right of a number's right edge, not half way -- at the
    # half-way point "March" of the next column's "March 31, 2026" falls into
    # the previous column and both dates become unreadable.
    edge = 0.25 * g
    bounds = [(centers[i - 1] + edge if i else c - g + edge, c + edge)
              for i, c in enumerate(centers)]

    def colhit(w):
        mid = (w[0] + w[2]) / 2
        for i, (lo, hi) in enumerate(bounds):
            if lo <= mid < hi:
                return i
        return None

    first_data_y = min(r['y'] for r in data_rows)
    span_lines, plain = [], []
    for r in rows:
        if r['y'] >= first_data_y:
            continue
        txt = r['label']
        # A genuine column-header row touches >=2 columns. A title such as
        # "...FOR THE QUARTER ENDED 30TH JUNE, 2026" has its date tokens
        # bunched in one place, so it is excluded by the same test.
        hits = {colhit(w) for w in r['line'] if HDR_TOK.match(w[4].strip())}
        hits.discard(None)
        if len(hits) >= 2 and not NOT_HEADER.search(txt):
            plain.append(r)
        if not NOT_HEADER.search(txt) and (SPAN_Q.search(txt) or SPAN_Y.search(txt)
                                           or SPAN_P.search(txt)):
            span_lines.append(r)

    spans = []   # (center_x, kind)
    for r in span_lines:
        toks = [w for w in r['line'] if not is_num(w[4])]
        # split the line into phrase groups by horizontal gaps
        groups, cur = [], []
        for w in toks:
            if cur and w[0] - cur[-1][2] > 22:
                groups.append(cur)
                cur = [w]
            else:
                cur.append(w)
        if cur:
            groups.append(cur)
        for g in groups:
            gt = ' '.join(w[4] for w in g)
            kind = ('Q' if SPAN_Q.search(gt) else
                    'Y' if SPAN_Y.search(gt) else
                    'P' if SPAN_P.search(gt) else None)
            if kind:
                spans.append(((g[0][0] + g[-1][2]) / 2, kind))
    spans.sort()

    def kind_of(i):
        if not spans:
            return 'Q'
        cx = centers[i]
        return min(spans, key=lambda s: abs(s[0] - cx))[1]

    # per-column header text -> end date. The reach is capped at under half a
    # column gap so a neighbour's date cannot bleed across.
    coltext = [''] * len(centers)
    for r in plain:
        for w in r['line']:
            j = colhit(w)
            if j is not None:
                coltext[j] += ' ' + w[4]
    dates = [parse_date(t) for t in coltext]

    # Keep only columns we could date. An undated column cannot be classified
    # quarter-vs-year, and guessing there is exactly how wrong data escapes.
    counts = [0] * len(centers)
    for r in data_rows:
        for w in r['nums']:
            i = col_of(w)
            if i is not None:
                counts[i] += 1
    dated = [i for i, d in enumerate(dates) if d]
    if len(dated) < 2:
        if why is not None:
            why.append('undated(%d of %d) hdrlines=%d %r' % (len(dated), len(centers), len(plain), [t[:40] for t in coltext]))
        return None
    lost = sum(1 for i, d in enumerate(dates)
               if not d and counts[i] >= len(data_rows) // 2)
    # Keep where the undated-but-populated columns sat. In Indian filings the
    # quarter columns always precede the year-to-date ones, so a dropped
    # column to the LEFT of whichever column we end up choosing means the real
    # quarter was the one we could not date -- and we are about to report a
    # full-year figure as a quarter.
    lost_x = [centers[i] for i, d in enumerate(dates)
              if not d and counts[i] >= len(data_rows) // 2]
    centers = [centers[i] for i in dated]
    coltext = [coltext[i] for i in dated]
    dates = [dates[i] for i in dated]

    # --- 3. values ---------------------------------------------------------
    vals = {}
    for r in data_rows:
        key = r['key']
        cells = {}
        for w in r['nums']:
            c = col_of(w)
            if c is None:
                continue
            v = parse_num(w[4])
            if v is not None and c not in cells:
                cells[c] = v
        if not cells:
            continue
        # first (topmost) occurrence wins, except pbt/pat where the LAST
        # (post-exceptional / final) line is the right one
        if key in vals and key not in ('pbt', 'pat'):
            continue
        vals[key] = cells

    return {'centers': centers, 'kinds': [kind_of(i) for i in range(len(centers))],
            'dates': dates, 'vals': vals, 'coltext': coltext,
            'nrows': len(data_rows), 'lost': lost, 'lost_x': lost_x}


def pick_periods(grid):
    """Choose (latest, qoq, yoy) column indices, driven by the column dates.

    The spanning "Quarter ended" / "Year ended" label is NOT centred over its
    columns in many filings, so it cannot be used to classify them. Dates can:
    a Q4 statement repeats the same end date for the quarter and the full year,
    and a quarter is always contained in its own year -- so where two columns
    share a date, the SMALLER one is the quarter. Column order breaks ties.
    """
    dates = grid['dates']
    idx = [i for i, d in enumerate(dates) if d]
    if not idx:
        return None, None, None, 'no dated columns'

    size = {}
    for i in idx:
        for k in ('revenue', 'total_income', 'total_exp', 'pat'):
            if k in grid['vals'] and i in grid['vals'][k]:
                size[i] = abs(grid['vals'][k][i])
                break

    def pick(target):
        c = [i for i in idx if dates[i] == target]
        if not c:
            return None
        return min(c, key=lambda i: (size.get(i, float('inf')), i))

    d0 = max(dates[i] for i in idx)
    return pick(d0), pick(d0 - 3), pick(d0 - 12), ''


def col_metrics(vals, ci, mult):
    g = lambda k: (vals[k][ci] * mult) if (k in vals and ci in vals[k]) else None
    rev, oi, ti = g('revenue'), g('other_income'), g('total_income')
    te, dep, fc = g('total_exp'), g('depreciation'), g('finance_cost')
    pbt, pat, exc = g('pbt'), g('pat'), g('exceptional')
    pbt_pre = g('pbt_pre_exc')
    if pbt is None:
        pbt = pbt_pre

    if rev is None and ti is not None and oi is not None:
        rev = ti - oi
    if ti is None and rev is not None and oi is not None:
        ti = rev + oi
    if oi is None and ti is not None and rev is not None:
        oi = ti - rev

    # EBITDA route A: straight off the operating lines.
    #        route B: back out of the profit line.
    # They are algebraically identical on a well-formed statement, so a
    # disagreement is proof that a row was mapped to the wrong label.
    ebitda_a = ebitda_b = None
    if None not in (rev, te):
        ebitda_a = rev - te + (dep or 0) + (fc or 0)
    # Use the pre-exceptional profit line only when an exceptional-items row is
    # actually present. A stray "profit before ..." match would otherwise make
    # the two routes disagree and reject an extraction that is in fact correct.
    if pbt_pre is not None and exc:
        ebitda_b = pbt_pre + (dep or 0) + (fc or 0) - (oi or 0)
    elif pbt is not None and not exc:
        ebitda_b = pbt + (dep or 0) + (fc or 0) - (oi or 0)
    ebitda = ebitda_a if ebitda_a is not None else ebitda_b

    return {'revenue': rev, 'other_income': oi, 'total_income': ti,
            'expenses': te, 'depreciation': dep, 'finance_cost': fc,
            'exceptional': exc, 'pbt': pbt, 'pat': pat, 'tax': g('tax'),
            'pbt_pre_exc': pbt_pre,
            'ebitda': ebitda, '_ea': ebitda_a, '_eb': ebitda_b}


def pct(new, old):
    if new is None or old is None or old == 0:
        return None
    if old < 0:
        return None            # % change off a negative base is meaningless
    return round((new - old) / abs(old) * 100, 2)


# Structural faults -- used to reject a candidate page and try the next one.
# `undated_data_col` is deliberately NOT here. It fires on an extra column we
# could not date -- usually a year-to-date column -- which says nothing about
# the quarter actually reported. The identity checks below cover correctness,
# and a genuinely wrong quarter is caught by the screener cross-check.
HARD_SELECT = {'ebitda_mismatch', 'income_identity_fail', 'missing_core_metric',
               'pat_gt_pbt', 'negative_revenue', 'undated_col_left_of_pick',
               'pat_dwarfs_revenue', 'negative_expenses'}
# Also downgrades the final status, but must NOT steer page selection: a page
# whose figures are right but whose scale marker is missing is still the right
# page, and rejecting it lands us on a worse one.
HARD = HARD_SELECT | {'unit_assumed'}


def validate(cur, grid, qoq, yoy, uname, latest=None):
    flags = []
    ea, eb = cur.get('_ea'), cur.get('_eb')
    scale = max(abs(cur.get('revenue') or 0), 1.0)
    # 5% of turnover: tight enough to catch a mis-mapped row (those are off by
    # a whole line item), loose enough to tolerate items the model does not
    # carry, e.g. share of profit of associates.
    tol = max(0.05 * scale, 0.5)
    if ea is not None and eb is not None and abs(ea - eb) > tol:
        flags.append(f'ebitda_mismatch({ea:.1f}vs{eb:.1f})')
    ti, rev, oi = cur.get('total_income'), cur.get('revenue'), cur.get('other_income')
    if None not in (ti, rev, oi) and abs(ti - rev - oi) > max(0.01 * scale, 0.5):
        flags.append('income_identity_fail')
    if cur.get('pbt') is not None and cur.get('pat') is not None \
            and cur['pat'] > cur['pbt'] + max(0.05 * scale, 1):
        flags.append('pat_gt_pbt')
    if grid.get('lost'):
        # a populated column whose header we could not read -- the quarter we
        # picked may not be the newest one on the page
        flags.append(f"undated_data_col({grid['lost']})")
        if latest is not None and grid.get('lost_x'):
            here = grid['centers'][latest]
            if any(x < here for x in grid['lost_x']):
                flags.append('undated_col_left_of_pick')
    if qoq is None:
        flags.append('no_qoq_column')
    if yoy is None:
        flags.append('no_yoy_column')
    if cur.get('revenue') is None or cur.get('pat') is None:
        flags.append('missing_core_metric')
    # Plausibility: revenue cannot be negative, and a profit many times larger
    # than turnover means a row or a column was picked up wrongly.
    rv2 = cur.get('revenue')
    if rv2 is not None and rv2 < 0:
        flags.append('negative_revenue')
    if rv2 is not None and cur.get('pat') is not None and rv2 > 0 \
            and abs(cur['pat']) > 10 * rv2:
        flags.append('pat_dwarfs_revenue')
    if cur.get('expenses') is not None and cur['expenses'] < 0:
        flags.append('negative_expenses')
    if uname.endswith('(assumed)'):
        flags.append('unit_assumed')
    return flags


def try_page(doc, pidx, texts, why=None, words=None):
    grid = extract_page(doc[pidx], why, words)
    if not grid or len(grid['vals']) < 4:
        return None
    mult, uname = detect_unit(texts[pidx], '\n'.join(texts))
    latest, qoq, yoy, err = pick_periods(grid)
    if latest is None:
        return None
    cur = col_metrics(grid['vals'], latest, mult)
    prv = col_metrics(grid['vals'], qoq, mult) if qoq is not None else {}
    lyr = col_metrics(grid['vals'], yoy, mult) if yoy is not None else {}
    return {'grid': grid, 'page': pidx, 'unit': uname, 'mult': mult,
            'latest': latest, 'qoq': qoq, 'yoy': yoy,
            'cur': cur, 'prv': prv, 'lyr': lyr,
            'flags': validate(cur, grid, qoq, yoy, uname, latest)}


OCR_FONT = re.compile(r'glyphless|hiddenhorz|invisible', re.I)


def has_ocr_layer(page):
    """True when the page's text is an invisible OCR layer, not real text.

    Tesseract stamps its layer with GlyphLessFont at a uniform character
    width, so the embedded text is somebody else's OCR guess -- and on these
    filings it silently drops commas and decimal points ("724377" for
    "7,243.77"). Knowing that up front means we can trust a fresh read of the
    pixels over the text layer instead of hoping the text layer validates.
    """
    try:
        return any(OCR_FONT.search(sp['font'])
                   for b in page.get_text('rawdict')['blocks']
                   for l in b.get('lines', []) for sp in l.get('spans', []))
    except Exception:
        return False


def ocr_discover(doc, texts, sym, engine_name=None):
    """Find the statement page by OCR, then read it."""
    try:
        from . import ocr_words
        pidx = ocr_words.find_statement_page(doc, engine_name=engine_name)
    except Exception:
        return None
    if pidx is None:
        return None
    return ocr_page(doc, pidx, texts, sym, engine_name)


def ocr_page(doc, pidx, texts, sym, engine_name=None):
    """Re-read one page through OCR and rebuild the row, or None."""
    try:
        from . import ocr_words
        w = ocr_words.words_from_page(doc[pidx], engine_name=engine_name)
        if not w:
            return None
        r = try_page(doc, pidx, texts, None, w)
        if not r:
            return None
    except Exception:
        return None
    o = {'symbol': sym, 'status': 'OK', 'flags': list(r['flags']),
         'page': pidx + 1, 'basis': r.get('basis', ''), 'unit': r['unit'],
         'engine': engine_name or 'auto'}
    g, cur, prv, lyr = r['grid'], r['cur'], r['prv'], r['lyr']
    o['quarter'] = om(g['dates'][r['latest']])
    o['qoq_quarter'] = om(g['dates'][r['qoq']]) if r['qoq'] is not None else ''
    o['yoy_quarter'] = om(g['dates'][r['yoy']]) if r['yoy'] is not None else ''
    for k in ('total_income', 'tax', 'depreciation', 'finance_cost'):
        v = cur.get(k)
        o[k] = round(v, 2) if v is not None else None
    for k in ('revenue', 'expenses', 'other_income', 'ebitda', 'pbt', 'pat'):
        v = cur.get(k)
        o[k] = round(v, 2) if v is not None else None
        o[k + '_yoy_pct'] = pct(cur.get(k), lyr.get(k))
        o[k + '_qoq_pct'] = pct(cur.get(k), prv.get(k))
        o[k + '_prev_q'] = round(prv[k], 2) if prv.get(k) is not None else None
        o[k + '_year_ago'] = round(lyr[k], 2) if lyr.get(k) is not None else None
    hard = [f for f in o['flags'] if f.split('(')[0] in HARD]
    o['status'] = 'REVIEW' if hard else ('OK' if not o['flags'] else 'OK_WARN')
    return o


def extract(path, symbol=None, ocr_fallback=False):
    sym = symbol or re.sub(r'_latest_quarter\.pdf$', '',
                           os.path.basename(path), flags=re.I)
    out = {'symbol': sym, 'status': 'FAIL', 'flags': []}
    try:
        doc = fitz.open(path)
    except Exception as e:
        out['flags'].append('open_error:' + type(e).__name__)
        return out
    try:
        texts = [p.get_text() for p in doc]
        flat = re.sub(r'\s+', ' ', '\n'.join(texts))
        # NSE serves an HTML error page for withdrawn/moved filings; it is
        # saved as a valid PDF, so it must be detected by content.
        if 'Page you are looking for has been' in flat or len(flat.strip()) < 200:
            out['status'] = 'INVALID_PDF'
            out['flags'].append('dead_download_or_moved')
            return out

        cands = []
        for i, t in enumerate(texts):
            s = page_score(t)
            if s >= 6:
                cands.append((s, i, basis_of(t)))
        if not cands:
            # No page could be scored from the text layer. If OCR is enabled,
            # find the statement by rendering pages instead -- this is the only
            # route for image-only filings, which otherwise never get read.
            if ocr_fallback:
                from . import ocr_words
                for name in ocr_words.available():
                    alt = ocr_discover(doc, texts, out['symbol'], name)
                    if alt:
                        alt['flags'].append(f'via_ocr_discovered:{name}')
                        return alt
            avg = sum(len(t) for t in texts) / max(1, len(texts))
            if avg < 600:
                out['status'] = 'NEEDS_OCR'
                out['flags'].append('image_only_pdf')
            else:
                out['flags'].append('no_pl_page_found')
            return out

        # Try candidate pages in (basis preference, page order) and accept the
        # FIRST one that passes validation. Page order matters: the primary
        # statement precedes subsidiary/segment tables, which score similarly.
        order = []
        for pref in ('Consolidated', 'Standalone', 'Unknown'):
            order += sorted([c for c in cands if c[2] == pref], key=lambda c: c[1])

        # Score every candidate rather than taking the first clean one. A
        # consolidated statement with one minor discrepancy is a better answer
        # than a spotless standalone one, because consolidated is what these
        # figures are normally compared against.
        best, chosen = None, None
        for s, i, b in order[:12]:
            r = try_page(doc, i, texts)
            if not r:
                continue
            r['basis'] = b
            hard = [f for f in r['flags'] if f.split('(')[0] in HARD_SELECT]
            sc = 30 if b == 'Consolidated' else (0 if b == 'Standalone' else -10)
            sc -= 25 * len(hard)
            sc -= 3 * (len(r['flags']) - len(hard))
            sc += 5 * (r['qoq'] is not None) + 5 * (r['yoy'] is not None)
            if best is None or sc > best:
                best, chosen = sc, r
            if sc >= 40:
                break
        if not chosen:
            out['flags'].append('grid_parse_failed')
            return out

        grid = chosen['grid']
        latest, qoq, yoy = chosen['latest'], chosen['qoq'], chosen['yoy']
        cur, prv, lyr = chosen['cur'], chosen['prv'], chosen['lyr']
        out.update({'page': chosen['page'] + 1, 'basis': chosen['basis'],
                    'unit': chosen['unit']})
        out['flags'] += chosen['flags']
        # Screener and most users want the consolidated numbers. If we ended up
        # on a standalone page while the filing also contains a consolidated
        # statement, say so -- the figures will legitimately differ.
        if chosen['basis'] != 'Consolidated' and any(c[2] == 'Consolidated' for c in cands):
            out['flags'].append('standalone_fallback')
        if any(h in texts[chosen['page']].lower() for h in BANK_HINT):
            out['flags'].append('bank_nbfc_format')

        out['quarter'] = om(grid['dates'][latest])
        out['qoq_quarter'] = om(grid['dates'][qoq]) if qoq is not None else ''
        out['yoy_quarter'] = om(grid['dates'][yoy]) if yoy is not None else ''

        for k in ('total_income', 'tax', 'depreciation', 'finance_cost'):
            v = cur.get(k)
            out[k] = round(v, 2) if v is not None else None
        for k in ('revenue', 'expenses', 'other_income', 'ebitda', 'pbt', 'pat'):
            v = cur.get(k)
            out[k] = round(v, 2) if v is not None else None
            out[k + '_yoy_pct'] = pct(cur.get(k), lyr.get(k))
            out[k + '_qoq_pct'] = pct(cur.get(k), prv.get(k))
            # Emit the comparative columns as absolutes too. Every filing
            # carries three quarters, so checking all three against screener
            # gives three independent verifications per PDF instead of one --
            # and a unit or column error shows up in all three at once.
            out[k + '_prev_q'] = (round(prv[k], 2)
                                  if prv.get(k) is not None else None)
            out[k + '_year_ago'] = (round(lyr[k], 2)
                                    if lyr.get(k) is not None else None)

        hard = [f for f in out['flags'] if f.split('(')[0] in HARD]
        out['status'] = 'REVIEW' if hard else ('OK' if not out['flags'] else 'OK_WARN')

        # ---- Path B ------------------------------------------------------
        # The text layer produced something we do not trust. Some filings
        # embed text that disagrees with the printed page, and no amount of
        # re-parsing fixes that -- so read the pixels instead and keep
        # whichever attempt validates cleanly.
        if hard and ocr_fallback:
            # Cascade: cheapest engine first, stop as soon as one validates.
            # The text layer only keeps its claim to authority if it is real
            # text -- if it is itself an OCR layer, a fresh read that merely
            # ties on flag count still wins.
            stale = has_ocr_layer(doc[chosen['page']])
            from . import ocr_words
            best, best_hard = None, len(hard)
            for name in ocr_words.available():
                alt = ocr_page(doc, chosen['page'], texts, out['symbol'], name)
                if not alt:
                    continue
                n = len([f for f in alt['flags'] if f.split('(')[0] in HARD])
                if n == 0:
                    alt['flags'].append(f'via_ocr:{name}')
                    return alt
                if n < best_hard or (stale and n <= best_hard):
                    best, best_hard = alt, n
            if best is not None:
                best['flags'].append(
                    f"via_ocr:{best['engine']}" + ('_over_stale' if stale else ''))
                return best
            out['flags'].append('ocr_also_failed')
        return out
    except Exception as e:
        out['flags'].append(f'error:{type(e).__name__}:{e}')
        return out
    finally:
        doc.close()


FIELDS = ['symbol', 'status', 'basis', 'quarter', 'qoq_quarter', 'yoy_quarter',
          'unit', 'page',
          'revenue', 'revenue_yoy_pct', 'revenue_qoq_pct',
          'expenses', 'expenses_yoy_pct', 'expenses_qoq_pct',
          'other_income', 'other_income_yoy_pct', 'other_income_qoq_pct',
          'ebitda', 'ebitda_yoy_pct', 'ebitda_qoq_pct',
          'pbt', 'pbt_yoy_pct', 'pbt_qoq_pct',
          'pat', 'pat_yoy_pct', 'pat_qoq_pct']
FIELDS += [m + s for m in ('revenue', 'expenses', 'other_income', 'ebitda',
                           'pbt', 'pat') for s in ('_prev_q', '_year_ago')]
FIELDS += ['total_income', 'tax', 'depreciation', 'finance_cost', 'engine', 'flags']


def _job(a):
    return extract(a[0], ocr_fallback=a[1])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--symbols', nargs='*')
    ap.add_argument('--all', action='store_true')
    ap.add_argument('--limit', type=int)
    ap.add_argument('--workers', type=int, default=8)
    ap.add_argument('--out', default='quarterly_results.csv')
    ap.add_argument('--ocr', action='store_true',
                    help='Path B: re-read failed pages via OCR of the rendered page')
    a = ap.parse_args()

    if a.symbols:
        paths = [os.path.join(PDF_DIR, f"{s}_latest_quarter.pdf") for s in a.symbols]
    else:
        paths = [os.path.join(PDF_DIR, f) for f in sorted(os.listdir(PDF_DIR))
                 if f.lower().endswith('.pdf')]
    if a.limit:
        paths = paths[:a.limit]

    res = []
    if a.workers > 1 and len(paths) > 4:
        with ProcessPoolExecutor(max_workers=a.workers) as ex:
            futs = {ex.submit(_job, (p, a.ocr)): p for p in paths}
            for n, f in enumerate(as_completed(futs), 1):
                res.append(f.result())
                if n % 100 == 0:
                    print(f"  {n}/{len(paths)}", file=sys.stderr)
    else:
        for p in paths:
            res.append(extract(p, ocr_fallback=a.ocr))

    res.sort(key=lambda r: r['symbol'])
    with open(a.out, 'w', newline='', encoding='utf-8-sig') as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction='ignore')
        w.writeheader()
        for r in res:
            r = dict(r)
            r['flags'] = ';'.join(r.get('flags', []))
            w.writerow(r)

    from collections import Counter
    c = Counter(r['status'] for r in res)
    print(json.dumps(dict(c), indent=2))
    print(f"-> {a.out}  ({len(res)} rows)")


if __name__ == '__main__':
    main()
