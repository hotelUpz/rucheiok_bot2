import time
import json
import hmac
import hashlib
import asyncio
from typing import Any, Dict, Optional
import aiohttp
from c_log import UnifiedLogger
from utils import float_to_str

logger = UnifiedLogger("api")

class PhemexPrivateClient:
    BASE_URL = "https://api.phemex.com"

    def __init__(self, api_key: str, api_secret: str, session: aiohttp.ClientSession, retries: int = 2):
        self.api_key = api_key
        self.api_secret = api_secret
        self.session = session
        self.retries = retries

    def _get_signature(self, path: str, query_no_question: str, expiry: int, body_str: str) -> str:
        message = f"{path}{query_no_question}{expiry}{body_str}"
        return hmac.new(self.api_secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()

    async def _request(self, method: str, path: str, query_no_q: str = "", body: Optional[Dict[str, Any]] = None, timeout_sec: float = 10.0) -> Dict[str, Any]:
        query_for_url = f"?{query_no_q}" if query_no_q else ""
        url = f"{self.BASE_URL}{path}{query_for_url}"
        body_str = json.dumps(body, separators=(',', ':')) if body else ""
        
        is_safe_to_retry = method.upper() in ("GET", "DELETE", "PUT")
        attempts = self.retries if is_safe_to_retry else 1
        last_err = None

        for attempt in range(1, attempts + 1):
            try:
                expiry = int(time.time() + 60)
                signature = self._get_signature(path, query_no_q, expiry, body_str)
                headers = {
                    "Content-Type": "application/json",
                    "x-phemex-access-token": self.api_key,
                    "x-phemex-request-expiry": str(expiry),
                    "x-phemex-request-signature": signature
                }
                
                async with self.session.request(method, url, headers=headers, data=body_str if body else None, timeout=timeout_sec) as resp:
                    text = await resp.text()
                    if resp.status not in (200, 201, 202, 204):
                        raise RuntimeError(f"HTTP {resp.status}: {text}")
                    try: data = json.loads(text)
                    except json.JSONDecodeError: raise RuntimeError(f"Invalid JSON: {text}")
                        
                    code = int(data.get("code", 0))
                    if code != 0:
                        raise RuntimeError(f"Phemex Error [{code}]: {data.get('msg', '')}")
                    return data
            except Exception as e:
                last_err = e
                if attempt < attempts: await asyncio.sleep(0.5 * attempt)
                else: break
        logger.error(f"API Request Failed ({method} {path}): {last_err}")
        raise RuntimeError(f"Private API request failed: {last_err}")
    
    async def set_margin_mode(self, symbol: str, margin_mode: int, pos_mode: int = 2) -> Dict[str, Any]:
        """
        Переключение режима маржи:
        marginMode: 1 = Isolated, 2 = Cross
        targetPosMode: 1 = One-way, 2 = Hedged
        """
        query_no_q = f"marginMode={margin_mode}&symbol={symbol}&targetPosMode={pos_mode}"
        return await self._request("PUT", "/g-positions/switch-pos-mode-sync", query_no_q=query_no_q)

    async def set_leverage(self, symbol: str, pos_side: str, leverage: float, mode: str = "hedged") -> Dict[str, Any]:
        lev_str = float_to_str(leverage)
        query_no_q = f"longLeverageRr={lev_str}&shortLeverageRr={lev_str}&symbol={symbol}" if mode == "hedged" else f"leverageRr={lev_str}&symbol={symbol}"
        return await self._request("PUT", "/g-positions/leverage", query_no_q=query_no_q)

    async def place_limit_order(self, symbol: str, side: str, qty: float, price: float, pos_side: str) -> Dict[str, Any]:
        body = {
            "symbol": symbol, "side": side, "orderQtyRq": float_to_str(qty),
            "priceRp": float_to_str(price), "ordType": "Limit", "timeInForce": "GoodTillCancel", "posSide": pos_side
        }
        return await self._request("POST", "/g-orders", body=body)

    async def cancel_order(self, symbol: str, order_id: str, pos_side: str) -> Dict[str, Any]:
        query_no_q = f"orderID={order_id}&posSide={pos_side}&symbol={symbol}"
        return await self._request("DELETE", "/g-orders/cancel", query_no_q=query_no_q)

    async def cancel_all_orders(self, symbol: str) -> Dict[str, Any]:
        if not symbol or not isinstance(symbol, str) or len(symbol) < 3:
            logger.error("🛑 КРИТИЧЕСКАЯ БЛОКИРОВКА: Попытка отменить все ордера без указания конкретной монеты!")
            return {}
        return await self._request("DELETE", "/g-orders/all", query_no_q=f"symbol={symbol}")

    async def get_active_positions(self) -> Dict[str, Any]:
        return await self._request("GET", "/g-accounts/accountPositions", query_no_q="currency=USDT")