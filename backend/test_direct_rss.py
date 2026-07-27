import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
import feedparser
import ssl

ssl._create_default_https_context = ssl._create_unverified_context

def test_feed(url, name):
    print(f"\n--- Testing {name} Feed parser ({url}) ---")
    
    # 1. Test using feedparser
    try:
        agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        feed = feedparser.parse(url, agent=agent)
        print(f"feedparser status: {feed.get('status', 'no status')}")
        print(f"feedparser bozo (parsing error): {feed.get('bozo', False)}")
        if feed.get('bozo'):
            print(f"feedparser bozo_exception: {feed.get('bozo_exception')}")
        entries = feed.entries
        print(f"feedparser found {len(entries)} entries.")
        for idx, entry in enumerate(entries[:3]):
            print(f"  {idx+1}. {entry.get('title')} | Link: {entry.get('link')}")
    except Exception as e:
        print(f"feedparser failed: {e}")
        
    # 2. Test using urllib directly
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as response:
            html = response.read()
        print(f"urllib download successful, length: {len(html)} bytes.")
        root = ET.fromstring(html)
        items = root.findall(".//item")
        print(f"urllib xml parser found {len(items)} items.")
    except Exception as e:
        print(f"urllib failed: {e}")

# Test Moneycontrol RSS feeds
test_feed("https://www.moneycontrol.com/rss/latestnews.xml", "Moneycontrol Latest News")
test_feed("https://www.moneycontrol.com/rss/MC_markets.xml", "Moneycontrol Markets")
test_feed("https://www.business-standard.com/rss/markets-106.rss", "Business Standard Markets")
test_feed("https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms", "Economic Times Markets")
