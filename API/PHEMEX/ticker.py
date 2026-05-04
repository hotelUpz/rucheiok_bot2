# ============================================================
# python -m API.PHEMEX.ticker
# ROLE: Phemex 24h ticker snapshot (curl_cffi)
# ============================================================
import ujson
from dataclasses import dataclass
from typing import Dict, Optional
from curl_cffi.requests import AsyncSession
from c_log import UnifiedLogger

logger = UnifiedLogger("api")

@dataclass
class TickerData:
    price: float
    fair_price: float
    volume_24h_usd: float

class PhemexTickerAPI:
    BASE_URL = "https://api.phemex.com"

    def __init__(self, session: Optional[AsyncSession] = None):
        self.session = session or AsyncSession(
            impersonate="chrome120",
            http_version=2,
            verify=True
        )

    async def get_all_tickers(self) -> Dict[str, TickerData]:
        url = f"{self.BASE_URL}/md/v3/ticker/24hr/all"
        try:
            resp = await self.session.get(url, timeout=10.0)
            if resp.status_code != 200:
                logger.error(f"Ticker fetch error: HTTP {resp.status_code}")
                return {}
                
            data = ujson.loads(resp.content)
            items = data.get("result", [])
            
            result: Dict[str, TickerData] = {}
            for item in items:
                if not isinstance(item, dict): continue
                sym = item.get("symbol")
                if not sym: continue
                
                # Поля из MD V3: lastRp (hot), markRp (fair), turnoverRv (volume)
                raw_price = item.get("lastRp")
                raw_fair = item.get("markRp")
                raw_volume = item.get("turnoverRv") or "0"
                
                try:
                    price = float(raw_price) if raw_price else 0.0
                    fair = float(raw_fair) if raw_fair else 0.0
                    volume = float(raw_volume)
                    
                    if price > 0:
                        result[sym] = TickerData(
                            price=price, 
                            fair_price=fair,
                            volume_24h_usd=volume
                        )
                except (ValueError, TypeError):
                    continue
            return result
            
        except Exception as e:
            logger.error(f"Error fetching tickers: {e}")
            return {}

    async def get_all_prices(self) -> Dict[str, float]:
        tickers = await self.get_all_tickers()
        return {sym: t.price for sym, t in tickers.items()}

    async def aclose(self):
        await self.session.close()

if __name__ == "__main__":
    import asyncio
    async def test():
        api = PhemexTickerAPI()
        try:
            tickers = await api.get_all_tickers()
            print(f"Fetched {len(tickers)} tickers from Phemex")
            
            for i, (sym, data) in enumerate(tickers.items()):
                print(f"{sym}: Hot={data.price} | Fair={data.fair_price} | Vol={data.volume_24h_usd}")
                if i >= 9:
                    break

            if "BTCUSDT" in tickers:
                print(f"\nBTCUSDT Data: {tickers['BTCUSDT']}")
        finally:
            await api.aclose()
    asyncio.run(test())

# python -m API.PHEMEX.ticker

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