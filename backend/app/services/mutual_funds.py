"""
Mutual funds in the portfolio: the CAMS/KFintech Consolidated Account
Statement (CAS) as the ledger, AMFI's daily NAV for value, and each scheme's
monthly portfolio disclosure for what the money is actually invested in.

- **Ledger.** One CAS PDF carries every folio under the email it was asked
  for: purchases, redemptions, switches, reinvestments, and the stamp duty /
  STT lines beside them. Each line is stored once (`uid` from folio, ISIN,
  date, kind, amount, units and running balance), so overlapping statements
  add nothing twice. The PAN is the client ID; PortfolioAccount maps it to a
  tracking name (broker "mf"), like a Zerodha or Upstox ID.
- **Matching.** FIFO per PAN, folio and scheme. Stamp duty goes into the cost
  of the purchase it was charged on. A redemption consumes the oldest units.
  A purchase reversal removes the purchase it reverses rather than selling.
- **Reconciled with the registrar.** The statement's own closing line (units,
  cost value) is kept per folio. When the ledger holds fewer units than the
  registrar says — a statement that starts after the first purchase — the
  rest is an undated lot at the registrar's remaining cost.
- **Tax.** By the scheme's class: *equity* (equity-oriented: 12 months, 20% /
  12.5% and the ₹1.25 lakh exemption shared with shares), *debt* (bought from
  1 Apr 2023: slab rate whatever the holding period), *other* (FoFs,
  international, gold, multi-asset: 24 months, then 12.5%; slab before).
  The class comes from AMFI's category, else the scheme's name, and can be
  overridden per scheme.
- **What the funds hold.** Each AMC publishes every scheme's portfolio at
  month end (Excel, sometimes PDF). `parse_portfolio_file` reads any of them
  generically — the header row is the one with an ISIN column and a "% to
  net assets" column — and matches each sheet to a held scheme by name.
  Shares are kept per ISIN; everything else is the remainder (debt, cash,
  REITs, derivatives margin…). `exposure()` then adds each fund's slice of
  every stock to the shares held directly.
"""
import base64
import hashlib
import io
import json
import logging
import re
import threading
import time
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("app.mutual_funds")

IST = timezone(timedelta(hours=5, minutes=30))
BROKER = "mf"
EPS = 1e-6
UNIT_EPS = 0.002          # units are stated to 3 decimals

NAV_URLS = ("https://portal.amfiindia.com/spages/NAVAll.txt",
            "https://www.amfiindia.com/spages/NAVAll.txt")
NAV_REFRESH_SECONDS = 3 * 3600
STALE_PORTFOLIO_DAYS = 50
DEBT_SLAB_FROM = date(2023, 4, 1)          # specified MF: slab rate from this purchase date
SPECIFIED_RULE_END = date(2025, 4, 1)      # "other" funds were specified (slab) for sales before this
RATE_CHANGE = date(2024, 7, 23)

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "text/plain,*/*",
}


def _today() -> date:
    return datetime.now(IST).date()


def _d(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _num(tok: str) -> Optional[float]:
    t = (tok or "").strip().replace(",", "").replace("₹", "").replace("INR", "").strip()
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()").strip()
    if t.endswith("%"):
        t = t[:-1]
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


def _cas_date(s: str) -> Optional[str]:
    try:
        return datetime.strptime(s, "%d-%b-%Y").date().isoformat()
    except ValueError:
        return None


# ─── Reading PDFs as lines ──────────────────────────────────────────────────

def _open_pdf(data: bytes, password: Optional[str] = None):
    import fitz  # PyMuPDF
    doc = fitz.open(stream=data, filetype="pdf")
    if doc.needs_pass:
        if not password or not doc.authenticate(password):
            doc.close()
            raise ValueError("This PDF is password-protected. Enter the password the statement was "
                             "requested with (CAMS: the one you typed on the request form; "
                             "KFintech: usually your PAN in capitals).")
    return doc


def pdf_lines(doc) -> List[str]:
    """Each page's words grouped into visual rows (by vertical centre), left to right."""
    out: List[str] = []
    for page in doc:
        rows: List[list] = []
        for w in sorted(page.get_text("words"), key=lambda w: ((w[1] + w[3]) / 2, w[0])):
            yc = (w[1] + w[3]) / 2
            if rows and abs(rows[-1][0] - yc) <= 2.5:
                rows[-1][1].append(w)
            else:
                rows.append([yc, [w]])
        for _, ws in rows:
            ws.sort(key=lambda w: w[0])
            out.append(" ".join(w[4] for w in ws))
    return out


# ─── CAS parser ─────────────────────────────────────────────────────────────

_DATE = r"\d{2}-[A-Za-z]{3}-\d{4}"
_TXN = re.compile(rf"^({_DATE})\s+(.*)$")
_NUMTOK = re.compile(r"^\(?-?[\d,]*\.\d+\)?$")
_AMC = re.compile(r"^[A-Za-z][A-Za-z0-9&.,'() -]*\bmutual\s+fund$", re.I)
_ISIN = re.compile(r"ISIN\s*:\s*([A-Z]{2}[A-Z0-9]{9}\d)")
_PAN = re.compile(r"PAN\s*:\s*([A-Z]{5}\d{4}[A-Z])")
_FOLIO = re.compile(r"Folio\s*No\s*:\s*(.+?)(?=\s+(?:PAN|KYC)\b|$)", re.I)
_CODE_PREFIX = re.compile(r"^[A-Za-z0-9]{2,15}-\s?[A-Z]")
_PERIOD = re.compile(rf"({_DATE})\s+To\s+({_DATE})", re.I)


def _clean_scheme(text: str) -> str:
    s = re.sub(r"Registrar\s*:\s*\S*", " ", text)
    s = re.sub(r"\(Advisor\s*:[^)]*\)", " ", s)
    s = re.sub(r"-?\s*ISIN\s*:\s*[A-Z0-9]{12}", " ", s)
    s = re.sub(r"\((?:Non-)?Demat\)", " ", s, flags=re.I)
    s = re.sub(r"\((?:formerly|erstwhile)[^)]*\)", " ", s, flags=re.I)
    s = re.sub(r"^[A-Za-z0-9]{2,15}-\s?(?=[A-Z])", "", s.strip())
    s = re.sub(r"\s+", " ", s).strip(" -")
    return s


def _kind(desc: str, units: Optional[float], n_numbers: int) -> str:
    d = desc.lower()
    if "stamp duty" in d:
        return "stamp_duty"
    if re.search(r"\bstt\b", d):
        return "stt"
    if re.search(r"\btds\b", d):
        return "tds"
    if units is None or abs(units) < EPS:
        return "other"
    if re.search(r"revers|reject", d):
        return "reversal"
    return "buy" if units > 0 else "sell"


def parse_cas(data: bytes, password: Optional[str] = None) -> dict:
    """
    A CAMS / KFintech CAS PDF → schemes with their transactions and the
    statement's closing line. Works on visual rows, so it does not depend on
    the PDF's text order.
    """
    doc = _open_pdf(data, password)
    try:
        lines = pdf_lines(doc)
    finally:
        doc.close()
    period = None
    amc = None
    folio = pan = holder = None
    expect_holder = False
    buf: List[str] = []
    schemes: List[dict] = []
    cur: Optional[dict] = None
    errors: List[str] = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if period is None:
            m = _PERIOD.search(line)
            if m:
                period = (_cas_date(m.group(1)), _cas_date(m.group(2)))
                continue
        if len(line) <= 60 and _AMC.match(line) and not re.search(r"\d", line):
            amc, buf, cur = line, [], None
            continue
        m = _FOLIO.search(line)
        if m and line.lower().startswith("folio"):
            folio = re.sub(r"\s+", "", m.group(1))
            pm = _PAN.search(line)
            pan = pm.group(1) if pm else None
            buf, cur, expect_holder = [], None, True
            continue
        im = _ISIN.search(line)
        if im:
            j = None
            for k in range(len(buf) - 1, -1, -1):
                if _CODE_PREFIX.match(buf[k]):
                    j = k
                    break
            text = " ".join((buf[j:] if j is not None else []) + [line])
            cur = {"amc": amc, "folio": folio or "", "pan": pan, "holder": holder,
                   "scheme": _clean_scheme(text), "isin": im.group(1),
                   "opening_units": None, "txns": [], "closing": {}}
            schemes.append(cur)
            buf, expect_holder = [], False
            continue
        if expect_holder and not _CODE_PREFIX.match(line):
            holder, expect_holder = line, False
            continue
        expect_holder = False
        if cur is None:
            buf.append(line)
            continue
        m = re.search(r"Opening Unit Balance\s*:\s*([\d,.()\-]+)", line, re.I)
        if m:
            cur["opening_units"] = _num(m.group(1))
            continue
        m = _TXN.match(line)
        if m and not _PERIOD.search(line):
            d = _cas_date(m.group(1))
            toks = m.group(2).split()
            nums: List[float] = []
            while toks and _NUMTOK.match(toks[-1]) and len(nums) < 4:
                nums.insert(0, _num(toks.pop()))
            desc = " ".join(toks).strip(" *") or "—"
            amount = units = nav = bal = None
            if len(nums) == 4:
                amount, units, nav, bal = nums
            elif len(nums) == 3:
                amount, units, bal = nums
                nav = abs(amount / units) if units else None
            elif len(nums) == 2:
                amount, units = nums
            elif len(nums) == 1:
                amount = nums[0]
            if d and nums:
                cur["txns"].append({"date": d, "desc": desc[:200], "amount": amount, "units": units,
                                    "nav": nav, "balance": bal, "kind": _kind(desc, units, len(nums))})
            continue
        if "Closing Unit Balance" in line or cur.get("closing"):
            # The closing line, sometimes wrapped; once its market value is
            # read, what follows is the scheme's footnote and the next name.
            c = cur["closing"]
            m = re.search(r"Closing Unit Balance\s*:\s*([\d,.]+)", line)
            if m:
                c["units"] = _num(m.group(1))
            m = re.search(rf"NAV on ({_DATE})\s*:\s*INR\s*([\d,.]+)", line)
            if m:
                c["nav_date"], c["nav"] = _cas_date(m.group(1)), _num(m.group(2))
            m = re.search(r"Cost Value\s*:\s*(?:INR\s*)?([\d,.]+)", line)
            if m:
                c["cost"] = _num(m.group(1))
            m = re.search(r"Market Value on .*?:\s*INR\s*([\d,.]+)", line)
            if m:
                c["value"] = _num(m.group(1))
            if "units" in c and "value" in c:
                cur = None
            continue
        buf.append(line)
    if not schemes:
        raise ValueError("No schemes found — this does not look like a CAMS / KFintech Consolidated "
                         "Account Statement (the detailed version, with transactions).")
    for s in schemes:
        bal = s["opening_units"] or 0.0
        for t in s["txns"]:
            if t["kind"] in ("buy", "sell", "reversal") and t["units"] is not None:
                bal += t["units"]
                if t["balance"] is not None and abs(bal - t["balance"]) > 0.01:
                    errors.append(f"{s['scheme']} ({s['folio']}): running balance {t['balance']:.3f} on "
                                  f"{t['date']} does not follow from the lines read ({bal:.3f})")
                    bal = t["balance"]
    return {"period_from": period[0] if period else None, "period_to": period[1] if period else None,
            "schemes": schemes, "errors": errors[:20]}


def _uid(pan, folio, isin, t, occ) -> str:
    key = "|".join(str(x) for x in (pan, folio, isin, t["date"], t["kind"],
                                    None if t["amount"] is None else round(t["amount"], 2),
                                    None if t["units"] is None else round(t["units"], 3),
                                    None if t["balance"] is None else round(t["balance"], 3), occ))
    return hashlib.sha1(key.encode()).hexdigest()


def _default_name(db, holder: Optional[str]) -> Optional[str]:
    """A new PAN joins the only tracking name there is; with several, it starts as the holder's name."""
    from app.services import portfolio as P
    names = sorted({a["name"] for a in P.accounts_map(db).values()})
    if len(names) == 1:
        return names[0]
    return (holder or "").title()[:60] or None


def store_cas(db, parsed: dict, name: Optional[str] = None) -> dict:
    from app.database import MfTransaction, MfSnapshot
    from app.services import portfolio as P
    added = dup = 0
    pans = set()
    existing = {u for (u,) in db.query(MfTransaction.uid).all()}
    for s in parsed["schemes"]:
        pan = s["pan"] or ("MF-" + re.sub(r"[^A-Z]", "", (s["holder"] or "UNKNOWN").upper())[:20])
        pans.add(pan)
        P.ensure_account(db, pan, BROKER, name or _default_name(db, s.get("holder")))
        occ: Dict[tuple, int] = defaultdict(int)
        for seq, t in enumerate(s["txns"]):
            k = (t["date"], t["kind"], t["amount"], t["units"], t["balance"])
            uid = _uid(pan, s["folio"], s["isin"], t, occ[k])
            occ[k] += 1
            if uid in existing:
                dup += 1
                continue
            existing.add(uid)
            db.add(MfTransaction(uid=uid, account_id=pan, folio=s["folio"], amc=s["amc"], scheme=s["scheme"][:250],
                                 isin=s["isin"], txn_date=t["date"], seq=seq, kind=t["kind"],
                                 description=t["desc"], amount=t["amount"], units=t["units"], nav=t["nav"],
                                 balance=t["balance"], source="cas"))
            added += 1
        c = s["closing"]
        if c.get("units") is not None:
            snap = db.query(MfSnapshot).filter(MfSnapshot.account_id == pan, MfSnapshot.folio == s["folio"],
                                               MfSnapshot.isin == s["isin"]).first()
            # The later valuation wins; on the same date, the statement reaching further back.
            nd, od = c.get("nav_date") or "", (snap.nav_date or "") if snap else ""
            newer = (not snap or nd > od or
                     (nd == od and (parsed.get("period_from") or "9") <= (snap.period_from or "9")))
            if newer:
                if not snap:
                    snap = MfSnapshot(account_id=pan, folio=s["folio"], isin=s["isin"])
                    db.add(snap)
                snap.scheme, snap.amc = s["scheme"][:250], s["amc"]
                snap.period_from, snap.period_to = parsed.get("period_from"), parsed.get("period_to")
                snap.opening_units = s["opening_units"]
                snap.units, snap.nav, snap.nav_date = c.get("units"), c.get("nav"), c.get("nav_date")
                snap.cost_value, snap.market_value = c.get("cost"), c.get("value")
                snap.imported_at = datetime.utcnow()
    db.commit()
    return {"added": added, "duplicates": dup, "pans": sorted(pans), "schemes": len(parsed["schemes"]),
            "period_from": parsed.get("period_from"), "period_to": parsed.get("period_to"),
            "errors": parsed.get("errors") or []}


def import_cas(db, content_b64: str, password: Optional[str] = None, name: Optional[str] = None) -> dict:
    data = base64.b64decode(content_b64)
    if not data.startswith(b"%PDF"):
        raise ValueError("Not a PDF. Upload the CAS PDF that CAMS or KFintech emailed.")
    res = store_cas(db, parse_cas(data, password), name)
    refresh_navs_async(force=True)
    return res


# ─── AMFI NAVs and categories ───────────────────────────────────────────────

_nav_lock = threading.Lock()
_nav_state = {"last_attempt": 0.0, "last_ok": None, "error": None, "running": False}


def parse_navall(text: str) -> Dict[str, dict]:
    """
    AMFI's NAVAll.txt: a header, then category lines ("Open Ended Schemes(Equity
    Scheme - Sectoral/ Thematic)"), AMC lines, and `;`-separated rows. The column
    set has changed over time (Plan and Option were added), so columns are
    found by the header's names.
    """
    lines = text.splitlines()
    if not lines:
        return {}
    head = [h.strip().lower() for h in lines[0].split(";")]

    def col(*keys, default=None):
        for i, h in enumerate(head):
            if any(k in h for k in keys):
                return i
        return default
    i_name = col("scheme name", default=3)
    i_nav = col("net asset value", default=len(head) - 2)
    i_date = col("date", default=len(head) - 1)
    i_plan = col("plan")
    i_opt = col("option")
    out: Dict[str, dict] = {}
    category = amc = None
    for raw in lines[1:]:
        s = raw.strip()
        if not s:
            continue
        if ";" not in s:
            m = re.match(r"^(?:open|close|closed|interval)[^(]*\((.*)\)\s*$", s, re.I)
            if m:
                category = m.group(1).strip()
            else:
                amc = s
            continue
        p = [x.strip() for x in s.split(";")]
        if len(p) <= max(i_name, i_nav, i_date):
            continue
        try:
            nav = float(p[i_nav].replace(",", ""))
        except ValueError:
            continue
        try:
            nd = datetime.strptime(p[i_date], "%d-%b-%Y").date().isoformat()
        except ValueError:
            continue
        name = p[i_name]
        extra = [p[i] for i in (i_plan, i_opt) if i is not None and i < len(p) and p[i] and p[i] != "-"]
        if extra and not all(e.lower() in name.lower() for e in extra):
            name = f"{name} - {' - '.join(extra)}"
        for isin in (p[1], p[2]):
            isin = isin.strip().upper()
            if re.match(r"^[A-Z]{2}[A-Z0-9]{9}\d$", isin):
                out[isin] = {"scheme_code": p[0], "name": name, "amc": amc, "category": category,
                             "nav": nav, "nav_date": nd}
    return out


def fetch_navall() -> Dict[str, dict]:
    import requests
    last = None
    for url in NAV_URLS:
        try:
            r = requests.get(url, headers=_HEADERS, timeout=60)
            r.raise_for_status()
            data = parse_navall(r.text)
            if len(data) > 1000:
                return data
            last = f"{url}: only {len(data)} rows"
        except Exception as e:
            last = f"{url}: {e}"
    raise RuntimeError(f"AMFI NAV file unavailable ({last})")


def held_isins(db) -> List[str]:
    from app.database import MfTransaction, MfSnapshot
    s = {i for (i,) in db.query(MfTransaction.isin).distinct().all()}
    s |= {i for (i,) in db.query(MfSnapshot.isin).distinct().all()}
    return sorted(x for x in s if x)


def refresh_navs(db) -> dict:
    """Today's AMFI file → mf_navs for every scheme on file; the previous NAV is kept for the day's change."""
    from app.database import MfNav
    _nav_state["last_attempt"] = time.time()
    try:
        allnav = fetch_navall()
    except Exception as e:
        _nav_state["error"] = str(e)
        logger.warning(f"[MF] NAV refresh failed: {e}")
        return {"ok": False, "error": str(e)}
    updated = missing = 0
    for isin in held_isins(db):
        n = allnav.get(isin)
        if not n:
            missing += 1
            continue
        row = db.query(MfNav).filter(MfNav.isin == isin).first()
        if not row:
            row = MfNav(isin=isin)
            db.add(row)
        if row.nav_date and n["nav_date"] > row.nav_date:
            row.prev_nav, row.prev_nav_date = row.nav, row.nav_date
        row.scheme_code, row.name, row.amc, row.category = n["scheme_code"], n["name"][:250], n["amc"], n["category"]
        row.nav, row.nav_date, row.updated_at = n["nav"], n["nav_date"], datetime.utcnow()
        updated += 1
    db.commit()
    _nav_state["last_ok"] = datetime.now(IST).isoformat(timespec="seconds")
    _nav_state["error"] = None
    logger.info(f"[MF] NAVs refreshed: {updated} schemes, {missing} not in AMFI's file")
    return {"ok": True, "updated": updated, "missing": missing}


def refresh_navs_async(force: bool = False) -> bool:
    """Background refresh at most every 3 hours (or now, when forced); single-flight."""
    with _nav_lock:
        if _nav_state["running"]:
            return False
        if not force and time.time() - _nav_state["last_attempt"] < NAV_REFRESH_SECONDS:
            return False
        _nav_state["running"] = True
        _nav_state["last_attempt"] = time.time()

    def run():
        from app.database import SessionLocal
        db = SessionLocal()
        try:
            refresh_navs(db)
        except Exception as e:
            logger.warning(f"[MF] NAV refresh error: {e}")
        finally:
            db.close()
            _nav_state["running"] = False
    threading.Thread(target=run, daemon=True, name="mf-nav").start()
    return True


def nav_status() -> dict:
    return {"last_ok": _nav_state["last_ok"], "error": _nav_state["error"], "running": _nav_state["running"]}


# ─── Tax class ──────────────────────────────────────────────────────────────

TAX_CLASSES = ("equity", "debt", "other")
OVERRIDES_KEY = "mf_tax_class_overrides"
_DEBT_WORDS = ("liquid", "overnight", "money market", "gilt", "bond", "debt", "credit risk", "duration",
               "banking and psu", "banking & psu", "corporate", "floater", "floating rate", "treasury",
               "g-sec", "gsec", "sdl", "crisil ibx", "target maturity", "fixed maturity", "fmp",
               "income fund", "ultra short", "low duration", "short term fund", "dynamic bond")
_OTHER_WORDS = ("fof", "fund of fund", "overseas", "off-shore", "offshore", "international", "global",
                "nasdaq", "s&p 500", "us equity", "greater china", "gold", "silver", "multi asset",
                "multi-asset", "asset allocation")


def classify(category: Optional[str], name: str) -> Tuple[str, str]:
    """(class, why). Equity-oriented, specified (debt), or other."""
    c = (category or "").lower()
    n = (name or "").lower()
    if c:
        if "fof" in c or "fund of fund" in c:
            if any(k in n for k in _DEBT_WORDS):
                return "debt", f"AMFI category: {category}, a debt fund of funds by its name"
            return "other", f"AMFI category: {category}"
        if "equity scheme" in c or "elss" in c:
            return "equity", f"AMFI category: {category}"
        if "debt scheme" in c:
            return "debt", f"AMFI category: {category}"
        if "hybrid" in c:
            if any(k in c for k in ("aggressive", "arbitrage", "equity savings", "balanced advantage", "dynamic asset")):
                return "equity", f"AMFI category: {category} (usually equity-taxed — check the scheme's SID)"
            if "conservative" in c:
                return "debt", f"AMFI category: {category}"
            return "other", f"AMFI category: {category}"
    if any(k in n for k in _DEBT_WORDS):
        return "debt", "from the scheme's name" + (f" ({category})" if category else "")
    if any(k in n for k in _OTHER_WORDS):
        return "other", "from the scheme's name" + (f" ({category})" if category else "")
    return "equity", ("from the scheme's name" + (f" ({category})" if category else "")) if not c else f"AMFI category: {category}"


def _overrides(db) -> Dict[str, str]:
    from app.database import SystemSetting
    row = db.query(SystemSetting).filter(SystemSetting.key == OVERRIDES_KEY).first()
    try:
        return json.loads(row.value) if row and row.value else {}
    except Exception:
        return {}


def set_tax_class(db, isin: str, cls: Optional[str]) -> Dict[str, str]:
    from app.database import SystemSetting
    ov = _overrides(db)
    if cls in TAX_CLASSES:
        ov[isin] = cls
    else:
        ov.pop(isin, None)
    row = db.query(SystemSetting).filter(SystemSetting.key == OVERRIDES_KEY).first()
    if row:
        row.value = json.dumps(ov)
    else:
        db.add(SystemSetting(key=OVERRIDES_KEY, value=json.dumps(ov)))
    db.commit()
    return ov


def _term(cls: str, buy: Optional[date], sell: date) -> str:
    """LTCG / STCG (equity rates) or SLAB / LTCG_OTHER for the rest."""
    from app.services.portfolio import is_long_term
    if buy is None:
        return "UNKNOWN"
    if cls == "equity":
        return "LTCG" if is_long_term(buy, sell) else "STCG"
    if cls == "debt" and buy >= DEBT_SLAB_FROM:
        return "SLAB"
    if cls == "other" and buy >= DEBT_SLAB_FROM and sell < SPECIFIED_RULE_END:
        return "SLAB"
    months = 24 if sell >= RATE_CHANGE else 36
    try:
        threshold = buy.replace(year=buy.year + months // 12)
    except ValueError:
        threshold = buy.replace(year=buy.year + months // 12, day=28)
    return "LTCG_OTHER" if sell > threshold else "SLAB"


def _lt_threshold(cls: str, buy: date) -> Optional[date]:
    """The first day a unit bought on `buy` sells as long-term, or None (debt bought from Apr 2023)."""
    if cls == "debt" and buy >= DEBT_SLAB_FROM:
        return None
    years = 1 if cls == "equity" else 2
    try:
        return buy.replace(year=buy.year + years) + timedelta(days=1)
    except ValueError:
        return buy.replace(year=buy.year + years, day=28) + timedelta(days=1)


# ─── XIRR ───────────────────────────────────────────────────────────────────

def xirr(flows: List[Tuple[date, float]]) -> Optional[float]:
    """Annualised internal rate of return, in percent; None under 30 days or without both signs."""
    flows = [(d, a) for d, a in flows if abs(a) > EPS]
    if len(flows) < 2 or all(a > 0 for _, a in flows) or all(a < 0 for _, a in flows):
        return None
    d0 = min(d for d, _ in flows)
    if (max(d for d, _ in flows) - d0).days < 30:
        return None

    def npv(r):
        return sum(a / (1 + r) ** ((d - d0).days / 365.0) for d, a in flows)
    lo, hi = -0.9999, 10.0
    f_lo, f_hi = npv(lo), npv(hi)
    if f_lo * f_hi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        f = npv(mid)
        if abs(f) < 1e-6:
            break
        if f_lo * f < 0:
            hi, f_hi = mid, f
        else:
            lo, f_lo = mid, f
    return round(mid * 100, 2)


# ─── Ledger → lots, sales, holdings ─────────────────────────────────────────

def _scheme_info(db) -> Dict[str, dict]:
    """isin → name, amc, category, nav and the tax class (override, else derived)."""
    from app.database import MfNav, MfTransaction, MfSnapshot, MfPortfolio
    navs = {n.isin: n for n in db.query(MfNav).all()}
    names: Dict[str, dict] = {}
    for isin, scheme, amc in db.query(MfTransaction.isin, MfTransaction.scheme, MfTransaction.amc).distinct().all():
        names.setdefault(isin, {"scheme": scheme, "amc": amc})
    for s in db.query(MfSnapshot).all():
        names.setdefault(s.isin, {"scheme": s.scheme, "amc": s.amc})
    ov = _overrides(db)
    ports = {p.scheme_isin: p for p in db.query(MfPortfolio).all()}
    out = {}
    for isin, nm in names.items():
        n = navs.get(isin)
        cls, why = classify(n.category if n else None, nm["scheme"] or (n.name if n else ""))
        if isin in ov:
            cls, why = ov[isin], "set by hand"
        p = ports.get(isin)
        out[isin] = {"scheme": nm["scheme"], "amc": nm["amc"] or (n.amc if n else None),
                     "category": n.category if n else None, "tax_class": cls, "tax_why": why,
                     "overridden": isin in ov,
                     "nav": n.nav if n else None, "nav_date": n.nav_date if n else None,
                     "prev_nav": n.prev_nav if n else None, "prev_nav_date": n.prev_nav_date if n else None,
                     "portfolio": ({"as_of": p.as_of, "equity_pct": p.equity_pct, "filename": p.filename,
                                    "n": len(json.loads(p.holdings_json or "[]"))} if p else None)}
    return out


def compute(db, name: Optional[str] = None) -> dict:
    """FIFO per PAN, folio and scheme → open lots, realised sales and cash flows."""
    from app.database import MfTransaction, MfSnapshot
    from app.services import portfolio as P
    amap = P.accounts_map(db)
    info = _scheme_info(db)
    q = db.query(MfTransaction)
    txns = q.order_by(MfTransaction.txn_date, MfTransaction.seq, MfTransaction.id).all()
    snaps = {(s.account_id, s.folio, s.isin): s for s in db.query(MfSnapshot).all()}
    groups: Dict[tuple, list] = defaultdict(list)
    for t in txns:
        if name and P.name_of(amap, t.account_id) != name:
            continue
        groups[(t.account_id, t.folio, t.isin)].append(t)
    for k in snaps:
        if (not name or P.name_of(amap, k[0]) == name) and k not in groups:
            groups[k] = []
    lots_by: Dict[tuple, list] = {}
    realised: List[dict] = []
    flows: Dict[tuple, list] = defaultdict(list)
    warnings: List[str] = []
    for key, ts in groups.items():
        pan, folio, isin = key
        cls = (info.get(isin) or {}).get("tax_class", "equity")
        scheme = (info.get(isin) or {}).get("scheme") or isin
        lots: List[dict] = []
        last_buy_on: Dict[str, dict] = {}
        for t in ts:
            td = _d(t.txn_date)
            amt = abs(t.amount or 0.0)
            if t.kind == "stamp_duty":
                lot = last_buy_on.get(t.txn_date) or (lots[-1] if lots else None)
                if lot:
                    lot["cost"] += amt
                flows[key].append((td, -amt))
            elif t.kind == "stt":
                for r in reversed(realised):
                    if r["account"] == pan and r["isin"] == isin and r["sell_date"] == t.txn_date:
                        r["stt"] += amt * r["qty"] / max(r["_sell_units"], EPS)
                flows[key].append((td, -amt))
            elif t.kind == "buy" or (t.kind == "reversal" and (t.units or 0) > 0):
                lot = {"date": t.txn_date, "units": t.units, "cost": amt, "nav": t.nav}
                lots.append(lot)
                last_buy_on[t.txn_date] = lot
                flows[key].append((td, -amt))
            elif t.kind == "reversal":
                u = -(t.units or 0.0)
                hit = next((l for l in reversed(lots) if abs(l["units"] - u) < UNIT_EPS), None)
                if hit:
                    lots.remove(hit)
                else:
                    lots = _consume(lots, u)
                flows[key].append((td, amt))
            elif t.kind == "sell":
                u = -(t.units or 0.0)
                sale_nav = amt / u if u else (t.nav or 0.0)
                left = u
                while left > EPS and lots:
                    l = lots[0]
                    take = min(left, l["units"])
                    cost = l["cost"] * take / l["units"]
                    bd = _d(l["date"]) if l["date"] else None
                    realised.append({"account": pan, "folio": folio, "isin": isin, "key": isin, "broker": BROKER,
                                     "symbol": scheme, "scheme": scheme, "tax_class": cls,
                                     "buy_date": l["date"], "sell_date": t.txn_date, "qty": take,
                                     "buy_price": cost / take if take else 0.0, "sell_price": sale_nav,
                                     "buy_charges": 0.0, "sell_charges": 0.0, "stt": 0.0,
                                     "gain": take * sale_nav - cost, "tax_gain": take * sale_nav - cost,
                                     "term": _term(cls, bd, td), "_sell_units": u,
                                     "grandfathered": bool(bd and bd < date(2018, 2, 1) and cls == "equity")})
                    l["cost"] -= cost
                    l["units"] -= take
                    left -= take
                    if l["units"] <= UNIT_EPS:
                        lots.pop(0)
                if left > UNIT_EPS:
                    warnings.append(f"{scheme} (folio {folio}): {left:.3f} units redeemed on {t.txn_date} with no "
                                    f"purchase on file — the statement does not reach back to them. Download a CAS "
                                    f"from the first investment.")
                flows[key].append((td, amt))
        # Reconcile with the registrar's closing line.
        snap = snaps.get(key)
        held = sum(l["units"] for l in lots)
        if snap and snap.units is not None and (not ts or (snap.nav_date or "") >= ts[-1].txn_date):
            diff = snap.units - held
            if diff > UNIT_EPS:
                dated_cost = sum(l["cost"] for l in lots)
                rest = max(0.0, (snap.cost_value or 0.0) - dated_cost) if snap.cost_value else None
                lots.insert(0, {"date": None, "units": diff, "cost": rest if rest is not None else 0.0,
                                "nav": None, "undated": True, "cost_unknown": rest is None})
            elif diff < -UNIT_EPS:
                warnings.append(f"{scheme} (folio {folio}): the statement says {snap.units:.3f} units, the "
                                f"transactions add up to {held:.3f} — a redemption may be missing.")
                lots = _consume(lots, -diff)
        lots_by[key] = lots
    return {"lots": lots_by, "realised": realised, "flows": flows, "warnings": warnings,
            "info": info, "snaps": snaps, "amap": amap}


def _consume(lots: List[dict], units: float) -> List[dict]:
    out = [dict(l) for l in lots]
    left = units
    while left > EPS and out:
        l = out[0]
        take = min(left, l["units"])
        l["cost"] -= l["cost"] * take / l["units"] if l["units"] else 0
        l["units"] -= take
        left -= take
        if l["units"] <= UNIT_EPS:
            out.pop(0)
    return out


def holdings(db, name: Optional[str] = None, refresh: bool = True) -> dict:
    """Per tracking name and scheme: units, cost, NAV, value, gain, XIRR, lots by age."""
    from app.services import portfolio as P
    if refresh:
        refresh_navs_async()
    c = compute(db, name)
    info, amap, snaps = c["info"], c["amap"], c["snaps"]
    today = _today()
    agg: Dict[tuple, dict] = {}
    for (pan, folio, isin), lots in c["lots"].items():
        nm = P.name_of(amap, pan)
        a = agg.setdefault((nm, isin), {"name": nm, "accounts": set(), "folios": [], "isin": isin, "lots": [],
                                        "flows": []})
        a["accounts"].add(pan)
        a["folios"].append(folio)
        a["lots"].extend(lots)
        a["flows"].extend(c["flows"].get((pan, folio, isin), []))
        s = snaps.get((pan, folio, isin))
        if s and s.nav and (not a.get("snap_nav_date") or (s.nav_date or "") > a["snap_nav_date"]):
            a["snap_nav"], a["snap_nav_date"] = s.nav, s.nav_date
    rows = []
    for (nm, isin), a in agg.items():
        units = sum(l["units"] for l in a["lots"])
        if units <= UNIT_EPS:
            continue
        inf = info.get(isin) or {}
        cls = inf.get("tax_class", "equity")
        invested = sum(l["cost"] for l in a["lots"])
        nav, nav_date, src = inf.get("nav"), inf.get("nav_date"), "amfi"
        if a.get("snap_nav") and (not nav or (a.get("snap_nav_date") or "") > (nav_date or "")):
            nav, nav_date, src = a["snap_nav"], a["snap_nav_date"], "statement"
        value = nav * units if nav else None
        day = None
        if src == "amfi" and inf.get("prev_nav") and inf.get("prev_nav_date") and nav_date:
            gap = (_d(nav_date) - _d(inf["prev_nav_date"])).days
            if 0 < gap <= 4:
                day = (nav / inf["prev_nav"] - 1) * 100
        lots_view, lt_units, next_lt = [], 0.0, None
        for l in sorted(a["lots"], key=lambda l: l["date"] or ""):
            if l["date"]:
                bd = _d(l["date"])
                thr = _lt_threshold(cls, bd)
                lt = thr is not None and today >= thr
                if lt:
                    lt_units += l["units"]
                elif thr and (next_lt is None or thr.isoformat() < next_lt):
                    next_lt = thr.isoformat()
                lots_view.append({"date": l["date"], "qty": round(l["units"], 3), "days": (today - bd).days,
                                  "long_term": lt, "cost": round(l["cost"], 2)})
            else:
                lots_view.append({"date": None, "qty": round(l["units"], 3), "days": None, "long_term": None,
                                  "cost": round(l["cost"], 2)})
        fl = list(a["flows"])
        if value:
            fl.append((_d(nav_date) if nav_date else today, value))
        rows.append({
            "name": nm, "accounts": sorted(a["accounts"]), "folios": sorted(set(a["folios"])), "isin": isin,
            "scheme": inf.get("scheme") or isin, "amc": inf.get("amc"), "category": inf.get("category"),
            "tax_class": cls, "tax_why": inf.get("tax_why"), "overridden": inf.get("overridden", False),
            "units": round(units, 3), "invested": round(invested, 2),
            "avg_nav": invested / units if units else None,
            "nav": nav, "nav_date": nav_date, "nav_source": src if nav else None,
            "value": value, "pnl": (value - invested) if value is not None else None,
            "pnl_pct": ((value - invested) / invested * 100) if value is not None and invested else None,
            "day_change_pct": day, "day_pnl": (value * day / (100 + day)) if value and day is not None else None,
            "xirr": xirr(fl) if value else None,
            "lots": lots_view, "long_term_units": round(lt_units, 3), "next_long_term": next_lt,
            "undated_units": round(sum(l["units"] for l in a["lots"] if not l["date"]), 3),
            "portfolio": inf.get("portfolio"),
        })
    rows.sort(key=lambda r: -(r["value"] or r["invested"]))
    fy = P.fy_of(today)
    real = [r for r in c["realised"] if P.fy_of(_d(r["sell_date"])) == fy]

    def totals(rs, rl, flows):
        priced = [r for r in rs if r["value"] is not None]
        val = sum(r["value"] for r in priced) if priced else None
        fl = list(flows)
        if val:
            fl.append((today, val))
        return {"invested": sum(r["invested"] for r in rs), "value": val,
                "pnl": sum(r["pnl"] for r in priced) if priced else None,
                "priced_invested": sum(r["invested"] for r in priced),
                "day_pnl": (sum(r["day_pnl"] for r in priced if r["day_pnl"] is not None)
                            if any(r["day_pnl"] is not None for r in priced) else None),
                "unpriced": len(rs) - len(priced),
                "xirr": xirr(fl) if val else None,
                "realised_fy": fy, "realised_fy_gain": sum(x["gain"] for x in rl)}
    all_flows = [f for k, fs in c["flows"].items() for f in fs]
    by_name = []
    for nm in sorted({r["name"] for r in rows}):
        fl = [f for (pan, _, _), fs in c["flows"].items() if P.name_of(amap, pan) == nm for f in fs]
        by_name.append({"name": nm, **totals([r for r in rows if r["name"] == nm],
                                             [x for x in real if P.name_of(amap, x["account"]) == nm], fl)})
    return {"rows": rows, "totals": totals(rows, real, all_flows), "by_name": by_name,
            "warnings": c["warnings"], "nav": nav_status(),
            "statements": _statement_spans(db)}


def _statement_spans(db) -> List[dict]:
    from app.database import MfSnapshot
    from sqlalchemy import func
    return [{"account": a, "from": f, "to": t} for a, f, t in
            db.query(MfSnapshot.account_id, func.min(MfSnapshot.period_from), func.max(MfSnapshot.period_to))
            .group_by(MfSnapshot.account_id).all()]


def realised_rows(db, fy: str, name: Optional[str] = None) -> List[dict]:
    """The FY's redemptions, matched to purchases, in the shape portfolio.tax_summary uses."""
    from app.services import portfolio as P
    c = compute(db, name)
    return [{k: v for k, v in r.items() if not k.startswith("_")} for r in c["realised"]
            if r["buy_date"] and P.fy_of(_d(r["sell_date"])) == fy]


def financial_years(db) -> List[str]:
    from app.database import MfTransaction
    from app.services import portfolio as P
    return sorted({P.fy_of(_d(d)) for (d,) in db.query(MfTransaction.txn_date).distinct().all() if d})


def transactions(db, name: Optional[str] = None, isin: Optional[str] = None, limit: int = 2000) -> List[dict]:
    from app.database import MfTransaction
    from app.services import portfolio as P
    amap = P.accounts_map(db)
    q = db.query(MfTransaction)
    if isin:
        q = q.filter(MfTransaction.isin == isin)
    out = []
    for t in q.order_by(MfTransaction.txn_date.desc(), MfTransaction.seq.desc()).limit(limit * 3).all():
        nm = P.name_of(amap, t.account_id)
        if name and nm != name:
            continue
        out.append({"id": t.id, "date": t.txn_date, "scheme": t.scheme, "isin": t.isin, "folio": t.folio,
                    "kind": t.kind, "description": t.description, "amount": t.amount, "units": t.units,
                    "nav": t.nav, "balance": t.balance, "account": t.account_id, "name": nm})
        if len(out) >= limit:
            break
    return out


# ─── Fund portfolio disclosures ─────────────────────────────────────────────

_ANY_ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")
_EQ_ISIN = re.compile(r"^IN[E9][A-Z0-9]{4}01[A-Z0-9]{3}$")
_ISIN_IN_TEXT = re.compile(r"\b([A-Z]{2}[A-Z0-9]{9}\d)\b")
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep",
                                        "oct", "nov", "dec"], 1)}


def isin_kind(isin: str) -> str:
    """equity (an Indian share), foreign (a non-Indian security: overseas shares or fund units), other."""
    if _EQ_ISIN.match(isin):
        return "equity"
    if not isin.startswith("IN"):
        return "foreign"
    return "other"


def _month_end(y: int, m: int) -> date:
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    return nxt - timedelta(days=1)


def _find_date(text: str) -> Optional[str]:
    t = text.lower()
    m = re.search(r"(\d{1,2})(?:st|nd|rd|th)?[\s\-/.,]+([a-z]{3,9})[\s\-/.,]+(\d{4})", t)
    if m and m.group(2)[:3] in _MONTHS:
        try:
            return date(int(m.group(3)), _MONTHS[m.group(2)[:3]], int(m.group(1))).isoformat()
        except ValueError:
            pass
    m = re.search(r"([a-z]{3,9})\s+(\d{1,2}),?\s+(\d{4})", t)
    if m and m.group(1)[:3] in _MONTHS:
        try:
            return date(int(m.group(3)), _MONTHS[m.group(1)[:3]], int(m.group(2))).isoformat()
        except ValueError:
            pass
    m = re.search(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4})\b", t)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
        except ValueError:
            pass
    full = ("january", "february", "march", "april", "may", "june", "july", "august", "september",
            "october", "november", "december")
    for m in re.finditer(r"\b([a-z]{3,9})[\s\-,']+(\d{4})\b", t):
        w = m.group(1)
        if w in full or (len(w) == 3 and w in _MONTHS):
            return _month_end(int(m.group(2)), _MONTHS[w[:3]]).isoformat()
    return None


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _sheet_rows_xlsx(data: bytes) -> List[Tuple[str, List[list]]]:
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    out = []
    for ws in wb.worksheets:
        rows = []
        for i, r in enumerate(ws.iter_rows(values_only=True)):
            if i > 4000:
                break
            rows.append(list(r))
        out.append((ws.title, rows))
    wb.close()
    return out


def _sheet_rows_xls(data: bytes) -> List[Tuple[str, List[list]]]:
    import xlrd
    wb = xlrd.open_workbook(file_contents=data)
    out = []
    for sh in wb.sheets():
        rows = [sh.row_values(i) for i in range(min(sh.nrows, 4000))]
        out.append((sh.name, rows))
    return out


def _parse_sheet(sheet: str, rows: List[list]) -> Optional[dict]:
    hdr = None
    for i, r in enumerate(rows[:120]):
        cells = [_cell(c).lower() for c in r]
        if any("isin" in c for c in cells) and any(("%" in c or "net asset" in c or "to nav" in c) for c in cells):
            hdr = i
            break
    if hdr is None:
        return None
    head = [_cell(c).lower() for c in rows[hdr]]
    nxt = rows[hdr + 1] if hdr + 1 < len(rows) else []
    if nxt and not any(_ANY_ISIN.match(_cell(c).upper()) for c in nxt):
        # A second header line ("(Rs. in Lakhs)", "to NAV") under the first.
        n = max(len(head), len(nxt))
        head = [((head[j] if j < len(head) else "") + " " +
                 (_cell(nxt[j]).lower() if j < len(nxt) else "")).strip() for j in range(n)]

    def find(pred):
        for j, h in enumerate(head):
            if pred(h):
                return j
        return None
    i_isin = find(lambda h: "isin" in h)
    i_pct = find(lambda h: "%" in h and any(k in h for k in ("net asset", "nav", "aum", "net assets")) and "yield" not in h)
    if i_pct is None:
        i_pct = find(lambda h: "%" in h and not any(k in h for k in ("yield", "ytm", "coupon", "ytc")))
    if i_pct is None:
        i_pct = find(lambda h: "net asset" in h)
    i_name = find(lambda h: "name" in h and "isin" not in h)
    if i_name is None:
        i_name = find(lambda h: any(k in h for k in ("instrument", "issuer", "company", "security")) and "isin" not in h)
    i_ind = find(lambda h: "industry" in h or "sector" in h)
    if i_ind is None:
        i_ind = find(lambda h: "rating" in h)
    if i_isin is None or i_pct is None:
        return None
    items: Dict[str, dict] = {}
    for r in rows[hdr + 1:]:
        if i_isin >= len(r):
            continue
        isin = _cell(r[i_isin]).upper().replace(" ", "")
        if not _ANY_ISIN.match(isin):
            continue
        pct = _num(_cell(r[i_pct])) if i_pct < len(r) else None
        if pct is None:
            continue
        nm = _cell(r[i_name]) if i_name is not None and i_name < len(r) else ""
        ind = _cell(r[i_ind]) if i_ind is not None and i_ind < len(r) else ""
        it = items.setdefault(isin, {"isin": isin, "name": nm, "industry": ind or None, "pct": 0.0,
                                     "kind": isin_kind(isin)})
        it["pct"] += pct
    if not items:
        return None
    vals = [i["pct"] for i in items.values()]
    if sum(vals) <= 1.6 and max(vals) <= 1.0:          # stored as fractions with a % format
        for i in items.values():
            i["pct"] *= 100
    pre = " ".join(_cell(c) for r in rows[:hdr] for c in r if isinstance(c, str) and c.strip())[:1500]
    dt = None
    for r in rows[:hdr]:
        for c in r:
            if isinstance(c, (datetime, date)):
                dt = (c.date() if isinstance(c, datetime) else c).isoformat()
                break
        if dt:
            break
    dt = _find_date(pre) or dt
    titles = [_cell(c) for r in rows[:hdr] for c in r if isinstance(c, str) and "fund" in c.lower() and len(c) < 250]
    title = next((t for t in titles if not re.search(r"mutual\s+fund\s*$", t, re.I)), None) \
        or (titles[0] if titles else sheet)
    return {"sheet": sheet, "title": title.strip(), "text": f"{sheet} {pre}", "as_of": dt,
            "holdings": list(items.values())}


def _parse_pdf_portfolio(data: bytes) -> List[dict]:
    """One section per run of pages under the same scheme title; equity rows are the ISIN lines."""
    doc = _open_pdf(data)
    try:
        pages = []
        for p in doc:
            sub = fitz_page_lines(p)
            pages.append(sub)
    finally:
        doc.close()
    sections: List[dict] = []
    cur = None
    for lines in pages:
        title = next((l for l in lines[:15] if "fund" in l.lower() and not l.lower().rstrip().endswith("mutual fund")
                      and not _ISIN_IN_TEXT.search(l)), None)
        if cur is None or (title and _norm(title) != _norm(cur["title"])):
            cur = {"sheet": f"page {len(sections) + 1}", "title": title or "", "text": " ".join(lines[:15]),
                   "as_of": _find_date(" ".join(lines[:20])), "items": {}}
            sections.append(cur)
        for l in lines:
            m = _ISIN_IN_TEXT.search(l)
            if not m:
                continue
            isin = m.group(1)
            before, after = l[:m.start()].strip(), l[m.end():].strip().split()
            text, nums = [], []
            for tok in after:
                v = _num(tok) if re.match(r"^\(?-?[\d,]*\.?\d+%?\)?$", tok) else None
                if v is None and not nums:
                    text.append(tok)
                elif v is not None:
                    nums.append(v)
            pct = next((v for v in reversed(nums) if abs(v) <= 100), None)
            if pct is None:
                continue
            it = cur["items"].setdefault(isin, {"isin": isin, "name": before, "industry": " ".join(text) or None,
                                                "pct": 0.0, "kind": isin_kind(isin)})
            it["pct"] += pct
    return [{"sheet": s["sheet"], "title": s["title"], "text": s["text"], "as_of": s["as_of"],
             "holdings": list(s["items"].values())} for s in sections if s["items"]]


def fitz_page_lines(page) -> List[str]:
    rows: List[list] = []
    for w in sorted(page.get_text("words"), key=lambda w: ((w[1] + w[3]) / 2, w[0])):
        yc = (w[1] + w[3]) / 2
        if rows and abs(rows[-1][0] - yc) <= 2.5:
            rows[-1][1].append(w)
        else:
            rows.append([yc, [w]])
    return [" ".join(w[4] for w in sorted(ws, key=lambda w: w[0])) for _, ws in rows]


def parse_portfolio_file(filename: str, data: bytes) -> List[dict]:
    """Every scheme section in an AMC's monthly portfolio file (xlsx, xls, pdf, or a zip of them)."""
    fn = (filename or "").lower()
    if data[:2] == b"PK" and not fn.endswith((".xlsx", ".xlsm")):
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
            names = zf.namelist()
            if "[Content_Types].xml" not in names:
                out = []
                for n in names:
                    if n.lower().endswith((".xlsx", ".xls", ".xlsm", ".pdf")) and not n.startswith("__MACOSX"):
                        for s in parse_portfolio_file(n, zf.read(n)):
                            s["sheet"] = f"{n.rsplit('/', 1)[-1]} · {s['sheet']}"
                            out.append(s)
                return out
        except zipfile.BadZipFile:
            pass
    if data.startswith(b"%PDF"):
        return _parse_pdf_portfolio(data)
    if data[:2] == b"PK":
        sheets = _sheet_rows_xlsx(data)
    elif data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        sheets = _sheet_rows_xls(data)
    else:
        raise ValueError("Unrecognised file — upload the AMC's monthly portfolio as Excel (.xlsx / .xls), "
                         "PDF, or a zip of them.")
    out = []
    for name, rows in sheets:
        try:
            p = _parse_sheet(name, rows)
        except Exception as e:
            logger.info(f"[MF] sheet {name!r} unreadable: {e}")
            p = None
        if p:
            out.append(p)
    return out


_STOP = {"fund", "direct", "plan", "growth", "option", "regular", "the", "of", "and", "idcw", "scheme", "mutual",
         "an", "open", "ended", "dir", "gr", "reinvestment", "payout", "dividend", "formerly", "erstwhile",
         "merged", "portfolio", "statement", "as", "on", "in", "for", "with", "ltd", "limited", "co"}


def _norm(s: str) -> str:
    return " ".join(sorted(_tok(s)))


def _tok(s: str) -> set:
    s = re.sub(r"\([^)]*\)", " ", s or "")
    s = s.lower().replace("&", " and ")
    return {w for w in re.findall(r"[a-z0-9]+", s) if w not in _STOP and len(w) > 1}


def _match_score(scheme: str, amc: Optional[str], section: dict) -> Tuple[float, float, int]:
    held = _tok(scheme)
    core = held - _tok(amc or "") - {"mutual"}
    if not core:
        core = held
    st = _tok(section["text"]) | _tok(section["title"])
    cov_core = len(core & st) / len(core) if core else 0.0
    cov_all = len(held & st) / len(held) if held else 0.0
    return cov_core, cov_all, -len(_tok(section["title"]) or st)


def store_portfolio_file(db, filename: str, content_b64: str, scheme_isin: Optional[str] = None) -> dict:
    """Read the file, match its sections to schemes on file, and keep each matched scheme's holdings."""
    from app.database import MfPortfolio, StockIndustry
    data = base64.b64decode(content_b64)
    sections = parse_portfolio_file(filename, data)
    if not sections:
        raise ValueError("No holdings table found — the file needs a column of ISINs and a "
                         "'% to net assets' column.")
    info = _scheme_info(db)
    targets = {scheme_isin: info.get(scheme_isin) or {"scheme": scheme_isin, "amc": None}} if scheme_isin else info
    # Matching by name leaves out the fund house ("Banking and Financial
    # Services" exists at several), so a file is only matched to schemes of
    # the fund house it names.
    file_tokens = _tok(filename) | set().union(*(_tok(x["text"]) | _tok(x["title"]) for x in sections))
    matched, unmatched = [], []
    used = set()
    for isin, inf in targets.items():
        brand = next(iter(sorted(_tok(inf.get("amc") or "") - {"mutual"}, key=lambda w: (inf.get("amc") or "").lower().find(w))), None)
        if not scheme_isin and brand and brand not in file_tokens:
            continue
        scored = sorted(((_match_score(inf.get("scheme") or "", inf.get("amc"), s), k)
                         for k, s in enumerate(sections)), reverse=True)
        if not scored:
            continue
        (core, full, _), k = scored[0]
        forced = bool(scheme_isin) and (len(sections) == 1 or core >= 0.34)
        if not forced and core < 0.8:
            if scheme_isin:
                unmatched.append({"isin": isin, "scheme": inf.get("scheme"),
                                  "best": sections[k]["title"] or sections[k]["sheet"], "score": round(core, 2)})
            continue
        s = sections[k]
        used.add(k)
        eq = [h for h in s["holdings"] if h["kind"] in ("equity", "foreign")]
        eq_pct = sum(h["pct"] for h in eq)
        row = db.query(MfPortfolio).filter(MfPortfolio.scheme_isin == isin).first()
        prev = row.as_of if row else None
        if not row:
            row = MfPortfolio(scheme_isin=isin)
            db.add(row)
        as_of = s["as_of"] or (_today().replace(day=1) - timedelta(days=1)).isoformat()
        row.as_of, row.filename, row.sheet = as_of, filename[:250], s["sheet"][:120]
        row.title, row.equity_pct = (s["title"] or "")[:300], round(eq_pct, 2)
        row.holdings_json = json.dumps([{k2: h[k2] for k2 in ("isin", "name", "industry", "pct", "kind")} for h in eq])
        row.uploaded_at = datetime.utcnow()
        for h in eq:
            if h["kind"] == "equity" and h.get("industry"):
                ind = db.query(StockIndustry).filter(StockIndustry.isin == h["isin"]).first()
                if not ind:
                    db.add(StockIndustry(isin=h["isin"], industry=h["industry"][:120], source="amc"))
                elif ind.source != "amc" or ind.industry != h["industry"][:120]:
                    ind.industry, ind.source, ind.updated_at = h["industry"][:120], "amc", datetime.utcnow()
        db.flush()
        matched.append({"isin": isin, "scheme": inf.get("scheme"), "sheet": s["sheet"], "title": s["title"],
                        "as_of": as_of, "previous_as_of": prev, "stocks": len(eq), "equity_pct": round(eq_pct, 2),
                        "score": round(core, 2), "date_found": bool(s["as_of"])})
    db.commit()
    return {"sections": len(sections), "matched": matched, "unmatched": unmatched,
            "unused_sections": len(sections) - len(used)}


# ─── Industries for shares held directly ────────────────────────────────────

_ind_state = {"last_attempt": 0.0, "running": False, "error": None}


def refresh_industries_bse(db) -> dict:
    """BSE's scrip list carries an INDUSTRY per scrip: fills ISINs no fund file has classified."""
    from app.database import StockIndustry
    from app.services.registry_builder import fetch_bse_scrips
    rows = fetch_bse_scrips()
    have = {r.isin: r for r in db.query(StockIndustry).all()}
    added = 0
    for r in rows:
        isin = str(r.get("ISIN_NUMBER") or "").strip().upper()
        ind = str(r.get("INDUSTRY") or r.get("Industry") or "").strip()
        if not isin or not ind or ind in ("-", "NA"):
            continue
        cur = have.get(isin)
        if cur is None:
            db.add(StockIndustry(isin=isin, industry=ind[:120], source="bse"))
            added += 1
        elif cur.source == "bse" and cur.industry != ind[:120]:
            cur.industry = ind[:120]
    db.commit()
    return {"added": added, "rows": len(rows)}


def refresh_industries_async() -> bool:
    if _ind_state["running"] or time.time() - _ind_state["last_attempt"] < 24 * 3600:
        return False
    _ind_state["running"] = True
    _ind_state["last_attempt"] = time.time()

    def run():
        from app.database import SessionLocal
        db = SessionLocal()
        try:
            res = refresh_industries_bse(db)
            logger.info(f"[MF] industries from BSE: {res}")
        except Exception as e:
            _ind_state["error"] = str(e)
            logger.warning(f"[MF] BSE industry list failed: {e}")
        finally:
            db.close()
            _ind_state["running"] = False
    threading.Thread(target=run, daemon=True, name="mf-industry").start()
    return True


# ─── Exposure: shares held directly plus each fund's slice ──────────────────

def exposure(db, name: Optional[str] = None) -> dict:
    from app.database import MfPortfolio, StockIndustry
    from app.services import portfolio as P
    from app.services import symbol_registry as SR
    direct = P.holdings(db, name=name)
    funds = holdings(db, name=name, refresh=True)
    ports = {p.scheme_isin: p for p in db.query(MfPortfolio).all()}
    inds = {r.isin: r.industry for r in db.query(StockIndustry).all()}
    items: Dict[str, dict] = {}

    def item(key, isin, symbol, company):
        it = items.get(key)
        if not it:
            rec = SR.lookup(isin=isin) if isin else None
            it = items[key] = {"key": key, "isin": isin, "symbol": (rec.display_symbol if rec else None) or symbol,
                               "company": (rec.company_name if rec else None) or company or symbol or isin,
                               "industry": None, "direct_value": 0.0, "direct_invested": 0.0,
                               "fund_value": 0.0, "fund_invested": 0.0, "funds": [], "holders": set(),
                               "kind": "equity"}
        return it
    direct_total = 0.0
    for r in direct["rows"]:
        v = r["value"] if r["value"] is not None else r["invested"]
        direct_total += v
        key = (r["isin"] or "").upper() or f"SYM:{r['symbol']}"
        it = item(key, (r["isin"] or "").upper() or None, r["symbol"], r["symbol"])
        it["direct_value"] += v
        it["direct_invested"] += r["invested"]
        it["holders"].add(r["name"])
    fund_total = broken_total = 0.0
    rest = {"value": 0.0, "invested": 0.0, "funds": []}
    not_broken, fund_status = [], []
    today = _today()
    for h in funds["rows"]:
        v = h["value"] if h["value"] is not None else h["invested"]
        fund_total += v
        p = ports.get(h["isin"])
        if not p:
            not_broken.append({"isin": h["isin"], "scheme": h["scheme"], "amc": h["amc"], "value": v, "name": h["name"]})
            continue
        broken_total += v
        hold = json.loads(p.holdings_json or "[]")
        stale = bool(p.as_of and (today - _d(p.as_of)).days > STALE_PORTFOLIO_DAYS)
        fund_status.append({"isin": h["isin"], "scheme": h["scheme"], "as_of": p.as_of, "stale": stale,
                            "equity_pct": p.equity_pct, "stocks": len(hold)})
        share = 0.0
        for e in hold:
            pct = e.get("pct") or 0.0
            share += pct
            amt = v * pct / 100
            it = item(e["isin"], e["isin"], None, e.get("name"))
            if e.get("kind") == "foreign":
                it["kind"] = "foreign"
            if not it["industry"] and e.get("industry") and e.get("kind") == "equity":
                it["_fund_industry"] = e["industry"]
            it["fund_value"] += amt
            it["fund_invested"] += h["invested"] * pct / 100
            it["funds"].append({"scheme": h["scheme"], "isin": h["isin"], "name": h["name"],
                                "pct_of_fund": round(pct, 3), "amount": amt, "as_of": p.as_of})
            it["holders"].add(h["name"])
        left = max(0.0, 100.0 - share)
        if left > 0.01:
            rest["value"] += v * left / 100
            rest["invested"] += h["invested"] * left / 100
            rest["funds"].append({"scheme": h["scheme"], "isin": h["isin"], "name": h["name"],
                                  "pct_of_fund": round(left, 2), "amount": v * left / 100})
    total = direct_total + fund_total
    missing_ind = False
    rows = []
    for it in items.values():
        ind = inds.get(it["isin"] or "") or it.pop("_fund_industry", None)
        it.pop("_fund_industry", None)
        if it["kind"] == "foreign":
            ind = ind or "Overseas"
        if not ind:
            ind = "Unclassified"
            if it["direct_value"]:
                missing_ind = True
        it["industry"] = ind
        it["total"] = it["direct_value"] + it["fund_value"]
        it["invested"] = it["direct_invested"] + it["fund_invested"]
        it["pct"] = it["total"] / total * 100 if total else None
        it["holders"] = sorted(it["holders"])
        it["funds"].sort(key=lambda f: -f["amount"])
        rows.append(it)
    rows.sort(key=lambda r: -r["total"])
    if missing_ind:
        refresh_industries_async()
    by_ind: Dict[str, dict] = {}
    for r in rows:
        g = by_ind.setdefault(r["industry"], {"industry": r["industry"], "direct_value": 0.0, "fund_value": 0.0,
                                              "invested": 0.0, "stocks": []})
        g["direct_value"] += r["direct_value"]
        g["fund_value"] += r["fund_value"]
        g["invested"] += r["invested"]
        g["stocks"].append({"key": r["key"], "symbol": r["symbol"], "company": r["company"], "total": r["total"],
                            "direct_value": r["direct_value"], "fund_value": r["fund_value"]})
    industries = []
    for g in by_ind.values():
        g["total"] = g["direct_value"] + g["fund_value"]
        g["pct"] = g["total"] / total * 100 if total else None
        industries.append(g)
    if rest["value"] > 0:
        industries.append({"industry": "Debt, cash & others", "direct_value": 0.0, "fund_value": rest["value"],
                           "invested": rest["invested"], "total": rest["value"],
                           "pct": rest["value"] / total * 100 if total else None, "stocks": [], "rest": True,
                           "funds": rest["funds"]})
    if not_broken:
        nb = sum(x["value"] for x in not_broken)
        industries.append({"industry": "Funds not broken down yet", "direct_value": 0.0, "fund_value": nb,
                           "invested": 0.0, "total": nb, "pct": nb / total * 100 if total else None,
                           "stocks": [], "unknown": True, "funds": not_broken})
    industries.sort(key=lambda g: (g.get("unknown", False), -g["total"]))
    return {"total": total, "direct_total": direct_total, "fund_total": fund_total,
            "broken_down_total": broken_total, "rest": rest, "not_broken_down": not_broken,
            "funds": fund_status, "rows": rows, "industries": industries,
            "names": sorted({r["name"] for r in direct["rows"]} | {r["name"] for r in funds["rows"]}),
            "notes": [
                "Fund holdings are from each scheme's latest monthly portfolio, scaled to your units' value today. "
                "A fund that traded since then holds slightly different weights.",
                "Value is today's price for shares and the latest NAV for funds; shares without a live quote count at cost.",
                "Invested via funds is your cost in the fund times each stock's weight: an approximation, since the "
                "fund bought and sold its stocks at its own prices.",
                "Balanced-advantage and arbitrage funds hedge part of their shares with futures; the shares are "
                "shown gross here.",
            ]}
