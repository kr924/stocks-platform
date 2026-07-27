import urllib.request
import feedparser

def test_feed(url, name):
    print(f"\n--- Testing {name} Feed parser ({url}) ---")
    try:
        agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        feed = feedparser.parse(url, agent=agent)
        print(f"feedparser status: {feed.get('status', 'no status')}")
        print(f"feedparser found {len(feed.entries)} entries.")
        for idx, entry in enumerate(feed.entries[:3]):
            print(f"  {idx+1}. {entry.get('title')} | Link: {entry.get('link')}")
    except Exception as e:
        print(f"failed: {e}")

test_feed("https://www.livemint.com/rss/markets", "Livemint Markets")
test_feed("https://www.livemint.com/rss/companies", "Livemint Companies")
test_feed("https://feeds.feedburner.com/ndtvprofit-latest", "NDTV Profit")
