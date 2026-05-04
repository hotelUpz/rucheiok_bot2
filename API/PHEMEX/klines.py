# ============================================================
# FILE: API/PHEMEX/klines.py
# ROLE: Phemex klines via curl_cffi with batch fetching support.
# ============================================================
from __future__ import annotations

import asyncio
import time
import ujson
from dataclasses import dataclass, asdict
from typing import List, Dict, Optional, Any
from curl_cffi.requests import AsyncSession
from c_log import UnifiedLogger
from utils import save_json_safe

logger = UnifiedLogger("api")

@dataclass
class Kline:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float

class PhemexKlinesAPI:
    BASE_URL = "https://api.phemex.com"
    
    def __init__(self, interval: str = "1m", limit: int = 100, semaphore_limit: int = 5, session: Optional[AsyncSession] = None):
        self.interval = interval
        self.limit = limit
        self.semaphore = asyncio.Semaphore(semaphore_limit)
        self._external_session = session is not None
        self.session = session or AsyncSession(
            impersonate="chrome120",
            http_version=2,
            verify=True
        )
        
        # Phemex V2 kline resolution map
        self.res_map = {
            "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
            "1h": 3600, "4h": 14400, "1d": 86400
        }
        
        # Rate limiting (shared across requests in this instance)
        self._lock = asyncio.Lock()
        self._last_request_time = 0.0
        self.min_interval = 0.15  # 150ms between requests

    async def aclose(self):
        if not self._external_session and self.session:
            await self.session.close()

    def _get_valid_limit(self, limit: int) -> int:
        """Phemex accepts only specific limit values."""
        allowed_limits = [5, 10, 50, 100, 500, 1000]
        return next((l for l in allowed_limits if l >= limit), 1000)

    async def get_price_scales(self) -> Dict[str, float]:
        """Fetches all symbols to identify their price scales (1/tickSize)."""
        url = f"{self.BASE_URL}/public/products"
        try:
            resp = await self.session.get(url, timeout=10.0)
            if resp.status_code != 200:
                logger.error(f"Failed to fetch products for scales: {resp.status_code}")
                return {}
            
            data = ujson.loads(resp.content)
            products = data.get("data", {}).get("perpProductsV2") or data.get("data", {}).get("perpProducts") or []
            
            scales = {}
            for p in products:
                sym = p.get("symbol", "").upper()
                
                # По опыту: надежнее всего брать масштаб из 1/tickSize
                tick = float(p.get("tickSize", 0.0001))
                scale = 1 / tick if tick > 0 else 10000.0
                
                # Если tickSize слишком мелкий или отсутствует, пробуем priceScale как экспоненту
                if scale < 1:
                    p_exp = int(p.get("priceScale", 4))
                    scale = 10 ** p_exp
                
                scales[sym] = scale
            return scales
        except Exception as e:
            logger.error(f"Error fetching price scales: {e}")
            return {}

    async def get_klines(self, symbol: str, interval: Optional[str] = None, limit: Optional[int] = None) -> List[Kline]:
        """Fetches klines for a single symbol."""
        res = self.res_map.get(interval or self.interval, 60)
        v_limit = self._get_valid_limit(limit or self.limit)
        
        url = f"{self.BASE_URL}/exchange/public/md/v2/kline/last"
        params = {
            "symbol": symbol,
            "resolution": int(res),
            "limit": v_limit
        }

        async with self._lock:
            elapsed = time.monotonic() - self._last_request_time
            if elapsed < self.min_interval:
                await asyncio.sleep(self.min_interval - elapsed)
            self._last_request_time = time.monotonic()

        try:
            resp = await self.session.get(url, params=params, timeout=10.0)
            if resp.status_code != 200:
                logger.debug(f"Klines fail for {symbol}: HTTP {resp.status_code}")
                return []
            
            data = ujson.loads(resp.content)
            rows = data.get("data", {}).get("rows", [])
            if not rows:
                return []

            klines = []
            for r in rows:
                # Phemex V2 format: [ts, interval, last_close, open, high, low, close, volume, turnover, ...]
                # Данные приходят уже в виде строк-чисел (human-readable), делить на scale НЕ НУЖНО.
                if len(r) >= 8:
                    klines.append(Kline(
                        timestamp=int(r[0]),
                        open=float(r[3]),
                        high=float(r[4]),
                        low=float(r[5]),
                        close=float(r[6]),
                        volume=float(r[7]) 
                    ))
            
            # Sort by time (asc)
            klines.sort(key=lambda x: x.timestamp)
            return klines
        except Exception as e:
            logger.debug(f"Exception fetching klines for {symbol}: {e}")
            return []

    async def get_all_klines(self, symbols: List[str], interval: Optional[str] = None, limit: Optional[int] = None, save_to_file: Optional[str] = None) -> Dict[str, List[Kline]]:
        """Batch fetches klines for multiple symbols using a semaphore."""
        async def _fetch_one(symbol: str):
            async with self.semaphore:
                data = await self.get_klines(symbol, interval, limit)
                return symbol, data

        tasks = [_fetch_one(s) for s in symbols]
        results = await asyncio.gather(*tasks)
        
        final_map = {sym: klines for sym, klines in results if klines}
        
        if save_to_file:
            # Convert klines to dict for JSON serialization
            serializable = {
                sym: [asdict(k) for k in klist]
                for sym, klist in final_map.items()
            }
            save_json_safe(save_to_file, serializable)
            logger.info(f"✅ Batch klines saved to {save_to_file}")
            
        return final_map

# --- Local Test ---
if __name__ == "__main__":
    async def main():
        api = PhemexKlinesAPI(interval="1m", limit=50)
        try:
            test_symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
            logger.info(f"Fetching klines for {test_symbols}...")
            data = await api.get_all_klines(test_symbols, save_to_file="test_klines.json")
            for sym, klines in data.items():
                if klines:
                    last = klines[-1]
                    logger.info(f"{sym}: Last Close = {last.close} at {last.timestamp}")
        finally:
            await api.aclose()

    # To run this test: PYTHONPATH=. python API/PHEMEX/klines.py
    asyncio.run(main())

# python -m API.PHEMEX.klines
