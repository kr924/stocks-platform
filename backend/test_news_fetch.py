import sys
import os
import logging

# Add backend directory to sys.path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# Configure logging to stdout
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)

from app.database import SessionLocal, NewsItem
from app.services.news_aggregator import fetch_all_news, parse_rss

def run_test():
    print("Starting news fetch test...")
    db = SessionLocal()
    try:
        # Check current count of articles in DB
        total_items = db.query(NewsItem).count()
        print(f"Current total news items in DB: {total_items}")
        
        # Test individual feed parsing
        print("\n--- Testing Google News Moneycontrol Feed ---")
        mc_url = "https://news.google.com/rss/search?q=site%3Amoneycontrol.com+Indian+stock+market&hl=en-IN&gl=IN&ceid=IN%3Aen"
        articles = parse_rss(mc_url, max_articles=5)
        print(f"Moneycontrol search returned {len(articles)} articles.")
        for idx, art in enumerate(articles):
            print(f"{idx+1}. {art['headline']} | URL: {art['url'][:50]}... | Date: {art['published_at']}")
            
        print("\n--- Testing Economic Times Feed ---")
        et_url = "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"
        articles = parse_rss(et_url, max_articles=5)
        print(f"Economic Times returned {len(articles)} articles.")
        for idx, art in enumerate(articles):
            print(f"{idx+1}. {art['headline']} | URL: {art['url'][:50]}... | Date: {art['published_at']}")

        print("\n--- Running Full Aggregator ---")
        res = fetch_all_news(db)
        print(f"Aggregator run results: {res}")
        
        new_total_items = db.query(NewsItem).count()
        print(f"New total news items in DB: {new_total_items} (Added: {new_total_items - total_items})")
        
    except Exception as e:
        print(f"Error in test: {e}")
        import traceback
        traceback.print_exc()
    finally:
        db.close()

if __name__ == "__main__":
    run_test()
