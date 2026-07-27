import sys
import os

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app.database import SessionLocal, NewsItem
from sqlalchemy import func

db = SessionLocal()
try:
    print("Database counts by source:")
    counts = db.query(NewsItem.source, func.count(NewsItem.id)).group_by(NewsItem.source).all()
    for source, count in counts:
        print(f"- {source}: {count}")
        
    print("\nRecent 5 news items:")
    recent = db.query(NewsItem).order_by(NewsItem.published_at.desc()).limit(5).all()
    for idx, item in enumerate(recent):
        print(f"{idx+1}. [{item.source}] {item.headline} | {item.published_at}")
        
except Exception as e:
    print(f"Error: {e}")
finally:
    db.close()
