import requests
from typing import List, Dict, Any, Optional
from datetime import datetime
from app.services.base_feed import BaseMarketFeed
from app.config import UPSTOX_CLIENT_ID, UPSTOX_CLIENT_SECRET, UPSTOX_REDIRECT_URI

import os
from urllib.parse import quote

class UpstoxAuthError(Exception):
    """Exception raised when Upstox authentication fails or is missing."""
    pass

class UpstoxMarketFeed(BaseMarketFeed):
    def __init__(self):
        self.access_token = None
        self.base_url = "https://api.upstox.com/v2"
        self._quotes_cache: Dict[str, Dict[str, Any]] = {}

    def _is_market_hours(self) -> bool:
        """Check if current time is within market hours (Mon-Fri 9:00 AM - 3:35 PM IST)."""
        from datetime import datetime, timezone, timedelta
        ist = timezone(timedelta(hours=5, minutes=30))
        now = datetime.now(ist)
        if now.weekday() > 4:  # Weekend (Saturday=5, Sunday=6)
            return False
        market_open = now.replace(hour=9, minute=0, second=0, microsecond=0)
        market_close = now.replace(hour=15, minute=35, second=0, microsecond=0)
        return market_open <= now <= market_close

    def get_auth_url(self) -> str:
        client_id = os.getenv("UPSTOX_CLIENT_ID", UPSTOX_CLIENT_ID)
        redirect_uri = os.getenv("UPSTOX_REDIRECT_URI", UPSTOX_REDIRECT_URI)
        encoded_redirect = quote(redirect_uri, safe="")
        return (
            f"https://api.upstox.com/v2/login/authorization/dialog"
            f"?response_type=code"
            f"&client_id={client_id}"
            f"&redirect_uri={encoded_redirect}"
        )

    def authenticate(self, code: str) -> str:
        url = f"{self.base_url}/login/authorization/token"
        headers = {
            "accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded"
        }
        client_id = os.getenv("UPSTOX_CLIENT_ID", UPSTOX_CLIENT_ID)
        client_secret = os.getenv("UPSTOX_CLIENT_SECRET", UPSTOX_CLIENT_SECRET)
        redirect_uri = os.getenv("UPSTOX_REDIRECT_URI", UPSTOX_REDIRECT_URI)
        data = {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code"
        }
        response = requests.post(url, headers=headers, data=data)
        if response.status_code != 200:
            raise UpstoxAuthError(f"Failed to authenticate with Upstox: {response.text}")
        
        resp_json = response.json()
        self.access_token = resp_json.get("access_token")
        return self.access_token

    def set_access_token(self, token: str) -> None:
        self.access_token = token

    def _get_headers(self) -> dict:
        if not self.access_token:
            raise UpstoxAuthError("No active access token. Please authorize your Upstox account first.")
        return {
            "accept": "application/json",
            "Authorization": f"Bearer {self.access_token}"
        }

    def get_quotes(self, instrument_keys: List[str]) -> Dict[str, Dict[str, Any]]:
        # If outside market hours (3:35 PM to 9:00 AM IST) and we have cached quotes for requested keys,
        # return the cached quotes directly to avoid unnecessary Upstox API calls when market is closed.
        if not self._is_market_hours() and self._quotes_cache:
            cached_result = {k: self._quotes_cache[k] for k in instrument_keys if k in self._quotes_cache}
            if len(cached_result) == len(instrument_keys):
                return cached_result

        # Upstox supports up to 500 comma separated instrument keys in one call
        url = f"{self.base_url}/market-quote/quotes"
        params = {"instrument_key": ",".join(instrument_keys)}
        
        response = requests.get(url, headers=self._get_headers(), params=params)
        if response.status_code == 401:
            raise UpstoxAuthError(f"Unauthorized Upstox API call: {response.text}")
        elif response.status_code != 200:
            # If request fails off-market, return cache if available
            if self._quotes_cache:
                return {k: self._quotes_cache[k] for k in instrument_keys if k in self._quotes_cache}
            raise Exception(f"Failed to fetch Upstox quotes: {response.text}")
            
        data = response.json().get("data", {})
        result = {}
        for key, val in data.items():
            # Use instrument_token if available (it matches the requested key like 'NSE_EQ|INE002A01018')
            resolved_key = val.get("instrument_token") or key
            last_price = val.get("last_price", 0.0)
            net_change = val.get("net_change", 0.0)
            # If net_change is non-zero, compute previous close dynamically to get correct non-zero daily change%
            prev_close = last_price - net_change if net_change != 0.0 else val.get("ohlc", {}).get("close", 0.0)
            
            # Parse depth for buyer vs seller sentiment
            depth = val.get("depth", {}) or {}
            buy_levels = depth.get("buy", []) or []
            sell_levels = depth.get("sell", []) or []
            
            total_buy_qty_weighted = 0.0
            total_sell_qty_weighted = 0.0
            total_buy_qty_raw = 0
            total_sell_qty_raw = 0
            epsilon = 0.05
            
            for bid in buy_levels:
                price = bid.get("price", 0.0)
                qty = bid.get("quantity", 0)
                total_buy_qty_raw += qty
                if qty > 0 and price > 0:
                    weight = 1.0 / (abs(price - last_price) + epsilon)
                    total_buy_qty_weighted += qty * weight
                    
            for ask in sell_levels:
                price = ask.get("price", 0.0)
                qty = ask.get("quantity", 0)
                total_sell_qty_raw += qty
                if qty > 0 and price > 0:
                    weight = 1.0 / (abs(price - last_price) + epsilon)
                    total_sell_qty_weighted += qty * weight
            
            grand_total = total_buy_qty_weighted + total_sell_qty_weighted
            if grand_total > 0:
                depth_buy_pct = round((total_buy_qty_weighted / grand_total) * 100, 2)
                depth_sell_pct = round(100.0 - depth_buy_pct, 2)
            else:
                depth_buy_pct = 50.0
                depth_sell_pct = 50.0

            item_quote = {
                "last_price": last_price,
                "volume": val.get("volume", 0),
                "ohlc": {
                    "open": val.get("ohlc", {}).get("open", 0.0),
                    "high": val.get("ohlc", {}).get("high", 0.0),
                    "low": val.get("ohlc", {}).get("low", 0.0),
                    "close": round(prev_close, 2),
                },
                "depth": {
                    "buy": buy_levels,
                    "sell": sell_levels
                },
                "depth_buy_pct": depth_buy_pct,
                "depth_sell_pct": depth_sell_pct,
                "total_buy_qty": total_buy_qty_raw,
                "total_sell_qty": total_sell_qty_raw
            }
            result[resolved_key] = item_quote
            self._quotes_cache[resolved_key] = item_quote
        return result

    def get_historical_candles(self, instrument_key: str, interval: str, to_date: str, from_date: Optional[str] = None) -> List[List[Any]]:
        # Format: /historical-candle/{instrumentKey}/{interval}/{to_date}[/{from_date}]
        # Route to intraday endpoint if from_date == to_date (for today's intraday ticks)
        if from_date and from_date == to_date:
            url = f"{self.base_url}/historical-candle/intraday/{instrument_key}/{interval}"
        elif from_date:
            url = f"{self.base_url}/historical-candle/{instrument_key}/{interval}/{to_date}/{from_date}"
        else:
            url = f"{self.base_url}/historical-candle/{instrument_key}/{interval}/{to_date}"
            
        # Historical candle does not strictly require headers if public, but authentication header ensures access
        headers = self._get_headers() if self.access_token else {"accept": "application/json"}
        
        response = requests.get(url, headers=headers)
        if response.status_code == 401:
            raise UpstoxAuthError(f"Unauthorized Upstox API call: {response.text}")
        elif response.status_code != 200:
            raise Exception(f"Failed to fetch historical candles: {response.text}")
            
        # Upstox candles list: [timestamp, open, high, low, close, volume, open_interest]
        candles = response.json().get("data", {}).get("candles", [])
        # Upstox returns candles sorted descending (newest first). Let's return them as is,
        # but the charting library expects them sorted ascending (oldest first).
        # We can reverse them here or on the frontend. Reversing here is very clean.
        candles.reverse()
        return candles

    def get_news(self, instrument_key: str) -> List[Dict[str, Any]]:
        # Upstox News API: /v2/news
        url = f"{self.base_url}/news"
        params = {
            "category": "instrument_keys",
            "instrument_keys": instrument_key,
            "page_number": 1,
            "page_size": 10
        }
        
        response = requests.get(url, headers=self._get_headers(), params=params)
        if response.status_code == 401:
            raise UpstoxAuthError(f"Unauthorized Upstox API call: {response.text}")
        elif response.status_code != 200:
            # If news API fails or is not available on this subscription, fallback to empty list
            return []
            
        data = response.json().get("data", {})
        articles = []
        if isinstance(data, dict):
            articles = data.get(instrument_key, [])
        elif isinstance(data, list):
            articles = data
            
        result = []
        for article in articles:
            # Map Upstox news schema fields:
            headline = article.get("heading") or article.get("headline") or article.get("title") or "Stock News Update"
            summary = article.get("summary") or article.get("description") or ""
            source = article.get("publisher") or article.get("source") or "Upstox News"
            url_link = article.get("article_link") or article.get("url") or article.get("link") or "#"
            
            pub_val = article.get("published_time") or article.get("publish_time") or article.get("published_at")
            if isinstance(pub_val, (int, float)):
                pub_time = datetime.utcfromtimestamp(pub_val / 1000).isoformat() + "Z"
            elif isinstance(pub_val, str):
                pub_time = pub_val
            else:
                pub_time = datetime.utcnow().isoformat()
            
            result.append({
                "headline": headline,
                "summary": summary,
                "source": source,
                "url": url_link,
                "published_at": pub_time
            })
        return result

    def search(self, query: str) -> List[Dict[str, Any]]:
        if not self.access_token:
            # Fallback to local Nifty 50 search if not authenticated
            query_upper = query.upper()
            from app.config import DEFAULT_NIFTY_50
            matches = []
            for stock in DEFAULT_NIFTY_50:
                if query_upper in stock["symbol"].upper() or query_upper in stock["name"].upper():
                    matches.append({
                        "symbol": stock["symbol"],
                        "name": stock["name"],
                        "key": stock["key"]
                    })
            if "PRAJ" in query_upper and not any(m["symbol"] == "PRAJ" for m in matches):
                matches.append({
                    "symbol": "PRAJ",
                    "name": "Praj Industries Ltd.",
                    "key": "NSE_EQ|INE171A01029"
                })
            return matches[:10]

        url = f"{self.base_url}/instruments/search"
        params = {
            "query": query,
            "exchanges": "NSE",
            "segments": "EQ"
        }
        try:
            response = requests.get(url, headers=self._get_headers(), params=params)
            if response.status_code == 401:
                raise UpstoxAuthError(f"Unauthorized Upstox API call: {response.text}")
            if response.status_code != 200:
                return []
            
            data = response.json().get("data", [])
            result = []
            for item in data:
                # Ensure it belongs to NSE EQ segment (handles both 'NSE_EQ' and 'EQ')
                exchange = item.get("exchange")
                segment = item.get("segment")
                if exchange == "NSE" and segment in ("NSE_EQ", "EQ"):
                    symbol = item.get("trading_symbol") or item.get("tradingsymbol")
                    if symbol:
                        result.append({
                            "symbol": symbol,
                            "name": item.get("name") or symbol,
                            "key": item.get("instrument_key")
                        })
            return result[:10]
        except UpstoxAuthError:
            raise
        except Exception:
            return []

