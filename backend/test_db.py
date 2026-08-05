import sqlite3
import os

db_path = "market_tracker.db"
if not os.path.exists(db_path):
    db_path = "backend/market_tracker.db"

db = sqlite3.connect(db_path)
print("=== HTMEDIA DATABASE RECORDS ===")
try:
    print("market_events:", db.execute("select count(*) from market_events where symbol='HTMEDIA'").fetchone()[0])
    for row in db.execute("select id, event_type, source, category, title from market_events where symbol='HTMEDIA'").fetchall():
        print("  ME:", row)
except Exception as e:
    print("Error querying market_events:", e)

try:
    print("company_filings:", db.execute("select count(*) from company_filings where symbol='HTMEDIA'").fetchone()[0])
    for row in db.execute("select id, filing_type, symbol, title, filed_at from company_filings where symbol='HTMEDIA'").fetchall():
        print("  CF:", row)
except Exception as e:
    print("Error querying company_filings:", e)

try:
    print("trade_ai_logs:", db.execute("select count(*) from trade_ai_logs where symbol='HTMEDIA'").fetchone()[0])
    for row in db.execute("select id, symbol, provider, nse_event_title, created_at from trade_ai_logs where symbol='HTMEDIA'").fetchall():
        print("  TL:", row)
except Exception as e:
    print("Error querying trade_ai_logs:", e)
db.close()
