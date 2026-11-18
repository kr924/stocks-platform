"""
The return-filing view of a financial year's gains: each kind of income by
the period it accrued in (the five windows of Schedule CG's "information
about accrual / receipt of capital gain", which are also the advance-tax
windows), the section it falls under, and the figures each ITR schedule asks
for — including Schedule 112A's scrip-wise rows.

Built from portfolio.tax_summary's result, so every number here is the same
number the Capital gains tab shows, regrouped.

Sections are the Income-tax Act 1961's up to FY 2025-26 and the Income-tax
Act 2025's from FY 2026-27 ("tax year"), where the new numbers are known:
196 (old 111A), 197 (old 112), 198 (old 112A). Rates by sale date: 15% / 10%
before 23 Jul 2024, 20% / 12.5% from then.
"""
import csv
import io
from datetime import date, datetime
from typing import Dict, List, Optional

RATE_CHANGE = date(2024, 7, 23)
NEW_ACT_FROM = "2026-27"

PERIODS = [
    {"key": "p1", "label": "1 Apr – 15 Jun", "pay_by": "15 Jun", "cum_pct": 15},
    {"key": "p2", "label": "16 Jun – 15 Sep", "pay_by": "15 Sep", "cum_pct": 45},
    {"key": "p3", "label": "16 Sep – 15 Dec", "pay_by": "15 Dec", "cum_pct": 75},
    {"key": "p4", "label": "16 Dec – 15 Mar", "pay_by": "15 Mar", "cum_pct": 100},
    {"key": "p5", "label": "16 Mar – 31 Mar", "pay_by": "31 Mar", "cum_pct": 100},
]


def period_of(d: date) -> int:
    md = (d.month, d.day)
    if (4, 1) <= md <= (6, 15):
        return 0
    if (6, 16) <= md <= (9, 15):
        return 1
    if (9, 16) <= md <= (12, 15):
        return 2
    if md >= (12, 16) or md <= (3, 15):
        return 3
    return 4


def _d(s: str) -> date:
    return datetime.strptime(s[:10], "%Y-%m-%d").date()


def _new_act(fy: str) -> bool:
    return fy >= NEW_ACT_FROM


def categories(fy: str) -> List[dict]:
    new = _new_act(fy)
    act = "Income-tax Act 2025" if new else "Income-tax Act 1961"
    old_rates, split = fy <= "2023-24", fy == "2024-25"
    r_st = "15%" if old_rates else "20% (15% for sales before 23 Jul 2024)" if split else "20%"
    r_lt = ("10% above ₹1 lakh a year" if old_rates else
            "12.5% above ₹1.25 lakh a year (10% for sales before 23 Jul 2024)" if split else "12.5% above ₹1.25 lakh a year")
    r_lo = ("20% with indexation" if old_rates else
            "12.5% without indexation (20% with indexation for some sales before 23 Jul 2024)" if split
            else "12.5% without indexation")
    return [
        {"key": "stcg_eq", "head": "Capital gains", "label": "Short-term — listed shares, equity funds & ETFs (STT paid)",
         "section": "Section 196" if new else "Section 111A", "act": act,
         "rate": r_st,
         "where": "ITR-2/3 Schedule CG, Part A — STCG on equity shares / units of equity-oriented funds where STT is "
                  "paid; also Schedule SI (special-rate income)"},
        {"key": "ltcg_eq", "head": "Capital gains", "label": "Long-term — listed shares, equity funds & ETFs (STT paid)",
         "section": "Section 198" if new else "Section 112A", "act": act,
         "rate": r_lt,
         "where": "Schedule 112A (scrip-wise: ISIN, units, sale value, cost, FMV on 31 Jan 2018 for older lots, "
                  "transfer expenses), then Schedule CG Part B and Schedule SI"},
        {"key": "stcg_other", "head": "Capital gains", "label": "Short-term — debt / gold / overseas funds & ETFs (at slab)",
         "section": ("deemed short-term (specified mutual fund) — at slab rates" if new
                     else "Section 50AA (debt funds bought from 1 Apr 2023) / section 48 — at slab rates"),
         "act": act, "rate": "your slab rate",
         "where": "Schedule CG, Part A — STCG on other assets (taxed at normal rates)"},
        {"key": "ltcg_other", "head": "Capital gains", "label": "Long-term — non-equity funds & ETFs",
         "section": "Section 197" if new else "Section 112", "act": act,
         "rate": r_lo,
         "where": "Schedule CG, Part B — LTCG on other assets; Schedule SI"},
        {"key": "intraday", "head": "Business income — speculative",
         "label": "Intraday equity (same-day buy and sell)",
         "section": ("speculative business; loss set off only against speculative income, carried forward 4 years "
                     "(section 113)" if new else
                     "Section 43(5) speculative business; loss set off only against speculative income, carried "
                     "forward 4 years (section 73)"),
         "act": act, "rate": "your slab rate",
         "where": "ITR-3 Schedule BP — speculative business (turnover, gross profit, expenses, net); Schedule CFL "
                  "for a loss"},
        {"key": "fno", "head": "Business income — non-speculative", "label": "Futures & options",
         "section": ("non-speculative business; tax audit above the limits (section 63)" if new else
                     "Business income — exchange-traded derivatives are not speculative (proviso (d) to section "
                     "43(5)); tax audit under section 44AB above the limits"),
         "act": act, "rate": "your slab rate",
         "where": "ITR-3 Schedule BP / Part A-P&L (no-account case: turnover, gross profit, expenses, net profit); "
                  "Schedule CFL for a loss (8 years)"},
    ]


def _rate_st(d: date) -> float:
    return 0.15 if d < RATE_CHANGE else 0.20


def _rate_lt(d: date) -> float:
    return 0.10 if d < RATE_CHANGE else 0.125


def build(tax: dict, fy: str) -> dict:
    """Regroup a tax_summary result by category and accrual period, with the ITR schedules' figures."""
    cats = categories(fy)
    zero = lambda: {"periods": [0.0] * 5, "sale_value": 0.0, "cost": 0.0, "expenses": 0.0, "stt": 0.0,
                    "net": 0.0, "turnover": 0.0, "count": 0, "gains": 0.0, "losses": 0.0}
    agg: Dict[str, dict] = {c["key"]: zero() for c in cats}
    split = fy == "2024-25"
    rate_split: Dict[str, Dict[str, float]] = {"stcg_eq": {"before": 0.0, "after": 0.0},
                                               "ltcg_eq": {"before": 0.0, "after": 0.0}}
    s112a: Dict[tuple, dict] = {}
    for r in tax.get("rows") or []:
        term = r.get("term")
        key = "stcg_eq" if term == "STCG" else "ltcg_eq" if term == "LTCG" else "intraday" if term == "INTRADAY" else None
        if not key:
            continue
        sd = _d(r["sell_date"])
        g = r.get("tax_gain") if r.get("tax_gain") is not None else r.get("gain", 0.0)
        a = agg[key]
        a["periods"][period_of(sd)] += g
        a["net"] += g
        a["count"] += 1
        a["gains" if g >= 0 else "losses"] += g
        a["sale_value"] += r.get("sale_value") or 0.0
        a["cost"] += (r.get("cost") or 0.0) + (r.get("buy_charges") or 0.0)
        a["expenses"] += r.get("sell_charges") or 0.0
        a["stt"] += r.get("stt") or 0.0
        a["turnover"] += abs(g)
        if key in rate_split:
            rate_split[key]["before" if sd < RATE_CHANGE else "after"] += g
        if key == "ltcg_eq":
            k = (r.get("isin") or r.get("symbol"), bool(r.get("grandfathered")))
            x = s112a.setdefault(k, {"isin": r.get("isin"), "name": r.get("symbol"), "acquired": (
                "on or before 31 Jan 2018" if r.get("grandfathered") else "after 31 Jan 2018"),
                "units": 0.0, "sale_value": 0.0, "cost": 0.0, "expenses": 0.0, "gain": 0.0,
                "fmv_31jan2018": None, "kind": r.get("asset") or "stock"})
            x["units"] += r.get("qty") or 0.0
            x["sale_value"] += r.get("sale_value") or 0.0
            x["cost"] += (r.get("cost") or 0.0) + (r.get("buy_charges") or 0.0)
            x["expenses"] += r.get("sell_charges") or 0.0
            x["gain"] += g
    for r in ((tax.get("mf_other") or {}).get("rows") or []):
        key = "stcg_other" if r.get("term") == "SLAB" else "ltcg_other"
        sd = _d(r["sell_date"])
        g = r.get("gain") or 0.0
        a = agg[key]
        a["periods"][period_of(sd)] += g
        a["net"] += g
        a["count"] += 1
        a["gains" if g >= 0 else "losses"] += g
        a["sale_value"] += r.get("sale_value") or 0.0
        a["cost"] += r.get("cost") or 0.0
        a["turnover"] += abs(g)
    fno = tax.get("fno") or {}
    if fno:
        a = agg["fno"]
        for i, p in enumerate(fno.get("periods") or []):
            a["periods"][i] += p.get("net", 0.0)
        a["net"] = fno.get("net", 0.0)
        a["turnover"] = fno.get("turnover", 0.0)
        a["count"] = fno.get("closed", 0)
        a["gross"] = fno.get("gross", 0.0)
        a["expenses"] = (fno.get("charges") or {}).get("total", 0.0)
        a["stt"] = (fno.get("charges") or {}).get("stt", 0.0)

    # Special-rate tax accrued per period: cumulative gains at the end of each
    # period, short-term loss set against long-term, the 112A/198 exemption
    # used first, then the difference from the period before.
    exemption = (tax.get("ltcg") or {}).get("exemption") or (100000.0 if fy <= "2023-24" else 125000.0)
    if tax.get("per_name") and len(tax["per_name"]) > 1:
        exemption = sum(p["exemption"] for p in tax["per_name"])
    rate_st = 0.20 if fy >= "2025-26" else 0.15 if fy <= "2023-24" else None
    rate_lt = 0.125 if fy >= "2025-26" else 0.10 if fy <= "2023-24" else None
    prev_tax, adv = 0.0, []
    for i in range(5):
        st = sum(agg["stcg_eq"]["periods"][: i + 1])
        lt = sum(agg["ltcg_eq"]["periods"][: i + 1])
        lo = sum(agg["ltcg_other"]["periods"][: i + 1])
        if st < 0:
            lt, st = lt + st, 0.0
        rst = rate_st if rate_st is not None else (0.15 if i == 0 else 0.20)
        rlt = rate_lt if rate_lt is not None else (0.10 if i == 0 else 0.125)
        cum = st * rst + max(0.0, lt - exemption) * rlt + max(0.0, lo) * 0.125
        adv.append({**PERIODS[i], "special_rate_tax": max(0.0, cum - prev_tax), "cumulative": cum,
                    "slab_income": sum(agg[k]["periods"][i] for k in ("stcg_other", "intraday", "fno"))})
        prev_tax = max(prev_tax, cum)

    has_business = bool(agg["intraday"]["count"] or tax.get("fno"))
    has_cg = any(agg[k]["count"] for k in ("stcg_eq", "ltcg_eq", "stcg_other", "ltcg_other"))
    only_small_112a = (has_cg and not has_business and not any(agg[k]["count"] for k in ("stcg_eq", "stcg_other", "ltcg_other"))
                       and 0 <= agg["ltcg_eq"]["net"] <= 125000)
    form = ("ITR-3 — you have business income (intraday and/or F&O)" if has_business
            else "ITR-2 — capital gains" + (" (ITR-1 also allows LTCG under 112A up to ₹1.25 lakh with no loss to "
                                           "carry forward — check the year's form)" if only_small_112a else "")
            if has_cg else "No gains or business income from trading this year")
    out_cats = []
    for c in cats:
        a = agg[c["key"]]
        if not a["count"] and not a["net"]:
            continue
        item = {**c, **{k: round(v, 2) if isinstance(v, float) else v for k, v in a.items() if k != "periods"},
                "periods": [round(x, 2) for x in a["periods"]]}
        if split and c["key"] in rate_split:
            item["rate_split"] = {k: round(v, 2) for k, v in rate_split[c["key"]].items()}
        out_cats.append(item)
    checklist = [
        f"Form: {form}.",
        "Schedule CG — for each kind of gain: full value of consideration (sale value), cost of acquisition "
        "(with buy-side charges), expenditure on transfer (sell-side charges except STT), and the gain — the "
        "'Sale value', 'Cost', 'Expenses' columns here.",
        "Schedule CG, 'Information about accrual/receipt of capital gain': each kind's gain in the five windows "
        "above (1 Apr–15 Jun, 16 Jun–15 Sep, 16 Sep–15 Dec, 16 Dec–15 Mar, 16 Mar–31 Mar). It decides interest "
        "under section 234C on advance tax.",
        "Schedule 112A — one row per scrip/fund for long-term equity sales: ISIN, name, units, sale price, full "
        "value of consideration, cost, and for units bought on or before 31 Jan 2018 their fair market value on "
        "31 Jan 2018 (not known here — take it from your broker's tax P&L or the exchange's closing price that day).",
        "Schedule SI — special-rate income is filled automatically from Schedule CG on the portal; check it.",
        "STT is not deductible from capital gains; it is deductible for intraday and F&O (business income).",
        "Intraday and F&O (ITR-3): turnover, gross profit, expenses and net profit go in Schedule BP / P&L "
        "(no-account case if no books). Check whether a tax audit applies at your turnover.",
        "Losses: a short-term capital loss sets off against any capital gain; a long-term loss only against "
        "long-term gains; both carry forward 8 years. A speculative loss only against speculative income (4 "
        "years); an F&O loss against any income except salary (8 years). File on time to carry forward.",
        "Reconcile with AIS / Form 26AS (sale values reported by brokers and registrars), your brokers' tax P&L "
        "and the CAMS/KFintech capital-gains statement before filing.",
        "Advance tax: gains are due in the instalment after the window they accrue in; a gain after 15 Mar can "
        "be paid by 31 Mar. Business income follows the usual 15/45/75/100% schedule on the year's estimate.",
    ]
    if _new_act(fy):
        checklist.append("From tax year 2026-27 the Income-tax Act 2025 applies: the same gains and rates, new "
                         "section numbers (196, 197, 198 for 111A, 112, 112A) and 'tax year' in place of "
                         "'previous year / assessment year'. The ITR forms for it are not out yet — the schedule "
                         "names here follow the current forms.")
    return {"fy": fy, "act": "Income-tax Act 2025" if _new_act(fy) else "Income-tax Act 1961",
            "assessment_year": None if _new_act(fy) else f"{int(fy[:4]) + 1}-{str(int(fy[:4]) + 2)[-2:]}",
            "periods": PERIODS, "categories": out_cats,
            "schedule_112a": sorted(({k: (round(v, 4) if isinstance(v, float) else v) for k, v in x.items()}
                                     for x in s112a.values()), key=lambda x: -(x["sale_value"])),
            "advance_tax": [{**a, "special_rate_tax": round(a["special_rate_tax"], 2),
                             "cumulative": round(a["cumulative"], 2), "slab_income": round(a["slab_income"], 2)}
                            for a in adv],
            "form": form, "checklist": checklist}


def to_csv(itr: dict) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow([f"FY {itr['fy']}", itr["act"], f"AY {itr['assessment_year']}" if itr.get("assessment_year") else ""])
    w.writerow([])
    w.writerow(["Kind", "Section", "Rate", "Where in the ITR", *[p["label"] for p in itr["periods"]], "Total",
                "Sale value", "Cost", "Expenses", "STT", "Turnover"])
    for c in itr["categories"]:
        w.writerow([c["label"], c["section"], c["rate"], c["where"], *c["periods"], c["net"],
                    c.get("sale_value", ""), c.get("cost", ""), c.get("expenses", ""), c.get("stt", ""),
                    c.get("turnover", "")])
    w.writerow([])
    w.writerow(["Schedule 112A"])
    w.writerow(["Acquired", "ISIN", "Name", "Units", "Sale price per unit", "Full value of consideration",
                "Cost of acquisition", "FMV on 31 Jan 2018", "Expenditure on transfer", "Balance (gain)"])
    for x in itr["schedule_112a"]:
        w.writerow([x["acquired"], x["isin"] or "", x["name"], x["units"],
                    round(x["sale_value"] / x["units"], 4) if x["units"] else "", x["sale_value"], x["cost"],
                    "fill in" if x["acquired"].startswith("on or before") else "", x["expenses"], x["gain"]])
    w.writerow([])
    w.writerow(["Advance tax on special-rate gains (estimate)"])
    w.writerow(["Window", "Pay by", "Tax accrued in the window", "Cumulative", "Slab-rate income in the window"])
    for a in itr["advance_tax"]:
        w.writerow([a["label"], a["pay_by"], a["special_rate_tax"], a["cumulative"], a["slab_income"]])
    w.writerow([])
    for line in itr["checklist"]:
        w.writerow([line])
    return out.getvalue()
