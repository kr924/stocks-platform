import sys
import os
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import html
import email.utils
from datetime import datetime

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app.database import SessionLocal
from app.main import gather_stock_news, fetch_rss_news

def test_gather():
    print("Testing gather_stock_news for RELIANCE...")
    db = SessionLocal()
    try:
        # Resolve Reliance Info
        symbol = "RELIANCE"
        name = "Reliance Industries Ltd."
        
        # Test fetch_rss_news directly for site:moneycontrol.com
        mc_query = f'site:moneycontrol.com "Reliance Industries" OR "RELIANCE"'
        print(f"Direct RSS news query: {mc_query}")
        mc_articles = fetch_rss_news(mc_query)
        print(f"Returned {len(mc_articles)} articles:")
        for idx, art in enumerate(mc_articles[:5]):
            print(f"  {idx+1}. [{art['source']}] {art['headline']} | URL: {art['url'][:60]}...")
            
        # Test gather_stock_news
        print("\nGathering stock news for RELIANCE:")
        combined = gather_stock_news(symbol, name)
        print(f"Total combined articles: {len(combined)}")
        for idx, art in enumerate(combined[:10]):
            print(f"  {idx+1}. [{art['source']}] {art['headline']} | {art['published_at']}")
            
    except Exception as e:
        print(f"Error: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    test_gather()
