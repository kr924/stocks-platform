import urllib.request
import json

try:
    url = "http://localhost:8000/api/intelligence/feed?page=1&page_size=100&hours=48&event_type=news"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req) as response:
        data = json.loads(response.read().decode())
    
    items = data.get("items", [])
    print(f"API returned {len(items)} items.")
    sources_in_feed = set()
    source_counts = {}
    for item in items:
        # Check source
        src = item.get("source")
        # For news stories, we also check the individual articles
        articles = item.get("articles", [])
        art_sources = [a.get("source") for a in articles]
        print(f"- Title: {item.get('title')[:50]}...")
        print(f"  Type: {item.get('type')} | Source: {src} | Article sources: {art_sources}")
        
        for s in art_sources:
            source_counts[s] = source_counts.get(s, 0) + 1
            sources_in_feed.add(s)
            
    print("\nSource counts in feed:")
    for s, c in source_counts.items():
        print(f"- {s}: {c}")
except Exception as e:
    print(f"Error calling API: {e}")
