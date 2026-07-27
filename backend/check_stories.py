import sys
import os

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app.database import SessionLocal, NewsStory, NewsItem, MarketEvent, CompanyFiling
from sqlalchemy import func

db = SessionLocal()
try:
    print("=== DATABASE OVERVIEW ===")
    print(f"Total News Items: {db.query(NewsItem).count()}")
    print(f"Total News Stories: {db.query(NewsStory).count()}")
    print(f"Total Market Events: {db.query(MarketEvent).count()}")
    print(f"Total Company Filings: {db.query(CompanyFiling).count()}")
    
    print("\n=== NEWS STORIES AI ANALYSIS STATUS ===")
    total_stories = db.query(NewsStory).count()
    analyzed_stories = db.query(NewsStory).filter(NewsStory.ai_analyzed_at.isnot(None)).count()
    pending_stories = db.query(NewsStory).filter(NewsStory.ai_analyzed_at.is_(None)).count()
    print(f"Analyzed stories: {analyzed_stories}")
    print(f"Pending stories: {pending_stories}")
    
    print("\n=== MONEYCONTROL ARTICLES ===")
    mc_items = db.query(NewsItem).filter(NewsItem.source == 'moneycontrol').limit(5).all()
    print(f"Found {len(mc_items)} sample Moneycontrol items:")
    for idx, item in enumerate(mc_items):
        story = db.query(NewsStory).filter(NewsStory.id == item.story_id).first() if item.story_id else None
        print(f"{idx+1}. Headline: {item.headline}")
        print(f"   URL: {item.url}")
        print(f"   Story ID: {item.story_id} | Story Analyzed: {story.ai_analyzed_at if story else 'No Story'}")
        
    print("\n=== STORIES WITH MULTIPLE ARTICLES ===")
    multi_art_stories = db.query(NewsStory).filter(NewsStory.article_count > 1).limit(5).all()
    for s in multi_art_stories:
        items = db.query(NewsItem).filter(NewsItem.story_id == s.id).all()
        print(f"- Story [{s.id}] Headline: {s.headline} (Article Count: {s.article_count})")
        for it in items:
            print(f"  * [{it.source}] {it.headline}")
            
except Exception as e:
    print(f"Error: {e}")
finally:
    db.close()
