import aiohttp
from typing import Dict, Optional

class PhemexTickerAPI:
    BASE_URL = "https://api.phemex.com"

    def __init__(self, timeout_sec: float = 10.0):
        self._timeout = aiohttp.ClientTimeout(total=timeout_sec)
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(enable_cleanup_closed=True)
            self._session = aiohttp.ClientSession(timeout=self._timeout, connector=connector)
        return self._session

    async def aclose(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def get_all_prices(self) -> Dict[str, float]:
        """Получает горячие цены (Real Price) по всем монетам Phemex (v3 API)"""
        session = await self._get_session()
        async with session.get(f"{self.BASE_URL}/md/v3/ticker/24hr/all") as resp:
            resp.raise_for_status()
            data = await resp.json()

            items = data.get("result", [])
            if not isinstance(items, list):
                return {}

            result = {}
            for item in items:
                if not isinstance(item, dict):
                    continue
                
                sym = item.get("symbol")
                raw_price = item.get("lastRp") or item.get("lastPriceRp") or item.get("lastPrice")
                
                if sym and raw_price is not None:
                    try:
                        price = float(raw_price)
                        if price > 0:
                            result[sym] = price
                    except (ValueError, TypeError):
                        continue
                        
            return result

# # --- Блок для локального тестирования ---
# if __name__ == "__main__":
#     import asyncio

#     async def main():
#         api = PhemexTickerAPI()
#         try:
#             prices = await api.get_all_prices()
#             print(f"Получено {len(prices)} тикеров от Phemex")
            
#             # Выведем первые 10 тикеров для проверки
#             for i, (sym, price) in enumerate(prices.items()):
#                 print(f"{sym}: {price}")
#                 if i >= 9:
#                     break
#         finally:
#             await api.aclose()

#     asyncio.run(main())