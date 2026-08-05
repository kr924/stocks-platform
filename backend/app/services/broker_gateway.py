"""
Broker Gateway — Unified abstraction layer for stock order execution.
Supports Upstox v2 and Zerodha/Kite Connect v3.
"""
import json
import logging
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List

import requests

logger = logging.getLogger("app.broker_gateway")


@dataclass
class OrderResult:
    success: bool
    broker_order_id: Optional[str] = None
    status: str = "pending"          # pending, placed, filled, failed, cancelled
    price: Optional[float] = None    # fill price
    message: str = ""
    raw_response: dict = field(default_factory=dict)


@dataclass
class Position:
    symbol: str
    quantity: int
    average_price: float
    ltp: float = 0.0
    pnl: float = 0.0
    instrument_key: str = ""


class BaseBroker(ABC):
    """Abstract broker interface."""

    @abstractmethod
    def place_order(self, symbol: str, instrument_key: str, side: str, quantity: int,
                    order_type: str = "MARKET", limit_price: float = None,
                    stoploss_type: str = "software", stoploss_pct: float = 2.0,
                    trigger_price: float = None) -> OrderResult:
        ...

    @abstractmethod
    def get_order_status(self, broker_order_id: str) -> OrderResult:
        ...

    @abstractmethod
    def cancel_order(self, broker_order_id: str) -> bool:
        ...

    @abstractmethod
    def get_ltp(self, instrument_key: str) -> Optional[float]:
        ...

    @abstractmethod
    def get_positions(self) -> List[Position]:
        ...


# ─── Upstox v2 Implementation ───────────────────────────────────────────────

class UpstoxBroker(BaseBroker):
    """Upstox v2 API order execution."""

    def __init__(self):
        self.base_url = "https://api.upstox.com/v2"
        self._access_token = None

    def _get_token(self) -> str:
        """Get Upstox access token from SessionStore DB or environment."""
        if self._access_token:
            return self._access_token
        # Try from DB session store
        try:
            from app.database import SessionLocal, SessionStore
            db = SessionLocal()
            try:
                session = db.query(SessionStore).filter(SessionStore.provider == "upstox").first()
                if session and session.access_token:
                    self._access_token = session.access_token
                    return self._access_token
            finally:
                db.close()
        except Exception:
            pass
        # Fallback to env
        token = os.getenv("UPSTOX_ACCESS_TOKEN", "")
        if token:
            self._access_token = token
        return self._access_token or ""

    def _headers(self) -> dict:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._get_token()}"
        }

    def _is_market_hours(self) -> bool:
        """Check if current time is within NSE market hours (9:15 AM - 3:30 PM IST)."""
        from datetime import timezone, timedelta
        ist = timezone(timedelta(hours=5, minutes=30))
        now_ist = datetime.now(ist)
        market_open = now_ist.replace(hour=9, minute=15, second=0, microsecond=0)
        market_close = now_ist.replace(hour=15, minute=30, second=0, microsecond=0)
        # Weekdays only (Mon=0, Fri=4)
        if now_ist.weekday() > 4:
            return False
        return market_open <= now_ist <= market_close

    def place_order(self, symbol: str, instrument_key: str, side: str, quantity: int,
                    order_type: str = "MARKET", limit_price: float = None,
                    stoploss_type: str = "software", stoploss_pct: float = 2.0,
                    trigger_price: float = None) -> OrderResult:
        """Place an order via Upstox v2 API."""
        if not self._get_token():
            return OrderResult(success=False, status="failed",
                               message="Upstox access token not available. Please authorize first.")

        if not self._is_market_hours():
            return OrderResult(success=False, status="failed",
                               message="Market is closed. Orders can only be placed during market hours (9:15 AM - 3:30 PM IST).")

        # Map order type
        upstox_order_type = "MARKET" if order_type.upper() == "MARKET" else "LIMIT"

        payload = {
            "quantity": quantity,
            "product": "D",            # Delivery (CNC) — holds in portfolio until sold
            "validity": "DAY",
            "price": limit_price or 0,
            "tag": "trading_engine",
            "instrument_token": instrument_key,
            "order_type": upstox_order_type,
            "transaction_type": side.upper(),  # "BUY" or "SELL"
            "disclosed_quantity": 0,
            "trigger_price": trigger_price or 0,
            "is_amo": False
        }

        # For bracket orders with stoploss, use Upstox's bracket order product type
        if stoploss_type == "bracket" and side.upper() == "BUY":
            # Get LTP to calculate stoploss price
            ltp = self.get_ltp(instrument_key)
            if ltp and ltp > 0:
                sl_price = round(ltp * (1 - stoploss_pct / 100), 2)
                payload["product"] = "OCO"  # One-Cancels-Other (bracket)
                payload["trigger_price"] = sl_price

        logger.info(f"📤 [UPSTOX ORDER]: Placing {side} {quantity}x {symbol} ({order_type}) via Upstox")

        try:
            url = f"{self.base_url}/order/place"
            res = requests.post(url, json=payload, headers=self._headers(), timeout=10)

            if res.status_code == 401:
                return OrderResult(success=False, status="failed",
                                   message="Upstox token expired. Please re-authorize.",
                                   raw_response=res.json() if res.text else {})

            resp_data = res.json() if res.text else {}

            if res.status_code == 200 and resp_data.get("status") == "success":
                order_id = resp_data.get("data", {}).get("order_id", "")
                logger.info(f"✅ [UPSTOX ORDER PLACED]: {side} {quantity}x {symbol} → Order ID: {order_id}")
                return OrderResult(
                    success=True,
                    broker_order_id=order_id,
                    status="placed",
                    message=f"Order placed successfully: {order_id}",
                    raw_response=resp_data
                )
            else:
                err_msg = resp_data.get("message", "") or resp_data.get("errors", [{}])[0].get("message", str(resp_data))
                logger.error(f"❌ [UPSTOX ORDER FAILED]: {side} {symbol} → {err_msg}")
                return OrderResult(success=False, status="failed", message=err_msg, raw_response=resp_data)

        except Exception as e:
            logger.error(f"❌ [UPSTOX ORDER ERROR]: {e}")
            return OrderResult(success=False, status="failed", message=str(e))

    def get_order_status(self, broker_order_id: str) -> OrderResult:
        """Check order status via Upstox API."""
        try:
            url = f"{self.base_url}/order/details"
            params = {"order_id": broker_order_id}
            res = requests.get(url, params=params, headers=self._headers(), timeout=10)
            if res.status_code == 200:
                data = res.json().get("data", {})
                status = data.get("status", "unknown").lower()
                price = data.get("average_price") or data.get("price") or 0
                mapped_status = "filled" if status in ("complete", "traded") else "placed" if status in ("open", "pending") else "failed" if status in ("rejected", "cancelled") else status
                return OrderResult(
                    success=mapped_status in ("filled", "placed"),
                    broker_order_id=broker_order_id,
                    status=mapped_status,
                    price=float(price) if price else None,
                    raw_response=res.json()
                )
            return OrderResult(success=False, status="unknown", message=f"HTTP {res.status_code}")
        except Exception as e:
            return OrderResult(success=False, status="unknown", message=str(e))

    def cancel_order(self, broker_order_id: str) -> bool:
        """Cancel a pending order."""
        try:
            url = f"{self.base_url}/order/cancel"
            params = {"order_id": broker_order_id}
            res = requests.delete(url, params=params, headers=self._headers(), timeout=10)
            return res.status_code == 200
        except Exception:
            return False

    def get_ltp(self, instrument_key: str) -> Optional[float]:
        """Get last traded price for an instrument."""
        try:
            url = f"{self.base_url}/market-quote/ltp"
            params = {"instrument_key": instrument_key}
            res = requests.get(url, params=params, headers=self._headers(), timeout=5)
            if res.status_code == 200:
                data = res.json().get("data", {})
                for key, val in data.items():
                    return float(val.get("last_price", 0))
            return None
        except Exception:
            return None

    def get_positions(self) -> List[Position]:
        """Get current positions from Upstox."""
        try:
            url = f"{self.base_url}/portfolio/short-term-positions"
            res = requests.get(url, headers=self._headers(), timeout=10)
            if res.status_code != 200:
                return []
            positions = res.json().get("data", [])
            result = []
            for p in positions:
                if p.get("quantity", 0) != 0:
                    result.append(Position(
                        symbol=p.get("trading_symbol", ""),
                        quantity=p.get("quantity", 0),
                        average_price=float(p.get("average_price", 0)),
                        ltp=float(p.get("last_price", 0)),
                        pnl=float(p.get("pnl", 0)),
                        instrument_key=p.get("instrument_token", "")
                    ))
            return result
        except Exception:
            return []


# ─── Zerodha/Kite Stub ──────────────────────────────────────────────────────

class ZerodhaBroker(BaseBroker):
    """Zerodha Kite Connect v3 API — stub implementation (to be filled when Kite subscription is active)."""

    def __init__(self):
        self.api_key = os.getenv("ZERODHA_API_KEY", "")
        self.access_token = os.getenv("ZERODHA_ACCESS_TOKEN", "")
        self.base_url = "https://api.kite.trade"

    def _headers(self) -> dict:
        return {
            "X-Kite-Version": "3",
            "Authorization": f"token {self.api_key}:{self.access_token}"
        }

    def place_order(self, symbol: str, instrument_key: str, side: str, quantity: int,
                    order_type: str = "MARKET", limit_price: float = None,
                    stoploss_type: str = "software", stoploss_pct: float = 2.0,
                    trigger_price: float = None) -> OrderResult:
        if not self.api_key or not self.access_token:
            return OrderResult(success=False, status="failed",
                               message="Zerodha API key and access token not configured. Set ZERODHA_API_KEY and ZERODHA_ACCESS_TOKEN in .env")

        payload = {
            "tradingsymbol": symbol,
            "exchange": "NSE",
            "transaction_type": side.upper(),
            "order_type": order_type.upper(),
            "quantity": quantity,
            "product": "CNC",  # Cash & Carry (Delivery)
            "validity": "DAY",
        }
        if order_type.upper() == "LIMIT" and limit_price:
            payload["price"] = limit_price

        try:
            url = f"{self.base_url}/orders/regular"
            res = requests.post(url, data=payload, headers=self._headers(), timeout=10)
            resp_data = res.json() if res.text else {}
            if resp_data.get("status") == "success":
                order_id = resp_data.get("data", {}).get("order_id", "")
                return OrderResult(success=True, broker_order_id=order_id, status="placed",
                                   message=f"Zerodha order placed: {order_id}", raw_response=resp_data)
            else:
                return OrderResult(success=False, status="failed",
                                   message=resp_data.get("message", str(resp_data)), raw_response=resp_data)
        except Exception as e:
            return OrderResult(success=False, status="failed", message=str(e))

    def get_order_status(self, broker_order_id: str) -> OrderResult:
        return OrderResult(success=False, status="unknown", message="Zerodha order status not yet implemented")

    def cancel_order(self, broker_order_id: str) -> bool:
        return False

    def get_ltp(self, instrument_key: str) -> Optional[float]:
        return None

    def get_positions(self) -> List[Position]:
        return []


# ─── Factory ─────────────────────────────────────────────────────────────────

def get_broker(broker_name: str = "upstox") -> BaseBroker:
    """Get broker instance by name."""
    if broker_name.lower() == "zerodha":
        return ZerodhaBroker()
    return UpstoxBroker()
