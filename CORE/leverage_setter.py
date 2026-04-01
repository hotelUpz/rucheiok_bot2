# ============================================================
# FILE: CORE/leverage_setter.py
# ROLE: Массовая установка Margin Mode и Leverage перед стартом
# ============================================================

import asyncio
import aiohttp
from typing import Set, List
from API.PHEMEX.symbol import PhemexSymbols, SymbolInfo
from API.PHEMEX.order import PhemexPrivateClient
from c_log import UnifiedLogger

logger = UnifiedLogger("setup")

class GlobalLeverageSetter:
    def __init__(
        self, 
        api_key: str, 
        api_secret: str, 
        leverage_val: float | None,
        margin_mode: int,
        black_list: List[str],
        use_cache: bool,
        cache: Set[str],
        delay_sec: float = 0.3
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.leverage_val = leverage_val
        self.margin_mode = margin_mode
        self.black_list = set(black_list)
        self.use_cache = use_cache
        self.cache = cache
        self.delay_sec = delay_sec

    async def apply(self):
        if not self.api_key or not self.api_secret:
            logger.error("❌ Отсутствуют API ключи для установки параметров.")
            return

        logger.info("🔄 Загрузка актуальных спецификаций с Phemex...")
        sym_api = PhemexSymbols()
        try:
            symbols_info: List[SymbolInfo] = await sym_api.get_all(quote="USDT", only_active=True)
        finally:
            await sym_api.aclose()

        if not symbols_info:
            logger.warning("⚠️ Не удалось получить список символов для настройки плеча.")
            return

        async with aiohttp.ClientSession() as session:
            client = PhemexPrivateClient(self.api_key, self.api_secret, session, retries=1)
            
            success_count = 0
            skipped_count = 0
            
            for spec in symbols_info:
                sym = spec.symbol

                if sym in self.black_list:
                    skipped_count += 1
                    continue

                if self.use_cache and sym in self.cache:
                    skipped_count += 1
                    continue

                try:
                    # 1. Сначала устанавливаем режим маржи (1: Isolated, 2: Cross) и Hedge Mode (2)
                    await client.set_margin_mode(sym, margin_mode=self.margin_mode, pos_mode=2)
                    
                    # 2. Если плечо задано, устанавливаем его с учетом лимитов биржи
                    if self.leverage_val is not None:
                        # Берем минимальное между конфигом и максимумом биржи
                        actual_leverage = min(self.leverage_val, spec.max_leverage)
                        
                        # Для кросс-маржи на Phemex плечо часто передается как 0
                        # Если требуется передавать 0 для кросс-маржи, раскомментируй строку ниже:
                        # if self.margin_mode == 2: actual_leverage = 0.0

                        await client.set_leverage(sym, "Merged", actual_leverage, mode="hedged")
                        logger.debug(f"[{sym}] Успешно: MarginMode={self.margin_mode}, Lev={actual_leverage}x (Max:{spec.max_leverage}x)")
                    else:
                        logger.debug(f"[{sym}] Успешно: MarginMode={self.margin_mode} (Плечо скипнуто - null)")

                    # Записываем в кэш
                    self.cache.add(sym)
                    success_count += 1

                except Exception as e:
                    err_msg = str(e)
                    # Игнорируем ошибки, если режим уже установлен
                    if "has no change" in err_msg.lower() or "same" in err_msg.lower():
                        self.cache.add(sym)
                        success_count += 1
                    else:
                        logger.error(f"[{sym}] Ошибка настройки: {err_msg[:100]}")
                        
                await asyncio.sleep(self.delay_sec)

        logger.info(f"✅ Настройка завершена. Обновлено: {success_count}, Пропущено (ЧС/Кэш): {skipped_count}")