import sys
import os

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app.database import SessionLocal, NewsItem

db = SessionLocal()
try:
    print("Latest items from Moneycontrol:")
    mc_items = db.query(NewsItem).filter(NewsItem.source == 'moneycontrol').order_by(NewsItem.published_at.desc()).limit(5).all()
    for idx, item in enumerate(mc_items):
        print(f"{idx+1}. Headline: {item.headline} | Published: {item.published_at}")
        
    print("\nLatest items from NDTV Profit:")
    ndtv_items = db.query(NewsItem).filter(NewsItem.source == 'ndtv_profit').order_by(NewsItem.published_at.desc()).limit(5).all()
    for idx, item in enumerate(ndtv_items):
        print(f"{idx+1}. Headline: {item.headline} | Published: {item.published_at}")
        
    print("\nLatest items from Livemint:")
    lm_items = db.query(NewsItem).filter(NewsItem.source == 'livemint').order_by(NewsItem.published_at.desc()).limit(5).all()
    for idx, item in enumerate(lm_items):
        print(f"{idx+1}. Headline: {item.headline} | Published: {item.published_at}")
        
except Exception as e:
    print(f"Error: {e}")
finally:
    db.close()
