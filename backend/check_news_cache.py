import sys
import os
import json

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app.database import SessionLocal, NewsCache

db = SessionLocal()
try:
    caches = db.query(NewsCache).all()
    print(f"Total News Cache entries: {len(caches)}")
    for c in caches:
        news = json.loads(c.news_json)
        sources = set(item.get("source") for item in news)
        print(f"- Stock: {c.instrument_key} | News Count: {len(news)} | Sources: {list(sources)}")
except Exception as e:
    print(f"Error: {e}")
finally:
    db.close()
