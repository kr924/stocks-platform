import sys
import os
from datetime import datetime, timedelta

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app.database import SessionLocal, NewsStory, NewsItem

db = SessionLocal()
try:
    since = datetime.utcnow() - timedelta(hours=24)
    print(f"Checking stories since {since}")
    stories = db.query(NewsStory).filter(NewsStory.last_published >= since).order_by(NewsStory.last_published.desc()).all()
    print(f"Found {len(stories)} stories in the last 24 hours:")
    for idx, s in enumerate(stories):
        items = db.query(NewsItem).filter(NewsItem.story_id == s.id).all()
        sources = [it.source for it in items]
        print(f"{idx+1}. [{s.id}] Headline: {s.headline[:60]}... | Published: {s.last_published} | Sources: {sources} | Sentiment: {s.ai_sentiment}")
except Exception as e:
    print(f"Error: {e}")
finally:
    db.close()
