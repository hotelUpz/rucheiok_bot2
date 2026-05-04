# ============================================================
# FILE: API/BINANCE/ticker.py
# ROLE: Binance 24h ticker snapshot (curl_cffi)
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

class BinanceTickerAPI:
    BASE_URL = "https://fapi.binance.com"

    def __init__(self, session: Optional[AsyncSession] = None):
        self.session = session or AsyncSession(
            impersonate="chrome120",
            http_version=2,
            verify=True
        )

    async def aclose(self):
        await self.session.close()

    async def get_all_tickers(self) -> Dict[str, TickerData]:
        """Получает горячие и справедливые цены Binance"""
        import asyncio
        url_price = f"{self.BASE_URL}/fapi/v1/ticker/price"
        url_fair = f"{self.BASE_URL}/fapi/v1/premiumIndex"
        
        try:
            res_p, res_f = await asyncio.gather(
                self.session.get(url_price, timeout=10.0),
                self.session.get(url_fair, timeout=10.0)
            )
            
            if res_p.status_code != 200 or res_f.status_code != 200:
                logger.error(f"Binance ticker error: HTTP {res_p.status_code}/{res_f.status_code}")
                return {}
                
            data_p = ujson.loads(res_p.content)
            data_f = ujson.loads(res_f.content)

            fair_map = {}
            if isinstance(data_f, list):
                for item in data_f:
                    sym = item.get("symbol")
                    mark = item.get("markPrice")
                    if sym and mark:
                        fair_map[sym] = float(mark)

            result = {}
            if isinstance(data_p, list):
                for item in data_p:
                    sym = item.get("symbol")
                    raw_price = item.get("price")
                    if sym and raw_price:
                        try:
                            price = float(raw_price)
                            fair = fair_map.get(sym, price) # fallback to price if no fair price
                            if price > 0:
                                result[sym] = TickerData(
                                    price=price,
                                    fair_price=fair,
                                    volume_24h_usd=0.0 # volume requires 3rd request or 24hr ticker
                                )
                        except (ValueError, TypeError):
                            continue
            return result
        except Exception as e:
            logger.error(f"Error fetching Binance tickers: {e}")
            return {}

    async def get_all_prices(self) -> Dict[str, float]:
        """Возвращает горячие цены (last price) по всем символам Binance разом"""
        tickers = await self.get_all_tickers()
        return {sym: t.price for sym, t in tickers.items()}

# --- Блок для локального тестирования ---
if __name__ == "__main__":
    import asyncio

    async def main():
        api = BinanceTickerAPI()
        try:
            tickers = await api.get_all_tickers()
            print(f"Получено {len(tickers)} тикеров от Binance")
            
            # Вывести первые 10 пар
            for i, (symbol, data) in enumerate(tickers.items()):
                print(f"{symbol}: Hot={data.price} | Fair={data.fair_price}")
                if i >= 9:
                    break

            # Получить конкретный тикер
            btc = tickers.get('BTCUSDT')
            if btc:
                print(f"\nBTCUSDT Data: {btc}")
        finally:
            await api.aclose()

    asyncio.run(main())
    
# python -m API.BINANCE.ticker