# ============================================================
# FILE: API/PHEMEX/funding.py
# ROLE: Phemex USDT-M Perpetual funding via REST (public)
# ENDPOINT: GET https://api.phemex.com/contract-biz/public/real-funding-rates
# NOTES:
#   Phemex paginates this endpoint. To fetch ALL symbols you must iterate pages.
# ROLE: Phemex USDT-M Perpetual funding via REST (curl_cffi)
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

class PhemexFunding:
    """Public funding client for Phemex contracts using curl_cffi."""

    BASE_URL = "https://api.phemex.com"

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
                logger.error(f"Phemex funding error: HTTP {resp.status_code} path={path}")
                return None
            return ujson.loads(resp.content)
        except Exception as e:
            logger.error(f"Phemex funding exception: {e} path={path}")
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
        return FundingInfo(
            symbol=str(sym).upper(),
            funding_rate=self._to_float(obj.get("fundingRate"), 0.0),
            next_funding_time_ms=self._to_int(obj.get("nextFundingTime") or obj.get("nextfundingTime"), 0),
        )

    @staticmethod
    def _extract_rows(payload: Any) -> List[Dict[str, Any]]:
        if not payload: return []
        if isinstance(payload, list):
            return [x for x in payload if isinstance(x, dict)]

        if not isinstance(payload, dict):
            return []

        data = payload.get("data")
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict):
            for k in ("rows", "result", "list"):
                v = data.get(k)
                if isinstance(v, list):
                    return [x for x in v if isinstance(x, dict)]

        for k in ("rows", "result", "list"):
            v = payload.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]

        return []

    async def get_all(self) -> List[FundingInfo]:
        out: List[FundingInfo] = []
        page_num = 1
        page_size = 200

        while True:
            payload = await self._get_json(
                "/contract-biz/public/real-funding-rates",
                params={"symbol": "ALL", "pageNum": page_num, "pageSize": page_size},
            )
            rows = self._extract_rows(payload)
            if not rows:
                break

            for obj in rows:
                fi = self._parse_one(obj)
                if fi:
                    out.append(fi)

            if len(rows) < page_size:
                break
            page_num += 1

        return out

# ----------------------------
# SELF TEST
# ----------------------------
if __name__ == "__main__":
    async def _main():
        api = PhemexFunding()
        try:
            rows = await api.get_all()
            print(f"Funding rows: {len(rows)}")
            for r in rows[:15]:
                print(r)
        finally:
            await api.aclose()

    asyncio.run(_main())

# python -m API.PHEMEX.funding