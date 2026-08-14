import requests
import time
import threading
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
    # Upstox rate limiting (UDAPI10005) was blanking prices and starving the
    # result baselines of a price to compare against. The dashboard polls every
    # 3s and each tick costs two upstream calls — watchlist and quotes — which
    # alone exceeds the quota, before the tracker, the intelligence feed, extra
    # browser tabs, or a results burst add anything.
    #
    # One background loop owns the Upstox quote endpoint, the way exchange_hub
    # owns the announcement endpoints. Every panel reads its cache instead of
    # fetching, so the metered request rate is set by this interval alone and
    # does not grow with panels, browser tabs or a burst of filings.
    #
    # Upstox takes 500 keys per call, and a full screen — watchlist, movers,
    # indices, and every pending result — fits in one. A 3s cycle is therefore
    # about 20 requests a minute total, well inside the limit that repeatedly
    # blanked the screen when four callers polled on their own timers.
    REFRESH_INTERVAL = 3.0
    IDLE_REFRESH_INTERVAL = 60.0      # outside market hours nothing is moving
    STALE_AFTER_SECONDS = 10.0        # serve older than this, but re-request it
    INTEREST_TTL_SECONDS = 300.0      # stop refreshing what nothing has asked for
    MAX_KEYS_PER_REQUEST = 450

    def __init__(self):
        self.access_token = None
        self.base_url = "https://api.upstox.com/v2"
        self._quotes_cache: Dict[str, Dict[str, Any]] = {}
        self._quote_fetched_at: Dict[str, float] = {}
        self._interest: Dict[str, float] = {}
        self._refresher: Optional[threading.Thread] = None
        self._refresher_lock = threading.Lock()

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

    def _note_interest(self, instrument_keys: List[str]) -> None:
        """Remember that something on screen wants these keys kept warm."""
        now = time.monotonic()
        for k in instrument_keys:
            if "|" in k:  # request form only; symbols and colon forms are lookup aliases
                self._interest[k] = now

    def _start_refresher(self) -> None:
        """One background loop refreshes every key anyone has asked for."""
        if self._refresher and self._refresher.is_alive():
            return
        with self._refresher_lock:
            if self._refresher and self._refresher.is_alive():
                return
            self._refresher = threading.Thread(
                target=self._refresh_loop, name="upstox-quote-refresher", daemon=True
            )
            self._refresher.start()

    def _refresh_loop(self) -> None:
        while True:
            interval = self.REFRESH_INTERVAL if self._is_market_hours() else self.IDLE_REFRESH_INTERVAL
            try:
                now = time.monotonic()
                keys = [k for k, seen in list(self._interest.items())
                        if now - seen <= self.INTEREST_TTL_SECONDS]
                for k in [k for k, seen in list(self._interest.items())
                          if now - seen > self.INTEREST_TTL_SECONDS]:
                    self._interest.pop(k, None)

                if keys and self.access_token:
                    # Upstox accepts 500 keys per call, so the whole screen —
                    # watchlist, movers, indices and every pending result —
                    # usually costs exactly one request per cycle.
                    for i in range(0, len(keys), self.MAX_KEYS_PER_REQUEST):
                        self._fetch_and_cache(keys[i:i + self.MAX_KEYS_PER_REQUEST])
            except UpstoxAuthError:
                pass  # token expired; endpoints surface this on their own path
            except Exception as e:
                print(f"Quote refresher cycle failed: {e}")
            time.sleep(interval)

    def _fetch_and_cache(self, instrument_keys: List[str]) -> Dict[str, Dict[str, Any]]:
        """One upstream call: fetch, parse and store. The only place that hits Upstox."""
        url = f"{self.base_url}/market-quote/quotes"
        params = {"instrument_key": ",".join(instrument_keys)}

        response = requests.get(url, headers=self._get_headers(), params=params)
        if response.status_code == 401:
            raise UpstoxAuthError(f"Unauthorized Upstox API call: {response.text}")
        elif response.status_code != 200:
            # Rate limiting lands here too, so fall back to the last known quote
            # for anything we have ever seen rather than failing the batch.
            served = {k: self._quotes_cache[k] for k in instrument_keys if k in self._quotes_cache}
            if served:
                return served
            raise Exception(f"Failed to fetch Upstox quotes: {response.text}")

        data = response.json().get("data", {})
        result: Dict[str, Dict[str, Any]] = {}
        now = time.monotonic()
        for key, val in data.items():
            # Use instrument_token if available (it matches the requested key like 'NSE_EQ|INE002A01018')
            resolved_key = val.get("instrument_token") or key
            last_price = val.get("last_price", 0.0)
            net_change = val.get("net_change", 0.0)
            # Illiquid scrips come back with "ohlc": null. A default only applies
            # when the key is absent, so `.get("ohlc", {})` hands back None there
            # and the AttributeError takes down the whole batch — every price on
            # the screen, not just this one.
            ohlc = val.get("ohlc") or {}
            # If net_change is non-zero, compute previous close dynamically to get correct non-zero daily change%
            prev_close = last_price - net_change if net_change != 0.0 else ohlc.get("close", 0.0)

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
                    "open": ohlc.get("open", 0.0),
                    "high": ohlc.get("high", 0.0),
                    "low": ohlc.get("low", 0.0),
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
            # One quote is reachable under several handles — the resolved token,
            # the key as requested, and the trading symbol. All of them are
            # stamped, so a lookup by any handle counts as fresh.
            handles = [resolved_key]
            if key:
                handles += [key, key.replace(":", "|")]
            sym_name = val.get("trading_symbol") or val.get("symbol")
            if sym_name:
                handles.append(sym_name.upper())

            for handle in handles:
                result[handle] = item_quote
                self._quotes_cache[handle] = item_quote
                self._quote_fetched_at[handle] = now
        return result

    def get_quotes(self, instrument_keys: List[str]) -> Dict[str, Dict[str, Any]]:
        """
        Quotes for these keys, served from the shared cache the background
        refresher keeps warm.

        Callers used to fetch independently, which is what exhausted the rate
        limit: four panels on their own timers meant four metered calls per
        cycle, and the account has one allowance between them. Now every caller
        registers interest and reads the same cache, so the request rate is set
        by the refresher's interval rather than by how many panels are open.

        Anything the refresher has not reached yet — a symbol just added, the
        first call after a restart — is fetched directly, so a cold cache still
        answers correctly rather than returning nothing.
        """
        self._note_interest(instrument_keys)
        self._start_refresher()

        now = time.monotonic()
        result: Dict[str, Dict[str, Any]] = {}
        missing: List[str] = []
        for k in instrument_keys:
            cached = self._quotes_cache.get(k)
            if cached is None:
                missing.append(k)
            elif (now - self._quote_fetched_at.get(k, 0.0)) <= self.STALE_AFTER_SECONDS:
                result[k] = cached
            else:
                # Past its freshness window: serve it, but ask for a new one.
                result[k] = cached
                missing.append(k)

        if missing:
            request_keys = [k for k in missing if "|" in k]
            try:
                if request_keys:
                    result.update(self._fetch_and_cache(request_keys[:self.MAX_KEYS_PER_REQUEST]))
            except UpstoxAuthError:
                raise
            except Exception:
                # Rate limited or upstream down. Whatever the cache holds is
                # better than an empty panel; only raise if it holds nothing.
                if not result:
                    raise
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

