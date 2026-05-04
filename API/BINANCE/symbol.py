# ============================================================
# FILE: API/BINANCE/symbol.py
# ROLE: Load Binance USDT-M Futures PERPETUAL symbols via REST (curl_cffi)
# ENDPOINT: GET https://fapi.binance.com/fapi/v1/exchangeInfo
# ============================================================
import ujson
import asyncio
from typing import Any, Dict, List, Optional
from curl_cffi.requests import AsyncSession
from c_log import UnifiedLogger

logger = UnifiedLogger("api")

class BinanceSymbols:
    """Public symbols loader for Binance USDT-M Futures using curl_cffi.

    Filters:
        - contractType == "PERPETUAL"
        - status == "TRADING"
        - quoteAsset == quote (default: "USDT")

    Returns:
        list[str] of raw Binance symbols, e.g. "BTCUSDT"
    """

    BASE_URL = "https://fapi.binance.com"

    def __init__(self, session: Optional[AsyncSession] = None):
        self.session = session or AsyncSession(
            impersonate="chrome120",
            http_version=2,
            verify=True
        )

    async def aclose(self) -> None:
        await self.session.close()

    async def _get_json(self, path: str, params: Optional[dict] = None) -> Dict[str, Any]:
        url = f"{self.BASE_URL}{path}"
        try:
            resp = await self.session.get(url, params=params, timeout=15.0)
            if resp.status_code != 200:
                logger.error(f"Binance symbols error: HTTP {resp.status_code} path={path}")
                return {}
            data = ujson.loads(resp.content)
            if not isinstance(data, dict):
                logger.error(f"Bad JSON root for Binance symbols: {type(data)}")
                return {}
            return data
        except Exception as e:
            logger.error(f"Binance symbols exception: {e} path={path}")
            return {}

    async def get_perp_symbols(self, quote: str = "USDT", limit: Optional[int] = None) -> List[str]:
        data = await self._get_json("/fapi/v1/exchangeInfo")
        quote_u = (quote or "USDT").upper()
        out: List[str] = []

        symbols = data.get("symbols", [])
        if not isinstance(symbols, list):
            return []

        for s in symbols:
            if not isinstance(s, dict):
                continue
            if s.get("contractType") != "PERPETUAL":
                continue
            if s.get("status") != "TRADING":
                continue
            if (s.get("quoteAsset") or "").upper() != quote_u:
                continue
            sym = s.get("symbol")
            if sym:
                out.append(str(sym))

        out.sort()
        if limit is not None:
            return out[: int(limit)]
        return out

# ----------------------------
# SELF TEST
# ----------------------------
if __name__ == "__main__":
    async def _main():
        api = BinanceSymbols()
        try:
            syms = await api.get_perp_symbols("USDT")
            print(f"BINANCE PERP USDT symbols: {len(syms)}")
            print("first 30:", syms[:30])
        finally:
            await api.aclose()

    asyncio.run(_main())

# python -m API.BINANCE.symbol