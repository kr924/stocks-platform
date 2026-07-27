import sys
import os
from datetime import datetime

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app.database import SessionLocal, NewsItem

db = SessionLocal()
try:
    print("Latest 10 news items overall in DB:")
    recent = db.query(NewsItem).order_by(NewsItem.fetched_at.desc()).limit(10).all()
    for idx, item in enumerate(recent):
        print(f"{idx+1}. [{item.source}] {item.headline[:60]}... | Published: {item.published_at} | Fetched: {item.fetched_at}")
except Exception as e:
    print(f"Error: {e}")
finally:
    db.close()
