from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional

class BaseMarketFeed(ABC):
    
    @abstractmethod
    def get_auth_url(self) -> str:
        """Get redirect URL to trigger login/auth flow."""
        pass

    @abstractmethod
    def authenticate(self, code: str) -> str:
        """Exchange redirect code for access token."""
        pass

    @abstractmethod
    def set_access_token(self, token: str) -> None:
        """Set active access token for session calls."""
        pass

    @abstractmethod
    def get_quotes(self, instrument_keys: List[str]) -> Dict[str, Dict[str, Any]]:
        """Fetch quotes containing LTP, Open, High, Low, Close, Volume for multiple stocks."""
        pass

    @abstractmethod
    def get_historical_candles(self, instrument_key: str, interval: str, to_date: str, from_date: Optional[str] = None) -> List[List[Any]]:
        """Fetch historical candle array: [timestamp, open, high, low, close, volume, open_interest]."""
        pass

    @abstractmethod
    def get_news(self, instrument_key: str) -> List[Dict[str, Any]]:
        """Fetch live news for the given instrument key from provider."""
        pass

    @abstractmethod
    def search(self, query: str) -> List[Dict[str, Any]]:
        """Search instruments based on query."""
        pass

