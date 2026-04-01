# ============================================================
# FILE: tools/set_global_leverage.py
# ROLE: Скрипт для принудительной установки плеча по всем монетам
# ============================================================

import asyncio
import os
import sys
import aiohttp
from dotenv import load_dotenv

# Добавляем корень проекта в sys.path, чтобы импорты работали
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from API.PHEMEX.symbol import PhemexSymbols
from API.PHEMEX.order import PhemexPrivateClient

load_dotenv()

TARGET_LEVERAGE = 20.0 # Впиши нужное плечо (или укажи 0 для кросс-маржи)

async def main():
    api_key = os.getenv("API_KEY")
    api_secret = os.getenv("API_SECRET")
    
    if not api_key or not api_secret:
        print("❌ Ошибка: API_KEY или API_SECRET не найдены в .env")
        return

    print(f"🔄 Получение списка активных USDT пар...")
    sym_api = PhemexSymbols()
    symbols_info = await sym_api.get_all(quote="USDT", only_active=True)
    await sym_api.aclose()
    
    symbols = [s.symbol for s in symbols_info]
    print(f"✅ Найдено {len(symbols)} активных пар. Установка плеча {TARGET_LEVERAGE}x...")

    async with aiohttp.ClientSession() as session:
        client = PhemexPrivateClient(api_key, api_secret, session)
        
        success = 0
        failed = 0
        
        for idx, sym in enumerate(symbols):
            try:
                # Отправляем запрос на установку плеча в режиме Hedged
                await client.set_leverage(sym, "Merged", TARGET_LEVERAGE, mode="hedged")
                success += 1
                print(f"[{idx+1}/{len(symbols)}] {sym}: Плечо установлено")
            except Exception as e:
                failed += 1
                print(f"[{idx+1}/{len(symbols)}] {sym}: Скип (ошибка или уже стоит) -> {str(e)[:50]}")
                
            await asyncio.sleep(0.3) # Защита от лимитов API

    print(f"\n🎉 Завершено! Успешно: {success}, Пропущено/Ошибок: {failed}")

# if __name__ == "__main__":
#     if sys.platform == 'win32':
#         asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
#     asyncio.run(main())