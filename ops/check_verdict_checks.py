"""
Regression check for services/verdict_checks.py on the two filings that
prompted it (23 Oct 2026), plus a steady grower and the loss-base labels.
Imports the live code; no database or network.

    python ops/check_verdict_checks.py          # on the host checkout
    docker exec stocks-app python ops/check_verdict_checks.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from app.services import verdict_checks as V  # noqa: E402
from app.services.result_compare import change_label, shift_quarter  # noqa: E402


def hist(rev, op, pat, last="Jun 2026"):
    qs = [shift_quarter(last, -3 * k) for k in range(len(rev) - 1, -1, -1)]
    return {q: {"revenue": r, "ebitda": o, "pat": p} for q, r, o, p in zip(qs, rev, op, pat)}


# Screener's quarters, Sep 2023 .. Jun 2026 (the oldest four approximate).
SAKAR = hist([36, 38, 40, 41, 43, 43, 50, 53, 57.56, 70, 71, 72.97],
             [9, 10, 11, 11, 12, 12, 16, 13, 11.35, 19, 26, 21.25],
             [3, 4, 4, 4, 5, 5, 6, 5, 4.54, 10, 11, 10.28])
PONNI = hist([98, 120, 95, 55, 94.54, 115.74, 94.45, 60.37, 113.73, 151.35, 89.54, 91.87],
             [10, 6, 10, -6, 12.63, 4.16, 11.84, -8.75, 17.29, 9.31, 17.89, -1.78],
             [9, 3, 5, -3, 11.53, 2.41, 4.53, -2.68, 14.56, 9.47, 26.68, -1.23])
STEADY = hist([100 * 1.05 ** i for i in range(12)], [20 * 1.05 ** i for i in range(12)],
              [10 * 1.05 ** i for i in range(12)])

SAKAR_AI = {"revenue": {"current_qtr": "₹87.42 Cr", "previous_qtr": "₹72.97 Cr", "last_year_same_qtr": "₹57.56 Cr"},
            "ebitda": {"current_qtr": "₹24.35 Cr", "previous_qtr": "₹21.25 Cr", "last_year_same_qtr": "₹11.35 Cr"},
            "pat": {"current_qtr": "₹11.31 Cr", "previous_qtr": "₹10.28 Cr", "last_year_same_qtr": "₹4.54 Cr"}}
PONNI_AI = {"revenue": {"current_qtr": "₹159.56 Cr", "previous_qtr": "₹91.87 Cr", "last_year_same_qtr": "₹113.73 Cr"},
            "ebitda": {"current_qtr": "₹33.49 Cr", "previous_qtr": "-₹1.78 Cr", "last_year_same_qtr": "₹17.29 Cr"},
            "pat": {"current_qtr": "₹21.69 Cr", "previous_qtr": "-₹1.23 Cr", "last_year_same_qtr": "₹14.56 Cr"}}
# A quarter that clearly steps up on the steady grower.
STEADY_AI = {"revenue": {"current_qtr": "₹200 Cr", "previous_qtr": "₹171 Cr"},
             "ebitda": {"current_qtr": "₹48 Cr", "previous_qtr": "₹34.2 Cr"},
             "pat": {"current_qtr": "₹24 Cr", "previous_qtr": "₹17.1 Cr"}}
CLEAN = {"reaction_risk": "low: clear beat", "red_flags": []}
NO_PRICE = {"available": False, "flags": []}


def expect(h):
    V._history = lambda db, s, q, n=12, _h=h: _h
    return V.expectation(object(), "X", "Sep 2026")


def main():
    failures = []

    def check(cond, msg):
        print(("ok   " if cond else "FAIL ") + msg)
        if not cond:
            failures.append(msg)

    e = expect(SAKAR)
    check(not e["seasonal"], "SAKAR is not seasonal")
    final, c = V.run(None, "SAKAR", "Sep 2026", e, NO_PRICE, "BUY", SAKAR_AI, CLEAN)
    check(c["trend"]["verdict"] == "in line", f"SAKAR in line with its run-rate ({c['trend']['detail']})")
    check(c["trend"]["margin_bps"] == -127, "SAKAR EBITDA margin -127 bps QoQ")
    check(final == "NA" and c.get("gate"), "SAKAR BUY withheld even when the model sees no flags")

    e = expect(PONNI)
    check(e["seasonal"], f"PONNI is seasonal ({e['season_why']})")
    final, c = V.run(None, "PONNIERODE", "Sep 2026", e, NO_PRICE, "BUY", PONNI_AI, CLEAN)
    check(c["trend"]["verdict"] == "in line", f"PONNI judged YoY against its trailing year ({c['trend']['detail']})")
    check(final == "NA", "PONNI BUY withheld")

    e = expect(STEADY)
    check(not e["seasonal"], "steady grower is not seasonal")
    final, c = V.run(None, "STEADY", "Sep 2026", e, NO_PRICE, "BUY", STEADY_AI, CLEAN)
    check(c["trend"]["verdict"] == "above", f"a clear step-up is above trend ({c['trend']['detail']})")
    check(final == "BUY", "a clean, above-trend BUY stands")
    final, c = V.run(None, "STEADY", "Sep 2026", e, NO_PRICE, "BUY", STEADY_AI,
                     {"reaction_risk": "medium: in line", "red_flags": []})
    check(final == "NA", "medium reaction risk withholds a BUY")
    rich = {"available": True, "flags": ["rich valuation: P/E 80"], "priced_in": True}
    final, c = V.run(None, "STEADY", "Sep 2026", e, rich, "BUY", STEADY_AI, CLEAN)
    check(final == "BUY", "+40% above run-rate clears the raised bar at P/E 80")

    check(change_label(21.69, -1.23) == "turnaround", "loss -> profit reads 'turnaround'")
    check(change_label(-5, -10) == "loss narrowed", "smaller loss reads 'loss narrowed'")
    check(change_label(-2, 5) == "swung to loss", "profit -> loss reads 'swung to loss'")
    check(change_label(11.3, 10.3) == "", "an ordinary change keeps its percentage")

    print(f"\n{len(failures)} failure(s)")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
