import asyncio
import json
from curl_cffi.requests import AsyncSession

async def test():
    session = AsyncSession(impersonate="chrome120")
    url = 'https://api.dexscreener.com/latest/dex/search?q=BEAT'
    resp = await session.get(url)
    if resp.status_code == 200:
        data = json.loads(resp.content)
        for p in data.get("pairs", [])[:20]:
            symbol = p.get("baseToken", {}).get("symbol")
            price = p.get("priceUsd")
            liq = p.get("liquidity", {}).get("usd")
            dex = p.get("dexId")
            addr = p.get("pairAddress")
            print(f"{dex} | {symbol} | {price} | Liq: {liq} | Addr: {addr}")
    else:
        print(f"Error: {resp.status_code}")
    await session.close()

if __name__ == "__main__":
    asyncio.run(test())
