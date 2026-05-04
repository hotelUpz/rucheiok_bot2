import urllib.request
import json

def test():
    url = 'https://api.dexscreener.com/latest/dex/search?q=BEAT'
    with urllib.request.urlopen(url) as r:
        data = json.loads(r.read())
        for p in data.get("pairs", [])[:20]:
            symbol = p.get("baseToken", {}).get("symbol")
            price = p.get("priceUsd")
            liq = p.get("liquidity", {}).get("usd")
            dex = p.get("dexId")
            addr = p.get("pairAddress")
            print(f"{dex} | {symbol} | {price} | Liq: {liq} | Addr: {addr}")

if __name__ == "__main__":
    test()
