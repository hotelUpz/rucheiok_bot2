# ============================================================
# FILE: API/BINANCE/funding.py
# ROLE: Binance USDT-M Futures funding via REST (curl_cffi)
# ENDPOINT: GET https://fapi.binance.com/fapi/v1/premiumIndex
# ============================================================
import ujson
import asyncio
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from curl_cffi.requests import AsyncSession
from c_log import UnifiedLogger

logger = UnifiedLogger("api")

@dataclass(frozen=True)
class FundingInfo:
    symbol: str
    funding_rate: float
    next_funding_time_ms: int

@dataclass(frozen=True)
class FundingIntervalInfo:
    symbol: str
    interval_hours: int

class BinanceFunding:
    """Public funding REST client using curl_cffi."""

    BASE_URL = "https://fapi.binance.com"

    def __init__(self, session: Optional[AsyncSession] = None):
        self.session = session or AsyncSession(
            impersonate="chrome120",
            http_version=2,
            verify=True
        )

    async def aclose(self) -> None:
        await self.session.close()

    async def _get_json(self, path: str, params: Optional[dict] = None) -> Any:
        url = f"{self.BASE_URL}{path}"
        try:
            resp = await self.session.get(url, params=params, timeout=15.0)
            if resp.status_code != 200:
                logger.error(f"Binance funding error: HTTP {resp.status_code} path={path}")
                return None
            return ujson.loads(resp.content)
        except Exception as e:
            logger.error(f"Binance funding exception: {e} path={path}")
            return None

    @staticmethod
    def _to_float(v: Any, default: float = 0.0) -> float:
        try:
            return float(v)
        except Exception:
            return default

    @staticmethod
    def _to_int(v: Any, default: int = 0) -> int:
        try:
            return int(v)
        except Exception:
            return default

    def _parse_one(self, obj: Dict[str, Any]) -> Optional[FundingInfo]:
        sym = obj.get("symbol")
        if not sym:
            return None
        # premiumIndex uses lastFundingRate + nextFundingTime
        return FundingInfo(
            symbol=str(sym),
            funding_rate=self._to_float(obj.get("lastFundingRate"), 0.0),
            next_funding_time_ms=self._to_int(obj.get("nextFundingTime"), 0),
        )

    def _parse_interval_one(self, obj: Dict[str, Any]) -> Optional[FundingIntervalInfo]:
        sym = obj.get("symbol")
        if not sym:
            return None
        interval_h = self._to_int(obj.get("fundingIntervalHours"), 0)
        if interval_h <= 0:
            return None
        return FundingIntervalInfo(symbol=str(sym), interval_hours=interval_h)

    async def get_all(self) -> List[FundingInfo]:
        data = await self._get_json("/fapi/v1/premiumIndex", params=None)
        out: List[FundingInfo] = []

        if isinstance(data, list):
            for obj in data:
                if isinstance(obj, dict):
                    fi = self._parse_one(obj)
                    if fi:
                        out.append(fi)
        elif isinstance(data, dict):
            fi = self._parse_one(data)
            if fi:
                out.append(fi)

        return out

    async def get_one(self, symbol: str) -> Optional[FundingInfo]:
        sym = (symbol or "").upper().strip()
        if not sym:
            return None
        data = await self._get_json("/fapi/v1/premiumIndex", params={"symbol": sym})
        if isinstance(data, dict):
            return self._parse_one(data)
        return None

    async def get_interval_overrides(self) -> Dict[str, int]:
        """Return symbol -> fundingIntervalHours overrides."""
        data = await self._get_json("/fapi/v1/fundingInfo", params=None)
        out: Dict[str, int] = {}

        if isinstance(data, list):
            for obj in data:
                if not isinstance(obj, dict):
                    continue
                row = self._parse_interval_one(obj)
                if row is None:
                    continue
                out[str(row.symbol).upper().strip()] = int(row.interval_hours)
        elif isinstance(data, dict):
            row = self._parse_interval_one(data)
            if row is not None:
                out[str(row.symbol).upper().strip()] = int(row.interval_hours)

        return out

# ----------------------------
# SELF TEST
# ----------------------------
if __name__ == "__main__":
    async def _main():
        api = BinanceFunding()
        try:
            rows = await api.get_all()
            print(f"Funding rows: {len(rows)}")
            top = sorted(rows, key=lambda r: abs(r.funding_rate), reverse=True)[:20]
            for i, r in enumerate(top, 1):
                print(f"{i:02d}. {r.symbol:<12} rate={r.funding_rate:+.6f} next={r.next_funding_time_ms}")
            print("BTCUSDT:", await api.get_one("BTCUSDT"))
        finally:
            await api.aclose()

    asyncio.run(_main())

# python -m API.BINANCE.funding