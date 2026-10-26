"""
Estimated brokerage and statutory charges for equity trades.

Neither Zerodha's tradebook CSV nor Upstox's trade-history API carries the
charges, so they are worked out from each broker's published schedule
(zerodha.com/charges, upstox.com/brokerage-charges, as of Oct 2026). They are
estimates: the contract note is the exact record. Rates that changed over
time are dated.

Per broker, per stock, per trading day (`day_charges`):
  delivery buy   brokerage (Upstox ₹20/order, Zerodha 0), STT 0.1%, exchange
                 transaction charge, SEBI ₹10/crore, stamp duty 0.015%,
                 GST 18% on brokerage + exchange + SEBI
  delivery sell  brokerage, STT 0.1%, exchange, SEBI, GST, and the DP charge
                 once per stock per day (Zerodha ₹15.34 incl. GST, Upstox
                 ₹20 + GST)
  intraday       brokerage min(₹20, 0.03% Zerodha / 0.1% Upstox) per order,
                 STT 0.025% on the sell, stamp 0.003% on the buy, exchange,
                 SEBI, GST; no DP charge

For capital gains every charge except STT is deductible (cost of acquisition
on the buy, expenses of transfer on the sale); STT is reported apart.
Orders are counted by distinct order id where the source gives one (Zerodha
CSV, Upstox today's trades), else one order per stock, side and day.
"""
from datetime import date
from typing import Dict, Iterable

GST = 0.18
SEBI = 10 / 1e7            # ₹10 per crore
STT_DELIVERY = 0.001
STT_INTRADAY_SELL = 0.00025
STAMP_DELIVERY_BUY = 0.00015
STAMP_INTRADAY_BUY = 0.00003

# NSE equity transaction charge by date (fraction of turnover).
_NSE_TXN = [(date(2026, 3, 1), 0.0000307), (date(2024, 10, 1), 0.0000297), (date(2000, 1, 1), 0.0000322)]
BSE_TXN = 0.0000375

BROKER = {
    "zerodha": {"delivery_per_order": 0.0, "intraday_pct": 0.0003, "intraday_cap": 20.0,
                "dp_per_scrip_day_incl_gst": 15.34},
    "upstox": {"delivery_per_order": 20.0, "intraday_pct": 0.001, "intraday_cap": 20.0,
               "dp_per_scrip_day_incl_gst": 20.0 * (1 + GST)},
}


def txn_rate(exchange: str, day: date) -> float:
    if (exchange or "NSE").upper().startswith("BSE"):
        return BSE_TXN
    for since, rate in _NSE_TXN:
        if day >= since:
            return rate
    return _NSE_TXN[-1][1]


def _orders(trades) -> int:
    ids = {t.order_id for t in trades if getattr(t, "order_id", None)}
    return len(ids) if ids else (1 if trades else 0)


def _brokerage(broker: str, trades, intraday_value: float, delivery: bool) -> float:
    cfg = BROKER.get(broker, BROKER["zerodha"])
    n = _orders(trades)
    if not n:
        return 0.0
    if delivery:
        return cfg["delivery_per_order"] * n
    per_order_value = intraday_value / n
    return n * min(cfg["intraday_cap"], per_order_value * cfg["intraday_pct"])


def day_charges(broker: str, day: date, buys: Iterable, sells: Iterable, intra_qty: float) -> Dict[str, float]:
    """
    Charges for one stock on one day at one broker, split by side and kind:
    {buy_other, buy_stt, sell_other, sell_stt, intra_other, intra_stt}
    ("other" = everything deductible: brokerage, exchange, SEBI, stamp, GST, DP).
    """
    buys, sells = list(buys), list(sells)
    bq = sum(t.quantity for t in buys)
    sq = sum(t.quantity for t in sells)
    bval = sum(t.quantity * t.price for t in buys)
    sval = sum(t.quantity * t.price for t in sells)
    exch = (buys or sells)[0].exchange if (buys or sells) else "NSE"
    rate = txn_rate(exch or "NSE", day)
    # Split each side between its intraday and delivery parts by quantity.
    bf_intra = intra_qty / bq if bq else 0.0
    sf_intra = intra_qty / sq if sq else 0.0
    b_intra_val, s_intra_val = bval * bf_intra, sval * sf_intra
    b_del_val, s_del_val = bval - b_intra_val, sval - s_intra_val

    def fees(value):            # exchange + SEBI, with GST on them
        x = value * rate + value * SEBI
        return x, x * GST

    out = {"buy_other": 0.0, "buy_stt": 0.0, "sell_other": 0.0, "sell_stt": 0.0,
           "intra_other": 0.0, "intra_stt": 0.0}
    if b_del_val > 0:
        brk = _brokerage(broker, buys, 0, True) * (1 - bf_intra)
        x, g = fees(b_del_val)
        out["buy_other"] = brk + brk * GST + x + g + b_del_val * STAMP_DELIVERY_BUY
        out["buy_stt"] = b_del_val * STT_DELIVERY
    if s_del_val > 0:
        brk = _brokerage(broker, sells, 0, True) * (1 - sf_intra)
        x, g = fees(s_del_val)
        out["sell_other"] = brk + brk * GST + x + g + BROKER.get(broker, BROKER["zerodha"])["dp_per_scrip_day_incl_gst"]
        out["sell_stt"] = s_del_val * STT_DELIVERY
    if intra_qty > 0:
        brk = (_brokerage(broker, buys, b_intra_val, False) * bf_intra
               + _brokerage(broker, sells, s_intra_val, False) * sf_intra)
        x, g = fees(b_intra_val + s_intra_val)
        out["intra_other"] = brk + brk * GST + x + g + b_intra_val * STAMP_INTRADAY_BUY
        out["intra_stt"] = s_intra_val * STT_INTRADAY_SELL
    return out
