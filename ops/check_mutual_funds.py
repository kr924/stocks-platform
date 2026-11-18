"""
Check services/mutual_funds.py against the live code, with no database or
network: AMFI's NAV file format, the tax class and holding-period rules,
XIRR — and, given files, what the parsers read from them.

    python ops/check_mutual_funds.py
    python ops/check_mutual_funds.py cas.pdf [--password X]      # a CAMS / KFintech statement
    python ops/check_mutual_funds.py hdfc-portfolio-oct.xlsx     # an AMC's month-end portfolio
    docker exec stocks-app python ops/check_mutual_funds.py /tmp/file.xlsx

Copy a file into the container first: docker cp file.xlsx stocks-app:/tmp/
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from app.services import mutual_funds as M  # noqa: E402

NAVALL = """Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Plan;Option;Net Asset Value;Date

Open Ended Schemes(Equity Scheme - Sectoral/ Thematic)

HDFC Mutual Fund

150000;INF179KC1HO1;-;HDFC Pharma and Healthcare Fund;Direct Plan;Growth Option;23.6100;14-Nov-2026

Open Ended Schemes(Other Scheme - FoF Overseas)

Edelweiss Mutual Fund

120000;INF843K01AU1;-;Edelweiss Greater China Equity Off-shore Fund;Direct Plan;Growth;N.A.;14-Nov-2026
"""

fails = 0


def check(label, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok ' if ok else 'FAIL'} {label}: {got!r}" + ("" if ok else f" (expected {want!r})"))


def builtin():
    nav = M.parse_navall(NAVALL)
    check("NAVAll row read", nav.get("INF179KC1HO1", {}).get("nav"), 23.61)
    check("NAVAll category", nav.get("INF179KC1HO1", {}).get("category"), "Equity Scheme - Sectoral/ Thematic")
    check("N.A. NAV skipped", "INF843K01AU1" in nav, False)
    check("sectoral = equity", M.classify("Equity Scheme - Sectoral/ Thematic", "x")[0], "equity")
    check("BAF = equity", M.classify("Hybrid Scheme - Balanced Advantage", "x")[0], "equity")
    check("FoF overseas = other", M.classify("Other Scheme - FoF Overseas", "x")[0], "other")
    check("gilt index = debt", M.classify("Other Scheme - Index Funds", "Nifty SDL Apr 2027 Index Fund")[0], "debt")
    check("no category, offshore = other", M.classify(None, "Greater China Equity Off-shore Fund")[0], "other")
    check("equity 12 months +1 day = LTCG", M._term("equity", date(2025, 9, 12), date(2026, 9, 13)), "LTCG")
    check("equity 12 months = STCG", M._term("equity", date(2025, 9, 12), date(2026, 9, 12)), "STCG")
    check("debt bought 2023-05 = slab", M._term("debt", date(2023, 5, 1), date(2028, 1, 1)), "SLAB")
    check("other, 24 months, sold 2025-06", M._term("other", date(2023, 1, 1), date(2025, 6, 1)), "LTCG_OTHER")
    check("other bought 2024, sold 2024 = slab", M._term("other", date(2024, 10, 4), date(2024, 10, 15)), "SLAB")
    check("XIRR 10% in a year", M.xirr([(date(2025, 1, 1), -100.0), (date(2026, 1, 1), 110.0)]), 10.0)
    check("equity ISIN", M.isin_kind("INE002A01018"), "equity")
    check("bond ISIN", M.isin_kind("INE040A08377"), "other")
    check("overseas ISIN", M.isin_kind("LU0117844026"), "foreign")
    check("date: as on October 31, 2026", M._find_date("Portfolio as on October 31, 2026"), "2026-10-31")
    check("date: 31st Oct 2026", M._find_date("as on 31st Oct 2026"), "2026-10-31")
    for ind, sec in [("Banks", "Financial Services"), ("Private Sector Bank", "Financial Services"),
                     ("IT - Software", "Information Technology"), ("Computers - Software & Consulting", "Information Technology"),
                     ("Healthcare Services", "Healthcare"), ("Edible Oil", "Fast Moving Consumer Goods"),
                     ("Environmental Services", "Services"), ("Cement & Cement Products", "Construction Materials"),
                     ("Agricultural, Commercial & Construction Vehicles", "Capital Goods"), ("Iron & Steel", "Metals & Mining")]:
        check(f"sector of {ind}", M.sector_of(ind), sec)
    check("gold ETF is gold", M.asset_class_of("Nippon India ETF Gold BeES", "Other Scheme - Gold ETF", "other"), "gold")
    check("liquid ETF is debt", M.asset_class_of("Nippon India ETF Nifty 1D Rate Liquid BeES", None, "debt"), "debt")
    check("date: month only", M._find_date("Monthly portfolio for October 2026"), "2026-10-31")


def show_file(path, password=None):
    data = open(path, "rb").read()
    if data.startswith(b"%PDF"):
        try:
            cas = M.parse_cas(data, password)
        except ValueError as e:
            cas = None
            print(f"  not a CAS ({e}); reading as a portfolio file")
        if cas:
            print(f"CAS {cas['period_from']} → {cas['period_to']}, {len(cas['schemes'])} schemes")
            for s in cas["schemes"]:
                print(f"  {s['amc']} | folio {s['folio']} | PAN {s['pan']} | {s['scheme']} [{s['isin']}]"
                      f" opening {s['opening_units']} closing {s['closing']}")
                for t in s["txns"]:
                    print(f"      {t['date']} {t['kind']:<10} {t['desc'][:40]:<40} {t['amount']} {t['units']} {t['nav']} {t['balance']}")
            for e in cas["errors"]:
                print("  !", e)
            return
    for sec in M.parse_portfolio_file(os.path.basename(path), data):
        eq = [h for h in sec["holdings"] if h["kind"] in ("equity", "foreign")]
        print(f"[{sec['sheet']}] {sec['title'][:90]!r} as of {sec['as_of']}: {len(eq)} shares, "
              f"{sum(h['pct'] for h in eq):.2f}% of the fund; {len(sec['holdings']) - len(eq)} other ISIN rows")
        for h in sorted(eq, key=lambda h: -h["pct"])[:8]:
            print(f"      {h['isin']} {h['pct']:6.2f}%  {h['name'][:45]:<45} {h['industry']}")


if __name__ == "__main__":
    args = sys.argv[1:]
    pw = None
    if "--password" in args:
        i = args.index("--password")
        pw = args[i + 1]
        del args[i:i + 2]
    if args:
        for a in args:
            print(f"── {a}")
            show_file(a, pw)
    else:
        builtin()
        print(f"\n{fails} failure(s)")
        sys.exit(1 if fails else 0)
