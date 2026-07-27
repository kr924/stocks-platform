from app.config import PROVIDER
from app.services.base_feed import BaseMarketFeed
from app.services.upstox_feed import UpstoxMarketFeed
from app.services.mock_feed import MockMarketFeed

_feed_instance = None

def get_feed() -> BaseMarketFeed:
    global _feed_instance
    if _feed_instance is None:
        if PROVIDER == "upstox":
            _feed_instance = UpstoxMarketFeed()
        else:
            # Default to mock
            _feed_instance = MockMarketFeed()
    return _feed_instance
