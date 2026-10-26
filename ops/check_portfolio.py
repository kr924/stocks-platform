"""
Regression check for services/portfolio.py and services/charges.py against
the live code: CSV parsing, FIFO/intraday matching, estimated charges, the
tax set-off, and reconciling the ledger with Upstox's holdings.
No database or network.

    python ops/check_portfolio.py
    docker exec stocks-app python ops/check_portfolio.py
"""
import os
import sys
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from app.services import portfolio as P  # noqa: E402

CSV = """Tradebook for Equity
symbol,isin,trade_date,exchange,segment,series,trade_type,auction,quantity,price,trade_id,order_id,order_execution_time
INFY,INE009A01021,2023-05-10,NSE,EQ,EQ,buy,false,10,1250.50,111,9001,2023-05-10T10:01:02
INFY,INE009A01021,2024-01-15,NSE,EQ,EQ,buy,false,5,1500,112,9002,2024-01-15T11:00:00
INFY,INE009A01021,2024-08-20,NSE,EQ,EQ,sell,false,12,1800,113,9003,2024-08-20T12:00:00
TCS,INE467B01029,2025-02-03,NSE,EQ,EQ,buy,false,2,4000,114,9004,2025-02-03T09:30:00
TCS,INE467B01029,2025-02-03,NSE,EQ,EQ,sell,false,2,4050,115,9005,2025-02-03T14:30:00
NIFTY24AUGFUT,,2024-08-01,NFO,FO,,buy,false,50,24000,116,9006,2024-08-01T10:00:00
"""


def T(uid, broker, sym, isin, d, side, q, p, acct=None):
    return NS(uid=uid, broker=broker, account_id=acct, symbol=sym, isin=isin, exchange="NSE", instrument_key=None, trade_date=d,
              trade_time=None, side=side, quantity=q, price=p, trade_id=uid, order_id=None,
              source="upstox_api" if broker == "upstox" else "zerodha_csv", note=None)


def main():
    fails = []

    def check(cond, msg):
        print(("ok   " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    check(P.client_id_from("tradebook-QY8357-EQ.csv") == "QY8357", "client ID read from Zerodha's file name")
    check(P.client_id_from("trades.csv", "Client ID,AB1234\nsymbol,isin") == "AB1234", "client ID read from a Client ID line")
    check(P.client_id_from("my-trades-2025.csv") is None, "no client ID invented from an ordinary file name")

    # Accounts: two Zerodha IDs under two names, Upstox under the first.
    amap = {"QY8357": {"broker": "zerodha", "name": "Koushik"}, "AB1234": {"broker": "zerodha", "name": "Wife"},
            "UP111": {"broker": "upstox", "name": "Koushik"}}
    P.accounts_map = lambda db: amap
    P.upstox_account = lambda db, feed=None: "UP111"
    P.acct_resolver = lambda db: (lambda t: t.account_id or ("UP111" if t.broker == "upstox" else ""))

    r = P.parse_csv(CSV, "zerodha", "QY8357")
    check(all(x["account_id"] == "QY8357" for x in r["rows"]), "every imported row carries the client ID")
    check(len(r["rows"]) == 5 and r["skipped"] == 1, "CSV: 5 equity rows read, the F&O row skipped")
    check([x["uid"] for x in r["rows"]] == [x["uid"] for x in P.parse_csv(CSV, "zerodha")["rows"]],
          "CSV: the same file gives the same uids (re-import adds nothing)")
    trades = [NS(**x) for x in r["rows"]]
    c = P.compute(trades)
    terms = sorted((x["term"], x["qty"]) for x in c["realised"])
    check(terms == [("INTRADAY", 2.0), ("LTCG", 10.0), ("STCG", 2.0)], f"FIFO: 10 long-term, 2 short-term, 2 intraday {terms}")
    lt = next(x for x in c["realised"] if x["term"] == "LTCG")
    check(0 < lt["buy_charges"] + lt["sell_charges"] < 30 and abs(lt["stt"] - 30.5) < 1,
          f"charges on the LTCG sale look right (deductible {lt['buy_charges'] + lt['sell_charges']:.2f}, STT {lt['stt']:.2f})")
    check(abs(lt["tax_gain"] - (lt["gain"] - lt["buy_charges"] - lt["sell_charges"])) < 1e-6,
          "taxable gain = gain − deductible charges (STT not deducted)")

    P._all_trades = lambda db, broker=None: [
        T("a", "upstox", "X", "INE000000001", "2025-05-01", "BUY", 100, 500),
        T("b", "upstox", "X", "INE000000001", "2025-09-01", "SELL", 100, 400),
        T("c", "upstox", "Y", "INE000000002", "2023-05-01", "BUY", 100, 100),
        T("d", "upstox", "Y", "INE000000002", "2025-10-01", "SELL", 100, 2000),
        T("e", "zerodha", "Y", "INE000000002", "2023-05-01", "BUY", 100, 100, "AB1234"),
        T("f", "zerodha", "Y", "INE000000002", "2025-10-01", "SELL", 100, 2000, "AB1234")]
    s = P.tax_summary(None, "2025-26", name="Koushik")
    check(s["short_term_loss_set_off_against_long_term"] > 0 and s["ltcg"]["exemption_used"] == 125000,
          "short-term loss set off against LTCG; ₹1.25 lakh exemption applied")
    w = P.tax_summary(None, "2025-26", name="Wife")
    check(abs(w["ltcg"]["gain"] - s["ltcg"]["gain"]) < 200 and w["stcg"]["net"] == 0
          and w["short_term_loss_set_off_against_long_term"] == 0 and w["ltcg"]["exemption_used"] == 125000,
          "each tracking name has its own gains, set-off and exemption")
    check(any("Tax is per person" in x for x in P.tax_summary(None, "2025-26")["warnings"]),
          "all names together carries the 'tax is per person' warning")
    # Same stock bought by two people at Zerodha: matched per account, not across them.
    P._all_trades = lambda db, broker=None: [
        T("g", "zerodha", "Z", "INE000000009", "2025-01-01", "BUY", 10, 100, "QY8357"),
        T("h", "zerodha", "Z", "INE000000009", "2025-02-01", "SELL", 10, 120, "AB1234")]
    cc = P.compute(P._all_trades(None), P.acct_resolver(None))
    check(len(cc["unmatched"]) == 1 and cc["unmatched"][0]["account"] == "AB1234",
          "one person's sale never matches another person's purchase")

    P._today = lambda: P._d("2026-10-26")
    trades2 = [T("1", "upstox", "SAKAR", "INE732S01012", "2026-10-23", "BUY", 15, 952),
               T("2", "upstox", "OLD", "INE000000AAA", "2025-01-10", "BUY", 10, 100),
               T("3", "upstox", "GONE", "INE000000BBB", "2025-02-10", "BUY", 5, 50),
               T("4", "upstox", "NEW", "INE000000CCC", "2026-10-26", "BUY", 7, 10)]
    P._all_trades = lambda db, broker=None: trades2
    P._instrument_key = lambda info: info.get("instrument_key") or ""
    row = lambda sym, isin, q, t1, avg: {"symbol": sym, "isin": isin, "instrument_key": f"NSE_EQ|{isin}", "exchange": "NSE",
                                          "company_name": "", "quantity": q, "t1_quantity": t1, "average_price": avg}
    P.upstox_holdings = lambda db, **kw: {"at": "2026-10-26T05:00:00Z", "changed": True, "rows": [
        row("SAKAR", "INE732S01012", 0, 15, 952), row("OLD", "INE000000AAA", 30, 0, 80), row("API", "INE000000DDD", 4, 0, 250)]}
    allpos = P.positions(None)["positions"]
    pos = {p["symbol"]: p for p in allpos}
    check(pos["SAKAR"]["brokers"]["upstox"]["source"] == "ledger", "ledger kept where it agrees with Upstox (T+1 shares)")
    check(pos["OLD"]["qty"] == 30 and any(l["date"] is None and l["qty"] == 20 for l in pos["OLD"]["lots"]),
          "Upstox's quantity wins; shares older than the history get an undated lot")
    check("GONE" not in pos, "a stock Upstox no longer holds is dropped")
    check(pos["NEW"]["qty"] == 7, "today's buy is kept until it becomes a holding at T+1")
    check(pos["API"]["qty"] == 4, "a holding with no trades on file comes from Upstox")
    check(all(h["name"] == "Koushik" for p in allpos for h in p["holders"]), "Upstox holdings carry the Upstox account's name")
    check(P.positions(None, name="Wife")["positions"] == [], "filtering by another name leaves Upstox out")

    print(f"\n{len(fails)} failure(s)")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
