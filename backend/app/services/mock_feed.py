import random
import time
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from app.services.base_feed import BaseMarketFeed

class MockMarketFeed(BaseMarketFeed):
    def __init__(self):
        self.token = "mock_access_token"
        
        # Base prices for determinism based on symbols
        self.base_prices = {
            "RELIANCE": 2450.0,
            "TCS": 3820.0,
            "HDFCBANK": 1580.0,
            "BHARTIARTL": 1120.0,
            "ICICIBANK": 980.0,
            "INFY": 1450.0,
            "SBI": 780.0,
            "LICI": 910.0,
            "ITC": 430.0,
            "HINDUNILVR": 2320.0,
            "SUZLON": 52.40,
            "Nifty 50": 23550.0,
            "SENSEX": 77200.0,
            "Nifty Bank": 51200.0,
            "Nifty IT": 38500.0,
            "Nifty Pharma": 19500.0,
            "Nifty Auto": 25400.0,
            "Nifty FMCG": 57200.0,
            "Nifty Metal": 9800.0,
            "Nifty Fin Service": 23800.0,
            "Nifty Realty": 1050.0,
        }

    def _get_base_price(self, symbol: str) -> float:
        clean_symbol = symbol.split("|")[-1] if "|" in symbol else symbol
        return self.base_prices.get(clean_symbol, 450.0)

    def get_auth_url(self) -> str:
        return "http://localhost:8000/api/auth/callback?code=mock_code"

    def authenticate(self, code: str) -> str:
        return self.token

    def set_access_token(self, token: str) -> None:
        self.token = token

    def get_quotes(self, instrument_keys: List[str]) -> Dict[str, Dict[str, Any]]:
        # Simulate slight fluctuation on real prices
        result = {}
        for key in instrument_keys:
            symbol = key.split("|")[-1] if "|" in key else key
            base = self._get_base_price(symbol)
            
            # Seed based on symbol + current day to have stable prices during the day
            today_str = datetime.now().strftime("%Y-%m-%d")
            # Create a semi-deterministic seed
            seed = sum(ord(c) for c in symbol) + sum(ord(c) for c in today_str)
            random.seed(seed)
            
            pct_change = random.uniform(-4.5, 4.5)
            prev_close = base
            last_price = prev_close * (1 + pct_change / 100)
            
            # Form high and low
            high = max(last_price, prev_close) * random.uniform(1.0, 1.02)
            low = min(last_price, prev_close) * random.uniform(0.98, 1.0)
            open_price = prev_close * random.uniform(0.99, 1.01)
            volume = random.randint(100000, 10000000)

            # Generate simulated depth (5 levels of bids and asks)
            # Use a seed based on key and current timestamp to simulate realistic updates
            # but keep it reproducible for the call
            quote_seed = sum(ord(c) for c in key) + int(time.time() * 10) % 1000
            random.seed(quote_seed)
            
            buy_levels = []
            sell_levels = []
            
            # Bid prices are below last_price
            bid_price = last_price
            for _ in range(5):
                bid_price = round(bid_price - random.uniform(0.05, 0.25), 2)
                qty = random.randint(100, 5000)
                orders = random.randint(1, 15)
                buy_levels.append({"price": bid_price, "quantity": qty, "orders": orders})
                
            # Ask prices are above last_price
            ask_price = last_price
            for _ in range(5):
                ask_price = round(ask_price + random.uniform(0.05, 0.25), 2)
                qty = random.randint(100, 5000)
                orders = random.randint(1, 15)
                sell_levels.append({"price": ask_price, "quantity": qty, "orders": orders})
                
            # Calculate distance-weighted sentiment
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
            
            weighted_total = total_buy_qty_weighted + total_sell_qty_weighted
            if weighted_total > 0:
                depth_buy_pct = round((total_buy_qty_weighted / weighted_total) * 100, 2)
                depth_sell_pct = round(100.0 - depth_buy_pct, 2)
            else:
                depth_buy_pct = 50.0
                depth_sell_pct = 50.0

            # Reset random seed for any non-deterministic usage later
            random.seed(None)

            result[key] = {
                "last_price": round(last_price, 2),
                "volume": volume,
                "ohlc": {
                    "open": round(open_price, 2),
                    "high": round(high, 2),
                    "low": round(low, 2),
                    "close": round(prev_close, 2)
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
        return result

    def get_historical_candles(self, instrument_key: str, interval: str, to_date: str, from_date: Optional[str] = None) -> List[List[Any]]:
        # Generate candles dynamically based on date range and interval
        symbol = instrument_key.split("|")[-1] if "|" in instrument_key else instrument_key
        base = self._get_base_price(symbol)
        
        try:
            end_dt = datetime.strptime(to_date, "%Y-%m-%d")
        except ValueError:
            end_dt = datetime.now()
            
        if from_date:
            try:
                start_dt = datetime.strptime(from_date, "%Y-%m-%d")
            except ValueError:
                start_dt = end_dt - timedelta(days=30)
        else:
            start_dt = end_dt - timedelta(days=30)
            
        num_days = (end_dt - start_dt).days
        if num_days < 0:
            num_days = 0
            
        # Fixed seed for a specific stock's chart trend consistency
        seed = sum(ord(c) for c in symbol)
        random.seed(seed)
        
        candles = []
        
        if interval in ("1minute", "30minute"):
            # Intraday candles: generate for each trading day in range
            current_price = base * 0.98
            step_minutes = 1 if interval == "1minute" else 30
            
            # Find trading days in the date range
            trading_days = []
            for d in range(num_days + 1):
                day_dt = start_dt + timedelta(days=d)
                if day_dt.date() > end_dt.date():
                    break
                if day_dt.weekday() < 5:  # Skip weekends
                    trading_days.append(day_dt)
                    
            for day_dt in trading_days:
                # Active market hours: 9:15 to 15:30 (375 minutes total)
                curr_time = datetime(day_dt.year, day_dt.month, day_dt.day, 9, 15, 0)
                end_time = datetime(day_dt.year, day_dt.month, day_dt.day, 15, 30, 0)
                
                while curr_time <= end_time:
                    open_p = current_price
                    change = current_price * random.uniform(-0.003, 0.0035)
                    close_p = current_price + change
                    high_p = max(open_p, close_p) + (current_price * random.uniform(0.0002, 0.0015))
                    low_p = min(open_p, close_p) - (current_price * random.uniform(0.0002, 0.0015))
                    volume = random.randint(5000, 100000)
                    
                    time_str = curr_time.strftime("%Y-%m-%dT%H:%M:%S+05:30")
                    candles.append([
                        time_str,
                        round(open_p, 2),
                        round(high_p, 2),
                        round(low_p, 2),
                        round(close_p, 2),
                        volume,
                        0
                    ])
                    current_price = close_p
                    curr_time += timedelta(minutes=step_minutes)
        else:
            # Daily/weekly/monthly candles
            step_days = 1
            if interval == "week":
                step_days = 7
            elif interval == "month":
                step_days = 30
                
            steps = num_days // step_days
            if steps <= 0:
                steps = 30
                
            factor = 0.45 if num_days > 1500 else 0.75 if num_days > 300 else 0.95
            current_price = base * factor
            
            for i in range(steps + 1):
                candle_dt = end_dt - timedelta(days=(steps - i) * step_days)
                if step_days == 1 and candle_dt.weekday() >= 5:
                    continue
                    
                open_p = current_price
                change = current_price * random.uniform(-0.02, 0.025)
                close_p = current_price + change
                high_p = max(open_p, close_p) + (current_price * random.uniform(0.002, 0.015))
                low_p = min(open_p, close_p) - (current_price * random.uniform(0.002, 0.015))
                volume = random.randint(300000, 4000000)
                
                candles.append([
                    candle_dt.strftime("%Y-%m-%dT09:15:00+05:30"),
                    round(open_p, 2),
                    round(high_p, 2),
                    round(low_p, 2),
                    round(close_p, 2),
                    volume,
                    0
                ])
                current_price = close_p
                
        random.seed(None)
        return candles

    def get_news(self, instrument_key: str) -> List[Dict[str, Any]]:
        symbol = instrument_key.split("|")[-1] if "|" in instrument_key else instrument_key
        
        headlines = [
            f"{symbol} Consolidated Net Profit Rises 14% YoY, Outperforms Expectations",
            f"Analysts Maintain 'Buy' Rating on {symbol} Following New Infrastructure Investment",
            f"Volume Alert: Trading Interest Surges for {symbol} in Afternoon Session",
            f"{symbol} Board of Directors to Consider Interim Dividend Next Week",
            f"Market Update: {symbol} Faces Resistance at Key Psychological Levels"
        ]
        
        summaries = [
            f"The company reported a strong quarterly financial performance with consolidated net profits rising 14% year-over-year. Robust revenue growth in core segments contributed to higher profit margins.",
            f"Leading research houses have reiterared their positive outlook on {symbol} with an upward price target revision, citing long-term value creation and new operational expansions.",
            f"A sharp spike in trading volume was witnessed for {symbol} on the exchanges today. Market experts point out that accumulative buying might be underway by institutional investors.",
            f"In an official filing, the company announced that a meeting of its Board is scheduled next week to consider, evaluate, and approve an interim dividend payout to eligible shareholders.",
            f"The stock price of {symbol} encountered heavy selling pressure at key technical moving averages. Traders suggest holding positions until a decisive breakout occurs above key pivots."
        ]
        
        sources = ["CNBC-TV18", "Economic Times", "Moneycontrol", "Bloomberg Quint", "Livemint"]
        
        now = datetime.now()
        news_list = []
        
        for i in range(len(headlines)):
            pub_time = now - timedelta(hours=(i * 4 + 1))
            news_list.append({
                "headline": headlines[i],
                "summary": summaries[i],
                "source": sources[i],
                "url": f"https://mockfinance.com/news/{symbol.lower()}-{i}",
                "published_at": pub_time.strftime("%Y-%m-%dT%H:%M:%SZ")
            })
            
        return news_list

    def search(self, query: str) -> List[Dict[str, Any]]:
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
        if "SUZLON" in query_upper and not any(m["symbol"] == "SUZLON" for m in matches):
            matches.append({
                "symbol": "SUZLON",
                "name": "Suzlon Energy Ltd.",
                "key": "NSE_EQ|INE040H01021"
            })
        return matches[:10]

