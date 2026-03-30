import aiohttp
from typing import Dict, Optional

class BinanceTickerAPI:
    BASE_URL = "https://fapi.binance.com"

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
        """Получает горячие цены (last price) по всем символам Binance разом"""
        session = await self._get_session()
        async with session.get(f"{self.BASE_URL}/fapi/v1/ticker/price") as resp:
            resp.raise_for_status()
            data = await resp.json()
            
            if not isinstance(data, list):
                return {}

            result = {}
            for item in data:
                if not isinstance(item, dict):
                    continue
                    
                sym = item.get("symbol")
                raw_price = item.get("price")
                
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
#         api = BinanceTickerAPI()
#         try:
#             prices = await api.get_all_prices()
#             print(f"Получено {len(prices)} тикеров от Binance")
            
#             # Вывести первые 10 пар
#             for i, (symbol, price) in enumerate(prices.items()):
#                 print(f"{symbol}: {price}")
#                 if i >= 9:
#                     break

#             # Получить конкретную цену
#             print(f"\nBTCUSDT: {prices.get('BTCUSDT')}")
#         finally:
#             await api.aclose()

#     asyncio.run(main())