"""
Check services/fno.py against the live code, with no database or network:
contract symbols, long and short FIFO, expiry settlement and the dated STT.

    python ops/check_fno.py
    docker exec stocks-app python ops/check_fno.py
"""
import os
import sys
from datetime import date
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from app.services import fno as F  # noqa: E402

fails = 0


def check(label, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok ' if ok else 'FAIL'} {label}: {got!r}" + ("" if ok else f" (expected {want!r})"))


c = F.parse_contract("NIFTY2481524000CE", "NFO")
check("weekly option", (c["underlying"], c["instrument"], c["expiry"], c["strike"]), ("NIFTY", "CE", "2024-08-15", 24000.0))
c = F.parse_contract("BANKNIFTY24AUGFUT", "NFO")
check("monthly future before Sep 2025 → last Thursday", (c["underlying"], c["instrument"], c["expiry"]), ("BANKNIFTY", "FUT", "2024-08-29"))
c = F.parse_contract("NIFTY25DEC26000PE", "NFO")
check("monthly NSE from Sep 2025 → last Tuesday", (c["instrument"], c["expiry"], c["strike"]), ("PE", "2025-12-30", 26000.0))
c = F.parse_contract("SENSEX25DEC85000CE", "BFO")
check("monthly BSE → last Thursday", c["expiry"], "2025-12-25")
c = F.parse_contract("SENSEX25O1680000CE", "BFO")
check("weekly October code", (c["expiry"], c["strike"]), ("2025-10-16", 80000.0))
c = F.parse_contract("NIFTY24AUGFUT", "NFO", expiry="2024-08-29")
check("stated expiry wins", (c["expiry"], c["expiry_estimated"]), ("2024-08-29", False))


def T(i, sym, d, side, q, p, inst="FUT", exp="2024-08-29"):
    return NS(id=i, account_id="A", broker="zerodha", symbol=sym, underlying="X", instrument=inst, expiry=exp,
              expiry_estimated=False, strike=None, exchange="NFO", instrument_key=None, trade_date=d,
              trade_time=None, side=side, quantity=q, price=p, order_id=None)


r = F.compute([T(1, "XFUT", "2024-08-01", "SELL", 10, 100), T(2, "XFUT", "2024-08-05", "BUY", 10, 90)], {}, {},
              today=date(2024, 9, 1))
check("short then cover: profit", [round(x["pnl"], 2) for x in r["realised"]], [100.0])
r = F.compute([T(1, "XFUT", "2024-08-01", "BUY", 10, 100), T(2, "XFUT", "2024-08-02", "SELL", 15, 110),
               T(3, "XFUT", "2024-08-03", "BUY", 5, 120)], {}, {}, today=date(2024, 8, 10))
check("long, flip short, cover", [round(x["pnl"], 2) for x in r["realised"]], [100.0, -50.0])
check("flat afterwards", r["positions"], [])
r = F.compute([T(1, "XCE", "2024-08-01", "BUY", 50, 20, inst="CE")], {}, {}, today=date(2024, 9, 1))
check("option open past expiry → worthless", [(x["pnl"], x["how"]) for x in r["realised"]], [(-1000.0, "expiry_assumed_zero")])
r = F.compute([T(1, "XCE", "2024-08-01", "BUY", 50, 20, inst="CE")], {"XCE": 35.0}, {}, today=date(2024, 9, 1))
check("settlement price entered", [x["pnl"] for x in r["realised"]], [750.0])
r = F.compute([T(1, "XFUT", "2024-08-01", "BUY", 10, 100)], {}, {}, today=date(2024, 9, 1))
check("future past expiry without a price stays open", (len(r["realised"]), len(r["positions"]), r["positions"][0]["expired"]), (0, 1, True))
check("STT options before Oct 2024", F._stt(True, date(2024, 9, 1)), 0.000625)
check("STT options from Apr 2026", F._stt(True, date(2026, 4, 1)), 0.0015)
check("STT futures Oct 2024 – Mar 2026", F._stt(False, date(2025, 6, 1)), 0.0002)
check("F&O segment from Zerodha", F.is_fno_segment("FO", "NFO"), True)
check("equity is not F&O", F.is_fno_segment("EQ", "NSE"), False)
from app.services import itr as I  # noqa: E402
check("ITR window: 15 Jun", I.period_of(date(2026, 6, 15)), 0)
check("ITR window: 16 Jun", I.period_of(date(2026, 6, 16)), 1)
check("ITR window: 15 Dec", I.period_of(date(2026, 12, 15)), 2)
check("ITR window: 2 Jan", I.period_of(date(2027, 1, 2)), 3)
check("ITR window: 15 Mar", I.period_of(date(2027, 3, 15)), 3)
check("ITR window: 16 Mar", I.period_of(date(2027, 3, 16)), 4)
check("new Act sections from FY 2026-27", [c["section"] for c in I.categories("2026-27")[:2]], ["Section 196", "Section 198"])
check("old Act sections for FY 2025-26", [c["section"] for c in I.categories("2025-26")[:2]], ["Section 111A", "Section 112A"])
x = I.build({"rows": [{"term": "LTCG", "sell_date": "2026-07-01", "tax_gain": 200000.0, "sale_value": 500000.0,
                       "cost": 300000.0, "isin": "INE1", "symbol": "A", "qty": 10}],
             "ltcg": {"exemption": 125000.0}}, "2026-27")
check("112A tax after the exemption, in its window", [a["special_rate_tax"] for a in x["advance_tax"]], [0.0, 9375.0, 0.0, 0.0, 0.0])
print(f"\n{fails} failure(s)")
sys.exit(1 if fails else 0)
