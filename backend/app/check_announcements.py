import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from database import SessionLocal, TradeConfig
from services.trade_nse_poller import _get_trade_nse_session, _check_match, _is_recent_announcement
import json

db = SessionLocal()
configs = db.query(TradeConfig).all()
print("=== ARMED TRADE CONFIGS IN DB ===")
for c in configs:
    print(f"ID: {c.id} | Symbol: {c.symbol} | Date: {c.purchase_date} | Status: {c.status} | Trigger: '{c.trigger_subject}'")
db.close()

print("\n=== FETCHING LIVE NSE ANNOUNCEMENTS ===")
nse = _get_trade_nse_session()
anns = nse.fetch_announcements()
print(f"Total live announcements fetched: {len(anns)}")

for symbol in ["STOVEKRAFT", "KANSAINER"]:
    print(f"\n--- Checking announcements for #{symbol} ---")
    matches = [a for a in anns if isinstance(a, dict) and (str(a.get("symbol") or a.get("sm_name")).upper().strip() == symbol)]
    if not matches:
        print(f"No active announcements currently in NSE 24h feed for {symbol}")
    for a in matches:
        desc = a.get("desc") or a.get("subject") or a.get("an_desc")
        dt = a.get("an_dt") or a.get("bcastDate") or a.get("date")
        is_rec = _is_recent_announcement(a, max_age_minutes=30)
        print(f"Symbol: {symbol} | Date: {dt} | Recent (<30m): {is_rec}")
        print(f"Subject/Desc: {desc}")
        print(f"Full RAW dict: {json.dumps(a)}")
        armed_dicts = [{
            "id": c.id,
            "symbol": c.symbol.upper().strip(),
            "trigger_subject": c.trigger_subject or "Outcome of Board Meeting",
        } for c in configs if c.symbol == symbol and c.status == 'armed']
        matched = _check_match(a, armed_dicts)
        print(f"Matched config: {matched}")
