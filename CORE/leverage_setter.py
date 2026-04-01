# ============================================================
# FILE: CORE/leverage_setter.py
# ROLE: Глобальная пред-установка плеча и маржи (с JSON кешем)
# ============================================================

import os
import json
import asyncio
import aiohttp
from typing import List, Dict, Optional, Any
from pathlib import Path

from API.PHEMEX.symbol import PhemexSymbols, SymbolInfo
from API.PHEMEX.order import PhemexPrivateClient
from c_log import UnifiedLogger

logger = UnifiedLogger("setup")

class GlobalLeverageSetter:
    def __init__(
        self, 
        api_key: str, 
        api_secret: str, 
        leverage_val: Optional[float],
        margin_mode: int,
        black_list: List[str],
        use_cache: bool,
        cache_path: str | Path,
        delay_sec: float = 0.3
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.leverage_val = leverage_val
        self.margin_mode = margin_mode
        self.black_list = set(black_list)
        self.use_cache = use_cache
        self.cache_path = Path(cache_path)
        self.delay_sec = delay_sec

    def _load_cache(self) -> Dict[str, Any]:
        """Считываем словарь. По дефолту пустой."""
        if not self.use_cache or not self.cache_path.exists():
            return {}
        try:
            with open(self.cache_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Ошибка чтения {self.cache_path.name}: {e}")
            return {}

    def _save_cache(self, data: Dict[str, Any]) -> None:
        """Сохраняем финальный словарь на диск."""
        if not self.use_cache:
            return
        try:
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)
        except Exception as e:
            logger.error(f"Ошибка записи {self.cache_path.name}: {e}")

    async def apply(self) -> None:
        if not self.api_key or not self.api_secret:
            logger.error("❌ Отсутствуют API ключи для установки параметров.")
            return

        logger.info("🔄 Загрузка спецификаций с Phemex для настройки плеча/маржи...")
        sym_api = PhemexSymbols()
        try:
            symbols_info: List[SymbolInfo] = await sym_api.get_all(quote="USDT", only_active=True)
        finally:
            await sym_api.aclose()

        if not symbols_info:
            logger.warning("⚠️ Не удалось получить список символов.")
            return

        # Транзакционный словарь в памяти
        current_cache = self._load_cache()
        new_cache = current_cache.copy()

        async with aiohttp.ClientSession() as session:
            client = PhemexPrivateClient(self.api_key, self.api_secret, session, retries=1)
            
            success_count = 0
            skipped_count = 0
            
            for spec in symbols_info:
                sym = spec.symbol

                # Скип ЧС
                if sym in self.black_list:
                    skipped_count += 1
                    continue

                # Скип, если юзаем кэш и монета уже там (даже с фейлом - None)
                if self.use_cache and sym in current_cache:
                    skipped_count += 1
                    continue

                try:
                    # 1. Всегда ставим Margin Mode и Pos Mode (Hedged = 2)
                    await client.set_margin_mode(sym, margin_mode=self.margin_mode, pos_mode=2)
                    
                    # 2. Ставим плечо, если задано (не null)
                    actual_leverage = None
                    if self.leverage_val is not None:
                        actual_leverage = min(self.leverage_val, spec.max_leverage)
                        await client.set_leverage(sym, "Merged", actual_leverage, mode="hedged")
                        logger.debug(f"[{sym}] Успешно: Mode={self.margin_mode}, Lev={actual_leverage}x (Max:{spec.max_leverage}x)")
                    else:
                        logger.debug(f"[{sym}] Успешно: Mode={self.margin_mode} (Плечо пропущено - None)")

                    new_cache[sym] = actual_leverage
                    success_count += 1

                except Exception as e:
                    err_msg = str(e).lower()
                    # Если биржа говорит, что "has no change" - считаем это успехом
                    if "has no change" in err_msg or "same" in err_msg:
                        actual_leverage = min(self.leverage_val, spec.max_leverage) if self.leverage_val is not None else None
                        new_cache[sym] = actual_leverage
                        success_count += 1
                    else:
                        logger.error(f"[{sym}] Ошибка настройки: {str(e)[:100]}")
                        # Фейлы кешируем как None
                        new_cache[sym] = None
                        
                await asyncio.sleep(self.delay_sec)

        # Сохраняем в конце
        self._save_cache(new_cache)
        logger.info(f"✅ Настройка завершена. Обработано: {success_count}, Пропущено (Кэш/ЧС): {skipped_count}")